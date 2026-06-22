from __future__ import annotations

import os

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
    WritingCard,
)
from app.db.session import get_db_session
from app.main import app


CLAIM = "Single-atom catalysts can promote sulfur redox kinetics in lithium-sulfur batteries."


@pytest.fixture
def citation_client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(Path(tmpdir) / "storage"))
        get_settings.cache_clear()
        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

        def override_get_db_session():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session
        seed = _seed(Session)
        yield TestClient(app), Session, seed
        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories

        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def test_claim_returns_candidate_papers(citation_client):
    client, _, seed = citation_client
    response = _post(client)
    assert response.status_code == 200
    ids = _ids(response)
    assert str(seed["safe"]) in ids
    assert response.json()["metadata"]["search_scope"] == "all_libraries"
    assert response.json()["candidate_count"] > 0


def test_citation_candidates_filter_by_library_name(citation_client):
    client, Session, _ = citation_client
    with Session() as session:
        paper_a = Paper(
            title="Library A sulfur redox kinetics",
            abstract=CLAIM,
            pdf_path="library-a.pdf",
            library_name="库A",
        )
        paper_b = Paper(
            title="Library B sulfur redox kinetics",
            abstract=CLAIM,
            pdf_path="library-b.pdf",
            library_name="库B",
        )
        session.add_all([paper_a, paper_b])
        session.commit()
        paper_b_id = str(paper_b.id)

    response = _post(client, library_name="库A")

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["library_name"] == "库A"
    assert payload["metadata"]["search_scope"] == "library"
    assert payload["metadata"]["total_papers_considered"] == 1
    assert payload["candidates"]
    assert all(item["library_name"] == "库A" for item in payload["candidates"])
    assert paper_b_id not in _ids(response)


def test_exclude_from_citation_true_is_not_returned(citation_client):
    client, _, seed = citation_client
    response = _post(client)
    assert str(seed["do_not_cite"]) not in _ids(response)
    assert {"paper_id": str(seed["do_not_cite"]), "reason": "exclude_from_citation=true"} in response.json()["excluded_reasons"]


def test_citation_priority_exclude_is_not_returned(citation_client):
    client, _, seed = citation_client
    response = _post(client)
    assert str(seed["priority_exclude"]) not in _ids(response)
    assert {"paper_id": str(seed["priority_exclude"]), "reason": "citation_priority=exclude"} in response.json()["excluded_reasons"]


def test_citation_priority_high_improves_sorting(citation_client):
    client, _, seed = citation_client
    response = _post(client, filters={"year_min": 2020})
    assert response.status_code == 200
    assert _ids(response)[0] == str(seed["safe"])
    assert response.json()["candidates"][0]["citation_priority"] == "high"


def test_year_min_and_year_max_filters_work(citation_client):
    client, _, seed = citation_client
    response = _post(client, filters={"year_min": 2022, "year_max": 2023})
    ids = _ids(response)
    assert str(seed["safe"]) in ids
    assert str(seed["metadata_only"]) not in ids


def test_journal_include_and_exclude_filters_work(citation_client):
    client, _, seed = citation_client
    response = _post(client, filters={"journal_include": ["Energy"]})
    assert str(seed["safe"]) in _ids(response)
    assert str(seed["pending_locator"]) not in _ids(response)

    response = _post(client, filters={"journal_exclude": ["Energy"]})
    assert str(seed["safe"]) not in _ids(response)


def test_impact_factor_min_applies_to_existing_if(citation_client):
    client, _, seed = citation_client
    response = _post(client, filters={"impact_factor_min": 10})
    ids = _ids(response)
    assert str(seed["safe"]) in ids
    assert str(seed["low_if"]) not in ids
    assert any(item["reason"] == "impact_factor_below_min" for item in response.json()["excluded_reasons"])


def test_missing_if_is_not_treated_as_zero_and_is_reported(citation_client):
    client, _, seed = citation_client
    response = _post(client, filters={"impact_factor_min": 10})
    ids = _ids(response)
    assert str(seed["missing_if"]) not in ids
    assert {"paper_id": str(seed["missing_if"]), "reason": "needs_metadata_excluded_by_impact_factor_min"} in response.json()["excluded_reasons"]
    assert response.json()["warnings"]


def test_missing_if_candidate_keeps_needs_metadata_status_without_if_filter(citation_client):
    client, _, seed = citation_client
    response = _post(client)
    candidate = _candidate(response, seed["missing_if"])
    assert candidate["impact_factor"] is None
    assert candidate["impact_factor_status"] == "needs_metadata"
    assert "impact_factor_needs_metadata" in candidate["warnings"]


def test_safe_verified_evidence_can_be_confirmed_only_with_safe_gate(citation_client):
    client, _, seed = citation_client
    response = _post(client)
    candidate = _candidate(response, seed["safe"])
    assert candidate["evidence_status"] == "safe_verified"
    assert candidate["can_be_used_as_confirmed_citation"] is True
    assert candidate["requires_human_verification"] is False


def test_pending_review_with_locator_requires_human_verification(citation_client):
    client, _, seed = citation_client
    candidate = _candidate(_post(client), seed["pending_locator"])
    assert candidate["evidence_status"] == "pending_with_locator"
    assert candidate["requires_human_verification"] is True
    assert candidate["can_be_used_as_confirmed_citation"] is False
    assert "suggestion_only_needs_human_verification" in candidate["warnings"]


def test_pending_review_without_locator_is_marked(citation_client):
    client, _, seed = citation_client
    candidate = _candidate(_post(client), seed["pending_no_locator"])
    assert candidate["evidence_status"] == "pending_without_locator"
    assert candidate["requires_human_verification"] is True


def test_unverified_extraction_is_marked_as_suggestion(citation_client):
    client, _, seed = citation_client
    candidate = _candidate(_post(client), seed["unverified_extraction"])
    assert candidate["evidence_status"] == "unverified_extraction"
    assert candidate["requires_human_verification"] is True
    assert candidate["can_be_used_as_confirmed_citation"] is False


def test_structured_non_dft_rows_feed_citation_recommendations(citation_client):
    client, Session, _ = citation_client
    with Session() as session:
        paper = _paper("Structured non-DFT citation", 2026, "Journal of Energy Chemistry", "structured evidence", "structured.pdf")
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4 catalyst",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
            support="N-doped carbon",
            evidence_strength="Fe-N4 catalyst on N-doped carbon accelerates LiPS conversion.",
        )
        mechanism = MechanismClaim(
            paper_id=paper.id,
            claim_type="lips_conversion",
            claim_text="Fe-N4 sites accelerate LiPS conversion by strengthening Li2S4 binding.",
            evidence_types=["Li2S4 binding"],
            evidence_text="Li2S4 binding supports faster LiPS conversion.",
        )
        electrochemical = ElectrochemicalPerformance(
            paper_id=paper.id,
            capacity_value=900,
            rate="0.5C",
            cycle_number=200,
            evidence_text="The cell retained 900 mAh/g at 0.5C after 200 cycles.",
        )
        writing_card = WritingCard(
            paper_id=paper.id,
            research_gap="single-atom LiPS conversion needs better catalyst identity tracking",
            proposed_solution="Use Fe-N4 coordination to connect mechanism and electrochemical performance.",
            core_hypothesis="Fe-N4 coordination improves sulfur redox kinetics.",
        )
        session.add_all([catalyst, mechanism, electrochemical, writing_card])
        session.commit()
        paper_id = paper.id

    response = client.post(
        "/api/writing/citation-candidates",
        json={
            "text": "Fe-N4 coordination Li2S4 binding 900 mAh/g 0.5C sulfur redox kinetics",
            "max_candidates": 5,
            "include_unverified_suggestions": True,
            "include_pending_review": True,
        },
    )

    assert response.status_code == 200
    candidate = _candidate(response, paper_id)
    assert candidate["evidence_status"] == "unverified_extraction"
    snippet_types = {item["source_type"] for item in candidate["supporting_snippets"]}
    assert {
        "catalyst_samples",
        "mechanism_claims",
        "electrochemical_performance",
    } & snippet_types
    assert any(item["source_id"] for item in candidate["supporting_snippets"])


def test_metadata_only_cannot_pose_as_evidence(citation_client):
    client, _, seed = citation_client
    candidate = _candidate(_post(client), seed["metadata_only"])
    assert candidate["evidence_status"] == "metadata_only"
    assert candidate["requires_human_verification"] is True
    assert candidate["can_be_used_as_confirmed_citation"] is False
    assert "metadata-only" in candidate["reason"]


def test_repaired_locator_does_not_equal_verified(citation_client):
    client, _, seed = citation_client
    candidate = _candidate(_post(client), seed["repaired_locator"])
    assert candidate["evidence_status"] == "pending_with_locator"
    assert candidate["can_be_used_as_confirmed_citation"] is False


def test_api_does_not_write_database(citation_client):
    client, Session, _ = citation_client
    with Session() as session:
        before = _counts(session)
    response = _post(client, filters={"year_min": 2020, "impact_factor_min": 1})
    assert response.status_code == 200
    with Session() as session:
        assert _counts(session) == before


def test_api_does_not_modify_guarded_tables(citation_client):
    client, Session, seed = citation_client
    with Session() as session:
        before = _guard_snapshot(session, seed["safe"])
    response = _post(client)
    assert response.status_code == 200
    with Session() as session:
        assert _guard_snapshot(session, seed["safe"]) == before


def test_service_does_not_call_mark_verified_or_export_writer_paths():
    backend_root = Path(__file__).resolve().parents[1]
    source = (backend_root / "app/services/writing_citation_candidate_service.py").read_text(encoding="utf-8")
    api_source = (backend_root / "app/api/writing.py").read_text(encoding="utf-8")
    combined = source + api_source
    assert "mark_verified" not in combined
    assert "draft_paper_sections" not in combined
    assert "Writer(" not in combined
    assert "/api/export" not in combined.lower()
    assert "ExportService" not in combined


def test_empty_or_too_short_text_returns_400(citation_client):
    client, _, _ = citation_client
    response = client.post("/api/writing/citation-candidates", json={"text": "and the"})
    assert response.status_code == 400
    assert "at least two searchable terms" in response.json()["detail"]


def test_max_candidates_limit_is_enforced(citation_client):
    client, _, _ = citation_client
    response = _post(client, max_candidates=2)
    assert response.status_code == 200
    assert response.json()["candidate_count"] == 2
    assert len(response.json()["candidates"]) == 2


def _post(client, *, filters=None, max_candidates=10, library_name=None):
    return client.post(
        "/api/writing/citation-candidates",
        json={
            "text": CLAIM,
            "max_candidates": max_candidates,
            "library_name": library_name,
            "filters": filters or {},
            "include_unverified_suggestions": True,
            "include_pending_review": True,
        },
    )


def _seed(Session):
    with Session() as session:
        safe = _paper("Safe High", 2023, "Energy Materials", "single-atom sulfur redox kinetics lithium sulfur", "safe.pdf")
        pending_locator = _paper("Pending Locator", 2024, "Catalysis Letters", "single-atom catalysts promote sulfur redox kinetics", "pending.pdf")
        pending_no_locator = _paper("Pending No Locator", 2024, "Catalysis Letters", "sulfur redox kinetics in batteries", "pending2.pdf")
        unverified_extraction = _paper("Extraction Match", 2022, "Battery Reports", "battery catalysis", "extract.pdf")
        metadata_only = _paper("Single-atom sulfur redox kinetics overview", 2019, "Old Energy", "lithium sulfur battery catalyst metadata", "meta.pdf")
        do_not_cite = _paper("Excluded Flag", 2024, "Energy Materials", "single-atom sulfur redox kinetics", "x.pdf")
        priority_exclude = _paper("Excluded Priority", 2024, "Energy Materials", "single-atom sulfur redox kinetics", "px.pdf")
        low_if = _paper("Low IF", 2024, "Low Impact Journal", "single-atom sulfur redox kinetics", "low.pdf")
        missing_if = _paper("Missing IF", 2024, "Missing Impact Journal", "single-atom sulfur redox kinetics", "missing.pdf")
        repaired_locator = _paper("Repaired Locator", 2024, "Repair Journal", "single-atom sulfur redox kinetics", "repair.pdf")
        session.add_all(
            [
                safe,
                pending_locator,
                pending_no_locator,
                unverified_extraction,
                metadata_only,
                do_not_cite,
                priority_exclude,
                low_if,
                missing_if,
                repaired_locator,
            ]
        )
        session.flush()
        session.add_all(
            [
                PaperCitationEligibility(paper_id=safe.id, citation_priority="high"),
                PaperCitationEligibility(paper_id=do_not_cite.id, exclude_from_citation=True, exclude_reason="manual"),
                PaperCitationEligibility(paper_id=priority_exclude.id, citation_priority="exclude"),
                PaperImpactMetadata(paper_id=safe.id, impact_factor=15.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=pending_locator.id, impact_factor=8.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=pending_no_locator.id, impact_factor=9.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=unverified_extraction.id, impact_factor=7.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=metadata_only.id, impact_factor=12.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=do_not_cite.id, impact_factor=20.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=priority_exclude.id, impact_factor=20.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=low_if.id, impact_factor=2.0, impact_factor_source="fixture", impact_factor_year=2024),
                PaperImpactMetadata(paper_id=repaired_locator.id, impact_factor=11.0, impact_factor_source="fixture", impact_factor_year=2024),
            ]
        )
        safe_result = DFTResult(
            paper_id=safe.id,
            property_type="sulfur redox kinetics",
            value=1.0,
            unit="a.u.",
            evidence_text="Single-atom catalysts promote sulfur redox kinetics in lithium-sulfur batteries.",
        )
        pending_result = DFTResult(
            paper_id=pending_locator.id,
            property_type="sulfur redox kinetics",
            value=2.0,
            unit="a.u.",
            evidence_text="Single-atom catalyst evidence for sulfur redox kinetics requires review.",
        )
        pending_no_locator_result = DFTResult(
            paper_id=pending_no_locator.id,
            property_type="sulfur redox kinetics",
            value=3.0,
            unit="a.u.",
            evidence_text="Pending evidence says sulfur redox kinetics improve.",
        )
        extraction_result = DFTResult(
            paper_id=unverified_extraction.id,
            property_type="sulfur redox kinetics",
            value=4.0,
            unit="a.u.",
            evidence_text="Unreviewed extraction mentions single-atom sulfur redox kinetics.",
        )
        repaired_result = DFTResult(
            paper_id=repaired_locator.id,
            property_type="sulfur redox kinetics",
            value=5.0,
            unit="a.u.",
            evidence_text="Repaired locator text mentions single-atom sulfur redox kinetics.",
        )
        session.add_all([safe_result, pending_result, pending_no_locator_result, extraction_result, repaired_result])
        session.flush()
        session.add_all(
            [
                _review(safe, safe_result, "verified", "active"),
                _review(pending_locator, pending_result, "pending", "active"),
                _review(pending_no_locator, pending_no_locator_result, "pending", "active"),
                _review(repaired_locator, repaired_result, "pending", "active"),
                _locator(pending_locator, pending_result, "page_only", 5),
                _locator(repaired_locator, repaired_result, "exact", 7),
                _locator(safe, safe_result, "exact", 3),
            ]
        )
        session.commit()
        return {
            "safe": safe.id,
            "pending_locator": pending_locator.id,
            "pending_no_locator": pending_no_locator.id,
            "unverified_extraction": unverified_extraction.id,
            "metadata_only": metadata_only.id,
            "do_not_cite": do_not_cite.id,
            "priority_exclude": priority_exclude.id,
            "low_if": low_if.id,
            "missing_if": missing_if.id,
            "repaired_locator": repaired_locator.id,
        }


def _paper(title, year, journal, abstract, pdf_path):
    return Paper(title=title, year=year, journal=journal, abstract=abstract, pdf_path=pdf_path)


def _review(paper, result, reviewer_status, target_resolution_status):
    return ExtractionFieldReview(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(result.id),
        field_name="value",
        original_value=result.value,
        reviewed_value=result.value,
        evidence_text=result.evidence_text,
        reviewer_status=reviewer_status,
        target_resolution_status=target_resolution_status,
    )


def _locator(paper, result, locator_status, page):
    return EvidenceLocator(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(result.id),
        source_type="text",
        page=page,
        evidence_text=result.evidence_text,
        locator_status=locator_status,
        locator_confidence=0.8,
        parser_source="fixture",
    )


def _ids(response):
    return [item["paper_id"] for item in response.json()["candidates"]]


def _candidate(response, paper_id):
    for item in response.json()["candidates"]:
        if item["paper_id"] == str(paper_id):
            return item
    raise AssertionError(f"candidate not found: {paper_id}")


def _counts(session):
    return {
        "papers": session.scalar(select(func.count(Paper.id))),
        "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
        "locators": session.scalar(select(func.count(EvidenceLocator.id))),
        "eligibility": session.scalar(select(func.count(PaperCitationEligibility.paper_id))),
        "impact": session.scalar(select(func.count(PaperImpactMetadata.paper_id))),
    }


def _guard_snapshot(session, paper_id):
    paper = session.get(Paper, paper_id)
    review = session.scalar(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id))
    locator = session.scalar(select(EvidenceLocator).where(EvidenceLocator.paper_id == paper_id))
    eligibility = session.get(PaperCitationEligibility, paper_id)
    impact = session.get(PaperImpactMetadata, paper_id)
    return {
        "paper": (paper.title, paper.year, paper.journal, paper.abstract, paper.pdf_path),
        "review": (review.reviewer_status, review.target_resolution_status, review.evidence_text),
        "locator": (locator.locator_status, locator.page, locator.evidence_text),
        "eligibility": (eligibility.exclude_from_citation, eligibility.citation_priority),
        "impact": (impact.impact_factor, impact.impact_factor_year, impact.impact_factor_source),
    }
