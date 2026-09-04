import os
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceLocator,
    EvidenceSpan,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperSection,
    WritingCard,
)
from app.rag.citation_guard import CitationGuard
from app.rag.prompt_builder import PaperWriterPromptBuilder
from app.rag.retriever import Retriever


def test_retriever_prompt_and_citation_guard_work_together():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)

            with Session(engine) as session:
                paper = Paper(title="RAG Paper", pdf_path="rag.pdf", authors=[])
                session.add(paper)
                session.flush()
                catalyst_sample = CatalystSample(
                    paper_id=paper.id,
                    name="Fe-N4 catalyst",
                    catalyst_type="single_atom",
                    metal_centers=["Fe"],
                    support="N-doped carbon",
                    evidence_strength="Fe-N4 catalyst was supported on N-doped carbon.",
                )
                session.add(catalyst_sample)
                session.flush()

                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Introduction",
                        section_type="introduction",
                        text="Sluggish LiPS conversion remains a challenge in lithium-sulfur batteries with single-atom catalysts.",
                        page_start=1,
                        page_end=1,
                    )
                )
                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Discussion",
                        section_type="discussion",
                        text="These data indicate that stronger Li2S4 binding accelerates LiPS conversion and improves cycling stability.",
                        page_start=5,
                        page_end=5,
                    )
                )
                dft_result = DFTResult(
                    paper_id=paper.id,
                    catalyst_sample_id=catalyst_sample.id,
                    adsorbate="Li2S4",
                    property_type="adsorption_energy",
                    value=-1.23,
                    unit="eV",
                    evidence_text="The adsorption energy of Li2S4 on Fe-N4 was -1.23 eV.",
                )
                electrochemical = ElectrochemicalPerformance(
                    paper_id=paper.id,
                    sulfur_loading_mg_cm2=4.2,
                    capacity_value=900.0,
                    rate="0.5C",
                    cycle_number=200,
                    evidence_text="The cell delivered 900 mAh/g at 0.5C after 200 cycles.",
                )
                mechanism = MechanismClaim(
                    paper_id=paper.id,
                    claim_type="lips_conversion",
                    claim_text="Fe-N4 accelerates LiPS conversion by strengthening intermediate binding.",
                    evidence_types=["Li2S4", "DOS"],
                    evidence_text="These data indicate that stronger Li2S4 binding accelerates LiPS conversion.",
                )
                session.add_all([dft_result, electrochemical, mechanism])
                session.flush()
                card_evidence_texts = (
                    "Existing sulfur hosts still struggle to balance adsorption and conversion.",
                    "Fe-N4 single-atom sites are introduced to regulate sulfur redox intermediates.",
                    "Strong but not overly irreversible LiPS binding can improve bidirectional redox kinetics.",
                )
                card_sources = [
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Introduction",
                        section_type="introduction",
                        text=text,
                        page_start=1,
                        page_end=1,
                    )
                    for text in card_evidence_texts
                ]
                session.add_all(card_sources)
                session.flush()
                writing_card = WritingCard(
                    paper_id=paper.id,
                    paper_type="mixed",
                    research_gap="existing sulfur hosts still struggle to balance adsorption and conversion",
                    proposed_solution="Fe-N4 single-atom sites are introduced to regulate sulfur redox intermediates",
                    core_hypothesis="strong but not overly irreversible LiPS binding can improve bidirectional redox kinetics",
                    figure_logic='[{"fig_id":"Figure 1","purpose":"structure"},{"fig_id":"Figure 2","purpose":"DFT evidence"}]',
                    evidence_chain=[
                        {
                            "text": card_evidence_texts[0],
                            "source": "Introduction", "page": 1, "locator_status": "exact_page",
                            "supports_fields": ["research_gap"],
                            "source_target_type": "sections",
                            "source_target_id": str(card_sources[0].id),
                        },
                            {
                                "text": card_evidence_texts[1],
                                "source": "Introduction", "page": 1, "locator_status": "exact_page",
                                "supports_fields": ["proposed_solution"],
                                "source_target_type": "sections",
                                "source_target_id": str(card_sources[1].id),
                            },
                            {
                                "text": card_evidence_texts[2],
                                "source": "Introduction", "page": 1, "locator_status": "exact_page",
                                "supports_fields": ["core_hypothesis"],
                                "source_target_type": "sections",
                                "source_target_id": str(card_sources[2].id),
                            },
                    ],
                )
                session.add(writing_card)
                session.flush()
                session.add_all(
                    [
                        ExtractionFieldReview(
                            paper_id=paper.id,
                            target_type="writing_cards",
                            target_id=str(writing_card.id),
                            field_name="evidence_chain",
                            reviewed_value=writing_card.evidence_chain,
                            reviewer_status="verified",
                            target_resolution_status="active",
                            evidence_text=card_evidence_texts[0],
                        ),
                        EvidenceLocator(
                            paper_id=paper.id,
                            source_type="pdf",
                            page=1,
                            target_type="writing_cards",
                            target_id=str(writing_card.id),
                            field_name="evidence_chain",
                            evidence_text=card_evidence_texts[0],
                            locator_status="exact_page",
                            locator_confidence=1.0,
                            parser_source="test_review",
                        ),
                    ]
                )
                for source, evidence_text in zip(card_sources, card_evidence_texts):
                    session.add_all(
                        [
                            ExtractionFieldReview(
                                paper_id=paper.id,
                                target_type="sections",
                                target_id=str(source.id),
                                field_name="text",
                                reviewer_status="verified",
                                target_resolution_status="active",
                                evidence_text=evidence_text,
                            ),
                            EvidenceLocator(
                                paper_id=paper.id,
                                source_type="pdf",
                                page=1,
                                target_type="sections",
                                target_id=str(source.id),
                                field_name="text",
                                evidence_text=evidence_text,
                                locator_status="exact_page",
                                locator_confidence=1.0,
                                parser_source="test_review",
                            ),
                        ]
                    )
                for field_name in ("research_gap", "proposed_solution", "core_hypothesis"):
                    evidence_text = str(getattr(writing_card, field_name))
                    session.add_all(
                        [
                            ExtractionFieldReview(
                                paper_id=paper.id,
                                target_type="writing_cards",
                                target_id=str(writing_card.id),
                                field_name=field_name,
                                reviewed_value=evidence_text,
                                reviewer_status="verified",
                                target_resolution_status="active",
                                evidence_text=evidence_text,
                            ),
                            EvidenceLocator(
                                paper_id=paper.id,
                                source_type="pdf",
                                page=1,
                                target_type="writing_cards",
                                target_id=str(writing_card.id),
                                field_name=field_name,
                                evidence_text=evidence_text,
                                locator_status="exact_page",
                                locator_confidence=1.0,
                                parser_source="test_review",
                            ),
                        ]
                    )
                for target_type, row, field_name, evidence_text in [
                    ("catalyst_samples", catalyst_sample, "name", catalyst_sample.evidence_strength),
                    ("dft_results", dft_result, "value", dft_result.evidence_text),
                    ("electrochemical_performance", electrochemical, "capacity", electrochemical.evidence_text),
                    ("mechanism_claims", mechanism, "claim_text", mechanism.evidence_text),
                ]:
                    session.add(
                        EvidenceSpan(
                            paper_id=paper.id,
                            object_type=target_type,
                            object_id=str(row.id),
                            text=evidence_text,
                            page=1,
                        )
                    )
                    session.add(
                        ExtractionFieldReview(
                            paper_id=paper.id,
                            target_type=target_type,
                            target_id=str(row.id),
                            field_name=field_name,
                            reviewer_status="verified",
                            target_resolution_status="active",
                            evidence_text=evidence_text,
                        )
                    )
                    session.add(
                        EvidenceLocator(
                            paper_id=paper.id,
                            source_type="pdf",
                            page=1,
                            target_type=target_type,
                            target_id=str(row.id),
                            field_name=field_name,
                            evidence_text=evidence_text,
                            locator_status="exact_page",
                            locator_confidence=1.0,
                            parser_source="test_review",
                        )
                    )
                session.commit()

                retrieved = Retriever(session).retrieve(
                    "Fe-N4 Li2S4 adsorption conversion lithium sulfur",
                    [paper.id],
                    3,
                    mode="comprehensive",
                )
                assert retrieved["catalyst_samples"]
                assert retrieved["dft_results"]
                assert retrieved["electrochemical_performance"]
                assert retrieved["mechanism_claims"]
                assert retrieved["writing_cards"]
                assert "score_breakdown" in retrieved["dft_results"][0]
                assert "semantic" in retrieved["dft_results"][0]["score_breakdown"]
                for evidence_type in ["catalyst_samples", "electrochemical_performance", "mechanism_claims"]:
                    item = retrieved[evidence_type][0]
                    assert item["source_type"]
                    assert item["source_id"]
                    assert item["review_status"] == "verified"
                    assert item["page"] == 1
                    assert item["evidence_locator"]["locator_status"] == "exact_page"

                prompt_payload = PaperWriterPromptBuilder().build(
                    topic="Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    user_notes=None,
                    requested_sections=["introduction", "dft_results", "discussion"],
                    retrieved=retrieved,
                )
                assert prompt_payload["evidence_pack"]["dft_results"]
                assert any(item["source_type"] == "catalyst_samples" for item in prompt_payload["evidence_pack"]["introduction"])
                assert all("source_id" in item for item in prompt_payload["evidence_pack"]["introduction"])
                assert prompt_payload["numeric_guardrails"]
                assert all("summary" in item for item in prompt_payload["evidence_pack"]["dft_results"])
                assert all("numeric_values" in item for item in prompt_payload["evidence_pack"]["discussion"])

                guard = CitationGuard()
                verdict = guard.validate("The adsorption energy is -9.99 eV.", retrieved)
                assert not verdict["ok"]
                assert verdict["missing_values"]

                mismatched_context = guard.validate(
                    "The cell delivered 900 mAh/g at 1.0C after 200 cycles.",
                    retrieved,
                )
                assert not mismatched_context["ok"]
                assert any(item["literal"] == "1.0C" for item in mismatched_context["missing_values"])

                supported_fact = guard.validate(
                    "Fe-N4 accelerates LiPS conversion by strengthening intermediate binding.",
                    retrieved,
                )
                assert supported_fact["ok"]
                assert supported_fact["checked_fact_count"] >= 1

                unsupported_fact = guard.validate(
                    "Fe-N4 suppresses LiPS conversion and is the best catalyst in this evidence set.",
                    retrieved,
                )
                assert not unsupported_fact["ok"]
                assert unsupported_fact["missing_fact_claims"]
                assert any("suppresses" in item["triggers"] for item in unsupported_fact["missing_fact_claims"])

                supported_causal = guard.validate(
                    "These data indicate that stronger Li2S4 binding accelerates LiPS conversion.",
                    retrieved,
                )
                assert supported_causal["ok"]
                assert any("evidences" in item["claim"]["triggers"] for item in supported_causal["supported_fact_claims"])

                unsupported_causal = guard.validate(
                    "These data prove that Fe-N4 causes complete sulfur immobilization.",
                    retrieved,
                )
                assert not unsupported_causal["ok"]
                assert unsupported_causal["missing_fact_claims"]
                assert any("causes" in item["triggers"] for item in unsupported_causal["missing_fact_claims"])

                unsupported_barrier = guard.validate(
                    "These data establish that stronger Li2S4 binding lowers the reaction barrier for LiPS conversion.",
                    retrieved,
                )
                assert not unsupported_barrier["ok"]
                assert unsupported_barrier["missing_fact_claims"]
                assert any("barrier" in item["context"] for item in unsupported_barrier["missing_fact_claims"])
                assert any("weakens" in item["triggers"] for item in unsupported_barrier["missing_fact_claims"])

        finally:
            engine.dispose()
