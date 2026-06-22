import os
import asyncio
import importlib
import tempfile
from datetime import datetime, timedelta
from uuid import uuid4
from pathlib import Path
from contextlib import contextmanager

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import session as db_session
from app.config import Settings
from app.db.models import Paper, PaperSection, WorkflowJob
from app.api.jobs import AgentActivityRequest, record_agent_activity
from app.api.jobs import delete_workflow_job
from app.api.papers.listing import get_paper_type_stats
from app.api.papers.workflow import delete_ai_workflow_job
from app.services import workflow_jobs as workflow_jobs_service
from app.services.workflow_jobs import (
    JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
    JOB_TYPE_AGENT_ACTIVITY,
    JOB_TYPE_LOCAL_PDF_PATH_INGEST,
    JobPreflightError,
    WORKFLOW_QUEUE_DEFAULT,
    WORKFLOW_QUEUE_PDF_INGEST,
    clone_job_for_retry_with_status,
    create_job_or_reuse_active,
    run_discovery_download_ingest_job,
    serialize_job,
    validate_extraction_preflight,
    workflow_queue_for_job_type,
)


def test_init_db_creates_workflow_jobs_table():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]

        db_session.init_db(db_url)

        engine = db_session.get_engine(db_url)
        inspector = inspect(engine)
        assert "workflow_jobs" in inspector.get_table_names()

        engine.dispose()
        db_session._session_factories.pop(db_url, None)
        db_session._engines.pop(db_url, None)


def test_workflow_queue_routes_pdf_ingest_to_dedicated_queue():
    assert workflow_queue_for_job_type(JOB_TYPE_LOCAL_PDF_PATH_INGEST) == WORKFLOW_QUEUE_PDF_INGEST
    assert workflow_queue_for_job_type(JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST) == WORKFLOW_QUEUE_PDF_INGEST
    assert workflow_queue_for_job_type(JOB_TYPE_AGENT_ACTIVITY) == WORKFLOW_QUEUE_DEFAULT


def test_init_db_backfills_paper_codes_after_migration_transaction(monkeypatch):
    db_url = "postgresql+psycopg://test:test@db:5432/test"
    db_session._initialized_urls.discard(db_url)
    events: list[str] = []
    state = {"migration_open": False}

    class FakeConnection:
        def execution_options(self, **_: object):
            return self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return None

    class FakeEngine:
        def __init__(self):
            self.dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            return FakeConnection()

        @contextmanager
        def begin(self):
            assert state["migration_open"] is False
            state["migration_open"] = True
            events.append("begin_enter")
            try:
                yield FakeConnection()
            finally:
                events.append("begin_exit")
                state["migration_open"] = False

    class FakeInspector:
        def get_table_names(self):
            return ["papers"]

        def get_columns(self, _table_name: str):
            return []

    class FakeSession:
        def __init__(self, _engine):
            assert state["migration_open"] is False
            events.append("session_open")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("session_close")
            return False

        def commit(self):
            events.append("session_commit")

    fake_engine = FakeEngine()
    monkeypatch.setattr(db_session, "get_engine", lambda _url: fake_engine)
    monkeypatch.setattr(db_session.Base.metadata, "create_all", lambda _engine: events.append("create_all"))
    monkeypatch.setattr(db_session, "inspect", lambda _engine: FakeInspector())
    monkeypatch.setattr(db_session, "Session", FakeSession)
    paper_codes = importlib.import_module("app.services.paper_codes")
    monkeypatch.setattr(
        paper_codes,
        "ensure_paper_codes",
        lambda _session, papers=None: events.append("ensure_paper_codes"),
    )

    db_session.init_db(db_url, force=True)

    assert "ensure_paper_codes" in events
    assert events.index("begin_exit") < events.index("session_open")
    assert events.index("session_open") < events.index("ensure_paper_codes")


def test_serialize_extraction_job_includes_readable_summary_and_failure_explanation():
    job = WorkflowJob(
        job_id="job-1",
        type="extraction",
        status="failed",
        library_name="DefaultLibrary",
        payload={"paper_id": "paper-1", "schemas": ["DFTResult"], "force": True},
        progress={"phase": "failed"},
        error="Docling parser failed: PDF preview render error",
    )

    data = serialize_job(job)

    assert data["summary"]["source_label"]
    assert data["summary"]["paper_id"] == "paper-1"
    assert data["summary"]["schemas"] == ["DFTResult"]
    assert data["summary"]["failure_count"] == 1
    assert data["failure_explanation"]["can_retry"] is True
    assert data["failure_explanation"]["reasons"][0]["code"] == "pdf_preview_failed"


def test_serialize_ai_workflow_job_summarizes_ingest_counts_and_failed_reasons():
    job = WorkflowJob(
        job_id="job-2",
        type="ai_workflow",
        status="completed",
        library_name="DefaultLibrary",
        payload={"query": "lithium sulfur catalyst", "max_results": 10, "max_downloads": 3},
        progress={"phase": "completed", "searched_total": 10, "attempted_downloads": 3},
        result={
            "ingested": [{"paper_id": "paper-1"}],
            "failed": [{"identifier": "10.1/demo", "code": "download_or_ingest_failed", "reason": "download timeout"}],
        },
    )

    data = serialize_job(job)

    assert data["summary"]["query"] == "lithium sulfur catalyst"
    assert data["summary"]["searched_total"] == 10
    assert data["summary"]["success_count"] == 1
    assert data["summary"]["failure_count"] == 1
    assert data["failure_explanation"]["reasons"][0]["code"] == "download_failed"


def test_serialize_agent_activity_job_summarizes_ai_work_trace():
    job = WorkflowJob(
        job_id="job-agent-1",
        type=JOB_TYPE_AGENT_ACTIVITY,
        status="completed",
        library_name="DefaultLibrary",
        payload={
            "agent": "Codex",
            "action": "figure_review",
            "title": "校对图像裁剪",
            "paper_id": "paper-1",
        },
        progress={"phase": "figure_review", "message": "校对图像裁剪"},
        result={"metrics": {"success_count": 3, "failure_count": 1}, "artifacts": [{"path": "figures/a.png"}]},
    )

    data = serialize_job(job)

    assert data["summary"]["source_label"] == "Codex 工作留痕"
    assert data["summary"]["action"] == "figure_review"
    assert data["summary"]["title"] == "校对图像裁剪"
    assert data["summary"]["success_count"] == 3
    assert data["summary"]["failure_count"] == 1
    assert data["summary"]["artifacts"][0]["path"] == "figures/a.png"


def test_record_agent_activity_persists_to_workflow_jobs_table():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            request = AgentActivityRequest(
                agent="Gemini",
                action="table_review",
                title="核对表格解析质量",
                library_name="DefaultLibrary",
                metrics={"success_count": 2, "failure_count": 0},
                artifacts=[{"path": "tables/table-1.md"}],
            )
            data = asyncio.run(record_agent_activity(request, session=session))

            assert data["type"] == JOB_TYPE_AGENT_ACTIVITY
            assert data["status"] == "completed"
            assert data["summary"]["source_label"] == "Gemini 工作留痕"
            assert data["summary"]["success_count"] == 2
            saved = session.get(WorkflowJob, data["job_id"])
            assert saved is not None
            assert saved.payload["action"] == "table_review"
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_force_delete_active_agent_activity_record():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            job = WorkflowJob(
                job_id="active-agent-job",
                type=JOB_TYPE_AGENT_ACTIVITY,
                status="running",
                library_name="DefaultLibrary",
                payload={"agent": "Codex", "action": "parse_review"},
                progress={"phase": "parse_review"},
            )
            session.add(job)
            session.commit()

            data = asyncio.run(delete_workflow_job("active-agent-job", force=True, session=session))

            assert data == {"status": "deleted", "job_id": "active-agent-job"}
            assert session.get(WorkflowJob, "active-agent-job") is None
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_legacy_ai_workflow_delete_endpoint_can_delete_agent_activity_record():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            job = WorkflowJob(
                job_id="legacy-agent-job",
                type=JOB_TYPE_AGENT_ACTIVITY,
                status="completed",
                library_name="DefaultLibrary",
                payload={"agent": "Gemini", "action": "table_review"},
                progress={"phase": "table_review"},
            )
            session.add(job)
            session.commit()

            data = asyncio.run(delete_ai_workflow_job("legacy-agent-job", session=session))

            assert data == {"ok": True, "job_id": "legacy-agent-job"}
            assert session.get(WorkflowJob, "legacy-agent-job") is None
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_paper_type_stats_are_database_backed_and_empty_when_no_papers():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            empty = get_paper_type_stats("DefaultLibrary", session=session)
            assert empty["total"] == 0
            assert [item["count"] for item in empty["items"]] == [0, 0, 0, 0]

            session.add_all(
                [
                    Paper(title="Core", pdf_path="", paper_type="A", library_name="DefaultLibrary"),
                    Paper(title="Related", pdf_path="", paper_type="B", library_name="DefaultLibrary"),
                    Paper(title="Unknown", pdf_path="", paper_type="Unknown", library_name="DefaultLibrary"),
                    Paper(title="Other library", pdf_path="", paper_type="C", library_name="OtherLibrary"),
                ]
            )
            session.commit()

            stats = get_paper_type_stats("DefaultLibrary", session=session)
            counts = {item["key"]: item["count"] for item in stats["items"]}
            assert stats["total"] == 3
            assert counts == {"A": 1, "B": 1, "C": 0, "uncategorized": 1}
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_create_job_or_reuse_active_deduplicates_same_extraction_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            payload = {"paper_id": "paper-1", "schemas": ["DFTResult"]}
            first, reused_first = create_job_or_reuse_active(
                session,
                job_type="extraction",
                library_name="DefaultLibrary",
                payload=payload,
                runtime_context={"database_url": db_url, "storage_root": str(Path(tmpdir) / "storage")},
                progress={"phase": "queued", "paper_id": "paper-1"},
            )
            second, reused_second = create_job_or_reuse_active(
                session,
                job_type="extraction",
                library_name="DefaultLibrary",
                payload=payload,
                runtime_context={"database_url": db_url, "storage_root": str(Path(tmpdir) / "storage")},
                progress={"phase": "queued", "paper_id": "paper-1"},
            )

            assert reused_first is False
            assert reused_second is True
            assert second.job_id == first.job_id
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_retry_reuses_active_equivalent_job():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            payload = {"paper_id": "paper-1", "schemas": ["DFTResult"]}
            session.add_all(
                [
                    WorkflowJob(
                        job_id="failed-job",
                        type="extraction",
                        status="failed",
                        library_name="DefaultLibrary",
                        payload=payload,
                        progress={"phase": "failed", "paper_id": "paper-1"},
                        runtime_context={"database_url": db_url, "storage_root": str(Path(tmpdir) / "storage")},
                    ),
                    WorkflowJob(
                        job_id="active-job",
                        type="extraction",
                        status="running",
                        library_name="DefaultLibrary",
                        payload=payload,
                        progress={"phase": "extraction", "paper_id": "paper-1"},
                        runtime_context={"database_url": db_url, "storage_root": str(Path(tmpdir) / "storage")},
                    ),
                ]
            )
            session.commit()

            retry_job, reused = clone_job_for_retry_with_status(session, "failed-job")

            assert reused is True
            assert retry_job.job_id == "active-job"
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_extraction_preflight_blocks_metadata_only_paper_even_with_abstract():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        paper_id = uuid4()
        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            session.add(
                Paper(
                    id=paper_id,
                    title="Metadata only",
                    abstract="Metadata abstract should not count as parsed text.",
                    pdf_path="",
                    oa_status="metadata_only",
                    library_name="DefaultLibrary",
                )
            )
            session.commit()

            with pytest.raises(JobPreflightError) as excinfo:
                validate_extraction_preflight(
                    session,
                    paper_id=paper_id,
                    settings=Settings(database_url=db_url, storage_root=Path(tmpdir) / "storage"),
                )

            assert excinfo.value.code == "pdf_unavailable"
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_extraction_preflight_blocks_missing_pdf_reference():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        paper_id = uuid4()
        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            session.add(
                Paper(
                    id=paper_id,
                    title="Lost PDF",
                    pdf_path="storage/pdf/missing.pdf",
                    library_name="DefaultLibrary",
                )
            )
            session.add(PaperSection(paper_id=paper_id, section_title="Body", section_type="body", text="Parsed text"))
            session.commit()

            with pytest.raises(JobPreflightError) as excinfo:
                validate_extraction_preflight(
                    session,
                    paper_id=paper_id,
                    settings=Settings(database_url=db_url, storage_root=Path(tmpdir) / "storage"),
                )

            assert excinfo.value.code == "pdf_missing"
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_extraction_preflight_requires_parsed_body_sections():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pdf_path = tmp_path / "storage" / "pdf" / "paper.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"%PDF-1.4\n")
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        paper_id = uuid4()
        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            session.add(
                Paper(
                    id=paper_id,
                    title="PDF without parsed text",
                    abstract="Abstract alone should not unlock stage2 extraction.",
                    pdf_path="storage/pdf/paper.pdf",
                    library_name="DefaultLibrary",
                )
            )
            session.commit()

            with pytest.raises(JobPreflightError) as excinfo:
                validate_extraction_preflight(
                    session,
                    paper_id=paper_id,
                    settings=Settings(database_url=db_url, storage_root=tmp_path / "storage"),
                )

            assert excinfo.value.code == "parsed_text_missing"
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_create_job_or_reuse_active_ignores_stale_running_job():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            stale_job = WorkflowJob(
                job_id="stale-discovery",
                type=JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
                status="running",
                library_name="StaleLibrary",
                payload={"identifier": "10.1000/stale", "library_name": "StaleLibrary"},
                progress={"phase": "fetch_metadata"},
                runtime_context={},
                created_at=datetime.utcnow() - timedelta(hours=2),
                updated_at=datetime.utcnow() - timedelta(hours=2),
            )
            session.add(stale_job)
            session.commit()

            job, reused = create_job_or_reuse_active(
                session,
                job_type=JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
                library_name="StaleLibrary",
                payload={"identifier": "10.1000/stale", "library_name": "StaleLibrary"},
                runtime_context={},
                progress={"phase": "queued"},
            )

            session.refresh(stale_job)
            assert reused is False
            assert job.job_id != stale_job.job_id
            assert stale_job.status == "failed"
            assert stale_job.progress["stale_cleanup"] is True
        finally:
            session.close()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)


def test_run_discovery_download_ingest_job_rolls_back_and_falls_back_to_metadata_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        storage_root = Path(tmpdir) / "storage"
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            session.add(
                WorkflowJob(
                    job_id="job-discovery-stable",
                    type=JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
                    status="queued",
                    library_name="StableLibrary",
                    payload={"identifier": "10.1000/fallback", "library_name": "StableLibrary"},
                    progress={"phase": "queued"},
                    runtime_context={},
                )
            )
            session.commit()
        finally:
            session.close()

        settings = Settings(database_url=db_url, storage_root=storage_root)

        class DummyPaper:
            pass

        def fake_fetch_metadata(self, identifier, providers=None):
            return DummyPaper(), {
                "identifier": identifier,
                "title": "Recovered Metadata Only",
                "doi": "10.1000/recovered-meta",
                "year": 2025,
                "journal": "Stable Journal",
                "authors": ["Alice"],
                "abstract": "Recovered after rollback",
                "url": "https://example.com/recovered-meta",
            }

        async def fake_download(*args, **kwargs):
            pdf_path = Path(tmpdir) / "downloaded.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 test")
            return pdf_path

        async def fake_ingest_pdf(self, *args, **kwargs):
            first = Paper(
                id=uuid4(),
                library_name="StableLibrary",
                title="first",
                authors=[],
                pdf_path="first.pdf",
            )
            self.session.add(first)
            self.session.flush()
            duplicate = Paper(
                id=first.id,
                library_name="StableLibrary",
                title="duplicate",
                authors=[],
                pdf_path="duplicate.pdf",
            )
            self.session.add(duplicate)
            with pytest.raises(IntegrityError):
                self.session.flush()
            raise RuntimeError("ingest_pdf left the session dirty")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(workflow_jobs_service, "get_settings", lambda: settings)
            monkeypatch.setattr(workflow_jobs_service.DiscoveryService, "fetch_metadata", fake_fetch_metadata)
            monkeypatch.setattr(workflow_jobs_service, "download_discovery_candidate", fake_download)
            monkeypatch.setattr(workflow_jobs_service.PaperIngestionService, "ingest_pdf", fake_ingest_pdf)

            run_discovery_download_ingest_job("job-discovery-stable", db_url)

            verify_session = factory()
            try:
                stored_job = verify_session.get(WorkflowJob, "job-discovery-stable")
                paper = verify_session.scalar(select(Paper).where(Paper.doi == "10.1000/recovered-meta"))
                assert stored_job is not None
                assert stored_job.status == "completed"
                assert stored_job.result["status"] == "metadata_only"
                assert paper is not None
                assert paper.oa_status == "metadata_only"
                assert paper.pdf_path == ""
            finally:
                verify_session.close()
        finally:
            monkeypatch.undo()
            db_session.get_engine(db_url).dispose()
            db_session._session_factories.pop(db_url, None)
            db_session._engines.pop(db_url, None)
