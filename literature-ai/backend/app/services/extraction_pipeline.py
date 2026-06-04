from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from app.config import Settings

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceLocator,
    EvidenceSpan,
    MechanismClaim,
    Paper,
    WritingCard,
)
from app.extractors.catalyst_extractor import CatalystExtractor
from app.extractors.dft_settings_extractor import DFTSettingsExtractor
from app.extractors.dft_results_extractor import DFTResultsExtractor
from app.extractors.electrochemical_performance_extractor import ElectrochemicalPerformanceExtractor
from app.extractors.mechanism_extractor import MechanismExtractor
from app.extractors.writing_card_extractor import WritingCardExtractor
from app.extractors.comprehensive_extractor import ComprehensiveExtractor
from app.normalizers.chemistry_normalizer import ChemistryNormalizer
from app.normalizers.dft_normalizer import DFTNormalizer
from app.schemas.documents import UnifiedPaperDocument
from app.services.embedding import get_embedding_service
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.review_target_resolver import ReviewTargetResolver
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.workbench_status import EXTRACTION_PROTOCOL_VERSION


STAGE2_LOCATOR_TARGET_TYPES = [
    "CatalystSample",
    "DFTSetting",
    "DFTResult",
    "MechanismClaim",
    "ElectrochemicalPerformance",
    "catalyst_samples",
    "dft_settings",
    "dft_results",
    "mechanism_claims",
    "electrochemical_performance",
    "catalyst_sample",
    "dft_setting",
    "dft_result",
    "mechanism_claim",
]

STAGE2_EVIDENCE_SPAN_TYPES = [
    "dft_setting",
    "catalyst_sample",
    "dft_result",
    "electrochemical_performance",
    "mechanism_claim",
    "writing_card",
]


class ExtractionPipelineService:
    """Runs Stage 2 extractors and persists normalized MVP outputs."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.dft_settings_extractor = DFTSettingsExtractor()
        self.catalyst_extractor = CatalystExtractor()
        self.dft_results_extractor = DFTResultsExtractor(settings)
        self.electrochemical_extractor = ElectrochemicalPerformanceExtractor()
        self.mechanism_extractor = MechanismExtractor()
        self.writing_card_extractor = WritingCardExtractor(settings)
        self.comprehensive_extractor = ComprehensiveExtractor(settings)
        self.dft_normalizer = DFTNormalizer()
        self.chemistry_normalizer = ChemistryNormalizer()
        self.embedding = get_embedding_service(
            provider=settings.embedding_provider,
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        self.locators = EvidenceLocatorService(session)

    def _rule_based_classify(
        self,
        title: str | None,
        journal: str | None,
        abstract: str | None = None,
        sections_text: str | None = None,
    ) -> dict[str, Any]:
        """Heuristic fallback classification using title, journal, abstract, and body text."""
        text = " ".join(part for part in [title, journal, abstract, sections_text] if part).lower()
        review_kw = ["review", "perspective", "overview", "mini review", "综述", "进展", "述评", "总结"]
        computational_kw = [
            "dft", "density functional", "ab initio", "first-principles", "molecular dynamics",
            "electronic structure", "band structure", "adsorption energy", "计算", "第一性原理",
            "密度泛函", "分子动力学", "理论研究", "模拟", "电子结构",
        ]
        experimental_kw = [
            "synthesis", "catalyst preparation", "in-situ", "operando", "experiment",
            "electrochemical", "characterization", "fabrication", "测试", "表征", "合成",
            "制备", "实验", "原位", "电化学", "循环性能",
        ]

        has_review = any(kw in text for kw in review_kw)
        has_comp = any(kw in text for kw in computational_kw)
        has_exp = any(kw in text for kw in experimental_kw)

        if has_review and not (has_comp or has_exp):
            ptype = "R"
            confidence = 0.72
        elif has_comp and has_exp:
            ptype = "B"
            confidence = 0.68
        elif has_comp:
            ptype = "A"
            confidence = 0.64
        elif has_exp:
            ptype = "C"
            confidence = 0.62
        elif has_review:
            ptype = "R"
            confidence = 0.58
        else:
            ptype = "Unknown"
            confidence = 0.0

        return {
            "paper_type": ptype,
            "type_confidence": confidence,
            "classification_source": "rule_heuristic"
        }

    def run_stage2(self, paper: Paper, document: UnifiedPaperDocument) -> dict[str, int]:
        # Stage 2a (快速分类)
        quick_class = None
        sections_text = "\n".join(section.text or "" for section in (document.sections or [])[:12])
        if not document.sections and not document.abstract:
            quick_class = self._rule_based_classify(paper.title, paper.journal, paper.abstract, sections_text)
        else:
            try:
                quick_class = self.comprehensive_extractor.extract_quick_classification(document)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("LLM quick classification failed, falling back to rules: %s", exc)
            
            if not quick_class or quick_class.get("paper_type") == "Unknown":
                quick_class = self._rule_based_classify(paper.title, paper.journal, document.abstract, sections_text)

        paper_type = "Unknown"
        if quick_class:
            paper.paper_type = quick_class.get("paper_type", "Unknown")
            paper.type_confidence = quick_class.get("type_confidence", 0.0)
            paper.classification_source = quick_class.get("classification_source", "quick")
            paper_type = str(paper.paper_type)
            
        # Stage 2b (差异化抽取)
        is_computational = paper_type.startswith("A") or paper_type.startswith("B") or paper_type == "Unknown"
        is_experimental = paper_type.startswith("C") or paper_type.startswith("B") or paper_type == "Unknown"
        
        dft_settings = []
        dft_results = []
        catalyst_data = []
        electrochemical_items = []
        mechanism_claims = []
        
        if is_computational:
            dft_settings = self.dft_settings_extractor.extract(document)
            dft_results = self._refine_evidence_items(self.dft_results_extractor.extract(document), "dft_result", paper_type)
            
        if is_experimental or self._has_electrochemical_signal(document):
            electrochemical_items = self.electrochemical_extractor.extract(document)
            
        catalyst_data = self.catalyst_extractor.extract(document)
        mechanism_claims = self._refine_evidence_items(self.mechanism_extractor.extract(document), "mechanism_claim", paper_type)
        
        comprehensive_data_raw = self.comprehensive_extractor.extract(document)
        
        writing_card_raw = self.writing_card_extractor.extract(document)
        if writing_card_raw and comprehensive_data_raw and comprehensive_data_raw.get("paper_type"):
            writing_card_raw["paper_type"] = comprehensive_data_raw.get("paper_type")
            
        writing_card = self._refine_writing_card(writing_card_raw)
        comprehensive_data = self._refine_comprehensive_analysis(
            comprehensive_data_raw,
            dft_results=dft_results,
            writing_card=writing_card,
        )

        settings_count = self._persist_dft_settings(paper.id, dft_settings, document)
        catalyst_count = self._persist_catalyst_samples(paper.id, catalyst_data)
        dft_count = self._persist_dft_results(paper.id, dft_results)
        electrochemical_count = self._persist_electrochemical_performance(paper.id, electrochemical_items)
        mechanism_count = self._persist_mechanism_claims(paper.id, mechanism_claims)
        writing_count = self._persist_writing_card(paper.id, writing_card)
        
        if comprehensive_data:
            paper.comprehensive_analysis = comprehensive_data
            ptype = comprehensive_data.get("paper_type")
            if ptype:
                paper.paper_type = str(ptype)
                if "type_confidence" in comprehensive_data:
                    conf = comprehensive_data["type_confidence"]
                    paper.type_confidence = float(conf) if conf is not None else 0.0
                paper.classification_source = "full"
                
        self.session.add(paper)
        self.session.flush()
            
        return {
            "dft_settings": settings_count,
            "catalyst_samples": catalyst_count,
            "dft_results": dft_count,
            "electrochemical_performance": electrochemical_count,
            "mechanism_claims": mechanism_count,
            "writing_cards": writing_count,
            "comprehensive_analysis": 1 if comprehensive_data else 0,
        }

    @staticmethod
    def _has_electrochemical_signal(document: UnifiedPaperDocument) -> bool:
        parts: list[str] = []
        if document.abstract:
            parts.append(document.abstract)
        for section in document.sections or []:
            parts.append(section.section_title or "")
            parts.append(section.section_type or "")
            parts.append(section.text or "")
        for table in document.tables or []:
            parts.append(table.caption or "")
            parts.append(table.markdown_content or "")
        text = "\n".join(parts)
        if not text.strip():
            return False
        strong_patterns = [
            r"\b\d+(?:\.\d+)?\s*mAh\s*/?\s*g(?:-1)?\b",
            r"\b\d+(?:\.\d+)?\s*mg\s*/?\s*cm(?:-2|2)\b",
            r"\b\d+(?:\.\d+)?\s*C\b",
            r"\b\d{2,5}\s+cycles?\b",
            r"\bsulfur\s+loading\b",
            r"\belectrochemical\s+performance\b",
        ]
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in strong_patterns)

    def _refine_evidence_items(self, items: list[dict[str, Any]] | None, item_type: str, paper_type: str | None = None) -> list[dict[str, Any]]:
        refined: list[dict[str, Any]] = []
        for item in items or []:
            evidence_text = str(item.get("evidence_text") or "").strip()
            if not evidence_text:
                continue
            evidence_score = self._evidence_score(evidence_text, item.get("source_location") or {}, paper_type)
            normalized = dict(item)
            try:
                base_conf = float(normalized.get("confidence") or 0.0)
            except (TypeError, ValueError):
                base_conf = 0.0
            adjusted_conf = max(0.15, min((base_conf * 0.6) + (evidence_score * 0.4), 0.99))
            if item_type == "dft_result":
                category = str(normalized.get("category") or "")
                if category in {"adsorption_energy", "gibbs_free_energy_change", "reaction_barrier", "li2s_decomposition_barrier", "li2s_nucleation_barrier", "bader_charge", "charge_transfer", "d_band_center"}:
                    if normalized.get("value") is None:
                        continue
                    if not normalized.get("unit") and category not in {"charge_transfer", "bader_charge"}:
                        adjusted_conf = min(adjusted_conf, 0.62)
            if item_type == "mechanism_claim":
                claim_text = str(normalized.get("claim_text") or "").strip()
                if len(claim_text) < 20:
                    continue
            normalized["confidence"] = round(adjusted_conf, 2)
            normalized["quality_flag"] = "ok" if evidence_score >= 0.75 else ("review" if evidence_score >= 0.5 else "weak")
            refined.append(normalized)
        return refined

    def _refine_writing_card(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        if not payload:
            return {}
        refined = dict(payload)
        evidence_chain = []
        seen: set[str] = set()
        for item in payload.get("evidence_chain") or []:
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
            if len(text) < 20:
                continue
            key = text[:140].lower()
            if key in seen:
                continue
            seen.add(key)
            evidence_chain.append({"text": text[:400], "source": source or "Unknown"})
        refined["evidence_chain"] = evidence_chain[:20]
        return refined

    def _refine_comprehensive_analysis(
        self,
        payload: dict[str, Any] | None,
        dft_results: list[dict[str, Any]],
        writing_card: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not payload:
            return None
        refined = dict(payload)
        comp_results = self._refine_evidence_items(refined.get("computational_results") or [], item_type="dft_result")
        merged_results, merged_count, dropped_count = self._merge_dft_result_views(comp_results, dft_results)
        refined["computational_results"] = merged_results or None
        if writing_card and refined.get("writing_logic"):
            chain = refined["writing_logic"].get("evidence_chain") or []
            if not chain and writing_card.get("evidence_chain"):
                refined["writing_logic"] = dict(refined["writing_logic"])
                refined["writing_logic"]["evidence_chain"] = [
                    {"step_description": item.get("text", "")[:300]}
                    for item in writing_card.get("evidence_chain", [])[:6]
                    if item.get("text")
                ]
        refined["quality_checks"] = {
            "computational_results_total": len(merged_results),
            "computational_results_merged_from_dft": merged_count,
            "computational_results_dropped_as_weak": dropped_count,
            "writing_evidence_chain_total": len(writing_card.get("evidence_chain") or []),
            "low_confidence_computational_results": sum(1 for item in merged_results if (item.get("confidence") or 0) < 0.55),
        }
        return refined

    def _merge_dft_result_views(
        self,
        comprehensive_results: list[dict[str, Any]],
        dft_results: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int]:
        merged: dict[str, dict[str, Any]] = {}
        dropped = 0
        added = 0
        for item in comprehensive_results:
            key = self._dft_result_key(item)
            if not key:
                dropped += 1
                continue
            merged[key] = item
        for item in dft_results:
            key = self._dft_result_key(item)
            if not key:
                continue
            existing = merged.get(key)
            if existing is None or (item.get("confidence") or 0) > (existing.get("confidence") or 0):
                if existing is None:
                    added += 1
                merged[key] = item
        ordered = sorted(
            merged.values(),
            key=lambda item: (
                str(item.get("category") or ""),
                str(item.get("adsorbate") or ""),
                float(item.get("value") or 0) if item.get("value") is not None else 0.0,
            ),
        )
        return ordered, added, dropped

    @staticmethod
    def _dft_result_key(item: dict[str, Any]) -> str | None:
        category = str(item.get("category") or "").strip()
        evidence = str(item.get("evidence_text") or "").strip()
        if not category or not evidence:
            return None
        value = item.get("value")
        if value is not None:
            try:
                value = round(float(value), 6)
            except (TypeError, ValueError):
                value = item.get("value")
        return "|".join(
            [
                category,
                str(item.get("adsorbate") or "").strip().lower(),
                str(value),
                str(item.get("unit") or "").strip().lower(),
            ]
        )

    @staticmethod
    def _evidence_score(evidence_text: str, source_location: dict[str, Any], paper_type: str | None = None) -> float:
        text = re.sub(r"\s+", " ", evidence_text).strip()
        score = 0.25
        if len(text) >= 40:
            score += 0.2
        if len(text) >= 90:
            score += 0.15
        if re.search(r"\d", text):
            score += 0.15
        if any(source_location.get(key) for key in ("section", "page", "figure", "table")):
            score += 0.15
            
        is_comp = paper_type and paper_type.startswith("A")
        is_exp = paper_type and paper_type.startswith("C")
        
        # 遗漏 E1 修正与优化：细化独立特征词库，避免混合偏差
        COMPUTATIONAL_KEYWORDS = r"(adsorption|barrier|free energy|bader|charge|dos|d\.band|dft|vasp|functional|k-point)"
        EXPERIMENTAL_KEYWORDS = r"(capacity|cycle|xps|xrd|exafs|xanes|sem|tem|eis|cv|lsv|bet|synthesis|in-situ|operando)"
        
        if is_comp and not is_exp:
            if re.search(COMPUTATIONAL_KEYWORDS, text, re.IGNORECASE):
                score += 0.1
        elif is_exp and not is_comp:
            if re.search(EXPERIMENTAL_KEYWORDS, text, re.IGNORECASE):
                score += 0.1
        else:
            if re.search(COMPUTATIONAL_KEYWORDS, text, re.IGNORECASE) or re.search(EXPERIMENTAL_KEYWORDS, text, re.IGNORECASE):
                score += 0.1
                
        return min(score, 1.0)

    def replace_stage2(self, paper: Paper, document: UnifiedPaperDocument) -> dict[str, int]:
        ReviewTargetResolver(self.session).backfill_review_targets(paper.id)
        self._delete_existing_stage2(paper.id)
        summary = self.run_stage2(paper, document)
        ReviewTargetResolver(self.session).remap_reviews_for_paper(paper.id)
        return summary

    def _delete_existing_stage2(self, paper_id: UUID) -> None:
        self.session.execute(
            delete(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.claim_id.is_(None),
                EvidenceLocator.target_type.in_(STAGE2_LOCATOR_TARGET_TYPES),
            )
        )
        self.session.execute(
            delete(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.claim_id.is_(None),
                EvidenceLocator.chunk_id.is_not(None),
                EvidenceLocator.target_type.in_(STAGE2_EVIDENCE_SPAN_TYPES),
            )
        )
        for model in (DFTSetting, CatalystSample, DFTResult, ElectrochemicalPerformance, MechanismClaim, WritingCard):
            self.session.execute(delete(model).where(model.paper_id == paper_id))
        self.session.execute(
            delete(EvidenceSpan).where(
                EvidenceSpan.paper_id == paper_id,
                EvidenceSpan.object_type.in_(STAGE2_EVIDENCE_SPAN_TYPES),
            )
        )

    def _persist_dft_settings(
        self,
        paper_id: UUID,
        payload: dict[str, list[dict[str, Any]]],
        document: UnifiedPaperDocument | None = None,
    ) -> int:
        if not payload:
            return 0

        def first_value(field: str) -> str | None:
            entries = payload.get(field) or []
            if not entries:
                return None
            return entries[0].get("value")

        def first_entry(field: str) -> dict[str, Any] | None:
            entries = payload.get(field) or []
            return entries[0] if entries else None

        def numeric_with_unit(field: str) -> tuple[float | None, str | None]:
            entry = first_entry(field) or {}
            value = self._safe_float(entry.get("value"))
            unit = str(entry.get("unit") or "").strip()
            return value, unit

        cutoff_value, cutoff_unit = numeric_with_unit("cutoff energy")
        if cutoff_value is not None and cutoff_unit.lower() in {"ry", "rydberg"}:
            cutoff_value = cutoff_value * 13.605693122994
        vacuum_value, vacuum_unit = numeric_with_unit("vacuum thickness")
        if vacuum_value is not None and vacuum_unit.lower() == "nm":
            vacuum_value = vacuum_value * 10.0
        normalization_payload: dict[str, Any] = {"extracted": payload}
        if document is not None:
            normalization_payload["supporting_text"] = self._collect_dft_context_text(document)
        normalized = self.dft_normalizer.normalize(normalization_payload)
        record = DFTSetting(
            paper_id=paper_id,
            software=first_value("software"),
            functional=first_value("functional"),
            dispersion_correction=first_value("dispersion correction"),
            pseudopotential=first_value("pseudopotential / basis set"),
            cutoff_energy_ev=cutoff_value,
            k_points=first_value("k-points"),
            convergence_settings={
                **self._group_field_payload(payload, ["convergence criteria", "spin polarization", "solvation model"]),
                "reproducibility": {
                    "score": normalized.get("dft_reproducibility_score"),
                    "missing_items": normalized.get("missing_items"),
                    "risk_level": normalized.get("risk_level"),
                },
            },
            vacuum_thickness_a=vacuum_value,
            raw_json={
                "extracted": payload,
                "normalized": normalized,
                "supporting_text": normalization_payload.get("supporting_text"),
            },
        )
        self.session.add(record)
        self.session.flush()

        for field_name, entries in payload.items():
            for entry in entries or []:
                self._persist_evidence_span(
                    paper_id=paper_id,
                    object_type="dft_setting",
                    object_id=str(record.id),
                    item=entry,
                    fallback_section=field_name,
                )
        return 1

    def _persist_catalyst_samples(self, paper_id: UUID, payload: dict[str, list[dict[str, Any]]]) -> int:
        if not payload:
            return 0

        atomicity = self._first_payload_value(payload, "single atom / dual atom")
        catalyst_type = self._normalize_catalyst_type(atomicity)
        metals = self._payload_values(payload, "metal centers")
        coordination = self._first_payload_value(payload, "coordination")
        support = self._first_payload_value(payload, "support")
        synthesis_method = self._first_payload_value(payload, "synthesis method")
        structural_evidence = self._payload_values(payload, "structural evidence: HAADF-STEM / XANES / EXAFS / XPS")
        norm_cat = self.chemistry_normalizer.normalize({
            "name": self._build_catalyst_name(metals, coordination, support) or "",
            "catalyst_type": catalyst_type or "",
            "support": support or "",
            "metal_centers": metals or [],
        })
        record = CatalystSample(
            paper_id=paper_id,
            name=norm_cat.get("name") or self._build_catalyst_name(metals, coordination, support),
            catalyst_type=catalyst_type,
            metal_centers=metals,
            coordination=coordination,
            support=support,
            synthesis_method=synthesis_method,
            evidence_strength=", ".join(structural_evidence) if structural_evidence else None,
        )
        self.session.add(record)
        self.session.flush()

        for field_name, entries in payload.items():
            for entry in entries or []:
                self._persist_evidence_span(
                    paper_id=paper_id,
                    object_type="catalyst_sample",
                    object_id=str(record.id),
                    item=entry,
                    fallback_section=field_name,
                )
        return 1

    def _persist_dft_results(self, paper_id: UUID, items: list[dict[str, Any]]) -> int:
        count = 0
        for item in items:
            location = item.get("source_location") or {}
            norm_item = self.chemistry_normalizer.normalize({
                "adsorbate": item.get("adsorbate") or "",
                "property_type": item.get("category") or "",
            })
            record = DFTResult(
                paper_id=paper_id,
                adsorbate=norm_item.get("adsorbate") or item.get("adsorbate"),
                property_type=norm_item.get("property_type") or item.get("category"),
                value=item.get("value"),
                unit=item.get("unit"),
                reaction_step=item.get("reaction_step"),
                source_section=location.get("section"),
                source_figure=location.get("figure"),
                evidence_text=item.get("evidence_text"),
                confidence=item.get("confidence"),
                candidate_status="Codex_Candidate",
                evidence_payload=PaperWorkbenchService.dft_evidence_payload(item),
                extraction_protocol_version=EXTRACTION_PROTOCOL_VERSION,
            )
            self.session.add(record)
            self.session.flush()
            self._persist_evidence_span(
                paper_id=paper_id,
                object_type="dft_result",
                object_id=str(record.id),
                item=item,
            )
            count += 1
        return count

    def _persist_electrochemical_performance(self, paper_id: UUID, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        grouped: dict[str, dict[str, Any]] = {}
        for item in items:
            field_name = item.get("field_name")
            if not field_name:
                continue
            existing = grouped.get(field_name)
            if existing is None or (item.get("confidence") or 0.0) > (existing.get("confidence") or 0.0):
                grouped[field_name] = item

        record = ElectrochemicalPerformance(
            paper_id=paper_id,
            sulfur_loading_mg_cm2=self._safe_float(grouped.get("sulfur_loading", {}).get("sulfur_loading_mg_cm2")),
            sulfur_content_wt_percent=self._safe_float(grouped.get("sulfur_content", {}).get("sulfur_content_wt_percent")),
            electrolyte_sulfur_ratio=self._stringify_ratio(grouped.get("electrolyte_sulfur_ratio")),
            capacity_value=self._safe_float(grouped.get("capacity", {}).get("capacity_value")),
            cycle_number=grouped.get("cycle_number", {}).get("cycle_number"),
            rate=grouped.get("rate", {}).get("rate"),
            decay_per_cycle=self._safe_float(grouped.get("decay_per_cycle", {}).get("decay_per_cycle")),
            evidence_text=self._join_evidence(items),
        )
        self.session.add(record)
        self.session.flush()

        for item in items:
            self._persist_evidence_span(
                paper_id=paper_id,
                object_type="electrochemical_performance",
                object_id=str(record.id),
                item=item,
                fallback_section=item.get("field_name"),
            )
        return 1

    def _persist_mechanism_claims(self, paper_id: UUID, items: list[dict[str, Any]]) -> int:
        count = 0
        for item in items:
            record = MechanismClaim(
                paper_id=paper_id,
                claim_type=item.get("mechanism_type"),
                claim_text=item.get("claim_text") or "",
                evidence_types=item.get("key_species") or [],
                confidence=item.get("confidence"),
                evidence_text=item.get("evidence_text"),
            )
            self.session.add(record)
            self.session.flush()
            self._persist_evidence_span(
                paper_id=paper_id,
                object_type="mechanism_claim",
                object_id=str(record.id),
                item=item,
            )
            count += 1
        return count

    def _persist_writing_card(self, paper_id: UUID, payload: dict[str, Any]) -> int:
        if not payload:
            return 0

        # Keep figure_logic JSON-encoded because the current column is text.
        figure_logic = payload.get("figure_logic")
        embedding_text = "\n".join(
            filter(
                None,
                [
                    payload.get("paper_type"),
                    payload.get("research_gap"),
                    payload.get("proposed_solution"),
                    payload.get("core_hypothesis"),
                    payload.get("abstract_logic"),
                    payload.get("introduction_logic"),
                    payload.get("discussion_logic"),
                ],
            )
        )
        record = WritingCard(
            paper_id=paper_id,
            paper_type=payload.get("paper_type"),
            research_gap=payload.get("research_gap"),
            proposed_solution=payload.get("proposed_solution"),
            core_hypothesis=payload.get("core_hypothesis"),
            evidence_chain=payload.get("evidence_chain"),
            section_strategy=payload.get("section_strategy"),
            figure_logic=json.dumps(figure_logic, ensure_ascii=False) if figure_logic is not None else None,
            abstract_logic=payload.get("abstract_logic"),
            introduction_logic=payload.get("introduction_logic"),
            discussion_logic=payload.get("discussion_logic"),
            embedding=self.embedding.embed_text(embedding_text),
        )
        self.session.add(record)
        self.session.flush()

        for evidence in payload.get("evidence_chain") or []:
            self.session.add(
                EvidenceSpan(
                    paper_id=paper_id,
                    object_type="writing_card",
                    object_id=str(record.id),
                    text=evidence.get("text") or "",
                    section=evidence.get("source"),
                    confidence=0.75,
                )
            )
        return 1

    def _persist_evidence_span(
        self,
        paper_id: UUID,
        object_type: str,
        object_id: str,
        item: dict[str, Any],
        fallback_section: str | None = None,
    ) -> None:
        location = item.get("source_location") or {}
        evidence_text = item.get("evidence_text")
        if not evidence_text:
            return
        self.session.add(
            EvidenceSpan(
                paper_id=paper_id,
                object_type=object_type,
                object_id=object_id,
                text=evidence_text,
                page=location.get("page"),
                section=location.get("section") or fallback_section,
                figure=location.get("figure"),
                table=location.get("table"),
                confidence=item.get("confidence"),
            )
        )
        self.session.flush()
        self.locators.create_locator_for_span(
            paper_id=paper_id,
            object_type=object_type,
            object_id=object_id,
            evidence_text=evidence_text,
            page=location.get("page"),
            section=location.get("section") or fallback_section,
            figure=location.get("figure"),
            table=location.get("table"),
            confidence=item.get("confidence"),
            bbox=location.get("bbox"),
            parser_source=item.get("parser_source") or "unknown",
        )

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        import re
        val_str = str(value).replace(",", "").strip()
        try:
            return float(val_str)
        except (TypeError, ValueError):
            pass
        match = re.search(r"[-+]?\d*\.?\d+", val_str)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                pass
        return None

    @staticmethod
    def _group_field_payload(payload: dict[str, list[dict[str, Any]]], fields: list[str]) -> dict[str, Any]:
        return {field: payload.get(field, []) for field in fields if payload.get(field)}

    @staticmethod
    def _first_payload_value(payload: dict[str, list[dict[str, Any]]], field: str) -> str | None:
        entries = payload.get(field) or []
        if not entries:
            return None
        return entries[0].get("value")

    @staticmethod
    def _payload_values(payload: dict[str, list[dict[str, Any]]], field: str) -> list[str]:
        entries = payload.get(field) or []
        seen: list[str] = []
        for entry in entries:
            value = entry.get("value")
            if value and value not in seen:
                seen.append(value)
        return seen

    @staticmethod
    def _normalize_catalyst_type(value: str | None) -> str | None:
        if not value:
            return None
        lowered = value.lower()
        if "dual" in lowered or "dac" in lowered:
            return "dual_atom"
        if "single" in lowered or "sac" in lowered:
            return "single_atom"
        return lowered.replace(" ", "_")

    @staticmethod
    def _build_catalyst_name(metals: list[str], coordination: str | None, support: str | None) -> str | None:
        parts = []
        if coordination:
            parts.append(coordination)
        elif metals:
            parts.append("-".join(metals))
        if support:
            parts.append(support)
        return " / ".join(parts) if parts else None

    @staticmethod
    def _collect_dft_context_text(document: UnifiedPaperDocument) -> str:
        snippets: list[str] = []
        for section in document.sections:
            title = (getattr(section, "section_title", None) or getattr(section, "section_type", None) or "").lower()
            if any(keyword in title for keyword in ["comput", "method", "theoretical", "dft", "supporting"]):
                text = getattr(section, "text", None)
                if text:
                    snippets.append(text)
        abstract = getattr(document, "abstract", None)
        if abstract:
            snippets.append(abstract)
        return "\n".join(snippets)

    @staticmethod
    def _join_evidence(items: list[dict[str, Any]]) -> str | None:
        snippets: list[str] = []
        for item in items:
            text = item.get("evidence_text")
            if text and text not in snippets:
                snippets.append(text)
        if not snippets:
            return None
        return "\n".join(snippets[:4])

    @staticmethod
    def _stringify_ratio(item: dict[str, Any] | None) -> str | None:
        if not item:
            return None
        value = item.get("value")
        unit = item.get("unit")
        if value is None:
            return None
        return f"{value} {unit}".strip()
