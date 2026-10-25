"""Register an already rendered PDF preview for one original DOCX file.

Run inside the backend container with a PDF path on /data. The command reads
the registered file from PostgreSQL but never edits the database or source.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import RebuildPaperFile
from app.db.session import get_engine
from app.services.rebuild_file_preview import install_derived_pdf


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-id", required=True, type=UUID)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--converter-name")
    parser.add_argument("--converter-version")
    args = parser.parse_args()
    settings = get_settings()
    with Session(get_engine(settings.database_url)) as session:
        record = session.get(RebuildPaperFile, args.file_id)
        if record is None:
            parser.error("registered_file_not_found")
        if bool(args.converter_name) != bool(args.converter_version):
            parser.error('converter_name_and_version_required_together')
        converter = {'name': args.converter_name, 'version': args.converter_version} if args.converter_name else None
        destination = install_derived_pdf(record, settings, args.pdf, converter=converter)
        print(json.dumps({"file_id": str(record.id), "source_sha256": record.sha256,
                          "reading_pdf": str(destination)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
