from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import Settings
from app.rag.backends import resolve_writer_backend
from app.rag.citation_guard import CitationGuard
from app.rag.prompt_builder import PaperWriterPromptBuilder
from app.rag.retriever import Retriever


class Writer:
    """Evidence-grounded writer with pluggable generation backends."""

    SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")
    TOKEN_PATTERN = re.compile(r"[a-z0-9_+-]+", re.IGNORECASE)

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or Settings()
        self.retriever = Retriever(session)
        self.citation_guard = CitationGuard()
        prompt_path = self._resolve_prompt_path(self.settings.writer_prompt_path)
        self.prompt_builder = PaperWriterPromptBuilder(prompt_path=prompt_path)
        self.backend = resolve_writer_backend(self.settings.writer_backend, self.settings)

    def write(
        self,
        topic: str,
        paper_ids: list[UUID] | None = None,
        user_notes: str | None = None,
        sections: list[str] | None = None,
        limit_per_type: int = 5,
        target_paper_type: str | None = None,
    ) -> dict[str, Any]:
        requested = sections or ["outline", "introduction", "dft_results", "discussion", "figure_storyline"]
        query = " ".join(part for part in [topic, user_notes or ""] if part)
        
        paper_type_filter = [target_paper_type[0]] if target_paper_type else None
        
        retrieved = self.retriever.retrieve(
            query=query, 
            paper_ids=paper_ids, 
            limit_per_type=limit_per_type,
            target_paper_type=target_paper_type,
            paper_type_filter=paper_type_filter
        )

        rule_sections = self._build_rule_sections(topic, retrieved, requested, target_paper_type)
        prompt_payload = self.prompt_builder.build(topic, user_notes, requested, retrieved, target_paper_type)
        messages = self.prompt_builder.render_messages(prompt_payload, rule_sections)
        generated = self.backend.generate(prompt_payload, rule_sections, messages)
        content = generated.get("sections", {})
        content, guard_actions = self._enforce_guardrails(content, rule_sections, retrieved)

        validations = {
            "introduction": self.citation_guard.validate(content.get("introduction", ""), retrieved) if content.get("introduction") else {"ok": True, "missing_values": []},
            "dft_results": self.citation_guard.validate(content.get("dft_results", ""), retrieved) if content.get("dft_results") else {"ok": True, "missing_values": []},
            "discussion": self.citation_guard.validate(content.get("discussion", ""), retrieved) if content.get("discussion") else {"ok": True, "missing_values": []},
        }

        return {
            "topic": topic,
            "query": query,
            "backend_used": generated.get("backend_used", self.backend.name),
            "prompt_preview": generated.get("prompt_preview", ""),
            "llm_status": generated.get("llm_status"),
            "llm_error": generated.get("llm_error"),
            "llm_diagnostics": generated.get("llm_diagnostics", {}),
            "outline": content.get("outline", []),
            "introduction": content.get("introduction", ""),
            "dft_results": content.get("dft_results", ""),
            "discussion": content.get("discussion", ""),
            "figure_storyline": content.get("figure_storyline", []),
            "retrieved": retrieved,
            "citation_guard": validations,
            "guard_actions": guard_actions,
        }

    def status(self) -> dict[str, Any]:
        if hasattr(self.backend, "status"):
            diagnostics = self.backend.status()
        else:
            diagnostics = {
                "mode": "unknown",
                "requested_backend": getattr(self.backend, "name", "unknown"),
                "final_backend": getattr(self.backend, "name", "unknown"),
                "ready": False,
            }
        return {
            "backend_used": getattr(self.backend, "name", "unknown"),
            "llm_status": "ready" if diagnostics.get("ready") else diagnostics.get("mode"),
            "llm_error": None if diagnostics.get("ready") else (
                f"Missing required configuration: {', '.join(diagnostics.get('missing_configuration', []))}"
                if diagnostics.get("missing_configuration")
                else None
            ),
            "llm_diagnostics": diagnostics,
        }

    def _build_rule_sections(
        self,
        topic: str,
        retrieved: dict[str, list[dict[str, Any]]],
        requested: list[str],
        target_paper_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "outline": self._build_outline(topic, retrieved, target_paper_type) if "outline" in requested else [],
            "introduction": self._build_introduction(topic, retrieved, target_paper_type) if "introduction" in requested else "",
            "dft_results": self._build_dft_results(topic, retrieved, target_paper_type) if "dft_results" in requested else "",
            "discussion": self._build_discussion(topic, retrieved, target_paper_type) if "discussion" in requested else "",
            "figure_storyline": self._build_figure_storyline(retrieved) if "figure_storyline" in requested else [],
        }

    def _build_outline(self, topic: str, retrieved: dict[str, list[dict[str, Any]]], target_paper_type: str | None) -> list[str]:
        if target_paper_type and target_paper_type.startswith("R"):
            return [
                f"Historical context and significance of {topic}",
                "Review of fundamental mechanisms and key challenges",
                "Summary of recent breakthroughs across computational and experimental studies",
                "Discussion on limitations of current methodologies",
                "Future perspectives and research directions",
            ]

        if target_paper_type and target_paper_type.startswith("A"):
            return [
                f"Background and motivation for computational study of {topic}",
                "Computational methodology \u2014 DFT functional, basis set, model construction",
                f"Structural optimization and stability analysis for {topic}",
                "Electronic structure analysis (DOS, charge density, Bader charge)",
                "Reaction pathway and energy barriers (NEB / transition state)",
                "Conclusions and implications for catalyst design",
            ]

        if target_paper_type and target_paper_type.startswith("C"):
            return [
                f"Background and experimental design rationale for {topic}",
                "Materials and synthesis methods",
                "Structural and morphological characterization (XRD, SEM, TEM, XPS)",
                "Electrochemical performance evaluation (CV, LSV, Tafel, EIS)",
                "Mechanistic analysis linking structure to performance",
                "Conclusions and outlook",
            ]
        
        outline = [
            f"Context and challenge in {topic}",
            "Catalyst design rationale and research gap",
            "Key DFT evidence for adsorption, conversion, or barrier tuning",
            "Mechanistic interpretation linked to sulfur redox behavior",
            "Implications for electrochemical performance and figure sequence",
        ]
        if retrieved.get("electrochemical_performance"):
            outline.append("Electrochemical validation and structure-performance correlation")
        return outline

    def _build_introduction(self, topic: str, retrieved: dict[str, list[dict[str, Any]]], target_paper_type: str | None) -> str:
        cards = retrieved.get("writing_cards", [])
        mechanisms = retrieved.get("mechanism_claims", [])
        gap = cards[0].get("research_gap") if cards else "prior studies still lack a stable way to balance polysulfide adsorption and fast conversion"
        solution = cards[0].get("proposed_solution") if cards else "single-atom or dual-atom catalytic centers are introduced to tune sulfur redox chemistry"
        mechanism = mechanisms[0].get("text") if mechanisms else "the catalytic site is expected to strengthen sulfur-species binding while accelerating bidirectional conversion"
        
        if target_paper_type and target_paper_type.startswith("R"):
            return (
                f"Lithium-sulfur batteries represent a promising next-generation energy storage system, yet {gap}. "
                f"This review comprehensively summarizes the recent progress in {topic}, focusing on how {solution}. "
                f"We particularly highlight the mechanistic consensus that {mechanism}. "
                "By bridging computational insights with experimental benchmarks, this review aims to provide a clear roadmap for future material design."
            )

        if target_paper_type and target_paper_type.startswith("A"):
            return (
                f"Understanding the electronic structure and reaction energetics of {topic} requires systematic first-principles investigation. "
                f"Prior computational studies face the challenge that {gap}. "
                f"In this work, {solution} is examined using density functional theory to reveal how {mechanism}. "
                "The computational framework established here provides atomic-level insights that guide rational catalyst design."
            )

        if target_paper_type and target_paper_type.startswith("C"):
            return (
                f"Developing high-performance catalysts for {topic} demands careful experimental validation of structure-activity relationships. "
                f"Previous experimental efforts are limited by {gap}. "
                f"To address this, {solution} was synthesized and systematically characterized, demonstrating that {mechanism}. "
                "This work establishes a clear experimental foundation linking catalyst design to electrochemical performance."
            )
            
        return (
            f"Lithium-sulfur cathodes remain attractive given their high theoretical energy density, yet {gap}. "
            f"For {topic}, a practical design strategy is that {solution}. "
            f"In this evidence set, the working hypothesis is that {mechanism}. "
            "This framing provides a manuscript structure in which catalyst design, electronic-structure evidence, and sulfur-redox consequences are discussed as one coherent argument."
        )

    def _build_dft_results(self, topic: str, retrieved: dict[str, list[dict[str, Any]]], target_paper_type: str | None) -> str:
        dft_items = retrieved.get("dft_results", [])
        if not dft_items:
            return (
                f"Available DFT evidence for {topic} is still sparse, so this section should stay qualitative until adsorption energies, free-energy changes, or kinetic barriers are extracted from the literature set."
            )
        lines = []
        for item in dft_items[:3]:
            value_part = ""
            if item.get("value") is not None:
                value_part = f"{item['value']} {item.get('unit') or ''}".strip()
            evidence = item.get("evidence_text") or item.get("text") or ""
            sentence = (
                f"For {item.get('adsorbate') or 'the relevant sulfur intermediate'}, "
                f"the reported {item.get('property_type') or 'DFT descriptor'} is {value_part}."
            )
            if item.get("property_type") and item["property_type"] not in {"dos_claim", "charge_density_difference_claim"}:
                sentence += " This value can be used to argue how the catalytic site modulates adsorption or conversion energetics."
            if evidence:
                sentence += f" Evidence: {evidence}"
            lines.append(sentence)
        return " ".join(lines)

    def _build_discussion(self, topic: str, retrieved: dict[str, list[dict[str, Any]]], target_paper_type: str | None) -> str:
        mechanisms = retrieved.get("mechanism_claims", [])
        electrochem = retrieved.get("electrochemical_performance", [])
        parts = []
        if mechanisms:
            top_claim = mechanisms[0]
            parts.append(
                f"For {topic}, the mechanistic anchor is {top_claim.get('text')}. "
                "This claim should be discussed together with the corresponding DFT descriptors rather than as an isolated statement."
            )
        if electrochem:
            perf = electrochem[0]
            perf_bits = []
            if perf.get("capacity_value") is not None:
                perf_bits.append(f"capacity {perf['capacity_value']} mAh/g")
            if perf.get("rate"):
                perf_bits.append(f"rate {perf['rate']}")
            if perf.get("cycle_number") is not None:
                perf_bits.append(f"{perf['cycle_number']} cycles")
            if perf_bits:
                parts.append(
                    "Electrochemical evidence should then be used to show that the mechanistic trend is experimentally meaningful, "
                    f"for example using {' ,'.join(perf_bits).replace(' ,', ', ')}."
                )
        if not parts:
            parts.append(
                f"The discussion for {topic} should connect catalyst coordination, sulfur-species binding, and redox kinetics in one chain, while clearly separating supported facts from broader interpretation."
            )
        return " ".join(parts)

    def _build_figure_storyline(self, retrieved: dict[str, list[dict[str, Any]]]) -> list[str]:
        cards = retrieved.get("writing_cards", [])
        if cards:
            figure_logic = cards[0].get("figure_logic")
            if isinstance(figure_logic, str):
                try:
                    figure_logic = json.loads(figure_logic)
                except json.JSONDecodeError:
                    figure_logic = None
            if isinstance(figure_logic, list) and figure_logic:
                storyline = []
                for item in figure_logic[:6]:
                    if isinstance(item, dict):
                        storyline.append(f"{item.get('fig_id') or 'Figure'}: {item.get('purpose') or 'supporting evidence'}")
                if storyline:
                    return storyline
        storyline = []
        if retrieved.get("sections"):
            storyline.append("Figure 1: catalyst structure and coordination evidence")
        if retrieved.get("dft_results"):
            storyline.append("Figure 2: adsorption energies, free-energy profile, or kinetic barriers")
        if retrieved.get("mechanism_claims"):
            storyline.append("Figure 3: mechanistic interpretation linking electronic structure to sulfur conversion")
        if retrieved.get("electrochemical_performance"):
            storyline.append("Figure 4: electrochemical validation under practical sulfur-loading or cycling conditions")
        return storyline or ["Figure 1: overview of the catalyst concept and supporting evidence"]

    def _enforce_guardrails(
        self,
        content: dict[str, Any],
        rule_sections: dict[str, Any],
        retrieved: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        guarded = dict(content)
        actions: dict[str, str] = {}
        for section_name in ["introduction", "dft_results", "discussion"]:
            text = guarded.get(section_name)
            if not isinstance(text, str) or not text.strip():
                continue
            verdict = self.citation_guard.validate(text, retrieved)
            if verdict["ok"]:
                continue
            repaired, action = self._repair_section_with_rule_seed(
                generated_text=text,
                rule_text=rule_sections.get(section_name, ""),
                retrieved=retrieved,
            )
            guarded[section_name] = repaired
            actions[section_name] = action
        return guarded, actions

    def _repair_section_with_rule_seed(
        self,
        generated_text: str,
        rule_text: str,
        retrieved: dict[str, list[dict[str, Any]]],
    ) -> tuple[str, str]:
        generated_sentences = self._split_sentences(generated_text)
        rule_sentences = self._split_sentences(rule_text)
        if not generated_sentences or not rule_sentences:
            return rule_text or generated_text, "reverted_to_rule_seed_due_to_unsupported_claims"

        repaired: list[str] = []
        replaced_count = 0
        fact_claim_only_count = 0
        for sentence in generated_sentences:
            verdict = self.citation_guard.validate(sentence, retrieved)
            if verdict["ok"]:
                repaired.append(sentence)
                continue
            # Not ok — determine if only fact claims are missing (no missing values)
            has_missing_values = bool(verdict.get("missing_values"))
            has_missing_fact_claims = bool(verdict.get("missing_fact_claims"))
            if not has_missing_values and not has_missing_fact_claims:
                # No actionable issues (e.g. checked_count == 0 and checked_fact_count == 0)
                repaired.append(sentence)
                continue
            repaired.append(self._select_rule_replacement(sentence, rule_sentences, retrieved))
            replaced_count += 1
            if has_missing_fact_claims and not has_missing_values:
                fact_claim_only_count += 1

        repaired_text = " ".join(part.strip() for part in repaired if part.strip()).strip()
        if not repaired_text:
            return rule_text or generated_text, "reverted_to_rule_seed_due_to_unsupported_claims"
        if replaced_count == 0:
            return repaired_text, "guard_reviewed_without_replacement"
        if replaced_count >= len(generated_sentences):
            suffix = "_fact_claim_unsupported" if fact_claim_only_count == replaced_count else ""
            return repaired_text, f"reverted_all_sentences_with_rule_seed_due_to_unsupported_claims{suffix}"
        suffix = "_fact_claim_unsupported" if fact_claim_only_count == replaced_count else ""
        return repaired_text, f"replaced_{replaced_count}_unsupported_sentence_with_rule_seed_support{suffix}"

    def _select_rule_replacement(
        self,
        sentence: str,
        rule_sentences: list[str],
        retrieved: dict[str, list[dict[str, Any]]],
    ) -> str:
        sentence_tokens = self._tokenize(sentence)
        best_sentence = rule_sentences[0]
        best_score = -1
        for candidate in rule_sentences:
            verdict = self.citation_guard.validate(candidate, retrieved)
            if (verdict["checked_count"] > 0 or verdict.get("checked_fact_count", 0) > 0) and not verdict["ok"]:
                continue
            overlap = len(sentence_tokens & self._tokenize(candidate))
            score = overlap
            if score > best_score:
                best_sentence = candidate
                best_score = score
        return best_sentence

    @classmethod
    def _split_sentences(cls, text: str) -> list[str]:
        return [part.strip() for part in cls.SENTENCE_SPLIT_PATTERN.split(text or "") if part and part.strip()]

    @classmethod
    def _tokenize(cls, text: str) -> set[str]:
        return {token.lower() for token in cls.TOKEN_PATTERN.findall(text or "") if len(token) > 1}

    @staticmethod
    def _resolve_prompt_path(path: Path) -> Path:
        if path.is_absolute():
            return path
        return Path(__file__).resolve().parents[3] / path
