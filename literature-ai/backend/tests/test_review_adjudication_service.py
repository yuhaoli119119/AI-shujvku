from app.services.dft_review_service import DFT_REVIEW_FIELD_ALIASES
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.services.review_service import ReviewService
from app.db.models import PaperCorrection
from datetime import datetime, timedelta
from uuid import uuid4


def test_dft_review_alias_maps_unit_to_unit():
    assert DFT_REVIEW_FIELD_ALIASES["unit"] == "unit"


def test_review_adjudication_treats_proposed_as_negative():
    assert ReviewAdjudicationService._decision_bucket("PROPOSED") == "negative"
    assert ReviewAdjudicationService._decision_bucket("PASS") == "positive"


def test_review_conflict_treats_proposed_as_negative():
    assert ReviewConflictAggregationService._decision_bucket("PROPOSED") == "negative"
    assert ReviewConflictAggregationService._decision_bucket("PASS") == "positive"


def test_review_adjudication_balances_positive_and_negative_scores():
    service = ReviewAdjudicationService.__new__(ReviewAdjudicationService)
    positive = {
        "decision": "PASS",
        "confidence": 0.91,
        "evidence": {"locator": {"page": 3, "locator_status": "exact_page"}, "evidence_text": "Exact evidence."},
    }
    negative = {
        "decision": "REJECT",
        "confidence": 0.91,
        "evidence": {"locator": {"page": 3, "locator_status": "exact_page"}, "evidence_text": "Exact evidence."},
    }

    assert service._opinion_score(positive) == service._opinion_score(negative)


def test_review_conflict_uses_materialized_id_for_structured_create_targets():
    paper_id = uuid4()
    target_id = uuid4()
    row = PaperCorrection(
        id=uuid4(),
        paper_id=paper_id,
        source="ide_ai",
        field_name="sections",
        target_path="sections:new:create",
        operation="create",
        proposed_value={"section_title": "Results", "text": "Evidence-backed section."},
        reason="create section",
        status="approved",
        evidence_payload={"structured_create": {"target_id": str(target_id)}},
    )

    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)

    assert service._correction_target(row) == (paper_id, "sections", str(target_id), "create")


def test_review_conflict_does_not_group_unmaterialized_creates_as_new():
    paper_id = uuid4()
    first = PaperCorrection(
        id=uuid4(),
        paper_id=paper_id,
        source="ide_ai",
        field_name="sections",
        target_path="sections:new:create",
        operation="create",
        proposed_value={"section_title": "Results", "text": "A"},
        reason="create section",
        status="pending",
    )
    second = PaperCorrection(
        id=uuid4(),
        paper_id=paper_id,
        source="ide_ai",
        field_name="sections",
        target_path="sections:new:create",
        operation="create",
        proposed_value={"section_title": "Methods", "text": "B"},
        reason="create section",
        status="pending",
    )

    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)

    assert service._correction_target(first)[2] == f"correction:{first.id}"
    assert service._correction_target(second)[2] == f"correction:{second.id}"


def test_review_conflict_ignores_pending_correction_superseded_by_approved_same_value():
    paper_id = uuid4()
    target_id = uuid4()
    pending = PaperCorrection(
        id=uuid4(),
        paper_id=paper_id,
        source="ide_ai",
        field_name="sections",
        target_path=f"sections:{target_id}:section_title",
        operation="replace",
        proposed_value="Abstract & Introduction",
        reason="draft",
        status="pending",
        created_at=datetime.utcnow(),
    )
    approved = PaperCorrection(
        id=uuid4(),
        paper_id=paper_id,
        source="ide_ai",
        field_name="sections",
        target_path=f"sections:{target_id}:section_title",
        operation="replace",
        proposed_value="Abstract & Introduction",
        reason="final",
        status="approved",
        created_at=datetime.utcnow() + timedelta(seconds=1),
    )

    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)

    assert service._is_superseded_pending_correction(pending, [pending, approved]) is True


def test_section_create_requires_strong_anchor_beyond_bare_page():
    assert ReviewService._has_strong_section_anchor({"page": 1}) is False
    assert ReviewService._has_strong_section_anchor({"page": 1, "quoted_text": "Methods evidence"}) is True
    assert ReviewService._has_strong_section_anchor({"evidence_location": {"page": 2, "bbox": [1, 2, 3, 4]}}) is True
