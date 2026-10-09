from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    CatalystSample,
    DFTResult,
    ElectrochemicalPerformance,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
)
from app.services.lis_sac_dac_feature_service import LiSSacDacFeatureService


def _seed_paper(session: Session, *, title: str) -> Paper:
    paper = Paper(
        title=title,
        library_name="锂硫双原子",
        pdf_path=f"{title}.pdf",
    )
    session.add(paper)
    session.flush()
    return paper


def _row_counts(session: Session) -> dict[str, int]:
    return {
        "papers": session.scalar(select(func.count(Paper.id))) or 0,
        "catalyst_samples": session.scalar(select(func.count(CatalystSample.id))) or 0,
        "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
        "performance": session.scalar(select(func.count(ElectrochemicalPerformance.id))) or 0,
        "external_candidates": session.scalar(select(func.count(ExternalAnalysisCandidate.id))) or 0,
    }


def _seed_external_run(session: Session, *, paper: Paper) -> ExternalAnalysisRun:
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="pytest",
        source_label="lis_sac_dac_feature_service_test",
        normalized_payload={},
        mapping_status="pending",
    )
    session.add(run)
    session.flush()
    return run


def test_structure_extraction_returns_ready_without_writes(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Structure ready")
        run = _seed_external_run(session, paper=paper)
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Fe-Co-N-C",
            catalyst_type="dual_atom",
            metal_centers=["Fe", "Co"],
            coordination="FeN2-CoN2",
            support="N-doped carbon",
        )
        session.add(catalyst)
        session.flush()
        dft = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="adsorption_energy",
            reaction_type="SRR_LiS",
            evidence_payload={
                "structure": {
                    "metal_metal_distance": {"value": 2.53, "unit": "Å"},
                }
            },
        )
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="structure_features",
            normalized_payload={
                "structure": {
                    "catalyst_scope": "DAC",
                    "support_material": "N-doped carbon",
                }
            },
            status="pending",
        )
        session.add_all([dft, candidate])
        session.commit()

    service = LiSSacDacFeatureService()
    with SessionLocal() as session:
        catalyst = session.scalar(select(CatalystSample))
        dft = session.scalar(select(DFTResult))
        candidate = session.scalar(select(ExternalAnalysisCandidate))
        before = _row_counts(session)
        payload = service.extract_structure_features(
            catalyst_sample=catalyst,
            dft_result=dft,
            candidate_payload=candidate.normalized_payload,
        )
        after = _row_counts(session)

    assert before == after
    assert payload.read_only is True
    assert payload.auto_verification_applied is False
    assert payload.status == "ready"
    assert payload.blockers == []
    assert payload.fields["metal_centers"].value == ["Fe", "Co"]
    assert payload.fields["catalyst_scope"].value == "DAC"
    assert payload.fields["metal_pairing_type"].value == "heteronuclear"
    assert payload.fields["coordination_environment"].value == "FeN2-CoN2"
    assert payload.fields["support_material"].value == "N-doped carbon"
    assert payload.fields["metal_metal_distance"].value == 2.53
    assert payload.fields["metal_metal_distance"].unit == "angstrom"


def test_structure_extraction_marks_missing_core_fields_as_blockers(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Structure blockers")
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="atomically dispersed catalyst",
            catalyst_type="atomically dispersed catalyst",
            metal_centers=[],
            coordination=None,
            support=None,
        )
        session.add(catalyst)
        session.commit()

    with SessionLocal() as session:
        catalyst = session.scalar(select(CatalystSample))
        payload = LiSSacDacFeatureService().extract_structure_features(catalyst_sample=catalyst)

    assert payload.status == "needs_fields"
    assert payload.read_only is True
    assert payload.auto_verification_applied is False
    assert "missing_metal_centers" in payload.blockers
    assert "ambiguous_catalyst_scope" in payload.blockers
    assert "missing_coordination_environment" in payload.blockers
    assert payload.fields["metal_centers"].unknown is True
    assert payload.fields["catalyst_scope"].unknown is True
    assert payload.fields["coordination_environment"].unknown is True


def test_structure_extraction_does_not_treat_lips_species_as_metal_centers(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Weak text structure")
        run = _seed_external_run(session, paper=paper)
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="structure_features",
            normalized_payload={
                "structure": {
                    "catalyst_scope": "DAC",
                    "material_identity": "Li2S4 adsorbed on Fe-N-C catalyst",
                    "coordination_environment": "Fe-N4",
                    "support_material": "N-doped carbon",
                    "metal_metal_distance": {"value": 2.4, "unit": "angstrom"},
                }
            },
            status="pending",
        )
        session.add(candidate)
        session.commit()

    with SessionLocal() as session:
        candidate = session.scalar(select(ExternalAnalysisCandidate))
        before = _row_counts(session)
        payload = LiSSacDacFeatureService().extract_structure_features(
            candidate_payload=candidate.normalized_payload,
        )
        after = _row_counts(session)

    assert before == after
    assert payload.read_only is True
    assert payload.auto_verification_applied is False
    assert payload.fields["metal_centers"].value == ["Fe"]
    assert "Li" not in payload.fields["metal_centers"].value
    assert payload.fields["metal_pairing_type"].unknown is True
    assert payload.status == "needs_fields"
    assert "ambiguous_metal_centers_weak_text" in payload.blockers
    assert "missing_metal_pairing_type" in payload.blockers


def test_experimental_performance_extraction_normalizes_explicit_units_without_writes(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Performance ready")
        run = _seed_external_run(session, paper=paper)
        performance = ElectrochemicalPerformance(
            paper_id=paper.id,
            sulfur_loading_mg_cm2=3.1,
            electrolyte_sulfur_ratio="8 uL mg^-1",
            cycle_number=300,
            rate="0.5 C",
            decay_per_cycle=0.08,
        )
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="experimental_performance",
            normalized_payload={
                "experimental_performance": {
                    "specific_capacity": {"value": 1250, "unit": "mAh g^-1"},
                    "cycling_stability_cycles": {"value": 300, "unit": "cycles"},
                    "capacity_decay_rate": {"value": 0.08, "unit": "% per cycle"},
                    "sulfur_loading": {"value": 3.1, "unit": "mg cm^-2"},
                    "electrolyte_to_sulfur_ratio": {"value": 8, "unit": "uL mg^-1"},
                    "rate_c_value": {"value": 0.5, "unit": "C"},
                }
            },
            status="pending",
        )
        session.add_all([performance, candidate])
        session.commit()

    service = LiSSacDacFeatureService()
    with SessionLocal() as session:
        performance = session.scalar(select(ElectrochemicalPerformance))
        candidate = session.scalar(select(ExternalAnalysisCandidate))
        before = _row_counts(session)
        payload = service.extract_experimental_performance_features(
            performance=performance,
            candidate_payload=candidate.normalized_payload,
        )
        after = _row_counts(session)

    assert before == after
    assert payload.read_only is True
    assert payload.auto_verification_applied is False
    assert payload.status == "ready"
    assert payload.blockers == []
    assert payload.fields["specific_capacity"].value == 1250
    assert payload.fields["specific_capacity"].unit == "mAh g^-1"
    assert payload.fields["rate_c_value"].value == 0.5
    assert payload.fields["rate_c_value"].unit == "C"
    assert payload.fields["sulfur_loading"].value == 3.1
    assert payload.fields["electrolyte_to_sulfur_ratio"].value == 8


def test_experimental_performance_keeps_unknown_when_units_require_conversion(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Performance blockers")
        run = _seed_external_run(session, paper=paper)
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="experimental_performance",
            normalized_payload={
                "experimental_performance": {
                    "specific_capacity": {"value": 4.2, "unit": "mAh cm^-2"},
                    "rate_c_value": "500 mA g^-1",
                    "electrolyte_to_sulfur_ratio": {"value": 10, "unit": "mL g^-1"},
                }
            },
            status="pending",
        )
        session.add(candidate)
        session.commit()

    with SessionLocal() as session:
        candidate = session.scalar(select(ExternalAnalysisCandidate))
        payload = LiSSacDacFeatureService().extract_experimental_performance_features(
            candidate_payload=candidate.normalized_payload,
        )

    assert payload.status == "needs_fields"
    assert payload.read_only is True
    assert payload.auto_verification_applied is False
    assert "unsupported_specific_capacity_unit" in payload.blockers
    assert "rate_requires_conversion" in payload.blockers
    assert "unsupported_electrolyte_to_sulfur_ratio_unit" in payload.blockers
    assert payload.fields["specific_capacity"].unknown is True
    assert payload.fields["rate_c_value"].unknown is True
    assert payload.fields["electrolyte_to_sulfur_ratio"].unknown is True
