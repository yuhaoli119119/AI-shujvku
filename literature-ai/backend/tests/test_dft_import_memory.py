from __future__ import annotations

import gc
import json
import os
import threading
import time
import tracemalloc
from collections import Counter
from time import perf_counter
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.services.dft_identity_service import build_dft_identity_v2
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.module_write_lock_service import ModuleWriteLockService


LARGE_IMPORT_SIZE = 370
EXISTING_DFT_SIZE = 7


def _complete_local_ai_audit(
    *,
    index: int,
    paper_id: str,
    compared_target_ids: list[str],
) -> dict[str, Any]:
    page = 5 + (index % 10)
    evidence_id = f"main:table:{index:03d}"
    source_record_id = f"00000000-0000-0000-0001-{index:012d}"
    # Each entry represents a distinct FeN4 catalyst environment. The scale
    # profile must exercise scientific identity, rather than make provenance
    # fields such as evidence_id or page number artificially unique.
    material = f"FeN4@graphene-vacancy-{index:03d}"
    adsorbate = ("S8", "Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S")[index % 6]
    property_type = (
        "adsorption_energy",
        "gibbs_free_energy_change",
        "zero_point_energy_correction",
        "entropy_correction_TS",
        "bader_charge_transfer",
        "bond_length",
    )[index % 6]
    value, unit = {
        "adsorption_energy": (round(-0.45 - 0.01 * (index % 31), 4), "eV"),
        "gibbs_free_energy_change": (round(-0.30 + 0.02 * (index % 21), 4), "eV"),
        "zero_point_energy_correction": (round(0.03 + 0.002 * (index % 16), 4), "eV"),
        "entropy_correction_TS": (round(0.12 + 0.003 * (index % 18), 4), "eV"),
        "bader_charge_transfer": (round(0.08 + 0.01 * (index % 15), 4), "e"),
        "bond_length": (round(2.16 + 0.01 * (index % 18), 4), "Å"),
    }[property_type]
    atom_pair = "Fe-S" if property_type == "bond_length" else None
    measurement_label = (
        f"{atom_pair} bond length" if atom_pair else property_type.replace("_", " ")
    )
    evidence_location = {
        "source_document_type": "main_text",
        "page": page,
        "table": f"Table {1 + index % 6}",
        "quoted_text": f"{material} {adsorbate} {measurement_label} {value} {unit}",
        "evidence_ids": [evidence_id],
        "bundle_fingerprint": "b" * 64,
        "figure_table_completed_snapshot_fingerprint": "s" * 64,
    }
    return {
        "paper_id": paper_id,
        "target_type": "dft_results",
        "target_id": "new",
        "temporary_id": f"new-dft-{index:03d}",
        "field_name": "dft_results",
        "decision": "new_candidate",
        "evidence_checked": True,
        "evidence_ids": [evidence_id],
        "corrected_value": {
            "material_identity": material,
            "property_type": property_type,
            "value": value,
            "unit": unit,
            "adsorbate": adsorbate,
            "reaction_step": f"SRR step {index % 5}",
            "source_document_type": "main_text",
            "source_page": page,
            "source_table": evidence_location["table"],
            **({"atom_pair": atom_pair} if atom_pair else {}),
        },
        "confidence": 0.95,
        "reason": "The structured table cell and source PDF page report this material, property, value, and unit.",
        "recommended_action": "create_unverified_dft_candidate",
        "dedupe_analysis": {
            "compared_target_ids": compared_target_ids,
            "conclusion": "distinct",
            "reason": "Compared with every terminal DFT target; material, property context, or value differs.",
        },
        "evidence_location": evidence_location,
        "source": "local_ai",
        "source_label": "large_import_memory_test",
        "agent_role": "local_ai_pdf_verifier",
        "requires_local_ai_verification": True,
        "required_evidence_checks": [
            {
                "evidence_id": evidence_id,
                "source_paper_id": paper_id,
                "source_paper_code": "MEM370",
                "source_record_id": source_record_id,
                "item_type": "table",
                "page": page,
                "source_document_type": "main_text",
            }
        ],
        "required_page_checks": [
            {
                "source_paper_id": paper_id,
                "source_paper_code": "MEM370",
                "page": page,
                "source_document_type": "main_text",
            }
        ],
        "local_ai_verification": {
            "verified_against_pdf": True,
            "used_tools": ["get_codex_item", "read_paper_page"],
            "checked_evidence_ids": [evidence_id],
            "checked_pages": [{"paper_id": paper_id, "page": page}],
            "verification_note": "Checked the evidence object, stored layout, and bundled source PDF page for material, property, value, and unit.",
        },
    }


def test_large_dft_import_memory_profile_is_b0102_scale(setup_test_db, monkeypatch):
    import_size = int(os.getenv("LITAI_DFT_MEMORY_TEST_SIZE", str(LARGE_IMPORT_SIZE)))
    trace_memory = os.getenv("LITAI_DFT_MEMORY_TRACEMALLOC", "0") == "1"
    query_counts: Counter[str] = Counter()
    query_fingerprints: Counter[str] = Counter()
    process_peak_rss = 0
    process_baseline_rss = 0
    process_peak_rss_by_stage: dict[str, int] = {}
    current_memory_stage = "setup"
    stop_sampling = threading.Event()

    def sample_process_memory():
        nonlocal process_peak_rss
        import psutil

        process = psutil.Process()
        while not stop_sampling.is_set():
            rss = process.memory_info().rss
            process_peak_rss = max(process_peak_rss, rss)
            process_peak_rss_by_stage[current_memory_stage] = max(
                process_peak_rss_by_stage.get(current_memory_stage, 0),
                rss,
            )
            stop_sampling.wait(0.02)

    sampler = threading.Thread(target=sample_process_memory, daemon=True)
    sampler.start()

    if os.getenv("LITAI_DFT_MEMORY_STAGE_PROFILE", "0") == "1":
        import psutil

        from app.services.verification_session_service import VerificationSessionService

        process = psutil.Process()

        def stage_payload(stage: str, started_at: float) -> str:
            return json.dumps(
                {
                    "stage": stage,
                    "elapsed_seconds": round(perf_counter() - started_at, 3),
                    "rss_mb": round(process.memory_info().rss / 1024 / 1024, 3),
                },
                sort_keys=True,
            )

        for method_name in (
            "_materialize_new_dft_candidates",
            "_paper_dft_audit_candidates",
            "_dft_settlement_counts",
        ):
            original = getattr(VerificationSessionService, method_name)

            def profiled(self, *args, __original=original, __name=method_name, **kwargs):
                nonlocal current_memory_stage
                current_memory_stage = __name
                stage_started = perf_counter()
                print("LARGE_DFT_IMPORT_STAGE=" + stage_payload(__name + ":before", stage_started), flush=True)
                result = __original(self, *args, **kwargs)
                print("LARGE_DFT_IMPORT_STAGE=" + stage_payload(__name + ":after", stage_started), flush=True)
                return result

            monkeypatch.setattr(VerificationSessionService, method_name, profiled)

        original_settle_row = VerificationSessionService._settle_dft_row_from_existing_audits
        settled_rows = 0
        settlement_started = perf_counter()

        def profiled_settle_row(self, *args, **kwargs):
            nonlocal settled_rows, current_memory_stage
            current_memory_stage = "settle_dft_rows"
            result = original_settle_row(self, *args, **kwargs)
            settled_rows += 1
            if settled_rows == 1 or settled_rows % 50 == 0:
                print(
                    "LARGE_DFT_IMPORT_STAGE="
                    + stage_payload(f"settled_rows:{settled_rows}", settlement_started),
                    flush=True,
                )
            return result

        monkeypatch.setattr(
            VerificationSessionService,
            "_settle_dft_row_from_existing_audits",
            profiled_settle_row,
        )

        original_readback = ExternalAnalysisService._dft_import_readback

        def profiled_readback(self, *args, **kwargs):
            nonlocal current_memory_stage
            current_memory_stage = "_dft_import_readback"
            stage_started = perf_counter()
            print("LARGE_DFT_IMPORT_STAGE=" + stage_payload("_dft_import_readback:before", stage_started), flush=True)
            result = original_readback(self, *args, **kwargs)
            print("LARGE_DFT_IMPORT_STAGE=" + stage_payload("_dft_import_readback:after", stage_started), flush=True)
            return result

        monkeypatch.setattr(ExternalAnalysisService, "_dft_import_readback", profiled_readback)

    def count_query(_conn, _cursor, statement, _parameters, _context, _executemany):
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "OTHER"
        query_counts[head] += 1
        query_fingerprints[" ".join(statement.split())[:240]] += 1

    event.listen(setup_test_db, "before_cursor_execute", count_query)
    try:
        with Session(setup_test_db, autoflush=False, expire_on_commit=False) as session:
            paper = Paper(title="370-candidate memory profile", paper_code="MEM370", pdf_path="memory.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            existing_rows = [
                DFTResult(
                    paper_id=paper.id,
                    property_type="adsorption_energy",
                    adsorbate=f"existing-{index}",
                    value=float(index),
                    unit="eV",
                    candidate_status="system_candidate",
                    evidence_payload={"material_identity": f"Existing-{index}", "page": 1},
                )
                for index in range(EXISTING_DFT_SIZE)
            ]
            session.add_all(existing_rows)
            session.flush()
            compared_target_ids = [str(row.id) for row in existing_rows]
            raw_payload = {
                "schema_version": "offline_dft_review_result_v1",
                "paper_id": str(paper.id),
                "paper_code": paper.paper_code,
                "bundle_fingerprint": "b" * 64,
                "figure_table_completed_snapshot_fingerprint": "s" * 64,
                "review_metadata": {
                    "schema_version": "offline_dft_review_result_v1",
                    "paper_code": paper.paper_code,
                    "bundle_fingerprint": "b" * 64,
                    "figure_table_completed_snapshot_fingerprint": "s" * 64,
                    "review_mode": "comprehensive_review",
                    "overall_status": "completed",
                    "review_source": {"review_source_type": "web_ai"},
                },
                "object_review_audits": [
                    _complete_local_ai_audit(
                        index=index,
                        paper_id=str(paper.id),
                        compared_target_ids=compared_target_ids,
                    )
                    for index in range(1, import_size + 1)
                ],
            }
            identities = [
                build_dft_identity_v2({"paper_id": str(paper.id), **audit})
                for audit in raw_payload["object_review_audits"]
            ]
            assert len(identities) == import_size
            assert all(not identity.error_codes for identity in identities)
            assert all(identity.dedupe_allowed for identity in identities)
            assert all(identity.observation_key for identity in identities)
            assert len({identity.subject_key for identity in identities}) == import_size
            assert len({identity.observation_key for identity in identities}) == import_size
            assert all(
                "missing_atom_pair_identity" not in identity.error_codes
                for identity in identities
            )
            settings = get_settings()
            service = ExternalAnalysisService(session, settings)
            monkeypatch.setattr(service, "_guard_dft_import_prerequisites", lambda _run, _candidates: None)

            gc.collect()
            import psutil

            process_baseline_rss = psutil.Process().memory_info().rss
            if trace_memory:
                tracemalloc.start()
            started = perf_counter()
            run = service.import_run(
                paper_id=paper.id,
                source="local_ai",
                source_label="large_import_memory_test",
                raw_text=None,
                raw_payload=raw_payload,
                source_identity="local_ai:large_import_memory_test",
                source_identity_verified=True,
            )
            current_after_import, peak_after_import = (
                tracemalloc.get_traced_memory() if trace_memory else (0, 0)
            )
            print(
                "LARGE_DFT_IMPORT_STAGE="
                + json.dumps(
                    {
                        "stage": "after_import_run",
                        "import_size": import_size,
                        "elapsed_seconds": round(perf_counter() - started, 3),
                        "identity_map_size": len(session.identity_map),
                        "query_counts": dict(query_counts),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            lock = ModuleWriteLockService(session).acquire(
                paper_id=paper.id,
                module_name="dft_results",
                locked_by="large_import_memory_test",
                ttl_minutes=30,
            )
            current_memory_stage = "apply_review_rules_for_run"
            summary = service.apply_review_rules_for_run(
                run.id,
                reviewer="large_import_memory_test",
                write_lock_tokens=[lock.lock_token],
                write_lock_owner="large_import_memory_test",
            )
            stop_sampling.set()
            sampler.join(timeout=2)
            current_after_apply, peak_after_apply = (
                tracemalloc.get_traced_memory() if trace_memory else (0, 0)
            )
            elapsed = perf_counter() - started
            if trace_memory:
                tracemalloc.stop()

            stored_count = session.scalar(
                select(func.count()).select_from(DFTResult).where(DFTResult.paper_id == paper.id)
            )
            candidate_count = session.scalar(
                select(func.count()).select_from(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == run.id)
            )
            metrics = {
                "import_size": import_size,
                "candidate_count": candidate_count,
                "materialized_count": summary["new_dft_candidates"]["materialized_count"],
                "stored_dft_count": stored_count,
                "elapsed_seconds": round(elapsed, 3),
                "python_current_after_import_mb": round(current_after_import / 1024 / 1024, 3),
                "python_peak_after_import_mb": round(peak_after_import / 1024 / 1024, 3),
                "python_current_after_apply_mb": round(current_after_apply / 1024 / 1024, 3),
                "python_peak_after_apply_mb": round(peak_after_apply / 1024 / 1024, 3),
                "process_peak_rss_mb": round(process_peak_rss / 1024 / 1024, 3),
                "process_baseline_rss_mb": round(process_baseline_rss / 1024 / 1024, 3),
                "process_peak_growth_mb": round(
                    (process_peak_rss - process_baseline_rss) / 1024 / 1024,
                    3,
                ),
                "process_peak_rss_by_stage_mb": {
                    key: round(value / 1024 / 1024, 3)
                    for key, value in sorted(process_peak_rss_by_stage.items())
                },
                "query_counts": dict(query_counts),
                "identity_map_size": len(session.identity_map),
                "summary_json_bytes": len(json.dumps(summary, ensure_ascii=False, default=str).encode("utf-8")),
                "summary_section_bytes": {
                    key: len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
                    for key, value in summary.items()
                },
                "top_query_fingerprints": query_fingerprints.most_common(12),
            }
            print("LARGE_DFT_IMPORT_METRICS=" + json.dumps(metrics, sort_keys=True))

            assert candidate_count == import_size
            assert summary["new_dft_candidates"]["materialized_count"] == import_size
            assert stored_count == EXISTING_DFT_SIZE + import_size
            assert len(summary["dft_readback"]["candidate_status"]) == import_size
            if import_size >= LARGE_IMPORT_SIZE:
                assert process_peak_rss - process_baseline_rss < 192 * 1024 * 1024
                assert query_counts["SELECT"] < import_size
                assert metrics["summary_json_bytes"] < 2 * 1024 * 1024
            session.rollback()
    finally:
        stop_sampling.set()
        sampler.join(timeout=2)
        event.remove(setup_test_db, "before_cursor_execute", count_query)


def test_large_dft_import_failure_rolls_back_all_materialized_rows(setup_test_db, monkeypatch):
    from app.services.dft_review_service import DFTResultReviewService

    with Session(setup_test_db, autoflush=False, expire_on_commit=False) as session:
        paper = Paper(title="DFT import atomicity profile", paper_code="MEM-ATOMIC", pdf_path="memory.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        existing_rows = [
            DFTResult(
                paper_id=paper.id,
                property_type="adsorption_energy",
                adsorbate=f"existing-{index}",
                value=float(index),
                unit="eV",
                candidate_status="system_candidate",
                evidence_payload={"material_identity": f"Existing-{index}", "page": 1},
            )
            for index in range(EXISTING_DFT_SIZE)
        ]
        session.add_all(existing_rows)
        session.commit()

        compared_target_ids = [str(row.id) for row in existing_rows]
        raw_payload = {
            "schema_version": "offline_dft_review_result_v1",
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "bundle_fingerprint": "b" * 64,
            "figure_table_completed_snapshot_fingerprint": "s" * 64,
            "object_review_audits": [
                _complete_local_ai_audit(
                    index=index,
                    paper_id=str(paper.id),
                    compared_target_ids=compared_target_ids,
                )
                for index in range(1, 31)
            ],
        }
        service = ExternalAnalysisService(session, get_settings())
        monkeypatch.setattr(service, "_guard_dft_import_prerequisites", lambda _run, _candidates: None)
        run = service.import_run(
            paper_id=paper.id,
            source="local_ai",
            source_label="large_import_atomicity_test",
            raw_text=None,
            raw_payload=raw_payload,
            source_identity="local_ai:large_import_atomicity_test",
            source_identity_verified=True,
        )
        lock = ModuleWriteLockService(session).acquire(
            paper_id=paper.id,
            module_name="dft_results",
            locked_by="large_import_atomicity_test",
            ttl_minutes=30,
        )

        original_apply = DFTResultReviewService.apply_imported_opinion
        applied_count = 0

        def fail_mid_batch(review_service, *args, **kwargs):
            nonlocal applied_count
            applied_count += 1
            if applied_count == 17:
                raise RuntimeError("injected_mid_batch_failure")
            return original_apply(review_service, *args, **kwargs)

        monkeypatch.setattr(DFTResultReviewService, "apply_imported_opinion", fail_mid_batch)
        with pytest.raises(RuntimeError, match="injected_mid_batch_failure"):
            service.apply_review_rules_for_run(
                run.id,
                reviewer="large_import_atomicity_test",
                write_lock_tokens=[lock.lock_token],
                write_lock_owner="large_import_atomicity_test",
            )
        session.rollback()

        assert session.scalar(
            select(func.count()).select_from(DFTResult).where(DFTResult.paper_id == paper.id)
        ) == EXISTING_DFT_SIZE
        assert session.scalar(
            select(func.count()).select_from(ExternalAnalysisRun).where(ExternalAnalysisRun.paper_id == paper.id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id)
        ) == 0
