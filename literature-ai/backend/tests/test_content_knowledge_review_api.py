from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    AuditLog,
    ContentEvidenceItem,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
)
from app.main import app


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _pdf(tmp_path: Path, name: str = "review.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\ncontent knowledge review\n%%EOF\n")
    return path


def _seed_persistent_search(engine, tmp_path: Path) -> dict[str, str]:
    factory = _factory(engine)
    with factory.begin() as session:
        paper = Paper(
            paper_code="B0097",
            title="Atomic kinetics study for Li-S conversion",
            doi="10.1000/atomic.0097",
            pdf_path=str(_pdf(tmp_path)),
            authors=[],
        )
        other = Paper(
            paper_code="B0098",
            title="Unrelated control paper",
            doi="10.1000/control.0098",
            pdf_path=str(_pdf(tmp_path, "other.pdf")),
            authors=[],
        )
        session.add_all([paper, other])
        session.flush()
        base = datetime(2026, 1, 1, 12, 0, 0)
        items = [
            ContentEvidenceItem(
                paper_id=paper.id,
                category="mechanism_evidence",
                source_type="mechanism_claim",
                source_id="search-a",
                content="alpha unique kinetics statement",
                evidence_text="supporting conversion text",
                section_title="Results",
                page_start=2,
                risk_flags=["check_units"],
                source_identity_verified=True,
                updated_at=base + timedelta(minutes=3),
            ),
            ContentEvidenceItem(
                paper_id=paper.id,
                category="performance_evidence",
                source_type="evidence_claim",
                source_id="search-b",
                content="alpha kinetics observation",
                evidence_text="unique supporting text",
                section_title="Discussion",
                page_start=3,
                risk_flags=[],
                source_identity_verified=False,
                updated_at=base + timedelta(minutes=2),
            ),
            ContentEvidenceItem(
                paper_id=paper.id,
                category="method_evidence",
                source_type="paper_note",
                source_id="search-c",
                content="synthesis protocol",
                evidence_text="method detail",
                section_title="Experimental",
                page_start=4,
                risk_flags=[],
                source_identity_verified=False,
                updated_at=base + timedelta(minutes=1),
            ),
            ContentEvidenceItem(
                paper_id=other.id,
                category="mechanism_evidence",
                source_type="mechanism_claim",
                source_id="other",
                content="alpha control kinetics statement",
                evidence_text="control evidence",
                section_title="Results",
                page_start=2,
                risk_flags=[],
                source_identity_verified=False,
                updated_at=base,
            ),
        ]
        session.add_all(items)
        session.add(
            MechanismClaim(
                paper_id=paper.id,
                claim_type="legacy_only",
                claim_text="legacy-only raw source must not replace an existing projection",
                evidence_types=[],
            )
        )
        session.flush()
        return {"paper_id": str(paper.id), "first_item_id": str(items[0].id)}


def _seed_review_item(
    engine,
    tmp_path: Path,
    *,
    category: str = "mechanism_evidence",
    pdf_exists: bool = True,
    evidence_text: str | None = "PDF-backed evidence",
    located: bool = True,
) -> tuple[str, str]:
    factory = _factory(engine)
    pdf_path = str(_pdf(tmp_path)) if pdf_exists else str(tmp_path / "missing.pdf")
    with factory.begin() as session:
        paper = Paper(paper_code="RV001", title="Review fixture", pdf_path=pdf_path, authors=[])
        session.add(paper)
        session.flush()
        item = ContentEvidenceItem(
            paper_id=paper.id,
            category=category,
            source_type="mechanism_claim",
            source_id=f"review-{category}-{evidence_text}-{located}",
            content="Reviewable statement",
            evidence_text=evidence_text,
            evidence_locator={"page": 2} if located else None,
            page_start=None,
            review_status="needs_review",
            citation_status="needs_review",
            risk_flags=[],
        )
        session.add(item)
        session.flush()
        return str(paper.id), str(item.id)


def _seed_sync_source(engine) -> str:
    factory = _factory(engine)
    with factory.begin() as session:
        paper = Paper(
            paper_code="SY001",
            title="Sync invalidation fixture",
            pdf_path="sync.pdf",
            authors=[],
        )
        session.add(paper)
        session.flush()
        session.add(
            MechanismClaim(
                paper_id=paper.id,
                claim_type="conversion",
                claim_text="Original Fe-N4 source content.",
                evidence_types=["text"],
                evidence_text="Original PDF evidence.",
            )
        )
        return str(paper.id)


def _detail(client: TestClient, item_id: str) -> dict:
    response = client.get(f"/api/content-knowledge/items/{item_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "content_knowledge.v1"
    assert payload["item"]["reviewable"] is True
    assert payload["item"]["requires_sync"] is False
    return payload["item"]


def test_persistent_search_all_fields_relevance_filters_and_pagination(setup_test_db, tmp_path):
    seeded = _seed_persistent_search(setup_test_db, tmp_path)
    client = TestClient(app)

    for query, expected_total in (
        ("B0097", 3),
        ("Atomic conversion", 3),
        ("10.1000/atomic.0097", 3),
        ("alpha unique", 2),
        ("mechanism_evidence mechanism_claim", 2),
        ("B0097 alpha", 2),
    ):
        response = client.get("/api/content-knowledge", params={"query": query, "include_blocked": True})
        assert response.status_code == 200
        assert response.json()["total"] == expected_total

    ranked = client.get("/api/content-knowledge", params={"query": "alpha unique"}).json()["items"]
    assert ranked[0]["source_id"] == "search-a"
    assert ranked[1]["source_id"] == "search-b"
    assert client.get("/api/content-knowledge", params={"query": "legacy-only"}).json()["total"] == 0

    trusted = client.get("/api/content-knowledge", params={"source_trust": "verified"}).json()
    assert [item["source_id"] for item in trusted["items"]] == ["search-a"]
    risky = client.get("/api/content-knowledge", params={"problem_status": "has_risk"}).json()
    assert [item["source_id"] for item in risky["items"]] == ["search-a"]
    assert client.get("/api/content-knowledge", params={"source_trust": "invalid"}).status_code == 422
    assert client.get("/api/content-knowledge", params={"review_status": "invalid"}).status_code == 422

    pages = [
        client.get(
            "/api/content-knowledge",
            params={"paper_id": seeded["paper_id"], "limit": 1, "offset": offset},
        ).json()
        for offset in range(3)
    ]
    ids = [page["items"][0]["item_id"] for page in pages]
    assert len(set(ids)) == 3
    assert all(page["total"] == 3 for page in pages)
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert all(page["offset"] == index for index, page in enumerate(pages))


def test_item_detail_404_and_paper_summary(setup_test_db, tmp_path):
    seeded = _seed_persistent_search(setup_test_db, tmp_path)
    client = TestClient(app)

    item = _detail(client, seeded["first_item_id"])
    assert item["paper_code"] == "B0097"
    assert item["paper_doi"] == "10.1000/atomic.0097"
    assert item["updated_at"]
    assert client.get("/api/content-knowledge/items/00000000-0000-0000-0000-000000000000").status_code == 404

    summary = client.get(f"/api/content-knowledge/papers/{seeded['paper_id']}/review-summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["total"] == 3
    assert payload["pending_total"] == 3
    assert payload["by_category"] == {
        "mechanism_evidence": 1,
        "method_evidence": 1,
        "performance_evidence": 1,
    }
    assert payload["by_review_status"] == {"needs_review": 3}


def test_paper_summary_uses_authoritative_gate_and_reports_projection_drift(setup_test_db, tmp_path):
    factory = _factory(setup_test_db)
    with factory.begin() as session:
        paper = Paper(
            paper_code="B0099",
            title="Authoritative summary",
            pdf_path=str(_pdf(tmp_path, "summary.pdf")),
            authors=[],
        )
        session.add(paper)
        session.flush()
        unsafe = MechanismClaim(
            paper_id=paper.id,
            claim_type="unsafe",
            claim_text="Projection claims this is citable",
            evidence_text="Unsafe projection evidence.",
        )
        safe = MechanismClaim(
            paper_id=paper.id,
            claim_type="safe",
            claim_text="Canonical gate allows this claim",
            evidence_text="Safe canonical evidence.",
        )
        session.add_all([unsafe, safe])
        session.flush()
        unsafe_projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="mechanism_evidence",
            source_type="mechanism_claim",
            source_id=str(unsafe.id),
            content=unsafe.claim_text,
            evidence_text=unsafe.evidence_text,
            review_status="validated",
            citation_status="citable",
        )
        stale_projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="mechanism_evidence",
            source_type="mechanism_claim",
            source_id=str(safe.id),
            content=safe.claim_text,
            evidence_text=safe.evidence_text,
            review_status="needs_review",
            citation_status="needs_review",
        )
        session.add_all([
            unsafe_projection,
            stale_projection,
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="mechanism_claims",
                target_id=str(safe.id),
                field_name="claim_text",
                reviewer_status="verified",
                target_resolution_status="active",
                evidence_text=safe.evidence_text,
            ),
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="mechanism_claims",
                target_id=str(safe.id),
                field_name="claim_text",
                page=2,
                evidence_text=safe.evidence_text,
                locator_status="exact_page",
                locator_confidence=1.0,
                parser_source="test",
            ),
        ])
        paper_id = paper.id

    payload = TestClient(app).get(
        f"/api/content-knowledge/papers/{paper_id}/review-summary"
    ).json()

    assert payload["total"] == 2
    assert payload["completed_total"] == 1
    assert payload["pending_total"] == 1
    assert payload["authoritative_reviewed_total"] == 1
    assert payload["can_use_for_writing_total"] == 1
    assert payload["can_use_for_citation_total"] == 1
    assert payload["blocked_total"] == 1
    assert payload["projection_gate_mismatch_total"] == 1
    assert payload["projection_cache_stale_total"] == 1


@pytest.mark.parametrize(
    ("decision", "reason", "review_status", "citation_status"),
    [
        ("approve_citable", None, "validated", "citable"),
        ("writing_only", None, "validated", "writing_only"),
        ("needs_human", "ambiguous evidence", "needs_human", "needs_review"),
        ("reject", "claim is unsupported", "rejected", "blocked"),
    ],
)
def test_four_manual_review_decisions_and_audit(
    setup_test_db,
    tmp_path,
    decision,
    reason,
    review_status,
    citation_status,
):
    _, item_id = _seed_review_item(setup_test_db, tmp_path)
    client = TestClient(app)
    current = _detail(client, item_id)
    response = client.post(
        f"/api/content-knowledge/items/{item_id}/review",
        json={
            "decision": decision,
            "reviewer": "Human Reviewer",
            "reason": reason,
            "expected_updated_at": current["updated_at"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["reviewed"] is True
    assert payload["audit_log_id"]
    assert payload["item"]["review_status"] == review_status
    assert payload["item"]["metadata"]["projection_state"]["citation_status"] == citation_status
    assert payload["item"]["citation_policy"] == "blocked"
    assert payload["item"]["can_use_for_writing"] is False
    assert payload["item"]["can_use_for_citation"] is False
    assert payload["item"]["reviewer"] == "Human Reviewer"

    factory = _factory(setup_test_db)
    with factory() as session:
        audit = session.get(AuditLog, UUID(payload["audit_log_id"]))
        assert audit is not None
        assert audit.action == "review_content_evidence_item"
        assert audit.payload["before"]["review_status"] == "needs_review"
        assert audit.payload["after"]["review_status"] == review_status
        assert audit.payload["reason"] == reason
        assert audit.payload["reviewer"] == "Human Reviewer"


@pytest.mark.parametrize(
    ("seed_kwargs", "expected_code"),
    [
        ({"pdf_exists": False}, "citable_requires_real_pdf"),
        ({"evidence_text": None}, "citable_requires_evidence_text"),
        ({"located": False}, "citable_requires_locator"),
        ({"category": "figure_table_evidence"}, "figure_table_evidence_requires_chart_review"),
    ],
)
def test_citable_evidence_gates(setup_test_db, tmp_path, seed_kwargs, expected_code):
    _, item_id = _seed_review_item(setup_test_db, tmp_path, **seed_kwargs)
    client = TestClient(app)
    current = _detail(client, item_id)
    response = client.post(
        f"/api/content-knowledge/items/{item_id}/review",
        json={
            "decision": "approve_citable",
            "reviewer": "Human Reviewer",
            "expected_updated_at": current["updated_at"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code


def test_reason_concurrency_and_ai_identity_guards(setup_test_db, tmp_path):
    _, item_id = _seed_review_item(setup_test_db, tmp_path)
    client = TestClient(app)
    current = _detail(client, item_id)

    missing_reason = client.post(
        f"/api/content-knowledge/items/{item_id}/review",
        json={
            "decision": "reject",
            "reviewer": "Human Reviewer",
            "expected_updated_at": current["updated_at"],
        },
    )
    assert missing_reason.status_code == 422

    ai_claim = client.post(
        f"/api/content-knowledge/items/{item_id}/review",
        headers={"Authorization": "Bearer test-reader-key"},
        json={
            "decision": "writing_only",
            "reviewer": "I am a human",
            "expected_updated_at": current["updated_at"],
        },
    )
    assert ai_claim.status_code == 403
    assert ai_claim.json()["detail"]["code"] == "human_review_requires_non_mcp_session"

    factory = _factory(setup_test_db)
    with factory.begin() as session:
        item = session.get(ContentEvidenceItem, UUID(item_id))
        item.content = "Concurrent source update"
        item.updated_at = item.updated_at + timedelta(seconds=1)
    stale = client.post(
        f"/api/content-knowledge/items/{item_id}/review",
        json={
            "decision": "writing_only",
            "reviewer": "Human Reviewer",
            "expected_updated_at": current["updated_at"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "content_knowledge_item_updated"


def test_sync_invalidates_review_when_source_changes_only(setup_test_db, monkeypatch):
    paper_id = _seed_sync_source(setup_test_db)
    monkeypatch.setattr(
        "app.services.content_knowledge_service.get_embedding_service",
        lambda **_: type("NoEmbedding", (), {"embed_text": lambda self, text: None})(),
    )
    client = TestClient(app)
    assert client.post("/api/content-knowledge/sync", params={"paper_id": paper_id}).status_code == 200

    factory = _factory(setup_test_db)
    with factory.begin() as session:
        item = session.scalar(
            select(ContentEvidenceItem).where(ContentEvidenceItem.source_type == "mechanism_claim")
        )
        item.review_status = "validated"
        item.citation_status = "citable"
        item.reviewer = "Human Reviewer"
        item.reviewed_at = datetime(2026, 1, 2, 12, 0, 0)
        item.embedding_model = "old-model"
        item_id = item.id

    unchanged = client.post("/api/content-knowledge/sync", params={"paper_id": paper_id})
    assert unchanged.status_code == 200
    with factory() as session:
        item = session.get(ContentEvidenceItem, item_id)
        assert item.review_status == "validated"
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "invalidate_content_evidence_review")
        ) == 0

    with factory.begin() as session:
        source = session.get(MechanismClaim, UUID(session.get(ContentEvidenceItem, item_id).source_id))
        source.claim_text = "Changed Fe-N4 source content after human review."

    changed = client.post("/api/content-knowledge/sync", params={"paper_id": paper_id})
    assert changed.status_code == 200
    with factory() as session:
        item = session.get(ContentEvidenceItem, item_id)
        assert item.review_status == "needs_review"
        assert item.citation_status == "needs_review"
        assert item.reviewer is None
        assert item.reviewed_at is None
        assert "source_changed_after_review" in item.risk_flags
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "invalidate_content_evidence_review")
        ).all()
        assert len(audits) == 1
        assert "content" in audits[0].payload["changed_fields"]
        assert audits[0].payload["before"]["citation_status"] == "citable"
        assert audits[0].payload["after"]["citation_status"] == "needs_review"

    assert client.post("/api/content-knowledge/sync", params={"paper_id": paper_id}).status_code == 200
    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "invalidate_content_evidence_review")
        ) == 1
