from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    EvidenceClaim,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    MechanismClaim,
    Paper,
    PaperNote,
    WritingCard,
)
from app.main import app
from app.services.content_knowledge_service import candidate_audit_semantics


def _seed_content_knowledge(engine) -> str:
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with Session.begin() as session:
        paper = Paper(
            library_name="内容知识测试库",
            title="Unified content knowledge for Li-S catalysts",
            paper_code="CK001",
            doi="10.1000/ck001",
            abstract="content knowledge test",
            pdf_path="content-knowledge.pdf",
        )
        session.add(paper)
        session.flush()

        mechanism = MechanismClaim(
            paper_id=paper.id,
            claim_type="lips_conversion",
            claim_text="Fe-N4 sites accelerate LiPS conversion.",
            evidence_types=["conversion"],
            evidence_text="The paper reports faster LiPS conversion on Fe-N4 sites.",
        )
        writing_card = WritingCard(
            paper_id=paper.id,
            paper_type="research",
            research_gap="conversion kinetics need stable catalyst evidence",
            proposed_solution="stable catalyst evidence links conversion kinetics",
            evidence_chain=[
                {
                    "supports_fields": ["research_gap"],
                    "text": "conversion kinetics need stable catalyst evidence",
                    "source": "pdf",
                    "page": 2,
                    "locator_status": "exact_page",
                    "can_jump_to_pdf_page": True,
                },
                {
                    "supports_fields": ["proposed_solution"],
                    "text": "stable catalyst evidence links conversion kinetics",
                    "source": "pdf",
                    "page": 3,
                    "locator_status": "exact_page",
                    "can_jump_to_pdf_page": True,
                },
            ],
        )
        note = PaperNote(
            paper_id=paper.id,
            source="ide_ai",
            content="Potential conflict between mechanism statement and Figure 3.",
            page=3,
            section_title="Results",
            quoted_text="Figure 3 reports a weaker trend.",
        )
        evidence = EvidenceClaim(
            paper_id=paper.id,
            claim_text="Unreviewed capacity claim should not be citable yet.",
            evidence_text="Capacity was described without a reviewed locator.",
            validation_status="unverified",
            target_type="electrochemical_performance",
            page_start=4,
            page_end=4,
        )
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="web_ai",
            source_label="网页AI审核",
            mapping_status="completed",
        )
        session.add_all([mechanism, writing_card, note, evidence, run])
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="mechanism_review",
                normalized_payload={
                    "target_type": "mechanism_claims",
                    "content": "Web AI reports a mechanism conflict needing resolution.",
                },
                evidence_payload={"text": "conflict sentence on page 3", "page": 3},
                status="requires_resolution",
                mapping_reason="conflicting mechanism evidence",
            )
        )
        return str(paper.id)


def _items_by_source(payload: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in payload["items"]:
        grouped.setdefault(item["source_type"], []).append(item)
    return grouped


def test_content_knowledge_unifies_sources_and_preserves_safety_policies(setup_test_db):
    paper_id = _seed_content_knowledge(setup_test_db)
    response = TestClient(app).get(
        "/api/content-knowledge",
        params={"paper_id": paper_id, "result_view": "all", "limit": 50},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "content_knowledge.v1"
    grouped = _items_by_source(payload)
    assert {"mechanism_claim", "writing_card", "paper_note", "evidence_claim", "external_analysis_candidate"} <= set(grouped)

    mechanism = grouped["mechanism_claim"][0]
    assert mechanism["category"] == "mechanism_evidence"
    assert mechanism["citation_policy"] == "needs_review"
    assert mechanism["can_use_for_citation"] is False
    assert mechanism["review_status"] == "missing"

    writing = grouped["writing_card"][0]
    assert writing["category"] == "writing_material"
    assert writing["candidate_status"] == "candidate_unverified"
    assert writing["review_status"] == "needs_review"
    assert writing["review_gate_status"] == "blocked"
    assert writing["citation_policy"] == "needs_review"
    assert writing["can_use_for_writing"] is False
    assert writing["can_use_for_citation"] is False
    assert "verified" not in writing["citation_policy"]

    evidence = grouped["evidence_claim"][0]
    assert evidence["citation_policy"] == "needs_review"
    assert evidence["can_use_for_citation"] is False
    assert "evidence_claim_unverified" in evidence["risk_flags"]

    candidate = grouped["external_analysis_candidate"][0]
    assert candidate["review_status"] == "needs_review"
    assert candidate["citation_policy"] == "needs_review"
    assert "candidate_requires_resolution" in candidate["risk_flags"]
    assert candidate["source_ai"] == "web_ai"
    assert candidate["source_label"] == "网页AI审核"


def test_content_knowledge_filters_category_query_and_candidates(setup_test_db):
    paper_id = _seed_content_knowledge(setup_test_db)
    client = TestClient(app)

    category_payload = client.get(
        "/api/content-knowledge",
        params={"paper_id": paper_id, "category": "mechanism_evidence", "limit": 50},
    ).json()
    assert category_payload["items"]
    assert {item["category"] for item in category_payload["items"]} == {"mechanism_evidence"}

    query_payload = client.get(
        "/api/content-knowledge",
        params={
            "paper_id": paper_id,
            "query": "mechanism conflict",
            "result_view": "audit",
            "limit": 50,
        },
    ).json()
    assert any(item["source_type"] == "external_analysis_candidate" for item in query_payload["items"])

    without_candidates = client.get(
        "/api/content-knowledge",
        params={
            "paper_id": paper_id,
            "result_view": "audit",
            "include_candidates": False,
            "query": "mechanism conflict",
            "limit": 50,
        },
    ).json()
    assert all(item["source_type"] != "external_analysis_candidate" for item in without_candidates["items"])

    by_paper_code = client.get(
        "/api/content-knowledge",
        params={"paper_id": "CK001", "category": "writing_material", "limit": 10},
    ).json()
    assert by_paper_code["items"]
    assert {item["paper_code"] for item in by_paper_code["items"]} == {"CK001"}


def test_result_views_keep_content_and_audit_counts_separate_before_and_after_sync(setup_test_db):
    paper_id = _seed_content_knowledge(setup_test_db)
    client = TestClient(app)

    def assert_views() -> None:
        content = client.get(
            "/api/content-knowledge",
            params={"paper_id": paper_id, "result_view": "content", "limit": 2},
        ).json()
        assert content["result_view"] == "content"
        assert content["total"] == 4
        assert content["result_item_count"] == 2
        assert content["distinct_paper_count"] == 1
        assert sum(content["category_counts"].values()) == 4
        assert all(item["source_type"] != "external_analysis_candidate" for item in content["items"])
        assert "not a paper count" in content["count_semantics"]["result_items"]

        audit = client.get(
            "/api/content-knowledge",
            params={"paper_id": paper_id, "result_view": "audit", "limit": 50},
        ).json()
        assert audit["total"] == 1
        assert audit["distinct_paper_count"] == 1
        assert sum(audit["category_counts"].values()) == 1
        assert {item["source_type"] for item in audit["items"]} == {"external_analysis_candidate"}
        candidate = audit["items"][0]
        assert candidate["item_kind"] == "audit"
        assert candidate["audit_state"] == "active_unresolved"
        assert candidate["audit_requires_action"] is True
        assert candidate["can_use_for_writing"] is False
        assert candidate["can_use_for_citation"] is False

        all_items = client.get(
            "/api/content-knowledge",
            params={"paper_id": paper_id, "result_view": "all", "limit": 50},
        ).json()
        assert all_items["total"] == 5
        assert {item["item_kind"] for item in all_items["items"]} == {"content", "audit"}

    assert_views()
    assert client.post("/api/content-knowledge/sync", params={"paper_id": paper_id}).status_code == 200
    assert_views()
    assert client.get("/api/content-knowledge", params={"result_view": "invalid"}).status_code == 422


def test_candidate_audit_semantics_require_explicit_linkage_and_keep_terminal_history():
    linked = candidate_audit_semantics(
        "materialized",
        target_type="dft_results",
        target_id="formal-dft-id",
    )
    assert linked["state"] == "applied_to_formal_dft"
    assert linked["linkage_explicit"] is True
    assert linked["requires_action"] is False

    unlinked = candidate_audit_semantics("materialized")
    assert unlinked["state"] == "unknown_requires_attention"
    assert unlinked["warning"] == "materialized_status_missing_explicit_target_link"

    for status in ("rejected_by_local_ai", "failed", "skipped"):
        terminal = candidate_audit_semantics(status)
        assert terminal["state"] == "terminal_history"
        assert terminal["requires_action"] is False
        assert terminal["label"] != "待处理审计候选"


def test_audit_view_does_not_hide_blocked_terminal_candidates_in_legacy_or_persistent_path(
    setup_test_db,
):
    paper_id = _seed_content_knowledge(setup_test_db)
    Session = sessionmaker(
        bind=setup_test_db,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    with Session.begin() as session:
        candidate = session.query(ExternalAnalysisCandidate).one()
        candidate.status = "failed"

    client = TestClient(app)

    def assert_terminal_visible() -> None:
        payload = client.get(
            "/api/content-knowledge",
            params={"paper_id": paper_id, "result_view": "audit"},
        ).json()
        assert payload["total"] == 1
        assert payload["items"][0]["audit_state"] == "terminal_history"
        assert payload["items"][0]["can_use_for_writing"] is False
        assert payload["items"][0]["can_use_for_citation"] is False

    assert_terminal_visible()
    assert client.post("/api/content-knowledge/sync", params={"paper_id": paper_id}).status_code == 200
    assert_terminal_visible()


def test_legacy_fallback_search_pagination_and_reviewability(setup_test_db):
    paper_id = _seed_content_knowledge(setup_test_db)
    client = TestClient(app)

    search = client.get(
        "/api/content-knowledge",
        params={
            "query": "CK001 catalysts 10.1000/ck001",
            "result_view": "all",
            "limit": 2,
            "offset": 0,
        },
    )
    assert search.status_code == 200
    first = search.json()
    assert first["total"] == 5
    assert first["offset"] == 0
    assert first["limit"] == 2
    assert first["has_more"] is True
    assert all(item["paper_id"] == paper_id for item in first["items"])
    assert all(item["paper_doi"] == "10.1000/ck001" for item in first["items"])
    assert all(item["reviewable"] is False and item["requires_sync"] is True for item in first["items"])

    pages = [
        client.get(
            "/api/content-knowledge",
            params={
                "query": "CK001 catalysts 10.1000/ck001",
                "result_view": "all",
                "limit": 2,
                "offset": offset,
            },
        ).json()
        for offset in (0, 2, 4)
    ]
    item_ids = [item["item_id"] for page in pages for item in page["items"]]
    assert len(item_ids) == first["total"]
    assert len(set(item_ids)) == first["total"]
    assert pages[-1]["has_more"] is False

    legacy_id = first["items"][0]["item_id"]
    review = client.post(
        f"/api/content-knowledge/items/{legacy_id}/review",
        json={
            "decision": "writing_only",
            "reviewer": "tester",
            "expected_updated_at": "2026-01-01T00:00:00",
        },
    )
    assert review.status_code == 422


def test_retrieval_search_includes_content_knowledge_with_policy_metadata(setup_test_db):
    paper_id = _seed_content_knowledge(setup_test_db)
    sync = TestClient(app).post("/api/content-knowledge/sync", params={"paper_id": paper_id})
    assert sync.status_code == 200
    response = TestClient(app).post(
        "/api/retrieval/search",
        json={
            "query": "mechanism conflict",
            "paper_ids": [paper_id],
            "limit": 10,
            "limit_per_type": 5,
            "rerank": False,
            "include_review_assist": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    content_items = [item for item in payload["items"] if item["source"] == "content_knowledge"]
    assert content_items
    assert all(item["source_type"] != "external_analysis_candidate" for item in content_items)
