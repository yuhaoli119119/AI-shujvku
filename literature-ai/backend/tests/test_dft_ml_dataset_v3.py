from __future__ import annotations

import copy
import csv
import io
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CatalystSample, DFTResult, DFTSetting, EvidenceSpan, ExtractionFieldReview, Paper
from app.services.dft_export_service import build_dft_ml_dataset, build_dft_ml_dataset_v3, build_dft_ml_dataset_v3_csv


def _session():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, future=True)


def _row(
    session,
    *,
    property_type="adsorption_energy",
    reaction_type="SRR_LiS",
    status="valid",
    complete=True,
    with_setting=True,
    evidence_object_type="dft_result",
    title=None,
    year=None,
    reaction_step="Li2S4 adsorption",
    evidence_text=None,
    value=-1.23,
    unit="eV",
):
    paper = Paper(
        title=title or f"Paper {property_type} {reaction_type} {status}",
        year=year,
        pdf_path="paper.pdf",
        authors=["A"],
    )
    session.add(paper)
    session.flush()
    catalyst = CatalystSample(
        paper_id=paper.id,
        name="Fe-N-C",
        catalyst_type="single_atom",
        metal_centers=["Fe"],
        coordination="Fe-N4" if complete else None,
        support="carbon",
    )
    session.add(catalyst)
    session.flush()
    row = DFTResult(
        paper_id=paper.id,
        catalyst_sample_id=catalyst.id,
        adsorbate="Li2S4",
        property_type=property_type,
        value=value,
        unit=unit,
        reaction_step=reaction_step,
        evidence_text=evidence_text or "Li2S4 adsorption is -1.23 eV.",
        reaction_type=reaction_type,
        reaction_profile_version="reaction_profiles_v1",
        reaction_validation_status=status,
    )
    session.add(row)
    session.flush()
    related = [
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                reviewer_status="verified",
                target_resolution_status="active",
                evidence_text=row.evidence_text,
            ),
            EvidenceSpan(
                paper_id=paper.id,
                object_type=evidence_object_type,
                object_id=str(row.id),
                text=row.evidence_text,
                page=7,
            ),
        ]
    if with_setting:
        related.append(DFTSetting(paper_id=paper.id, software="VASP", functional="PBE"))
    session.add_all(related)
    return row, paper


def _without_created_at(payload):
    result = copy.deepcopy(payload)
    result["metadata"].pop("created_at", None)
    return result


def test_v3_ready_record_contract_and_v2_is_unchanged():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            row, paper = _row(session)
            session.commit()
            before = build_dft_ml_dataset(session)
            payload = build_dft_ml_dataset_v3(session, task="adsorption_energy")
            after = build_dft_ml_dataset(session)

            assert _without_created_at(before) == _without_created_at(after)
            assert payload["manifest"]["schema_version"] == "dft_results_ml_v3"
            assert payload["manifest"]["source_schema_version"] == "dft_results_ml_v2"
            assert payload["manifest"]["task_status"] == "candidate"
            assert payload["manifest"]["property_type_fields"] == [
                "property_type",
                "normalized_property_type",
                "canonical_property_type",
                "property_subtype",
            ]
            record = payload["records"][0]
            assert record["record_id"] == str(row.id)
            assert record["target"]["value"] == -1.23
            assert record["target"]["normalized_value"] == -1.23
            assert record["provenance"]["evidence_text"]
            assert record["provenance"]["locator_status"] in {"exact", "exact_page", "verified"}
            assert record["provenance"]["page_locators"] == [7]
            assert record["reaction_type"] == "SRR_LiS"
            assert record["reaction_profile_version"] == "reaction_profiles_v1"
            assert record["label_ready"] is True
            assert record["tabular_ml_ready"] is True
            assert record["split_group_values"]["paper_id"] == str(paper.id)
            family = record["split_group_values"]["catalyst_family"]
            assert str(row.id) not in family and "li2s4" not in family
    finally:
        engine.dispose()


def test_v3_keeps_blocked_records_and_ready_only_filters_them():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            _row(session, complete=False)
            _row(session, with_setting=False)
            _row(session, property_type="cohesive_energy")
            session.commit()
            default = build_dft_ml_dataset_v3(session, task="adsorption_energy")
            ready = build_dft_ml_dataset_v3(session, task="adsorption_energy", ready_only=True)

            assert len(default["records"]) == 2
            feature_blocked = next(record for record in default["records"] if record["label_ready"])
            label_blocked = next(record for record in default["records"] if not record["label_ready"])
            assert feature_blocked["tabular_ml_ready"] is False
            assert "missing_coordination" in feature_blocked["feature_blockers"]
            assert "missing_result_setting_link" in label_blocked["label_blockers"]
            assert ready["records"] == []
            assert default["manifest"]["excluded_counts"] == {"target_property_not_allowed": 1}
    finally:
        engine.dispose()


def test_v3_exclusion_counts_and_task_separation_are_stable():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            _row(session)
            _row(session, property_type="reaction_barrier")
            _row(session, reaction_type="HER")
            _row(session, reaction_type="OER")
            _row(session, reaction_type="UNKNOWN")
            _row(session, status="ambiguous")
            _row(session, status="out_of_scope")
            session.commit()

            adsorption = build_dft_ml_dataset_v3(session, task="adsorption_energy")
            barrier = build_dft_ml_dataset_v3(session, task="reaction_barrier")
            assert {r["target"]["canonical_property_type"] for r in adsorption["records"]} == {"adsorption_energy"}
            assert {r["target"]["canonical_property_type"] for r in barrier["records"]} == {"reaction_barrier"}
            assert adsorption["manifest"]["excluded_counts"] == {
                "reaction_type_HER": 1,
                "reaction_type_OER": 1,
                "reaction_validation_ambiguous": 1,
                "reaction_validation_out_of_scope": 1,
                "target_property_not_allowed": 1,
                "unknown_reaction_type": 1,
            }
            assert adsorption["manifest"]["task_status"] == barrier["manifest"]["task_status"] == "candidate"
    finally:
        engine.dispose()


def test_v3_unknown_task_and_empty_dataset():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            with pytest.raises(KeyError, match="Unknown tabular task"):
                build_dft_ml_dataset_v3(session, task="not-a-task")
            payload = build_dft_ml_dataset_v3(session, task="adsorption_energy")
            assert payload["records"] == []
            assert payload["manifest"]["returned_count"] == 0
            assert payload["manifest"]["excluded_counts"] == {}
    finally:
        engine.dispose()


def test_v3_limit_is_applied_after_task_filtering():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            _row(session, reaction_type="HER", title="A HER", year=2026)
            _row(session, property_type="reaction_barrier", title="B barrier", year=2025)
            expected, _paper = _row(session, title="C valid adsorption", year=2024)
            session.commit()

            payload = build_dft_ml_dataset_v3(session, task="adsorption_energy", limit=1)

            assert [record["record_id"] for record in payload["records"]] == [str(expected.id)]
            assert payload["manifest"]["task_candidate_count"] == 1
            assert payload["manifest"]["returned_count"] == 1
            assert payload["manifest"]["filters"]["limit"] == 1
    finally:
        engine.dispose()


def test_v3_ready_only_limit_is_applied_after_readiness_filtering():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            _row(session, complete=False, title="A blocked", year=2026)
            _row(session, title="B ready", year=2025)
            _row(session, title="C ready", year=2024)
            session.commit()

            payload = build_dft_ml_dataset_v3(
                session,
                task="adsorption_energy",
                ready_only=True,
                limit=1,
            )

            assert len(payload["records"]) == 1
            assert payload["records"][0]["tabular_ml_ready"] is True
            assert payload["manifest"]["task_candidate_count"] == 2
            assert payload["manifest"]["returned_count"] == 1
    finally:
        engine.dispose()


def test_v3_csv_defaults_to_training_ready_records_and_manifest_filter():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            blocked, _paper = _row(session, complete=False, title="A blocked", year=2026)
            ready, _paper = _row(session, title="B ready", year=2025)
            session.commit()

            csv_text, manifest = build_dft_ml_dataset_v3_csv(session, task="adsorption_energy")
            rows = list(csv.DictReader(io.StringIO(csv_text)))

            assert manifest["filters"]["ready_only"] is True
            assert manifest["returned_count"] == 1
            assert [row["record_id"] for row in rows] == [str(ready.id)]
            assert rows[0]["paper_id"]
            assert rows[0]["title"] == "B ready"
            assert rows[0]["year"] == "2025"
            assert rows[0]["catalyst_name"] == "Fe-N-C"
            assert rows[0]["catalyst_type"] == "single_atom"
            assert rows[0]["metal_centers"] == '["Fe"]'
            assert rows[0]["coordination"] == "Fe-N4"
            assert rows[0]["support"] == "carbon"
            assert rows[0]["reaction_type"] == "SRR_LiS"
            assert rows[0]["task_profile"] == "SRR_LiS:adsorption_energy"
            assert rows[0]["property_type"] == "adsorption_energy"
            assert rows[0]["normalized_property_type"] == "adsorption_energy"
            assert rows[0]["canonical_property_type"] == "adsorption_energy"
            assert rows[0]["property_subtype"] == "adsorption"
            assert rows[0]["normalized_value"] == "-1.23"
            assert rows[0]["normalized_unit"] == "eV"
            assert rows[0]["raw_value"] == "-1.23"
            assert rows[0]["raw_unit"] == "eV"
            assert rows[0]["adsorbate"] == "Li2S4"
            assert rows[0]["intermediate"] == "Li2S4"
            assert rows[0]["reaction_step"] == "Li2S4 adsorption"
            assert rows[0]["dft_software"] == "VASP"
            assert rows[0]["dft_functional"] == "PBE"
            assert rows[0]["evidence_text"] == "Li2S4 adsorption is -1.23 eV."
            assert rows[0]["page_locators"] == "[7]"
            assert rows[0]["label_ready"] == "true"
            assert rows[0]["tabular_ml_ready"] == "true"
            assert rows[0]["label_blockers"] == "[]"
            assert rows[0]["feature_blockers"] == "[]"
            assert rows[0]["split_paper_id"] == rows[0]["paper_id"]
            assert rows[0]["split_catalyst_family"]
            assert rows[0]["reaction_profile_version"] == "reaction_profiles_v1"
            assert rows[0]["task_profile_version"] == "tabular_task_profiles_v1"

            all_csv, all_manifest = build_dft_ml_dataset_v3_csv(
                session,
                task="adsorption_energy",
                ready_only=False,
            )
            all_rows = list(csv.DictReader(io.StringIO(all_csv)))
            assert all_manifest["filters"]["ready_only"] is False
            assert {row["record_id"] for row in all_rows} == {str(blocked.id), str(ready.id)}
            blocked_row = next(row for row in all_rows if row["record_id"] == str(blocked.id))
            assert blocked_row["tabular_ml_ready"] == "false"
            assert "missing_coordination" in blocked_row["feature_blockers"]
    finally:
        engine.dispose()


def test_v3_barrier_records_keep_specific_subtypes_in_json_and_csv():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            migration, _paper = _row(
                session,
                property_type="migration_barrier",
                title="A migration barrier",
                year=2026,
            )
            li2s, _paper = _row(
                session,
                property_type="li2s_decomposition_barrier",
                title="B Li2S decomposition barrier",
                year=2025,
            )
            session.commit()

            payload = build_dft_ml_dataset_v3(session, task="reaction_barrier", ready_only=False)
            csv_text, manifest = build_dft_ml_dataset_v3_csv(
                session,
                task="reaction_barrier",
                ready_only=False,
            )
            by_id = {record["record_id"]: record for record in payload["records"]}
            rows = {row["record_id"]: row for row in csv.DictReader(io.StringIO(csv_text))}

            assert payload["manifest"]["property_type_display_priority"][0] == "property_subtype"
            assert by_id[str(migration.id)]["target"]["canonical_property_type"] == "reaction_barrier"
            assert by_id[str(migration.id)]["target"]["normalized_property_type"] == "migration_barrier"
            assert by_id[str(migration.id)]["target"]["property_subtype"] == "migration_barrier"
            assert by_id[str(li2s.id)]["target"]["normalized_property_type"] == "li2s_decomposition_barrier"
            assert by_id[str(li2s.id)]["target"]["property_subtype"] == "li2s_decomposition_barrier"
            assert rows[str(migration.id)]["canonical_property_type"] == "reaction_barrier"
            assert rows[str(migration.id)]["property_subtype"] == "migration_barrier"
            assert rows[str(li2s.id)]["property_subtype"] == "li2s_decomposition_barrier"
            assert manifest["property_type_fields"][-1] == "property_subtype"
    finally:
        engine.dispose()


def test_v3_rds_gibbs_free_energy_task_only_keeps_rds_free_energy_records():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            rds, _paper = _row(
                session,
                property_type="gibbs_free_energy_change",
                title="A RDS Gibbs free energy",
                year=2026,
                reaction_step="RDS",
                evidence_text="The Gibbs free energy of the rate-determining step is 0.42 eV.",
                value=0.42,
            )
            _row(
                session,
                property_type="gibbs_free_energy_change",
                title="B overall SRR free energy",
                year=2025,
                reaction_step="S8 -> Li2S",
                evidence_text="The overall SRR from S8 to Li2S shows a Gibbs free energy change of -1.10 eV.",
                value=-1.10,
            )
            _row(
                session,
                property_type="gibbs_free_energy_change",
                title="B2 overall SRR RDS wording",
                year=2025,
                reaction_step="RDS of overall SRR",
                evidence_text="For the overall SRR from S8 to Li2S, the reported RDS Gibbs free energy is -0.80 eV.",
                value=-0.80,
            )
            _row(
                session,
                property_type="reaction_barrier",
                title="C activation barrier",
                year=2024,
                reaction_step="Li2S decomposition",
                evidence_text="The activation energy barrier is 0.55 eV.",
                value=0.55,
            )
            session.commit()

            payload = build_dft_ml_dataset_v3(session, task="rds_gibbs_free_energy", ready_only=False)
            csv_text, manifest = build_dft_ml_dataset_v3_csv(
                session,
                task="rds_gibbs_free_energy",
                ready_only=False,
            )
            rows = {row["record_id"]: row for row in csv.DictReader(io.StringIO(csv_text))}

            assert [record["record_id"] for record in payload["records"]] == [str(rds.id)]
            assert payload["records"][0]["target"]["canonical_property_type"] == "gibbs_free_energy_change"
            assert payload["records"][0]["target"]["property_subtype"] == "gibbs_free_energy_change"
            assert payload["records"][0]["target"]["reaction_step"] == "RDS"
            assert rows[str(rds.id)]["canonical_property_type"] == "gibbs_free_energy_change"
            assert rows[str(rds.id)]["property_subtype"] == "gibbs_free_energy_change"
            assert rows[str(rds.id)]["reaction_step"] == "RDS"
            assert manifest["task"] == "SRR_LiS:rds_gibbs_free_energy"
            assert manifest["excluded_counts"]["missing_rds_semantics"] == 1
            assert manifest["excluded_counts"]["overall_srr_free_energy_not_rds"] == 1
            assert manifest["excluded_counts"]["target_property_not_allowed"] == 1
    finally:
        engine.dispose()


def test_v3_reaction_barrier_task_does_not_include_rds_gibbs_free_energy():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            _row(
                session,
                property_type="gibbs_free_energy_change",
                reaction_step="RDS",
                evidence_text="The Gibbs free energy of the rate-determining step is 0.31 eV.",
                value=0.31,
            )
            barrier, _paper = _row(
                session,
                property_type="reaction_barrier",
                reaction_step="Li2S decomposition",
                evidence_text="The reaction barrier is 0.66 eV.",
                value=0.66,
            )
            session.commit()

            payload = build_dft_ml_dataset_v3(session, task="reaction_barrier", ready_only=False)

            assert [record["record_id"] for record in payload["records"]] == [str(barrier.id)]
            assert payload["manifest"]["excluded_counts"]["target_property_not_allowed"] == 1
    finally:
        engine.dispose()


def test_v3_rds_gibbs_free_energy_with_null_adsorbate_and_complete_catalyst_is_training_ready():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            rds, _paper = _row(
                session,
                property_type="gibbs_free_energy_change",
                reaction_step="rate-determining step of SRR",
                evidence_text="The Gibbs free energies corresponding to the rate-determining steps of the SRR are 0.42 eV.",
                value=0.42,
            )
            rds.adsorbate = None
            session.commit()

            payload = build_dft_ml_dataset_v3(session, task="rds_gibbs_free_energy", ready_only=False)
            assert [record["record_id"] for record in payload["records"]] == [str(rds.id)]
            record = payload["records"][0]
            assert record["label_ready"] is True
            assert record["tabular_ml_ready"] is True
            assert record["feature_blockers"] == []

            csv_text, manifest = build_dft_ml_dataset_v3_csv(session, task="rds_gibbs_free_energy")
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            assert manifest["returned_count"] == 1
            assert [row["record_id"] for row in rows] == [str(rds.id)]
    finally:
        engine.dispose()


def test_v3_adsorption_energy_with_null_adsorbate_stays_blocked():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            row, _paper = _row(session, property_type="adsorption_energy")
            row.adsorbate = None
            session.commit()

            payload = build_dft_ml_dataset_v3(session, task="adsorption_energy", ready_only=False)
            assert [record["record_id"] for record in payload["records"]] == [str(row.id)]
            record = payload["records"][0]
            assert record["label_ready"] is True
            assert record["tabular_ml_ready"] is False
            assert "missing_canonical_adsorbate" in record["feature_blockers"]
    finally:
        engine.dispose()


def test_v3_rejects_negative_limit_and_accepts_legacy_evidence_alias():
    engine, SessionLocal = _session()
    try:
        with SessionLocal() as session:
            _row(session, evidence_object_type="dft_results")
            session.commit()

            with pytest.raises(ValueError, match="non-negative integer"):
                build_dft_ml_dataset_v3(session, task="adsorption_energy", limit=-1)
            payload = build_dft_ml_dataset_v3(session, task="adsorption_energy")
            assert payload["records"][0]["provenance"]["page_locators"] == [7]
    finally:
        engine.dispose()
