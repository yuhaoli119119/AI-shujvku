from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    ContentEvidenceItem,
    ContentWebReviewBundleV2,
    ContentWebReviewLocalVerificationResult,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCorrection,
)
from app.main import app
from app.services.content_web_review_bundle_retention_service import (
    ContentWebReviewBundleRetentionService,
)
from app.services.content_web_review_bundle_v2_service import (
    ContentWebReviewBundleV2Service,
)


def _factory(engine):
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def _seed(engine, tmp_path) -> UUID:
    pdf = tmp_path / "retention.pdf"
    pdf.write_bytes(b"%PDF-1.4\nretention\n%%EOF")
    with _factory(engine).begin() as session:
        paper = Paper(
            paper_code="WV-RET",
            title="Retention",
            pdf_path=str(pdf),
            authors=[],
        )
        session.add(paper)
        session.flush()
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="mechanism",
            claim_text="Safe claim",
            evidence_text="Safe claim",
        )
        session.add(claim)
        session.flush()
        session.add(
            ContentEvidenceItem(
                paper_id=paper.id,
                category="mechanism_evidence",
                source_type="mechanism_claim",
                source_id=str(claim.id),
                content="Safe claim",
                review_status="validated",
                citation_status="citable",
                risk_flags=[],
            )
        )
        return paper.id


def _clone_bundle(
    source: ContentWebReviewBundleV2,
    *,
    status: str = "generated",
    proposal_payload=None,
    fingerprint: str | None = None,
    created_at: datetime | None = None,
) -> ContentWebReviewBundleV2:
    values = {
        "paper_id": source.paper_id,
        "policy_version": source.policy_version,
        "snapshot_fingerprint": fingerprint or source.snapshot_fingerprint,
        "active_generation_key": None,
        "manifest": dict(source.manifest),
        "status": status,
        "created_by": "retention-test",
        "created_at": created_at or datetime.utcnow(),
        "updated_at": created_at or datetime.utcnow(),
    }
    if proposal_payload is not None:
        values["proposal_payload"] = proposal_payload
    return ContentWebReviewBundleV2(**values)


def _local_result(bundle_id: UUID) -> ContentWebReviewLocalVerificationResult:
    return ContentWebReviewLocalVerificationResult(
        bundle_id=bundle_id,
        plan_item_id=uuid4(),
        payload_hash="1" * 64,
        target_type="mechanism_claim",
        target_id=str(uuid4()),
        field_name="claim_text",
        object_snapshot_hash="2" * 64,
        outcome="CONFIRMED",
        checked_evidence_ids=[],
        checked_pages=[],
        verification_note="audit result",
        status="applied",
        stale_reasons=[],
        applied_by="local-ai",
    )


def test_retention_deletes_unused_duplicates_but_keeps_one_current_bundle(
    setup_test_db,
    tmp_path,
):
    paper_id = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        generated = service.generate(paper_id=paper_id, module="paper_content")
        source = service._bundle(UUID(generated["bundle_id"]))
        duplicate = _clone_bundle(source)
        session.add(duplicate)
        session.flush()
        report = ContentWebReviewBundleRetentionService(session).cleanup(
            paper_id=paper_id,
            dry_run=False,
            limit=100,
            exclude_bundle_ids={UUID(generated["bundle_id"])},
        )
        assert report["duplicate_deleted_count"] == 1
        assert report["expired_deleted_count"] == 0
        assert len(report["deleted_bundle_ids"]) == 1
        assert report["deleted_bundle_ids"] == [str(duplicate.id)]
        assert session.get(
            ContentWebReviewBundleV2,
            UUID(generated["bundle_id"]),
        ) is not None
        assert session.scalar(
            select(func.count())
            .select_from(ContentWebReviewBundleV2)
            .where(ContentWebReviewBundleV2.paper_id == paper_id)
        ) == 1


def test_retention_dry_run_and_apply_delete_only_expired_unused_rows(
    setup_test_db,
    tmp_path,
):
    paper_id = _seed(setup_test_db, tmp_path)
    old = datetime.utcnow() - timedelta(days=31)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        current_data = service.generate(paper_id=paper_id, module="paper_content")
        current = service._bundle(UUID(current_data["bundle_id"]))
        expired = _clone_bundle(
            current,
            status="stale",
            fingerprint="a" * 64,
            created_at=old,
        )
        proposal = _clone_bundle(
            current,
            proposal_payload={"accepted": True},
            fingerprint="b" * 64,
            created_at=old,
        )
        awaiting_human = _clone_bundle(
            current,
            status="awaiting_human",
            fingerprint="c" * 64,
            created_at=old,
        )
        web_validated = _clone_bundle(
            current,
            status="web_proposal_validated",
            fingerprint="1" * 64,
            created_at=old,
        )
        awaiting_local = _clone_bundle(
            current,
            status="awaiting_local_verification",
            fingerprint="2" * 64,
            created_at=old,
        )
        applied = _clone_bundle(
            current,
            status="applied",
            fingerprint="d" * 64,
            created_at=old,
        )
        finalized = _clone_bundle(
            current,
            status="finalized",
            fingerprint="e" * 64,
            created_at=old,
        )
        with_local_result = _clone_bundle(
            current,
            status="stale",
            fingerprint="f" * 64,
            created_at=old,
        )
        session.add_all(
            [
                expired,
                proposal,
                awaiting_human,
                web_validated,
                awaiting_local,
                applied,
                finalized,
                with_local_result,
            ]
        )
        session.flush()
        session.add(_local_result(with_local_result.id))
        session.flush()
        before_count = session.scalar(
            select(func.count()).select_from(ContentWebReviewBundleV2)
        )
        dry_run = ContentWebReviewBundleRetentionService(session).cleanup(
            paper_id=paper_id,
            dry_run=True,
            older_than_days=30,
            limit=100,
        )
        assert dry_run["expired_deleted_count"] == 1
        assert dry_run["protected_count"] == 7
        assert session.scalar(
            select(func.count()).select_from(ContentWebReviewBundleV2)
        ) == before_count
        applied_report = ContentWebReviewBundleRetentionService(session).cleanup(
            paper_id=paper_id,
            dry_run=False,
            older_than_days=30,
            limit=100,
        )
        assert applied_report["expired_deleted_count"] == 1, applied_report
        assert session.get(ContentWebReviewBundleV2, expired.id) is None
        for protected in (
            proposal,
            awaiting_human,
            web_validated,
            awaiting_local,
            applied,
            finalized,
            with_local_result,
        ):
            assert session.get(ContentWebReviewBundleV2, protected.id) is not None
        assert session.get(
            ContentWebReviewLocalVerificationResult,
            session.scalar(
                select(ContentWebReviewLocalVerificationResult.id).where(
                    ContentWebReviewLocalVerificationResult.bundle_id
                    == with_local_result.id
                )
            ),
        ) is not None


def test_history_api_reports_reusable_protected_cleanup_and_json_estimates(
    setup_test_db,
    tmp_path,
):
    paper_id = _seed(setup_test_db, tmp_path)
    old = datetime.utcnow() - timedelta(days=31)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        current_data = service.generate(paper_id=paper_id, module="paper_content")
        current = service._bundle(UUID(current_data["bundle_id"]))
        session.add_all(
            [
                _clone_bundle(
                    current,
                    proposal_payload={"accepted": True},
                    fingerprint="b" * 64,
                    created_at=old,
                ),
                _clone_bundle(
                    current,
                    status="stale",
                    fingerprint="c" * 64,
                    created_at=old,
                ),
            ]
        )
    response = TestClient(app).get(
        "/api/content-knowledge/review-bundles/v2/history",
        params={
            "paper_id": str(paper_id),
            "module": "paper_content",
            "limit": 20,
        },
    )
    assert response.status_code == 200
    history = response.json()
    assert history["total_count"] == 3
    assert history["reusable_count"] == 1
    assert history["protected_count"] == 1
    assert history["cleanup_eligible_count"] == 1
    assert history["estimated_manifest_bytes"] > 0
    assert history["estimated_proposal_bytes"] > 0
    assert len(history["items"]) == 3
    assert {
        "bundle_id",
        "status",
        "created_at",
        "updated_at",
        "selected_modules",
        "bundle_fingerprint",
        "has_proposal",
        "local_result_count",
        "reusable",
        "cleanup_eligible",
    } <= set(history["items"][0])


def test_generate_reuse_and_cleanup_do_not_change_formal_review_truth(
    setup_test_db,
    tmp_path,
):
    paper_id = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        evidence = session.scalar(
            select(ContentEvidenceItem).where(
                ContentEvidenceItem.paper_id == paper_id
            )
        )
        before = (
            evidence.review_status,
            evidence.citation_status,
            session.scalar(select(func.count()).select_from(ExtractionFieldReview)),
            session.scalar(select(func.count()).select_from(EvidenceLocator)),
            session.scalar(select(func.count()).select_from(PaperCorrection)),
        )
        service = ContentWebReviewBundleV2Service(session)
        first = service.generate(paper_id=paper_id, module="paper_content")
        second = service.generate(paper_id=paper_id, module="paper_content")
        assert first["bundle_id"] == second["bundle_id"]
        ContentWebReviewBundleRetentionService(session).cleanup(
            paper_id=paper_id,
            dry_run=False,
            limit=100,
            exclude_bundle_ids={UUID(first["bundle_id"])},
        )
        session.refresh(evidence)
        after = (
            evidence.review_status,
            evidence.citation_status,
            session.scalar(select(func.count()).select_from(ExtractionFieldReview)),
            session.scalar(select(func.count()).select_from(EvidenceLocator)),
            session.scalar(select(func.count()).select_from(PaperCorrection)),
        )
        assert after == before
