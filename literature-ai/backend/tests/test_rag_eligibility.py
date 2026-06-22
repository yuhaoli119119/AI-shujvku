import os
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    EvidenceSpan,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperChunk,
    PaperFigure,
    PaperSection,
    WritingCard,
)
from app.rag.eligibility import is_rag_eligible, writing_card_rag_review_status
from app.rag.quality import build_rag_quality_summary
from app.services.retrieval_service import RetrievalService


def test_rag_eligibility_recognizes_legacy_materialized_ai_records():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Legacy RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            raw_section = PaperSection(
                paper_id=paper.id,
                section_title="Page 1",
                section_type="body",
                text="Raw parser text",
            )
            reviewed_section = PaperSection(
                paper_id=paper.id,
                section_title="Results",
                section_type="results",
                text="AI-reviewed text",
                page_start=2,
                page_end=2,
            )
            session.add_all([raw_section, reviewed_section])
            session.flush()

            assert is_rag_eligible(session, raw_section, "section") is False

            run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="legacy_overall")
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="note",
                    normalized_payload={
                        "field_name": "sections",
                        "target_path": f"sections:{reviewed_section.id}:text",
                        "content": "[AI_REVIEWED]",
                    },
                    status="materialized",
                )
            )
            session.flush()

            assert is_rag_eligible(session, raw_section, "section") is False
            assert is_rag_eligible(session, reviewed_section, "section") is True

        engine.dispose()


def test_full_context_retrieval_requires_ai_reviewed_sections():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Full Context Gate Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            raw_section = PaperSection(
                paper_id=paper.id,
                section_title="Raw Results",
                section_type="results",
                text="Parser-only section text must stay out of retrieval.",
                page_start=2,
                page_end=2,
            )
            reviewed_section = PaperSection(
                paper_id=paper.id,
                section_title="Reviewed Results",
                section_type="results",
                text="AI-reviewed section text may enter full context retrieval.",
                page_start=3,
                page_end=3,
            )
            session.add_all([raw_section, reviewed_section])
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="sections",
                    target_path=f"sections:{reviewed_section.id}:text",
                    operation="replace",
                    proposed_value=reviewed_section.text,
                    reason="IDE AI approved reviewed section.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.flush()

            service = object.__new__(RetrievalService)
            service.session = session
            rows = service._full_context([paper.id], limit=20)

            assert [row.section_title for row in rows] == ["Reviewed Results"]
            assert "Parser-only" not in "\n".join(row.text for row in rows)

        engine.dispose()


def test_rag_eligibility_recognizes_approved_ide_ai_corrections():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Correction RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            section = PaperSection(
                paper_id=paper.id,
                section_title="Results",
                section_type="results",
                text="AI-corrected section text",
                page_start=3,
                page_end=3,
            )
            session.add(section)
            session.flush()

            assert is_rag_eligible(session, section, "section") is False

            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="sections",
                    target_path=f"sections:{section.id}:section_title",
                    operation="replace",
                    proposed_value="Results",
                    reason="IDE AI approved section correction.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.flush()

            assert is_rag_eligible(session, section, "section") is True

        engine.dispose()


def test_rag_eligibility_does_not_promote_chunks_from_paper_level_ai_records():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Chunk RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            section = PaperSection(
                paper_id=paper.id,
                section_title="Results",
                section_type="results",
                text="Raw section text",
                page_start=4,
                page_end=4,
            )
            session.add(section)
            session.flush()
            chunk = PaperChunk(
                paper_id=paper.id,
                section_id=section.id,
                chunk_index=0,
                text="Raw chunk text with band gap values.",
                page_start=4,
                page_end=4,
                content_hash="raw-chunk-hash",
            )
            session.add(chunk)
            session.flush()
            run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="paper_level")
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="note",
                    normalized_payload={"field_name": "sections", "content": "paper-level note"},
                    status="ai_reviewed",
                )
            )
            session.flush()

            assert is_rag_eligible(session, chunk, "chunk") is False

            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="sections",
                    target_path=f"sections:{section.id}:text",
                    operation="replace",
                    proposed_value="Reviewed section text",
                    reason="IDE AI approved this exact section.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.flush()

            assert is_rag_eligible(session, chunk, "chunk") is True

        engine.dispose()


def test_rag_eligibility_allows_only_classified_or_verified_figures():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Figure RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            raw_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 1. Raw crop.",
                image_path="figures/raw.png",
                page=2,
            )
            classified_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 2. DFT charge density.",
                image_path="figures/classified.png",
                page=3,
                figure_role="dft_evidence",
                content_summary="Charge density plot for DFT evidence.",
                key_elements=["charge density", "DFT"],
            )
            verified_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 3. Verified image.",
                image_path="figures/verified.png",
                page=4,
                figure_role="structure",
                content_summary="Verified structural figure.",
                key_elements=["structure"],
            )
            no_key_elements_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 3a. Reliable caption and summary.",
                image_path="figures/no-key-elements.png",
                page=4,
                figure_role="structure",
                content_summary="Reliable structural figure with no extracted key-elements list.",
            )
            placeholder_key_elements_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 3c. Reliable caption and summary.",
                image_path="figures/placeholder-key-elements.png",
                page=4,
                figure_role="structure",
                content_summary="Reliable structural figure with placeholder key elements.",
                key_elements=["verified_figure"],
            )
            caption_echo_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 3d. Optimized geometry and band structure for graphdiyne cathodes.",
                image_path="figures/caption-echo.png",
                page=4,
                figure_role="band_structure",
                content_summary="Figure 3d. Optimized geometry and band structure for graphdiyne cathodes.",
                key_elements=["band structure", "graphdiyne"],
            )
            reviewed_caption_echo_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 3e. Stress-regulated band structure and transport trends for graphdiyne nanoribbons.",
                image_path="figures/reviewed-caption-echo.png",
                page=4,
                figure_role="band_structure",
                content_summary="Figure 3e. Stress-regulated band structure and transport trends for graphdiyne nanoribbons.",
                key_elements=["band structure", "transport trend", "graphdiyne nanoribbon"],
            )
            missing_summary_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 3b. Missing summary.",
                image_path="figures/missing-summary.png",
                page=4,
                figure_role="structure",
                key_elements=["structure"],
            )
            missing_image = PaperFigure(
                paper_id=paper.id,
                caption="Figure 4. Caption only.",
                page=5,
                figure_role="mechanism",
            )
            noisy_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 5. Noisy parser artifact.",
                image_path="figures/noisy.png",
                page=6,
                figure_role="noise",
                content_summary="Parser noise.",
            )
            unlocated_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 6. Caption without page.",
                image_path="figures/unlocated.png",
                figure_role="dft_evidence",
                content_summary="DFT figure without a PDF page.",
            )
            recrop_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 7. Needs recrop.",
                image_path="figures/recrop.png",
                page=7,
                figure_role="dft_evidence",
                content_summary="Risky crop.",
                crop_status="needs_recrop",
            )
            full_page_recrop_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 8. Full-page fallback.",
                image_path="figures/full-page.png",
                page=8,
                figure_role="dft_evidence",
                content_summary="Band structure for the candidate material.",
                key_elements=["band structure", "candidate material"],
                crop_status="recropped",
                crop_source="recrop:full_page:ide_ai",
            )
            precise_recrop_with_stale_status = PaperFigure(
                paper_id=paper.id,
                caption=(
                    "Figure 9. Binding energy per atom for AA, AB, and ABC stacking sequences, "
                    "plus cohesive energy per atom for graphite, diamond, alpha-GDY, and HsGDY."
                ),
                image_path="figures/precise.png",
                page=9,
                figure_role="property_data",
                content_summary="Binding and cohesive energies for HsGDY and other carbon allotropes",
                key_elements=["binding energy", "cohesive energy", "AA stacking"],
                crop_status="needs_recrop",
                crop_source="legacy_image",
                prov=[
                    {"action": "recrop_figure", "strategy": "full_page"},
                    {"action": "recrop_figure", "strategy": "ai_bbox"},
                ],
            )
            detailed_two_panel_summary = PaperFigure(
                paper_id=paper.id,
                caption="Figure 10. Upper panel: H2-C pair potential (meV) vs intermolecular distance (A).",
                image_path="figures/two-panel.png",
                page=10,
                figure_role="property_data",
                content_summary=(
                    "Two-panel potential energy diagram with upper-panel H2-C pair potential, lower-panel "
                    "interaction energy vs z position, multiple ILJ and ILJFH curves, and numeric ranges."
                ),
                key_elements=["pair potential", "interaction energy", "ILJ", "ILJFH"],
                crop_status="candidate_crop",
                crop_source="image_block_near_caption",
            )
            session.add_all([raw_figure, classified_figure, verified_figure, no_key_elements_figure, placeholder_key_elements_figure, caption_echo_figure, reviewed_caption_echo_figure, missing_summary_figure, missing_image, noisy_figure, unlocated_figure, recrop_figure, full_page_recrop_figure, precise_recrop_with_stale_status, detailed_two_panel_summary])
            session.flush()
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="figures",
                    target_id=str(verified_figure.id),
                    field_name="caption",
                    reviewer_status="verified",
                    target_resolution_status="active",
                    evidence_text="Verified against the PDF figure.",
                )
            )
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="figures",
                    target_id=str(reviewed_caption_echo_figure.id),
                    field_name="content_summary",
                    reviewer_status="verified",
                    target_resolution_status="active",
                    evidence_text="Verified figure summary against the PDF figure.",
                )
            )
            session.flush()

            assert is_rag_eligible(session, raw_figure, "figure") is False
            assert is_rag_eligible(session, classified_figure, "figure") is True
            assert is_rag_eligible(session, verified_figure, "figure") is True
            assert is_rag_eligible(session, no_key_elements_figure, "figure") is False
            assert is_rag_eligible(session, placeholder_key_elements_figure, "figure") is False
            assert is_rag_eligible(session, caption_echo_figure, "figure") is False
            assert is_rag_eligible(session, reviewed_caption_echo_figure, "figure") is False
            assert is_rag_eligible(session, missing_summary_figure, "figure") is False
            assert is_rag_eligible(session, missing_image, "figure") is False
            assert is_rag_eligible(session, noisy_figure, "figure") is False
            assert is_rag_eligible(session, unlocated_figure, "figure") is False
            assert is_rag_eligible(session, recrop_figure, "figure") is False
            assert is_rag_eligible(session, full_page_recrop_figure, "figure") is False
            assert is_rag_eligible(session, precise_recrop_with_stale_status, "figure") is True
            assert is_rag_eligible(session, detailed_two_panel_summary, "figure") is True

        engine.dispose()


def test_rag_quality_summary_blocks_reviewed_caption_echo_figures():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Reviewed Caption Echo Figure", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            blocked_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 1. Band structure and density-of-states comparison for graphdiyne nanoribbons.",
                image_path="figures/blocked.png",
                page=2,
                figure_role="band_structure",
                content_summary="Figure 1. Band structure and density-of-states comparison for graphdiyne nanoribbons.",
                key_elements=["band structure", "density of states", "graphdiyne"],
            )
            reviewed_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 2. Strain-regulated transport coefficients for graphdiyne nanoribbons.",
                image_path="figures/reviewed.png",
                page=3,
                figure_role="property_data",
                content_summary="Figure 2. Strain-regulated transport coefficients for graphdiyne nanoribbons.",
                key_elements=["transport coefficient", "strain", "graphdiyne nanoribbon"],
            )
            session.add_all([blocked_figure, reviewed_figure])
            session.flush()
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="figures",
                    target_id=str(reviewed_figure.id),
                    field_name="content_summary",
                    reviewer_status="verified",
                    target_resolution_status="active",
                    evidence_text="Verified against the PDF figure.",
                )
            )
            session.commit()

            summary = build_rag_quality_summary(
                session,
                figures=[blocked_figure, reviewed_figure],
                dft_results=[],
                writing_cards=[],
            )

            assert summary["figures"]["eligible"] == 0
            assert summary["figures"]["blocked"] == 2
            assert summary["figures"]["blocked_reasons"]["caption_echo_summary"] == 2

        engine.dispose()


def test_dft_rag_eligibility_requires_identity_value_unit_and_locator():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="DFT RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            catalyst = CatalystSample(
                paper_id=paper.id,
                name="Fe-N4",
                catalyst_type="single_atom",
            )
            session.add(catalyst)
            session.flush()
            eligible = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=catalyst.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                evidence_text="Li2S4 adsorption energy is -1.23 eV.",
            )
            missing_unit = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=catalyst.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                evidence_text="Li2S4 adsorption energy is -1.23.",
            )
            missing_identity = DFTResult(
                paper_id=paper.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                evidence_text="Li2S4 adsorption energy is -1.23 eV.",
            )
            session.add_all([eligible, missing_unit, missing_identity])
            session.flush()
            for row in [eligible, missing_unit, missing_identity]:
                session.add(
                    EvidenceSpan(
                        paper_id=paper.id,
                        object_type="dft_results",
                        object_id=str(row.id),
                        text=row.evidence_text or "Evidence",
                        page=6,
                    )
                )
                session.add(
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="dft_results",
                        target_id=str(row.id),
                        field_name="value",
                        reviewer_status="verified",
                        target_resolution_status="active",
                        evidence_text=row.evidence_text,
                    )
                )
            session.flush()

            assert is_rag_eligible(session, eligible, "dft_result") is True
            assert is_rag_eligible(session, missing_unit, "dft_result") is False
            assert is_rag_eligible(session, missing_identity, "dft_result") is False

        engine.dispose()


def test_writing_card_rag_eligibility_recognizes_approved_ide_ai_review():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Writing Card RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            raw_card = WritingCard(
                paper_id=paper.id,
                paper_type="A",
                research_gap="Raw writing card should not enter formal RAG.",
                evidence_chain={"figures": [1, 2]},
            )
            reviewed_card = WritingCard(
                paper_id=paper.id,
                paper_type="A",
                research_gap="IDE AI reviewed writing card should enter formal RAG.",
                evidence_chain={"figures": [1, 2]},
            )
            session.add_all([raw_card, reviewed_card])
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="writing_cards",
                    target_path=f"writing_cards:{reviewed_card.id}:research_gap",
                    operation="replace",
                    proposed_value="IDE AI reviewed writing card should enter formal RAG.",
                    reason="IDE AI approved writing card.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.flush()

            assert is_rag_eligible(session, raw_card, "writing_card") is False
            assert is_rag_eligible(session, reviewed_card, "writing_card") is False
            assert writing_card_rag_review_status(session, reviewed_card) == "blocked"

        engine.dispose()


def test_writing_card_rag_eligibility_recognizes_approved_ide_ai_create():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Created Writing Card RAG Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            created_card = WritingCard(
                paper_id=paper.id,
                paper_type="A",
                research_gap="AI-created writing card should enter formal RAG.",
                proposed_solution="Use approved create correction content.",
                evidence_chain={"pages": [1, 2]},
            )
            unrelated_card = WritingCard(
                paper_id=paper.id,
                paper_type="A",
                research_gap="Unrelated raw writing card should stay out.",
                proposed_solution="Different content.",
                evidence_chain={"pages": [1, 2]},
            )
            session.add_all([created_card, unrelated_card])
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="writing_cards",
                    target_path="writing_cards:new:create",
                    operation="create",
                    proposed_value={
                        "paper_type": "A",
                        "research_gap": "AI-created writing card should enter formal RAG.",
                        "proposed_solution": "Use approved create correction content.",
                    },
                    reason="IDE AI created writing card.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.flush()

            assert is_rag_eligible(session, created_card, "writing_card") is False
            assert writing_card_rag_review_status(session, created_card) == "blocked"
            assert is_rag_eligible(session, unrelated_card, "writing_card") is False

        engine.dispose()


def test_rag_quality_summary_counts_eligible_and_blocked_reasons():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="RAG Quality Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            eligible_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 1. Classified structure.",
                image_path="figures/1.png",
                page=1,
                figure_role="structure",
                content_summary="Classified structure figure.",
                key_elements=["structure"],
            )
            blocked_figure = PaperFigure(
                paper_id=paper.id,
                caption="Figure 2. Missing image.",
                page=2,
                figure_role="structure",
            )
            blocked_dft = DFTResult(
                paper_id=paper.id,
                property_type="band_gap",
                value=1.2,
                evidence_text="Band gap is 1.2.",
            )
            eligible_card = WritingCard(
                paper_id=paper.id,
                paper_type="A",
                research_gap="A conversion limitation remains unresolved in current catalysts.",
                proposed_solution="This work develops atomically dispersed sites for conversion.",
                evidence_chain=[
                    {"text": "A conversion limitation remains unresolved in current catalysts.", "source": "Introduction", "page": 1, "locator_status": "exact_page", "supports_fields": ["research_gap"]},
                    {"text": "This work develops atomically dispersed sites for conversion.", "source": "Introduction", "page": 1, "locator_status": "exact_page", "supports_fields": ["proposed_solution"]},
                ],
            )
            session.add_all([eligible_figure, blocked_figure, blocked_dft, eligible_card])
            session.flush()
            session.add(
                PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="writing_cards",
                    target_path="writing_cards:new:create",
                    operation="create",
                    proposed_value={"paper_type": "A", "research_gap": "A conversion limitation remains unresolved in current catalysts."},
                    reason="IDE AI created writing card.",
                    status="approved",
                    reviewed_by="ide_ai",
                )
            )
            session.flush()

            summary = build_rag_quality_summary(
                session,
                figures=[eligible_figure, blocked_figure],
                dft_results=[blocked_dft],
                writing_cards=[eligible_card],
            )

            assert summary["figures"]["eligible"] == 1
            assert summary["figures"]["blocked_reasons"]["missing_image"] == 1
            assert summary["dft_results"]["eligible"] == 0
            assert summary["dft_results"]["blocked_reasons"]["missing_unit"] == 1
            assert summary["writing_cards"]["eligible"] == 1
            assert summary["eligible_total"] == 2

        engine.dispose()
