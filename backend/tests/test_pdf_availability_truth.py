from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Paper
from app.schemas.api import PaperListFilterParams
from app.services.paper_filter_service import PaperFilterCriteria, PaperFilterService
from app.services.paper_query import PaperQueryService


def test_pdf_availability_requires_a_real_file(monkeypatch, tmp_path: Path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True)
    (storage_root / "present.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()

    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    with Session(engine) as session:
        present = Paper(
            title="Physical PDF",
            pdf_path="present.pdf",
            authors=[],
            library_name="PDF truth test",
        )
        stale = Paper(
            title="Stale PDF path",
            pdf_path="missing.pdf",
            authors=[],
            library_name="PDF truth test",
        )
        session.add_all([present, stale])
        session.commit()

        present_rows = PaperQueryService(session).list_papers(
            PaperListFilterParams(library_name="PDF truth test", has_pdf=True)
        )
        missing_rows = PaperQueryService(session).list_papers(
            PaperListFilterParams(library_name="PDF truth test", has_pdf=False)
        )

        assert [row.title for row in present_rows] == ["Physical PDF"]
        assert [row.title for row in missing_rows] == ["Stale PDF path"]
        assert present_rows[0].pdf_exists is True
        assert missing_rows[0].pdf_exists is False

        filtered = PaperFilterService(session).filter(
            PaperFilterCriteria(has_pdf=True, keyword="Physical PDF")
        )
        stale_filtered = PaperFilterService(session).filter(
            PaperFilterCriteria(has_pdf=True, keyword="Stale PDF path")
        )
        assert [row.title for row in filtered] == ["Physical PDF"]
        assert stale_filtered == []

    engine.dispose()
    get_settings.cache_clear()
