from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base, ExternalAnalysisCandidate, Paper, PaperCorrection, PaperNote, PaperRelationship
from app.db.session import get_db_session
from app.main import app
from app.services.external_analysis_service import ExternalAnalysisNormalizedModel


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


def test_external_analysis_import_and_materialize_flow():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'external_analysis.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                main_paper = Paper(
                    title="Main Paper",
                    doi="10.1000/main",
                    pdf_path="main.pdf",
                    authors=["Author A"],
                )
                support_paper = Paper(
                    title="Support Paper",
                    doi="10.1000/support",
                    pdf_path="support.pdf",
                    authors=["Author B"],
                )
                session.add_all([main_paper, support_paper])
                session.commit()
                session.refresh(main_paper)
                session.refresh(support_paper)

            client = TestClient(app)

            import_payload = {
                "paper_id": str(main_paper.id),
                "source": "chatgpt_web",
                "source_label": "ChatGPT web upload",
                "raw_payload": {
                    "review_notes": [
                        {
                            "content": "The abstract wording may overstate the mechanism claim.",
                            "field_name": "abstract",
                            "page": 1,
                            "quoted_text": "This catalyst proves complete sulfur immobilization.",
                        }
                    ],
                    "correction_proposals": [
                        {
                            "field_name": "abstract",
                            "target_path": "abstract",
                            "operation": "replace",
                            "proposed_value": "A more conservative abstract rewrite.",
                            "reason": "The original sentence is too strong compared with the source evidence.",
                            "evidence_payload": {"page": 1},
                        }
                    ],
                    "supporting_papers": [
                        {
                            "relationship_type": "supports",
                            "target_doi": "10.1000/support",
                            "target_title": "Support Paper",
                            "note": "This paper provides complementary mechanism evidence.",
                        }
                    ],
                },
            }

            imported = client.post("/api/external-analysis/import", json=import_payload)
            assert imported.status_code == 200
            run_payload = imported.json()
            assert run_payload["mapping_status"] in {"normalized", "heuristic", "normalized_with_llm"}
            assert len(run_payload["candidates"]) == 3

            run_id = run_payload["id"]
            materialized = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"explicit_all": True, "created_by": "reviewer_ai"},
            )
            assert materialized.status_code == 200
            assert materialized.json()["created_notes"] == 1
            assert materialized.json()["created_corrections"] == 1
            assert materialized.json()["created_relationships"] == 1

            detail = client.get(f"/api/papers/{main_paper.id}")
            assert detail.status_code == 200
            detail_payload = detail.json()
            assert detail_payload["relationship_summary"]["supports"] == 1
            assert len(detail_payload["outgoing_relationships"]) == 1
            assert detail_payload["outgoing_relationships"][0]["related_paper_title"] == "Support Paper"

            listing = client.get("/api/papers")
            assert listing.status_code == 200
            listing_payload = listing.json()
            listed_main = next(item for item in listing_payload if item["id"] == str(main_paper.id))
            assert listed_main["relationship_summary"]["supports"] == 1

            with Session(engine) as session:
                assert session.query(PaperNote).count() == 1
                assert session.query(PaperCorrection).count() == 1
                assert session.query(PaperRelationship).count() == 1
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_materialize_rejects_empty_or_implicit_all():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'external_analysis_contract.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Contract Paper", pdf_path="contract.pdf", authors=[])
                session.add(paper)
                session.commit()
                session.refresh(paper)

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper.id),
                    "source": "chatgpt_web",
                    "raw_payload": {
                        "review_notes": [{"content": "Candidate note.", "field_name": "abstract"}],
                    },
                },
            )
            assert imported.status_code == 200
            run_id = imported.json()["id"]

            empty_selection = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"candidate_ids": [], "created_by": "reviewer_ai"},
            )
            assert empty_selection.status_code == 400
            assert "candidate_ids=[]" in empty_selection.json()["detail"]

            implicit_all = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"created_by": "reviewer_ai"},
            )
            assert implicit_all.status_code == 400
            assert "explicit_all=true" in implicit_all.json()["detail"]

            with Session(engine) as session:
                assert session.query(PaperNote).count() == 0
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_paper_level_audit_payload_creates_unverified_candidate():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'external_audit.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Gemini Audit Paper", pdf_path="paper.pdf", authors=[])
                session.add(paper)
                session.flush()
                _make_external_audit_ready(paper, Path(tmpdir))
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "gemini_external_audit",
                    "raw_payload": {
                        "paper_id": str(paper_id),
                        "verdict": "WARN",
                        "recommended_action": "needs_dft_review",
                        "suspected_missing": ["dft_result"],
                        "metadata_status": "ok",
                        "section_structure_status": "warn",
                        "table_status": "warn",
                        "figure_status": "ok",
                        "dft_status": "warn",
                        "evidence_examples": [{"text": "DFT was mentioned but no DFT result was found."}],
                    },
                },
            )

            assert imported.status_code == 200
            payload = imported.json()
            assert len(payload["candidates"]) == 1
            candidate = payload["candidates"][0]
            assert candidate["candidate_type"] == "external_audit_opinion"
            assert candidate["status"] == "candidate"
            assert candidate["normalized_payload"]["source"] == "gemini_external_audit"
            assert candidate["normalized_payload"]["verdict"] == "WARN"
            assert candidate["normalized_payload"]["recommended_action"] == "needs_dft_review"
            assert candidate["normalized_payload"]["verification_status"] == "unverified"

            center = client.get("/api/workbench/review-center")
            assert center.status_code == 200
            row = next(item for item in center.json()["rows"] if item["paper_id"] == str(paper_id))
            assert row["external_audit_count"] == 1
            assert row["external_audit_source_counts"] == {"gemini_external_audit": 1}
            assert row["external_audit_opinions"][0]["candidate_type"] == "external_audit_opinion"
            assert row["external_audit_opinions"][0]["verification_status"] == "unverified"

            with Session(engine) as session:
                candidate_row = session.query(ExternalAnalysisCandidate).one()
                assert candidate_row.status == "candidate"
                assert candidate_row.candidate_type == "external_audit_opinion"
                assert candidate_row.materialized_target_type is None
                assert candidate_row.materialized_target_id is None
                assert candidate_row.normalized_payload["verification_status"] == "unverified"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_delete_post_alias_and_utc_created_at():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'external_analysis_delete.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Delete Contract Paper", pdf_path="delete.pdf", authors=[])
                session.add(paper)
                session.commit()
                session.refresh(paper)

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper.id),
                    "source": "chatgpt_web",
                    "raw_payload": {
                        "review_notes": [{"content": "Candidate note.", "field_name": "abstract"}],
                    },
                },
            )
            assert imported.status_code == 200
            payload = imported.json()
            assert payload["created_at"].endswith(("Z", "+00:00"))
            assert payload["candidates"][0]["created_at"].endswith(("Z", "+00:00"))
            assert datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00")).tzinfo == UTC

            run_id = payload["id"]
            deleted = client.post(f"/api/external-analysis/runs/{run_id}/delete")
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": True, "run_id": run_id}

            listed = client.get(f"/api/external-analysis/runs?paper_id={paper.id}")
            assert listed.status_code == 200
            assert listed.json() == []
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_internal_ai_parse_endpoint_auto_materializes(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "database.sqlite"
        monkeypatch.setenv("LITAI_DATABASE_URL", f"sqlite:///{db_path}")
        get_settings.cache_clear()
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                main_paper = Paper(
                    title="Internal Parse Paper",
                    doi="10.1000/internal",
                    pdf_path="internal.pdf",
                    abstract="A parsed paper ready for internal AI review.",
                    authors=["Reviewer A"],
                )
                support_paper = Paper(
                    title="Known Support Paper",
                    doi="10.1000/known-support",
                    pdf_path="support.pdf",
                    authors=["Reviewer B"],
                )
                session.add_all([main_paper, support_paper])
                session.commit()
                session.refresh(main_paper)
                session.refresh(support_paper)

            monkeypatch.setattr(
                "app.services.llm_service.LLMService.is_configured",
                lambda self: True,
            )
            monkeypatch.setattr(
                "app.services.llm_service.LLMService.structured_extract",
                lambda self, system_prompt, user_prompt, response_format: ExternalAnalysisNormalizedModel.model_validate(
                    {
                        "review_notes": [
                            {
                                "content": "Internal AI thinks the abstract should be softened.",
                                "field_name": "abstract",
                                "page": 1,
                                "quoted_text": "A parsed paper ready for internal AI review.",
                            }
                        ],
                        "correction_proposals": [
                            {
                                "field_name": "abstract",
                                "target_path": "abstract",
                                "operation": "replace",
                                "proposed_value": "A softened abstract generated by internal AI.",
                                "reason": "The abstract wording is too generic.",
                            }
                        ],
                        "supporting_papers": [
                            {
                                "relationship_type": "supports",
                                "target_doi": "10.1000/known-support",
                                "target_title": "Known Support Paper",
                                "note": "The known support paper complements this record.",
                            }
                        ],
                    }
                ),
            )

            client = TestClient(app)
            health = client.get("/api/health")
            assert health.status_code == 200
            health_payload = health.json()
            assert health_payload["db_kind"] == "sqlite"
            assert health_payload["db_path"].endswith("database.sqlite")
            assert health_payload["is_active_library_sqlite"] is True

            response = client.post(
                f"/api/external-analysis/papers/{main_paper.id}/internal-parse",
                json={"source_label": "内部AI解析", "auto_apply": True},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["mapping_status"] in {"normalized", "heuristic", "normalized_with_llm"}
            assert payload["created_notes"] == 1
            assert payload["created_corrections"] == 1
            assert payload["created_relationships"] == 1
            assert payload["auto_applied_corrections"] == 0

            detail = client.get(f"/api/papers/{main_paper.id}")
            assert detail.status_code == 200
            detail_payload = detail.json()
            assert detail_payload["relationship_summary"]["supports"] == 1

            runs = client.get(f"/api/external-analysis/runs?paper_id={main_paper.id}")
            assert runs.status_code == 200
            runs_payload = runs.json()
            assert len(runs_payload) == 1
            assert runs_payload[0]["source"] == "internal_ai"
            assert len(runs_payload[0]["candidates"]) == 3

            with Session(engine) as session:
                assert session.query(PaperNote).count() == 1
                assert session.query(PaperCorrection).count() == 1
                assert session.query(PaperRelationship).count() == 1
                correction = session.query(PaperCorrection).first()
                assert correction is not None
                assert correction.status == "pending"
                stored_paper = session.get(Paper, main_paper.id)
                assert stored_paper.abstract == "A parsed paper ready for internal AI review."
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()


def test_internal_ai_parse_uses_persisted_writer_settings(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "persisted_settings.sqlite"
        monkeypatch.setenv("LITAI_DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.delenv("LITAI_WRITER_API_KEY", raising=False)
        monkeypatch.delenv("LITAI_WRITER_API_BASE", raising=False)
        monkeypatch.delenv("LITAI_WRITER_MODEL", raising=False)
        get_settings.cache_clear()

        engine = create_engine(f"sqlite:///{db_path}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(connection)
            connection.execute(
                text(
                    "CREATE TABLE app_settings ("
                    "  key   VARCHAR(255) PRIMARY KEY,"
                    "  value TEXT"
                    ")"
                )
            )
            connection.execute(
                text("INSERT INTO app_settings (key, value) VALUES (:key, :value)"),
                [
                    {"key": "writer_api_key", "value": "persisted-secret-key"},
                    {"key": "writer_api_base", "value": "https://writer.example.test"},
                    {"key": "writer_model", "value": "persisted-model"},
                ],
            )

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(
                    title="Persisted Settings Paper",
                    doi="10.1000/persisted",
                    pdf_path="persisted.pdf",
                    abstract="A parsed paper that relies on persisted writer settings.",
                    authors=["Reviewer A"],
                )
                session.add(paper)
                session.commit()
                session.refresh(paper)

            monkeypatch.setattr(
                "app.services.llm_service.LLMService.structured_extract",
                lambda self, system_prompt, user_prompt, response_format: ExternalAnalysisNormalizedModel.model_validate(
                    {
                        "review_notes": [{"content": "Persisted config note.", "field_name": "abstract"}],
                        "correction_proposals": [],
                        "supporting_papers": [],
                    }
                ),
            )

            client = TestClient(app)
            response = client.post(
                f"/api/external-analysis/papers/{paper.id}/internal-parse",
                json={"source_label": "持久化配置解析", "auto_apply": False},
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["mapping_status"] in {"normalized", "heuristic", "normalized_with_llm"}
            assert payload["created_notes"] == 0
            assert payload["auto_applied_corrections"] == 0

            settings = get_settings()
            assert settings.writer_api_key == "persisted-secret-key"
            assert settings.writer_api_base == "https://writer.example.test"
            assert settings.writer_model == "persisted-model"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()
