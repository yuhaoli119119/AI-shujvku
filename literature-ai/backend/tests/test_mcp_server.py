from __future__ import annotations

from copy import deepcopy
import asyncio
import base64
import hashlib
import json
import os

import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import fitz
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import (
    AuditLog,
    Base,
    CatalystSample,
    ContentWebReviewBundleV2,
    ContentWebReviewLocalVerificationResult,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceLocator,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    MechanismClaim,
    ModuleWriteLock,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperNote,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.main import app
from app.mcp.auth import parse_mcp_api_keys, validate_mcp_capability_assignments
from app.mcp.context import MCPAuthInfo, get_mcp_auth, mcp_auth_context, reset_mcp_auth, set_mcp_auth
from app.mcp.server import (
    append_note,
    approve_correction,
    apply_analysis_review_rules,
    apply_content_web_review_local_verification,
    acquire_module_write_lock,
    get_correction_detail,
    get_codex_context,
    get_correction_queue,
    get_codex_item,
    get_dft_review_queue,
    get_ai_verification_tasks,
    get_review_coverage,
    get_paper_knowledge,
    get_content_web_review_local_verification_plan,
    get_parse_status,
    ingest_pdf_batch,
    import_analysis,
    list_notes,
    parse_paper,
    propose_correction,
    propose_dft_result_correction,
    reject_dft_result,
    release_module_write_lock,
    review_figure,
    read_content_web_review_page_asset,
    query_papers,
    reject_correction,
    scan_local_pdfs,
    scan_duplicate_dois,
    submit_ai_verification_batch,
    mcp_server,
    _mcp_review_identity,
)
from app.services.paper_query import PaperQueryService
from app.services.dft_review_bundle_service import DFTReviewBundleService
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from app.services.content_web_review_bundle_v2_service import ContentWebReviewBundleV2Service
from app.utils.library_names import DEFAULT_LIBRARY_NAME
from app.utils.ai_verification import ai_target_fingerprint


def _validated_local_ai_dft_request(engine, paper_id: str, audits: list[dict]) -> dict:
    paper_uuid = UUID(str(paper_id))
    normalized_audits = deepcopy(audits)
    with Session(engine) as session:
        paper = session.get(Paper, paper_uuid)
        assert paper is not None
        if not paper.paper_code:
            paper.paper_code = f"MCP-{str(paper.id)[:8]}"
        if session.query(PaperSection).filter(PaperSection.paper_id == paper.id).count() == 0:
            session.add(
                PaperSection(
                    paper_id=paper.id,
                    section_title="DFT evidence",
                    section_type="results",
                    text="DFT evidence " + json.dumps(normalized_audits, ensure_ascii=False, default=str),
                    page_start=1,
                    page_end=10,
                )
            )
        if any(audit.get("decision") == "new_candidate" for audit in normalized_audits):
            evidence = normalized_audits[0].get("evidence_location") or {}
            session.add(
                PaperTable(
                    paper_id=paper.id,
                    caption=str(evidence.get("table") or evidence.get("figure") or "Reviewed DFT evidence"),
                    markdown_content="| material | property | value |\n|---|---|---|\n| test | DFT | reviewed |",
                    page=int(evidence.get("page") or 1),
                    extraction_source="unit_test_reviewed_evidence",
                )
            )
        session.add(paper)
        session.commit()

    if any(audit.get("decision") == "new_candidate" for audit in normalized_audits):
        with Session(engine) as session:
            settings = get_settings()
            state = DFTReviewBundleService(session, settings).get_figure_table_review_state(paper_uuid)
            materials = EvidenceReviewBundleService(session, settings)._build_materials(paper_uuid)
            for table_id in materials["table_id_map"]:
                session.add(
                    AuditLog(
                        paper_id=paper_uuid,
                        action="KEEP",
                        source="test_local_ai",
                        target_type="paper_table",
                        target_id=str(table_id),
                        payload={"action": "KEEP", "actor_type": "local_ai"},
                    )
                )
            session.add(
                AuditLog(
                    paper_id=paper_uuid,
                    action="offline_evidence_review_applied",
                    source="test_local_ai",
                    target_type="offline_evidence_review",
                    target_id=state["current_snapshot_fingerprint"][:32],
                    payload={
                        "stage_status": "completed",
                        "completed_snapshot_fingerprint": state["current_snapshot_fingerprint"],
                        "review_source": {"review_source_type": "local_ai", "reviewer_label": "test"},
                        "applied": [],
                        "skipped": [],
                        "dft_evidence_candidates": [],
                    },
                )
            )
            session.commit()

    with Session(engine) as session:
        service = DFTReviewBundleService(session, get_settings())
        materials = service._build_materials(paper_uuid)
        evidence_ids = list(materials["evidence_map"])
        assert evidence_ids
        for audit in normalized_audits:
            audit.setdefault("target_id", "new")
            audit.setdefault("field_name", "dft_results")
            audit.setdefault("reason", "Verified against the cited DFT evidence.")
            if audit.get("decision") in {"PASS", "REVISE", "new_candidate"}:
                audit.setdefault("recommended_action", "ready_for_ml_export")
            audit["evidence_checked"] = True
            preferred_evidence_id = next(
                (
                    evidence_id
                    for evidence_id in evidence_ids
                    if audit.get("decision") == "new_candidate" and ":table:" in evidence_id
                ),
                evidence_ids[0],
            )
            audit["evidence_ids"] = [preferred_evidence_id]
            corrected = audit.get("corrected_value")
            if audit.get("decision") == "REVISE" and not isinstance(corrected, dict):
                row = session.get(DFTResult, UUID(str(audit["target_id"])))
                assert row is not None
                sample = session.get(CatalystSample, row.catalyst_sample_id) if row.catalyst_sample_id else None
                audit["corrected_value"] = {
                    "material_identity": (
                        audit.get("normalized_material")
                        or (sample.name if sample is not None else None)
                        or "DFT material"
                    ),
                    "property_type": row.property_type,
                    "value": corrected,
                    "unit": row.unit,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                }
            if audit.get("decision") == "new_candidate" and isinstance(audit.get("corrected_value"), dict):
                value = audit["corrected_value"]
                if not value.get("material_identity") and value.get("material"):
                    value["material_identity"] = value["material"]
        allowed_audit_fields = {
            "target_type",
            "target_id",
            "temporary_id",
            "field_name",
            "decision",
            "evidence_checked",
            "evidence_ids",
            "corrected_value",
            "confidence",
            "reason",
            "blocking_errors",
            "recommended_action",
            "dedupe_analysis",
        }
        normalized_audits = [
            {key: value for key, value in audit.items() if key in allowed_audit_fields}
            for audit in normalized_audits
        ]
        payload = {
            "schema_version": "offline_dft_review_result_v1",
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "figure_table_completed_snapshot_fingerprint": materials["curated_evidence_snapshot"][
                "completed_snapshot_fingerprint"
            ],
            "paper_id": str(paper_uuid),
            "paper_code": materials["paper_metadata"]["paper_code"],
            "review_mode": materials["review_mode"],
            "review_source": {
                "review_source_type": "web_ai",
                "reviewer_label": "MCP test Web AI",
                "reviewer_model": "test",
                "tool_capabilities": ["none"],
            },
            "overall_status": "uncertain",
            "object_review_audits": normalized_audits,
            "uncertainties": [],
            "notes": [],
        }
        validation = service.validate_result(paper_uuid, payload)
        assert validation["valid"] is True, validation["errors"]
        request = validation["import_analysis_request"]
        for audit in request["raw_payload"]["object_review_audits"]:
            audit["source"] = "local_ai"
            audit["source_label"] = "local_ai_after_pdf_evidence_check"
            audit["agent_role"] = "local_ai_pdf_verifier"
            requirements = audit["required_evidence_checks"]
            audit["local_ai_verification"] = {
                "verified_against_pdf": True,
                "used_tools": ["get_codex_item", "read_paper_page"],
                "checked_evidence_ids": [item["evidence_id"] for item in requirements],
                "checked_pages": [
                    {"paper_id": item["source_paper_id"], "page": item["page"]}
                    for item in audit["required_page_checks"]
                ],
                "verification_note": "Checked stored page layout and bundled source PDF evidence.",
            }
        return request


@pytest.fixture
def mcp_test_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv(
            "LITAI_MCP_API_KEYS",
            "claude|Claude Desktop|litmcp_claude|read_papers,append_notes,propose_corrections,request_parse;"
            "dft_primary_repair|DFT Primary Repair AI|litmcp_dft_primary_repair|read_papers,repair_dft_issues;"
            "admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections;"
            "ide_ai|IDE AI|litmcp_ide_ai|read_papers,append_notes,propose_corrections,request_parse;"
            "owner_export|Owner Export|litmcp_owner_export|read_papers,export_data;"
            "ai_pc_1|AI PC 1|litmcp_ai_pc_1|read_papers,append_notes,propose_corrections,request_parse,review_corrections;"
            "single_verifier|Single Verifier|litmcp_single_verifier|read_papers,ai_verify_content",
        )
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(Path(tmpdir) / "storage"))
        monkeypatch.setenv("LITAI_LOCAL_INGEST_ROOTS", tmpdir)
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


def _auth() -> str:
    return "litmcp_claude"


def _ide_auth() -> str:
    return "litmcp_ide_ai"


def _export_auth() -> str:
    return "litmcp_owner_export"


def _admin_auth() -> str:
    return "litmcp_admin"


def _dft_primary_repair_auth() -> str:
    return "litmcp_dft_primary_repair"


def _ai_reviewer_auth() -> str:
    return "litmcp_ai_pc_1"


def _single_verifier_auth() -> str:
    return "litmcp_single_verifier"


def _validated_content_web_bundle(engine, root: Path) -> tuple[str, dict, UUID]:
    """Create one validated bundle with a page that local AI must inspect."""
    suffix = uuid4().hex[:10]
    pdf = root / f"content-local-{suffix}.pdf"
    preview = root / f"content-page-{suffix}.png"
    pdf.write_bytes(b"%PDF-1.4\ncontent\n%%EOF")
    preview.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
        "pZ7q8QAAAABJRU5ErkJggg=="
    ))
    with Session(engine) as session:
        paper = Paper(title="content local MCP", paper_code=f"C{suffix}", pdf_path=str(pdf), authors=[])
        session.add(paper); session.flush()
        section = PaperSection(
            paper_id=paper.id, section_title="Results", text="grounded 2.0 eV statement", page_start=1, page_end=1,
        )
        session.add(section); session.flush()
        session.add(EvidenceLocator(
            paper_id=paper.id, target_type="paper_section", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="layout_verifier",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        service = ContentWebReviewBundleV2Service(session)
        created = service.generate(paper_id=paper.id, module="sections")
        target = created["manifest"]["targets"][0]
        evidence = target["evidence"]
        proposal = {
            "schema_version": "content_web_review_proposal_v2",
            "bundle_fingerprint": created["manifest"]["bundle_fingerprint"],
            "paper_id": str(paper.id), "paper_code": paper.paper_code,
            "proposal_status": "web_ai_proposal", "source_identity_verified": False,
            "writes_final_truth": False, "local_ai_verification": None,
            "actions": [{
                "plan_item_id": target["plan_item_id"], "target_type": target["target_type"],
                "target_id": target["target_id"], "field_name": target["field_name"],
                "object_snapshot_hash": target["object_snapshot_hash"], "decision": "PASS",
                "evidence_ref_ids": [evidence["evidence_ref_id"]], "evidence_quote": evidence["evidence_excerpt"],
                "evidence_asset_sha256": evidence["evidence_asset_sha256"], "page": evidence["page"],
                "proposed_value": None, "verification_note": None,
            }],
            "discovery_proposals": [],
        }
        assert service.validate_web_proposal(UUID(created["bundle_id"]), proposal)["valid"]
        plan = service.local_verification_plan(UUID(created["bundle_id"]))
        assert len(plan["required_page_checks"]) == 1
        session.commit()
        return created["bundle_id"], plan["required_page_checks"][0], section.id


def _content_read_snapshot(engine, bundle_id: str) -> dict:
    with Session(engine) as session:
        bundle = session.get(ContentWebReviewBundleV2, UUID(bundle_id))
        assert bundle is not None
        return {
            "bundle": {
                "status": bundle.status, "manifest": deepcopy(bundle.manifest),
                "proposal_payload": deepcopy(bundle.proposal_payload), "updated_at": bundle.updated_at,
            },
            "sections": [(str(row.id), row.text) for row in session.scalars(select(PaperSection).order_by(PaperSection.id))],
            "locators": [(str(row.id), deepcopy(row.bbox), row.locator_status) for row in session.scalars(select(EvidenceLocator).order_by(EvidenceLocator.id))],
            "correction_count": session.scalar(select(func.count()).select_from(PaperCorrection)),
            "review_count": session.scalar(select(func.count()).select_from(ExtractionFieldReview)),
            "result_count": session.scalar(select(func.count()).select_from(ContentWebReviewLocalVerificationResult)),
            "audit_count": session.scalar(select(func.count()).select_from(AuditLog)),
        }


def test_example_mcp_key_split_reserves_repair_for_primary_repair_key(mcp_test_env):
    configs = parse_mcp_api_keys(os.environ["LITAI_MCP_API_KEYS"])

    assert validate_mcp_capability_assignments(configs) == []
    assert configs["litmcp_dft_primary_repair"].source_prefix == "dft_primary_repair"
    assert configs["litmcp_dft_primary_repair"].display_name == "DFT Primary Repair AI"
    assert configs["litmcp_dft_primary_repair"].capabilities == frozenset(
        {"read_papers", "repair_dft_issues"}
    )
    assert "repair_dft_issues" not in configs["litmcp_claude"].capabilities
    assert "repair_dft_issues" not in configs["litmcp_admin"].capabilities
    assert "propose_corrections" not in configs["litmcp_dft_primary_repair"].capabilities


def test_in_process_mcp_context_uses_configured_key_identity(mcp_test_env):
    with mcp_auth_context("litmcp_claude"):
        auth = get_mcp_auth()
        assert auth is not None
        assert auth.source_identity == "mcp:claude"
        assert auth.identity_verified is True


def test_in_process_mcp_context_rejects_direct_identity_injection_even_if_pytest_env_is_spoofed(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "spoofed-by-runtime")

    spoofed = MCPAuthInfo(
        source_prefix="spoofed",
        display_name="Spoofed",
        capabilities=frozenset({"read_papers", "repair_dft_issues"}),
        raw_key="not-a-real-configured-key",
        source_identity="mcp:spoofed",
        identity_verified=True,
    )
    with pytest.raises(PermissionError, match="Invalid MCP API key"):
        with mcp_auth_context(spoofed):  # type: ignore[arg-type]
            pass


def test_repair_capability_lint_does_not_depend_on_ai_identity():
    configs = parse_mcp_api_keys(
        "dft_primary_repair|DFT Primary Repair AI|litmcp_primary|read_papers,repair_dft_issues;"
        "lab_primary_repair|Lab Primary Repair|litmcp_lab|read_papers,repair_dft_issues"
    )

    assert validate_mcp_capability_assignments(configs) == []


@pytest.mark.parametrize(
    ("raw_config", "source_prefix", "display_name"),
    [
        (
            "assigned_dft_audit|Assigned DFT Audit|litmcp_audit_secret|read_papers,repair_dft_issues",
            "assigned_dft_audit",
            "Assigned DFT Audit",
        ),
        (
            "admin|Admin|litmcp_admin_secret|read_papers,review_corrections,repair_dft_issues",
            "admin",
            "Admin",
        ),
    ],
)
def test_repair_capability_lint_warns_without_raw_key(raw_config, source_prefix, display_name):
    warnings = validate_mcp_capability_assignments(parse_mcp_api_keys(raw_config))
    assert warnings == []


@pytest.mark.no_test_database
def test_single_dft_ai_identity_is_valid():
    warnings = validate_mcp_capability_assignments(
        parse_mcp_api_keys(
            "owner|Local Owner|litmcp_owner_secret|read_papers,append_notes,propose_corrections,request_parse"
        )
    )

    assert warnings == []


def test_agent_guide_documents_optional_fast_dft_roles():
    import asyncio

    from app.api.system import get_agent_guide

    guide = asyncio.run(get_agent_guide())
    by_source = {item["source_prefix"]: item for item in guide["mcp"]["key_role_examples"]}

    assert "dft_primary_repair" not in by_source
    assert "repair_dft_issues" not in by_source["ide_ai"]["capabilities"]
    assert "repair_dft_issues" not in by_source["assigned_dft_audit"]["capabilities"]
    assert "repair_dft_issues" not in by_source["human_reviewer"]["capabilities"]
    assert "same authenticated identity may run the fast processor" in by_source["assigned_dft_audit"]["purpose"]
    assert "fast DFT processing does not wait for it" in by_source["human_reviewer"]["purpose"]
    assert guide["mcp"]["capability_warnings"] == []


def test_agent_guide_accepts_repair_capability_without_identity_role(monkeypatch):
    import asyncio

    from app.api.system import get_agent_guide

    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "admin|Admin|litmcp_admin_secret|read_papers,review_corrections,repair_dft_issues",
    )
    get_settings.cache_clear()
    try:
        guide = asyncio.run(get_agent_guide())
    finally:
        get_settings.cache_clear()

    warnings = guide["mcp"]["capability_warnings"]
    assert warnings == []


@pytest.mark.no_test_database
def test_agent_guide_accepts_one_authenticated_dft_ai(monkeypatch):
    import asyncio

    from app.api.system import get_agent_guide

    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "owner|Local Owner|litmcp_owner_secret|read_papers,append_notes,propose_corrections,request_parse",
    )
    get_settings.cache_clear()
    try:
        guide = asyncio.run(get_agent_guide())
    finally:
        get_settings.cache_clear()

    warnings = guide["mcp"]["capability_warnings"]
    assert warnings == []


def _make_external_audit_ready(paper: Paper, root: Path) -> None:
    pdf_path = root / f"{paper.id}.pdf"
    markdown_path = root / f"{paper.id}.md"
    docling_path = root / f"{paper.id}.docling.json"
    workspace_path = root / "workspace" / str(paper.id)
    pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    markdown_path.write_text("# Ready paper\n\nDFT evidence is available.", encoding="utf-8")
    docling_path.write_text('{"texts": [{"text": "DFT evidence is available."}]}', encoding="utf-8")
    package_path = workspace_path / "extraction" / "ai_reading_package.json"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_text('{"sections": [{"title": "Results"}]}', encoding="utf-8")
    paper.pdf_path = str(pdf_path)
    paper.markdown_path = str(markdown_path)
    paper.docling_json_path = str(docling_path)
    paper.workspace_path = str(workspace_path)


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

        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="metadata",
            locked_by="claude",
        )
        correction = propose_correction(
            paper_id=paper_id,
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="Updated abstract text",
            reason="Cross-check against the uploaded PDF abstract.",
            evidence_payload={"page": 1, "section_title": "Abstract"},
            write_lock_token=lock["lock_token"],
        )
        assert correction["status"] == "approved"
        assert correction["target_path"] == "abstract"

    with Session(mcp_test_env["engine"]) as session:
        saved_notes = session.scalars(select(PaperNote)).all()
        saved_corrections = session.scalars(select(PaperCorrection)).all()
        audit_logs = session.scalars(select(AuditLog).order_by(AuditLog.created_at.asc())).all()

        assert len(saved_notes) == 1
        assert len(saved_corrections) == 1
        assert [item.action for item in audit_logs] == [
            "append_note",
            "acquire_module_write_lock",
            "propose_correction",
            "approve_correction",
        ]


def test_apply_content_web_review_local_verification_uses_authenticated_internal_short_lock(mcp_test_env):
    root = mcp_test_env["tmpdir"]
    pdf = root / "content-local.pdf"
    preview = root / "content-page.png"
    pdf.write_bytes(b"%PDF-1.4\ncontent\n%%EOF")
    preview.write_bytes(b"verified page asset")
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="content local MCP", paper_code="MCP-CONTENT", pdf_path=str(pdf), authors=[])
        session.add(paper); session.flush()
        section = PaperSection(paper_id=paper.id, section_title="Results", text="grounded statement", page_start=1, page_end=1)
        session.add(section); session.flush()
        session.add(EvidenceLocator(
            paper_id=paper.id, target_type="paper_section", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="layout_verifier",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        session.flush()
        bundle_service = ContentWebReviewBundleV2Service(session)
        created = bundle_service.generate(paper_id=paper.id, module="sections")
        target = created["manifest"]["targets"][0]
        evidence = target["evidence"]
        proposal = {
            "schema_version": "content_web_review_proposal_v2",
            "bundle_fingerprint": created["manifest"]["bundle_fingerprint"],
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "proposal_status": "web_ai_proposal",
            "source_identity_verified": False,
            "writes_final_truth": False,
            "local_ai_verification": None,
            "actions": [{
                "plan_item_id": target["plan_item_id"], "target_type": target["target_type"],
                "target_id": target["target_id"], "field_name": target["field_name"],
                "object_snapshot_hash": target["object_snapshot_hash"], "decision": "PASS",
                "evidence_ref_ids": [evidence["evidence_ref_id"]], "evidence_quote": evidence["evidence_excerpt"],
                "evidence_asset_sha256": evidence["evidence_asset_sha256"], "page": evidence["page"],
                "proposed_value": None, "verification_note": None,
            }],
            "discovery_proposals": [],
        }
        assert bundle_service.validate_web_proposal(UUID(created["bundle_id"]), proposal)["valid"]
        check = bundle_service.local_verification_plan(UUID(created["bundle_id"]))["required_object_checks"][0]
        assert check["requires_page_render"] is False
        session.commit()
        bundle_id = created["bundle_id"]
        result = {
            "plan_item_id": check["plan_item_id"],
            "object_snapshot_hash": check["object_snapshot_hash"],
            "outcome": "CONFIRMED",
            "checked_evidence_ids": [check["evidence_ref_id"]],
            "checked_pages": [],
            "verification_note": "authenticated local conclusion",
        }

    with mcp_auth_context(_auth()):
        applied = apply_content_web_review_local_verification(bundle_id=bundle_id, results=[result])
    assert applied["status"] == "finalized"
    assert applied["submitted_results"][0]["applied_by"] == "claude"
    with Session(mcp_test_env["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(ModuleWriteLock).where(ModuleWriteLock.status == "active")) == 0

    with mcp_auth_context(_export_auth()):
        with pytest.raises(PermissionError, match="propose_corrections"):
            apply_content_web_review_local_verification(bundle_id=bundle_id, results=[result])

    token = set_mcp_auth(MCPAuthInfo(
        source_prefix="open_mcp", display_name="Open", capabilities=frozenset({"propose_corrections"}),
        raw_key="", source_identity="mcp:open_mcp", identity_verified=False,
    ))
    try:
        with pytest.raises(PermissionError, match="identity_required"):
            apply_content_web_review_local_verification(bundle_id=bundle_id, results=[result])
    finally:
        reset_mcp_auth(token)


def test_content_web_local_verification_read_tools_return_only_planned_image_and_do_not_mutate(mcp_test_env):
    bundle_id, page_check, _ = _validated_content_web_bundle(mcp_test_env["engine"], mcp_test_env["tmpdir"])
    before = _content_read_snapshot(mcp_test_env["engine"], bundle_id)

    with mcp_auth_context(_auth()):
        plan = get_content_web_review_local_verification_plan(bundle_id)
        assert plan["required_page_checks"] == [page_check]
        direct = read_content_web_review_page_asset(bundle_id=bundle_id, **{
            key: page_check[key]
            for key in ("source_paper_id", "source_pdf_sha256", "page", "page_asset_ref", "page_asset_sha256")
        })
        metadata, image = direct
        assert metadata["page_asset_sha256"] == page_check["page_asset_sha256"]
        assert hashlib.sha256(image.data).hexdigest() == page_check["page_asset_sha256"]
        content = asyncio.run(mcp_server.call_tool(
            "read_content_web_review_page_asset",
            {"bundle_id": bundle_id, **{
                key: page_check[key]
                for key in ("source_paper_id", "source_pdf_sha256", "page", "page_asset_ref", "page_asset_sha256")
            }},
        ))
    image_blocks = [block for block in content if getattr(block, "type", None) == "image"]
    assert len(image_blocks) == 1
    assert hashlib.sha256(base64.b64decode(image_blocks[0].data)).hexdigest() == page_check["page_asset_sha256"]
    assert _content_read_snapshot(mcp_test_env["engine"], bundle_id) == before


def test_content_web_local_verification_read_tools_reject_unsafe_requests_without_mutation(mcp_test_env):
    bundle_id, page_check, section_id = _validated_content_web_bundle(mcp_test_env["engine"], mcp_test_env["tmpdir"])
    before = _content_read_snapshot(mcp_test_env["engine"], bundle_id)
    with pytest.raises(PermissionError, match="authentication context is missing"):
        get_content_web_review_local_verification_plan(bundle_id)
    with mcp_auth_context(_export_auth()):
        with pytest.raises(PermissionError, match="propose_corrections"):
            get_content_web_review_local_verification_plan(bundle_id)
    token = set_mcp_auth(MCPAuthInfo(
        source_prefix="unverified", display_name="Unverified", capabilities=frozenset({"propose_corrections"}),
        raw_key="", source_identity="mcp:unverified", identity_verified=False,
    ))
    try:
        with pytest.raises(PermissionError, match="identity_required"):
            get_content_web_review_local_verification_plan(bundle_id)
    finally:
        reset_mcp_auth(token)
    with mcp_auth_context(_auth()):
        with pytest.raises(ValueError, match="unknown_required_page_asset"):
            read_content_web_review_page_asset(
                bundle_id=bundle_id, source_paper_id=page_check["source_paper_id"],
                source_pdf_sha256=page_check["source_pdf_sha256"], page=page_check["page"],
                page_asset_ref="evidence/pages/untrusted.png", page_asset_sha256=page_check["page_asset_sha256"],
            )
    assert _content_read_snapshot(mcp_test_env["engine"], bundle_id) == before

    # A stale read is blocked but never writes stale status/manifest/audit state.
    with Session(mcp_test_env["engine"]) as session:
        section = session.get(PaperSection, section_id)
        assert section is not None
        section.text = "changed after web proposal"
        session.commit()
    stale_before = _content_read_snapshot(mcp_test_env["engine"], bundle_id)
    with mcp_auth_context(_auth()):
        with pytest.raises(ValueError, match="bundle_stale"):
            get_content_web_review_local_verification_plan(bundle_id)
        with pytest.raises(ValueError, match="bundle_stale"):
            read_content_web_review_page_asset(bundle_id=bundle_id, **{
                key: page_check[key]
                for key in ("source_paper_id", "source_pdf_sha256", "page", "page_asset_ref", "page_asset_sha256")
            })
    assert _content_read_snapshot(mcp_test_env["engine"], bundle_id) == stale_before


def test_content_web_local_read_tools_reject_unvalidated_and_cross_bundle_assets_without_mutation(mcp_test_env):
    root = mcp_test_env["tmpdir"]
    pdf = root / "unvalidated-content.pdf"
    preview = root / "unvalidated-page.png"
    pdf.write_bytes(b"%PDF-1.4\nunvalidated\n%%EOF")
    preview.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
        "pZ7q8QAAAABJRU5ErkJggg=="
    ))
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="unvalidated", paper_code="MCP-UNVALIDATED", pdf_path=str(pdf), authors=[])
        session.add(paper); session.flush()
        section = PaperSection(paper_id=paper.id, section_title="Results", text="2.0 eV", page_start=1, page_end=1)
        session.add(section); session.flush()
        session.add(EvidenceLocator(
            paper_id=paper.id, target_type="paper_section", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="layout_verifier",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        created = ContentWebReviewBundleV2Service(session).generate(paper_id=paper.id, module="sections")
        unvalidated_asset = created["manifest"]["targets"][0]["evidence"]
        session.commit()
    unvalidated_before = _content_read_snapshot(mcp_test_env["engine"], created["bundle_id"])
    with mcp_auth_context(_auth()):
        with pytest.raises(ValueError, match="proposal_must_be_validated"):
            get_content_web_review_local_verification_plan(created["bundle_id"])
        with pytest.raises(ValueError, match="proposal_must_be_validated"):
            read_content_web_review_page_asset(
                bundle_id=created["bundle_id"], source_paper_id=unvalidated_asset["source_paper_id"],
                source_pdf_sha256=unvalidated_asset["source_pdf_sha256"], page=unvalidated_asset["page"],
                page_asset_ref=unvalidated_asset["page_asset_ref"], page_asset_sha256=unvalidated_asset["page_asset_sha256"],
            )
    assert _content_read_snapshot(mcp_test_env["engine"], created["bundle_id"]) == unvalidated_before

    bundle_a, page_a, _ = _validated_content_web_bundle(mcp_test_env["engine"], root)
    bundle_b, _, _ = _validated_content_web_bundle(mcp_test_env["engine"], root)
    bundle_b_before = _content_read_snapshot(mcp_test_env["engine"], bundle_b)
    with mcp_auth_context(_auth()):
        with pytest.raises(ValueError, match="unknown_required_page_asset"):
            read_content_web_review_page_asset(bundle_id=bundle_b, **{
                key: page_a[key]
                for key in ("source_paper_id", "source_pdf_sha256", "page", "page_asset_ref", "page_asset_sha256")
            })
    assert bundle_a != bundle_b
    assert _content_read_snapshot(mcp_test_env["engine"], bundle_b) == bundle_b_before


@pytest.mark.no_test_database
def test_content_web_local_verification_read_tools_are_listed_by_fastmcp():
    tools = asyncio.run(mcp_server.list_tools())
    names = {tool.name for tool in tools}
    assert "get_content_web_review_local_verification_plan" in names
    assert "read_content_web_review_page_asset" in names


def test_review_figure_is_idempotent_and_persists_one_authoritative_verdict(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Review figure", pdf_path="paper.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 1. Reaction pathway.",
            image_path="figures/one.png",
            page=2,
            figure_role="mechanism",
            content_summary="Two reaction branches contain distinct intermediates and barrier labels.",
            key_elements=["reaction arrows", "intermediates", "barrier labels"],
        )
        session.add(figure)
        session.commit()
        figure_id = str(figure.id)

    with mcp_auth_context(_admin_auth()):
        first = review_figure(figure_id, "rejected", "The crop does not match the caption.")
        second = review_figure(figure_id, "rejected", "Repeated check reaches the same result.")

    assert first["verdict"] == "rejected"
    assert first["idempotent"] is False
    assert second["verdict"] == "rejected"
    assert second["idempotent"] is True
    assert second["note_created"] is False
    with Session(mcp_test_env["engine"]) as session:
        logs = session.scalars(select(AuditLog).where(AuditLog.action == "review_figure")).all()
        notes = session.scalars(select(PaperNote).where(PaperNote.field_name == "figure_review")).all()
        assert len(logs) == 1
        assert len(notes) == 1
        assert logs[0].payload["verdict"] == "rejected"


def test_review_figure_normalizes_caption_echo_summary_prefix(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Normalize review figure summary", pdf_path="paper.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Fig. 2 | Structural characterization of HEASA-Pt2.3%, Pt1-NiCoMgBiSn.",
            image_path="figures/two.png",
            page=4,
            figure_role="characterization",
            content_summary="Old summary",
            key_elements=["HAADF-STEM", "XANES"],
        )
        session.add(figure)
        session.commit()
        figure_id = str(figure.id)

    with mcp_auth_context(_admin_auth()):
        result = review_figure(
            figure_id,
            "verified",
            "Use a visual summary instead of repeating the caption.",
            content_summary=(
                "Fig. 2 | Structural characterization of HEASA-Pt2.3%, Pt1-NiCoMgBiSn. "
                "(a) HAADF-STEM image with EDS elemental maps for Pt, Ni, Co, Mg, Bi, and Sn. "
                "(b-f) XANES/EXAFS comparisons and Pt-Pt coordination number chart."
            ),
        )

    assert result["applied_updates"]["content_summary"] == (
        "(a) HAADF-STEM image with EDS elemental maps for Pt, Ni, Co, Mg, Bi, and Sn. "
        "(b-f) XANES/EXAFS comparisons and Pt-Pt coordination number chart."
    )
    with Session(mcp_test_env["engine"]) as session:
        stored = session.get(PaperFigure, UUID(figure_id))
        assert stored is not None
        assert stored.content_summary == result["applied_updates"]["content_summary"]


def test_review_figure_normalizes_stringified_key_elements(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Normalize figure key elements", pdf_path="paper.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 3. Structural analysis.",
            image_path="figures/three.png",
            page=3,
            figure_role="characterization",
            content_summary="(a) STEM image and (b) EXAFS comparison.",
            key_elements=["old"],
        )
        session.add(figure)
        session.commit()
        figure_id = str(figure.id)

    with mcp_auth_context(_admin_auth()):
        result = review_figure(
            figure_id,
            "verified",
            "Normalize stringified dict key elements.",
            key_elements=[
                "{'description': 'Panel (a): HAADF-STEM image with Pt single-atom dispersion'}",
                "{'description': 'Panel (b): EXAFS fitting and coordination comparison'}",
            ],
        )

    assert result["applied_updates"]["key_elements"] == [
        "Panel (a): HAADF-STEM image with Pt single-atom dispersion",
        "Panel (b): EXAFS fitting and coordination comparison",
    ]
    with Session(mcp_test_env["engine"]) as session:
        stored = session.get(PaperFigure, UUID(figure_id))
        assert stored is not None
        assert stored.key_elements == result["applied_updates"]["key_elements"]


def test_scan_duplicate_dois_groups_default_library_aliases(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        session.add_all(
            [
                Paper(title="Default Alias One", doi="10.1000/default-alias", library_name=DEFAULT_LIBRARY_NAME, pdf_path="one.pdf"),
                Paper(title="Default Alias Two", doi="10.1000/default-alias", library_name="Codex ????????", pdf_path="two.pdf"),
                Paper(title="Other Library", doi="10.1000/default-alias", library_name="OtherLibrary", pdf_path="three.pdf"),
            ]
        )
        session.commit()

    with mcp_auth_context(_auth()):
        payload = scan_duplicate_dois()

    duplicate = next(item for item in payload["duplicates"] if item["doi"] == "10.1000/default-alias")
    assert duplicate["library_name"] == DEFAULT_LIBRARY_NAME
    assert duplicate["count"] == 2
    assert len(duplicate["paper_ids"]) == 2


def test_mcp_query_papers_sort_by_created_at(mcp_test_env):
    """Verify query_papers supports sort_by='created_at' and sort_order."""
    from datetime import datetime, timezone

    with Session(mcp_test_env["engine"]) as session:
        paper1 = Paper(
            doi="10.1000/old",
            title="Old Paper",
            year=2020,
            pdf_path="old.pdf",
        )
        session.add(paper1)
        session.flush()
        # Force older created_at
        paper1.created_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        paper2 = Paper(
            doi="10.1000/new",
            title="New Paper",
            year=2025,
            pdf_path="new.pdf",
        )
        session.add(paper2)
        session.flush()
        paper2.created_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        session.commit()

    with mcp_auth_context(_auth()):
        # Descending: newest first
        desc = query_papers(sort_by="created_at", sort_order="desc", limit=10)
        assert desc["returned"] == 2
        assert desc["items"][0]["title"] == "New Paper"
        assert desc["items"][1]["title"] == "Old Paper"

        # Ascending: oldest first
        asc = query_papers(sort_by="created_at", sort_order="asc", limit=10)
        assert asc["returned"] == 2
        assert asc["items"][0]["title"] == "Old Paper"
        assert asc["items"][1]["title"] == "New Paper"


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
    blocked_reasons = payload["context"]["export_safety"]["blocked_reasons"]
    assert "missing_material_identity" in blocked_reasons
    assert "missing_review" in blocked_reasons
    assert payload["context"]["evidence_locators"]["items"][0]["page"] == 5


def test_mcp_get_codex_item_reads_dft_after_detail_page_and_all_sample_bindings(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP exact DFT item paper", pdf_path="exact-dft-item.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Fe-N4")
        session.add(sample)
        session.flush()
        rows = [
            DFTResult(
                id=UUID(int=index + 1),
                paper_id=paper.id,
                catalyst_sample_id=sample.id,
                property_type="adsorption_energy",
                adsorbate=f"Li2S{index}",
                value=float(index),
                unit="eV",
                evidence_text=f"Exact DFT evidence {index}",
            )
            for index in range(30)
        ]
        session.add_all(rows)
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)
        target_id = str(rows[-1].id)
        expected_row_ids = {str(row.id) for row in rows}

        detail = PaperQueryService(session).get_paper_detail(paper.id)
        assert detail is not None
        assert len(detail.dft_results_items) == 28
        assert target_id not in {str(item.id) for item in detail.dft_results_items}

    with mcp_auth_context(_auth()):
        dft_payload = get_codex_item(
            paper_id=paper_id,
            item_type="dft_result",
            item_id=target_id,
        )
        sample_payload = get_codex_item(
            paper_id=paper_id,
            item_type="catalyst_sample",
            item_id=sample_id,
        )

    assert dft_payload["context"]["item"]["id"] == target_id
    assert dft_payload["context"]["item"]["value"] == 29.0
    sample_item = sample_payload["context"]["item"]
    assert sample_item["dependent_dft_summary"] == {
        "total": 30,
        "bound": 30,
        "future_unbound": 0,
    }
    assert len(sample_item["dependent_dft_results"]) == 30
    assert {item["id"] for item in sample_item["dependent_dft_results"]} == expected_row_ids


def test_ordinary_ide_ai_reads_context_and_imports_unverified_audit_candidate(mcp_test_env):
    configs = parse_mcp_api_keys(
        "ide_ai|IDE AI|litmcp_ide_ai|read_papers,append_notes,propose_corrections,request_parse"
    )
    assert configs["litmcp_ide_ai"].capabilities == frozenset(
        {"read_papers", "append_notes", "propose_corrections", "request_parse"}
    )
    assert "review_corrections" not in configs["litmcp_ide_ai"].capabilities

    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(
            title="Ordinary IDE AI MCP Workflow Paper",
            doi="10.1000/ide-ai-workflow",
            year=2026,
            journal="Workflow Journal",
            authors=["AI Reviewer"],
            abstract="A paper with DFT, figures, tables, mechanism claims, and writing cards.",
            pdf_path="workflow.pdf",
        )
        session.add(paper)
        session.flush()
        _make_external_audit_ready(paper, mcp_test_env["tmpdir"])
        section = PaperSection(
            paper_id=paper.id,
            section_title="Results",
            section_type="results",
            text="The adsorption energy of Li2S4 is -1.23 eV and the figure supports the trend.",
            page_start=3,
            page_end=4,
        )
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 2. Adsorption configuration and charge redistribution.",
            image_path="figures/fig2.png",
            page=4,
            figure_role="data_figure",
        )
        table = PaperTable(
            paper_id=paper.id,
            caption="Table 1. DFT adsorption energies.",
            markdown_content="| Species | Energy |\n| Li2S4 | -1.23 eV |",
            page=5,
        )
        dft = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.23,
            unit="eV",
            evidence_text="The adsorption energy of Li2S4 is -1.23 eV.",
            confidence=0.82,
        )
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="adsorption",
            claim_text="The catalyst strengthens polysulfide adsorption.",
            evidence_types=["dft", "figure"],
            evidence_text="Charge redistribution indicates stronger adsorption.",
        )
        card = WritingCard(
            paper_id=paper.id,
            research_gap="Weak polysulfide adsorption remains a limitation.",
            proposed_solution="Use defect sites to tune adsorption.",
            core_hypothesis="Defect engineering improves sulfur conversion.",
        )
        session.add_all([section, figure, table, dft, claim, card])
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(dft.id),
                field_name="value",
                page=5,
                table_id=table.id,
                evidence_text=dft.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.91,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        dft_id = str(dft.id)
        claim_id = str(claim.id)

    with mcp_auth_context(_ide_auth()):
        papers = query_papers(q="Ordinary IDE AI", limit=5)
        assert papers["returned"] == 1
        assert papers["items"][0]["id"] == paper_id

        context = get_codex_context(paper_id=paper_id)
        assert context["context"]["external_audit_precondition"]["status"] == "ready"
        assert len(context["context"]["content"]["sections"]) == 1
        assert len(context["context"]["content"]["figures"]) == 1
        assert len(context["context"]["content"]["tables"]) == 1
        assert context["context"]["review_workflow_state"]["dft_review"]["active_candidates"] == 1
        assert context["context"]["review_workflow_state"]["figure_table_review"]["stage_status"] in {
            "pending",
            "not_started",
            "completed",
            "not_required",
            "stale",
            "unknown",
        }

        dft_context = get_codex_item(paper_id=paper_id, item_type="dft_result", item_id=dft_id)
        assert dft_context["context"]["export_safety"]["eligible"] is False
        assert "missing_review" in dft_context["context"]["export_safety"]["blocked_reasons"]

        mechanism_context = get_codex_item(paper_id=paper_id, item_type="mechanism_claim", item_id=claim_id)
        assert mechanism_context["context"]["item"]["claim_text"].startswith("The catalyst strengthens")

        imported = import_analysis(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            raw_payload={
                "paper_id": paper_id,
                "agent_role": "dft_auditor",
                "verdict": "WARN",
                "recommended_action": "needs_human_review",
                "suspected_missing": [],
                "metadata_status": "ok",
                "section_structure_status": "ok",
                "table_status": "ok",
                "figure_status": "ok",
                "dft_status": "warn",
                "evidence_examples": [{"text": "DFT row needs final reviewer confirmation."}],
                "confidence": 0.74,
            },
        )
        assert imported["candidate_count"] == 1
        assert imported["candidates"][0]["type"] == "external_audit_opinion"

        with pytest.raises(PermissionError):
            approve_correction(str(UUID(int=0)))

    with Session(mcp_test_env["engine"]) as session:
        candidate = session.scalar(select(ExternalAnalysisCandidate))
        assert candidate is not None
        assert candidate.candidate_type == "external_audit_opinion"
        assert candidate.status == "candidate"
        assert candidate.materialized_target_type is None
        assert candidate.materialized_target_id is None
        assert candidate.normalized_payload["verification_status"] == "unverified"
        assert candidate.normalized_payload["source"] == "assigned_dft_audit"

        row = session.get(DFTResult, UUID(dft_id))
        assert row is not None
        assert row.candidate_status == "system_candidate"


def test_mcp_import_analysis_accepts_object_level_review_payload(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Object Audit Paper", pdf_path="mcp-object.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 1 reports adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        imported = import_analysis(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            raw_payload={
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": row_id,
                        "field_name": "value",
                        "decision": "REVISE",
                        "evidence_checked": True,
                        "evidence_location": {"page": 8, "table": "Table 1"},
                        "corrected_value": -1.35,
                        "recommended_action": "propose_correction",
                        "confidence": 0.71,
                    }
                ]
            },
            auto_apply_review_rules=False,
        )

    assert imported["candidate_count"] == 1
    candidate = imported["candidates"][0]
    assert candidate["type"] == "object_review_audit"
    assert candidate["target_type"] == "dft_results"
    assert candidate["target_id"] == row_id
    assert candidate["field_name"] == "value"
    assert candidate["decision"] == "REVISE"
    assert candidate["verification_status"] == "unverified"

    with Session(mcp_test_env["engine"]) as session:
        stored_row = session.get(DFTResult, UUID(row_id))
        stored_candidate = session.scalar(select(ExternalAnalysisCandidate))
        assert stored_row.candidate_status == "system_candidate"
        assert stored_candidate.candidate_type == "object_review_audit"
        assert stored_candidate.status == "candidate"
        assert stored_candidate.normalized_payload["writes_final_truth"] is False


def test_mcp_import_analysis_warns_on_non_countable_dft_decision(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP DFT Decision Warning Paper", pdf_path="mcp-dft-warning.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 1 reports adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        imported = import_analysis(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            auto_apply_review_rules=False,
            raw_payload={
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": row_id,
                        "field_name": "value",
                        "decision": "evidence_verified",
                        "evidence_location": {"page": 8, "table": "Table 1", "quoted_text": "-1.20 eV"},
                        "corrected_value": -1.2,
                        "confidence": 0.88,
                    }
                ]
            },
        )

    assert imported["candidate_count"] == 1
    assert imported["warnings"][0]["code"] == "non_countable_dft_decision"
    assert imported["warnings"][0]["decision"] == "evidence_verified"
    assert "PROPOSED" in imported["warnings"][0]["allowed_decisions"]
    assert "needs_user_decision" in imported["warnings"][0]["message"]
    assert "manual adjudication" in imported["warnings"][0]["message"]


def test_mcp_import_analysis_does_not_warn_on_countable_dft_decision(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP DFT Decision Clean Paper", pdf_path="mcp-dft-clean.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 1 reports adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        imported = import_analysis(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            auto_apply_review_rules=False,
            raw_payload={
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": row_id,
                        "field_name": "value",
                        "decision": "PROPOSED",
                        "evidence_location": {"page": 8, "table": "Table 1", "quoted_text": "-1.20 eV"},
                        "corrected_value": -1.2,
                        "confidence": 0.88,
                    }
                ]
            },
        )

    assert imported["candidate_count"] == 1
    assert imported["warnings"] == []


def test_mcp_import_analysis_treats_needs_user_decision_as_manual_dft_decision(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Needs User Decision Paper", pdf_path="mcp-needs-user-decision.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 1 reports adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        imported = import_analysis(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            auto_apply_review_rules=False,
            raw_payload={
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": row_id,
                        "field_name": "value",
                        "decision": "needs_user_decision",
                        "evidence_location": {"page": 8, "table": "Table 1", "quoted_text": "-1.20 eV"},
                        "corrected_value": -1.2,
                        "confidence": 0.88,
                    }
                ]
            },
        )

    assert imported["candidate_count"] == 1
    assert imported["warnings"] == []


def test_mcp_import_analysis_treats_ambiguous_as_manual_dft_decision(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Ambiguous DFT Decision Paper", pdf_path="mcp-ambiguous-dft-warning.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 1 reports adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    with mcp_auth_context(_auth()):
        imported = import_analysis(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            auto_apply_review_rules=False,
            raw_payload={
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": row_id,
                        "field_name": "value",
                        "decision": "ambiguous",
                        "evidence_location": {"page": 8, "table": "Table 1", "quoted_text": "-1.20 eV"},
                        "corrected_value": -1.2,
                        "confidence": 0.88,
                    }
                ]
            },
        )

    assert imported["candidate_count"] == 1
    assert imported["warnings"] == []


def test_mcp_codex_context_and_item_use_full_candidate_detail_query(mcp_test_env, monkeypatch):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Compact Codex Context Paper", pdf_path="compact-context.pdf", authors=[])
        session.add(paper)
        session.flush()
        section = PaperSection(
            paper_id=paper.id,
            section_title="Introduction",
            section_type="introduction",
            text="Compact detail should still include sections.",
            page_start=1,
            page_end=1,
        )
        figure = PaperFigure(
            paper_id=paper.id,
            page=1,
            caption="Figure 1. Compact detail figure.",
            image_path="figures/compact-context-figure.png",
        )
        session.add_all([section, figure])
        session.commit()
        paper_id = str(paper.id)
        figure_id = str(figure.id)

    original = PaperQueryService.get_paper_detail
    compact_flags: list[bool] = []

    def wrapped(self, paper_id, *args, **kwargs):
        compact_flags.append(bool(kwargs.get("compact", False)))
        return original(self, paper_id, *args, **kwargs)

    monkeypatch.setattr(PaperQueryService, "get_paper_detail", wrapped)

    with mcp_auth_context(_ide_auth()):
        context = get_codex_context(paper_id=paper_id)
        item = get_codex_item(paper_id=paper_id, item_type="figure", item_id=figure_id)

    assert compact_flags == [False, False]
    assert len(context["context"]["content"]["sections"]) == 1
    assert item["context"]["item"]["caption"] == "Figure 1. Compact detail figure."


def test_mcp_import_analysis_applies_each_evidence_backed_dft_review(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Auto Apply DFT Paper", pdf_path="mcp-auto-apply.pdf", authors=[])
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Vacancy graphene",
            catalyst_type="defective_graphene",
            coordination="single vacancy",
            support="graphene",
        )
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            reaction_step="adsorption",
            evidence_text="Table 1 reports -1.20 eV for Li2S4 adsorption.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=7,
                evidence_text="Table 1 reports -1.20 eV for Li2S4 adsorption.",
                locator_status="exact_page",
                locator_confidence=0.95,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    payload = {
        "object_review_audits": [
            {
                "target_type": "dft_results",
                "target_id": row_id,
                "field_name": "value",
                "decision": "PASS",
                "corrected_value": -1.2,
                "evidence_checked": True,
                "confidence": 0.91,
                "reason": "Table 1 confirms the DFT value.",
                "evidence_location": {"page": 7, "table": "Table 1", "quoted_text": "-1.20 eV"},
            }
        ]
    }
    request = _validated_local_ai_dft_request(
        mcp_test_env["engine"],
        paper_id,
        payload["object_review_audits"],
    )

    with mcp_auth_context(_auth()):
        first = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            raw_payload=request["raw_payload"],
    )

    assert first["candidate_count"] == 1
    assert first["auto_apply_summary"]["object_reviews"]["pending_count"] == 0

    with Session(mcp_test_env["engine"]) as session:
        stored_row = session.get(DFTResult, UUID(row_id))
        candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()
        runs = session.query(ExternalAnalysisRun).order_by(ExternalAnalysisRun.created_at.asc()).all()
        reviews = session.query(ExtractionFieldReview).all()
        audit_logs = session.query(AuditLog).filter(AuditLog.action == "verify_dft_result").all()

    assert stored_row is not None
    assert stored_row.candidate_status == "system_candidate"
    assert [run.source_identity for run in runs] == ["mcp:claude"]
    assert all(run.source_identity_verified for run in runs)
    assert {candidate.status for candidate in candidates} <= {
        "candidate", "pending", "pending_ai_verification", "requires_resolution", "needs_human"
    }
    assert reviews == []
    assert audit_logs == []


@pytest.mark.parametrize(
    ("decision", "corrected_value", "expected_value", "expected_status", "expected_review_status", "expected_pending"),
    [
        ("REVISE", -1.45, -1.2, "system_candidate", None, 0),
        ("REJECT", None, -1.2, "system_candidate", None, 0),
        ("NEEDS_HUMAN", None, -1.2, "system_candidate", None, 1),
    ],
)
def test_mcp_single_dft_ai_revision_rejection_and_human_hold(
    mcp_test_env,
    decision,
    corrected_value,
    expected_value,
    expected_status,
    expected_review_status,
    expected_pending,
):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title=f"Single AI DFT {decision}", pdf_path="single-ai-dft.pdf", authors=[])
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4")
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 1 reports the adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=7,
                evidence_text="Table 1 reports the adsorption energy.",
                locator_status="exact_page",
                locator_confidence=0.95,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    audit = {
        "target_type": "dft_results",
        "target_id": row_id,
        "field_name": "value",
        "decision": decision,
        "corrected_value": corrected_value,
        "normalized_material": "Fe-N4",
        "reason": f"Single AI decision: {decision}",
        "evidence_location": {"page": 7, "table": "Table 1", "quoted_text": "adsorption energy"},
    }
    request = _validated_local_ai_dft_request(mcp_test_env["engine"], paper_id, [audit])
    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(paper_id=paper_id, module_name="dft_results")
        imported = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
        )

    summary = imported["auto_apply_summary"]
    assert summary["object_reviews"]["pending_count"] == expected_pending
    with Session(mcp_test_env["engine"]) as session:
        stored = session.get(DFTResult, UUID(row_id))
        reviews = session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.target_id == row_id)
        ).all()

    assert stored is not None
    assert stored.value == expected_value
    assert stored.candidate_status == expected_status
    if expected_review_status is None:
        assert reviews == []
    else:
        assert {review.reviewer_status for review in reviews} == {expected_review_status}


def test_mcp_import_analysis_materializes_new_dft_candidate_with_custom_reviewer_and_mcp_lock_owner(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP New Candidate DFT Paper", pdf_path="mcp-new-candidate.pdf", authors=[])
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    payload = {
        "object_review_audits": [
            {
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "Fe-N4",
                    "property_type": "adsorption_energy",
                    "value": -1.23,
                    "unit": "eV",
                    "adsorbate": "Li2S4",
                    "reaction_step": "adsorption",
                },
                "evidence_location": {
                    "page": 3,
                    "table": "Table 1",
                    "quoted_text": "The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
                },
                "confidence": 0.9,
            }
        ]
    }
    request = _validated_local_ai_dft_request(
        mcp_test_env["engine"],
        paper_id,
        payload["object_review_audits"],
    )

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="dft_results",
        )
        imported = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            reviewer="codex_window_b",
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
        )

    assert imported["reviewer"] == "codex_window_b"
    assert imported["warnings"] == []
    assert imported["auto_apply_summary"]["new_dft_candidates"]["materialized_count"] == 1
    with Session(mcp_test_env["engine"]) as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == UUID(paper_id))).all()
        assert len(rows) == 1
        assert rows[0].candidate_status == "new_candidate"
        assert rows[0].property_type == "adsorption_energy"


def test_mcp_import_analysis_preserves_new_candidate_contract_with_terminal_dft_context(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Terminal Context DFT Paper", pdf_path="mcp-terminal-context.pdf", authors=[])
        session.add(paper)
        session.flush()
        terminal_row = DFTResult(
            paper_id=paper.id,
            property_type="formation_energy",
            value=-9.87,
            unit="eV",
            evidence_text="Previously rejected terminal DFT context.",
            candidate_status="Rejected",
        )
        session.add(terminal_row)
        session.commit()
        paper_id = str(paper.id)
        terminal_id = str(terminal_row.id)

    temporary_id = "new-dft-terminal-context-001"
    request = _validated_local_ai_dft_request(
        mcp_test_env["engine"],
        paper_id,
        [
            {
                "target_type": "dft_results",
                "target_id": "new",
                "temporary_id": temporary_id,
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "Fe-N4",
                    "property_type": "adsorption_energy",
                    "value": -1.23,
                    "unit": "eV",
                    "adsorbate": "Li2S4",
                    "reaction_step": "adsorption",
                },
                "evidence_location": {
                    "page": 3,
                    "table": "Table 1",
                    "quoted_text": "The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
                },
                "dedupe_analysis": {
                    "compared_target_ids": [terminal_id],
                    "conclusion": "distinct",
                    "reason": "The property type, material identity, and value differ from the terminal row.",
                },
                "confidence": 0.9,
            }
        ],
    )
    validated_audit = request["raw_payload"]["object_review_audits"][0]
    evidence_ids = validated_audit["evidence_ids"]
    assert validated_audit["temporary_id"] == temporary_id
    assert validated_audit["dedupe_analysis"]["compared_target_ids"] == [terminal_id]

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(paper_id=paper_id, module_name="dft_results")
        imported = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
        )

    summary = imported["auto_apply_summary"]
    materialized = summary["new_dft_candidates"]["materialized_items"]
    assert summary["new_dft_candidates"]["materialized_count"] == 1
    assert summary["new_dft_candidates"]["skipped_count"] == 0
    assert len(materialized) == 1
    new_dft_id = materialized[0]["dft_result_id"]
    readback = summary["dft_readback"]
    assert new_dft_id in readback["candidate_status"]
    assert new_dft_id not in readback["object_versions"]
    assert new_dft_id in readback["export_safety"]
    assert readback["conflicts"] == []
    assert readback["unfinished_items"] == []

    with Session(mcp_test_env["engine"]) as session:
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == UUID(paper_id))
        ).all()
        assert len(candidates) == 1
        normalized_payload = candidates[0].normalized_payload
        assert normalized_payload["temporary_id"] == temporary_id
        assert normalized_payload["evidence_ids"] == evidence_ids
        assert normalized_payload["dedupe_analysis"]["conclusion"] == "distinct"
        assert normalized_payload["dedupe_analysis"]["compared_target_ids"] == [terminal_id]
        assert candidates[0].materialized_target_id == new_dft_id

        terminal_after = session.get(DFTResult, UUID(terminal_id))
        assert terminal_after is not None
        assert terminal_after.value == pytest.approx(-9.87)
        assert terminal_after.candidate_status == "Rejected"
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == UUID(paper_id))).all()
        assert {str(row.id) for row in rows} == {terminal_id, new_dft_id}


def test_mcp_import_analysis_materializes_new_dft_candidate_with_custom_lock_owner_matching_reviewer(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Custom Lock Owner DFT Paper", pdf_path="mcp-custom-lock-owner.pdf", authors=[])
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    payload = {
        "object_review_audits": [
            {
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "Pt(111)",
                    "property_type": "adsorption_energy",
                    "value": -0.88,
                    "unit": "eV",
                    "adsorbate": "CO",
                    "reaction_step": "adsorption",
                },
                "evidence_location": {
                    "page": 7,
                    "figure": "Fig. 5d",
                    "quoted_text": "CO adsorption energy is -0.88 eV on Pt(111).",
                },
                "confidence": 0.84,
            }
        ]
    }
    request = _validated_local_ai_dft_request(
        mcp_test_env["engine"],
        paper_id,
        payload["object_review_audits"],
    )

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="dft_results",
            locked_by="codex_window_b",
        )
        imported = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            reviewer="codex_window_b",
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
        )

    assert imported["reviewer"] == "codex_window_b"
    assert imported["auto_apply_summary"]["new_dft_candidates"]["materialized_count"] == 1
    with Session(mcp_test_env["engine"]) as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == UUID(paper_id))).all()
        assert len(rows) == 1
        assert rows[0].adsorbate == "CO"
        assert rows[0].value == pytest.approx(-0.88)


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
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            normalized_payload={"verdict": "WARN"},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="external_audit_opinion",
                normalized_payload={
                    "source": "assigned_dft_audit",
                    "source_label": "Assigned AI DFT audit",
                    "agent_role": "dft_auditor",
                    "model_name": "glm-test",
                    "verdict": "WARN",
                    "recommended_action": "verify_against_pdf",
                    "verification_status": "unverified",
                    "confidence": 0.72,
                    "summary": "Check the migration barrier against the source PDF.",
                },
                status="candidate",
                confidence=0.72,
            )
        )
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "paper_id": str(paper.id),
                    "target_type": "dft_results",
                    "target_id": str(row.id),
                    "field_name": "value",
                    "source": "assigned_dft_audit",
                    "source_label": "Assigned AI DFT audit",
                    "agent_role": "dft_auditor",
                    "model_name": "glm-test",
                    "decision": "REVISE",
                    "recommended_action": "propose_correction",
                    "verification_status": "unverified",
                    "confidence": 0.71,
                    "reason": "Object-level check says the numeric value needs PDF review.",
                    "evidence_location": {"page": 7, "table": "Table 1"},
                    "writes_final_truth": False,
                    "confirmation_required": True,
                },
                status="candidate",
                confidence=0.71,
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
    assert "missing_material_identity" in row["blocked_reasons"]
    assert "missing_review" in row["blocked_reasons"]
    assert row["recommended_action"] == "bind_material_identity"
    assert row["sanity_flags"] == []
    assert row["can_mark_verified"] is False
    assert row["evidence_locators"][0]["page"] == 7
    assert row["primary_evidence_locator"]["page"] == 7
    assert row["evidence_page"] == 7
    assert row["pdf_page_url"].endswith(f"/api/papers/{paper_id}/pdf#page=7")
    assert row["latest_external_audit_opinions"][0]["source"] == "assigned_dft_audit"
    assert row["latest_external_audit_opinions"][0]["verification_status"] == "unverified"
    assert row["object_review_audits_count"] == 1
    assert row["object_review_audits"][0]["candidate_type"] == "object_review_audit"
    assert row["object_review_audits"][0]["decision"] == "REVISE"
    assert row["object_review_audits"][0]["verification_status"] == "unverified"
    assert row["object_review_audits"][0]["evidence_location"]["page"] == 7
    assert row["codex_item_url"].endswith(f"/codex-item/dft_result/{row_id}")
    assert row["correction_url"].endswith(f"/dft-results/{row_id}/corrections")

    with Session(mcp_test_env["engine"]) as session:
        stored_row = session.get(DFTResult, UUID(row_id))
        assert stored_row.candidate_status == "system_candidate"


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

    assert rejected["status"] == "requires_ai_verify_content"
    assert rejected["writes_final_truth"] is False
    assert active_queue["rows"]
    assert rejected_queue["rows"] == []


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


def test_propose_correction_requires_module_lock_for_abstract(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="AI Review Lock Target", abstract="Old abstract", pdf_path="review-lock.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        with pytest.raises(ValueError, match="module_write_lock_required:metadata"):
            propose_correction(
                paper_id=paper_id,
                field_name="abstract",
                target_path="abstract",
                operation="replace",
                proposed_value="Rejected unlocked abstract",
                reason="AI proposes a safer abstract.",
            )
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="metadata",
            locked_by="claude",
        )
        correction = propose_correction(
            paper_id=paper_id,
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="Locked abstract",
            reason="AI proposes a safer abstract.",
            write_lock_token=lock["lock_token"],
        )

        assert correction["status"] == "approved"
        assert correction["reviewed_by"] == "claude"

    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(Paper, UUID(paper_id))
        assert updated is not None
        assert updated.abstract == "Locked abstract"


def test_import_analysis_defaults_reviewer_to_mcp_source_for_lock_validation(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Default Reviewer Lock Target", abstract="Old abstract", pdf_path="review-lock.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="content",
        )
        imported = import_analysis(
            paper_id=paper_id,
            source="claude_overall_review",
            source_label="claude_overall_review",
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
            raw_payload={
                "correction_proposals": [
                    {
                        "field_name": "abstract",
                        "target_path": "abstract",
                        "operation": "replace",
                        "proposed_value": "Rewritten abstract",
                        "reason": "Evidence-backed rewrite.",
                        "evidence_payload": {"page": 1, "quoted_text": "Old abstract"},
                    }
                ]
            },
        )

    assert imported["reviewer"] == "claude"
    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(Paper, UUID(paper_id))
        assert updated is not None
        assert updated.abstract == "Old abstract"


def test_import_analysis_custom_reviewer_does_not_break_mcp_lock_validation(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Custom Reviewer Lock Target", abstract="Old abstract", pdf_path="custom-review-lock.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="content",
        )
        imported = import_analysis(
            paper_id=paper_id,
            source="claude_overall_review",
            source_label="claude_overall_review",
            reviewer="codex_window_b",
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
            raw_payload={
                "correction_proposals": [
                    {
                        "field_name": "abstract",
                        "target_path": "abstract",
                        "operation": "replace",
                        "proposed_value": "Rewritten with custom reviewer label",
                        "reason": "Evidence-backed rewrite.",
                        "evidence_payload": {"page": 1, "quoted_text": "Old abstract"},
                    }
                ]
            },
        )

    assert imported["reviewer"] == "codex_window_b"
    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(Paper, UUID(paper_id))
        assert updated is not None
        assert updated.abstract == "Old abstract"


def test_import_analysis_custom_lock_owner_matching_reviewer_is_accepted(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Custom Lock Owner Target", abstract="Old abstract", pdf_path="custom-lock-owner.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="content",
            locked_by="codex_window_b",
        )
        imported = import_analysis(
            paper_id=paper_id,
            source="claude_overall_review",
            source_label="claude_overall_review",
            reviewer="codex_window_b",
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
            raw_payload={
                "correction_proposals": [
                    {
                        "field_name": "abstract",
                        "target_path": "abstract",
                        "operation": "replace",
                        "proposed_value": "Rewritten with custom lock owner",
                        "reason": "Evidence-backed rewrite.",
                        "evidence_payload": {"page": 1, "quoted_text": "Old abstract"},
                    }
                ]
            },
        )

    assert imported["reviewer"] == "codex_window_b"
    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(Paper, UUID(paper_id))
        assert updated is not None
        assert updated.abstract == "Old abstract"


def test_import_analysis_object_review_can_apply_paper_type_metadata(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Object Review Paper Type Target", paper_type="A", pdf_path="paper-type-review.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="metadata",
        )
        imported = import_analysis(
            paper_id=paper_id,
            source="claude_object_review",
            source_label="claude_object_review",
            auto_apply_review_rules=True,
            write_lock_token=lock["lock_token"],
            raw_payload={
                "object_review_audits": [
                    {
                        "target_type": "paper",
                        "target_id": paper_id,
                        "field_name": "paper_type",
                        "decision": "revise",
                        "corrected_value": "B",
                        "confidence": 0.94,
                        "evidence_location": {"page": 1, "quoted_text": "This paper surveys prior computational studies."},
                    }
                ]
            },
        )

    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(Paper, UUID(paper_id))
        assert updated is not None
        assert updated.paper_type == "A"


def test_non_dft_proposals_apply_immediately_and_leave_queue_empty(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Review Target", abstract="Old abstract", pdf_path="review.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="metadata",
            locked_by="claude",
        )
        first = propose_correction(
            paper_id=paper_id,
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="Approved abstract",
            reason="Better aligned with PDF abstract.",
            write_lock_token=lock["lock_token"],
        )
        second = propose_correction(
            paper_id=paper_id,
            field_name="title",
            target_path="title",
            operation="replace",
            proposed_value="Rejected title",
            reason="This one should be rejected.",
        )
        assert first["status"] == "approved"
        assert second["status"] == "approved"

    with mcp_auth_context(_admin_auth()):
        queue = get_correction_queue()
        assert queue["items"] == []

    with Session(mcp_test_env["engine"]) as session:
        paper = session.get(Paper, UUID(first["paper_id"]))
        assert paper is not None
        assert paper.abstract == "Approved abstract"
        assert paper.title == "Rejected title"

        logs = session.scalars(select(AuditLog).order_by(AuditLog.created_at.asc())).all()
        actions = [log.action for log in logs]
        assert "approve_correction" in actions
        assert "reject_correction" not in actions


def test_admin_mcp_review_flow_accepts_paper_type_alias_target_path(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="Paper Type Review Target", paper_type="A", pdf_path="review.pdf")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    with mcp_auth_context(_auth()):
        correction = propose_correction(
            paper_id=paper_id,
            field_name="paper_type",
            target_path="paper.paper_type",
            operation="replace",
            proposed_value="B",
            reason="The PDF content matches B rather than A.",
            evidence_payload={"page": 1, "quoted_text": "Review-style summary of prior work."},
        )

        assert correction["status"] == "approved"

    with Session(mcp_test_env["engine"]) as session:
        updated = session.get(Paper, UUID(paper_id))
        assert updated is not None
        assert updated.paper_type == "B"


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
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="mechanism_claims",
            locked_by="claude",
        )
        correction = propose_correction(
            paper_id=paper_id,
            field_name="mechanism_claims",
            target_path=f"mechanism_claims:{claim_id}:claim_text",
            operation="replace",
            proposed_value="Fe-N4 is associated with reduced shuttle behavior under the reported test conditions.",
            reason="The original wording overstates causality compared with the source text.",
            write_lock_token=lock["lock_token"],
        )

    with mcp_auth_context(_admin_auth()):
        detail = get_correction_detail(correction["id"])
        assert detail["current_value"] == "Fe-N4 is associated with reduced shuttle behavior under the reported test conditions."
        assert detail["proposed_value"] == "Fe-N4 is associated with reduced shuttle behavior under the reported test conditions."
        assert detail["status"] == "approved"

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
        monkeypatch.setenv("LITAI_LOCAL_INGEST_ROOTS", str(folder))
        get_settings.cache_clear()
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

        async def fake_ingest_pdf(
            self,
            source_path,
            original_filename,
            copy_pdf=True,
            external_metadata=None,
            source_reference=None,
            ingest_source=None,
        ):
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
    with TestClient(app, base_url="http://localhost") as client:
        invalid_host = client.get(
            "/mcp/",
            headers={
                "Host": "evil.example",
                "Authorization": "Bearer litmcp_claude",
            },
        )
        assert invalid_host.status_code == 421
        assert invalid_host.text == "Invalid Host header"

        response = client.get("/mcp")
        assert response.status_code == 401
        assert response.json()["detail"] == "Missing MCP API key"

        invalid_key = client.get("/mcp", headers={"Authorization": "Bearer not-a-real-key"})
        assert invalid_key.status_code == 401
        assert invalid_key.json()["detail"] == "Invalid MCP API key"

        authorized = client.get("/mcp", headers={"Authorization": "Bearer litmcp_claude"})
        assert authorized.status_code not in {401, 403, 421}


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


def test_mcp_apply_analysis_review_rules_materializes_deferred_dft_candidate(mcp_test_env):
    """A run imported with auto_apply_review_rules=False must be materializable
    later via the apply_analysis_review_rules MCP tool — the counterpart of
    HTTP POST /runs/{run_id}/apply-review-rules.
    """
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP Deferred DFT Paper", pdf_path="mcp-deferred.pdf", authors=[])
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    payload = {
        "object_review_audits": [
            {
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "Co-N3",
                    "property_type": "adsorption_energy",
                    "value": -0.95,
                    "unit": "eV",
                    "adsorbate": "H",
                    "reaction_step": "adsorption",
                },
                "evidence_location": {
                    "page": 5,
                    "quoted_text": "Co-N3 H -0.95 eV",
                },
                "confidence": 0.88,
            }
        ]
    }
    request = _validated_local_ai_dft_request(
        mcp_test_env["engine"],
        paper_id,
        payload["object_review_audits"],
    )

    with mcp_auth_context(_auth()):
        imported = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=False,
        )

    run_id = imported["run_id"]
    assert imported["auto_apply_summary"] is None
    assert imported["candidate_count"] == 1

    # Pre-condition: no DFTResult yet, candidate still in "candidate" status
    with Session(mcp_test_env["engine"]) as session:
        dft_rows = session.scalars(
            select(DFTResult).where(DFTResult.paper_id == UUID(paper_id))
        ).all()
        assert len(dft_rows) == 0
        candidate = session.scalar(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.run_id == UUID(run_id)
            )
        )
        assert candidate is not None
        assert candidate.status == "candidate"

    with mcp_auth_context(_auth()):
        result = apply_analysis_review_rules(run_id=run_id)

    assert result["run_id"] == run_id
    assert result["paper_id"] == paper_id
    assert result["reviewer"] == "claude"
    assert result["candidate_count"] == 1
    assert result["auto_apply_summary"]["new_dft_candidates"]["materialized_count"] == 1

    candidate_payload = result["candidates"][0]
    assert candidate_payload["status"] == "pending_ai_verification"
    assert candidate_payload["materialized_target_type"] == "dft_results"
    assert candidate_payload["materialized_target_id"] is not None

    with Session(mcp_test_env["engine"]) as session:
        dft_rows = session.scalars(
            select(DFTResult).where(DFTResult.paper_id == UUID(paper_id))
        ).all()
        assert len(dft_rows) == 1
        assert dft_rows[0].candidate_status == "new_candidate"
        assert dft_rows[0].value == pytest.approx(-0.95)
        assert dft_rows[0].adsorbate == "H"

        # No active dft_results lock leaked
        active_locks = session.scalars(
            select(ModuleWriteLock).where(
                ModuleWriteLock.paper_id == UUID(paper_id),
                ModuleWriteLock.module_name == "dft_results",
                ModuleWriteLock.status == "active",
            )
        ).all()
        assert active_locks == [], "apply_analysis_review_rules leaked an active dft_results lock"


def test_mcp_apply_analysis_review_rules_preserves_multi_owner_lock_validation(mcp_test_env):
    """When the MCP auth source_prefix differs from the reviewer, and a lock was
    pre-acquired by auth.source_prefix, apply_analysis_review_rules must accept
    the caller's token because effective_lock_owners includes both identities.
    """
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(
            title="MCP Owner Semantics DFT Paper", pdf_path="mcp-owner-semantics.pdf", authors=[]
        )
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    payload = {
        "object_review_audits": [
            {
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "Fe-N4",
                    "property_type": "adsorption_energy",
                    "value": -1.23,
                    "unit": "eV",
                    "adsorbate": "Li2S4",
                    "reaction_step": "adsorption",
                },
                "evidence_location": {
                    "page": 3,
                    "table": "Table 1",
                    "quoted_text": "The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
                },
                "confidence": 0.9,
            }
        ]
    }
    request = _validated_local_ai_dft_request(
        mcp_test_env["engine"],
        paper_id,
        payload["object_review_audits"],
    )

    with mcp_auth_context(_auth()):
        # Import without auto-apply so candidates remain pending
        imported = import_analysis(
            paper_id=paper_id,
            source=request["source"],
            source_label=request["source_label"],
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=False,
        )
        run_id = imported["run_id"]

        # Pre-acquire a dft_results lock locked_by = auth.source_prefix ("claude")
        lock = acquire_module_write_lock(
            paper_id=paper_id,
            module_name="dft_results",
        )
        assert lock["locked_by"] == "claude"

        # Apply with reviewer="codex_window_b" and the pre-acquired token.
        # effective_lock_owners = ["claude", "codex_window_b"], so the token
        # locked_by="claude" should be accepted.
        result = apply_analysis_review_rules(
            run_id=run_id,
            reviewer="codex_window_b",
            write_lock_token=lock["lock_token"],
        )

        # The caller-provided lock is NOT auto-released by the apply step
        # (only auto-acquired locks are). Release it explicitly.
        release_module_write_lock(lock_token=lock["lock_token"])

    assert result["reviewer"] == "codex_window_b"
    assert result["auto_apply_summary"]["new_dft_candidates"]["materialized_count"] == 1

    with Session(mcp_test_env["engine"]) as session:
        dft_rows = session.scalars(
            select(DFTResult).where(DFTResult.paper_id == UUID(paper_id))
        ).all()
        assert len(dft_rows) == 1
        assert dft_rows[0].candidate_status == "new_candidate"
        assert dft_rows[0].adsorbate == "Li2S4"

        # No active dft_results lock leaked after explicit release
        active_locks = session.scalars(
            select(ModuleWriteLock).where(
                ModuleWriteLock.paper_id == UUID(paper_id),
                ModuleWriteLock.module_name == "dft_results",
                ModuleWriteLock.status == "active",
            )
        ).all()
        assert active_locks == [], "active dft_results lock remains after explicit release"


def test_mcp_review_identity_helper_consistency():
    """Unit test for the _mcp_review_identity helper shared by import_analysis
    and apply_analysis_review_rules. Verifies the three documented identity
    contracts without touching the database.
    """
    # Case 1: reviewer=None, source_prefix="claude" -> all collapse to "claude"
    auth = MCPAuthInfo(
        source_prefix="claude",
        display_name="claude",
        capabilities=frozenset(),
        raw_key="",
        source_identity="mcp:claude",
        identity_verified=True,
    )
    reviewer, internal, owners = _mcp_review_identity(None, auth)
    assert reviewer == "claude"
    assert internal == "claude"
    assert owners == ["claude"]

    # Case 2: reviewer="codex_window_b", source_prefix="claude"
    # -> reviewer=codex_window_b, internal=claude, owners=[claude, codex_window_b]
    reviewer, internal, owners = _mcp_review_identity("codex_window_b", auth)
    assert reviewer == "codex_window_b"
    assert internal == "claude"
    assert owners == ["claude", "codex_window_b"]

    # Case 3: reviewer="claude" == source_prefix="claude" -> dedup to ["claude"]
    reviewer, internal, owners = _mcp_review_identity("claude", auth)
    assert reviewer == "claude"
    assert internal == "claude"
    assert owners == ["claude"]


def test_ai_verify_content_capability_is_distinct_and_dry_run_is_zero_write(mcp_test_env):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion."
    pdf_path = mcp_test_env["tmpdir"] / "single-ai-mcp.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), evidence)
    document.save(pdf_path)
    document.close()

    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP single AI", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="mechanism",
            claim_text=evidence,
            evidence_types=["pdf_text"],
            evidence_text=evidence,
        )
        session.add(claim)
        session.commit()
        paper_id = str(paper.id)
        claim_id = str(claim.id)
        fingerprint = ai_target_fingerprint("mechanism_claims", claim)

    with pytest.raises(PermissionError, match="authentication context"):
        get_ai_verification_tasks(paper_id=paper_id)

    with mcp_auth_context(_admin_auth()):
        with pytest.raises(PermissionError, match="ai_verify_content"):
            get_ai_verification_tasks(paper_id=paper_id)

    with mcp_auth_context(_single_verifier_auth()):
        tasks = get_ai_verification_tasks(paper_id=paper_id)
        result = submit_ai_verification_batch(
            paper_id=paper_id,
            dry_run=True,
            submissions=[
                {
                    "target_type": "mechanism_claims",
                    "target_id": claim_id,
                    "field_name": "claim_text",
                    "decision": "accept",
                    "confidence": 0.98,
                    "evidence_text": evidence,
                    "page": 1,
                    "reasoning_summary": "Direct support on the PDF page.",
                    "expected_target_fingerprint": fingerprint,
                }
            ],
        )

    assert tasks["single_ai"] is True
    assert tasks["second_ai_used"] is False
    assert tasks["total"] == 1
    assert tasks["returned"] == 1
    assert tasks["offset"] == 0
    assert tasks["has_more"] is False
    assert tasks["next_offset"] is None
    assert tasks["task_count"] == 1
    assert result["dry_run"] is True
    assert result["auto_repaired"] == 1
    assert result["database_writes"] is False
    with Session(mcp_test_env["engine"]) as session:
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 0
        assert session.scalar(select(func.count(AuditLog.id))) == 0

    with mcp_auth_context(_single_verifier_auth()):
        formal = submit_ai_verification_batch(
            paper_id=paper_id,
            dry_run=False,
            submissions=[
                {
                    "target_type": "mechanism_claims",
                    "target_id": claim_id,
                    "field_name": "claim_text",
                    "decision": "accept",
                    "confidence": 0.98,
                    "evidence_text": evidence,
                    "page": 1,
                    "reasoning_summary": "Direct support on the PDF page.",
                    "expected_target_fingerprint": fingerprint,
                }
            ],
        )
        coverage = get_review_coverage(paper_id=paper_id)

    assert formal["auto_repaired"] == 1
    assert coverage["mechanism_claims"]["human_verified"] == 0
    assert coverage["mechanism_claims"]["ai_verified"] == 1
    assert coverage["mechanism_claims"]["can_use_for_writing"] == 1
    assert coverage["mechanism_claims"]["can_use_for_citation"] == 1
    with Session(mcp_test_env["engine"]) as session:
        review = session.scalar(select(ExtractionFieldReview))
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "single_ai_verification_decision"))
        assert review.reviewer_status == "ai_verified"
        assert review.reviewer != "human"
        assert audit.source == "mcp:single_verifier"


def test_ai_verification_task_mcp_pagination_contract_has_no_gaps_duplicates_or_writes(mcp_test_env):
    with Session(mcp_test_env["engine"]) as session:
        paper = Paper(title="MCP pagination", pdf_path="mcp-pagination.pdf", authors=["Tester"])
        other_paper = Paper(
            title="MCP pagination isolation",
            pdf_path="mcp-pagination-isolation.pdf",
            authors=["Tester"],
        )
        session.add_all([paper, other_paper])
        session.flush()
        claims = [
            MechanismClaim(
                paper_id=paper.id,
                claim_type="mechanism",
                claim_text=f"MCP paged claim {index:02d}",
                evidence_types=["pdf_text"],
                evidence_text=f"MCP paged evidence {index:02d}",
            )
            for index in range(22)
        ]
        isolated_claim = MechanismClaim(
            paper_id=other_paper.id,
            claim_type="mechanism",
            claim_text="Other paper claim",
            evidence_types=["pdf_text"],
            evidence_text="Other paper evidence",
        )
        session.add_all([*claims, isolated_claim])
        session.commit()
        paper_id = str(paper.id)
        expected_ids = {str(claim.id) for claim in claims}
        isolated_id = str(isolated_claim.id)

    with mcp_auth_context(_single_verifier_auth()):
        first = get_ai_verification_tasks(
            paper_id=paper_id,
            limit=20,
            offset=0,
            recover_evidence=False,
            target_type="mechanism_claims",
        )
        second = get_ai_verification_tasks(
            paper_id=paper_id,
            limit=20,
            offset=first["next_offset"],
            recover_evidence=False,
            target_type="mechanism_claims",
        )

    first_ids = [task["target_id"] for task in first["tasks"]]
    second_ids = [task["target_id"] for task in second["tasks"]]
    combined = first_ids + second_ids
    assert first["total"] == second["total"] == 22
    assert first["returned"] == 20
    assert first["has_more"] is True
    assert first["next_offset"] == 20
    assert second["offset"] == 20
    assert second["returned"] == 2
    assert second["has_more"] is False
    assert second["next_offset"] is None
    assert len(combined) == len(set(combined)) == 22
    assert set(combined) == expected_ids
    assert isolated_id not in combined
    assert first["database_writes"] is False
    assert second["database_writes"] is False
    assert first["postgres_transaction_read_only"] is True
    assert second["postgres_transaction_read_only"] is True

    with Session(mcp_test_env["engine"]) as session:
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 0
        assert session.scalar(select(func.count(AuditLog.id))) == 0
        assert session.scalar(select(func.count(EvidenceLocator.id))) == 0
