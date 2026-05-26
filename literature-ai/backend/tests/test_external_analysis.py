from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base, Paper, PaperCorrection, PaperNote, PaperRelationship
from app.db.session import get_db_session
from app.main import app
from app.services.external_analysis_service import ExternalAnalysisNormalizedModel


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
