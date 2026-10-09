from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.models import DFTResult, Paper
from app.rag.quality import _dft_minimum_field_reasons
from app.schemas.dft_review_bundle import OfflineObjectReviewAudit
from app.services.ai_verification_service import AIVerificationService
from app.services.catalyst_analysis_service import CatalystAnalysisService, _ReadyRow
from app.services.dft_identity_service import build_dft_identity_v2
from app.services.dft_ml_policy import analysis_entity_id, resolve_dft_unit
from app.services.dft_review_bundle_service import DFTReviewBundleService
from app.services.dft_review_queue_service import DFTReviewQueueService
from app.services.ide_prompt_service import build_ide_review_prompt
from app.services.verification_session_candidates import VerificationSessionDFTCandidateMixin
from app.utils.review_safety import required_dft_review_fields


pytestmark = pytest.mark.no_test_database


def _anonymous_ready(*, paper: Paper, entity: str, property_type: str, value: float, adsorbate=None, reaction_step=None):
    row = DFTResult(
        id=uuid4(),
        paper_id=paper.id,
        catalyst_sample_id=None,
        property_type=property_type,
        value=value,
        unit=None,
        adsorbate=adsorbate,
        reaction_step=reaction_step,
        evidence_text=f"{entity} {value}",
        evidence_payload={"analysis_entity_id": entity},
        identity_version=2,
    )
    unit = resolve_dft_unit(property_type, None).unit
    record = {
        "record_id": str(row.id),
        "is_ml_ready": True,
        "target": {
            "normalized_value": value,
            "normalized_unit": unit,
            "normalization_status": "normalized",
        },
        "linked_dft_setting": None,
        "setting_link_status": "missing",
    }
    return _ReadyRow(row=row, paper=paper, record=record, catalyst=None)


def test_units_are_never_dropped_and_standard_units_are_inferred():
    assert resolve_dft_unit("adsorption_energy", None).as_payload() == {
        "unit": "eV",
        "unit_origin": "ai_inferred",
        "unit_inference_basis": "property_taxonomy:adsorption_energy",
        "unit_confidence": 0.95,
    }
    assert resolve_dft_unit("ICOHP", "not reported").unit == "eV"
    assert resolve_dft_unit("bond_length_M-M", None).unit == "Å"
    generic_length = resolve_dft_unit("bond_length", None)
    assert generic_length.unit == "Å"
    assert generic_length.origin == "ai_inferred"
    assert generic_length.confidence == 0.85
    assert resolve_dft_unit("charge_transfer", None).unit == "e"
    assert resolve_dft_unit("magnetic_moment", None).unit == "μB"
    assert resolve_dft_unit("unknown_custom_property", None).resolved is False


def test_identity_accepts_confirmed_value_without_catalyst_or_optional_metadata():
    identity = build_dft_identity_v2({
        "paper_id": str(uuid4()),
        "corrected_value": {
            "analysis_entity_id": "anonymous-row-7",
            "property_type": "adsorption_energy",
            "value": -1.82,
            "unit": None,
        },
    })
    assert identity.error_codes == ()
    assert identity.observation_key
    assert identity.identity_payload["observation"]["unit"] == "eV"
    assert identity.identity_payload["unit_resolution"]["unit_origin"] == "ai_inferred"


def test_only_property_and_value_are_required_for_dft_review():
    row = DFTResult(property_type="adsorption_energy", value=-1.0, unit=None)
    assert required_dft_review_fields(row) == ("energy_type", "value")


def test_anonymous_source_entity_pairs_two_properties_for_regression():
    paper = Paper(id=uuid4(), paper_code="T0001", title="Anonymous catalyst regression")
    ready = []
    for index in range(3):
        entity = f"table-s1-row-{index}"
        adsorption = _anonymous_ready(
            paper=paper,
            entity=entity,
            property_type="adsorption_energy",
            value=-1.0 - index,
            adsorbate="Li2S",
        )
        dissociation = _anonymous_ready(
            paper=paper,
            entity=entity,
            property_type="li2s_decomposition_barrier",
            value=0.4 + index * 0.1,
            adsorbate="Li2S",
            reaction_step="Li2S dissociation",
        )
        assert adsorption.analysis_entity_id == dissociation.analysis_entity_id
        assert analysis_entity_id(adsorption.row).startswith("source_entity:")
        ready.extend((adsorption, dissociation))

    service = CatalystAnalysisService(None)
    service._load_pair_analysis_rows = lambda _library: (
        ready,
        Counter(),
        {"pair_analysis_ready_numeric_rows": len(ready)},
    )
    payload = service.correlation(
        library_name=None,
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
    )
    assert payload["n_catalysts"] == 3
    assert len(payload["points"]) == 3
    assert all(point["catalyst_sample_id"] is None for point in payload["points"])
    assert all(point["analysis_entity_id"].startswith("source_entity:") for point in payload["points"])



def test_offline_new_candidate_accepts_core_value_without_optional_metadata_or_source_unit():
    audit = OfflineObjectReviewAudit(
        target_id="new",
        decision="new_candidate",
        evidence_checked=True,
        evidence_ids=["evidence-1"],
        corrected_value={"property_type": "adsorption_energy", "value": -1.25},
        reason="Exact table cell",
    )
    assert audit.corrected_value["value"] == -1.25
    assert DFTReviewBundleService._validate_structured_dft_value(audit.model_dump()) == []


def test_offline_unknown_property_still_requires_meaningful_unit():
    errors = DFTReviewBundleService._validate_structured_dft_value({
        "corrected_value": {"property_type": "unknown_custom_property", "value": 3.0},
    })
    assert [item["code"] for item in errors] == ["unresolved_unit"]


def test_missing_known_unit_is_not_a_rag_quality_blocker():
    row = DFTResult(property_type="bond_length", value=2.4, unit=None)
    assert _dft_minimum_field_reasons(row) == []


def test_terminal_unusable_queue_state_never_requests_another_ai():
    state = DFTReviewQueueService.build_dft_workflow_state(
        gate=SimpleNamespace(eligible=False, reasons=("unsafe_review",), review_status="needs_human"),
        object_review_audits=[],
        candidate_status="ai_terminal_unusable",
    )
    assert state["state"] == "terminal_unusable"
    assert state["next_required_action"] == "none"
    assert "不再派给其他 AI" in state["reason"]


def test_canonical_dft_prompt_matches_ml_first_closure_policy():
    prompt = build_ide_review_prompt("dft")
    assert "只审核 energy_type 和 value" in prompt
    assert "corrected_value 只强制包含 property_type 和 value" in prompt
    assert "defer/NEEDS_HUMAN 是终态" in prompt
    assert "second-AI" not in prompt
    assert "corrected_value 至少包含 material_identity" not in prompt



def test_new_candidate_materializer_infers_unit_and_keeps_anonymous_value():
    payload = {
        "target_type": "dft_results",
        "target_id": "new",
        "decision": "new_candidate",
        "corrected_value": {"property_type": "adsorption_energy", "value": -1.75},
        "evidence_location": {
            "page": 3,
            "table": "Table 2",
            "quoted_text": "Adsorption energy is -1.75.",
        },
        "reason": "Exact source table value",
    }
    item, reason = VerificationSessionDFTCandidateMixin()._new_dft_candidate_item(
        payload,
        paper_id=uuid4(),
        run=SimpleNamespace(
            paper_id=uuid4(),
            source="web_ai",
            source_label="web-ai",
        ),
        candidate_id=uuid4(),
    )
    assert reason == ""
    assert item is not None
    assert item["material_identity"] is None
    assert item["unit"] == "eV"
    assert item["evidence_payload"]["unit_resolution"]["unit_origin"] == "ai_inferred"



def test_one_blocked_core_field_immediately_closes_the_whole_record():
    status = AIVerificationService._dft_record_status({
        "energy_type": "ai_blocked",
        "value": "pending",
    })
    assert status == "terminal_unusable"
