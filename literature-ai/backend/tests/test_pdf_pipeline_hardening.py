from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.config import Settings, get_settings
from app.db.models import Base, Paper, PaperChunk, PaperSection
from app.parsers.body_boundary_cleaner import BodyBoundaryCleaner
from app.parsers.docling_parser import DoclingParser
from app.schemas.documents import UnifiedTable
from app.services.artifact_store import ArtifactStore
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_query import PaperQueryService
from app.services.pdf_image_extractor import PdfImageExtractor
from app.services.parse_quality_auditor import ParseQualityAuditor
from app.services.paper_workbench_service import PaperWorkbenchService


def test_ocr_policy_auto_enables_without_forcing_full_page():
    settings = Settings(
        docling_do_ocr=False,
        docling_auto_ocr=True,
        docling_force_full_page_ocr=False,
    )
    parser = DoclingParser(settings)

    assert parser._ocr_enabled(ocr_required=True) is True
    assert parser._ocr_enabled(ocr_required=False) is False
    assert settings.docling_force_full_page_ocr is False


def test_scanned_quality_remains_human_confirmation_gated():
    report = PaperWorkbenchService._quality_report(
        status="C_scan_clear",
        score=0.1,
        reason="scan_or_image_pdf_requires_ocr",
        metrics={},
        parse_allowed=True,
        created_at="2026-06-19T00:00:00Z",
        ocr_enabled=True,
    )

    assert report["ocr_policy"] == {
        "ocr_enabled": True,
        "ocr_required": True,
        "ocr_text_must_be_marked": True,
    }
    assert report["markdown_trust"] == "ocr_required_candidate"
    assert report["needs_human_confirmation"] is True
    assert report["parse_allowed"] is False


def test_scanned_pdf_is_never_allowed_into_initial_parsing(tmp_path, monkeypatch):
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-test")

    class ScanPage:
        @staticmethod
        def get_text(kind):
            return {"blocks": [{"type": 1}]} if kind == "dict" else ""

        @staticmethod
        def get_images(*, full):
            return [(1,)]

    class ScanDocument(list):
        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda _: ScanDocument([ScanPage(), ScanPage()])))
    report = PaperWorkbenchService.assess_pdf_path(
        pdf_path,
        Settings(docling_do_ocr=True, docling_auto_ocr=True),
    )

    assert report["quality_status"] == "C_scan_clear"
    assert report["parse_allowed"] is False
    assert report["needs_human_confirmation"] is True
    assert PaperIngestionService._quality_allows_initial_parse(report) is False


@pytest.mark.asyncio
async def test_quality_blocked_ingest_skips_grobid_and_docling(tmp_path, monkeypatch):
    pdf_path = tmp_path / "poor.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    report = PaperWorkbenchService._quality_report(
        status="D_scan_unclear",
        score=0.05,
        reason="too_little_text_or_image_signal",
        metrics={},
        parse_allowed=False,
        created_at="2026-06-19T00:00:00Z",
    )
    monkeypatch.setattr(PaperWorkbenchService, "assess_pdf_path", lambda *_: report)

    async def parser_must_not_run(_):
        raise AssertionError("quality-blocked PDFs must not reach a parser")

    service = PaperIngestionService.__new__(PaperIngestionService)
    service.settings = SimpleNamespace(auto_enrich_ingested_metadata=False)
    service.session = SimpleNamespace()
    service.grobid_parser = SimpleNamespace(parse_pdf=parser_must_not_run)
    service.docling_parser = SimpleNamespace(parse_pdf=parser_must_not_run)
    service.identity = SimpleNamespace(find_metadata_placeholder=lambda *_, **__: None)
    service._build_identity_metadata = lambda *_, **__: {"title": "Poor PDF", "doi": None, "year": None, "arxiv_id": None}
    service._find_conflicting_paper = lambda **_: None
    captured = {}

    def fake_persist(document, *_, **kwargs):
        captured["document"] = document
        captured["oa_status"] = kwargs["oa_status"]
        captured["quality_report"] = kwargs["quality_report"]
        return SimpleNamespace(id="paper-id")

    service._persist = fake_persist

    paper = await service.ingest_pdf(
        source_path=pdf_path,
        original_filename="poor.pdf",
        copy_pdf=False,
        external_metadata={"title": "Poor PDF"},
    )

    assert paper._ingest_status == "completed"
    assert captured["oa_status"] == "quality_blocked"
    assert captured["quality_report"]["parse_allowed"] is False
    assert captured["document"].sections == []
    assert captured["document"].markdown == ""


@pytest.mark.asyncio
async def test_quality_blocked_reparse_skips_parsers_and_clears_parsed_entities(tmp_path, monkeypatch):
    pdf_path = tmp_path / "poor-reparse.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    report = PaperWorkbenchService._quality_report(
        status="C_scan_clear",
        score=0.1,
        reason="scan_or_image_pdf_requires_ocr",
        metrics={},
        parse_allowed=False,
        created_at="2026-06-19T00:00:00Z",
    )
    monkeypatch.setattr(PaperWorkbenchService, "assess_pdf_path", lambda *_: report)

    async def parser_must_not_run(_):
        raise AssertionError("quality-blocked PDFs must not reach a parser")

    paper = SimpleNamespace(
        id="paper-id",
        pdf_path=str(pdf_path),
        title="Existing",
        doi="10.1000/existing",
        year=2024,
        journal="Journal",
        authors=["Author"],
        abstract="Existing abstract",
        source_path=None,
        oa_status="reparsed",
        workflow_status="Parsed_Material_Ready",
        comprehensive_analysis={"old": True},
    )
    service = PaperIngestionService.__new__(PaperIngestionService)
    service.settings = Settings(storage_root=tmp_path / "storage")
    service.session = SimpleNamespace(get=lambda *_: paper)
    service.grobid_parser = SimpleNamespace(parse_pdf=parser_must_not_run)
    service.docling_parser = SimpleNamespace(parse_pdf=parser_must_not_run)
    cleared = []
    stage2_cleared = []
    service._clear_document_entities = cleared.append
    service.extraction_pipeline = SimpleNamespace(_delete_existing_stage2=stage2_cleared.append)
    service._merge_into_existing_paper = lambda existing, document, **_: existing

    reparsed = await service.reparse_existing_paper("paper-id")

    assert reparsed is paper
    assert cleared == ["paper-id"]
    assert stage2_cleared == ["paper-id"]
    assert paper.comprehensive_analysis is None
    assert paper.oa_status == "quality_blocked"


def test_empty_text_fallback_is_blocked_not_warning_section(tmp_path, monkeypatch):
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-test")

    class EmptyPage:
        @staticmethod
        def extract_text():
            return ""

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda _: SimpleNamespace(pages=[EmptyPage()])))

    result = DoclingParser._fallback_parse(pdf_path)

    assert result.markdown == ""
    assert result.page_blocks == []
    assert result.json_payload["parse_blocked"] is True
    assert result.json_payload["parse_quality"]["ocr_required"] is True


@pytest.mark.parametrize("reader", ["raises", "no_pages"])
def test_failed_or_pageless_fallback_keeps_warning_out_of_body(tmp_path, monkeypatch, reader):
    pdf_path = tmp_path / "unreadable.pdf"
    pdf_path.write_bytes(b"%PDF-test")

    def fake_reader(_):
        if reader == "raises":
            raise RuntimeError("fixture read failure")
        return SimpleNamespace(pages=[])

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=fake_reader))
    result = DoclingParser._fallback_parse(pdf_path)

    assert result.markdown == ""
    assert result.page_blocks == []
    assert result.json_payload["pages"] == []
    assert result.json_payload["parse_blocked"] is True
    assert result.json_payload["parse_warning"].startswith("[Warning]")


def test_text_fallback_still_produces_normal_body_content(tmp_path, monkeypatch):
    pdf_path = tmp_path / "text.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    page = SimpleNamespace(extract_text=lambda: "Normal extracted paragraph with DOI 10.2000/body123.")
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=lambda _: SimpleNamespace(pages=[page])))

    result = DoclingParser._fallback_parse(pdf_path)

    assert result.json_payload.get("parse_blocked") is not True
    assert "Normal extracted paragraph" in result.markdown
    assert result.page_blocks[0]["text"].startswith("Normal extracted paragraph")


def test_running_header_footer_cleanup_is_boundary_only():
    pages = [
        {
            "page": 1,
            "text": "Journal of Careful Parsing\nhttps://doi.org/10.1000/header\nBody paragraph discusses DOI 10.2000/body in the experiment.\n1",
        },
        {
            "page": 2,
            "text": "Journal of Careful Parsing\nhttps://doi.org/10.1000/header\nSecond body paragraph about Fe-N-C material.\n2",
        },
        {
            "page": 3,
            "text": "Journal of Careful Parsing\nhttps://doi.org/10.1000/header\nThird body paragraph.\n3",
        },
    ]

    cleaned = DoclingParser._clean_running_headers_footers(pages)

    combined = "\n".join(page["text"] for page in cleaned)
    assert "Journal of Careful Parsing" not in combined
    assert "10.1000/header" not in combined
    assert "10.2000/body" in combined
    assert "Fe-N-C material" in combined


def test_single_page_boundary_analysis_does_not_remove_body_text():
    pages = [{"page": 1, "text": "Journal name\nDOI 10.1000/body\n2024\n1\nA short result."}]
    plan = BodyBoundaryCleaner.analyze(pages)

    assert not plan.removable_signatures
    assert BodyBoundaryCleaner.clean_page_blocks(pages, plan) == pages


def _unified_builder(tmp_path, monkeypatch):
    settings = Settings(storage_root=tmp_path / "storage", embedding_provider="deterministic")
    service = PaperIngestionService.__new__(PaperIngestionService)
    service.artifacts = ArtifactStore(settings)
    monkeypatch.setattr(PdfImageExtractor, "extract_figures", lambda **_: None)
    return service


def _sqlite_ingestion_service(tmp_path):
    db_path = tmp_path / "pipeline_hardening.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_url=f"sqlite:///{db_path}",
        embedding_provider="deterministic",
    )
    service = PaperIngestionService(session=session, settings=settings)
    return engine, session, service


@pytest.mark.asyncio
async def test_unified_document_cleans_markdown_sections_and_chunks(tmp_path, monkeypatch):
    service = _unified_builder(tmp_path, monkeypatch)
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    raw_pages = [
        {"page": 1, "text": "Journal of Careful Parsing\nhttps://doi.org/10.1000/header123\nActual Article Title\nDOI: 10.2000/body123\nBody DOI discussion remains intact.\n1"},
        {"page": 2, "text": "Journal of Careful Parsing\nhttps://doi.org/10.1000/header123\nResults retain the year 2024 and section number 1.1.\n2"},
    ]
    plan = BodyBoundaryCleaner.analyze(raw_pages)
    docling_result = SimpleNamespace(
        markdown=(
            "Journal of Careful Parsing\nhttps://doi.org/10.1000/header123\nActual Article Title\n"
            "DOI: 10.2000/body123\nBody DOI discussion remains intact.\n1\n"
            "Journal of Careful Parsing\nhttps://doi.org/10.1000/header123\n"
            "Results retain the year 2024 and section number 1.1.\n2"
        ),
        json_payload={"pages": raw_pages, "parse_quality": {"boundary_cleanup": plan.to_metadata()}},
        tables=[],
        figures=[],
        page_blocks=raw_pages,
    )
    grobid_result = SimpleNamespace(
        metadata={"title": "paper.pdf"},
        abstract="",
        sections=[
            {
                "section_title": "Results",
                "section_type": "results",
                "text": "Journal of Careful Parsing\nhttps://doi.org/10.1000/header123\nDOI: 10.2000/body123\nBody DOI discussion remains intact.\n2024\nSection 1.1",
                "page_start": None,
                "page_end": None,
            }
        ],
        references=[],
        tei_xml="<TEI/>",
    )

    document = await service._build_unified_document(pdf_path, grobid_result, docling_result)
    persisted_markdown = document.markdown_path.read_text(encoding="utf-8")
    section_text = "\n".join(section.text for section in document.sections)
    chunks = PaperIngestionService._chunk_text(section_text, max_tokens=20, overlap=0)

    for value in (document.markdown, persisted_markdown, section_text, "\n".join(chunks)):
        assert "Journal of Careful Parsing" not in value
        assert "10.1000/header123" not in value
    assert "10.2000/body123" in document.markdown
    assert "10.2000/body123" in section_text
    assert "2024" in section_text
    assert "Section 1.1" in section_text
    assert document.metadata["doi"] == "10.2000/body123"
    assert all(block["text"] not in {"1", "2"} for block in document.docling_json["pages"])


@pytest.mark.asyncio
async def test_parse_blocked_preserves_valid_grobid_sections_and_chunks(tmp_path, monkeypatch):
    service = _unified_builder(tmp_path, monkeypatch)
    pdf_path = tmp_path / "grobid-valid.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    warning = "[Warning] Docling failed"
    docling_result = SimpleNamespace(
        markdown=warning,
        json_payload={
            "parse_blocked": True,
            "parse_warning": warning,
            "pages": [{"page": 1, "text": warning}],
            "texts": [{"text": warning}],
            "parse_quality": {"markdown_trust": "unavailable"},
        },
        tables=[],
        figures=[],
        page_blocks=[{"page": 1, "text": warning}],
    )
    grobid_result = SimpleNamespace(
        metadata={"title": "GROBID title", "doi": "10.2000/grobid123"},
        abstract="GROBID abstract.",
        sections=[
            {
                "section_title": "Results",
                "section_type": "results",
                "text": "Valid GROBID body about adsorption kinetics.",
                "page_start": None,
                "page_end": None,
            }
        ],
        references=[{"title": "Reference retained"}],
        tei_xml="<TEI><text>retained</text></TEI>",
    )

    document = await service._build_unified_document(pdf_path, grobid_result, docling_result)
    chunks = [
        chunk
        for section in document.sections
        for chunk in PaperIngestionService._chunk_text(section.text, max_tokens=20, overlap=0)
    ]

    assert document.markdown == ""
    assert [section.text for section in document.sections] == ["Valid GROBID body about adsorption kinetics."]
    assert chunks == ["Valid GROBID body about adsorption kinetics."]
    assert warning not in "\n".join(chunks)
    assert document.abstract == "GROBID abstract."
    assert document.references == [{"title": "Reference retained"}]
    assert document.metadata["doi"] == "10.2000/grobid123"
    assert document.tei_xml == "<TEI><text>retained</text></TEI>"
    assert document.docling_json["pages"] == []
    assert document.docling_json["texts"] == []


@pytest.mark.asyncio
async def test_parse_blocked_cleans_confirmed_boundaries_from_grobid_section(tmp_path, monkeypatch):
    service = _unified_builder(tmp_path, monkeypatch)
    pdf_path = tmp_path / "grobid-cleaned.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    boundary_pages = [
        {"page": 1, "text": "Repeated Journal Header\nhttps://doi.org/10.1000/header123\nFirst body.\n1"},
        {"page": 2, "text": "Repeated Journal Header\nhttps://doi.org/10.1000/header123\nSecond body.\n2"},
    ]
    plan = BodyBoundaryCleaner.analyze(boundary_pages)
    warning = "[Warning] Docling failed after preflight"
    docling_result = SimpleNamespace(
        markdown=warning,
        json_payload={
            "parse_blocked": True,
            "parse_warning": warning,
            "pages": boundary_pages,
            "texts": [{"text": warning}],
            "parse_quality": {"boundary_cleanup": plan.to_metadata()},
        },
        tables=[],
        figures=[],
        page_blocks=[{"page": 1, "text": warning}],
    )
    grobid_result = SimpleNamespace(
        metadata={"title": "GROBID retained"},
        abstract="",
        sections=[
            {
                "section_title": "Discussion",
                "section_type": "discussion",
                "text": (
                    "Repeated Journal Header\nhttps://doi.org/10.1000/header123\n"
                    "The body DOI 10.2000/body123 and material discussion remain."
                ),
                "page_start": None,
                "page_end": None,
            }
        ],
        references=[],
        tei_xml="<TEI/>",
    )

    document = await service._build_unified_document(pdf_path, grobid_result, docling_result)
    section_text = document.sections[0].text

    assert "Repeated Journal Header" not in section_text
    assert "10.1000/header123" not in section_text
    assert "10.2000/body123" in section_text
    assert "material discussion remain" in section_text
    assert document.markdown == ""


@pytest.mark.asyncio
async def test_parse_blocked_is_a_second_ingestion_defense(tmp_path, monkeypatch):
    service = _unified_builder(tmp_path, monkeypatch)
    pdf_path = tmp_path / "blocked.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    warning = "[Warning] forged parser warning"
    docling_result = SimpleNamespace(
        markdown=f"## Page 1\n\n{warning}",
        json_payload={
            "parse_blocked": True,
            "parse_warning": warning,
            "pages": [{"page": 1, "text": warning}],
            "texts": [{"text": warning}],
            "parse_quality": {"markdown_trust": "unavailable"},
        },
        tables=[],
        figures=[],
        page_blocks=[{"page": 1, "text": warning}],
    )
    grobid_result = SimpleNamespace(
        metadata={"title": "blocked.pdf"},
        abstract="",
        sections=[],
        references=[],
        tei_xml="<TEI/>",
    )

    document = await service._build_unified_document(pdf_path, grobid_result, docling_result)

    assert document.markdown == ""
    assert document.markdown_path.read_text(encoding="utf-8") == ""
    assert document.sections == []
    assert document.docling_json["parse_warning"] == warning
    assert document.docling_json["pages"] == []
    assert document.docling_json["texts"] == []
    chunks = [chunk for section in document.sections for chunk in PaperIngestionService._chunk_text(section.text)]
    assert chunks == []


@pytest.mark.asyncio
async def test_unified_document_repairs_truncated_titles_and_preserves_heading_metadata(tmp_path, monkeypatch):
    service = _unified_builder(tmp_path, monkeypatch)
    pdf_path = tmp_path / "section-repair.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    docling_result = SimpleNamespace(
        markdown="## Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems\n\nBody text.",
        json_payload={
            "pages": [{"page": 9, "text": "Body text."}],
            "texts": [
                {
                    "label": "section_header",
                    "text": "Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems",
                    "level": 1,
                    "prov": [{"page_no": 9}],
                }
            ],
            "parse_quality": {"boundary_cleanup": BodyBoundaryCleaner.analyze([{"page": 9, "text": "Body text."}]).to_metadata()},
        },
        tables=[],
        figures=[],
        page_blocks=[{"page": 9, "text": "Body text."}],
    )
    grobid_result = SimpleNamespace(
        metadata={"title": "Section repair"},
        abstract="",
        sections=[
            {
                "section_title": "Catalysis mechanism in",
                "section_type": "body",
                "text": "Body text.",
                "page_start": None,
                "page_end": None,
                "level": 1,
                "section_number": None,
                "parent_title": None,
                "heading_path": ["Catalysis mechanism in"],
            },
            {
                "section_title": "Section 1",
                "section_type": "body",
                "text": "Untitled introduction body.",
                "page_start": None,
                "page_end": None,
                "level": 1,
                "section_number": None,
                "parent_title": None,
                "heading_path": ["Section 1"],
            },
        ],
        references=[],
        tei_xml="<TEI/>",
    )

    document = await service._build_unified_document(pdf_path, grobid_result, docling_result)

    assert document.sections[0].section_title == "Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems"
    assert document.sections[0].section_level == 1
    assert document.sections[0].heading_path == ["Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems"]
    assert document.sections[0].page_start == 9
    assert document.sections[1].section_title is None
    assert document.sections[1].heading_path == []


def test_persisted_sections_keep_heading_metadata_and_figure_captions_do_not_create_chunks(tmp_path):
    engine, session, service = _sqlite_ingestion_service(tmp_path)
    try:
        paper = Paper(title="Structured paper", pdf_path="paper.pdf", authors=[])
        session.add(paper)
        session.flush()
        document = SimpleNamespace(
            sections=[
                SimpleNamespace(
                    section_title="Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems",
                    section_type="body",
                    text="Body section text for retrieval.",
                    page_start=9,
                    page_end=9,
                    section_level=1,
                    section_number="6",
                    parent_heading=None,
                    heading_path=["Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems"],
                )
            ],
            tables=[],
            figures=[
                SimpleNamespace(
                    caption="Fig. 1 | Caption only.",
                    image_path="fig1.png",
                    page=3,
                    figure_role="figure",
                    role_confidence=None,
                    content_summary=None,
                    key_elements=None,
                    numerical_data_points=None,
                    prov=[],
                )
            ],
        )

        service._persist_document_entities(paper, document)
        session.commit()

        sections = session.query(PaperSection).order_by(PaperSection.page_start.asc().nullsfirst(), PaperSection.id).all()
        chunks = session.query(PaperChunk).order_by(PaperChunk.id).all()

        body_section = next(item for item in sections if item.section_type == "body")
        caption_section = next(item for item in sections if item.section_type == "figure_caption")
        assert body_section.section_level == 1
        assert body_section.section_number == "6"
        assert body_section.parent_heading is None
        assert body_section.heading_path == ["Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems"]
        assert all(chunk.section_id != caption_section.id for chunk in chunks)

        payload = PaperQueryService._serialize_section(body_section)
        assert payload.section_level == 1
        assert payload.section_number == "6"
        assert payload.heading_path == ["Catalysis mechanism in Ni1Co1@TiO2 -MgH2 systems"]
    finally:
        session.close()
        engine.dispose()


def test_continued_table_merges_only_with_compatible_header_and_context():
    first = UnifiedTable(
        caption="Table 1. Adsorption energies for sulfur species.",
        markdown_content="| Species | Energy |\n| --- | --- |\n| S8 | -1.2 |",
        page=4,
        extraction_source="docling",
        prov=[{"page_no": 4}],
    )
    continued = UnifiedTable(
        caption="Table 1. Adsorption energies for sulfur species (continued).",
        markdown_content="| Species | Energy |\n| --- | --- |\n| Li2S | -2.4 |",
        page=5,
        extraction_source="docling",
        prov=[{"page_no": 5}],
    )

    cleaned = ParseQualityAuditor.clean_tables([first, continued])

    assert len(cleaned) == 1
    assert "S8" in cleaned[0].markdown_content
    assert "Li2S" in cleaned[0].markdown_content
    assert cleaned[0].prov[-1]["merged_pages"] == [4, 5]

    unrelated = continued.model_copy(
        update={
            "caption": "Table 1. Mechanical properties in a separate appendix.",
            "markdown_content": "| Material | Modulus |\n| --- | --- |\n| Alloy | 90 |",
        }
    )
    assert len(ParseQualityAuditor.clean_tables([first, unrelated])) == 2


def test_structural_chunking_prefers_paragraphs_and_bounds_long_text():
    paragraphs = [
        "# Results\nAlpha beta gamma delta epsilon.",
        "Adsorption remains stable under the tested reaction conditions.",
        "Kinetics improve while the material identity remains unchanged.",
    ]
    chunks = PaperIngestionService._chunk_text("\n\n".join(paragraphs), max_tokens=10, overlap=0)

    assert chunks == paragraphs

    long_paragraph = " ".join(f"token{index}" for index in range(55))
    long_chunks = PaperIngestionService._chunk_text(long_paragraph, max_tokens=20, overlap=4)
    assert len(long_chunks) == 3
    assert all(len(PaperIngestionService._chunk_tokens(chunk)) <= 20 for chunk in long_chunks)


@pytest.mark.asyncio
async def test_deprecated_database_endpoints_default_to_gone(monkeypatch):
    from app.api.system import SwitchDbPayload, switch_db

    monkeypatch.delenv("LITAI_ENABLE_DEPRECATED_DB_ENDPOINTS", raising=False)
    get_settings.cache_clear()
    with pytest.raises(Exception) as exc_info:
        await switch_db(SwitchDbPayload(database_url="sqlite:///legacy.db"))
    assert exc_info.value.status_code == 410
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_agent_guide_common_tool_lists_exclude_review_paper():
    from app.api.system import get_agent_guide

    guide = await get_agent_guide()
    tool_groups = guide["recommended_entrypoint"]["json_schema_hint"]
    advertised_tools = {tool for tools in tool_groups.values() for tool in tools}
    assert "review_paper" not in advertised_tools
    assert {"get_codex_context", "get_codex_item", "read_paper_page", "import_analysis"} <= advertised_tools


@pytest.mark.asyncio
async def test_deprecated_database_endpoint_requires_explicit_enable_and_keeps_guards(monkeypatch):
    from app.api.system import SwitchDbPayload, switch_db, upload_db

    monkeypatch.setenv("LITAI_ENABLE_DEPRECATED_DB_ENDPOINTS", "true")
    monkeypatch.setenv("LITAI_FORCE_CONFIGURED_DATABASE", "true")
    get_settings.cache_clear()
    switched: list[str] = []
    monkeypatch.setattr("app.db.session.switch_database", switched.append)

    with pytest.raises(Exception) as force_exc:
        await switch_db(SwitchDbPayload(database_url="sqlite:///legacy.db"))
    assert force_exc.value.status_code == 400
    assert switched == []

    monkeypatch.setenv("LITAI_FORCE_CONFIGURED_DATABASE", "false")
    get_settings.cache_clear()

    response = await switch_db(SwitchDbPayload(database_url="sqlite:///legacy.db"))
    assert response["status"] == "ok"
    assert switched == ["sqlite:///legacy.db"]

    with pytest.raises(Exception) as exc_info:
        await switch_db(SwitchDbPayload(database_url="postgresql://unsafe"))
    assert exc_info.value.status_code == 400
    with pytest.raises(Exception) as upload_exc:
        await upload_db(UploadFile(filename="not-a-db.txt", file=BytesIO(b"x")))
    assert upload_exc.value.status_code == 400
    with pytest.raises(Exception) as traversal_exc:
        await upload_db(UploadFile(filename="../unsafe.db", file=BytesIO(b"x")))
    assert traversal_exc.value.status_code == 400
    get_settings.cache_clear()


def test_frontend_has_no_writer_secret_inputs_or_review_paper_tool():
    backend_root = Path(__file__).resolve().parents[1]
    settings_html = (backend_root.parent / "frontend/pages/settings/index.html").read_text(encoding="utf-8")
    library_api = (backend_root.parent / "frontend/pages/literature_library/api.js").read_text(encoding="utf-8")
    review_center = (backend_root.parent / "frontend/pages/review_center/index.html").read_text(encoding="utf-8")

    assert 'name="writer_api_key"' not in settings_html
    assert 'name="writer_api_base"' not in settings_html
    assert "writer_api_key" not in library_api
    assert "writer_api_base" not in library_api
    assert "review_paper(" not in review_center
