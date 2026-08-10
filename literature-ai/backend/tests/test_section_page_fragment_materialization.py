from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import fitz
import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EvidenceClaim, ExtractionFieldReview, Paper, PaperSection
from app.mcp.context import mcp_auth_context
from app.mcp.server import (
    get_ai_verification_tasks,
    get_review_coverage,
    materialize_ai_section_page_fragments,
    submit_ai_verification_batch,
)
from app.services.ai_verification_service import AuthenticatedAIVerificationIdentity
from app.services.evidence_page_recovery import EvidencePageRecoveryService
from app.services.section_page_fragment_materialization_service import (
    SectionPageFragmentMaterializationService,
)
from app.utils.ai_verification import AI_VERIFICATION_CAPABILITY
from app.utils.review_safety import content_object_gate


def _pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(60, 60, 540, 780), text, fontsize=10)
    document.save(path)
    document.close()


def _seed_body(
    session: Session,
    tmp_path: Path,
    *,
    title: str,
    pages: list[str],
    section_text: str | None = None,
) -> tuple[Paper, PaperSection]:
    pdf_path = tmp_path / f"{title.replace(' ', '-')}-{uuid4().hex}.pdf"
    _pdf(pdf_path, pages)
    paper = Paper(title=title, pdf_path=str(pdf_path), authors=["Tester"])
    session.add(paper)
    session.flush()
    section = PaperSection(
        paper_id=paper.id,
        section_title="Results",
        section_type="body",
        text=section_text or " ".join(pages),
        page_start=None,
        page_end=None,
    )
    session.add(section)
    session.flush()
    return paper, section


def _candidate(session: Session, paper: Paper, section: PaperSection, index: int = 0) -> dict[str, str]:
    recovered = EvidencePageRecoveryService(session).recover_section_page_fragments(
        paper=paper,
        section=section,
    )
    fragment = recovered["fragments"][index]
    return {
        "fragment_id": fragment["fragment_id"],
        "fragment_fingerprint": fragment["fragment_fingerprint"],
    }


def _identity() -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:single-verifier",
        source_label="Single Verifier",
        model_agent="single_verifier",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}),
        identity_verified=True,
    )


def _configure_mcp_identities(monkeypatch) -> None:
    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "reader|Reader|fragment-reader|read_papers;"
        "owner|Owner|fragment-owner|read_papers,append_notes,propose_corrections,review_corrections;"
        "single_verifier|Single Verifier|fragment-single-verifier|read_papers,ai_verify_content",
    )
    get_settings.cache_clear()


def test_public_mcp_materialization_is_pending_idempotent_and_flows_to_dry_run_verification(
    setup_test_db,
    tmp_path,
    monkeypatch,
):
    first = "Fe-N4 sites accelerate Li2S4 conversion at the cathode."
    second = "The resulting pathway lowers the reported barrier to 0.42 eV."
    with Session(setup_test_db) as session:
        paper, section = _seed_body(
            session,
            tmp_path,
            title="Public materialization",
            pages=[first, second],
        )
        candidate = _candidate(session, paper, section)
        session.commit()
        paper_id = str(paper.id)
        section_id = str(section.id)

    _configure_mcp_identities(monkeypatch)
    with pytest.raises(PermissionError, match="authentication context"):
        materialize_ai_section_page_fragments(
            paper_id=paper_id,
            parent_section_id=section_id,
            candidates=[candidate],
        )
    for credential in ("fragment-reader", "fragment-owner"):
        with mcp_auth_context(credential), pytest.raises(PermissionError, match="ai_verify_content"):
            materialize_ai_section_page_fragments(
                paper_id=paper_id,
                parent_section_id=section_id,
                candidates=[candidate],
            )

    with mcp_auth_context("fragment-single-verifier"):
        dry_run = materialize_ai_section_page_fragments(
            paper_id=paper_id,
            parent_section_id=section_id,
            candidates=[candidate],
        )
    assert dry_run["dry_run"] is True
    assert dry_run["postgres_transaction_read_only"] is True
    assert dry_run["database_writes"] is False
    assert dry_run["items"][0]["status"] == "would_materialize_pending"
    with Session(setup_test_db) as session:
        assert session.scalar(select(func.count(EvidenceClaim.id))) == 0

    with mcp_auth_context("fragment-single-verifier"):
        formal = materialize_ai_section_page_fragments(
            paper_id=paper_id,
            parent_section_id=section_id,
            candidates=[candidate],
            dry_run=False,
        )
        repeated = materialize_ai_section_page_fragments(
            paper_id=paper_id,
            parent_section_id=section_id,
            candidates=[candidate],
            dry_run=False,
        )
        tasks = get_ai_verification_tasks(
            paper_id=paper_id,
            target_type="section_page_fragments",
            recover_evidence=True,
        )
        coverage = get_review_coverage(paper_id=paper_id)

    assert formal["materialized"] == 1
    assert formal["items"][0]["status"] == "pending"
    assert formal["database_writes"] is True
    assert repeated["idempotent"] == 1
    assert repeated["items"][0]["status"] == "existing"
    assert repeated["database_writes"] is False
    assert tasks["total"] == 1
    task = tasks["tasks"][0]
    assert task["target_id"] == candidate["fragment_id"]
    fragment_coverage = coverage["section_page_fragments"]
    assert fragment_coverage["total"] == 1
    assert fragment_coverage["pending"] == 1
    assert fragment_coverage["unreviewed"] == 1
    assert fragment_coverage["ai_verified"] == 0
    assert fragment_coverage["blocked"] == 1
    assert fragment_coverage["can_use_for_writing"] == 0
    assert fragment_coverage["can_use_for_citation"] == 0

    with mcp_auth_context("fragment-single-verifier"):
        verification = submit_ai_verification_batch(
            paper_id=paper_id,
            dry_run=True,
            submissions=[
                {
                    "target_type": "section_page_fragments",
                    "target_id": task["target_id"],
                    "field_name": "text",
                    "decision": "accept",
                    "confidence": 0.99,
                    "evidence_text": task["target_snapshot"]["evidence_text"],
                    "page": formal["items"][0]["page"],
                    "reasoning_summary": "Exact materialized fragment is reread from its physical PDF page.",
                    "expected_target_fingerprint": task["target_snapshot_fingerprint"],
                    "expected_write_version": task["expected_write_version"],
                }
            ],
        )
    assert verification["dry_run"] is True
    assert verification["database_writes"] is False
    assert verification["items"][0]["status"] == "ai_verified"
    with Session(setup_test_db) as session:
        fragment = session.get(EvidenceClaim, UUID(candidate["fragment_id"]))
        parent = session.get(PaperSection, UUID(section_id))
        assert fragment is not None
        assert fragment.source_type == "section_page_fragment"
        assert fragment.validation_status == "unverified"
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 0
        assert content_object_gate(session, "section_page_fragments", fragment).can_use_for_writing is False
        assert content_object_gate(session, "sections", parent).can_use_for_writing is False


def test_materialization_rejects_stale_forged_approximate_and_cross_scope_candidates(
    setup_test_db,
    tmp_path,
):
    exact = "Ni-N4 sites promote Li2S6 conversion with a 0.38 eV barrier."
    with Session(setup_test_db) as session:
        paper, section = _seed_body(session, tmp_path, title="Scope A", pages=[exact])
        other_paper, other_section = _seed_body(
            session,
            tmp_path,
            title="Scope B",
            pages=["A separate paper reports a 0.91 eV barrier."],
        )
        sibling = PaperSection(
            paper_id=paper.id,
            section_title="Discussion",
            section_type="body",
            text="A sibling section contains different content on purpose.",
        )
        session.add(sibling)
        session.flush()
        candidate = _candidate(session, paper, section)
        session.commit()

        service = SectionPageFragmentMaterializationService(session)
        with pytest.raises(ValueError, match="batch exceeds limit 20"):
            service.materialize(
                paper_id=paper.id,
                parent_section_id=section.id,
                candidates=[candidate] * 21,
                identity=_identity(),
            )
        stale = service.materialize(
            paper_id=paper.id,
            parent_section_id=section.id,
            candidates=[{**candidate, "fragment_fingerprint": "0" * 64}],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert stale["status"] == "rejected"
        assert "stale_fragment_fingerprint" in stale["items"][0]["blocked_reasons"]

        forged = service.materialize(
            paper_id=paper.id,
            parent_section_id=section.id,
            candidates=[{"fragment_id": str(uuid4()), "fragment_fingerprint": "1" * 64}],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert "candidate_not_recovered_or_stale" in forged["items"][0]["blocked_reasons"]

        wrong_parent = service.materialize(
            paper_id=paper.id,
            parent_section_id=sibling.id,
            candidates=[candidate],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert wrong_parent["status"] == "rejected"

        cross_paper = service.materialize(
            paper_id=other_paper.id,
            parent_section_id=other_section.id,
            candidates=[candidate],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert cross_paper["status"] == "rejected"
        with pytest.raises(LookupError, match="Parent section not found for paper"):
            service.materialize(
                paper_id=other_paper.id,
                parent_section_id=section.id,
                candidates=[candidate],
                identity=_identity(),
            )

        with pytest.raises(ValidationError):
            service.materialize(
                paper_id=paper.id,
                parent_section_id=section.id,
                candidates=[{**candidate, "page": 999, "text": "forged client text"}],
                identity=_identity(),
            )

        approximate_paper, approximate_section = _seed_body(
            session,
            tmp_path,
            title="Approximate",
            pages=["The reported barrier is 2.5 eV under the tested condition."],
            section_text="The reported barrier is 3.5 eV under the tested condition.",
        )
        session.flush()
        approximate_recovery = EvidencePageRecoveryService(session).recover_section_page_fragments(
            paper=approximate_paper,
            section=approximate_section,
        )
        assert approximate_recovery["fragment_count"] == 0
        approximate = service.materialize(
            paper_id=approximate_paper.id,
            parent_section_id=approximate_section.id,
            candidates=[{"fragment_id": str(uuid4()), "fragment_fingerprint": "2" * 64}],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert approximate["status"] == "rejected"
        assert session.scalar(select(func.count(EvidenceClaim.id))) == 0


def test_materialization_transaction_failure_leaves_no_partial_rows(setup_test_db, tmp_path):
    pages = [
        "Co-N4 sites accelerate sulfur reduction on the first physical page.",
        "The second physical page reports a consistent 0.44 eV energy barrier.",
    ]
    with Session(setup_test_db) as session:
        paper, section = _seed_body(session, tmp_path, title="Atomic batch", pages=pages)
        candidates = [_candidate(session, paper, section, index) for index in range(2)]
        paper_id = paper.id
        section_id = section.id
        session.commit()

    calls = {"count": 0}

    def fail_second_insert(_mapper, _connection, _target):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected_materialization_failure")

    event.listen(EvidenceClaim, "before_insert", fail_second_insert)
    try:
        with Session(setup_test_db) as session:
            with pytest.raises(RuntimeError, match="injected_materialization_failure"):
                SectionPageFragmentMaterializationService(session).materialize(
                    paper_id=paper_id,
                    parent_section_id=section_id,
                    candidates=candidates,
                    identity=_identity(),
                    dry_run=False,
                    commit=False,
                )
            session.rollback()
    finally:
        event.remove(EvidenceClaim, "before_insert", fail_second_insert)

    with Session(setup_test_db) as session:
        assert session.scalar(select(func.count(EvidenceClaim.id))) == 0
