import os
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    DFTSetting,
    MechanismClaim,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperRelationship,
    PaperNote,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.schemas.api import PaperListFilterParams
from app.services.paper_query import PaperQueryService, _cached_pdf_size_for_storage


def test_paper_query_service_returns_counts_and_detail_payload():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Queryable Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()

            session.add(PaperSection(paper_id=paper.id, section_title="Intro", section_type="introduction", text="Intro text", page_start=1, page_end=1))
            session.add(PaperTable(paper_id=paper.id, caption="Table 1", markdown_content="|a|b|", page=2, extraction_source="docling"))
            session.add(PaperFigure(paper_id=paper.id, caption="Figure 1", image_path=None, page=3, figure_role="summary"))
            session.add(DFTSetting(paper_id=paper.id, software="VASP", raw_json={}))
            session.add(CatalystSample(paper_id=paper.id, name="Fe-N4", metal_centers=["Fe"]))
            session.add(DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.23))
            session.add(MechanismClaim(paper_id=paper.id, claim_type="shuttle_suppression", claim_text="claim"))
            session.add(
                WritingCard(
                    paper_id=paper.id,
                    paper_type="mixed",
                    figure_logic='[{"fig_id":"Figure_1","purpose":"summary","supports_claim":"claim"}]',
                )
            )
            session.commit()

            service = PaperQueryService(session)
            listing = service.list_papers()
            detail = service.get_paper_detail(paper.id)

            assert len(listing) == 1
            assert listing[0].counts.dft_settings == 1
            assert listing[0].counts.catalyst_samples == 1
            assert listing[0].counts.writing_cards == 1

            assert detail is not None
            assert len(detail.sections) == 1
            assert len(detail.tables) == 1
            assert len(detail.figures) == 1
            assert len(detail.dft_settings_items) == 1
            assert len(detail.catalyst_samples_items) == 1
            assert len(detail.dft_results_items) == 1
            assert len(detail.mechanism_claims_items) == 1
            assert len(detail.writing_cards_items) == 1
            assert isinstance(detail.writing_cards_items[0].figure_logic, list)

        engine.dispose()


def test_detail_payload_cleans_pdf_text_without_flattening_table_markdown():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Ligature Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()

            session.add(
                PaperTable(
                    paper_id=paper.id,
                    caption="Table /uniFB01ndings",
                    markdown_content="| field | value |\n| --- | --- |\n| con/uniFB01guration | e/uniFB00ect |",
                    page=2,
                    extraction_source="docling",
                )
            )
            session.add(
                PaperFigure(
                    paper_id=paper.id,
                    caption="Figure /uniFB02ow",
                    content_summary="A /uniFB02ow summary",
                    image_path=None,
                    page=3,
                    figure_role="summary",
                )
            )
            session.add(
                DFTResult(
                    paper_id=paper.id,
                    property_type="adsorption_energy",
                    value=-1.23,
                    source_section="con/uniFB01guration",
                    evidence_text="The con/uniFB01guration has a clear e/uniFB00ect.",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail is not None
            assert detail.tables[0].caption == "Table findings"
            assert "| --- | --- |" in detail.tables[0].markdown_content
            assert "\n" in detail.tables[0].markdown_content
            assert "configuration" in detail.tables[0].markdown_content
            assert detail.figures[0].caption == "Figure flow"
            assert detail.figures[0].content_summary == "A flow summary"
            assert detail.dft_results_items[0].source_section == "configuration"
            assert "configuration has a clear effect" in detail.dft_results_items[0].evidence_text

        engine.dispose()


def test_table_review_status_recognizes_legacy_codex_item_corrections():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Table Review Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            table = PaperTable(
                paper_id=paper.id,
                caption="Table 3",
                markdown_content="| raw |\n| --- |\n| Ef fi ciency |",
                page=9,
                extraction_source="docling",
            )
            session.add(table)
            session.flush()
            correction = PaperCorrection(
                paper_id=paper.id,
                source="ide_ai",
                field_name="markdown_content",
                target_path=f"codex_item:{table.id}",
                operation="replace",
                proposed_value="| raw |\n| --- |\n| Efficiency |",
                reason="IDE AI corrected table parser output.",
                status="pending",
            )
            session.add(correction)
            session.commit()

            pending_detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert pending_detail.tables[0].table_review_status == "pending_correction"

            correction.status = "approved"
            correction.reviewed_by = "ide_ai"
            session.add(correction)
            session.commit()

            approved_detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert approved_detail.tables[0].table_review_status == "verified"

        engine.dispose()


def test_table_review_status_treats_approved_style_object_audit_as_verified():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Table Positive Audit Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            table = PaperTable(
                paper_id=paper.id,
                caption="Table 1",
                markdown_content="| col |\n| --- |\n| value |",
                page=4,
                extraction_source="docling",
            )
            session.add(table)
            session.flush()
            run = ExternalAnalysisRun(
                paper_id=paper.id,
                source="ide_ai",
                source_label="ide_table_review",
            )
            session.add(run)
            session.flush()
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                status="ai_reviewed",
                normalized_payload={
                    "paper_id": str(paper.id),
                    "target_type": "tables",
                    "target_id": str(table.id),
                    "field_name": "table_review",
                    "decision": "approve",
                    "verification_status": "unverified",
                    "evidence_location": {"page": 4, "table": "Table 1", "quoted_text": "Table 1. caption"},
                },
                evidence_payload={"page": 4, "table": "Table 1", "quoted_text": "Table 1. caption"},
            )
            session.add(candidate)
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert detail is not None
            assert detail.tables[0].object_review_audit_count == 1
            assert detail.tables[0].table_review_status == "verified"

        engine.dispose()


def test_main_detail_uses_supplementary_table_owner_review_status():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            main_paper = Paper(title="B0095 Main", pdf_path="main.pdf", authors=["A"])
            si_paper = Paper(title="S0095 SI", pdf_path="si.pdf", authors=["A"])
            session.add_all([main_paper, si_paper])
            session.flush()
            session.add(
                PaperRelationship(
                    source_paper_id=main_paper.id,
                    target_paper_id=si_paper.id,
                    relationship_type="supplementary_information",
                    created_by="test",
                )
            )
            approved_table = PaperTable(
                paper_id=si_paper.id,
                caption="SI Table 1",
                markdown_content="| a |\n| --- |\n| checked |",
                page=2,
                extraction_source="docling",
            )
            untouched_table = PaperTable(
                paper_id=si_paper.id,
                caption="SI Table 2",
                markdown_content="| a |\n| --- |\n| raw |",
                page=3,
                extraction_source="docling",
            )
            session.add_all([approved_table, untouched_table])
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=si_paper.id,
                    source="ide_ai",
                    field_name="tables",
                    target_path=f"tables:{approved_table.id}:markdown_content",
                    operation="replace",
                    proposed_value="| a |\n| --- |\n| checked |",
                    reason="SI table markdown was checked.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(main_paper.id)

            assert detail is not None
            table_status = {str(item.id): item.table_review_status for item in detail.tables}
            assert table_status[str(approved_table.id)] == "verified"
            assert table_status[str(untouched_table.id)] == "unreviewed"

        engine.dispose()


def test_table_review_status_flags_reviewed_empty_markdown_content():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Empty Reviewed Table Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            table = PaperTable(
                paper_id=paper.id,
                caption=None,
                markdown_content=None,
                page=None,
                extraction_source="docling",
            )
            session.add(table)
            session.flush()
            run = ExternalAnalysisRun(
                paper_id=paper.id,
                source="ide_ai",
                source_label="ide_empty_table_review",
            )
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    status="ai_applied",
                    normalized_payload={
                        "paper_id": str(paper.id),
                        "target_type": "tables",
                        "target_id": str(table.id),
                        "field_name": "markdown_content",
                        "decision": "approve",
                        "verification_status": "unverified",
                        "evidence_location": {"page": 6, "table": "Table 1", "quoted_text": "Table 1 exists"},
                    },
                    evidence_payload={"page": 6, "table": "Table 1", "quoted_text": "Table 1 exists"},
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert detail is not None
            assert detail.tables[0].object_review_audit_count == 1
            assert detail.tables[0].table_review_status == "reviewed_empty_content"

        engine.dispose()


def test_figure_detail_exposes_pending_delete_proposal_count():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Figure Pending Delete Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            figure = PaperFigure(
                paper_id=paper.id,
                figure_label="fig_3",
                caption="Duplicate parser fragment",
                image_path="figures/dup.png",
                page=6,
                crop_status="needs_recrop",
            )
            session.add(figure)
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="literature_library_user",
                    field_name="figures",
                    target_path=f"figures:{figure.id}:delete",
                    operation="delete",
                    proposed_value=None,
                    reason="Duplicate fragment should be removed.",
                    status="pending",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert detail is not None
            assert detail.figures[0].pending_correction_count == 1
            assert detail.figures[0].pending_delete_proposal_count == 1
            assert detail.figures[0].pending_correction_fields == ["delete"]
            assert detail.figures[0].direct_delete_eligible is True
            assert detail.figures[0].direct_delete_reason is not None

        engine.dispose()


def test_figure_detail_hides_direct_delete_for_clean_figure():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Clean Figure Gate Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            session.add(
                PaperFigure(
                    paper_id=paper.id,
                    figure_label="fig_2",
                    caption="Figure 2. Full clean figure.",
                    image_path="figures/clean.png",
                    page=4,
                    crop_status="recropped",
                    figure_role="experimental_evidence",
                    content_summary="Full figure crop.",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert detail is not None
            assert detail.figures[0].direct_delete_eligible is False
            assert detail.figures[0].direct_delete_reason is None

        engine.dispose()


def test_figure_detail_marks_duplicate_figure_number_as_direct_delete_eligible():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Duplicate Figure Gate Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            session.add_all(
                [
                    PaperFigure(
                        paper_id=paper.id,
                        figure_label="Figure 7",
                        caption="Figure 7. Full panel crop.",
                        image_path="figures/fig7-full.png",
                        page=7,
                        crop_status="recropped",
                        figure_role="experimental_evidence",
                        content_summary="Full Figure 7 panel.",
                    ),
                    PaperFigure(
                        paper_id=paper.id,
                        figure_label="Figure 7",
                        caption="Figure 7. Fragment crop without duplicate keyword.",
                        image_path="figures/fig7-fragment.png",
                        page=7,
                        crop_status="candidate_crop",
                        figure_role="experimental_evidence",
                        content_summary=None,
                    ),
                ]
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)
            assert detail is not None
            assert len(detail.figures) == 2
            assert all(item.direct_delete_eligible is True for item in detail.figures)
            assert all(str(item.direct_delete_reason).startswith("duplicate_group_") for item in detail.figures)

        engine.dispose()


def test_detail_review_status_recognizes_legacy_ai_materialized_records():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Legacy AI Paper", pdf_path="paper.pdf", abstract="Abstract text", authors=["A"])
            session.add(paper)
            session.flush()
            section = PaperSection(
                paper_id=paper.id,
                section_title="Introduction",
                section_type="introduction",
                text="Legacy reviewed section text",
            )
            figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 1",
                image_path="figures/legacy.png",
                page=1,
                figure_role="plot",
                crop_status="candidate_crop",
            )
            session.add_all([section, figure])
            session.flush()

            run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="legacy_overall")
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="note",
                    normalized_payload={"field_name": "sections:semantic_structure", "content": "[AI_REVIEWED]"},
                    status="materialized",
                )
            )
            session.add(
                PaperNote(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name=f"figures:{figure.id}:figure_role",
                    content="[AI_REVIEWED] figure role checked",
                )
            )
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="sections",
                    target_path=f"sections:{section.id}:section_title",
                    operation="replace",
                    proposed_value="Abstract & Introduction",
                    reason="IDE AI approved section normalization.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail.sections_review_status == "ai_verified"
            assert detail.figures_review_status == "ai_verified"

        engine.dispose()


def test_detail_review_status_recognizes_approved_ide_ai_corrections():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Approved Correction Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            section = PaperSection(
                paper_id=paper.id,
                section_title="Introduction",
                section_type="introduction",
                text="Section text",
            )
            session.add(section)
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="sections",
                    target_path=f"sections:{section.id}:section_title",
                    operation="replace",
                    proposed_value="Introduction",
                    reason="IDE AI approved section correction.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail.sections_review_status == "ai_verified"

        engine.dispose()


def test_detail_excludes_page_and_deprecated_sections_from_display():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Section Filter Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            session.add_all(
                [
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Page 1",
                        section_type="body",
                        text="Whole-page parser dump",
                        page_start=1,
                        page_end=1,
                    ),
                    PaperSection(
                        paper_id=paper.id,
                        section_title="[DEPRECATED] Replaced by structured Results",
                        section_type="deprecated_stale",
                        text="Deprecated stale section",
                        page_start=2,
                        page_end=2,
                    ),
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Results and discussion",
                        section_type="results",
                        text="Clean structured section",
                        page_start=3,
                        page_end=4,
                    ),
                ]
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail is not None
            assert [section.section_title for section in detail.sections] == ["Results and discussion"]

        engine.dispose()


def test_detail_payload_exposes_figure_approved_correction_fields():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Figure Correction Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 1",
                image_path="figures/figure_1.png",
                page=1,
                figure_label="fig_1",
                crop_status="candidate_crop",
            )
            session.add(figure)
            session.flush()
            for field_name in ("figure_role", "content_summary", "key_elements"):
                session.add(
                    PaperCorrection(
                        paper_id=paper.id,
                        source="ide_ai",
                        field_name="figures",
                        target_path=f"figures:{figure.id}:{field_name}",
                        operation="replace",
                        proposed_value="reviewed",
                        reason=f"Approved {field_name}",
                        status="approved",
                        reviewed_by="antigravity",
                    )
                )
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="figures",
                    target_path=f"figures:{figure.id}:caption",
                    operation="replace",
                    proposed_value="pending caption",
                    reason="Pending changes should not be exposed as approved.",
                    status="pending",
                    reviewed_by="antigravity",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail is not None
            assert detail.figures[0].approved_correction_count == 3
            assert set(detail.figures[0].approved_correction_fields) == {
                "figure_role",
                "content_summary",
                "key_elements",
            }

        engine.dispose()


def test_detail_payload_normalizes_stringified_figure_key_elements():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Figure key elements detail", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            session.add(
                PaperFigure(
                    paper_id=paper.id,
                    caption="Figure 2",
                    image_path="figures/figure_2.png",
                    page=2,
                    figure_label="fig_2",
                    figure_role="characterization",
                    key_elements=[
                        "{'description': 'Panel (a): HAADF-STEM image with Pt dispersion'}",
                        "{'description': 'Panel (b): EXAFS fitting and shell assignment'}",
                    ],
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail is not None
            assert detail.figures[0].key_elements == [
                "Panel (a): HAADF-STEM image with Pt dispersion",
                "Panel (b): EXAFS fitting and shell assignment",
            ]

        engine.dispose()


def test_cached_pdf_size_uses_direct_storage_candidates(tmp_path):
    _cached_pdf_size_for_storage.cache_clear()
    storage_root = tmp_path / "storage"
    pdf_dir = storage_root / "pdf"
    pdf_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "paper.pdf"
    pdf_path.write_bytes(b"123456789")

    assert _cached_pdf_size_for_storage("storage/pdf/paper.pdf", str(storage_root)) == 9
    assert _cached_pdf_size_for_storage("paper.pdf", str(storage_root)) == 9


def test_list_papers_with_filters():
    """Verify year/journal/has_dft_results/reviewed_writing_cards/has_pdf/limit/offset filtering."""
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            # Paper A: 2024, Nature Catalysis, has DFT results + reviewed writing cards
            pa = Paper(title="Paper A", year=2024, journal="Nature Catalysis", pdf_path="a.pdf", authors=["A"])
            session.add(pa)
            session.flush()
            session.add(DFTResult(paper_id=pa.id, property_type="adsorption_energy", value=-1.0))
            session.add(WritingCard(paper_id=pa.id, paper_type="mixed"))
            session.add(
                PaperCorrection(
                    paper_id=pa.id,
                    source="ide_ai",
                    field_name="writing_cards",
                    target_path="writing_cards",
                    operation="replace",
                    proposed_value={"status": "reviewed"},
                    reason="IDE AI approved writing card.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )

            # Paper B: 2023, JACS, no DFT, no writing cards
            pb = Paper(title="Paper B", year=2023, journal="JACS", pdf_path="b.pdf", authors=["B"])
            session.add(pb)
            session.flush()

            # Paper C: 2024, Angewandte, has DFT only, but metadata-only so should count as "无 PDF"
            pc = Paper(
                title="Paper C",
                year=2024,
                journal="Angewandte Chemie",
                pdf_path="c.pdf",
                oa_status="metadata_only",
                authors=["C"],
            )
            session.add(pc)
            session.flush()
            session.add(DFTResult(paper_id=pc.id, property_type="barrier", value=0.8))

            # Paper D: has raw writing cards but no AI review, and empty pdf_path so it should still count as "无可用写作卡" + "无 PDF"
            pd = Paper(title="Paper D", year=2022, journal="Chem", pdf_path="", authors=["D"])
            session.add(pd)
            session.flush()
            session.add(WritingCard(paper_id=pd.id, paper_type="mixed", research_gap="Raw extracted card only"))

            session.commit()
            service = PaperQueryService(session)

            # No filter -> all 4
            assert len(service.list_papers()) == 4

            # Filter by year
            result = service.list_papers(PaperListFilterParams(year=2024))
            assert len(result) == 2
            assert all(p.year == 2024 for p in result)

            # Filter by journal (fuzzy)
            result = service.list_papers(PaperListFilterParams(journal="JACS"))
            assert len(result) == 1
            assert result[0].title == "Paper B"

            # Keyword search across title / journal
            result = service.list_papers(PaperListFilterParams(q="Angewandte"))
            assert len(result) == 1
            assert result[0].title == "Paper C"

            result = service.list_papers(PaperListFilterParams(q="Nature"))
            assert len(result) == 1
            assert result[0].title == "Paper A"

            # Filter has_dft_results=True
            result = service.list_papers(PaperListFilterParams(has_dft_results=True))
            assert len(result) == 2
            titles = {p.title for p in result}
            assert titles == {"Paper A", "Paper C"}

            # Filter has_writing_cards=True
            result = service.list_papers(PaperListFilterParams(has_writing_cards=True))
            assert len(result) == 1
            assert result[0].title == "Paper A"

            # Filter has_writing_cards=False
            result = service.list_papers(PaperListFilterParams(has_writing_cards=False))
            assert len(result) == 3
            titles = {p.title for p in result}
            assert titles == {"Paper B", "Paper C", "Paper D"}

            # Filter has_pdf=True
            result = service.list_papers(PaperListFilterParams(has_pdf=True))
            assert len(result) == 2
            titles = {p.title for p in result}
            assert titles == {"Paper A", "Paper B"}

            # Filter has_pdf=False
            result = service.list_papers(PaperListFilterParams(has_pdf=False))
            assert len(result) == 2
            titles = {p.title for p in result}
            assert titles == {"Paper C", "Paper D"}

            # Pagination: limit=1 offset=0
            result = service.list_papers(PaperListFilterParams(limit=1, offset=0))
            assert len(result) == 1
            # Pagination: limit=1 offset=1
            result = service.list_papers(PaperListFilterParams(limit=1, offset=1))
            assert len(result) == 1
            assert result[0].title != service.list_papers(PaperListFilterParams(limit=1, offset=0))[0].title

        engine.dispose()


def test_list_papers_defaults_to_newest_year_then_serial_order():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            p1 = Paper(title="Later serial", year=2019, serial_number=3, pdf_path="1.pdf", authors=["A"])
            p2 = Paper(title="Earlier year", year=2018, serial_number=9, pdf_path="2.pdf", authors=["B"])
            p3 = Paper(title="Earlier serial", year=2019, serial_number=1, pdf_path="3.pdf", authors=["C"])
            p4 = Paper(title="Missing year", year=None, serial_number=2, pdf_path="4.pdf", authors=["D"])
            session.add_all([p1, p2, p3, p4])
            session.commit()

            service = PaperQueryService(session)
            result = service.list_papers()

            assert [paper.title for paper in result] == [
                "Earlier serial",
                "Later serial",
                "Earlier year",
                "Missing year",
            ]

        engine.dispose()


def test_list_papers_supports_descending_year_serial_order():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            p1 = Paper(title="Year 2019 serial 1", year=2019, serial_number=1, pdf_path="1.pdf", authors=["A"])
            p2 = Paper(title="Year 2020 serial 2", year=2020, serial_number=2, pdf_path="2.pdf", authors=["B"])
            p3 = Paper(title="Year 2020 serial 1", year=2020, serial_number=1, pdf_path="3.pdf", authors=["C"])
            session.add_all([p1, p2, p3])
            session.commit()

            service = PaperQueryService(session)
            result = service.list_papers(PaperListFilterParams(sort_by="year_serial", sort_order="desc"))

            assert [paper.title for paper in result] == [
                "Year 2020 serial 1",
                "Year 2020 serial 2",
                "Year 2019 serial 1",
            ]

        engine.dispose()
