import asyncio
import tempfile
from uuid import uuid4
from pathlib import Path

import pytest
from sqlalchemy import inspect

from app.db import session as db_session
from app.config import Settings
from app.db.models import Paper, PaperSection, WorkflowJob
from app.api.jobs import AgentActivityRequest, record_agent_activity
from app.api.jobs import delete_workflow_job
from app.api.papers.listing import get_paper_type_stats
from app.api.papers.workflow import delete_ai_workflow_job
from app.services.workflow_jobs import (
    JOB_TYPE_AGENT_ACTIVITY,
    JobPreflightError,
    clone_job_for_retry_with_status,
    create_job_or_reuse_active,
    serialize_job,
    validate_extraction_preflight,
)


def test_init_db_creates_workflow_jobs_table():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "schema_check.db"
        db_url = f"sqlite:///{db_path}"

        db_session.init_db(db_url)

        engine = db_session.get_engine(db_url)
        inspector = inspect(engine)
        assert "workflow_jobs" in inspector.get_table_names()

        engine.dispose()
        db_session._session_factories.pop(db_url, None)
        db_session._engines.pop(db_url, None)


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
        db_path = Path(tmpdir) / "agent_activity.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = Path(tmpdir) / "delete_active.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = Path(tmpdir) / "legacy_delete.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = Path(tmpdir) / "type_stats.db"
        db_url = f"sqlite:///{db_path}"
        db_session.init_db(db_url)

        factory = db_session._session_factories[db_url]
        session = factory()
        try:
            empty = asyncio.run(get_paper_type_stats("DefaultLibrary", session=session))
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

            stats = asyncio.run(get_paper_type_stats("DefaultLibrary", session=session))
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
        db_path = Path(tmpdir) / "jobs.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = Path(tmpdir) / "retry.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = Path(tmpdir) / "preflight.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = Path(tmpdir) / "preflight_missing_pdf.db"
        db_url = f"sqlite:///{db_path}"
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
        db_path = tmp_path / "preflight_no_text.db"
        db_url = f"sqlite:///{db_path}"
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
