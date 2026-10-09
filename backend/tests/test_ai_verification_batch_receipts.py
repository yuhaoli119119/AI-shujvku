from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from uuid import UUID
from zipfile import ZipFile

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.services.ai_verification_batch_receipt_service as receipt_module
from app.db.models import AIVerificationBatchReceipt, CatalystSample, DFTResult, EvidenceLocator, ExtractionFieldReview, Paper, PaperSection, PaperTable
from app.main import app
from app.services.ai_verification_batch_receipt_service import AIVerificationBatchReceiptService
from app.services.ai_verification_service import AIVerificationService, AuthenticatedAIVerificationIdentity
from app.services.dft_review_bundle_service import DFTReviewBundleService
from app.utils.ai_verification import AI_VERIFICATION_CAPABILITY, ai_target_fingerprint, build_structured_table_cell_evidence
from test_dft_review_bundle import _mark_figure_table_review_completed, _seed_review_materials


def _identity() -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:receipt-test", source_label="receipt-test", model_agent="receipt-test-agent",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}), identity_verified=True,
    )


def _rows(engine, tmp_path) -> tuple[UUID, list[UUID]]:
    pdf_path = tmp_path / "receipt-source.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(20, 40, 570, 200),
        "Fe-N4 single atom N4 Li2S4 adsorption energy -1.2 eV. "
        "Fe-N4 single atom N4 Li2S4 adsorption energy -2.2 eV. "
        "Fe-N4 single atom N4 Li2S4 adsorption energy -3.2 eV. "
        "Counter evidence: the reported row is not the Fe-N4 structure.",
        fontsize=9,
    )
    document.save(pdf_path)
    document.close()
    with Session(engine) as session:
        paper = Paper(title="Receipt isolation paper", authors=["Tester"], pdf_path=str(pdf_path))
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom", metal_centers=["Fe"], coordination="N4")
        session.add(catalyst)
        session.flush()
        rows = [DFTResult(paper_id=paper.id, catalyst_sample_id=catalyst.id, property_type="adsorption_energy", adsorbate="Li2S4", value=-1.2-index, unit="eV", evidence_payload={"material_identity": "Fe-N4"}) for index in range(3)]
        session.add_all(rows)
        session.commit()
        return paper.id, [row.id for row in rows]


def _submission(row: DFTResult, decision: str = "accept") -> dict:
    payload = {"target_type": "dft_results", "target_id": str(row.id), "field_name": "value", "decision": decision, "confidence": 0.99, "page": 1, "evidence_text": f"Fe-N4 single atom N4 Li2S4 adsorption energy {row.value:.1f} eV", "expected_target_fingerprint": ai_target_fingerprint("dft_results", row)}
    if decision == "defer":
        payload["blocked_reasons"] = ["source_unit_not_stated"]
    if decision == "reject":
        payload["evidence_text"] = "Counter evidence: the reported row is not the Fe-N4 structure."
        payload["counter_evidence_text"] = payload["evidence_text"]
        payload["reasoning_summary"] = "The source explicitly excludes the claimed structure."
    return payload


def _apply(session: Session, paper_id: UUID, request_id: str, submissions: list[dict]):
    return AIVerificationBatchReceiptService(session, AIVerificationService(session)).apply(paper_id=paper_id, request_id=request_id, submissions=submissions, identity=_identity())


def test_direct_accept_defer_reject_commit_and_new_session_readback(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        rows = [session.get(DFTResult, row_id) for row_id in row_ids]
        receipt, replayed = _apply(session, paper_id, "mixed-001", [_submission(rows[0]), _submission(rows[1], "defer"), _submission(rows[2], "reject")])
        session.commit()
        assert replayed is False and receipt["items"][0]["outcome"] in {"auto_verified", "auto_repaired"}
        assert receipt["items"][1]["status"] == "ai_blocked" and receipt["items"][2]["status"] == "rejected"
        assert rows[0].value == pytest.approx(-1.2)
    with Session(setup_test_db) as session:
        confirmed = AIVerificationBatchReceiptService(session).get(paper_id=paper_id, request_id="mixed-001", identity=_identity())
        session.commit()
        assert confirmed["current_readback"]["status"] == "confirmed"


def test_readback_failure_recovers_on_query_without_duplicate_write(setup_test_db, tmp_path, monkeypatch):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        _apply(session, paper_id, "recover-001", [_submission(session.get(DFTResult, row_ids[0]))])
        session.commit()
    original = receipt_module.get_ai_target
    monkeypatch.setattr(receipt_module, "get_ai_target", lambda **_kwargs: (_ for _ in ()).throw(LookupError("temporary")))
    with Session(setup_test_db) as session:
        failed = AIVerificationBatchReceiptService(session).get(paper_id=paper_id, request_id="recover-001", identity=_identity())
        session.commit()
        assert failed["current_readback"]["status"] == "not_confirmed"
    monkeypatch.setattr(receipt_module, "get_ai_target", original)
    with Session(setup_test_db) as session:
        replay, replayed = _apply(session, paper_id, "recover-001", [_submission(session.get(DFTResult, row_ids[0]))])
        recovered = AIVerificationBatchReceiptService(session).get(paper_id=paper_id, request_id="recover-001", identity=_identity())
        session.commit()
        assert replayed is True and replay["schema_version"] == "ai_verification_batch_receipt.v2"
        assert "submission_items" not in replay and "readback" not in replay
        assert recovered["current_readback"]["status"] == "confirmed"
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 1


def test_terminal_and_stale_requests_cannot_overwrite_existing_review(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        _apply(session, paper_id, "terminal-001", [_submission(row, "defer")])
        session.commit()
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        result, _ = _apply(session, paper_id, "terminal-002", [{**_submission(row, "reject"), "expected_target_fingerprint": "stale" * 16}])
        session.commit()
        review = AIVerificationService(session)._find_review(paper_id, "dft_results", str(row.id), "value")
        assert result["items"][0]["outcome"] == "skipped_terminal"
        assert review is not None and review.reviewer_status == "ai_blocked"


def test_stale_fingerprint_or_review_version_only_returns_conflict_without_a_write(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        pending = ExtractionFieldReview(
            paper_id=paper_id, target_type="dft_results", target_id=str(row.id), field_name="value",
            reviewer_status="pending", write_version=3,
        )
        session.add(pending)
        session.flush()
        original_version = int(pending.write_version)
        fingerprint_conflict, _ = _apply(session, paper_id, "conflict-fingerprint", [{
            **_submission(row), "expected_target_fingerprint": "0" * 64, "expected_write_version": original_version,
        }])
        version_conflict, _ = _apply(session, paper_id, "conflict-version", [{
            **_submission(row), "expected_write_version": original_version + 1,
        }])
        session.commit()
        current = AIVerificationService(session)._find_review(paper_id, "dft_results", str(row.id), "value")
        assert fingerprint_conflict["items"][0]["outcome"] == "write_conflict"
        assert version_conflict["items"][0]["outcome"] == "write_conflict"
        assert current is not None and current.reviewer_status == "pending" and current.write_version == original_version


def test_non_dft_and_format_errors_do_not_block_valid_dft_item(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        section = PaperSection(paper_id=paper_id, text="Not DFT", section_type="body")
        session.add(section)
        session.flush()
        valid = session.get(DFTResult, row_ids[0])
        receipt, _ = _apply(session, paper_id, "scope-001", [{**_submission(valid), "target_type": "sections", "target_id": str(section.id), "field_name": "text"}, {"target_type": "dft_results", "target_id": str(row_ids[1]), "decision": "accept"}, _submission(valid)])
        session.commit()
        assert receipt["items"][0]["outcome"] == "invalid_target_type"
        assert receipt["items"][1]["outcome"] == "format_error"
        assert receipt["items"][2]["outcome"] in {"auto_verified", "auto_repaired"}


def test_reject_requires_counter_evidence_and_missing_review_is_not_confirmed(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        no_write, _ = _apply(session, paper_id, "reject-no-proof", [{**_submission(row, "reject"), "counter_evidence_text": "invented counter evidence"}])
        proof = _submission(row, "reject")
        proof["counter_evidence_text"] = "Counter evidence:"
        accepted, _ = _apply(session, paper_id, "reject-proof", [proof])
        session.commit()
        assert no_write["items"][0]["outcome"] == "reject_evidence_required"
        assert accepted["items"][0]["status"] == "rejected", accepted["items"][0]
        session.delete(AIVerificationService(session)._find_review(paper_id, "dft_results", str(row.id), "value"))
        session.commit()
    with Session(setup_test_db) as session:
        receipt = AIVerificationBatchReceiptService(session).get(paper_id=paper_id, request_id="reject-proof", identity=_identity())
        session.commit()
        assert receipt["current_readback"]["status"] == "not_confirmed"
        assert receipt["current_readback"]["items"][0]["review_status"] == "no_review_row"


def test_accept_gate_failure_is_repairable_no_write_then_corrected_retry(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        failed, _ = _apply(session, paper_id, "accept-repair-001", [{**_submission(row), "page": 99}])
        session.commit()
        item = failed["items"][0]
        assert item["outcome"] == "accept_validation_failed"
        assert item["status"] == "pending" and item["database_writes"] is False
        assert "page_valid" in item["blocked_reasons"]
        assert item["record_id"] == str(row.id) and item["requested_decision"] == "accept"
        assert item["evidence_location"]["page"] == 99
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 0
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        corrected, _ = _apply(session, paper_id, "accept-repair-002", [_submission(row)])
        session.commit()
        assert corrected["items"][0]["outcome"] in {"auto_verified", "auto_repaired"}
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 1


def test_accept_source_id_conflict_is_repairable_zero_write(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        result, _ = _apply(session, paper_id, "accept-source-conflict-001", [{
            **_submission(row),
            "source_paper_id": str(paper_id),
            "evidence_paper_id": "00000000-0000-0000-0000-000000000001",
        }])
        session.commit()
        item = result["items"][0]
        assert item["outcome"] == "accept_validation_failed"
        assert item["database_writes"] is False
        assert item["blocked_reasons"] == ["conflicting_evidence_paper_ids"]
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 0


def test_task_and_apply_share_authority_for_legacy_human_and_ai_terminal_reviews(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        # A legacy label without current authority is still actionable.
        legacy = ExtractionFieldReview(
            paper_id=paper_id, target_type="dft_results", target_id=str(row.id), field_name="value",
            reviewer_status="verified", target_resolution_status="active", write_version=1,
        )
        session.add(legacy)
        session.flush()
        task_review, task_status = AIVerificationService(session)._select_dft_field_review(
            [legacy], paper_id, row, "value", "scope",
        )
        assert task_review is legacy and task_status == "pending"
        applied, _ = _apply(session, paper_id, "legacy-retry-001", [{**_submission(row), "expected_write_version": 1}])
        session.commit()
        assert applied["items"][0]["outcome"] in {"auto_verified", "auto_repaired"}
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        ai = AIVerificationService(session)._find_review(paper_id, "dft_results", str(row.id), "value")
        task_review, task_status = AIVerificationService(session)._select_dft_field_review(
            [ai], paper_id, row, "value", "scope",
        )
        assert task_review is ai and task_status == "ai_verified"
        skipped, _ = _apply(session, paper_id, "ai-terminal-001", [_submission(row)])
        session.commit()
        assert skipped["items"][0]["outcome"] == "skipped_terminal"
    with Session(setup_test_db) as session:
        human_row = session.get(DFTResult, row_ids[1])
        evidence = _submission(human_row)["evidence_text"]
        human = ExtractionFieldReview(
            paper_id=paper_id, target_type="dft_results", target_id=str(human_row.id), field_name="value",
            reviewer_status="verified", target_resolution_status="active", reviewer="human_verifier",
            target_fingerprint=ai_target_fingerprint("dft_results", human_row), evidence_text=evidence,
            review_payload={"human_verification": {
                "verification_actor_type": "human", "identity_verified": True,
                "writes_final_truth": True, "decision": "verified", "reviewer": "human_verifier",
            }},
        )
        session.add_all([human, EvidenceLocator(
            paper_id=paper_id, target_type="dft_results", target_id=str(human_row.id), field_name="value",
            evidence_text=evidence, page=1, locator_status="exact_page",
        )])
        session.flush()
        task_review, task_status = AIVerificationService(session)._select_dft_field_review(
            [human], paper_id, human_row, "value", "scope",
        )
        assert task_review is human and task_status == "verified"
        preserved, _ = _apply(session, paper_id, "human-terminal-001", [_submission(human_row)])
        session.commit()
        assert preserved["items"][0]["outcome"] == "skipped_terminal"


def test_task_and_apply_share_current_explicit_rejection_contract(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        _apply(session, paper_id, "explicit-reject-001", [_submission(row, "reject")])
        session.commit()
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        service = AIVerificationService(session)
        review = service._find_review(paper_id, "dft_results", str(row.id), "value")
        assert service._select_dft_field_review([review], paper_id, row, "value", "scope") == (review, "rejected")
        applied, _ = _apply(session, paper_id, "explicit-reject-002", [_submission(row)])
        session.commit()
        assert applied["items"][0]["outcome"] == "skipped_terminal"

    with Session(setup_test_db) as session:
        legacy_row = session.get(DFTResult, row_ids[1])
        legacy = ExtractionFieldReview(
            paper_id=paper_id, target_type="dft_results", target_id=str(legacy_row.id), field_name="value",
            reviewer_status="rejected", target_resolution_status="active", write_version=1,
            review_payload={"ai_verification": {"decision": "rejected", "outcome": "auto_rejected"}},
        )
        session.add(legacy)
        session.flush()
        service = AIVerificationService(session)
        assert service._select_dft_field_review([legacy], paper_id, legacy_row, "value", "scope") == (legacy, "pending")
        retried, _ = _apply(session, paper_id, "legacy-reject-retry-001", [{
            **_submission(legacy_row), "expected_write_version": 1,
        }])
        session.commit()
        assert retried["items"][0]["outcome"] in {"auto_verified", "auto_repaired"}

    with Session(setup_test_db) as session:
        stale_row = session.get(DFTResult, row_ids[2])
        _apply(session, paper_id, "stale-reject-001", [_submission(stale_row, "reject")])
        session.commit()
        stale_row.value = -9.9
        session.commit()
        service = AIVerificationService(session)
        stale_review = service._find_review(paper_id, "dft_results", str(stale_row.id), "value")
        assert service._select_dft_field_review([stale_review], paper_id, stale_row, "value", "scope") == (stale_review, "pending")
        current_attempt, _ = _apply(session, paper_id, "stale-reject-002", [{
            **_submission(stale_row), "expected_write_version": stale_review.write_version,
        }])
        session.commit()
        assert current_attempt["items"][0]["outcome"] == "accept_validation_failed"


def test_v1_receipt_remains_queryable_without_rewriting_submission_items(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        v1_items = [{
            "item_index": 0, "target_type": "dft_results", "target_id": str(row.id),
            "field_name": "value", "outcome": "exception", "status": "needs_human",
            "database_writes": False,
        }]
        session.add(AIVerificationBatchReceipt(
            paper_id=paper_id, source_identity=_identity().source_identity, request_id="legacy-v1-001",
            request_fingerprint="0" * 64, status="committed",
            receipt_payload={"schema_version": "ai_verification_batch_receipt.v1", "items": v1_items,
                             "submission_items": v1_items, "counts": {"submitted": 1}},
        ))
        session.commit()
    with Session(setup_test_db) as session:
        receipt = AIVerificationBatchReceiptService(session).get(
            paper_id=paper_id, request_id="legacy-v1-001", identity=_identity(),
        )
        session.commit()
        assert receipt["schema_version"] == "ai_verification_batch_receipt.v1"
        assert receipt["submission_items"] == receipt["items"]
        assert receipt["current_readback"]["items"][0]["record_id"] == str(row_ids[0])


def test_concurrent_identical_request_has_one_receipt_and_one_review(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    def worker() -> bool:
        with Session(setup_test_db) as session:
            _receipt, replayed = _apply(session, paper_id, "race-001", [_submission(session.get(DFTResult, row_ids[0]))])
            session.commit()
            return replayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _unused: worker(), range(2)))
    assert sorted(outcomes) == [False, True]
    with Session(setup_test_db) as session:
        assert session.scalar(select(func.count(AIVerificationBatchReceipt.id))) == 1
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 1


def test_duplicate_field_in_one_request_is_rejected_before_a_second_write(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_ids[0])
        receipt, replayed = _apply(session, paper_id, "duplicate-field-001", [
            _submission(row, "accept"),
            _submission(row, "defer"),
        ])
        session.commit()
        review = AIVerificationService(session)._find_review(paper_id, "dft_results", str(row.id), "value")
        assert replayed is False
        assert receipt["items"][0]["status"] == "ai_verified"
        assert receipt["items"][1]["outcome"] == "duplicate_field_in_request"
        assert receipt["items"][1]["database_writes"] is False
        assert review is not None and review.reviewer_status == "ai_verified" and review.write_version == 2


def test_distinct_request_ids_concurrently_preserve_the_first_terminal_review(setup_test_db, tmp_path):
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    row_id = row_ids[0]
    from threading import Barrier

    barrier = Barrier(2)

    def worker(request_id: str) -> dict:
        with Session(setup_test_db) as session:
            row = session.get(DFTResult, row_id)
            barrier.wait(timeout=10)
            receipt, _ = _apply(session, paper_id, request_id, [_submission(row)])
            session.commit()
            return receipt["items"][0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, ["field-race-001", "field-race-002"]))
    with Session(setup_test_db) as session:
        review = AIVerificationService(session)._find_review(paper_id, "dft_results", str(row_id), "value")
        assert session.scalar(select(func.count(AIVerificationBatchReceipt.id))) == 2
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 1
        assert review is not None and review.reviewer_status == "ai_verified" and review.write_version == 2
    assert {item["outcome"] for item in outcomes} <= {"auto_verified", "auto_repaired", "skipped_terminal"}
    assert any(item["outcome"] in {"auto_verified", "auto_repaired"} for item in outcomes)
    assert any(item["outcome"] == "skipped_terminal" for item in outcomes)


def test_opposite_order_multi_field_requests_complete_without_deadlock_or_overwrite(setup_test_db, tmp_path):
    """PostgreSQL locks must be acquired in a fixed order, not input order."""
    paper_id, row_ids = _rows(setup_test_db, tmp_path)
    from threading import Barrier

    barrier = Barrier(2)

    def worker(request_id: str, reversed_order: bool) -> list[dict]:
        with Session(setup_test_db) as session:
            rows = [session.get(DFTResult, row_id) for row_id in row_ids[:2]]
            submissions = [_submission(row) for row in rows]
            if reversed_order:
                submissions.reverse()
                # UUID spelling must also map to the same deterministic locks
                # and the same review identities as canonical UUID strings.
                for submission in submissions:
                    submission["target_id"] = "{" + submission["target_id"].upper() + "}"
            barrier.wait(timeout=10)
            receipt, replayed = _apply(session, paper_id, request_id, submissions)
            session.commit()
            assert replayed is False
            return receipt["items"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "opposite-order-001", False)
        second = pool.submit(worker, "opposite-order-002", True)
        outcomes = [first.result(timeout=20), second.result(timeout=20)]

    with Session(setup_test_db) as session:
        reviews = session.scalars(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_type == "dft_results",
            ExtractionFieldReview.field_name == "value",
        )).all()
        assert session.scalar(select(func.count(AIVerificationBatchReceipt.id))) == 2
        assert len(reviews) == 2
        assert {review.target_id for review in reviews} == {str(row_id) for row_id in row_ids[:2]}
        assert all(review.reviewer_status == "ai_verified" for review in reviews)
        assert all(review.write_version == 2 for review in reviews)
    assert all([item["item_index"] for item in outcome] == [0, 1] for outcome in outcomes)
    assert all(
        {item["outcome"] for item in outcome} <= {"auto_verified", "auto_repaired", "skipped_terminal"}
        for outcome in outcomes
    )
    assert sum(
        item["outcome"] in {"auto_verified", "auto_repaired"}
        for outcome in outcomes for item in outcome
    ) == 2
    assert sum(item["outcome"] == "skipped_terminal" for outcome in outcomes for item in outcome) == 2


def test_web_package_continues_past_empty_keyset_page():
    service = object.__new__(AIVerificationService)
    calls: list[str | None] = []
    pages = [{"records": [], "next_cursor": "cursor-after-terminal-rows"}, {"records": [{"record_id": "pending-row"}], "next_cursor": None}]
    service.list_dft_record_tasks = lambda **kwargs: (calls.append(kwargs["cursor"]) or pages[len(calls) - 1])
    package = service.build_dft_direct_apply_package(paper_id=UUID("00000000-0000-0000-0000-000000000001"))
    assert calls == [None, "cursor-after-terminal-rows"]
    assert package["record_count"] == 1 and package["apply_tool"]["name"] == "apply_ai_verification_batch"


def test_direct_apply_schema_and_templates_expose_all_fields_without_batch_validation():
    """The advertised tool contract is complete, but one bad item remains repairable."""
    import asyncio

    from app.mcp.server import mcp_server

    tool = next(item for item in asyncio.run(mcp_server.list_tools()) if item.name == "apply_ai_verification_batch")
    item_schema = tool.inputSchema["properties"]["submissions"]["items"]
    assert {"target_id", "field_name", "decision", "expected_target_fingerprint", "table_id", "blocked_reasons"} <= set(item_schema["properties"])
    assert item_schema["properties"]["decision"]["enum"] == ["accept", "defer", "reject"]

    service = object.__new__(AIVerificationService)
    service.list_dft_record_tasks = lambda **_kwargs: {
        "records": [{
            "record_id": "record-1", "fields": [{
                "field_name": "value", "target_snapshot_fingerprint": "a" * 64,
                "expected_write_version": 2, "evidence_candidates": [{
                    "page": 8, "source_paper_id": "paper-1", "evidence_paper_id": "paper-1",
                    "table_reference_status": "resolved", "table_id": "table-1", "source_row_index": 3, "source_column_index": 2,
                }],
            }],
        }], "next_cursor": None,
    }
    # Build package plumbing remains pagination-compatible; precise template
    # construction is verified directly below without a database fixture.
    template = service._direct_apply_submission_templates(
        row=type("Row", (), {"id": "record-1"})(), field_name="value", target_fingerprint="a" * 64,
        expected_write_version=2, evidence_candidates=service.list_dft_record_tasks()["records"][0]["fields"][0]["evidence_candidates"],
    )
    assert template["accept"]["page"] == 8
    assert template["accept"]["table_id"] == "table-1"
    assert template["defer"]["blocked_reasons"]
    unknown = service._direct_apply_submission_templates(
        row=type("Row", (), {"id": "record-2"})(), field_name="value", target_fingerprint="b" * 64,
        expected_write_version=None,
        evidence_candidates=[{"page": 9, "table_id": "unresolved-table", "table_reference_status": "ambiguous"}],
    )
    assert not {"table_id", "source_row_index", "source_column_index"} & set(unknown["accept"])


def test_reaction_barrier_accepts_explicit_free_energy_barrier_language():
    """The field dictionary permits this kinetic-barrier wording."""
    target = type("BarrierTarget", (), {
        "property_type": "reaction_barrier", "catalyst_sample_id": None,
        "evidence_payload": {"material_identity": "synthetic"},
    })()
    checks = object.__new__(AIVerificationService)._content_checks(
        "dft_results", target, "energy_type", "reaction_barrier", None,
        "The catalyst showed the lowest free energy barrier (0.57 eV) for SRR.",
    )
    assert checks["energy_type_consistent"] is True


def test_real_direct_apply_zip_manifest_and_download_contract(setup_test_db):
    """Generate, download, and open a real ZIP from synthetic main/SI data."""
    paper_id, row_id = _seed_review_materials(setup_test_db)
    with Session(setup_test_db) as session:
        table = PaperTable(
            paper_id=paper_id, caption="Table 1. Field verification values",
            markdown_content="| catalyst | adsorption energy (eV) |\n| Fe-N-C | -1.20 |", page=1,
        )
        session.add(table)
        session.flush()
        structured = build_structured_table_cell_evidence(
            session, paper_id=paper_id, table_id=table.id, page=1,
            source_row_index=0, source_column_index=1,
        )
        assert structured is not None
        session.add(EvidenceLocator(
            paper_id=paper_id, source_type="table", page=1, table_id=table.id,
            target_type="dft_results", target_id=str(row_id), field_name="value",
            evidence_text=structured["canonical_evidence_text"], locator_status="resolved", locator_confidence=1.0,
            parser_source="test",
        ))
        session.commit()
    _mark_figure_table_review_completed(setup_test_db, paper_id)

    with TestClient(app) as client:
        response = client.post(
            f"/api/papers/{paper_id}/dft-direct-apply-bundle?include_figure_files=false",
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-litai-direct-apply"] == "true"
    with ZipFile(BytesIO(response.content), "r") as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        tasks = json.loads(archive.read("direct_apply/field_tasks.json"))
        members = {name: archive.read(name) for name in names if name != "manifest.json"}

    assert manifest["schema_version"] == "dft_direct_apply_bundle.v1"
    assert manifest["return_json_required"] is False
    assert manifest["direct_apply_contract"]["decisions"] == ["accept", "defer", "reject"]
    assert manifest["legacy_source_bundle_fingerprint"]
    assert manifest["bundle_fingerprint"] != manifest["legacy_source_bundle_fingerprint"]
    assert manifest["file_count"] == len(members)
    assert manifest["total_file_bytes"] == sum(len(data) for data in members.values())
    assert "manifest.json" not in {entry["path"] for entry in manifest["files"]}
    assert {entry["path"] for entry in manifest["files"]} == set(members)
    for entry in manifest["files"]:
        data = members[entry["path"]]
        assert entry["size_bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    expected_fingerprint = hashlib.sha256(json.dumps({
        "schema_version": "dft_direct_apply_bundle.v1",
        "task_snapshot_sha256": manifest["task_snapshot_sha256"],
        "files": manifest["files"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    assert manifest["bundle_fingerprint"] == expected_fingerprint
    assert manifest["task_snapshot_sha256"] == hashlib.sha256(members["direct_apply/field_tasks.json"]).hexdigest()
    assert any(record["record_id"] == str(row_id) for record in tasks["records"])
    value_field = next(
        field for record in tasks["records"] if record["record_id"] == str(row_id)
        for field in record["fields"] if field["field_name"] == "value"
    )
    assert any(candidate["table_reference_status"] == "resolved" for candidate in value_field["evidence_candidates"])
    assert any(path.startswith("source/main") for path in members)
    assert any(path.startswith("source/si/") for path in members)
    assert any(path.startswith("evidence/tables/") for path in members)
    forbidden = ("PASS", "REVISE", "REJECT", "NEEDS_HUMAN", "return_template.json", "WEB_AI_FILL_THIS.json")
    for path, data in members.items():
        if path.endswith((".json", ".md", ".jsonl")):
            text = data.decode("utf-8")
            assert not any(token in text for token in forbidden), path
    assert "return_template.json" not in names and "parsed/dft_review_checklist.json" not in names
