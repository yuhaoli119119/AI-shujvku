"""One transactional catalogue numbering authority; identifiers never replace UUIDs."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import re
from typing import Iterable
from uuid import UUID

from sqlalchemy import event, exists, inspect, or_, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from app.db.models import AuditLog, Paper, PaperRelationship

PAPER_CODE_RE = re.compile(r"^([A-Z])(\d+)$")
SUPPLEMENTARY_MAIN_CODE_RE = PAPER_CODE_RE
SUPPLEMENTARY_CODE_RE = re.compile(r"^S(\d+)(?:-(\d+))?$")
SUPPLEMENTARY_TYPES = frozenset({"supplementary", "supplementary_information", "supporting_information", "si"})


def supplementary_paper_clause():
    from sqlalchemy import func
    return or_(func.lower(func.trim(func.coalesce(Paper.paper_type, ""))).in_(SUPPLEMENTARY_TYPES),
        exists().where(PaperRelationship.target_paper_id == Paper.id,
                       PaperRelationship.relationship_type.in_(SUPPLEMENTARY_TYPES)))


def main_paper_clause():
    return ~supplementary_paper_clause()


def is_supplementary(paper: Paper) -> bool:
    return str(paper.paper_type or "").strip().lower() in SUPPLEMENTARY_TYPES


def paper_code_prefix(paper_type: str | None) -> str:
    value = str(paper_type or "").strip()
    for prefix, terms in (("A", ("comput", "dft")), ("B", ("mixed", "hybrid")),
                          ("C", ("experim",)), ("R", ("review",))):
        if value.upper().startswith(prefix) or any(term in value.lower() for term in terms):
            return prefix
    return "U"


def format_paper_code(prefix: str, number: int) -> str:
    return f"{prefix}{number:03d}"


def supplementary_base_code(main_paper_code: str | None = None, serial_number: int | None = None) -> str:
    match = PAPER_CODE_RE.match(str(main_paper_code or "").strip().upper())
    number = int(match.group(2)) if match else serial_number
    if number is None:
        raise ValueError("supplementary_code_requires_main_code_or_serial")
    return format_paper_code("S", number)


def lock_catalog_numbering(session: Session) -> None:
    # Also covers cross-library moves and relationship reassignment without lock-order cycles.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(184735, 1)"))


def numbering_plan(papers: Iterable[Paper], relationships: Iterable[PaperRelationship]) -> list[dict]:
    papers = list(papers)
    by_id = {paper.id: paper for paper in papers}
    owner = {}
    for relationship in relationships:
        if relationship.relationship_type not in SUPPLEMENTARY_TYPES:
            continue
        if relationship.target_paper_id in owner and owner[relationship.target_paper_id] != relationship.source_paper_id:
            raise ValueError("supplementary_has_multiple_main_owners")
        owner[relationship.target_paper_id] = relationship.source_paper_id
    libraries = defaultdict(list)
    for paper in papers:
        if not str(paper.library_name or "").strip():
            raise ValueError("paper_library_required_for_numbering")
        libraries[paper.library_name].append(paper)
    order = lambda paper: (paper.created_at or datetime.min, str(paper.id))
    result = {}
    for library, rows in libraries.items():
        mains = sorted((paper for paper in rows if not is_supplementary(paper) and paper.id not in owner), key=order)
        for number, paper in enumerate(mains, 1):
            result[paper.id] = (format_paper_code(paper_code_prefix(paper.paper_type), number), number)
        attachments = defaultdict(list)
        for paper in rows:
            if is_supplementary(paper) or paper.id in owner:
                main = by_id.get(owner.get(paper.id))
                if main is None:
                    result[paper.id] = (None, None)  # pending association; never guess an owner
                    continue
                if main.library_name != library or main.id not in result:
                    raise ValueError("supplementary_owner_must_be_main_in_same_library")
                attachments[main.id].append(paper)
        for main_id, children in attachments.items():
            base = supplementary_base_code(result[main_id][0])
            for index, child in enumerate(sorted(children, key=order), 1):
                result[child.id] = (base if index == 1 else f"{base}-{index}", None)
    return [{"paper_id": str(paper.id), "library_name": paper.library_name,
             "old_code": paper.paper_code, "new_code": result[paper.id][0],
             "old_serial": paper.serial_number, "new_serial": result[paper.id][1]}
            for paper in sorted(papers, key=lambda paper: (str(paper.library_name), *order(paper)))]


def _renumber(session: Session, libraries: set[str], *, reason="catalog_mutation", originals=None) -> dict[str, str]:
    if not libraries:
        return {}
    connection = session.connection()
    papers = list(session.scalars(select(Paper).where(Paper.library_name.in_(libraries)).execution_options(populate_existing=True)).all())
    ids = [paper.id for paper in papers]
    relationships = session.scalars(select(PaperRelationship).where(PaperRelationship.target_paper_id.in_(ids))).all()
    plan = numbering_plan(papers, relationships)
    by_id = {str(paper.id): paper for paper in papers}
    changed = [entry for entry in plan if (entry['old_code'], entry['old_serial']) != (entry['new_code'], entry['new_serial'])]
    # Two stages avoid immediate unique-index collisions when the middle of a library moves.
    if changed:
        connection.execute(update(Paper.__table__).where(Paper.id.in_([UUID(entry['paper_id']) for entry in changed]))
                           .values(paper_code=None, serial_number=None))
    assigned = {}
    for entry in changed:
        paper = by_id[entry['paper_id']]
        connection.execute(update(Paper.__table__).where(Paper.id == paper.id)
                           .values(paper_code=entry['new_code'], serial_number=entry['new_serial']))
        set_committed_value(paper, 'paper_code', entry['new_code'])
        set_committed_value(paper, 'serial_number', entry['new_serial'])
        original = (originals or {}).get(paper.id)
        if original:
            entry = {**entry, 'old_code': original[0], 'old_serial': original[1], 'old_library_name': original[2]}
        session.add(AuditLog(paper_id=paper.id, action='paper_code_reassigned', source=reason,
                            target_type='papers', target_id=str(paper.id), payload=entry))
        assigned[str(paper.id)] = entry['new_code']
    return assigned


def ensure_paper_codes(session: Session, papers: Iterable[Paper] | None = None) -> dict[str, str]:
    lock_catalog_numbering(session)
    selected = list(papers) if papers is not None else list(session.scalars(select(Paper)).all())
    session.flush()
    return _renumber(session, {paper.library_name for paper in selected})


def next_supplementary_paper_code(session: Session, *, main_paper_code=None, serial_number=None,
                                  exclude_paper_id: UUID | None = None, library_name=None) -> str:
    lock_catalog_numbering(session)
    if library_name is None and exclude_paper_id:
        paper = session.get(Paper, exclude_paper_id)
        library_name = paper.library_name if paper else None
    if library_name is None:
        raise ValueError('supplementary_code_requires_library')
    base = supplementary_base_code(main_paper_code, serial_number)
    codes = session.execute(select(Paper.paper_code, Paper.id).where(Paper.library_name == library_name,
                                                                   Paper.paper_code.like(base + '%'))).all()
    occupied = {int(match.group(2) or 1) for code, paper_id in codes
                if paper_id != exclude_paper_id and (match := SUPPLEMENTARY_CODE_RE.match(code or ''))
                and int(match.group(1)) == int(base[1:])}
    number = max(occupied, default=0) + 1
    return base if number == 1 else f'{base}-{number}'


def backfill_paper_codes_detached(session: Session, papers=None) -> dict[str, str]:
    # GET is read-only: migration and mutation transactions are the only numbering writers.
    return {}


@event.listens_for(Session, 'before_flush')
def _before_catalog_flush(session, flush_context, instances):
    libraries = session.info.setdefault('catalog_numbering_libraries', set())
    originals = session.info.setdefault('catalog_numbering_originals', {})
    pending = list(session.new) + list(session.dirty) + list(session.deleted)
    seen = set()
    with session.no_autoflush:
        while pending:
            obj = pending.pop(0)
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if isinstance(obj, Paper):
                state = inspect(obj)
                if obj in session.new and not obj.library_name:
                    from app.utils.library_names import normalize_library_name
                    obj.library_name = normalize_library_name(None)
                relevant = obj in session.new or obj in session.deleted or any(
                    state.attrs[key].history.has_changes() for key in ('paper_type', 'library_name', 'created_at', 'paper_code', 'serial_number'))
                if not relevant:
                    continue
                lock_catalog_numbering(session)
                # An expired ORM attribute can have no deleted history after assignment.
                # Read the persisted identity before flush to always close the old library gap.
                previous = None if obj in session.new else session.connection().execute(
                    select(Paper.paper_code, Paper.serial_number, Paper.library_name).where(Paper.id == obj.id)).first()
                old_library = previous[2] if previous else obj.library_name
                libraries.update(filter(None, (old_library, obj.library_name)))
                moved = previous is not None and old_library != obj.library_name
                if obj in session.new or moved:
                    if previous:
                        originals[obj.id] = tuple(previous)
                    obj.paper_code = None
                    obj.serial_number = None
                if moved and not is_supplementary(obj):
                    child_ids = session.scalars(select(PaperRelationship.target_paper_id).where(
                        PaperRelationship.source_paper_id == obj.id,
                        PaperRelationship.relationship_type.in_(SUPPLEMENTARY_TYPES))).all()
                    for child_id in child_ids:
                        child = session.get(Paper, child_id)
                        if child and child not in session.deleted:
                            child.library_name = obj.library_name
                            seen.discard(id(child))
                            pending.append(child)
            elif isinstance(obj, PaperRelationship):
                state = inspect(obj)
                previous = None if obj in session.new else session.connection().execute(select(
                    PaperRelationship.source_paper_id, PaperRelationship.target_paper_id, PaperRelationship.relationship_type
                ).where(PaperRelationship.id == obj.id)).first()
                old_types = state.attrs.relationship_type.history.deleted
                if obj.relationship_type not in SUPPLEMENTARY_TYPES and not any(value in SUPPLEMENTARY_TYPES for value in old_types) and not (previous and previous[2] in SUPPLEMENTARY_TYPES):
                    continue
                lock_catalog_numbering(session)
                paper_ids = {obj.source_paper_id, obj.target_paper_id}
                if previous:
                    paper_ids.update(previous[:2])
                for key in ('source_paper_id', 'target_paper_id'):
                    paper_ids.update(state.attrs[key].history.deleted)
                for paper_id in paper_ids:
                    paper = session.get(Paper, paper_id)
                    if paper:
                        libraries.add(paper.library_name)
                if obj not in session.deleted and obj.relationship_type in SUPPLEMENTARY_TYPES:
                    owner = session.get(Paper, obj.source_paper_id)
                    child = session.get(Paper, obj.target_paper_id)
                    if owner and child and owner.library_name != child.library_name:
                        child.library_name = owner.library_name
                        seen.discard(id(child))
                        pending.append(child)


@event.listens_for(Session, 'after_flush_postexec')
def _after_catalog_flush(session, flush_context):
    libraries = session.info.pop('catalog_numbering_libraries', set())
    originals = session.info.pop('catalog_numbering_originals', {})
    if libraries:
        _renumber(session, libraries, originals=originals)


@event.listens_for(Session, 'after_soft_rollback')
def _catalog_rollback(session, previous_transaction):
    session.info.pop('catalog_numbering_libraries', None)
    session.info.pop('catalog_numbering_originals', None)
