from __future__ import annotations

import base64
import copy
import csv
import io
import json
from pathlib import Path
from uuid import UUID, uuid4

import fitz
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    ActiveSiteMetal,
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperRelationship,
    PaperTable,
)
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import export_ml_dataset, get_ai_verification_record_tasks
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.services.dft_export_service import (
    build_dft_csv_rows,
    build_dft_ml_dataset,
    extract_configuration_index,
)
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.utils.ai_verification import (
    AI_VERIFICATION_CAPABILITY,
    ai_target_fingerprint,
    authoritative_ai_review_valid,
    build_structured_table_cell_evidence,
    cached_read_pdf_page_text,
    locator_fingerprint,
    normalize_evidence_text,
    stable_hash,
)
import app.utils.ai_verification as ai_verification_utils
from app.utils.review_safety import (
    bulk_export_gate_results,
    authoritative_human_review_locator_pair_valid,
    is_authoritative_verified_review,
    is_export_eligible_extraction,
    required_dft_review_fields,
    required_review_fields,
)


def _authorize_dft_result(session: Session, dft: DFTResult, *, evidence_paper_id: Any = None, page: int = 1, evidence_text: str | None = None):
    target_paper_id = dft.paper_id
    evidence_paper_id = evidence_paper_id or target_paper_id
    assert target_paper_id == dft.paper_id
    if evidence_paper_id != target_paper_id:
        assert session.scalar(select(PaperRelationship.id).where(
            PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
            ((PaperRelationship.source_paper_id == target_paper_id) & (PaperRelationship.target_paper_id == evidence_paper_id)) |
            ((PaperRelationship.target_paper_id == target_paper_id) & (PaperRelationship.source_paper_id == evidence_paper_id)),
        )) is not None
    evidence_text = evidence_text or dft.evidence_text
    assert evidence_text
    paper = session.get(Paper, evidence_paper_id)
    assert paper is not None
    page_text, error, _ = cached_read_pdf_page_text(session, paper, page)
    assert error is None and normalize_evidence_text(evidence_text) in normalize_evidence_text(page_text)
    for fn in required_review_fields("dft_results", dft):
        session.add(
            ExtractionFieldReview(
                paper_id=target_paper_id,
                target_type="dft_results",
                target_id=str(dft.id),
                field_name=fn,
                reviewer_status="verified",
                target_resolution_status="active",
                reviewer="human_verifier",
                target_fingerprint=ai_target_fingerprint("dft_results", dft),
                evidence_text=evidence_text,
                review_payload={
                    "human_verification": {
                        "decision": "verified",
                        "writes_final_truth": True,
                        "verification_actor_type": "human",
                        "identity_verified": True,
                        "reviewer": "human_verifier",
                    }
                },
            )
        )
        session.add(
            EvidenceLocator(
                paper_id=evidence_paper_id,
                target_type="dft_results",
                target_id=str(dft.id),
                field_name=fn,
                evidence_text=evidence_text,
                page=page,
                locator_status="exact_page",
            )
        )
    session.flush()


def _make_pdf(path: Path, pages_text: dict[int, str]) -> None:
    document = fitz.open()
    max_page = max(pages_text.keys())
    for page_num in range(1, max_page + 1):
        page = document.new_page()
        t = pages_text.get(page_num, "")
        if t:
            page.insert_text((20, 72), t, fontsize=8)
    document.save(path)
    document.close()


def _identity() -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:test-verifier",
        source_label="test-verifier",
        model_agent="codex-test-agent",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}),
        identity_verified=True,
    )


def test_structured_table_reference_is_all_or_none():
    base = {
        "target_type": "dft_results",
        "target_id": str(uuid4()),
        "field_name": "value",
        "decision": "accept",
        "confidence": 0.99,
        "page": 1,
        "expected_target_fingerprint": "fingerprint",
    }
    with pytest.raises(ValueError, match="must be supplied together"):
        AIVerificationSubmission.model_validate({**base, "table_id": str(uuid4())})


def test_structured_table_cell_authorizes_without_weakening_plain_text_gate(setup_test_db, tmp_path):
    pdf_path = tmp_path / "structured-table.pdf"
    _make_pdf(
        pdf_path,
        {
            1: (
                "Table S1. Adsorption energy data.\n"
                "Catalyst Li2S4 adsorption energy (eV)\n"
                "Fe-N4 -1.23 -0.88"
            )
        },
    )
    with Session(setup_test_db) as session:
        paper = Paper(title="Structured table evidence", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        table = PaperTable(
            paper_id=paper.id,
            caption="Table S1. Adsorption energy data.",
            markdown_content=(
                "| Catalyst | Li2S4 adsorption energy (eV) |\n"
                "| --- | --- |\n"
                "| Fe-N4 | -1.23/-0.88 |"
            ),
            page=1,
            extraction_source="pytest",
        )
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="N4",
        )
        session.add_all([table, catalyst])
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.23,
            unit="eV",
            evidence_payload={"material_identity": "Fe-N4"},
        )
        session.add(row)
        session.commit()

        submission = AIVerificationSubmission(
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            decision="accept",
            confidence=0.99,
            evidence_text="",
            page=1,
            expected_target_fingerprint=ai_target_fingerprint("dft_results", row),
            table_id=str(table.id),
            source_row_index=0,
            source_column_index=1,
        )
        result = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        assert result["auto_repaired"] == 1, result["items"]
        review = session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.target_id == str(row.id),
            ExtractionFieldReview.field_name == "value",
        ))
        locator = session.scalar(select(EvidenceLocator).where(
            EvidenceLocator.target_id == str(row.id),
            EvidenceLocator.field_name == "value",
        ))
        assert review is not None and locator is not None
        assert locator.table_id == table.id
        legacy_locator = EvidenceLocator(
            id=uuid4(),
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            evidence_text="legacy PDF evidence",
            page=1,
            locator_status="exact_page",
        )
        assert locator_fingerprint(legacy_locator) == stable_hash({
            "id": str(legacy_locator.id),
            "paper_id": str(paper.id),
            "target_type": "dft_results",
            "target_id": str(row.id),
            "field_name": "value",
            "page": 1,
            "bbox": None,
            "evidence_text": "legacy pdf evidence",
            "locator_status": "exact_page",
        })
        assert review.review_payload["ai_verification"]["table_evidence"] == {
            "kind": "paper_table_cell.v1",
            "table_id": str(table.id),
            "source_row_index": 0,
            "source_column_index": 1,
        }
        assert authoritative_ai_review_valid(session, review, row) is True

        electron_row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="bader_charge_transfer",
            adsorbate="Li2S4",
            value=-0.52,
            unit="e",
            evidence_payload={"material_identity": "Fe-N4"},
        )
        electron_checks = AIVerificationService(session)._content_checks(
            "dft_results",
            electron_row,
            "value",
            electron_row.value,
            electron_row.unit,
            (
                "caption=Table S6 charge distribution with positive and negative signs "
                "indicating electron loss and gain; row=Fe-N4; column=Li2S4; "
                "cell_context=electron loss and gain Li2S4 -0.52"
            ),
        )
        assert electron_checks["unit_matches"] is True
        assert electron_checks["value_unit_same_evidence_item"] is True
        service = AIVerificationService(session)
        assert service._content_checks(
            "dft_results", row, "adsorbate", "Li2S4", None,
            "column=E_a/E_a^sol(Li₂S₄) (eV)",
        )["adsorbate_consistent"] is True
        assert service._content_checks(
            "dft_results", electron_row, "energy_type", "bader_charge_transfer", None,
            "caption=Table S6 charge distribution with electron loss and gain",
        )["energy_type_consistent"] is True
        for property_type, evidence in (
            ("zero_point_energy_correction", "row=ZPE (eV)"),
            ("entropy_correction_ts", "row=TS (eV); caption=entropy correction"),
            ("adsorption_configuration_energy", "adsorption configurations and energies"),
            ("adsorption_energy_solvated", "adsorption energies under solvation effect"),
        ):
            semantic_row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=catalyst.id,
                property_type=property_type,
                value=-0.52,
                unit="eV",
                evidence_payload={"material_identity": "Fe-N4"},
            )
            assert service._content_checks(
                "dft_results", semantic_row, "energy_type", property_type, None, evidence,
            )["energy_type_consistent"] is True

        table.markdown_content = table.markdown_content.replace("-1.23", "-9.99")
        session.flush()
        assert authoritative_ai_review_valid(session, review, row) is False


def _seed_test_papers(session: Session, tmp_path: Path):
    main_pdf = tmp_path / "b0102_main.pdf"
    _make_pdf(main_pdf, {1: "Main text page 1", 11: "Main text page 11"})
    main_paper = Paper(title="B0102 Main Paper", pdf_path=str(main_pdf), authors=["Main Author"])

    si_pdf = tmp_path / "s0102_si.pdf"
    _make_pdf(si_pdf, {
        1: "SI page 1",
        17: "Table S2. Single-atom SAC Fe-N4-C catalyst: Fe metal in Fe-N4 coordination on C support.\nCalculated binding energy of Li2S4 on Fe-N4-C (config 2): -1.75 eV.\nReaction step Li2S6 -> Li2S4.",
        18: "Table S2 Continued. Calculated binding energy of Li2S6 on Fe-N4-C (config 1): -2.10 eV.",
        23: "SI end page 23",
    })
    si_paper = Paper(title="S0102 Supplementary Information", pdf_path=str(si_pdf), authors=["SI Author"])

    unrelated_pdf = tmp_path / "unrelated.pdf"
    _make_pdf(unrelated_pdf, {1: "Unrelated paper page 1", 17: "Table S2 in unrelated paper: -1.75 eV."})
    unrelated_paper = Paper(title="Unrelated Paper", pdf_path=str(unrelated_pdf), authors=["Other Author"])

    session.add_all([main_paper, si_paper, unrelated_paper])
    session.flush()

    rel = PaperRelationship(
        source_paper_id=main_paper.id,
        target_paper_id=si_paper.id,
        relationship_type="supplementary",
    )
    session.add(rel)
    session.flush()

    catalyst = CatalystSample(
        paper_id=main_paper.id,
        name="Fe-N4-C",
        catalyst_type="single_atom",
        metal_centers=["Fe"],
        coordination="Fe-N4",
        support="C",
    )
    session.add(catalyst)
    session.flush()

    dft_result = DFTResult(
        paper_id=main_paper.id,
        catalyst_sample_id=catalyst.id,
        property_type="binding_energy",
        adsorbate="Li2S4",
        reaction_step="Li2S6 -> Li2S4",
        value=-1.75,
        unit="eV",
        evidence_text="Table S2. Single-atom SAC Fe-N4-C catalyst: Fe metal in Fe-N4 coordination on C support.\nCalculated binding energy of Li2S4 on Fe-N4-C (config 2): -1.75 eV.\nReaction step Li2S6 -> Li2S4.",
        evidence_payload={"configuration_index": 2, "material_identity": "Fe-N4-C"},
    )
    session.add(dft_result)
    session.flush()

    return main_paper, si_paper, unrelated_paper, catalyst, dft_result


def _seed_dac_test_papers(session: Session, tmp_path: Path):
    dac_main_pdf = tmp_path / "dac_main.pdf"
    _make_pdf(dac_main_pdf, {1: "DAC Main text page 1"})
    dac_main_paper = Paper(title="DAC Main Paper", pdf_path=str(dac_main_pdf), authors=["DAC Main Author"])

    dac_si_pdf = tmp_path / "dac_si.pdf"
    dac_evidence_1 = (
        "Table S1. Dual-atom DAC FeCo-NC catalyst contains Fe and Co in "
        "FeCo-N6 coordination on C support.\n"
        "Calculated binding energy of Li2S4 on FeCo-NC (config 1): -1.85 eV.\n"
        "Reaction step Li2S6 -> Li2S4."
    )
    dac_evidence_2 = (
        "Table S1 Continued. Dual-atom DAC FeCo-NC catalyst contains Fe and Co in "
        "FeCo-N6 coordination on C support.\n"
        "Calculated binding energy of Li2S4 on FeCo-NC (config 2): -1.95 eV.\n"
        "Reaction step Li2S6 -> Li2S4."
    )
    _make_pdf(dac_si_pdf, {
        1: "DAC SI page 1",
        5: dac_evidence_1,
        6: dac_evidence_2,
    })
    dac_si_paper = Paper(title="DAC Supplementary Information", pdf_path=str(dac_si_pdf), authors=["DAC SI Author"])
    session.add_all([dac_main_paper, dac_si_paper])
    session.flush()

    rel = PaperRelationship(
        source_paper_id=dac_main_paper.id,
        target_paper_id=dac_si_paper.id,
        relationship_type="supplementary",
    )
    session.add(rel)
    session.flush()

    dac_catalyst = CatalystSample(
        paper_id=dac_main_paper.id,
        name="FeCo-NC",
        catalyst_type="dual_atom",
        metal_centers=["Fe", "Co"],
        coordination="FeCo-N6",
        support="C",
    )
    session.add(dac_catalyst)
    session.flush()

    dac_result1 = DFTResult(
        paper_id=dac_main_paper.id,
        catalyst_sample_id=dac_catalyst.id,
        property_type="binding_energy",
        adsorbate="Li2S4",
        reaction_step="Li2S6 -> Li2S4",
        value=-1.85,
        unit="eV",
        evidence_text=dac_evidence_1,
        evidence_payload={"configuration_index": 1, "material_identity": "FeCo-NC"},
    )
    dac_result2 = DFTResult(
        paper_id=dac_main_paper.id,
        catalyst_sample_id=dac_catalyst.id,
        property_type="binding_energy",
        adsorbate="Li2S4",
        reaction_step="Li2S6 -> Li2S4",
        value=-1.95,
        unit="eV",
        evidence_text=dac_evidence_2,
        evidence_payload={"configuration_index": 2, "material_identity": "FeCo-NC"},
    )
    session.add_all([dac_result1, dac_result2])
    session.flush()
    return dac_main_paper, dac_si_paper, dac_catalyst, dac_result1, dac_result2


# 1. 主文DFT目标使用明确关联SI的真实页码通过dry_run
def test_main_paper_dft_target_with_associated_si_page_passes_dry_run(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        submission = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": dft_result.evidence_text,
            "page": 17,
            "source_paper_id": str(si_paper.id),
            "reasoning_summary": "Verified against SI Table S2 on page 17.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        batch_result = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )

        assert batch_result["dry_run"] is True
        assert batch_result["database_writes"] is False
        assert (batch_result["auto_repaired"] + batch_result["auto_verified"]) == 1
        item = batch_result["items"][0]
        assert item["outcome"] in ("auto_verified", "auto_repaired")
        assert item["status"] == "ai_verified"
        assert item["blocked_reasons"] == []
        assert item["evidence_checks"]["evidence_paper_authorized"] is True
        assert item["evidence_checks"]["page_valid"] is True
        assert item["evidence_checks"]["evidence_on_pdf_page"] is True
        assert item["target_paper_id"] == str(main_paper.id)
        assert item["source_paper_id"] == str(si_paper.id)


# 2. 非关联论文作为source_paper_id被拒绝
def test_unrelated_paper_as_source_paper_id_is_rejected(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, _, unrelated_paper, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        submission = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Table S2 in unrelated paper: -1.75 eV.",
            "page": 17,
            "source_paper_id": str(unrelated_paper.id),
            "reasoning_summary": "Attempting to cite unrelated paper.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        batch_result = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )

        assert batch_result["auto_verified"] == 0
        assert batch_result["accept_validation_failed"] == 1
        item = batch_result["items"][0]
        assert item["outcome"] == "accept_validation_failed"
        assert item["evidence_checks"]["evidence_paper_authorized"] is False
        assert "evidence_paper_authorized" in item["blocked_reasons"]


# 3. 越界SI页码被拒绝
def test_out_of_range_si_page_is_rejected(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        submission = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Table S2. Calculated binding energy of Li2S4 on Fe-N4-C (config 2): -1.75 eV.",
            "page": 99,
            "source_paper_id": str(si_paper.id),
            "reasoning_summary": "Page 99 is beyond SI 23 pages.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        batch_result = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )

        item = batch_result["items"][0]
        assert item["outcome"] == "accept_validation_failed"
        assert item["evidence_checks"]["page_valid"] is False
        assert "page_valid" in item["blocked_reasons"]


# 4. source_paper_id 与 evidence_paper_id 冲突时必须拒绝
def test_conflicting_source_and_evidence_paper_ids_rejected(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, unrelated_paper, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        submission = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Table S2. Calculated binding energy of Li2S4 on Fe-N4-C (config 2): -1.75 eV.",
            "page": 17,
            "source_paper_id": str(si_paper.id),
            "evidence_paper_id": str(unrelated_paper.id),
            "reasoning_summary": "Providing conflicting source_paper_id and evidence_paper_id.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        batch_result = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )

        assert batch_result["auto_verified"] == 0
        assert batch_result["accept_validation_failed"] == 1
        item = batch_result["items"][0]
        assert item["outcome"] == "accept_validation_failed"
        assert "conflicting_evidence_paper_ids" in item["blocked_reasons"]


# 5. 幂等键必须包含 evidence_paper_id
def test_idempotency_key_includes_evidence_paper_id(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, unrelated_paper, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        sub1 = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Evidence text",
            "page": 17,
            "source_paper_id": str(si_paper.id),
            "reasoning_summary": "Test 1",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })
        sub2 = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Evidence text",
            "page": 17,
            "source_paper_id": str(unrelated_paper.id),
            "reasoning_summary": "Test 2",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })
        key1 = service._idempotency_key(main_paper.id, "dft_results", sub1, _identity())
        key2 = service._idempotency_key(main_paper.id, "dft_results", sub2, _identity())
        assert key1 != key2


# 6. value正确时，不因reaction_step非逐字匹配而失败
def test_value_verification_does_not_fail_on_reaction_step_non_verbatim(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        partial_evidence = "Calculated binding energy of Li2S4 on Fe-N4-C (config 2): -1.75 eV."
        submission = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "value",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": partial_evidence,
            "page": 17,
            "source_paper_id": str(si_paper.id),
            "reasoning_summary": "Numeric value and unit match, reaction_step not in text.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        batch_result = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )

        item = batch_result["items"][0]
        assert item["outcome"] in ("auto_verified", "auto_repaired")
        assert item["status"] == "ai_verified"
        assert "reaction_step_consistent" not in item.get("blocked_reasons", [])


# 7. reaction_step自身核验仍执行对应一致性检查
def test_reaction_step_field_verification_enforces_consistency(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, _, dft_result = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)

        # 7a. Evidence lacking reaction_step -> fails consistency check
        submission_missing_step = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "reaction_step",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Calculated binding energy of Li2S4 on Fe-N4-C (config 2): -1.75 eV.",
            "page": 17,
            "source_paper_id": str(si_paper.id),
            "reasoning_summary": "Checking reaction_step with evidence that lacks it.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        result_fail = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission_missing_step],
            identity=_identity(),
            dry_run=True,
        )
        assert result_fail["items"][0]["outcome"] == "accept_validation_failed"
        assert "reaction_step_consistent" in result_fail["items"][0]["blocked_reasons"]

        # 7b. Evidence containing reaction_step -> passes consistency check
        submission_with_step = AIVerificationSubmission.model_validate({
            "target_type": "dft_results",
            "target_id": str(dft_result.id),
            "field_name": "reaction_step",
            "decision": "accept",
            "confidence": 0.95,
            "evidence_text": "Reaction step Li2S6 -> Li2S4.",
            "page": 17,
            "source_paper_id": str(si_paper.id),
            "reasoning_summary": "Checking reaction_step with matching evidence.",
            "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
        })

        result_pass = service.process_batch(
            paper_id=main_paper.id,
            submissions=[submission_with_step],
            identity=_identity(),
            dry_run=True,
        )
        assert result_pass["items"][0]["outcome"] in ("auto_verified", "auto_repaired")
        assert result_pass["items"][0]["evidence_checks"]["reaction_step_consistent"] is True


# 8. configuration_index进入ML输出但不改变reaction_step (支持嵌套 corrected_value)
def test_configuration_index_in_ml_output_without_mutating_reaction_step(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, _, _, _, dft_result = _seed_test_papers(session, tmp_path)

        # Set nested configuration_index in evidence_payload
        dft_result.evidence_payload = {
            "corrected_value": {"configuration_index": 2},
            "material_identity": "Fe-N4-C",
        }
        session.flush()

        # Mark review as verified for all required fields so it passes safety gate
        _authorize_dft_result(session, dft_result, evidence_paper_id=session.scalar(select(PaperRelationship.target_paper_id).where(PaperRelationship.source_paper_id == main_paper.id)), page=17)

        # Test extract_configuration_index directly
        assert extract_configuration_index({"corrected_value": {"configuration_index": 2}}) == 2
        assert extract_configuration_index({"configuration": {"index": 3}}) == 3
        assert extract_configuration_index({"configuration_index": "1"}) == 1
        assert extract_configuration_index({"configuration_index": "invalid"}) is None
        assert extract_configuration_index({"configuration_index": 0}) is None
        assert extract_configuration_index({"configuration_index": -1}) is None
        assert extract_configuration_index({"configuration_index": "0"}) is None
        assert extract_configuration_index({"configuration_index": "-5"}) is None
        assert extract_configuration_index({"configuration_index": True}) is None
        assert extract_configuration_index({"configuration_index": 1.5}) is None
        assert extract_configuration_index({"configuration_index": "1.5"}) is None

        # Test JSON dataset export
        dataset = build_dft_ml_dataset(session, paper_id=main_paper.id)
        assert len(dataset["records"]) == 1
        rec = dataset["records"][0]
        assert rec["target"]["configuration_index"] == 2
        assert rec["target"]["reaction_step"] == "Li2S6 -> Li2S4"
        assert "Configuration" not in rec["target"]["reaction_step"]

        # Test CSV export
        csv_text, csv_summary = build_dft_csv_rows(session, paper_id=main_paper.id)
        assert csv_summary["exported_rows"] == 1
        assert ",Li2S6 -> Li2S4,2," in csv_text or ",2," in csv_text

        # Database row itself is completely unchanged
        assert dft_result.reaction_step == "Li2S6 -> Li2S4"


# 9. 基础门禁阻断溶剂记录，目标 Profile 再排除回归参数
def test_base_gate_blocks_solvent_and_dac_profile_excludes_regression(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, _, _, _, seeded_dft = _seed_test_papers(session, tmp_path)
        session.delete(seeded_dft)
        session.flush()

        # DAC catalyst specifically for this test
        dac_cat = CatalystSample(paper_id=main_paper.id, name="FeCo-NC", catalyst_type="dual_atom", metal_centers=["Fe", "Co"])
        session.add(dac_cat)
        session.flush()
        dft_dac = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=dac_cat.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.75,
            unit="eV",
            evidence_text=(
                "Dual-atom DAC FeCo-NC catalyst contains Fe and Co.\n"
                "Calculated binding energy of Li2S4 on FeCo-NC: -1.75 eV."
            ),
            evidence_payload={"material_identity": "FeCo-NC"},
        )
        session.add(dft_dac)
        session.flush()

        # Real solvent record with name="DME+DOL", catalyst_type="solvent", NO material_type="solvent" in payload
        solvent_cat = CatalystSample(paper_id=main_paper.id, name="DME+DOL", catalyst_type="solvent")
        session.add(solvent_cat)
        session.flush()
        dft_solvent = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=solvent_cat.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-0.45,
            unit="eV",
            evidence_text="Binding energy of Li2S4 in DME+DOL solvent is -0.45 eV.",
            evidence_payload={"material_identity": "DME+DOL"},
        )
        # Regression record
        dft_regression = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=dac_cat.id,
            property_type="linear_regression_slope",
            adsorbate="Li2S4",
            value=0.92,
            unit="eV",
            evidence_text=(
                "Dual-atom DAC FeCo-NC catalyst contains Fe and Co.\n"
                "Linear regression slope for Li2S4 on FeCo-NC is 0.92 eV."
            ),
            evidence_payload={"material_identity": "FeCo-NC"},
        )
        session.add_all([dft_solvent, dft_regression])
        session.flush()

        profile_pdf = tmp_path / "profile_gate.pdf"
        _make_pdf(profile_pdf, {
            1: dft_dac.evidence_text,
            2: dft_solvent.evidence_text,
            3: dft_regression.evidence_text,
        })
        main_paper.pdf_path = str(profile_pdf)
        session.flush()
        for evidence_page, row in enumerate((dft_dac, dft_solvent, dft_regression), start=1):
            _authorize_dft_result(session, row, page=evidence_page)

        for expected_row in (dft_dac, dft_regression):
            expected_gate = is_export_eligible_extraction(session, expected_row, target_type="dft_results")
            assert expected_gate.eligible is True, expected_gate.reasons

        solvent_gate = is_export_eligible_extraction(session, dft_solvent, target_type="dft_results")
        assert solvent_gate.eligible is False
        assert "missing_material_identity" in solvent_gate.reasons

        # 9a. Target profile export excludes solvent and regression in JSON
        ml_dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile="dac_lis_ml")
        rec_ids = [r["record_id"] for r in ml_dataset["records"]]
        assert str(dft_dac.id) in rec_ids
        assert str(dft_solvent.id) not in rec_ids
        assert str(dft_regression.id) not in rec_ids

        meta = ml_dataset["metadata"]
        assert meta["dataset_profile"] == "dac_lis_ml"
        assert meta["base_eligible_count"] == 2
        assert meta["blocked_count"] == 1
        assert meta["profile_candidate_count"] == 2
        assert meta["profile_included_count"] == 1
        assert meta["profile_excluded_count"] == 1
        assert meta["profile_excluded_reasons"] == {
            "excluded_regression_or_statistical_parameter": 1,
        }

        # 9b. CSV export with profile also excludes them identically
        csv_text, csv_summary = build_dft_csv_rows(session, paper_id=main_paper.id, dataset_profile="dac_lis_ml")
        assert csv_summary["dataset_profile"] == "dac_lis_ml"
        assert csv_summary["base_eligible_count"] == 2
        assert csv_summary["blocked_count"] == 1
        assert csv_summary["profile_candidate_count"] == 2
        assert csv_summary["profile_included_count"] == 1
        assert csv_summary["profile_excluded_count"] == 1
        assert csv_summary["profile_excluded_reasons"] == {
            "excluded_regression_or_statistical_parameter": 1,
        }
        assert "FeCo-NC" in csv_text or "-1.75" in csv_text
        assert "linear_regression_slope" not in csv_text
        assert "DME+DOL" not in csv_text

        # 9c. Generic export still obeys the base safety gate.
        generic_dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile=None)
        generic_ids = [r["record_id"] for r in generic_dataset["records"]]
        assert str(dft_dac.id) in generic_ids
        assert str(dft_regression.id) in generic_ids
        assert str(dft_solvent.id) not in generic_ids
        assert generic_dataset["metadata"]["dataset_profile"] is None

        csv_gen_text, csv_gen_summary = build_dft_csv_rows(session, paper_id=main_paper.id, dataset_profile=None)
        assert csv_gen_summary["dataset_profile"] is None
        assert "DME+DOL" not in csv_gen_text
        assert "linear_regression_slope" in csv_gen_text
        assert "-1.75" in csv_gen_text


# 10. 未知 dataset_profile 抛出 ValueError
def test_unknown_dataset_profile_raises_error(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, _, _, _, _ = _seed_test_papers(session, tmp_path)
        with pytest.raises(ValueError, match="Unsupported dataset_profile"):
            build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile="invalid_profile_xyz")
        with pytest.raises(ValueError, match="Unsupported dataset_profile"):
            build_dft_csv_rows(session, paper_id=main_paper.id, dataset_profile="invalid_profile_xyz")


# 10b. Dataset Profile 严格范围规则测试（七大场景）
def test_dataset_profile_scoping_rules(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, _, _, _, seeded_dft = _seed_test_papers(session, tmp_path)
        session.delete(seeded_dft)
        session.flush()

        # 1. dual_atom
        cat_dual = CatalystSample(paper_id=main_paper.id, name="FeCo-NC", catalyst_type="dual_atom", metal_centers=["Fe", "Co"])
        # 2. bimetallic (alias for dual_atom)
        cat_bimetallic = CatalystSample(paper_id=main_paper.id, name="PtRu-C", catalyst_type="bimetallic", metal_centers=["Pt", "Ru"])
        # 3. single_atom
        cat_single = CatalystSample(paper_id=main_paper.id, name="Fe-NC", catalyst_type="single_atom", metal_centers=["Fe"])
        # 4. solvent
        cat_solvent = CatalystSample(paper_id=main_paper.id, name="DME+DOL", catalyst_type="solvent")
        # 5. catalyst_type 缺失
        cat_no_type = CatalystSample(paper_id=main_paper.id, name="Untyped-Catalyst", catalyst_type=None)
        session.add_all([cat_dual, cat_bimetallic, cat_single, cat_solvent, cat_no_type])
        session.flush()

        dft_dual = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_dual.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.75,
            unit="eV",
            evidence_text=(
                "Dual-atom DAC FeCo-NC catalyst contains Fe and Co.\n"
                "Calculated binding energy of Li2S4 on FeCo-NC: -1.75 eV."
            ),
        )
        dft_bimetallic = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_bimetallic.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.50,
            unit="eV",
            evidence_text=(
                "Bimetallic PtRu-C catalyst contains Pt and Ru.\n"
                "Calculated adsorption energy of Li2S4 on PtRu-C: -1.50 eV."
            ),
        )
        dft_single = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_single.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text=(
                "Single-atom SAC Fe-NC catalyst contains Fe.\n"
                "Calculated binding energy of Li2S4 on Fe-NC: -1.20 eV."
            ),
        )
        dft_solvent = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_solvent.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-0.40,
            unit="eV",
            evidence_text="Binding energy of Li2S4 in DME+DOL solvent is -0.40 eV.",
            evidence_payload={"material_identity": "DME+DOL"},
        )
        dft_no_type = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_no_type.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.10,
            unit="eV",
            evidence_text="Binding energy of Li2S4 on Untyped-Catalyst is -1.10 eV.",
        )
        # 6. 未绑定催化剂，但仅有 material_identity
        dft_unbound = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=None,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.60,
            unit="eV",
            evidence_text="Binding energy of Li2S4 on FeCo-NC material is -1.60 eV.",
            evidence_payload={"material_identity": "FeCo-NC"},
        )
        all_dfts = [dft_dual, dft_bimetallic, dft_single, dft_solvent, dft_no_type, dft_unbound]
        session.add_all(all_dfts)
        session.flush()

        scoping_pdf = tmp_path / "dataset_profile_scoping.pdf"
        _make_pdf(scoping_pdf, {
            page: row.evidence_text
            for page, row in enumerate(all_dfts, start=1)
        })
        main_paper.pdf_path = str(scoping_pdf)
        session.flush()
        for evidence_page, row in enumerate(all_dfts, start=1):
            _authorize_dft_result(session, row, page=evidence_page)

        for expected_row in (dft_dual, dft_bimetallic, dft_single):
            expected_gate = is_export_eligible_extraction(session, expected_row, target_type="dft_results")
            assert expected_gate.eligible is True, expected_gate.reasons

        for blocked_row in (dft_solvent, dft_no_type, dft_unbound):
            gate = is_export_eligible_extraction(session, blocked_row, target_type="dft_results")
            assert gate.eligible is False
        assert "missing_material_identity" in is_export_eligible_extraction(
            session, dft_solvent, target_type="dft_results"
        ).reasons
        assert "missing_material_identity" in is_export_eligible_extraction(
            session, dft_no_type, target_type="dft_results"
        ).reasons
        assert "non_authoritative_review:catalyst" in is_export_eligible_extraction(
            session, dft_unbound, target_type="dft_results"
        ).reasons

        # (a) dac_lis_ml: dual_atom 和 bimetallic 进入；single_atom, solvent, catalyst_type缺失, 未绑定 不进入
        dac_dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile="dac_lis_ml")
        dac_ids = {r["record_id"] for r in dac_dataset["records"]}
        assert str(dft_dual.id) in dac_ids
        assert str(dft_bimetallic.id) in dac_ids
        assert str(dft_single.id) not in dac_ids
        assert str(dft_solvent.id) not in dac_ids
        assert str(dft_no_type.id) not in dac_ids
        assert str(dft_unbound.id) not in dac_ids
        dac_meta = dac_dataset["metadata"]
        assert dac_meta["base_eligible_count"] == 3
        assert dac_meta["blocked_count"] == 3
        assert dac_meta["profile_candidate_count"] == 3
        assert dac_meta["profile_included_count_before_limit"] == 2
        assert dac_meta["profile_excluded_count"] == 1
        assert dac_meta["profile_excluded_reasons"] == {
            "excluded_non_target_catalyst_scope": 1,
        }

        # (b) bimetallic_lis_ml: 与 dac_lis_ml 规则一致
        bim_dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile="bimetallic_lis_ml")
        bim_ids = {r["record_id"] for r in bim_dataset["records"]}
        assert bim_ids == {str(dft_dual.id), str(dft_bimetallic.id)}

        # (c) sac_lis_ml: 只接收 single_atom 记录
        sac_dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile="sac_lis_ml")
        sac_ids = {r["record_id"] for r in sac_dataset["records"]}
        assert str(dft_single.id) in sac_ids
        assert str(dft_dual.id) not in sac_ids
        assert str(dft_bimetallic.id) not in sac_ids
        assert str(dft_solvent.id) not in sac_ids
        assert str(dft_no_type.id) not in sac_ids
        assert str(dft_unbound.id) not in sac_ids
        sac_meta = sac_dataset["metadata"]
        assert sac_meta["profile_included_count_before_limit"] == 1
        assert sac_meta["profile_excluded_count"] == 2
        assert sac_meta["profile_excluded_reasons"] == {
            "excluded_non_target_catalyst_scope": 2,
        }

        # (d) Generic profile=None: 保持全部通用导出
        gen_dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile=None)
        gen_ids = {r["record_id"] for r in gen_dataset["records"]}
        assert str(dft_dual.id) in gen_ids
        assert str(dft_bimetallic.id) in gen_ids
        assert str(dft_single.id) in gen_ids
        assert str(dft_solvent.id) not in gen_ids
        assert str(dft_no_type.id) not in gen_ids
        assert str(dft_unbound.id) not in gen_ids


# 10c. 修正 limit 与 Profile 过滤顺序测试（前N条排除后，limit=1仍能正确导出后续DAC）
def test_limit_applied_after_profile_filtering(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, _, _, _, seeded_dft = _seed_test_papers(session, tmp_path)
        session.delete(seeded_dft)
        session.flush()

        cat_solvent = CatalystSample(paper_id=main_paper.id, name="DME+DOL", catalyst_type="solvent")
        cat_dac = CatalystSample(paper_id=main_paper.id, name="FeCo-NC", catalyst_type="dual_atom", metal_centers=["Fe", "Co"])
        session.add_all([cat_solvent, cat_dac])
        session.flush()

        # 溶剂由基础门禁阻断；回归参数通过基础门禁后由 Profile 排除。
        dft_solvent = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_solvent.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-0.45,
            unit="eV",
            evidence_text="Binding energy of Li2S4 in DME+DOL solvent is -0.45 eV.",
        )
        dft_regression = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_dac.id,
            property_type="linear_regression_slope",
            adsorbate="Li2S4",
            value=0.92,
            unit="eV",
            evidence_text=(
                "Dual-atom DAC FeCo-NC catalyst contains Fe and Co.\n"
                "Linear regression slope for Li2S4 on FeCo-NC is 0.92 eV."
            ),
        )
        # 后2条：合规 DAC 记录
        dft_dac_1 = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_dac.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.75,
            unit="eV",
            evidence_text=(
                "Dual-atom DAC FeCo-NC catalyst contains Fe and Co.\n"
                "Calculated binding energy of Li2S4 on FeCo-NC: -1.75 eV."
            ),
        )
        dft_dac_2 = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=cat_dac.id,
            property_type="binding_energy",
            adsorbate="Li2S6",
            value=-2.10,
            unit="eV",
            evidence_text=(
                "Dual-atom DAC FeCo-NC catalyst contains Fe and Co.\n"
                "Calculated binding energy of Li2S6 on FeCo-NC: -2.10 eV."
            ),
        )
        rows = [dft_solvent, dft_regression, dft_dac_1, dft_dac_2]
        session.add_all(rows)
        session.flush()

        limit_pdf = tmp_path / "profile_limit.pdf"
        _make_pdf(limit_pdf, {
            page: row.evidence_text
            for page, row in enumerate(rows, start=1)
        })
        main_paper.pdf_path = str(limit_pdf)
        session.flush()
        for evidence_page, row in enumerate(rows, start=1):
            _authorize_dft_result(session, row, page=evidence_page)

        for expected_row in (dft_regression, dft_dac_1, dft_dac_2):
            expected_gate = is_export_eligible_extraction(session, expected_row, target_type="dft_results")
            assert expected_gate.eligible is True, expected_gate.reasons

        solvent_gate = is_export_eligible_extraction(session, dft_solvent, target_type="dft_results")
        assert solvent_gate.eligible is False
        assert "missing_material_identity" in solvent_gate.reasons

        # 验证 JSON: limit=1 跨过前2条排除项，正确导出第3条 DAC 记录
        dataset = build_dft_ml_dataset(session, paper_id=main_paper.id, dataset_profile="dac_lis_ml", limit=1)
        assert len(dataset["records"]) == 1
        assert dataset["records"][0]["record_id"] == str(dft_dac_1.id)
        assert dataset["records"][0]["target"]["value"] == -1.75

        meta = dataset["metadata"]
        assert meta["base_eligible_count"] == 3
        assert meta["blocked_count"] == 1
        assert meta["profile_candidate_count"] == 3
        assert meta["profile_included_count_before_limit"] == 2
        assert meta["profile_excluded_count"] == 1
        assert meta["profile_excluded_reasons"] == {
            "excluded_regression_or_statistical_parameter": 1,
        }
        assert meta["exported_count_after_limit"] == 1
        assert meta["limit"] == 1

        # 验证 CSV: limit=1 同样导出第3条 DAC 记录
        csv_text, csv_summary = build_dft_csv_rows(session, paper_id=main_paper.id, dataset_profile="dac_lis_ml", limit=1)
        assert csv_summary["exported_rows"] == 1
        assert csv_summary["exported_count_after_limit"] == 1
        assert csv_summary["profile_included_count_before_limit"] == 2
        assert csv_summary["limit"] == 1
        assert "-1.75" in csv_text
        assert "-2.10" not in csv_text


# 10d. MCP CSV 导出审计日志数量与字段修正测试
def test_mcp_csv_export_audit_log_counts(setup_test_db, tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "owner_export|Owner Export|litmcp_owner_export|read_papers,export_data",
    )
    get_settings.cache_clear()

    with Session(setup_test_db) as session:
        dac_main_paper, _, cat_dac, dft_dac, _ = _seed_dac_test_papers(session, tmp_path)
        main_paper_id = dac_main_paper.id

        # 审核通过 1 条 DAC 记录的所有必审字段
        _authorize_dft_result(session, dft_dac, evidence_paper_id=session.scalar(select(PaperRelationship.target_paper_id).where(PaperRelationship.source_paper_id == main_paper_id)), page=5)
        session.commit()

    with mcp_auth_context("litmcp_owner_export"):
        res = export_ml_dataset(
            paper_id=str(main_paper_id),
            dataset_profile="dac_lis_ml",
            format="csv",
            limit=10,
        )
        assert res is not None
        assert "dft_results" in res
        assert "csv" in res["dft_results"]

    with Session(setup_test_db) as session:
        audit = session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "export_ml_dataset")
            .order_by(AuditLog.id.desc())
        )
        assert audit is not None
        payload = audit.payload
        assert payload["format"] == "csv"
        assert payload["dataset_profile"] == "dac_lis_ml"
        assert payload["base_eligible_count"] >= 1
        assert payload["dft_record_count"] == 1
        assert payload["exported_count"] == 1


# 11. rejected、blocked、证据不完整和未决冲突数据仍不能进入ML出口
def test_rejected_blocked_incomplete_conflict_excluded_from_ml_export(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, dac_cat, dft_dac = _seed_test_papers(session, tmp_path)

        # 11a. Rejected review
        dft_rejected = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=dac_cat.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.11,
            unit="eV",
            evidence_text="evidence rejected",
        )
        session.add(dft_rejected)
        session.flush()
        rev_rej = ExtractionFieldReview(
            paper_id=main_paper.id,
            target_type="dft_results",
            target_id=str(dft_rejected.id),
            field_name="value",
            reviewer_status="rejected",
            review_payload={"rejected": True},
        )
        session.add(rev_rej)

        # 11b. Missing required evidence
        dft_incomplete = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=dac_cat.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.22,
            unit="eV",
            evidence_text="",
        )
        session.add(dft_incomplete)
        session.flush()

        # 11c. Pending audit conflict
        dft_conflict = DFTResult(
            paper_id=main_paper.id,
            catalyst_sample_id=dac_cat.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            value=-1.33,
            unit="eV",
            evidence_text="conflict evidence",
        )
        session.add(dft_conflict)
        session.flush()
        issue = DFTAuditIssue(
            paper_id=main_paper.id,
            result_id=dft_conflict.id,
            issue_type="value_conflict",
            status="open",
            fingerprint="fp-test-conflict",
            evidence_payload={"note": "Unsettled conflict"},
        )
        session.add(issue)
        session.flush()

        # Valid record passes the same complete authority contract as production.
        _authorize_dft_result(session, dft_dac, evidence_paper_id=si_paper.id, page=17)

        dataset = build_dft_ml_dataset(session, paper_id=main_paper.id)
        exported_ids = [r["record_id"] for r in dataset["records"]]
        assert str(dft_dac.id) in exported_ids
        assert str(dft_rejected.id) not in exported_ids
        assert str(dft_incomplete.id) not in exported_ids
        assert str(dft_conflict.id) not in exported_ids


# 12. 正规生命周期端到端隔离测试（10步完整闭环）
def test_full_formal_lifecycle_and_authoritative_reverification(setup_test_db, tmp_path):
    # 步骤 1 & 2: 在独立 schema 中准备数据与真实物理 PDF 文件
    with Session(setup_test_db) as session:
        main_paper, si_paper, unrelated_paper, catalyst, dft_result = _seed_test_papers(session, tmp_path)
        main_paper_id = main_paper.id
        si_paper_id = si_paper.id
        unrelated_paper_id = unrelated_paper.id
        dft_result_id = dft_result.id

        # 步骤 3: 调用 AIVerificationService.process_batch(dry_run=False) 正式写入所有必审字段
        service = AIVerificationService(session)
        req_fields = required_review_fields("dft_results", dft_result)
        submissions = [
            AIVerificationSubmission.model_validate({
                "target_type": "dft_results",
                "target_id": str(dft_result_id),
                "field_name": fn,
                "decision": "accept",
                "confidence": 0.95,
                "evidence_text": dft_result.evidence_text,
                "page": 17,
                "evidence_paper_id": str(si_paper_id),
                "reasoning_summary": f"Formally verified {fn} against SI Table S2 page 17.",
                "expected_target_fingerprint": ai_target_fingerprint("dft_results", dft_result),
            })
            for fn in req_fields
        ]
        batch_result = service.process_batch(
            paper_id=main_paper_id,
            submissions=submissions,
            identity=_identity(),
            dry_run=False,
        )
        assert batch_result["database_writes"] is True
        assert batch_result["auto_verified"] + batch_result["auto_repaired"] == len(submissions)

        # 步骤 4: 提交事务并关闭数据库 session
        session.commit()

    # 步骤 5: 开启全新的 session 从数据库重新读取数据并核对状态与结构
    with Session(setup_test_db) as new_session:
        refreshed_dft = new_session.get(DFTResult, dft_result_id)
        assert refreshed_dft is not None

        for fn in req_fields:
            review = new_session.scalar(
                select(ExtractionFieldReview).where(
                    ExtractionFieldReview.paper_id == main_paper_id,
                    ExtractionFieldReview.target_id == str(dft_result_id),
                    ExtractionFieldReview.field_name == fn,
                )
            )
            assert review is not None
            assert review.reviewer_status == "ai_verified"
            assert review.target_resolution_status == "active"
            assert review.review_payload is not None
            ai_data = review.review_payload["ai_verification"]
            assert ai_data["decision"] == "verified"
            assert ai_data["evidence_checks"]["evidence_paper_authorized"] is True

        review = new_session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == main_paper_id,
                ExtractionFieldReview.target_id == str(dft_result_id),
                ExtractionFieldReview.field_name == "value",
            )
        )

        locator = new_session.scalar(
            select(EvidenceLocator).where(
                EvidenceLocator.target_id == str(dft_result_id),
                EvidenceLocator.field_name == "value",
            )
        )
        assert locator is not None
        assert locator.paper_id == si_paper_id
        # 铁律：bbox 必须只为真实坐标或 None，严禁写入元数据字典
        assert locator.bbox is None
        assert locator.page == 17
        assert locator.locator_status == "exact_page"

        # 步骤 6: 调用 authoritative_ai_review_valid() 验证通过，并穿透到 SI 的物理 PDF 读取
        is_valid = authoritative_ai_review_valid(new_session, review, refreshed_dft)
        assert is_valid is True

        # 步骤 7: 调用 bulk_export_gate_results()，验证安全门全部通过且 locator 正常
        gates = bulk_export_gate_results(new_session, [refreshed_dft], target_type="dft_results")
        gate_res = gates[str(dft_result_id)]
        assert gate_res.eligible is True
        assert gate_res.locator_status == "exact_page"

        # 步骤 8: 验证 Profile 过滤契约
        # 8a. sac_lis_ml 纳入 B0102 (单原子)
        ml_sac = build_dft_ml_dataset(new_session, paper_id=main_paper_id, dataset_profile="sac_lis_ml")
        assert len(ml_sac["records"]) == 1
        rec = ml_sac["records"][0]
        assert rec["record_id"] == str(dft_result_id)
        assert rec["target"]["value"] == -1.75
        assert rec["target"]["reaction_step"] == "Li2S6 -> Li2S4"
        assert rec["target"]["configuration_index"] == 2

        csv_sac_text, csv_sac_summary = build_dft_csv_rows(new_session, paper_id=main_paper_id, dataset_profile="sac_lis_ml")
        assert csv_sac_summary["exported_rows"] == 1
        assert str(dft_result_id) in csv_sac_text
        assert str(main_paper_id) in csv_sac_text
        assert "-1.75" in csv_sac_text
        assert "Li2S6 -> Li2S4" in csv_sac_text
        assert ",2," in csv_sac_text

        # 8b. dac_lis_ml 严格排除 B0102 (单原子不得进入 DAC 数据集，计数严格为 0)
        ml_dac = build_dft_ml_dataset(new_session, paper_id=main_paper_id, dataset_profile="dac_lis_ml")
        assert len(ml_dac["records"]) == 0
        assert ml_dac["metadata"]["profile_excluded_count"] == 1
        assert ml_dac["metadata"]["profile_excluded_reasons"]["excluded_non_target_catalyst_scope"] == 1

        csv_dac_text, csv_dac_summary = build_dft_csv_rows(new_session, paper_id=main_paper_id, dataset_profile="dac_lis_ml")
        assert csv_dac_summary["exported_rows"] == 0

        # 8c. Generic 模式 (dataset_profile=None) 正常纳入
        ml_gen = build_dft_ml_dataset(new_session, paper_id=main_paper_id, dataset_profile=None)
        assert len(ml_gen["records"]) == 1
        csv_gen_text, csv_gen_summary = build_dft_csv_rows(new_session, paper_id=main_paper_id, dataset_profile=None)
        assert csv_gen_summary["exported_rows"] == 1

        # 步骤 9: 篡改测试
        # 9a: 篡改 review payload 中的 evidence_paper_id 为未关联论文 -> authoritative_ai_review_valid 判定无效
        tampered_review = copy.deepcopy(review)
        tampered_payload = copy.deepcopy(review.review_payload)
        tampered_payload["ai_verification"]["evidence_paper_id"] = str(unrelated_paper_id)
        tampered_payload["ai_verification"]["source_paper_id"] = str(unrelated_paper_id)
        tampered_review.review_payload = tampered_payload
        assert authoritative_ai_review_valid(new_session, tampered_review, refreshed_dft) is False

        # 9b: 篡改 locator page 为越界页码 -> 判定无效；恢复后恢复有效
        locator.page = 99
        new_session.flush()
        assert authoritative_ai_review_valid(new_session, review, refreshed_dft) is False
        locator.page = 17
        new_session.flush()
        assert authoritative_ai_review_valid(new_session, review, refreshed_dft) is True

        # 9c: 篡改物理 PDF 文件内容导致证据不匹配 -> 判定无效
        si_paper_obj = new_session.get(Paper, si_paper_id)
        _make_pdf(Path(si_paper_obj.pdf_path), {17: "Tampered content without matching evidence text."})
        assert authoritative_ai_review_valid(new_session, review, refreshed_dft) is False

        # 步骤 10: 断言全过程 session 处于 pytest_ 隔离 schema，未向生产 public schema 写入任何数据
        current_schema = new_session.execute(text("SELECT current_schema()")).scalar()
        assert current_schema is not None
        assert current_schema != "public"
        assert current_schema.startswith("pytest_")


# 13. 验证 REST 导出端点对非法 Profile 返回 422（非 500），且返回新增的 CSV 响应头
def test_rest_export_endpoints_profile_validation_and_headers(setup_test_db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db.session import get_db_session

    def override_get_db():
        with Session(setup_test_db) as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db
    try:
        client = TestClient(app)

        # 1. 非法 dataset_profile 绝不得产生 HTTP 500，必须返回 422
        res_csv_invalid = client.get("/api/papers/export/csv", params={"dataset_profile": "invalid_profile"})
        assert res_csv_invalid.status_code == 422

        res_json_invalid = client.get("/api/papers/export/dft-dataset", params={"dataset_profile": "invalid_profile"})
        assert res_json_invalid.status_code == 422

        # 2. 合法 Profile (dac_lis_ml) 正常返回 200，并携带补充的 CSV 响应头
        res_csv_valid = client.get("/api/papers/export/csv", params={"dataset_profile": "dac_lis_ml"})
        assert res_csv_valid.status_code == 200
        assert "X-D3-Base-Eligible-Count" in res_csv_valid.headers
        assert "X-D3-Profile-Included-Count" in res_csv_valid.headers
        assert "X-D3-Profile-Excluded-Count" in res_csv_valid.headers
        assert res_csv_valid.headers.get("X-D3-Dataset-Profile") == "dac_lis_ml"

        # 3. 合法 Profile (dac_lis_ml) JSON 导出正常返回 200，并包含完整元数据
        res_json_valid = client.get("/api/papers/export/dft-dataset", params={"dataset_profile": "dac_lis_ml"})
        assert res_json_valid.status_code == 200
        meta = res_json_valid.json().get("metadata", {})
        assert meta.get("dataset_profile") == "dac_lis_ml"
        assert "base_eligible_count" in meta
        assert "profile_included_count_before_limit" in meta
        assert "profile_excluded_count" in meta
        assert "exported_count_after_limit" in meta

        # 4. Generic 模式 (dataset_profile 为空) 正常返回 200
        res_csv_gen = client.get("/api/papers/export/csv")
        assert res_csv_gen.status_code == 200
        assert "X-D3-Base-Eligible-Count" in res_csv_gen.headers
        assert res_csv_gen.headers.get("X-D3-Dataset-Profile") == ""
    finally:
        app.dependency_overrides.pop(get_db_session, None)


# 14. 确认所有测试没有生产数据库写入
def test_zero_production_database_writes(setup_test_db):
    with Session(setup_test_db) as session:
        current_schema = session.execute(text("SELECT current_schema()")).scalar()
        assert current_schema is not None
        assert current_schema != "public"
        assert current_schema.startswith("pytest_")


# 15. 必审字段策略契约测试
def test_required_dft_review_fields_contract():
    # 基础必审字段：catalyst, energy_type, value
    base_dft = DFTResult(property_type="formation_energy", value=-0.5, unit="eV")
    assert required_dft_review_fields(base_dft) == ("catalyst", "energy_type", "value")

    # 吸附/结合能：自动包含 adsorbate
    ads_dft = DFTResult(property_type="adsorption_energy", adsorbate="Li2S4", value=-1.5, unit="eV")
    assert required_dft_review_fields(ads_dft) == ("catalyst", "energy_type", "value", "adsorbate")

    # 反应能垒/路径：自动包含 reaction_step
    rxn_dft = DFTResult(property_type="reaction_barrier", reaction_step="Li2S6 -> Li2S4", value=0.45, unit="eV")
    assert required_dft_review_fields(rxn_dft) == ("catalyst", "energy_type", "value", "reaction_step")

    # 两者兼具
    full_dft = DFTResult(
        property_type="binding_energy",
        adsorbate="Li2S4",
        reaction_step="Li2S6 -> Li2S4",
        value=-1.75,
        unit="eV",
        evidence_payload={"configuration_index": 2},
    )
    assert required_dft_review_fields(full_dft) == ("catalyst", "energy_type", "value", "adsorbate", "reaction_step")


# 16. 单一 value 审核不得放行整个 DFT 对象，必审字段全覆盖与一票否决测试
def test_single_value_review_does_not_authorize_whole_dft_result(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, cat, dft = _seed_test_papers(session, tmp_path)
        main_paper_id = main_paper.id
        dft_id = dft.id

        # 仅为 value 建立权威审核
        rev_val = ExtractionFieldReview(
            paper_id=main_paper_id,
            target_type="dft_results",
            target_id=str(dft_id),
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="active",
            reviewer="human_verifier",
            review_payload={
                "human_verification": {
                    "decision": "verified",
                    "writes_final_truth": True,
                    "verification_actor_type": "human",
                }
            },
        )
        loc = EvidenceLocator(
            paper_id=main_paper_id,
            target_type="dft_results",
            target_id=str(dft_id),
            field_name="value",
            evidence_text=dft.evidence_text,
            page=1,
            locator_status="exact_page",
        )
        session.add_all([rev_val, loc])
        session.flush()

        # 仅有 value 审核时，导出门禁判定不通过，且指出缺失的必审字段
        gate_partial = is_export_eligible_extraction(session, dft, target_type="dft_results")
        assert gate_partial.eligible is False
        assert any("missing_required_review:catalyst" in r for r in gate_partial.reasons)
        assert any("missing_required_review:energy_type" in r for r in gate_partial.reasons)

        # 补齐所有必审字段权威审核
        req_fields = required_review_fields("dft_results", dft)
        for fn in req_fields:
            if fn == "value":
                continue
            session.add(
                ExtractionFieldReview(
                    paper_id=main_paper_id,
                    target_type="dft_results",
                    target_id=str(dft_id),
                    field_name=fn,
                    reviewer_status="verified",
                    target_resolution_status="active",
                    reviewer="human_verifier",
                    review_payload={
                        "human_verification": {
                            "decision": "verified",
                            "writes_final_truth": True,
                            "verification_actor_type": "human",
                        }
                    },
                )
            )
        session.flush()

        gate_full = is_export_eligible_extraction(session, dft, target_type="dft_results")
        assert gate_full.eligible is False
        assert "unsafe_review" in gate_full.reasons

        # 一票否决：如果其中任何一个必审字段被拒绝 (例如 catalyst 被判定 rejected)，整条记录立即 blocked
        cat_rev = session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == main_paper_id,
                ExtractionFieldReview.target_id == str(dft_id),
                ExtractionFieldReview.field_name == "catalyst",
            )
        )
        cat_rev.reviewer_status = "rejected"
        session.flush()

        gate_rejected = is_export_eligible_extraction(session, dft, target_type="dft_results")
        assert gate_rejected.eligible is False
        assert "unsafe_review" in gate_rejected.reasons


# 17. list_tasks 复用权威判断：绝不跳过伪 verified 历史记录，全量吐出必审字段任务
def test_list_tasks_does_not_skip_pseudo_verified_reviews(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, cat, dft = _seed_test_papers(session, tmp_path)
        main_paper_id = main_paper.id
        dft_id = dft.id

        # 插入旧版未授权 AI 写入的 pseudo-verified 审核记录 (payload 包含 ai_verification 但无 human_verification)
        pseudo_rev = ExtractionFieldReview(
            paper_id=main_paper_id,
            target_type="dft_results",
            target_id=str(dft_id),
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="active",
            review_payload={"ai_verification": {"decision": "accept"}},
        )
        session.add(pseudo_rev)
        session.commit()

        # 调用 list_tasks
        service = AIVerificationService(session)
        task_res = service.list_tasks(
            paper_id=main_paper_id,
            target_type="dft_results",
            limit=50,
        )
        tasks = task_res["tasks"]

        # 伪 verified 记录的 value 字段必须重新出现在任务列表中，绝不能被跳过！
        task_field_names = [t["field_name"] for t in tasks if t["target_id"] == str(dft_id)]
        assert "value" in task_field_names
        # 且包含 catalyst, energy_type 等所有未完成权威审核的必审字段
        req_fields = set(required_review_fields("dft_results", dft))
        assert req_fields.issubset(set(task_field_names))

        # 验证只读无写
        assert task_res["database_writes"] is False


# 18. CSV 与 JSON 真实 record_id 一致性测试（覆盖 B0102 SAC 纳入与 DAC 排除，以及独立 DAC fixture 验证）
def test_csv_and_json_record_id_parity(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main_paper, si_paper, _, cat, dft1 = _seed_test_papers(session, tmp_path)
        main_paper_id = main_paper.id

        # 创建第2条同数值(-1.75)、同属性(binding_energy)、但不同 configuration_index (3) 的 B0102 SAC 记录
        dft2 = DFTResult(
            paper_id=main_paper_id,
            catalyst_sample_id=cat.id,
            property_type="binding_energy",
            adsorbate="Li2S4",
            reaction_step="Li2S6 -> Li2S4",
            value=-1.75,
            unit="eV",
            evidence_text=(
                "Table S2. Single-atom SAC Fe-N4-C catalyst: Fe metal in Fe-N4 "
                "coordination on C support.\n"
                "Calculated binding energy of Li2S4 on Fe-N4-C (config 3): -1.75 eV.\n"
                "Reaction step Li2S6 -> Li2S4."
            ),
            evidence_payload={"configuration_index": 3, "material_identity": "Fe-N4-C"},
        )
        session.add(dft2)
        session.flush()

        parity_si_pdf = tmp_path / "s0102_parity_si.pdf"
        _make_pdf(parity_si_pdf, {
            17: f"{dft1.evidence_text}\n{dft2.evidence_text}",
            18: "Table S2 end.",
        })
        si_paper.pdf_path = str(parity_si_pdf)
        session.flush()

        # 授权 B0102 的两行 SAC 记录
        _authorize_dft_result(session, dft1, evidence_paper_id=si_paper.id, page=17)
        _authorize_dft_result(session, dft2, evidence_paper_id=si_paper.id, page=17)

        # 独立创建非 B0102 的 DAC 催化剂与双构型记录
        dac_paper, dac_si, dac_cat, dac1, dac2 = _seed_dac_test_papers(session, tmp_path)
        _authorize_dft_result(session, dac1, evidence_paper_id=dac_si.id, page=5)
        _authorize_dft_result(session, dac2, evidence_paper_id=dac_si.id, page=6)
        session.commit()

        # 1. sac_lis_ml 模式：B0102 (2条单原子) 纳入，DAC (2条双原子) 严格排除
        json_sac = build_dft_ml_dataset(session, dataset_profile="sac_lis_ml")
        json_sac_ids = [r["record_id"] for r in json_sac["records"]]
        assert len(json_sac_ids) == 2
        assert str(dft1.id) in json_sac_ids
        assert str(dft2.id) in json_sac_ids
        assert str(dac1.id) not in json_sac_ids
        assert str(dac2.id) not in json_sac_ids

        csv_sac_text, csv_sac_summary = build_dft_csv_rows(session, dataset_profile="sac_lis_ml")
        assert csv_sac_summary["exported_rows"] == 2
        reader_sac = csv.reader(io.StringIO(csv_sac_text))
        header_sac = next(reader_sac)
        assert header_sac[0] == "record_id"
        csv_sac_ids = [row[0] for row in reader_sac]
        assert csv_sac_ids == json_sac_ids

        # 2. dac_lis_ml 模式：DAC (2条双原子) 纳入，B0102 (2条单原子) 严格排除 (0条 B0102)
        json_dac = build_dft_ml_dataset(session, dataset_profile="dac_lis_ml")
        json_dac_ids = [r["record_id"] for r in json_dac["records"]]
        assert len(json_dac_ids) == 2
        assert str(dac1.id) in json_dac_ids
        assert str(dac2.id) in json_dac_ids
        assert str(dft1.id) not in json_dac_ids
        assert str(dft2.id) not in json_dac_ids

        csv_dac_text, csv_dac_summary = build_dft_csv_rows(session, dataset_profile="dac_lis_ml")
        assert csv_dac_summary["exported_rows"] == 2
        reader_dac = csv.reader(io.StringIO(csv_dac_text))
        header_dac = next(reader_dac)
        assert header_dac[0] == "record_id"
        csv_dac_ids = [row[0] for row in reader_dac]
        assert csv_dac_ids == json_dac_ids

        # 3. Generic 模式：全量 4 条记录完全一致
        json_gen = build_dft_ml_dataset(session, dataset_profile=None)
        json_gen_ids = [r["record_id"] for r in json_gen["records"]]
        assert len(json_gen_ids) == 4

        csv_gen_text, csv_gen_summary = build_dft_csv_rows(session, dataset_profile=None)
        assert csv_gen_summary["exported_rows"] == 4
        reader_gen = csv.reader(io.StringIO(csv_gen_text))
        header_gen = next(reader_gen)
        assert header_gen[0] == "record_id"
        csv_gen_ids = [row[0] for row in reader_gen]
        assert csv_gen_ids == json_gen_ids


def test_human_authority_contract_requires_one_complete_review_locator_pair(setup_test_db, tmp_path):
    """A genuine associated-SI review passes; each required authority condition fails alone."""
    with Session(setup_test_db) as session:
        main, si, unrelated, _, row = _seed_test_papers(session, tmp_path)
        review = ExtractionFieldReview(
            paper_id=main.id,
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="active",
            reviewer="human_verifier",
            target_fingerprint=ai_target_fingerprint("dft_results", row),
            evidence_text=row.evidence_text,
            review_payload={"human_verification": {
                "verification_actor_type": "human", "identity_verified": True,
                "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier",
            }},
        )
        locator = EvidenceLocator(
            paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            evidence_text=row.evidence_text, page=17, locator_status="exact_page",
        )
        session.add_all([review, locator])
        session.flush()
        assert is_authoritative_verified_review(session, review, row) is True

        for key, invalid in (
            ("verification_actor_type", "ai"), ("identity_verified", False),
            ("writes_final_truth", False), ("decision", "blocked"), ("reviewer", ""),
        ):
            payload = copy.deepcopy(review.review_payload)
            payload["human_verification"][key] = invalid
            review.review_payload = payload
            assert is_authoritative_verified_review(session, review, row) is False
        review.review_payload = {"human_verification": {
            "verification_actor_type": "human", "identity_verified": True,
            "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier",
        }}
        review.target_fingerprint = "stale"
        assert is_authoritative_verified_review(session, review, row) is False
        review.target_fingerprint = ai_target_fingerprint("dft_results", row)
        locator.evidence_text = "different text"
        assert is_authoritative_verified_review(session, review, row) is False
        locator.evidence_text = row.evidence_text
        locator.field_name = "catalyst"
        assert is_authoritative_verified_review(session, review, row) is False
        locator.field_name = "value"
        locator.paper_id = unrelated.id
        assert is_authoritative_verified_review(session, review, row) is False


def test_exact_bbox_requires_non_degenerate_on_page_geometry(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, unrelated, _, row = _seed_test_papers(session, tmp_path)
        review = ExtractionFieldReview(
            paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
            target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=row.evidence_text,
            review_payload={"human_verification": {"verification_actor_type": "human", "identity_verified": True,
                "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}},
        )
        locator = EvidenceLocator(paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            evidence_text=row.evidence_text, page=17, locator_status="exact_bbox", bbox={"x0": 1, "y0": 1, "x1": 100, "y1": 100})
        session.add_all([review, locator])
        session.flush()
        assert is_authoritative_verified_review(session, review, row) is True
        for bbox in (None, {"x0": 1, "y0": 1, "x1": 1, "y1": 100}, {"x0": -1, "y0": 1, "x1": 100, "y1": 100}):
            locator.bbox = bbox
            assert is_authoritative_verified_review(session, review, row) is False


def test_complete_human_field_reviews_open_single_and_bulk_dft_gates(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, unrelated, _, row = _seed_test_papers(session, tmp_path)
        for field_name in required_review_fields("dft_results", row):
            session.add(ExtractionFieldReview(
                paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name=field_name,
                reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
                target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=row.evidence_text,
                review_payload={"human_verification": {"verification_actor_type": "human", "identity_verified": True,
                    "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}},
            ))
            session.add(EvidenceLocator(paper_id=si.id, target_type="dft_results", target_id=str(row.id),
                field_name=field_name, evidence_text=row.evidence_text, page=17, locator_status="exact_page"))
        session.flush()
        catalyst_review = next(review for review in session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.target_id == str(row.id),
                ExtractionFieldReview.field_name == "catalyst",
            )
        ) if review.field_name == "catalyst")
        catalyst_locator = next(locator for locator in session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.target_id == str(row.id),
                EvidenceLocator.field_name == "catalyst",
            )
        ) if locator.field_name == "catalyst")
        # Keep this diagnostic contract beside the positive fixture.  It makes
        # a failure identify its first authority subcondition, rather than
        # hiding it behind the aggregate export-gate reason.
        assert catalyst_review.target_fingerprint == ai_target_fingerprint("dft_results", row)
        assert catalyst_review.reviewer_status == "verified"
        assert catalyst_review.target_resolution_status == "active"
        assert catalyst_review.reviewer == "human_verifier"
        human_payload = catalyst_review.review_payload["human_verification"]
        assert human_payload["reviewer"] == catalyst_review.reviewer == "human_verifier"
        assert human_payload["verification_actor_type"] == "human"
        assert human_payload["identity_verified"] is True
        assert human_payload["writes_final_truth"] is True
        assert human_payload["decision"] == "verified"
        assert catalyst_locator.target_type == catalyst_review.target_type == "dft_results"
        assert catalyst_locator.target_id == catalyst_review.target_id == str(row.id)
        assert catalyst_locator.field_name == catalyst_review.field_name == "catalyst"
        assert catalyst_locator.paper_id in {main.id, si.id}
        assert catalyst_locator.locator_status == "exact_page"
        assert catalyst_locator.page == 17
        assert normalize_evidence_text(catalyst_locator.evidence_text) == normalize_evidence_text(catalyst_review.evidence_text)
        page_text, page_error, _pdf_path = cached_read_pdf_page_text(session, si, 17)
        assert page_error is None
        assert normalize_evidence_text(catalyst_review.evidence_text) in normalize_evidence_text(page_text), page_text
        catalyst_snapshot = ai_verification_utils.ai_field_snapshot("dft_results", row, "catalyst")
        catalyst_checks = AIVerificationService(session)._content_checks(
            "dft_results", row, "catalyst", catalyst_snapshot["value"], catalyst_snapshot["unit"], catalyst_review.evidence_text,
        )
        for check_name, passed in catalyst_checks.items():
            assert passed, {"failed_check": check_name, "checks": catalyst_checks, "page_text": page_text}
        assert authoritative_human_review_locator_pair_valid(session, catalyst_review, row, catalyst_locator) is True
        single = is_export_eligible_extraction(session, row, target_type="dft_results")
        bulk = bulk_export_gate_results(session, [row], target_type="dft_results")[str(row.id)]
        assert single.eligible is True
        assert bulk.eligible is True
        assert single.reasons == bulk.reasons == ()
        assert single.provenance_level == bulk.provenance_level == "exact_pdf_page"

        # An unrelated paper may not satisfy either the single-row evidence
        # reference or the batch preload, even if target/field ids coincide.
        for locator in session.scalars(select(EvidenceLocator).where(EvidenceLocator.target_id == str(row.id))).all():
            locator.paper_id = unrelated.id
        session.flush()
        single = is_export_eligible_extraction(session, row, target_type="dft_results")
        bulk = bulk_export_gate_results(session, [row], target_type="dft_results")[str(row.id)]
        assert single.eligible is False
        assert bulk.eligible is False
        assert single.reasons == bulk.reasons
        assert "missing_evidence" in single.reasons
        assert not (single.provenance_level == "exact_pdf_page" and "missing_evidence" in single.reasons)


def test_value_and_unit_must_share_one_evidence_item(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        _, _, _, _, row = _seed_test_papers(session, tmp_path)
        service = AIVerificationService(session)
        split_item = "Table row 1: -1.75\nTable row 2: eV"
        checks = service._content_checks("dft_results", row, "value", row.value, row.unit, split_item)
        assert checks["numeric_value_matches"] is True
        assert checks["unit_matches"] is True
        assert checks["value_unit_same_evidence_item"] is False
        bound_item = "Table row 1: -1.75 eV"
        assert service._content_checks("dft_results", row, "value", row.value, row.unit, bound_item)["value_unit_same_evidence_item"] is True


def test_support_c_does_not_match_letters_inside_ordinary_words(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, _, _, _, _ = _seed_test_papers(session, tmp_path)
        catalyst = CatalystSample(
            paper_id=main.id, name="Fe-N4", catalyst_type="single_atom",
            metal_centers=["Fe"], coordination="Fe-N4", support="C",
        )
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=main.id, catalyst_sample_id=catalyst.id, property_type="binding_energy",
            value=-1.0, unit="eV", evidence_text="single atom Fe-N4 calculated configuration",
        )
        session.add(row)
        session.flush()
        checks = AIVerificationService(session)._content_checks(
            "dft_results", row, "catalyst", catalyst.name, row.unit, row.evidence_text,
        )
        assert checks["catalyst_consistent"] is False


def test_b0102_explicit_fen4_ps_dg_family_evidence_supports_only_its_sac_isomers(setup_test_db, tmp_path):
    """B0102 main-text SAC-family evidence may support a named P/S isomer, not other materials."""
    with Session(setup_test_db) as session:
        main, _, _, _, _ = _seed_test_papers(session, tmp_path)
        family_evidence = (
            "Initially, an FeN4-G SAC structure was constructed on a graphene monolayer. "
            "Subsequently, defects were introduced, and heteroatoms (P or S) were doped, "
            "resulting in the formation of eight edge-type FeN4P2/S2-DG structures. "
            "The Fe transition metal atom is connected to four N atoms."
        )
        valid_catalyst = CatalystSample(
            paper_id=main.id, name="FeN4P1,2-DG", catalyst_type="single_atom",
            metal_centers=["Fe"], coordination="FeN4", support="graphene",
        )
        invalid_catalyst = CatalystSample(
            paper_id=main.id, name="FeN3P1,2-DG", catalyst_type="single_atom",
            metal_centers=["Fe"], coordination="FeN4", support="graphene",
        )
        solvent = CatalystSample(
            paper_id=main.id, name="DME+DOL", catalyst_type="unknown",
            metal_centers=[], coordination=None, support="UNKNOWN",
        )
        graphene_control = CatalystSample(
            # Deliberately mislabelled to prove name evidence alone can never
            # convert the paper's graphene baseline into a SAC record.
            paper_id=main.id, name="Graphene", catalyst_type="single_atom",
            metal_centers=["Fe"], coordination="FeN4", support="graphene",
        )
        session.add_all([valid_catalyst, invalid_catalyst, solvent, graphene_control])
        session.flush()
        rows = [
            DFTResult(paper_id=main.id, catalyst_sample_id=item.id, property_type="adsorption_energy", value=-1.0, unit="eV")
            for item in (valid_catalyst, invalid_catalyst, solvent, graphene_control)
        ]
        session.add_all(rows)
        session.flush()
        service = AIVerificationService(session)
        assert service._content_checks("dft_results", rows[0], "catalyst", valid_catalyst.name, "eV", family_evidence)["catalyst_consistent"] is True
        assert service._content_checks("dft_results", rows[1], "catalyst", invalid_catalyst.name, "eV", family_evidence)["catalyst_consistent"] is False
        assert service._content_checks("dft_results", rows[2], "catalyst", solvent.name, "eV", family_evidence)["catalyst_consistent"] is False
        assert service._content_checks("dft_results", rows[3], "catalyst", graphene_control.name, "eV", family_evidence)["catalyst_consistent"] is False


def test_bulk_authority_reuses_one_pdf_page_read_per_paper_page(setup_test_db, tmp_path, monkeypatch):
    with Session(setup_test_db) as session:
        main, si, _, catalyst, first = _seed_test_papers(session, tmp_path)
        second = DFTResult(
            paper_id=main.id, catalyst_sample_id=catalyst.id, property_type="binding_energy",
            adsorbate="Li2S4", reaction_step="Li2S6 -> Li2S4", value=-1.75, unit="eV",
            evidence_text=first.evidence_text, evidence_payload={"configuration_index": 2},
        )
        session.add(second)
        session.flush()
        for row in (first, second):
            for field_name in required_review_fields("dft_results", row):
                session.add(ExtractionFieldReview(
                    paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name=field_name,
                    reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
                    target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=row.evidence_text,
                    review_payload={"human_verification": {"verification_actor_type": "human", "identity_verified": True,
                        "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}},
                ))
                session.add(EvidenceLocator(
                    paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name=field_name,
                    evidence_text=row.evidence_text, page=17, locator_status="exact_page",
                ))
        session.flush()
        original_read = ai_verification_utils.read_pdf_page_text
        calls = 0
        def counted_read(paper, page):
            nonlocal calls
            calls += 1
            return original_read(paper, page)
        monkeypatch.setattr(ai_verification_utils, "read_pdf_page_text", counted_read)
        bulk_export_gate_results(session, [first, second], target_type="dft_results")
        assert calls == 1


@pytest.mark.parametrize("field,value", [
    ("reviewer_status", "pending"), ("target_resolution_status", "stale"),
    ("verification_actor_type", "ai"), ("identity_verified", False),
    ("writes_final_truth", False), ("decision", "blocked"),
    ("payload_reviewer", ""), ("database_reviewer", ""),
    ("reviewer_mismatch", "another_human"), ("machine_identity", "ai"),
])
def test_human_authority_identity_baseline_rejects_one_broken_condition(setup_test_db, tmp_path, field, value):
    with Session(setup_test_db) as session:
        main, si, _, _, row = _seed_test_papers(session, tmp_path)
        payload = {"verification_actor_type": "human", "identity_verified": True,
            "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}
        review = ExtractionFieldReview(
            paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
            target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=row.evidence_text,
            review_payload={"human_verification": payload},
        )
        locator = EvidenceLocator(paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            evidence_text=row.evidence_text, page=17, locator_status="exact_page")
        if field == "reviewer_status":
            review.reviewer_status = value
        elif field == "target_resolution_status":
            review.target_resolution_status = value
        elif field in payload:
            payload[field] = value
        elif field == "payload_reviewer":
            payload["reviewer"] = value
        elif field == "database_reviewer":
            review.reviewer = value
        elif field == "reviewer_mismatch":
            payload["reviewer"] = value
        else:
            payload["reviewer"] = value
            review.reviewer = value
        session.add_all([review, locator])
        session.flush()
        assert is_authoritative_verified_review(session, review, row) is False


def test_catalyst_and_active_site_changes_revoke_catalyst_review(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, _, catalyst, row = _seed_test_papers(session, tmp_path)
        site = ActiveSiteMetal(paper_id=main.id, catalyst_sample_id=catalyst.id, active_site_key="Fe1",
            site_type="single", site_role="primary", element_symbol="Fe", element_order=1, order_source="test")
        session.add(site)
        session.flush()
        row.evidence_text = f"{row.evidence_text}\nActive site Fe1."
        active_site_si_pdf = tmp_path / "s0102_active_site_si.pdf"
        _make_pdf(active_site_si_pdf, {17: row.evidence_text})
        si.pdf_path = str(active_site_si_pdf)
        session.flush()
        review = ExtractionFieldReview(paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name="catalyst",
            reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
            target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=row.evidence_text,
            review_payload={"human_verification": {"verification_actor_type": "human", "identity_verified": True,
                "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}})
        locator = EvidenceLocator(paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name="catalyst",
            evidence_text=row.evidence_text, page=17, locator_status="exact_page")
        session.add_all([review, locator]); session.flush()
        assert is_authoritative_verified_review(session, review, row) is True
        catalyst.support = "N-doped carbon"
        session.flush()
        assert is_authoritative_verified_review(session, review, row) is False
        catalyst.support = "C"
        review.target_fingerprint = ai_target_fingerprint("dft_results", row)
        session.flush()
        assert is_authoritative_verified_review(session, review, row) is True
        site.active_site_key = "Fe2"
        session.flush()
        assert is_authoritative_verified_review(session, review, row) is False


def test_configuration_requires_the_authoritative_value_pair_itself(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, _, _, row = _seed_test_papers(session, tmp_path)
        for field_name in required_review_fields("dft_results", row):
            text = "-1.75 eV" if field_name == "value" else row.evidence_text
            session.add(ExtractionFieldReview(paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name=field_name,
                reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
                target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=text,
                review_payload={"human_verification": {"verification_actor_type": "human", "identity_verified": True,
                    "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}}))
            session.add(EvidenceLocator(paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name=field_name,
                evidence_text=text, page=17, locator_status="exact_page"))
        session.add(ExtractionFieldReview(paper_id=main.id, target_type="dft_result", target_id=str(row.id), field_name="value",
            reviewer_status="rejected", target_resolution_status="active", reviewer="human_verifier",
            evidence_text="config 2: -1.75 eV", review_payload={"human_verification": {"reviewer": "human_verifier"}}))
        session.flush()
        gate = is_export_eligible_extraction(session, row, target_type="dft_results")
        assert "configuration_index_not_supported_by_evidence" in gate.reasons


def test_authority_cache_does_not_survive_a_second_gate_call(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, _, _, row = _seed_test_papers(session, tmp_path)
        review = ExtractionFieldReview(paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
            target_fingerprint=ai_target_fingerprint("dft_results", row), evidence_text=row.evidence_text,
            review_payload={"human_verification": {"verification_actor_type": "human", "identity_verified": True,
                "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier"}})
        session.add(review); session.flush()
        assert is_authoritative_verified_review(session, review, row) is False
        locator = EvidenceLocator(paper_id=si.id, target_type="dft_results", target_id=str(row.id), field_name="value",
            evidence_text=row.evidence_text, page=17, locator_status="exact_page")
        session.add(locator); session.flush()
        assert is_authoritative_verified_review(session, review, row) is True
        locator.locator_status = "approximate"; session.flush()
        assert is_authoritative_verified_review(session, review, row) is False


def test_list_tasks_recovery_returns_only_explicit_si_source_ids(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, unrelated, _, _ = _seed_test_papers(session, tmp_path)
        payload = AIVerificationService(session).list_tasks(
            paper_id=main.id, target_type="dft_results", limit=50,
        )
        candidates = [candidate for task in payload["tasks"] for candidate in task["evidence_candidates"]]
        assert payload["database_writes"] is False
        assert any(candidate["evidence_paper_id"] == str(si.id) and candidate["source_paper_id"] == str(si.id) for candidate in candidates)
        assert all(candidate["evidence_paper_id"] != str(unrelated.id) for candidate in candidates)


def test_bulk_gate_preloads_relationship_reviews_locators_and_active_sites(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        main, si, _, catalyst, first = _seed_test_papers(session, tmp_path)
        rows = [first]
        for _ in range(2):
            row = DFTResult(paper_id=main.id, catalyst_sample_id=catalyst.id, property_type="binding_energy",
                adsorbate="Li2S4", reaction_step="Li2S6 -> Li2S4", value=-1.75, unit="eV",
                evidence_text=first.evidence_text, evidence_payload={"configuration_index": 2})
            session.add(row); rows.append(row)
        session.flush()
        for row in rows:
            _authorize_dft_result(session, row, evidence_paper_id=si.id, page=17)
        counts = {"paper_relationships": 0, "extraction_field_reviews": 0, "evidence_locators": 0, "active_site_metals": 0}
        def count_queries(_conn, _cursor, statement, *_args):
            text_lower = statement.casefold()
            for table in counts:
                if table in text_lower:
                    counts[table] += 1
        event.listen(session.bind, "before_cursor_execute", count_queries)
        try:
            bulk_export_gate_results(session, rows, target_type="dft_results")
        finally:
            event.remove(session.bind, "before_cursor_execute", count_queries)
        assert counts["paper_relationships"] <= 2
        assert counts["extraction_field_reviews"] <= 2
        assert counts["evidence_locators"] <= 2
        assert counts["active_site_metals"] <= 1


def test_dft_record_bundles_use_keyset_cursor_and_preserve_field_contract(setup_test_db, tmp_path):
    """A completed/deferred first record must never move a later UUID out of a cursor sweep."""
    pdf_path = tmp_path / "record-bundles.pdf"
    evidence = "Fe-N4 binds Li2S4 with adsorption energy -1.20 eV in configuration 2."
    _make_pdf(pdf_path, {1: evidence})
    with Session(setup_test_db) as session:
        paper = Paper(title="B0102 record bundles", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper); session.flush()
        catalyst = CatalystSample(
            paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom",
            metal_centers=["Fe"], coordination="FeN4", support="graphene",
        )
        session.add(catalyst); session.flush()
        rows = [
            DFTResult(
                paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy",
                adsorbate="Li2S4", value=-1.2, unit="eV", evidence_text=evidence,
                evidence_payload={"configuration_index": 2},
            )
            for _ in range(3)
        ]
        session.add_all(rows); session.commit()

        service = AIVerificationService(session)
        first_page = service.list_dft_record_tasks(paper_id=paper.id, limit=1)
        assert first_page["total_pending_records"] is None
        assert first_page["total_pending_fields"] is None
        assert first_page["total_records"] == 3
        assert first_page["page_pending_records"] == 1
        assert first_page["page_pending_fields"] == 4
        assert first_page["returned_records"] == 1
        assert first_page["has_more"] is True and first_page["next_cursor"]
        first_bundle = first_page["records"][0]
        required = {item["field_name"] for item in first_bundle["fields"] if item["required"]}
        assert required == {"catalyst", "energy_type", "value", "adsorbate"}
        configuration = next(item for item in first_bundle["fields"] if item["field_name"] == "configuration_index")
        assert configuration["verification_field_name"] == "value"
        assert configuration["writable"] is False
        assert all(item["target_snapshot_fingerprint"] == first_bundle["fields"][0]["target_snapshot_fingerprint"] for item in first_bundle["fields"])
        assert all(item["expected_write_version"] is None for item in first_bundle["fields"] if item["required"])

        submissions = [
            AIVerificationSubmission(
                target_type="dft_results",
                target_id=first_bundle["record_id"],
                field_name=item["field_name"],
                decision="defer",
                confidence=0.0,
                expected_target_fingerprint=item["target_snapshot_fingerprint"],
                expected_write_version=item["expected_write_version"],
                blocked_reasons=["no_supporting_evidence"],
            )
            for item in first_bundle["fields"] if item["required"]
        ]
        deferred = service.process_batch(
            paper_id=paper.id, submissions=submissions, identity=_identity(), dry_run=False,
        )
        assert deferred["auto_deferred"] == 4
        assert deferred["auto_rejected"] == 0

        after_cursor = service.list_dft_record_tasks(
            paper_id=paper.id, limit=10, cursor=first_page["next_cursor"],
        )
        assert {bundle["record_id"] for bundle in after_cursor["records"]} == {
            str(row.id) for row in rows if str(row.id) > first_bundle["record_id"]
        }
        default_sweep = service.list_dft_record_tasks(paper_id=paper.id, limit=10)
        assert first_bundle["record_id"] not in {bundle["record_id"] for bundle in default_sweep["records"]}
        blocked_sweep = service.list_dft_record_tasks(paper_id=paper.id, limit=10, include_blocked=True)
        blocked_bundle = next(bundle for bundle in blocked_sweep["records"] if bundle["record_id"] == first_bundle["record_id"])
        assert all(item["current_status"] == "ai_blocked" for item in blocked_bundle["fields"] if item["required"])
        assert all(item["blocked_reasons"] == ["no_supporting_evidence"] for item in blocked_bundle["fields"] if item["required"])

        first_row = session.get(DFTResult, UUID(first_bundle["record_id"]))
        assert first_row is not None
        blocked_review = session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.target_id == first_bundle["record_id"],
            ExtractionFieldReview.field_name == "value",
        ))
        assert blocked_review is not None
        assert is_authoritative_verified_review(session, blocked_review, first_row) is False
        assert "unsafe_review" in is_export_eligible_extraction(
            session, first_row, target_type="dft_results",
        ).reasons

        # Catalyst identity is part of the target fingerprint, so a material
        # correction invalidates every old blocked decision and makes it pending.
        catalyst.support = "defect_graphene"
        session.commit()
        invalidated = service.list_dft_record_tasks(paper_id=paper.id, limit=10)
        invalidated_bundle = next(bundle for bundle in invalidated["records"] if bundle["record_id"] == first_bundle["record_id"])
        assert all(item["current_status"] == "pending" for item in invalidated_bundle["fields"] if item["required"])


def test_dft_record_bundle_preloads_locators_and_reads_shared_pdf_page_once(setup_test_db, tmp_path, monkeypatch):
    pdf_path = tmp_path / "bundle-shared-page.pdf"
    evidence = "Fe-N4 adsorption energy for Li2S4 is -1.20 eV."
    _make_pdf(pdf_path, {1: evidence})
    with Session(setup_test_db) as session:
        paper = Paper(title="shared bundle evidence", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper); session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        session.add(catalyst); session.flush()
        rows = [
            DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2, unit="eV", evidence_text=evidence)
            for _ in range(3)
        ]
        session.add_all(rows); session.flush()
        for row in rows:
            for field_name in required_dft_review_fields(row):
                session.add(EvidenceLocator(
                    paper_id=paper.id, target_type="dft_results", target_id=str(row.id), field_name=field_name,
                    evidence_text=evidence, page=1, locator_status="exact_page", parser_source="pytest",
                ))
        session.commit()

        from app.services import ai_verification_service as service_module
        original_reader = service_module.cached_read_pdf_page_text
        calls = 0
        def count_reads(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_reader(*args, **kwargs)
        monkeypatch.setattr(service_module, "cached_read_pdf_page_text", count_reads)
        counts = {"dft_results": 0, "extraction_field_reviews": 0, "evidence_locators": 0, "active_site_metals": 0}
        def count_queries(_conn, _cursor, statement, *_args):
            lowered = statement.casefold()
            for table in counts:
                if table in lowered:
                    counts[table] += 1
        event.listen(session.bind, "before_cursor_execute", count_queries)
        try:
            result = AIVerificationService(session).list_dft_record_tasks(paper_id=paper.id, limit=10)
        finally:
            event.remove(session.bind, "before_cursor_execute", count_queries)
        assert result["returned_records"] == 3
        assert calls == 1
        # One keyset read plus one explicit page-total count is constant; the
        # assertion is deliberately not an accidental N+1 exact-count proxy.
        assert counts["dft_results"] <= 2
        assert counts["extraction_field_reviews"] <= 1
        assert counts["evidence_locators"] <= 1
        assert counts["active_site_metals"] <= 1


def test_record_bundle_returns_exact_table_triad_reusable_by_dry_run_and_formal_submit(setup_test_db, tmp_path):
    pdf_path = tmp_path / "record-table.pdf"
    pdf_text = "Table S1. Adsorption energy. Fe-N4 Li2S4 adsorption energy (eV) -1.20"
    _make_pdf(pdf_path, {1: pdf_text})
    with Session(setup_test_db) as session:
        paper = Paper(title="table bundle", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper); session.flush()
        table = PaperTable(
            paper_id=paper.id, page=1, caption="Table S1. Adsorption energy.",
            markdown_content="| Catalyst | Li2S4 adsorption energy (eV) |\n| --- | --- |\n| Fe-N4 | -1.20 |",
        )
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        session.add_all([table, catalyst]); session.flush()
        row = DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2, unit="eV")
        session.add(row); session.flush()
        built = build_structured_table_cell_evidence(session, paper_id=paper.id, table_id=table.id, page=1, source_row_index=0, source_column_index=1)
        assert built is not None
        session.add(EvidenceLocator(
            paper_id=paper.id, source_type="table", target_type="dft_result", target_id=str(row.id), field_name="value",
            table_id=table.id, page=1, evidence_text=built["canonical_evidence_text"], locator_status="exact_page",
        ))
        session.commit()

        bundle = AIVerificationService(session).list_dft_record_tasks(paper_id=paper.id, limit=1)["records"][0]
        value = next(item for item in bundle["fields"] if item["field_name"] == "value")
        candidate = value["evidence_candidates"][0]
        assert candidate["table_reference_status"] == "resolved"
        assert candidate["table_reference"] == {"table_id": str(table.id), "source_row_index": 0, "source_column_index": 1}
        assert candidate["table_cell"] == {
            "cell_value": "-1.20", "column_header": "Li2S4 adsorption energy (eV)",
            "row_label": "Fe-N4", "canonical_evidence_text": built["canonical_evidence_text"],
        }
        submission = AIVerificationSubmission(
            target_type="dft_results", target_id=str(row.id), field_name="value", decision="accept", confidence=0.99,
            page=candidate["page"], evidence_paper_id=candidate["evidence_paper_id"], evidence_text="",
            table_id=candidate["table_reference"]["table_id"], source_row_index=candidate["source_row_index"],
            source_column_index=candidate["source_column_index"], expected_target_fingerprint=value["target_snapshot_fingerprint"],
        )
        service = AIVerificationService(session)
        dry = service.process_batch(paper_id=paper.id, submissions=[submission], identity=_identity(), dry_run=True)
        formal = service.process_batch(paper_id=paper.id, submissions=[submission], identity=_identity(), dry_run=False)
        assert dry["auto_repaired"] + dry["auto_verified"] == 1, dry["items"]
        assert formal["auto_repaired"] + formal["auto_verified"] == 1, formal["items"]


def test_defer_rejects_unsafe_source_fields_and_is_idempotent_with_table_revision_invalidation(setup_test_db, tmp_path):
    pdf_path = tmp_path / "defer-contract.pdf"
    _make_pdf(pdf_path, {1: "Table S2. Fe-N4 Li2S4 adsorption energy (eV) -1.20"})
    with Session(setup_test_db) as session:
        paper = Paper(title="defer contract", pdf_path=str(pdf_path), authors=["Tester"])
        unrelated = Paper(title="unrelated", pdf_path=str(pdf_path), authors=["Other"])
        session.add_all([paper, unrelated]); session.flush()
        table = PaperTable(
            paper_id=paper.id, page=1, caption="Table S2.",
            markdown_content="| Catalyst | Li2S4 adsorption energy (eV) |\n| --- | --- |\n| Fe-N4 | -1.20 |",
        )
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        session.add_all([table, catalyst]); session.flush()
        row = DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2, unit="eV")
        session.add(row); session.commit()
        fingerprint = ai_target_fingerprint("dft_results", row)
        base = dict(target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
                    expected_target_fingerprint=fingerprint, blocked_reasons=["no_supporting_evidence"])
        service = AIVerificationService(session)
        for invalid in (
            {**base, "evidence_paper_id": str(unrelated.id)},
            {**base, "source_paper_id": str(paper.id), "evidence_paper_id": str(unrelated.id)},
            {**base, "page": 99},
            {**base, "field_name": "configuration_index"},
            {**base, "field_name": "not_a_dft_field"},
        ):
            result = service.process_batch(paper_id=paper.id, submissions=[AIVerificationSubmission.model_validate(invalid)], identity=_identity(), dry_run=False)
            assert result["items"][0]["outcome"] == "exception"
        assert session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.target_id == str(row.id))).all() == []

        valid = AIVerificationSubmission.model_validate({
            **base, "page": 1, "table_id": str(table.id), "source_row_index": 0, "source_column_index": 1,
        })
        first = service.process_batch(paper_id=paper.id, submissions=[valid], identity=_identity(), dry_run=False)
        review = session.scalar(select(ExtractionFieldReview).where(ExtractionFieldReview.target_id == str(row.id), ExtractionFieldReview.field_name == "value"))
        audits_before = len(session.scalars(select(AuditLog).where(AuditLog.target_id == str(row.id))).all())
        version_before = review.write_version
        second = service.process_batch(paper_id=paper.id, submissions=[valid], identity=_identity(), dry_run=False)
        session.refresh(review)
        assert first["auto_deferred"] == 1 and second["items"][0]["idempotent"] is True
        assert review.write_version == version_before
        assert len(session.scalars(select(AuditLog).where(AuditLog.target_id == str(row.id))).all()) == audits_before
        table.markdown_content = "| Catalyst | Li2S4 adsorption energy (eV) |\n| --- | --- |\n| Fe-N4 | -1.21 |"
        session.commit()
        refreshed = service.list_dft_record_tasks(paper_id=paper.id, limit=1)
        refreshed_value = next(item for item in refreshed["records"][0]["fields"] if item["field_name"] == "value")
        assert refreshed_value["current_status"] == "pending"


def test_record_keyset_aliases_and_evidence_changes_do_not_skip_or_hide_stale_blocks(setup_test_db, tmp_path):
    pdf_path = tmp_path / "cursor-alias.pdf"
    evidence = "Fe-N4 binds Li2S4 with adsorption energy -1.20 eV."
    _make_pdf(pdf_path, {1: evidence})
    with Session(setup_test_db) as session:
        main = Paper(title="main", pdf_path=str(pdf_path), authors=["Tester"])
        si = Paper(title="SI", pdf_path=str(pdf_path), authors=["Tester"])
        session.add_all([main, si]); session.flush()
        catalyst = CatalystSample(paper_id=main.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        session.add(catalyst); session.flush()
        rows = [DFTResult(paper_id=main.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2, unit="eV", evidence_text=evidence) for _ in range(3)]
        session.add_all(rows); session.flush()
        legacy = ExtractionFieldReview(
            paper_id=main.id, target_type="dft_result", target_id=str(rows[0].id), field_name="value",
            reviewer_status="pending", target_resolution_status="active", write_version=3,
        )
        session.add_all([
            legacy,
            EvidenceLocator(paper_id=main.id, target_type="dft_result", target_id=str(rows[0].id), field_name="value", evidence_text=evidence, page=1, locator_status="exact_page"),
        ])
        session.commit()
        service = AIVerificationService(session)
        first = service.list_dft_record_tasks(paper_id=main.id, limit=1)
        bundle = first["records"][0]
        value = next(item for item in bundle["fields"] if item["field_name"] == "value")
        if bundle["record_id"] == str(rows[0].id):
            assert value["expected_write_version"] == legacy.write_version
            assert len(value["evidence_candidates"]) == 1
        statements: list[str] = []
        def capture_keyset(_conn, _cursor, statement, *_args):
            if "dft_results" in statement.casefold():
                statements.append(statement.casefold())
        event.listen(session.bind, "before_cursor_execute", capture_keyset)
        try:
            second = service.list_dft_record_tasks(paper_id=main.id, limit=1, cursor=first["next_cursor"])
        finally:
            event.remove(session.bind, "before_cursor_execute", capture_keyset)
        assert all(item["record_id"] != bundle["record_id"] for item in second["records"])
        assert any("dft_results.id >" in statement and "limit" in statement for statement in statements)

        # A current block becomes stale as soon as a locator, PDF revision, or
        # explicit SI relationship changes; each is a fresh evidence package.
        blocked_submission = AIVerificationSubmission(
            target_type="dft_results", target_id=bundle["record_id"], field_name="value", decision="defer", confidence=0.1,
            expected_target_fingerprint=value["target_snapshot_fingerprint"], expected_write_version=value["expected_write_version"],
            blocked_reasons=["no_supporting_evidence"], page=1,
        )
        service.process_batch(paper_id=main.id, submissions=[blocked_submission], identity=_identity(), dry_run=False)
        row = session.get(DFTResult, UUID(bundle["record_id"]))
        session.add(EvidenceLocator(paper_id=main.id, target_type="dft_results", target_id=str(row.id), field_name="value", evidence_text=evidence, page=1, locator_status="exact_page"))
        session.commit()
        after_locator = service.list_dft_record_tasks(paper_id=main.id, limit=10)
        assert next(item for item in next(bundle for bundle in after_locator["records"] if bundle["record_id"] == str(row.id))["fields"] if item["field_name"] == "value")["current_status"] == "pending"
        locator_value = next(item for item in next(bundle for bundle in after_locator["records"] if bundle["record_id"] == str(row.id))["fields"] if item["field_name"] == "value")
        service.process_batch(paper_id=main.id, submissions=[AIVerificationSubmission(
            target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
            expected_target_fingerprint=locator_value["target_snapshot_fingerprint"], expected_write_version=locator_value["expected_write_version"],
            blocked_reasons=["no_supporting_evidence"], page=1,
        )], identity=_identity(), dry_run=False)
        pdf_path.touch()
        after_pdf = service.list_dft_record_tasks(paper_id=main.id, limit=10)
        pdf_value = next(item for item in next(bundle for bundle in after_pdf["records"] if bundle["record_id"] == str(row.id))["fields"] if item["field_name"] == "value")
        assert pdf_value["current_status"] == "pending"
        service.process_batch(paper_id=main.id, submissions=[AIVerificationSubmission(
            target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
            expected_target_fingerprint=pdf_value["target_snapshot_fingerprint"], expected_write_version=pdf_value["expected_write_version"],
            blocked_reasons=["no_supporting_evidence"], page=1,
        )], identity=_identity(), dry_run=False)
        session.add(PaperRelationship(source_paper_id=main.id, target_paper_id=si.id, relationship_type="supplementary"))
        session.commit()
        after_si = service.list_dft_record_tasks(paper_id=main.id, limit=10)
        si_value = next(item for item in next(bundle for bundle in after_si["records"] if bundle["record_id"] == str(row.id))["fields"] if item["field_name"] == "value")
        assert si_value["current_status"] == "pending" and after_si["database_writes"] is False


def test_table_backed_block_scope_ignores_other_record_table_but_stales_its_own(setup_test_db, tmp_path):
    pdf_path = tmp_path / "field-scoped-tables.pdf"
    _make_pdf(pdf_path, {1: "Table S1 Fe-N4 Li2S4 adsorption energy -1.20; Table S2 Fe-N4 -1.30"})
    with Session(setup_test_db) as session:
        paper = Paper(title="field scoped table blocks", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper); session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        one = PaperTable(paper_id=paper.id, page=1, caption="Table S1.", markdown_content="| Catalyst | Li2S4 adsorption energy (eV) |\n| --- | --- |\n| Fe-N4 | -1.20 |")
        two = PaperTable(paper_id=paper.id, page=1, caption="Table S2.", markdown_content="| Catalyst | Li2S4 adsorption energy (eV) |\n| --- | --- |\n| Fe-N4 | -1.30 |")
        session.add_all([catalyst, one, two]); session.flush()
        rows = [
            DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=value, unit="eV")
            for value in (-1.2, -1.3)
        ]
        session.add_all(rows); session.commit()
        service = AIVerificationService(session)
        for row, table in zip(rows, (one, two), strict=True):
            submission = AIVerificationSubmission(
                target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
                expected_target_fingerprint=ai_target_fingerprint("dft_results", row), blocked_reasons=["no_supporting_evidence"],
                page=1, table_id=str(table.id), source_row_index=0, source_column_index=1,
            )
            assert service.process_batch(paper_id=paper.id, submissions=[submission], identity=_identity(), dry_run=False)["auto_deferred"] == 1

        two.markdown_content = two.markdown_content.replace("-1.30", "-1.31")
        session.commit()
        after_unrelated = service.list_dft_record_tasks(paper_id=paper.id, limit=10, include_blocked=True)
        one_value = next(field for bundle in after_unrelated["records"] if bundle["record_id"] == str(rows[0].id) for field in bundle["fields"] if field["field_name"] == "value")
        assert one_value["current_status"] == "ai_blocked"
        one.markdown_content = one.markdown_content.replace("-1.20", "-1.21")
        session.commit()
        after_referenced = service.list_dft_record_tasks(paper_id=paper.id, limit=10)
        one_value = next(field for bundle in after_referenced["records"] if bundle["record_id"] == str(rows[0].id) for field in bundle["fields"] if field["field_name"] == "value")
        assert one_value["current_status"] == "pending"


def test_table_backed_blocks_preload_constant_queries_and_index_once(setup_test_db, tmp_path, monkeypatch):
    pdf_path = tmp_path / "table-backed-page.pdf"
    evidence = "Table S1. Fe-N4 Li2S4 adsorption energy (eV) -1.20"
    _make_pdf(pdf_path, {1: evidence})
    with Session(setup_test_db) as session:
        paper = Paper(title="many table-backed blocks", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper); session.flush()
        table = PaperTable(paper_id=paper.id, page=1, caption="Table S1.", markdown_content="| Catalyst | Li2S4 adsorption energy (eV) |\n| --- | --- |\n| Fe-N4 | -1.20 |")
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        session.add_all([table, catalyst]); session.flush()
        rows = [DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2, unit="eV") for _ in range(20)]
        session.add_all(rows); session.flush()
        cell = build_structured_table_cell_evidence(session, paper_id=paper.id, table_id=table.id, page=1, source_row_index=0, source_column_index=1)
        assert cell is not None
        for row in rows:
            for field_name in required_dft_review_fields(row):
                session.add(EvidenceLocator(paper_id=paper.id, target_type="dft_results", target_id=str(row.id), field_name=field_name, evidence_text=cell["canonical_evidence_text"], page=1, table_id=table.id, locator_status="exact_page"))
        session.commit()
        service = AIVerificationService(session)
        for row in rows:
            assert service.process_batch(paper_id=paper.id, submissions=[AIVerificationSubmission(
                target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
                expected_target_fingerprint=ai_target_fingerprint("dft_results", row), blocked_reasons=["no_supporting_evidence"],
                page=1, table_id=str(table.id), source_row_index=0, source_column_index=1,
            )], identity=_identity(), dry_run=False)["auto_deferred"] == 1
        from app.services import ai_verification_service as service_module
        original_index = service_module.build_structured_table_cell_evidence_index
        original_reader = service_module.cached_read_pdf_page_text
        index_calls = 0
        page_reads = 0
        def count_index(*args, **kwargs):
            nonlocal index_calls
            index_calls += 1
            return original_index(*args, **kwargs)
        def count_reads(*args, **kwargs):
            nonlocal page_reads
            page_reads += 1
            return original_reader(*args, **kwargs)
        monkeypatch.setattr(service_module, "build_structured_table_cell_evidence_index", count_index)
        monkeypatch.setattr(service_module, "cached_read_pdf_page_text", count_reads)
        counts = {name: 0 for name in ("catalyst_samples", "active_site_metals", "extraction_field_reviews", "evidence_locators", "paper_tables", "paper_relationships")}
        def count_queries(_conn, _cursor, statement, *_args):
            lowered = statement.casefold()
            for name in counts:
                if name in lowered:
                    counts[name] += 1
        event.listen(session.bind, "before_cursor_execute", count_queries)
        try:
            result = service.list_dft_record_tasks(paper_id=paper.id, limit=20, include_blocked=True)
        finally:
            event.remove(session.bind, "before_cursor_execute", count_queries)
        assert result["page_blocked_fields"] == 20
        assert index_calls == 1
        assert page_reads == 1
        assert all(value <= 2 for value in counts.values()), counts


def test_record_cursor_hides_blocked_by_default_and_never_loses_raw_keyset_progress(setup_test_db, tmp_path):
    pdf_path = tmp_path / "blocked-keyset.pdf"
    _make_pdf(pdf_path, {1: "Fe-N4 Li2S4 adsorption energy -1.20 eV"})
    with Session(setup_test_db) as session:
        paper = Paper(title="blocked cursor", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper); session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="FeN4")
        session.add(catalyst); session.flush()
        rows = [DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2, unit="eV") for _ in range(2)]
        session.add_all(rows); session.commit()
        service = AIVerificationService(session)
        for row in rows:
            first = service.list_dft_record_tasks(paper_id=paper.id, limit=10, include_blocked=True)
            bundle = next(item for item in first["records"] if item["record_id"] == str(row.id))
            submissions = [AIVerificationSubmission(
                target_type="dft_results", target_id=str(row.id), field_name=field["field_name"], decision="defer", confidence=0.1,
                expected_target_fingerprint=field["target_snapshot_fingerprint"], expected_write_version=field["expected_write_version"],
                blocked_reasons=["no_supporting_evidence"], page=1,
            ) for field in bundle["fields"] if field["required"]]
            assert service.process_batch(paper_id=paper.id, submissions=submissions, identity=_identity(), dry_run=False)["auto_deferred"] == len(submissions)
        first = service.list_dft_record_tasks(paper_id=paper.id, limit=1)
        assert first["returned_records"] == 0 and first["has_more"] is True and first["next_cursor"]
        shown = service.list_dft_record_tasks(paper_id=paper.id, limit=1, include_blocked=True)
        assert shown["returned_records"] == 1 and all(field["blocked_reasons"] for field in shown["records"][0]["fields"] if field["required"])
        last = service.list_dft_record_tasks(paper_id=paper.id, limit=1, cursor=first["next_cursor"])
        assert last["returned_records"] == 0 and last["has_more"] is False and last["next_cursor"] is None
        other = Paper(title="other", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(other); session.commit()
        malformed_uuid = base64.urlsafe_b64encode(json.dumps({"v": 1, "paper_id": str(paper.id), "last_id": "not-a-uuid"}).encode("utf-8")).decode("ascii").rstrip("=")
        for bad in ("not-base64", service._encode_record_cursor(other.id, rows[0].id), malformed_uuid):
            with pytest.raises(ValueError, match="Invalid record-task cursor"):
                service.list_dft_record_tasks(paper_id=paper.id, limit=1, cursor=bad)


def test_ai_blocked_alone_blocks_exports_mixed_bundle_and_real_mcp_read_only_transaction(setup_test_db, tmp_path, monkeypatch):
    with Session(setup_test_db) as session:
        paper, si_paper, _unrelated, _catalyst, row = _seed_test_papers(session, tmp_path)
        _authorize_dft_result(session, row, evidence_paper_id=si_paper.id, page=17)
        session.commit()

        def exported_ids(profile: str | None) -> tuple[set[str], set[str], list[str]]:
            payload = build_dft_ml_dataset(session, paper_id=paper.id, dataset_profile=profile)
            json_ids = {item["record_id"] for item in payload["records"]}
            csv_text, _summary = build_dft_csv_rows(session, paper_id=paper.id, dataset_profile=profile)
            csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
            assert csv_rows or csv_text.splitlines()[0].split(",")[0] == "record_id"
            assert "record_id" in csv_text.splitlines()[0].split(",")
            return json_ids, {item["record_id"] for item in csv_rows}, list(csv_rows[0]) if csv_rows else ["record_id"]

        baseline = is_export_eligible_extraction(session, row, target_type="dft_results")
        assert baseline.eligible is True, baseline.reasons
        for profile in (None, "sac_lis_ml"):
            json_ids, csv_ids, columns = exported_ids(profile)
            assert columns[0] == "record_id"
            assert json_ids == {str(row.id)} == csv_ids

        value_review = session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper.id,
            ExtractionFieldReview.target_id == str(row.id),
            ExtractionFieldReview.field_name == "value",
        ))
        assert value_review is not None and is_authoritative_verified_review(session, value_review, row)
        verified_state = {
            "reviewer_status": value_review.reviewer_status,
            "reviewer": value_review.reviewer,
            "review_payload": copy.deepcopy(value_review.review_payload),
            "target_fingerprint": value_review.target_fingerprint,
            "evidence_text": value_review.evidence_text,
            "original_value": value_review.original_value,
            "reviewed_value": value_review.reviewed_value,
            "unit": value_review.unit,
        }
        # The service intentionally refuses to let AI defer overwrite a human
        # verification.  Remove only this isolated fixture review so defer is
        # exercised through its real write path, while all other required
        # authoritative review/locator pairs remain intact.
        session.delete(value_review)
        session.flush()
        deferred = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[AIVerificationSubmission(
                target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
                expected_target_fingerprint=ai_target_fingerprint("dft_results", row),
                expected_write_version=None, blocked_reasons=["no_supporting_evidence"],
                evidence_paper_id=str(si_paper.id), page=17,
            )],
            identity=_identity(), dry_run=False,
        )
        assert deferred["auto_deferred"] == 1
        value_review = session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper.id,
            ExtractionFieldReview.target_id == str(row.id),
            ExtractionFieldReview.field_name == "value",
        ))
        assert value_review is not None
        assert value_review.reviewer_status == "ai_blocked"
        blocked_gate = is_export_eligible_extraction(session, row, target_type="dft_results")
        assert blocked_gate.eligible is False
        assert "non_authoritative_review:value" in blocked_gate.reasons
        for profile in (None, "sac_lis_ml"):
            json_ids, csv_ids, columns = exported_ids(profile)
            assert columns[0] == "record_id"
            assert str(row.id) not in json_ids
            assert str(row.id) not in csv_ids

        for name, value in verified_state.items():
            setattr(value_review, name, value)
        session.commit()
        restored_gate = is_export_eligible_extraction(session, row, target_type="dft_results")
        assert restored_gate.eligible is True, restored_gate.reasons
        session.delete(value_review)
        session.flush()
        assert AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[AIVerificationSubmission(
                target_type="dft_results", target_id=str(row.id), field_name="value", decision="defer", confidence=0.1,
                expected_target_fingerprint=ai_target_fingerprint("dft_results", row),
                expected_write_version=None, blocked_reasons=["no_supporting_evidence"],
                evidence_paper_id=str(si_paper.id), page=17,
            )],
            identity=_identity(), dry_run=False,
        )["auto_deferred"] == 1
        assert "non_authoritative_review:value" in is_export_eligible_extraction(
            session, row, target_type="dft_results",
        ).reasons

        adsorbate_review = session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper.id,
            ExtractionFieldReview.target_id == str(row.id),
            ExtractionFieldReview.field_name == "adsorbate",
        ))
        assert adsorbate_review is not None
        session.delete(adsorbate_review)
        session.commit()
        mixed = AIVerificationService(session).list_dft_record_tasks(paper_id=paper.id, limit=1)
        assert mixed["returned_records"] == 1
        mixed_fields = {field["field_name"]: field for field in mixed["records"][0]["fields"] if field["required"]}
        assert mixed_fields["value"]["current_status"] == "ai_blocked"
        assert mixed_fields["value"]["blocked_reasons"] == ["no_supporting_evidence"]
        assert mixed_fields["adsorbate"]["current_status"] == "pending"

        from app.config import get_settings
        from app.services import ai_verification_service as service_module
        before_targets = session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id))
        before_audits = session.scalar(select(func.count(AuditLog.id)).where(AuditLog.paper_id == paper.id))
        observed_read_only: list[str] = []
        original_list = service_module.AIVerificationService.list_dft_record_tasks

        def observe_real_transaction(self, **kwargs):
            observed_read_only.append(str(self.session.scalar(text("SELECT current_setting('transaction_read_only')"))))
            return original_list(self, **kwargs)

        monkeypatch.setenv("LITAI_MCP_API_KEYS", "test_ai|Test AI|litmcp_test_ai|ai_verify_content")
        get_settings.cache_clear()
        monkeypatch.setattr(service_module.AIVerificationService, "list_dft_record_tasks", observe_real_transaction)
        with mcp_auth_context("litmcp_test_ai"):
            result = get_ai_verification_record_tasks(str(paper.id), limit=1)
        assert result["postgres_transaction_read_only"] is True
        assert observed_read_only == ["on"]
        assert session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)) == before_targets
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.paper_id == paper.id)) == before_audits
        get_settings.cache_clear()
