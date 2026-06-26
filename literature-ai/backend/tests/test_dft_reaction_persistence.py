from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, Paper
from app.domain.reaction_taxonomy import PROFILE_VERSION
from app.normalizers.chemistry_normalizer import ChemistryNormalizer
from app.services.extraction_pipeline import ExtractionPipelineService


@pytest.fixture
def persistence_env():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    with Session(engine) as session:
        paper = Paper(title="Reaction persistence test", pdf_path="test.pdf", authors=[])
        session.add(paper)
        session.flush()

        service = object.__new__(ExtractionPipelineService)
        service.session = session
        service.chemistry_normalizer = ChemistryNormalizer()
        service._persist_evidence_span = lambda **_: None
        yield session, paper, service
    engine.dispose()


def _persist_one(persistence_env, **overrides):
    session, paper, service = persistence_env
    item = {
        "category": "adsorption_energy",
        "adsorbate": "Li2S6",
        "value": -1.25,
        "unit": "eV",
        "reaction_step": "Li2S8 -> Li2S6",
        "evidence_text": "The Li2S6 adsorption energy is -1.25 eV.",
        "confidence": 0.88,
    }
    item.update(overrides)
    assert service._persist_dft_results(paper.id, [item]) == 1
    session.flush()
    return session.scalars(select(DFTResult)).one()


def test_new_srr_candidate_persists_rule_classification(persistence_env):
    row = _persist_one(persistence_env)

    assert row.reaction_type == "SRR_LiS"
    assert row.reaction_type_source == "rule"
    assert row.reaction_type_confidence == 0.95
    assert row.reaction_profile_version == PROFILE_VERSION
    assert row.reaction_validation_status == "valid"


def test_shared_intermediate_under_default_srr_target_is_preserved_out_of_scope(persistence_env):
    row = _persist_one(
        persistence_env,
        adsorbate="*OOH",
        category="gibbs_free_energy_change",
        reaction_step="*O -> *OOH",
        evidence_text="The free energy change for *OOH is 0.42 eV.",
    )

    assert row.candidate_status == "system_candidate"
    assert row.reaction_type == "SRR_LiS"
    assert row.reaction_type_source == "rule"
    assert row.reaction_validation_status == "out_of_scope"


def test_explicit_reaction_type_is_preserved(persistence_env):
    row = _persist_one(
        persistence_env,
        reaction_type="OER",
        adsorbate="*OOH",
        category="gibbs_free_energy_change",
        reaction_step="*O -> *OOH",
        evidence_text="For OER, the *O to *OOH free energy change is 0.42 eV.",
    )

    assert row.reaction_type == "OER"
    assert row.reaction_type_source == "explicit"
    assert row.reaction_type_confidence == 1.0
    assert row.reaction_validation_status == "valid"


def test_out_of_scope_candidate_is_preserved_without_verified_status(persistence_env):
    row = _persist_one(
        persistence_env,
        reaction_type="SRR_LiS",
        adsorbate="*OOH",
        evidence_text="The *OOH adsorption energy is -1.25 eV.",
    )

    assert row.candidate_status == "system_candidate"
    assert row.reaction_type == "SRR_LiS"
    assert row.reaction_validation_status == "out_of_scope"
    assert row.reaction_validation_status not in {"verified", "safe_verified"}


def test_candidate_savepoint_failure_does_not_block_later_candidate(persistence_env):
    session, paper, service = persistence_env
    calls = 0

    def fail_first_evidence_span(**_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated evidence persistence failure")

    service._persist_evidence_span = fail_first_evidence_span
    count = service._persist_dft_results(
        paper.id,
        [
            {
                "category": "adsorption_energy",
                "adsorbate": "Li2S6",
                "value": -1.25,
                "unit": "eV",
                "evidence_text": "First candidate fails after its row flush.",
            },
            {
                "category": "adsorption_energy",
                "adsorbate": "Li2S4",
                "value": -1.05,
                "unit": "eV",
                "evidence_text": "Second candidate persists.",
            },
        ],
    )
    session.flush()
    rows = session.scalars(select(DFTResult)).all()

    assert count == 1
    assert len(rows) == 1
    assert rows[0].adsorbate == "Li2S4"
    assert rows[0].reaction_validation_status == "valid"


def test_existing_human_reaction_fields_are_not_overwritten(persistence_env):
    session, paper, service = persistence_env
    existing = DFTResult(
        paper_id=paper.id,
        adsorbate="Li2S6",
        property_type="adsorption_energy",
        value=-1.25,
        unit="eV",
        reaction_step="Li2S8 -> Li2S6",
        confidence=0.5,
        candidate_status="system_candidate",
        reaction_type="HER",
        reaction_type_source="human",
        reaction_type_confidence=0.77,
        reaction_profile_version="human_profile_v9",
        reaction_validation_status="unsupported",
    )
    session.add(existing)
    session.flush()

    count = service._persist_dft_results(
        paper.id,
        [
            {
                "category": "adsorption_energy",
                "adsorbate": "Li2S6",
                "value": -1.25,
                "unit": "eV",
                "reaction_step": "Li2S8 -> Li2S6",
                "evidence_text": "A stronger duplicate extraction.",
                "confidence": 0.9,
            }
        ],
    )
    session.flush()

    assert count == 1
    assert session.scalars(select(DFTResult)).all() == [existing]
    assert existing.reaction_type == "HER"
    assert existing.reaction_type_source == "human"
    assert existing.reaction_type_confidence == 0.77
    assert existing.reaction_profile_version == "human_profile_v9"
    assert existing.reaction_validation_status == "unsupported"


def test_existing_nonhuman_reaction_type_does_not_mix_incoming_metadata(persistence_env):
    session, paper, service = persistence_env
    existing = DFTResult(
        paper_id=paper.id,
        adsorbate="Li2S6",
        property_type="adsorption_energy",
        value=-1.25,
        unit="eV",
        reaction_step="Li2S8 -> Li2S6",
        confidence=0.5,
        candidate_status="system_candidate",
        reaction_type="HER",
        reaction_type_source="rule",
        reaction_type_confidence=None,
        reaction_profile_version=None,
        reaction_validation_status=None,
    )
    session.add(existing)
    session.flush()

    count = service._persist_dft_results(
        paper.id,
        [
            {
                "category": "adsorption_energy",
                "adsorbate": "Li2S6",
                "value": -1.25,
                "unit": "eV",
                "reaction_step": "Li2S8 -> Li2S6",
                "evidence_text": "The Li2S6 adsorption energy is -1.25 eV.",
                "confidence": 0.9,
            }
        ],
    )
    session.flush()

    assert count == 1
    assert session.scalars(select(DFTResult)).all() == [existing]
    assert existing.reaction_type == "HER"
    assert existing.reaction_type_source == "rule"
    assert existing.reaction_type_confidence is None
    assert existing.reaction_validation_status is None
    assert existing.reaction_profile_version == PROFILE_VERSION
    assert existing.reaction_type_confidence != 0.95
    assert existing.reaction_validation_status != "valid"
