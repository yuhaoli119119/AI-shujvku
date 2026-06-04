from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import (
    AuditLog,
    Base,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceLocator,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperNote,
    PaperSection,
)
from app.main import app
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import (
    append_note,
    approve_correction,
    get_correction_detail,
    get_correction_queue,
    get_codex_item,
    get_dft_review_queue,
    get_paper_knowledge,
    get_parse_status,
    ingest_pdf_batch,
    insert_word_citation,
    list_notes,
    parse_paper,
    propose_correction,
    propose_dft_result_correction,
    reject_dft_result,
    query_papers,
    reject_correction,
    scan_local_pdfs,
)


@pytest.fixture
def mcp_test_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "mcp_test.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv(
            "LITAI_MCP_API_KEYS",
            "claude|Claude Desktop|litmcp_claude|read_papers,append_notes,propose_corrections,request_parse;"
            "admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections",
        )
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(Path(tmpdir) / "storage"))
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

        yield {"sessionmaker": SessionLocal, "engine": engine, "tmpdir": Path(tmpdir)}

        engine.dispose()
        from app.db.session import _engines, _session_factories

        for eng in list(_engines.values()):
            try:
                eng.dispose()
            except Exception:
                pass
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def _auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="claude",
        display_name="Claude Desktop",
        capabilities=frozenset({"read_papers", "append_notes", "propose_corrections", "request_parse"}),
        raw_key="litmcp_claude",
    )


def _admin_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="admin",
        display_name="Admin",
        capabilities=frozenset(
            {"read_papers", "append_notes", "propose_corrections", "request_parse", "review_corrections"}
        ),
        raw_key="litmcp_admin",
    )


def test_mcp_query_note_and_correction_workflow(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(
            doi="10.1000/test-doi",
            title="MCP Test Paper",
            journal="Nature Energy",
            year=2025,
            authors=["Alice", "Bob"],
            pdf_path="test.pdf",
        )
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        result = query_papers(q="MCP Test", limit=10)
        assert result["returned"] == 1
        assert result["items"][0]["title"] == "MCP Test Paper"

        note = append_note(
            paper_id=paper_id,
            content="The adsorption energy sentence should be rechecked.",
            field_name="dft_results_items",
            page=5,
            section_title="Results and Discussion",
            quoted_text="The adsorption energy of Li2S4 is -1.23 eV.",
        )
        assert note["source"] == "claude"
        assert note["page"] == 5

        notes = list_notes(paper_id=paper_id)
        assert len(notes["items"]) == 1
        assert notes["items"][0]["quoted_text"] == "The adsorption energy of Li2S4 is -1.23 eV."

        correction = propose_correction(
            paper_id=paper_id,
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="Updated abstract text",
            reason="Cross-check against the uploaded PDF abstract.",
            evidence_payload={"page": 1, "section_title": "Abstract"},
        )
        assert correction["status"] == "pending"
        assert correction["target_path"] == "abstract"

    with Session(mcp_test_env["engine"]) as session:
        saved_notes = session.scalars(select(PaperNote)).all()
        saved_corrections = session.scalars(select(PaperCorrection)).all()
        audit_logs = session.scalars(select(AuditLog).order_by(AuditLog.created_at.asc())).all()

        assert len(saved_notes) == 1
        assert len(saved_corrections) == 1
        assert [item.action for item in audit_logs] == ["append_note", "propose_correction"]


def test_mcp_get_codex_item_returns_low_token_dft_context(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Codex Item Paper", pdf_path="codex-item.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="formation_energy",
            value=7.5,
            unit="eV",
            evidence_text="The reported defect formation energy is 7.5 eV.",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(row.id),
                field_name="value",
                page=5,
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.9,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        payload = get_codex_item(
            paper_id=paper_id,
            item_type="dft_result",
            item_id=row_id,
        )

    assert payload["schema_version"] == "codex_item_context_v1"
    assert payload["item_type"] == "dft_result"
    assert payload["context"]["item"]["value"] == 7.5
    assert payload["context"]["export_safety"]["blocked_reasons"] == ["missing_review"]
    assert payload["context"]["evidence_locators"]["items"][0]["page"] == 5


def test_mcp_get_dft_review_queue_returns_codex_ready_candidates(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP DFT Queue Paper", doi="10.1000/dft-queue", year=2025, pdf_path="queue.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="reaction_barrier",
            adsorbate="vacancy",
            value=1.3,
            unit="eV",
            reaction_step="single vacancy migration",
            evidence_text="The migration barrier for the vacancy is 1.3 eV.",
            confidence=0.88,
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(row.id),
                field_name="value",
                page=7,
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.93,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        payload = get_dft_review_queue(paper_id=paper_id, limit=10)

    assert payload["metadata"]["schema_version"] == "dft_review_queue_v1"
    assert payload["metadata"]["blocked_count"] == 1
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["record_id"] == row_id
    assert row["blocked_reasons"] == ["missing_review"]
    assert row["recommended_action"] == "verify_against_pdf"
    assert row["sanity_flags"] == []
    assert row["can_mark_verified"] is True
    assert row["evidence_locators"][0]["page"] == 7
    assert row["codex_item_url"].endswith(f"/codex-item/dft_result/{row_id}")
    assert row["correction_url"].endswith(f"/dft-results/{row_id}/corrections")


def test_mcp_get_paper_knowledge_returns_section_fallback_candidates(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(
            title="MCP Knowledge Fallback Paper",
            abstract="Graphene vacancy defects alter adsorption and electronic structure.",
            pdf_path="knowledge.pdf",
        )
        session.add(paper)
        session.flush()
        session.add_all(
            [
                PaperNote(
                    paper_id=paper.id,
                    source="claude",
                    field_name="mechanism",
                    content="Check the vacancy adsorption mechanism before citing.",
                    quoted_text="vacancy defects alter adsorption",
                    page=1,
                ),
            ]
        )
        session.add(
            PaperSection(
                paper_id=paper.id,
                section_title="Results and Discussion",
                section_type="results",
                text="Vacancy defects alter adsorption energy and charge density around the defect site.",
                page_start=4,
                page_end=5,
            )
        )
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        payload = get_paper_knowledge(paper_id=paper_id, max_candidates=10)

    assert payload["schema_version"] == "paper_knowledge_context_v1"
    assert payload["metadata"]["returned"] >= 2
    categories = {item["category"] for item in payload["candidates"]}
    assert "mechanism_context" in categories
    assert any(item["source_type"] == "paper_note" for item in payload["candidates"])
    assert payload["reliability_policy"]["knowledge_items_are_candidates"] is True


def test_mcp_insert_word_citation_creates_guarded_docx_copy(mcp_test_env):
    docx_path = mcp_test_env["tmpdir"] / "draft.docx"
    document = Document()
    document.add_paragraph("Draft manuscript body.")
    document.save(docx_path)

    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(
            title="MCP Word Citation Paper",
            year=2026,
            journal="Citation Journal",
            authors=[{"last": "Word"}],
            abstract="Graphene defect citation context.",
            pdf_path="word.pdf",
        )
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        payload = insert_word_citation(
            docx_path=str(docx_path),
            selected_paper_id=paper_id,
            text="Graphene defects alter adsorption behavior.",
            output_filename="mcp-word-citation.docx",
        )

    assert payload["status"] == "inserted"
    assert payload["output_filename"] == "mcp-word-citation.docx"
    assert payload["safety"]["mutates_original_file"] is False
    assert payload["safety"]["writes_database"] is False
    paragraphs = [paragraph.text for paragraph in Document(payload["output_path"]).paragraphs]
    assert paragraphs[0] == "Draft manuscript body."
    assert "[DRAFT CITATION - VERIFY SOURCE BEFORE USE: Word, 2026]" in paragraphs[-1]


def test_admin_mcp_reject_dft_result_leaves_active_queue(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Reject DFT Candidate", pdf_path="reject-dft.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="limiting_potential",
            adsorbate="[22]",
            value=436.0,
            unit="e",
            evidence_text="Reference-like artifact was parsed as a DFT result.",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(row.id),
                field_name="value",
                page=4,
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.9,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_admin_auth()):
        rejected = reject_dft_result(
            paper_id=paper_id,
            dft_result_id=row_id,
            confirm_reject_candidate=True,
            reviewer_note="Reject citation-like DFT artifact.",
        )
        active_queue = get_dft_review_queue(paper_id=paper_id)
        rejected_queue = get_dft_review_queue(paper_id=paper_id, status="rejected")

    assert rejected["export_safety"]["review_status"] == "rejected"
    assert rejected["export_safety"]["blocked_reasons"] == ["unsafe_review"]
    assert active_queue["rows"] == []
    assert rejected_queue["rows"][0]["record_id"] == row_id
    assert rejected_queue["rows"][0]["decision_status"] == "rejected"


def test_mcp_propose_dft_result_correction_enters_review_queue(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP DFT Correction Target", pdf_path="dft-correction.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="limiting_potential",
            adsorbate="ORR",
            value=0.66,
            unit="e",
            evidence_text="The limiting potential is 0.66 V.",
            confidence=0.81,
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        with pytest.raises(ValueError):
            propose_dft_result_correction(
                paper_id=paper_id,
                dft_result_id=row_id,
                field_name="unit",
                proposed_value="V",
                reason="The source table reports potential in volts.",
                confirm_correction_proposal=False,
            )
        correction = propose_dft_result_correction(
            paper_id=paper_id,
            dft_result_id=row_id,
            field_name="unit",
            proposed_value="V",
            reason="The source table reports potential in volts.",
            confirm_correction_proposal=True,
            evidence_payload={"page": 6, "quoted_text": "The limiting potential is 0.66 V."},
        )

    assert correction["status"] == "pending"
    assert correction["field_name"] == "dft_results"
    assert correction["target_path"] == f"dft_results:{row_id}:unit"
    assert correction["proposed_value"] == "V"

    with mcp_auth_context(_admin_auth()):
        approved = approve_correction(correction["id"])
        assert approved["status"] == "approved"

    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(DFTResult, UUID(row_id))
        assert updated is not None
        assert updated.unit == "V"
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "propose_dft_result_correction"))
        assert audit is not None
        assert audit.target_id == correction["id"]


def test_admin_mcp_review_flow_applies_or_rejects_corrections(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Review Target", abstract="Old abstract", pdf_path="review.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        first = propose_correction(
            paper_id=paper_id,
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="Approved abstract",
            reason="Better aligned with PDF abstract.",
        )
        second = propose_correction(
            paper_id=paper_id,
            field_name="title",
            target_path="title",
            operation="replace",
            proposed_value="Rejected title",
            reason="This one should be rejected.",
        )

    with mcp_auth_context(_admin_auth()):
        queue = get_correction_queue()
        assert len(queue["items"]) == 2

        approved = approve_correction(first["id"])
        assert approved["status"] == "approved"
        assert approved["reviewed_by"] == "admin"

        rejected = reject_correction(second["id"], reason="Not supported by source PDF.")
        assert rejected["status"] == "rejected"
        assert rejected["reviewed_by"] == "admin"

    with Session(mcp_test_env["engine"]) as session:
        paper = session.get(Paper, UUID(first["paper_id"]))
        assert paper is not None
        assert paper.abstract == "Approved abstract"
        assert paper.title == "Review Target"

        logs = session.scalars(select(AuditLog).order_by(AuditLog.created_at.asc())).all()
        actions = [log.action for log in logs]
        assert "approve_correction" in actions
        assert "reject_correction" in actions


def test_admin_mcp_review_flow_applies_dft_result_patch(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="DFT Review Target", pdf_path="dft-review.pdf")
        session.add(paper)
        session.flush()
        dft_result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            confidence=0.82,
        )
        session.add(dft_result)
        session.commit()
        paper_id = str(paper.id)
        dft_result_id = str(dft_result.id)

    with mcp_auth_context(_auth()):
        correction = propose_correction(
            paper_id=paper_id,
            field_name="dft_results",
            target_path=f"dft_results:{dft_result_id}:value",
            operation="replace",
            proposed_value=-1.45,
            reason="Cross-check with Table 2 gives a corrected adsorption energy.",
            evidence_payload={
                "page": 6,
                "section_title": "DFT Results",
                "quoted_text": "Li2S4 adsorption energy on Fe-N4 is -1.45 eV.",
            },
        )
        assert correction["status"] == "pending"

    with mcp_auth_context(_admin_auth()):
        approved = approve_correction(correction["id"])
        assert approved["status"] == "approved"

    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(DFTResult, UUID(dft_result_id))
        assert updated is not None
        assert updated.value == -1.45


def test_admin_mcp_review_flow_applies_mechanism_claim_patch(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Mechanism Review Target", pdf_path="mechanism-review.pdf")
        session.add(paper)
        session.flush()
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="shuttle_suppression",
            claim_text="Fe-N4 suppresses the shuttle effect.",
            evidence_types=["electrochem"],
            confidence=0.61,
        )
        session.add(claim)
        session.commit()
        paper_id = str(paper.id)
        claim_id = str(claim.id)

    with mcp_auth_context(_auth()):
        correction = propose_correction(
            paper_id=paper_id,
            field_name="mechanism_claims",
            target_path=f"mechanism_claims:{claim_id}:claim_text",
            operation="replace",
            proposed_value="Fe-N4 is associated with reduced shuttle behavior under the reported test conditions.",
            reason="The original wording overstates causality compared with the source text.",
        )

    with mcp_auth_context(_admin_auth()):
        detail = get_correction_detail(correction["id"])
        assert detail["current_value"] == "Fe-N4 suppresses the shuttle effect."
        assert detail["proposed_value"] == "Fe-N4 is associated with reduced shuttle behavior under the reported test conditions."

        approved = approve_correction(correction["id"])
        assert approved["status"] == "approved"

    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(MechanismClaim, UUID(claim_id))
        assert updated is not None
        assert (
            updated.claim_text
            == "Fe-N4 is associated with reduced shuttle behavior under the reported test conditions."
        )


def test_http_correction_detail_returns_current_value_for_structured_targets(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Detail Target", pdf_path="detail.pdf")
        session.add(paper)
        session.flush()
        perf = ElectrochemicalPerformance(
            paper_id=paper.id,
            capacity_value=873.0,
            cycle_number=100,
            rate="0.5 C",
            evidence_text="873 mAh g-1 at 100 cycles",
        )
        session.add(perf)
        session.flush()
        correction = PaperCorrection(
            paper_id=paper.id,
            source="claude",
            field_name="electrochemical_performance",
            target_path=f"electrochemical_performance:{perf.id}:capacity_value",
            operation="replace",
            proposed_value=892.0,
            reason="Figure annotation shows 892 mAh g-1 instead of 873.",
            status="pending",
        )
        session.add(correction)
        session.commit()
        correction_id = str(correction.id)

    client = TestClient(app)
    response = client.get(
        f"/api/corrections/{correction_id}",
        headers={"Authorization": "Bearer litmcp_admin"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["current_value"] == 873.0
    assert payload["proposed_value"] == 892.0
    assert payload["target_exists"] is True


@pytest.mark.anyio
async def test_scan_local_pdfs_and_ingest_pdf_batch(mcp_test_env, monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        folder = Path(tmpdir)
        first_pdf = folder / "paper_a.pdf"
        second_pdf = folder / "paper_b.pdf"
        first_pdf.write_bytes(b"%PDF-1.4 first")
        second_pdf.write_bytes(b"%PDF-1.4 second")

        with Session(mcp_test_env["engine"]) as session:
            existing = Paper(
                title="Already Parsed",
                pdf_path="stored_existing.pdf",
                source_path=str(first_pdf.resolve()),
            )
            session.add(existing)
            session.commit()
            existing_id = str(existing.id)

        async def fake_ingest_pdf(self, source_path, original_filename, copy_pdf=True, external_metadata=None, source_reference=None):
            paper = Paper(
                title=f"Parsed {original_filename}",
                pdf_path=f"stored_{original_filename}",
                source_path=source_reference,
            )
            self.session.add(paper)
            self.session.commit()
            self.session.refresh(paper)
            return paper

        monkeypatch.setattr("app.services.paper_ingestion.PaperIngestionService.ingest_pdf", fake_ingest_pdf)

        with mcp_auth_context(_auth()):
            scan = scan_local_pdfs(folder_path=str(folder), recursive=False, limit=10)
            assert scan["returned"] == 2
            existing_items = [item for item in scan["items"] if item["already_ingested"]]
            pending_items = [item for item in scan["items"] if not item["already_ingested"]]
            assert len(existing_items) == 1
            assert existing_items[0]["paper_id"] == existing_id
            assert len(pending_items) == 1
            assert pending_items[0]["filename"] == "paper_b.pdf"

            batch = await ingest_pdf_batch(
                folder_path=str(folder),
                recursive=False,
                limit=10,
                only_unparsed=True,
            )
            assert batch["requested"] == 2
            statuses = {item["path"]: item["status"] for item in batch["results"]}
            assert statuses[str(first_pdf.resolve())] == "already_ingested"
            assert statuses[str(second_pdf.resolve())] == "completed"

        with Session(mcp_test_env["engine"]) as session:
            rows = session.scalars(select(Paper).order_by(Paper.created_at.asc())).all()
            assert len(rows) == 2
            assert any(row.source_path == str(second_pdf.resolve()) for row in rows)


@pytest.mark.anyio
async def test_parse_paper_reuses_existing_paper_and_records_job(mcp_test_env, monkeypatch):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(
            doi="10.1000/existing-doi",
            title="Existing Paper",
            pdf_path="existing.pdf",
        )
        session.add(paper)
        session.commit()
        existing_paper_id = str(paper.id)

    class FakeDiscoveryService:
        def fetch_metadata(self, identifier, providers=None):
            return object(), {
                "doi": "10.1000/existing-doi",
                "title": "Existing Paper",
                "authors": [],
                "providers": providers or [],
            }

    monkeypatch.setattr("app.mcp.server.DiscoveryService", FakeDiscoveryService)

    with mcp_auth_context(_auth()):
        job = await parse_paper(identifier="10.1000/existing-doi", providers=["openalex"])
        assert job["status"] == "completed"
        assert job["paper_id"] == existing_paper_id

        fetched = get_parse_status(job_id=job["id"])
        assert fetched["identifier"] == "10.1000/existing-doi"
        assert fetched["status"] == "completed"


def test_mcp_http_auth_middleware_requires_api_key(mcp_test_env):
    with TestClient(app) as client:
        response = client.get("/mcp")
        assert response.status_code == 401
        assert response.json()["detail"] == "Missing MCP API key"

        authorized = client.get("/mcp", headers={"Authorization": "Bearer litmcp_claude"})
        assert authorized.status_code != 401


def test_http_correction_review_api_requires_admin_and_applies_update(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="HTTP Review Target", abstract="Initial abstract", pdf_path="http-review.pdf")
        session.add(paper)
        session.flush()
        correction = PaperCorrection(
            paper_id=paper.id,
            source="claude",
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="HTTP approved abstract",
            reason="Reviewed against source abstract.",
            status="pending",
        )
        session.add(correction)
        session.commit()
        correction_id = str(correction.id)
        paper_id = str(paper.id)

    client = TestClient(app)
    forbidden = client.post(
        f"/api/corrections/{correction_id}/approve",
        headers={"Authorization": "Bearer litmcp_claude"},
    )
    assert forbidden.status_code == 403

    approved = client.post(
        f"/api/corrections/{correction_id}/approve",
        headers={"Authorization": "Bearer litmcp_admin"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    queue = client.get(
        "/api/corrections",
        headers={"Authorization": "Bearer litmcp_admin"},
    )
    assert queue.status_code == 200
    assert queue.json() == []

    with Session(mcp_test_env["engine"]) as session:
        paper = session.get(Paper, UUID(paper_id))
        assert paper is not None
        assert paper.abstract == "HTTP approved abstract"
