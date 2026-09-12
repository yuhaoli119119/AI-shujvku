from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID, uuid4
import pytest

from app.config import Settings
from app.db.models import Paper, WorkflowJob, VerificationSessionPaperClaim
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.verification_session_service import VerificationSessionService


@pytest.mark.no_test_database
def test_artifact_reliability_audit_paper_is_read_only_and_invokes_no_commit_or_flush():
    """1. 实际调用 ArtifactReliabilityAuditService.audit_paper，验证只读性，确保没有 commit、flush 或对象变更。"""
    mock_session = MagicMock()
    mock_settings = Settings()

    paper_id = uuid4()
    paper = Paper(id=paper_id, title="Test Battery Paper", library_name="default")
    mock_session.get.return_value = paper
    mock_session.scalars.return_value.all.return_value = []

    service = ArtifactReliabilityAuditService(mock_session, mock_settings)
    result = service.audit_paper(paper_id)

    # Must produce expected read-only audit structure
    assert result["paper_id"] == str(paper_id)
    assert result["title"] == "Test Battery Paper"
    assert "figure_issue_counts" in result
    assert "table_issue_counts" in result
    assert "locator_issue_counts" in result

    # Verify strictly 0 writes, commits or flushes
    assert mock_session.commit.call_count == 0
    assert mock_session.flush.call_count == 0
    assert mock_session.add.call_count == 0
    assert mock_session.delete.call_count == 0


@pytest.mark.no_test_database
def test_dft_auto_advance_batch_preserves_audit_only_safety_boundary():
    """2. DFT 自动推进维持 audit_only 安全边界，不得直接写入最终事实。"""
    mock_session = MagicMock()
    service = ReviewAdjudicationService.__new__(ReviewAdjudicationService)
    service.session = mock_session

    paper_id = str(uuid4())
    target_id = str(uuid4())

    dft_row = {
        "paper_id": paper_id,
        "target_type": "dft_result",
        "target_id": target_id,
        "field_name": "adsorption_energy",
    }
    adjudication = {
        "adjudication_mode": "auto",
        "recommended_action": "accept_recommendation",
        "recommended_payload": {"proposed_value": "-2.15 eV"},
        "reason_summary": "高置信度原文定位匹配",
    }

    # Calling _execute_action in auto_mode for DFT must return audit_only and write nothing
    res = service._execute_action(
        row=dft_row,
        adjudication=adjudication,
        reviewer="review_center_auto",
        auto_mode=True,
    )

    assert res["status"] == "skipped"
    assert res["action"] == "audit_only"
    assert res["reason"] == "dft_auto_advance_disabled"
    assert res["writes_final_truth"] is False
    assert res["paper_id"] == paper_id


@pytest.mark.no_test_database
def test_ai_verification_service_process_batch_requires_dedicated_capability():
    """3. 实际调用 AIVerificationService.process_batch，验证非 ai_verify_content 身份不能写入权威核验。"""
    mock_session = MagicMock()
    service = AIVerificationService(mock_session, Settings())

    non_dedicated_identity = AuthenticatedAIVerificationIdentity(
        source_identity="ide_agent",
        source_label="ide_agent",
        model_agent="mock_agent",
        capabilities=frozenset({"read_papers", "propose_corrections"}),
        identity_verified=True,
    )

    paper_id = uuid4()
    submission = {
        "target_type": "dft_results",
        "target_id": str(uuid4()),
        "field_name": "value",
        "proposed_value": "1.25",
        "confidence": 0.95,
        "evidence_text": "Binding energy was calculated as 1.25 eV.",
    }

    with pytest.raises(PermissionError) as exc_info:
        service.process_batch(
            paper_id=paper_id,
            submissions=[submission],
            identity=non_dedicated_identity,
        )
    assert "Missing capability: ai_verify_content" in str(exc_info.value)

    # Dedicated identity with ai_verify_content passes identity check
    dedicated_identity = AuthenticatedAIVerificationIdentity(
        source_identity="dedicated_single_ai",
        source_label="dedicated_single_ai",
        model_agent="mock_agent",
        capabilities=frozenset({"read_papers", "ai_verify_content"}),
        identity_verified=True,
    )
    AIVerificationService._require_identity(dedicated_identity)


@pytest.mark.no_test_database
def test_verification_session_service_lifecycle_create_get_settle():
    """4. 实际调用 VerificationSessionService.create_session/get_session/settle_session，断言真实状态为 settled 且结算字段完全一致。"""
    paper_id = uuid4()
    paper = Paper(id=paper_id, title="Test Paper", paper_code="B0001", library_name="default")

    mock_session = MagicMock()
    mock_session.get_bind.return_value.dialect.name = "sqlite"
    mock_session.scalars.return_value.all.return_value = [paper]
    mock_session.scalar.return_value = None

    service = VerificationSessionService(mock_session, Settings())
    service._resolve_papers = MagicMock(return_value=[paper])

    job_store: dict[str, WorkflowJob] = {}

    def add_mock(obj):
        if isinstance(obj, WorkflowJob):
            job_store[obj.job_id] = obj

    mock_session.add.side_effect = add_mock
    service._get_session_job = MagicMock(side_effect=lambda sid: job_store[sid])

    # 1. create_session with dft_only scope
    created = service.create_session(
        paper_ids=[paper_id],
        scope="dft_only",
        refresh_materials=False,
        reviewer="test_reviewer",
    )
    assert created["status"] == "active"
    assert created["scope"] == "dft_only"
    assert created["settlement"] is None
    session_id = created["session_id"]

    # 2. get_session
    retrieved = service.get_session(session_id)
    assert retrieved["session_id"] == session_id
    assert retrieved["status"] == "active"
    assert retrieved["scope"] == "dft_only"

    # 3. settle_session (authorized Owner settlement)
    service._settle_low_risk_notes = MagicMock(
        return_value={"materialized_count": 0, "auto_materialized_count": 0, "skipped_count": 0}
    )
    service._settle_high_risk_targets = MagicMock(
        return_value={"auto_applied_count": 1, "manual_conflict_count": 0, "blocked_reason_counts": {}}
    )

    settled = service.settle_session(session_id, reviewer="owner_user")
    # Assert authentic terminal state is 'settled' - NOT 'completed'!
    assert settled["status"] == "settled"
    assert settled["settlement"] is not None
    # Assert settlement only has genuine backend fields
    settlement_keys = set(settled["settlement"].keys())
    assert settlement_keys == {"settled_at", "scope", "low_risk_notes", "high_risk"}
    assert "verified_count" not in settled["settlement"]
    assert "active_phase" not in settled["settlement"]
    assert "settled_phase" not in settled["settlement"]
    assert "findings" not in settled["settlement"]


@pytest.mark.no_test_database
def test_resolving_one_conflict_leaves_other_conflicts_on_same_paper_actionable():
    """5. 调用真实 ReviewAdjudicationService 判定，证明某条问题被核验后，同论文下其余未完成问题仍保持 actionable。"""
    resolved_conflict_row = {
        "conflict": False,
        "adjudication": {
            "is_resolved": True,
            "adjudication_mode": "verified",
            "recommended_action": "verify",
        },
    }
    unresolved_conflict_row = {
        "conflict": True,
        "adjudication": {
            "is_resolved": False,
            "adjudication_mode": "manual",
            "recommended_action": "manual_review",
            "reason": "置信度不足且缺少强原文定位证据",
        },
    }

    assert ReviewAdjudicationService.is_actionable_conflict(resolved_conflict_row) is False
    assert ReviewAdjudicationService.is_actionable_conflict(unresolved_conflict_row) is True
