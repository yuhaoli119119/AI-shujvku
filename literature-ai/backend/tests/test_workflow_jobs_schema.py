import tempfile
from uuid import uuid4
from pathlib import Path

import pytest
from sqlalchemy import inspect

from app.db import session as db_session
from app.config import Settings
from app.db.models import Paper, PaperSection, WorkflowJob
from app.services.workflow_jobs import (
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
