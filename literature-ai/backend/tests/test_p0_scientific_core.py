from __future__ import annotations

import os

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, DFTResult, ElectrochemicalPerformance, MechanismClaim, Paper, PaperSection
from app.main import app
from app.services.evidence_service import EvidenceService
from app.services.extraction_schema_service import ExtractionSchemaService
from app.services.extraction_validator import ExtractionValidator
from app.services.retrieval_service import RetrievalService
from app.schemas.retrieval import RetrievalSearchRequest


def _seed(session: Session) -> Paper:
    paper = Paper(title="Fe-N4 Li-S Catalyst", pdf_path="paper.pdf", authors=["A. Researcher"])
    session.add(paper)
    session.flush()
    session.add(
        PaperSection(
            paper_id=paper.id,
            section_title="Computational Methods",
            section_type="methods",
            text="VASP with PBE and a 500 eV cutoff was used for Li2S4 adsorption calculations.",
            page_start=2,
            page_end=2,
        )
    )
    session.add(
        PaperSection(
            paper_id=paper.id,
            section_title="Results",
            section_type="results",
            text="The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV and accelerates LiPS conversion.",
            page_start=5,
            page_end=5,
        )
    )
    session.add(
        DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            source_section="Results",
            evidence_text="The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.",
            confidence=0.92,
        )
    )
    session.add(
        MechanismClaim(
            paper_id=paper.id,
            claim_type="LiPS conversion",
            claim_text="Fe-N4 accelerates LiPS conversion by strengthening Li2S4 binding.",
            evidence_types=["Li2S4"],
            evidence_text="Li2S4 binding accelerates LiPS conversion on Fe-N4.",
            confidence=0.86,
        )
    )
    session.add(
        ElectrochemicalPerformance(
            paper_id=paper.id,
            capacity_value=900,
            cycle_number=200,
            rate="0.5C",
            evidence_text="The cell delivered 900 mAh/g at 0.5C after 200 cycles.",
        )
    )
    session.commit()
    return paper


def _add_sections(session: Session, paper: Paper, count: int, prefix: str) -> None:
    for index in range(count):
        session.add(
            PaperSection(
                paper_id=paper.id,
                section_title=f"{prefix} {index + 1}",
                section_type="results",
                text=f"{prefix} section {index + 1} text",
                page_start=index + 1,
                page_end=index + 1,
            )
        )


def test_evidence_service_audits_claims_against_unified_evidence():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = _seed(session)
                service = EvidenceService(session)

                claims = service.list_claims(paper_id=paper.id, include_derived=True)
                assert any(claim.target_type == "dft_result" for claim in claims)
                assert claims[0].evidence[0].paper_id == paper.id

                audit = service.audit_text(
                    "The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.",
                    paper_ids=[paper.id],
                    min_confidence=0.1,
                )
                assert audit.ok
                assert audit.supported_claims == 1

                unsupported = service.audit_text("Fe-N4 fully eliminates every shuttle effect.", paper_ids=[paper.id])
                assert not unsupported.ok
                assert unsupported.unsupported_claims == 1
        finally:
            engine.dispose()


def test_retrieval_service_supports_focused_and_full_context_modes():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = _seed(session)
                service = RetrievalService(session)

                focused = service.search(
                    RetrievalSearchRequest(query="Li2S4 adsorption energy Fe-N4", paper_ids=[paper.id], mode="focused", limit=5)
                )
                assert focused.items
                assert focused.items[0].score_breakdown["bm25"] >= 0
                assert focused.items[0].source
                assert focused.items[0].paper_id == paper.id

                full = service.search(
                    RetrievalSearchRequest(query="ignored", paper_ids=[paper.id], mode="full_context", limit=10)
                )
                assert len(full.items) == 2
                assert all(item.source == "full_context" for item in full.items)
                assert full.items[0].chunk_id
                assert [item.section_title for item in full.items] == ["Computational Methods", "Results"]
                assert focused.items[0].source != full.items[0].source
        finally:
            engine.dispose()


def test_retrieval_service_full_context_handles_empty_results_and_duplicate_paper_ids():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = Paper(title="Empty Retrieval Paper", pdf_path="paper.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)

                service = RetrievalService(session)
                full = service.search(
                    RetrievalSearchRequest(query="ignored", paper_ids=[paper.id, paper.id], mode="full_context", limit=10)
                )
                assert full.items == []

                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Results",
                        section_type="results",
                        text="A single reviewed-agnostic full-context section.",
                        page_start=4,
                        page_end=4,
                    )
                )
                session.commit()

                deduped = service.search(
                    RetrievalSearchRequest(query="ignored", paper_ids=[paper.id, paper.id], mode="full_context", limit=10)
                )
                assert len(deduped.items) == 1
                assert deduped.items[0].section_title == "Results"
        finally:
            engine.dispose()


def test_retrieval_service_full_context_reclaims_quota_from_empty_outer_papers():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                first_empty = Paper(title="First Empty Paper", pdf_path="first-empty.pdf", authors=["A"])
                rich_paper = Paper(title="Rich Paper", pdf_path="rich.pdf", authors=["B"])
                last_empty = Paper(title="Last Empty Paper", pdf_path="last-empty.pdf", authors=["C"])
                session.add_all([first_empty, rich_paper, last_empty])
                session.flush()
                _add_sections(session, rich_paper, 20, "Rich")
                session.commit()

                service = RetrievalService(session)
                full = service.search(
                    RetrievalSearchRequest(
                        query="ignored",
                        paper_ids=[first_empty.id, rich_paper.id, last_empty.id],
                        mode="full_context",
                        limit=10,
                    )
                )
                assert len(full.items) == 10
                assert all(item.paper_id == rich_paper.id for item in full.items)
                assert [item.section_title for item in full.items] == [f"Rich {index}" for index in range(1, 11)]
        finally:
            engine.dispose()


def test_retrieval_service_full_context_redistributes_fairly_and_preserves_order():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                short_first = Paper(title="Short First", pdf_path="short-first.pdf", authors=["A"])
                rich_middle = Paper(title="Rich Middle", pdf_path="rich-middle.pdf", authors=["B"])
                short_last = Paper(title="Short Last", pdf_path="short-last.pdf", authors=["C"])
                session.add_all([short_first, rich_middle, short_last])
                session.flush()
                _add_sections(session, short_first, 2, "First")
                _add_sections(session, rich_middle, 20, "Middle")
                _add_sections(session, short_last, 1, "Last")
                session.commit()

                service = RetrievalService(session)
                full = service.search(
                    RetrievalSearchRequest(
                        query="ignored",
                        paper_ids=[short_first.id, rich_middle.id, short_last.id],
                        mode="full_context",
                        limit=10,
                    )
                )

                assert len(full.items) == 10
                assert [item.paper_id for item in full.items] == (
                    [short_first.id] * 2 + [rich_middle.id] * 7 + [short_last.id]
                )
                assert [item.section_title for item in full.items] == [
                    "First 1",
                    "First 2",
                    *[f"Middle {index}" for index in range(1, 8)],
                    "Last 1",
                ]
        finally:
            engine.dispose()


def test_retrieval_service_full_context_returns_all_sections_when_below_limit():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                papers = [
                    Paper(title=f"Paper {index}", pdf_path=f"paper-{index}.pdf", authors=["A"])
                    for index in range(1, 4)
                ]
                session.add_all(papers)
                session.flush()
                for paper, count, prefix in zip(papers, [2, 3, 1], ["First", "Second", "Third"], strict=True):
                    _add_sections(session, paper, count, prefix)
                session.commit()

                service = RetrievalService(session)
                full = service.search(
                    RetrievalSearchRequest(
                        query="ignored",
                        paper_ids=[paper.id for paper in papers],
                        mode="full_context",
                        limit=10,
                    )
                )

                assert len(full.items) == 6
                assert [item.paper_id for item in full.items] == (
                    [papers[0].id] * 2 + [papers[1].id] * 3 + [papers[2].id]
                )
                assert [item.section_title for item in full.items] == [
                    "First 1",
                    "First 2",
                    "Second 1",
                    "Second 2",
                    "Second 3",
                    "Third 1",
                ]
        finally:
            engine.dispose()


def test_retrieval_search_request_limit_boundaries_match_schema():
    assert RetrievalSearchRequest(query="q", limit=1).limit == 1
    assert RetrievalSearchRequest(query="q", limit=100).limit == 100
    with pytest.raises(ValidationError):
        RetrievalSearchRequest(query="q", limit=0)
    with pytest.raises(ValidationError):
        RetrievalSearchRequest(query="q", limit=101)


def test_retrieval_service_defaults_to_focused_mode_for_compatibility():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = _seed(session)
                service = RetrievalService(session)

                default_mode = service.search(
                    RetrievalSearchRequest(query="Li2S4 adsorption energy Fe-N4", paper_ids=[paper.id], limit=5)
                )
                assert default_mode.mode == "focused"
                assert default_mode.items
                assert all(item.source != "full_context" for item in default_mode.items)
        finally:
            engine.dispose()


def test_extraction_schema_results_and_validator_warnings():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = _seed(session)
                response = ExtractionSchemaService(session).results(paper.id)

                assert "DFTResult" in response.schemas
                assert "ProjectLibraryV4Extraction" in response.schemas
                assert response.results["ProjectLibraryV4Extraction"] == []
                project_schema = response.schemas["ProjectLibraryV4Extraction"]
                assert set(project_schema["properties"]) >= {
                    "catalyst_samples",
                    "active_site_instances",
                    "adsorbate_properties",
                    "reaction_step_properties",
                    "electronic_properties",
                    "structure_properties",
                    "ambiguous_records",
                }
                assert response.results["DFTResult"][0]["value"]["value"] == -1.23
                assert response.results["DFTResult"][0]["value"]["evidence_text"]

                bad_payload = {
                    "DFTResult": [
                        {
                            "energy_type": {"value": "adsorption_energy", "evidence_text": "x", "confidence": 0.8},
                            "value": {"value": -50, "unit": "", "evidence_text": "x", "confidence": 0.8},
                        }
                    ],
                    "ElectrochemicalPerformance": [
                        {"cycle_number": {"value": 200, "evidence_text": "x"}, "capacity": {"value": None}}
                    ],
                }
                warnings = ExtractionValidator().validate_payload(bad_payload)
                codes = {warning.code for warning in warnings}
                assert "out_of_expected_range" in codes
                assert "energy_missing_unit" in codes
                assert "cycle_without_capacity" in codes
        finally:
            engine.dispose()


def test_p0_api_routes_are_registered():
    client = TestClient(app)
    schemas = client.get("/api/extraction/schemas")
    assert schemas.status_code == 200
    assert "DFTResult" in schemas.json()
    assert "ProjectLibraryV4Extraction" in schemas.json()
