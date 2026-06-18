from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceSpan,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperSection,
    WritingCard,
)
from app.rag.backends import OpenAICompatibleWriterBackend, RuleWriterBackend
from app.rag.citation_guard import CitationGuard
from app.rag.prompt_builder import PaperWriterPromptBuilder
from app.rag.retriever import Retriever
from app.rag.writer import Writer


def test_retriever_writer_and_citation_guard_work_together():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'rag.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
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
                session.add(
                    WritingCard(
                        paper_id=paper.id,
                        paper_type="mixed",
                        research_gap="existing sulfur hosts still struggle to balance adsorption and conversion",
                        proposed_solution="Fe-N4 single-atom sites are introduced to regulate sulfur redox intermediates",
                        core_hypothesis="strong but not overly irreversible LiPS binding can improve bidirectional redox kinetics",
                        figure_logic='[{"fig_id":"Figure 1","purpose":"structure"},{"fig_id":"Figure 2","purpose":"DFT evidence"}]',
                        evidence_chain=[
                            {
                                "text": "The adsorption energy of Li2S4 on Fe-N4 was -1.23 eV.",
                                "source": "Results",
                                "reviewer_status": "verified",
                                "target_resolution_status": "active",
                            }
                        ],
                    )
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
                session.commit()

                retrieved = Retriever(session).retrieve("Fe-N4 Li2S4 adsorption conversion lithium sulfur", [paper.id], 3)
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

                draft = Writer(session, settings=Settings(writer_backend="rule")).write(
                    topic="Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    paper_ids=[paper.id],
                )
                assert draft["outline"]
                assert draft["backend_used"] == "rule"
                assert "instruction" in draft["prompt_preview"]
                assert "-1.23 eV" in draft["dft_results"]
                assert any(token in draft["discussion"] for token in ["900.0", "0.5C", "200 cycles"])
                assert draft["citation_guard"]["dft_results"]["ok"]
                assert draft["guard_actions"] == {}

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

                stub_draft = Writer(session, settings=Settings(writer_backend="llm_stub")).write(
                    topic="Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    paper_ids=[paper.id],
                )
                assert stub_draft["backend_used"] == "llm_stub"
                assert stub_draft["introduction"].startswith("[LLM-STUB REWRITE]")
                assert stub_draft["guard_actions"] == {}

                llm_draft = Writer(
                    session,
                    settings=Settings(
                        writer_backend="openai_compatible",
                        writer_api_base=None,
                        writer_api_key=None,
                    ),
                ).write(
                    topic="Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    paper_ids=[paper.id],
                )
                assert llm_draft["backend_used"] == "openai_compatible"
                assert llm_draft["llm_status"] == "disabled"
                assert llm_draft["llm_diagnostics"]["mode"] == "disabled"

                llm_status = Writer(
                    session,
                    settings=Settings(
                        writer_backend="openai_compatible",
                        writer_api_base=None,
                        writer_api_key=None,
                    ),
                ).status()
                assert llm_status["backend_used"] == "openai_compatible"
                assert llm_status["llm_status"] == "disabled"
                assert llm_status["llm_diagnostics"]["mode"] == "disabled"

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

                class CustomBackend:
                    name = "custom_llm"

                    def generate(self, prompt_payload, rule_sections, messages=None):
                        return {
                            "backend_used": self.name,
                            "prompt_preview": "custom",
                            "sections": {
                                **rule_sections,
                                "discussion": (
                                    "These data prove that Fe-N4 causes complete sulfur immobilization. "
                                    "The cell delivered 900 mAh/g at 1.0C after 200 cycles."
                                ),
                            },
                            "llm_status": "ok",
                        }

                partial_writer = Writer(session, settings=Settings(writer_backend="rule"))
                partial_writer.backend = CustomBackend()
                partial_draft = partial_writer.write(
                    topic="Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    paper_ids=[paper.id],
                    sections=["discussion"],
                )
                assert partial_draft["backend_used"] == "custom_llm"
                assert "1.0C" not in partial_draft["discussion"]
                assert "0.5C" in partial_draft["discussion"]
                assert "prove" not in partial_draft["discussion"].lower()
                assert "complete sulfur immobilization" not in partial_draft["discussion"]
                assert "Fe-N4 accelerates LiPS conversion" in partial_draft["discussion"]
                assert partial_draft["citation_guard"]["discussion"]["ok"]
                assert partial_draft["guard_actions"]["discussion"] == "reverted_all_sentences_with_rule_seed_due_to_unsupported_claims"
        finally:
            engine.dispose()


def test_openai_compatible_backend_diagnostics_and_parsing():
    backend = OpenAICompatibleWriterBackend(
        settings=Settings(
            writer_backend="openai_compatible",
            writer_api_base=None,
            writer_api_key=None,
        ),
        fallback=RuleWriterBackend(),
    )

    missing = backend.generate(
        prompt_payload={"instruction": "draft"},
        rule_sections={"discussion": "rule discussion"},
        messages=[{"role": "user", "content": "hello"}],
    )
    assert missing["llm_status"] == "disabled"
    assert "IDE/MCP AI" in (missing["llm_error"] or "")
    assert missing["llm_diagnostics"]["mode"] == "disabled"

    assert backend._build_chat_completions_url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert backend._build_chat_completions_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"

    parsed, parse_mode = backend._parse_sections(
        '```json\n{"discussion":"LLM discussion","outline":["a"]}\n```',
        {"discussion": "rule discussion", "outline": ["rule"]},
    )
    assert parsed["discussion"] == "LLM discussion"
    assert parsed["outline"] == ["a"]
    assert parse_mode == "json_code_fence"

    parsed_string_list, _ = backend._parse_sections(
        '{"outline":"1. Context\\n2. DFT evidence","figure_storyline":"Figure 1: structure; Figure 2: cycling"}',
        {"outline": ["rule"], "figure_storyline": ["rule fig"]},
    )
    assert parsed_string_list["outline"] == ["Context", "DFT evidence"]
    assert parsed_string_list["figure_storyline"] == ["Figure 1: structure", "Figure 2: cycling"]

    content = backend._extract_message_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": '{"discussion":"from list content"}'},
                        ]
                    }
                }
            ]
        }
    )
    assert "from list content" in content
