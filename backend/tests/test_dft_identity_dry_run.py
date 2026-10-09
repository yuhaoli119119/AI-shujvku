from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models import (
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperRelationship,
)
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_identity_dry_run_service import (
    B0102_EXPECTED,
    DFTIdentityDryRunError,
    DFTIdentityDryRunService,
    canonical_sha256,
)
from scripts.dft_identity_dry_run import (
    assert_safe_temporary_database,
    atomic_write_manifest,
    build_parser,
)


def _identity_payload(
    *,
    material: str,
    value: float,
    property_type: str = "adsorption_energy",
    adsorbate: str = "Li2S4",
    bond_pair: str | None = None,
) -> dict:
    corrected = {
        "material_identity": material,
        "property_type": property_type,
        "adsorbate": adsorbate,
        "value": value,
        "unit": "Å" if property_type == "bond_length" else "eV",
    }
    if bond_pair:
        corrected["bond_pair"] = bond_pair
    return {
        "decision": "new_candidate",
        "target_type": "dft_results",
        "target_id": "new",
        "corrected_value": corrected,
        "evidence_location": {
            "source_document_type": "supplementary_information",
            "page": 18,
            "table": "Table S3",
            "row": material,
        },
    }


def _result(
    session: Session,
    paper: Paper,
    *,
    material: str,
    value: float,
    status: str = "ai_verified_ml_ready",
    property_type: str = "adsorption_energy",
    adsorbate: str = "Li2S4",
    bond_pair: str | None = None,
    row_id: UUID | None = None,
    store_identity: bool = True,
) -> DFTResult:
    payload = _identity_payload(
        material=material,
        value=value,
        property_type=property_type,
        adsorbate=adsorbate,
        bond_pair=bond_pair,
    )
    corrected = payload["corrected_value"]
    row = DFTResult(
        id=row_id or uuid4(),
        paper_id=paper.id,
        adsorbate=adsorbate,
        property_type=property_type,
        value=value,
        value_kind="point",
        unit=corrected["unit"],
        evidence_text="PDF evidence",
        evidence_payload={
            "material_identity": material,
            "corrected_value": corrected,
            "page": 18,
        },
        candidate_status=status,
        candidate_identity=uuid4().hex,
    )
    if store_identity:
        identity = DFTAuditIssueLifecycleService.build_identity(
            paper_id=paper.id,
            payload=payload,
        )
        DFTAuditIssueLifecycleService.apply_result_identity(row, identity)
    session.add(row)
    session.flush()
    return row


def _candidate(
    session: Session,
    run: ExternalAnalysisRun,
    paper: Paper,
    result: DFTResult,
    *,
    material: str,
    value: float,
    property_type: str = "adsorption_energy",
    adsorbate: str = "Li2S4",
    bond_pair: str | None = None,
    candidate_id: UUID | None = None,
) -> ExternalAnalysisCandidate:
    payload = _identity_payload(
        material=material,
        value=value,
        property_type=property_type,
        adsorbate=adsorbate,
        bond_pair=bond_pair,
    )
    row = ExternalAnalysisCandidate(
        id=candidate_id or uuid4(),
        run_id=run.id,
        paper_id=paper.id,
        candidate_type="object_review_audit",
        normalized_payload=payload,
        evidence_payload={"decision": "new_candidate", "page": 18},
        status="ai_applied",
        materialized_target_type="dft_results",
        materialized_target_id=str(result.id),
    )
    session.add(row)
    session.flush()
    return row


def _missing_issue(
    session: Session,
    paper: Paper,
    candidates: list[ExternalAnalysisCandidate],
    *,
    issue_id: UUID | None = None,
) -> DFTAuditIssue:
    row = DFTAuditIssue(
        id=issue_id or uuid4(),
        paper_id=paper.id,
        target_type="dft_results",
        target_id="new",
        issue_type="missing_dft_result",
        severity="high",
        status="needs_primary_ai",
        source_candidate_ids=[str(candidate.id) for candidate in candidates],
        fingerprint=uuid4().hex,
    )
    session.add(row)
    session.flush()
    return row


def test_read_only_database_rejects_flush_and_service_detects_orm_dirty(setup_test_db):
    with setup_test_db.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(connection, autoflush=False) as session:
            session.add(Paper(title="must not flush", pdf_path="x.pdf"))
            with pytest.raises(DFTIdentityDryRunError, match="orm_write_state_detected"):
                DFTIdentityDryRunService(session)._assert_clean_session("test")
            with pytest.raises(DBAPIError):
                session.flush()
        if transaction.is_active:
            transaction.rollback()


def test_database_fingerprint_is_stable_and_covers_all_public_tables(setup_test_db):
    with setup_test_db.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(connection, autoflush=False) as session:
            service = DFTIdentityDryRunService(session)
            before = service.database_data_fingerprint()
            after = service.database_data_fingerprint()
            assert before == after
            public_count = connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
            )
            assert before["table_count"] == public_count
        transaction.rollback()


def test_global_exact_conflict_and_invalid_identity_classification(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(title="classification", paper_code="B9001", pdf_path="x.pdf")
        session.add(paper)
        session.flush()
        _result(session, paper, material="Fe-GDY", value=-1.2, store_identity=False)
        _result(session, paper, material="Fe-GDY", value=-1.2, store_identity=False)
        _result(session, paper, material="Fe-GDY", value=-1.3, store_identity=False)
        invalid = DFTResult(
            paper_id=paper.id,
            property_type="bond_length",
            value=2.1,
            unit="Å",
            candidate_status="system_candidate",
            candidate_identity=uuid4().hex,
            evidence_payload={"material_identity": "Fe-GDY"},
        )
        session.add(invalid)
        session.flush()
        _, _, analysis = DFTIdentityDryRunService(session)._global_result_identity_analysis()
        assert analysis["exact_duplicate_group_count"] == 1
        assert analysis["scientific_conflict_group_count"] == 1
        assert analysis["invalid_identity_row_count"] == 1
        paper_group = analysis["papers_with_classifications"][0]
        assert paper_group["invalid_identity_rows"][0]["result_id"] == str(invalid.id)


def test_source_relation_priority_legacy_fallback_and_result_id_authority():
    relation_id = uuid4()
    legacy_id = uuid4()
    issue = SimpleNamespace(source_candidate_ids=[str(legacy_id)])
    relation = DFTIdentityDryRunService._source_resolution(issue, {relation_id})
    assert relation["authority"] == "source_relation"
    assert relation["effective_candidate_ids"] == [str(relation_id)]
    assert relation["consistency"] == "mismatch"
    fallback = DFTIdentityDryRunService._source_resolution(issue, set())
    assert fallback["authority"] == "legacy_source_candidate_ids"
    assert fallback["effective_candidate_ids"] == [str(legacy_id)]

    result_id = uuid4()
    result_issue = SimpleNamespace(result_id=result_id, target_id=str(uuid4()), target_type="dft_results")
    resolution = DFTIdentityDryRunService._result_resolution(result_issue)
    assert resolution["authority"] == "result_id"
    assert resolution["effective_result_id"] == str(result_id)
    assert resolution["consistency"] == "mismatch"
    legacy_issue = SimpleNamespace(result_id=None, target_id=str(legacy_id), target_type="dft_results")
    assert DFTIdentityDryRunService._result_resolution(legacy_issue)["effective_result_id"] == str(legacy_id)


def test_pdf_inventory_is_stable_and_missing_or_ambiguous_fails(setup_test_db, tmp_path):
    data_root = tmp_path / "data"
    main_path = data_root / "storage" / "pdf" / "main.pdf"
    si_path = data_root / "storage" / "pdf" / "si.pdf"
    main_path.parent.mkdir(parents=True)
    main_path.write_bytes(b"main-pdf")
    si_path.write_bytes(b"si-pdf")
    with Session(setup_test_db) as session:
        main = Paper(paper_code="B0102", title="main", pdf_path="storage/pdf/main.pdf")
        si = Paper(paper_code="S0102", title="si", pdf_path="storage/pdf/si.pdf")
        session.add_all([main, si])
        session.flush()
        session.add(
            PaperRelationship(
                source_paper_id=main.id,
                target_paper_id=si.id,
                relationship_type="supplementary",
            )
        )
        session.flush()
        service = DFTIdentityDryRunService(session)
        first = service.pdf_snapshot(paper_code="B0102", data_root=data_root)
        second = service.pdf_snapshot(paper_code="B0102", data_root=data_root)
        assert first == second
        assert [row["paper_code"] for row in first["files"]] == ["B0102", "S0102"]

        si_path.unlink()
        with pytest.raises(FileNotFoundError):
            service.pdf_snapshot(paper_code="B0102", data_root=data_root)
        si_path.write_bytes(b"si-pdf")
        si.pdf_path = "storage/pdf/main.pdf"
        session.flush()
        with pytest.raises(DFTIdentityDryRunError, match="ambiguous_duplicate_pdf_path"):
            service.pdf_snapshot(paper_code="B0102", data_root=data_root)


def test_b0102_fixture_classifies_366_safe_and_two_identity_splits(setup_test_db, monkeypatch):
    monkeypatch.setattr(
        "app.services.dft_identity_dry_run_service.bulk_export_gate_results",
        lambda _session, rows, target_type: {
            str(row.id): SimpleNamespace(eligible=True, reasons=()) for row in rows
        },
    )
    with Session(setup_test_db) as session:
        paper = Paper(title="B0102 fixture", paper_code="B0102", pdf_path="main.pdf")
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="pytest")
        session.add(run)
        session.flush()

        for index in range(366):
            material = f"safe-material-{index:03d}"
            result = _result(session, paper, material=material, value=float(index + 1))
            candidate = _candidate(
                session,
                run,
                paper,
                result,
                material=material,
                value=float(index + 1),
            )
            _missing_issue(session, paper, [candidate])

        split_specs = (
            (
                UUID("23fdcb8b-31d9-4b78-9d3a-a374be9a3050"),
                UUID("fe4f3e50-5aa1-4e67-b99f-80c09158200c"),
                UUID("3111d2e3-2f30-4c25-a94c-8c347db6f58f"),
                UUID("1095771c-43b5-4da9-a7e1-e09b09d4ef71"),
                "Graphene@Li2S",
                2.11,
            ),
            (
                UUID("c3eeb0be-d023-4ae7-9de7-18ada01e81d2"),
                UUID("90e5f450-6900-4b1f-86a0-3e7579e1da36"),
                UUID("8002c4b1-22b3-42bf-a958-e32a53dd3bc4"),
                UUID("b63df0cb-7e34-4bab-a6cc-9aca3e23e151"),
                "FeN4-G@Li2S",
                2.18,
            ),
        )
        for issue_id, result_id, li1_id, li2_id, material, value in split_specs:
            result = _result(
                session,
                paper,
                material=material,
                value=value,
                property_type="bond_length",
                adsorbate="Li2S",
                bond_pair="Li1-S",
                row_id=result_id,
            )
            li1 = _candidate(
                session,
                run,
                paper,
                result,
                material=material,
                value=value,
                property_type="bond_length",
                adsorbate="Li2S",
                bond_pair="Li1-S",
                candidate_id=li1_id,
            )
            li2 = _candidate(
                session,
                run,
                paper,
                result,
                material=material,
                value=value,
                property_type="bond_length",
                adsorbate="Li2S",
                bond_pair="Li2-S",
                candidate_id=li2_id,
            )
            _missing_issue(session, paper, [li1, li2], issue_id=issue_id)

        for index in range(5):
            _result(session, paper, material=f"extra-{index}", value=-100.0 - index)
        for index in range(2):
            _result(
                session,
                paper,
                material=f"rejected-{index}",
                value=-200.0 - index,
                status="Rejected",
            )
        session.flush()

        service = DFTIdentityDryRunService(session)
        results, identities, _analysis = service._global_result_identity_analysis()
        reconciliation = service._paper_reconciliation(
            paper_code="B0102",
            results=results,
            result_identities=identities,
            candidate_analysis={},
        )
        assert reconciliation["actual"] == B0102_EXPECTED
        assert len(reconciliation["safe_single_targets"]) == 366
        assert len(reconciliation["identity_split_parent_issues"]) == 2
        for split in reconciliation["identity_split_parent_issues"]:
            atom_pairs = {
                candidate["identity"]["canonical_atom_pair"]
                for candidate in split["candidates"]
            }
            assert atom_pairs == {"li1-s", "li2-s"}
            assert split["li2_result_missing"] is True


def test_manifest_hash_atomic_write_and_cli_has_no_apply(tmp_path):
    payload = {"z": 1, "a": [2, 3]}
    assert canonical_sha256(payload) == canonical_sha256({"a": [2, 3], "z": 1})
    path = tmp_path / "manifest.json"
    manifest = {"canonical_payload": payload, "canonical_sha256": canonical_sha256(payload)}
    atomic_write_manifest(path, manifest)
    first = path.read_bytes()
    atomic_write_manifest(path, manifest)
    assert path.read_bytes() == first

    parser = build_parser()
    assert all(action.dest != "apply" for action in parser._actions)
    with pytest.raises(SystemExit):
        parser.parse_args([])
    assert_safe_temporary_database(
        "postgresql+psycopg://postgres@127.0.0.1:55439/litai_p1b2",
        "litai_p1b2",
    )
    with pytest.raises(ValueError, match="loopback"):
        assert_safe_temporary_database(
            "postgresql+psycopg://postgres@example.com/literature_ai",
            "literature_ai",
        )
