from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ContentEvidenceItem,
    ContentReviewBundle,
    EvidenceClaim,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    WorkflowJob,
)
from app.services.task_log_service import TaskLogService


def _seed_runs(engine):
    with Session(engine) as session:
        paper = Paper(
            library_name="任务补建测试库",
            paper_code="TB001",
            title="Task backfill paper",
            pdf_path="task-backfill.pdf",
            authors=[],
        )
        session.add(paper)
        session.flush()
        missing_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="web_ai",
            source_label="pretend verified label",
            source_identity="untrusted:http_external_analysis",
            source_identity_verified=False,
            mapping_status="normalized",
        )
        existing_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="IDE pass",
            source_identity="mcp:local_ide",
            source_identity_verified=True,
            mapping_status="normalized",
        )
        session.add_all([missing_run, existing_run])
        session.flush()
        statuses = ["candidate", "pending", "requires_resolution", "validated", "failed"]
        session.add_all(
            [
                ExternalAnalysisCandidate(
                    run_id=missing_run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit" if index == 0 else "note",
                    normalized_payload={"target_type": "dft_results"} if index == 0 else {"field_name": "summary"},
                    mapping_reason="needs target resolution" if status == "requires_resolution" else None,
                    status=status,
                )
                for index, status in enumerate(statuses)
            ]
        )
        session.add(
            ExternalAnalysisCandidate(
                run_id=existing_run.id,
                paper_id=paper.id,
                candidate_type="note",
                normalized_payload={"field_name": "summary"},
                status="pending",
            )
        )
        session.add(
            ContentEvidenceItem(
                paper_id=paper.id,
                run_id=missing_run.id,
                category="draft_evidence_check",
                source_type="external_analysis_candidate",
                source_id="seed-backfill-item",
                content="unreviewed candidate",
                review_status="needs_review",
                citation_status="needs_review",
            )
        )
        session.add(
            WorkflowJob(
                job_id="existing-agent-task",
                type="agent_activity",
                status="completed",
                library_name=paper.library_name,
                payload={"external_analysis_run_id": str(existing_run.id)},
                progress={},
                result={},
                runtime_context={},
            )
        )
        session.commit()
        return paper.id, missing_run.id, existing_run.id


def test_backfill_external_analysis_tasks_is_dry_run_idempotent_and_preserves_sources(setup_test_db):
    paper_id, missing_run_id, existing_run_id = _seed_runs(setup_test_db)
    with Session(setup_test_db) as session:
        before = {
            "runs": session.query(ExternalAnalysisRun).count(),
            "candidates": session.query(ExternalAnalysisCandidate).count(),
            "content": session.query(ContentEvidenceItem).count(),
            "claims": session.query(EvidenceClaim).count(),
            "bundles": session.query(ContentReviewBundle).count(),
            "jobs": session.query(WorkflowJob).count(),
        }
        dry = TaskLogService(session).backfill_missing_external_analysis_tasks(
            paper_code="TB001", apply=False
        )
        session.rollback()
        assert dry["expected_new_tasks"] == 1
        assert dry["created_tasks"] == 0
        assert [item["run_id"] for item in dry["missing_runs"]] == [str(missing_run_id)]
        assert dry["missing_runs"][0]["candidate_count"] == 5
        assert dry["missing_runs"][0]["module"] == "dft"
        assert dry["missing_runs"][0]["source_identity_verified"] is False
        assert session.query(WorkflowJob).count() == before["jobs"]

    with Session(setup_test_db) as session:
        applied = TaskLogService(session).backfill_missing_external_analysis_tasks(
            paper_id=paper_id, apply=True
        )
        session.commit()
        assert applied["expected_new_tasks"] == 1
        assert applied["created_tasks"] == 1
        job = session.scalar(
            select(WorkflowJob).where(
                WorkflowJob.payload["external_analysis_run_id"].astext == str(missing_run_id)
            )
        )
        assert job is not None
        assert job.payload["backfilled"] is True
        assert job.payload["paper_code"] == "TB001"
        assert job.payload["source"] == "web_ai"
        assert job.payload["source_identity_verified"] is False
        assert "已认证" not in job.payload["source_display"]
        assert job.payload["module"] == "dft"
        assert job.result["metrics"]["total"] == 5
        assert job.result["metrics"]["success"] == 1
        assert job.result["metrics"]["pending"] == 2
        assert job.result["metrics"]["problem"] == 2
        assert job.result["metrics"]["blocking"] == 1
        assert len(job.result["problem_items"]) == 2
        assert job.result["last_action"] == "backfilled"

    with Session(setup_test_db) as session:
        second = TaskLogService(session).backfill_missing_external_analysis_tasks(
            paper_code="TB001", apply=True
        )
        session.commit()
        assert second["expected_new_tasks"] == 0
        assert second["created_tasks"] == 0
        assert second["existing_tasks"] == 2
        assert session.query(WorkflowJob).count() == before["jobs"] + 1
        assert session.query(ExternalAnalysisRun).count() == before["runs"]
        assert session.query(ExternalAnalysisCandidate).count() == before["candidates"]
        assert session.query(ContentEvidenceItem).count() == before["content"]
        assert session.query(EvidenceClaim).count() == before["claims"]
        assert session.query(ContentReviewBundle).count() == before["bundles"]
        assert session.scalar(
            select(WorkflowJob).where(WorkflowJob.job_id == "existing-agent-task")
        ) is not None
        assert session.scalar(
            select(WorkflowJob).where(
                WorkflowJob.payload["external_analysis_run_id"].astext == str(existing_run_id)
            )
        ) is not None
