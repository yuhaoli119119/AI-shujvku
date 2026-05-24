from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
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


def test_evidence_service_audits_claims_against_unified_evidence():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'core.db'}", future=True)
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
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'retrieval.db'}", future=True)
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
        finally:
            engine.dispose()


def test_extraction_schema_results_and_validator_warnings():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'schema.db'}", future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = _seed(session)
                response = ExtractionSchemaService(session).results(paper.id)

                assert "DFTResult" in response.schemas
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
