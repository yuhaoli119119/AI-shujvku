"""Preview or explicitly migrate catalogue codes after a database backup.

Run inside backend runtime: python scripts/reassign_paper_codes.py --output /app/outputs/<run>/numbering.json
Then same command with --apply --backup /app/outputs/<run>/database.dump.
Default never writes database. Output must be an unused project-local filename.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select, text
from app.config import get_settings
from app.db.models import Paper, PaperRelationship
from app.db.session import session_scope
from app.services.paper_codes import _renumber, lock_catalog_numbering, numbering_plan


def migrate_constraints(session):
    connection = session.connection()
    # PostgreSQL may represent the legacy global uniqueness as a constraint or index.
    constraints = connection.execute(text("""
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid='papers'::regclass AND c.contype='u'
        AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='papers'::regclass AND attname='paper_code')]::smallint[]
    """)).scalars().all()
    quote = connection.dialect.identifier_preparer.quote
    for name in constraints:
        connection.execute(text(f'ALTER TABLE papers DROP CONSTRAINT {quote(name)}'))
    indexes = connection.execute(text("""
        SELECT indexrelid::regclass::text FROM pg_index
        WHERE indrelid='papers'::regclass AND indisunique AND indnkeyatts=1
        AND indkey[0]=(SELECT attnum FROM pg_attribute WHERE attrelid='papers'::regclass AND attname='paper_code')
    """)).scalars().all()
    for name in indexes:
        connection.execute(text('DROP INDEX ' + '.'.join(quote(part) for part in name.split('.'))))


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    project = Path(__file__).resolve().parents[1]
    if project.name == "backend":
        project = project.parent
    # Containers mount persistent outputs at /app/outputs; source project also accepted.
    if not output.is_relative_to(project) and not output.is_relative_to(Path('/opt/literature-ai').resolve()):
        parser.error('output must be within the server project')
    if output.exists():
        parser.error('output already exists; use a new artifact path')
    backup = Path(args.backup).resolve() if args.backup else None
    if args.apply and (backup is None or not backup.is_file() or backup.stat().st_size == 0):
        parser.error('--apply requires an existing nonempty database backup')
    with session_scope(get_settings().database_url) as session:
        if args.apply:
            lock_catalog_numbering(session)
            session.execute(text('LOCK TABLE papers, paper_relationships IN ACCESS EXCLUSIVE MODE'))
        papers = session.scalars(select(Paper).order_by(Paper.created_at, Paper.id)).all()
        relationships = session.scalars(select(PaperRelationship)).all()
        plan = numbering_plan(papers, relationships)
        receipt = {'mode': 'apply' if args.apply else 'preview', 'mapping': plan,
                   'paper_count': len(papers), 'main_count': sum(entry['new_serial'] is not None for entry in plan),
                   'relationship_count': len(relationships)}
        if backup:
            receipt['backup'] = str(backup)
            digest = hashlib.sha256()
            with backup.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            receipt['backup_sha256'] = digest.hexdigest()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Write exact old/new map before mutation, also retained if the transaction fails.
        with output.open('x') as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2)
        if args.apply:
            migrate_constraints(session)
            _renumber(session, {paper.library_name for paper in papers}, reason='numbering_migration')
            session.flush()
            for name, column in (('uq_papers_library_code', 'paper_code'),
                                 ('uq_papers_library_main_serial', 'serial_number')):
                exists = session.scalar(text("SELECT 1 FROM pg_constraint WHERE conrelid='papers'::regclass AND conname=:name"), {'name': name})
                if not exists:
                    session.execute(text(f'ALTER TABLE papers ADD CONSTRAINT {name} UNIQUE (library_name, {column})'))
    print(json.dumps({'mode': receipt['mode'], 'main_count': receipt['main_count'], 'papers': receipt['paper_count'], 'output': str(output)}))

if __name__ == '__main__':
    run()
