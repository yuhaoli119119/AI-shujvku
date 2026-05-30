from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base, Paper, PaperSection
from app.db.session import get_db_session
from app.main import app


def test_paper_translation_preview_uses_persisted_writer_settings(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "translation_preview.sqlite"
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
                    title="Translation Preview Paper",
                    pdf_path="translation.pdf",
                    abstract="This paper evaluates sulfur redox kinetics.",
                    authors=["Author A"],
                )
                session.add(paper)
                session.flush()
                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Results",
                        section_type="results",
                        text="The catalyst improves Li2S deposition kinetics.",
                        page_start=3,
                        page_end=4,
                    )
                )
                session.commit()
                session.refresh(paper)

            def fake_complete_text(self, system_prompt, user_prompt):
                assert "简体中文" in system_prompt
                assert self.settings.writer_api_key == "persisted-secret-key"
                return "这是一段中文译文预览。"

            monkeypatch.setattr("app.services.llm_service.LLMService.complete_text", fake_complete_text)

            client = TestClient(app)
            response = client.post(
                f"/api/papers/{paper.id}/translation/preview",
                json={"include_abstract": True, "max_sections": 1},
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["paper_id"] == str(paper.id)
            assert payload["target_language"] == "zh-CN"
            assert len(payload["items"]) == 2
            assert payload["items"][0]["title"] == "摘要"
            assert payload["items"][0]["translated_text"] == "这是一段中文译文预览。"

            settings = get_settings()
            assert settings.writer_api_key == "persisted-secret-key"
            assert settings.writer_api_base == "https://writer.example.test"
            assert settings.writer_model == "persisted-model"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()


def test_paper_translation_preview_requires_writer_configuration(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "translation_unconfigured.sqlite"
        monkeypatch.setenv("LITAI_DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.delenv("LITAI_WRITER_API_KEY", raising=False)
        monkeypatch.delenv("LITAI_WRITER_API_BASE", raising=False)
        monkeypatch.delenv("LITAI_WRITER_MODEL", raising=False)
        get_settings.cache_clear()

        engine = create_engine(f"sqlite:///{db_path}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(connection)

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
                    title="Unconfigured Translation Paper",
                    pdf_path="translation.pdf",
                    abstract="This abstract needs translation.",
                    authors=[],
                )
                session.add(paper)
                session.commit()
                session.refresh(paper)

            client = TestClient(app)
            response = client.post(
                f"/api/papers/{paper.id}/translation/preview",
                json={"include_abstract": True, "max_sections": 1},
            )

            assert response.status_code == 400
            assert "Writer LLM 尚未配置完整" in response.json()["detail"]
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()


def test_paper_translation_preview_respects_selected_sections(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "translation_selected_sections.sqlite"
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
            paper_id = None
            selected_section_id = None
            with Session(engine) as session:
                paper = Paper(
                    title="Selected Section Translation Paper",
                    pdf_path="translation.pdf",
                    abstract="This abstract should be skipped.",
                    authors=["Author A"],
                )
                session.add(paper)
                session.flush()
                first = PaperSection(
                    paper_id=paper.id,
                    section_title="Introduction",
                    section_type="introduction",
                    text="This introduction should not be translated.",
                    page_start=1,
                    page_end=1,
                )
                second = PaperSection(
                    paper_id=paper.id,
                    section_title="Results",
                    section_type="results",
                    text="Only this results section should be translated.",
                    page_start=3,
                    page_end=4,
                )
                session.add_all([first, second])
                session.commit()
                session.refresh(second)
                paper_id = str(paper.id)
                selected_section_id = str(second.id)

            prompts = []

            def fake_complete_text(self, system_prompt, user_prompt):
                prompts.append(user_prompt)
                return "选定章节的中文译文。"

            monkeypatch.setattr("app.services.llm_service.LLMService.complete_text", fake_complete_text)

            client = TestClient(app)
            response = client.post(
                f"/api/papers/{paper_id}/translation/preview",
                json={
                    "include_abstract": False,
                    "section_ids": [selected_section_id],
                    "max_sections": 8,
                },
            )

            assert response.status_code == 200
            payload = response.json()
            assert len(payload["items"]) == 1
            assert payload["items"][0]["title"] == "Results"
            assert payload["items"][0]["translated_text"] == "选定章节的中文译文。"
            assert len(prompts) == 1
            assert "Only this results section should be translated." in prompts[0]
            assert "This abstract should be skipped." not in prompts[0]
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()
