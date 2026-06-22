from __future__ import annotations

import os

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base, Paper
from app.services.external_analysis_service import ExternalAnalysisService


def _make_external_audit_ready(paper: Paper, root: Path) -> None:
    pdf_path = root / f"{paper.id}.pdf"
    markdown_path = root / f"{paper.id}.md"
    docling_path = root / f"{paper.id}.docling.json"
    workspace_path = root / "workspace" / str(paper.id)
    pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    markdown_path.write_text("# Ready paper\n\nDFT evidence is available.", encoding="utf-8")
    docling_path.write_text('{"texts": [{"text": "DFT evidence is available."}]}', encoding="utf-8")
    package_path = workspace_path / "extraction" / "ai_reading_package.json"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_text('{"sections": [{"title": "Results"}]}', encoding="utf-8")
    paper.pdf_path = str(pdf_path)
    paper.markdown_path = str(markdown_path)
    paper.docling_json_path = str(docling_path)
    paper.workspace_path = str(workspace_path)


def test_get_review_coverage_includes_external_audit_opinion(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        settings = get_settings().model_copy(update={"database_url": os.environ["LITAI_TEST_DATABASE_URL"]})

        try:
            with Session(engine) as session:
                paper = Paper(title="Coverage Audit Paper", pdf_path="paper.pdf", authors=[])
                session.add(paper)
                session.flush()
                _make_external_audit_ready(paper, Path(tmpdir))
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

                service = ExternalAnalysisService(session, settings)
                run = service.import_run(
                    paper_id=paper_id,
                    source="gemini_external_audit",
                    source_label="Gemini",
                    raw_text=None,
                    raw_payload={
                        "paper_id": str(paper_id),
                        "verdict": "WARN",
                        "recommended_action": "needs_dft_review",
                        "suspected_missing": ["dft_result"],
                        "evidence_examples": [{"text": "DFT signal exists."}],
                    },
                )
                candidates = service.list_candidates(run.id)
                session.commit()
                assert len(candidates) == 1
                assert candidates[0].status == "candidate"

            @contextmanager
            def fake_session_scope(database_url):
                session = TestingSessionLocal()
                try:
                    yield session
                finally:
                    session.close()

            monkeypatch.setattr("app.mcp.server.get_settings", lambda: settings)
            monkeypatch.setattr("app.mcp.server.require_mcp_capability", lambda capability: None)
            monkeypatch.setattr("app.mcp.server.session_scope", fake_session_scope)

            from app.mcp.server import get_review_coverage

            coverage = get_review_coverage(str(paper_id))

            assert coverage["external_audit_count"] == 1
            assert coverage["external_audit_source_distribution"] == {"gemini_external_audit": 1}
            assert coverage["external_audits"]["source_distribution"] == {"gemini_external_audit": 1}
            audit = coverage["latest_external_audits"][0]
            assert audit["candidate_type"] == "external_audit_opinion"
            assert audit["status"] == "candidate"
            assert audit["verification_status"] == "unverified"
            assert audit["verdict"] == "WARN"
        finally:
            engine.dispose()
