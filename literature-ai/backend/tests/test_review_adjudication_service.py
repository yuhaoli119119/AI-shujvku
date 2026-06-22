from app.services.dft_review_service import DFT_REVIEW_FIELD_ALIASES
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.services.review_service import ReviewService
from app.db.models import DFTResult, PaperCorrection
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


def test_review_conflict_treats_approved_non_value_dft_correction_as_settled_without_source_metadata():
    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)
    correction = {
        "paper_id": "paper-1",
        "target_type": "dft_results",
        "target_id": "row-1",
        "field_name": "adsorbate",
        "source_type": "paper_correction",
        "evidence": {
            "review_source": "ide_ai",
            "review_source_label": "older-run",
            "review_decision": "PROPOSED",
        },
        "value": "OH*",
        "created_at": datetime.utcnow() + timedelta(seconds=1),
    }
    opinion = {
        "paper_id": "paper-1",
        "target_type": "dft_results",
        "target_id": "row-1",
        "field_name": "adsorbate",
        "source_type": "object_review_audit",
        "source_id": "opinion-1",
        "source": "ide_ai",
        "source_label": "newer-run",
        "decision": "PROPOSED",
        "value": "OH*",
        "created_at": datetime.utcnow(),
    }

    service._same_opinion_target = lambda left, right: True
    service._opinion_matches_current_target_state = lambda item: item.get("value") == "OH*"
    service._opinion_values_match = lambda left, right: True
    service._dft_scalar_correction_can_adopt_opinion = lambda left, right: True

    assert service._approved_correction_adopts_opinion(correction, opinion) is True


def test_review_conflict_counts_repeated_dynamic_source_once():
    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)
    older = {
        "source_id": "older",
        "source": "ide_audit",
        "source_label": "dynamic-ai-run",
        "created_at": "2026-06-22T01:00:00+00:00",
        "confidence": 0.9,
    }
    newer = {
        **older,
        "source_id": "newer",
        "created_at": "2026-06-22T01:01:00+00:00",
    }

    assert service._collapse_repeated_source_opinions([older, newer]) == [newer]


def test_review_conflict_active_view_ignores_materialized_external_audits():
    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)
    active = {"source_type": "object_review_audit", "status": "candidate", "source_id": "active"}
    materialized = {"source_type": "object_review_audit", "status": "materialized", "source_id": "done"}
    verified = {"source_type": "extraction_field_review", "status": "verified", "source_id": "verified"}

    assert service._active_opinions([active, materialized, verified]) == [active, verified]


def test_review_conflict_active_third_ai_adjudication_supersedes_prior_dft_opinions():
    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)
    prior = {
        "target_type": "dft_results",
        "source_id": "prior",
        "created_at": "2026-06-22T01:00:00+00:00",
        "raw_payload": {},
    }
    adjudication = {
        "target_type": "dft_results",
        "source_id": "adjudication",
        "created_at": "2026-06-22T01:01:00+00:00",
        "raw_payload": {"adjudication_role": "third_ai"},
    }

    assert service._collapse_active_dft_adjudication([prior, adjudication]) == [adjudication]


def test_review_conflict_whole_row_dft_ignores_missing_fields_and_locator_differences():
    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)
    row = DFTResult(
        property_type="adsorption_energy",
        adsorbate="CO2",
        reaction_step=None,
        value=-1.05,
        unit="eV",
    )
    service._load_target_row = lambda target_type, target_id: row
    base = {
        "paper_id": "paper-1",
        "target_type": "dft_results",
        "target_id": "row-1",
        "field_name": "dft_results",
        "unit": None,
        "status": "candidate",
    }
    first = {
        **base,
        "decision": "PROPOSED",
        "value": {
            "value": -1.05,
            "unit": "eV",
            "property_type": "adsorption_energy",
            "adsorbate": "CO2",
            "material_identity": "FeFe@C2N",
        },
        "evidence": {"page": 5},
        "raw_payload": {},
    }
    second = {
        **base,
        "decision": "REVISE",
        "value": {
            "value": -1.05,
            "unit": "eV",
            "property_type": "adsorption_energy",
            "adsorbate": "CO2",
            "reaction_step": "CO2 adsorption",
            "material_identity": "FeFe@C2N",
        },
        "evidence": {"page": 6},
        "raw_payload": {},
    }

    assert service._conflict_types([first, second]) == []


def test_review_conflict_whole_row_dft_keeps_real_numeric_disagreement():
    service = ReviewConflictAggregationService.__new__(ReviewConflictAggregationService)
    row = DFTResult(property_type="adsorption_energy", value=-1.05, unit="eV")
    service._load_target_row = lambda target_type, target_id: row
    base = {
        "paper_id": "paper-1",
        "target_type": "dft_results",
        "target_id": "row-1",
        "field_name": "dft_results",
        "decision": "REVISE",
        "status": "candidate",
        "unit": None,
        "evidence": {"page": 5},
        "raw_payload": {},
    }
    first = {**base, "value": {"value": -1.05, "unit": "eV"}}
    second = {**base, "value": {"value": -0.85, "unit": "eV"}}

    assert "value_conflict" in service._conflict_types([first, second])


def test_section_create_requires_strong_anchor_beyond_bare_page():
    assert ReviewService._has_strong_section_anchor({"page": 1}) is False
    assert ReviewService._has_strong_section_anchor({"page": 1, "quoted_text": "Methods evidence"}) is True
    assert ReviewService._has_strong_section_anchor({"evidence_location": {"page": 2, "bbox": [1, 2, 3, 4]}}) is True
