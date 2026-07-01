from __future__ import annotations

from collections import Counter
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    MechanismClaim,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_rescan_policy import build_dft_dedupe_signature
from app.utils.protocol_tracking import protocol_snapshot
from app.utils.workbench_status import EXTRACTION_PROTOCOL_VERSION, WORKBENCH_SCHEMA_VERSION


SUPPLEMENTARY_RELATIONSHIP_TYPES = {
    "supplementary",
    "supplementary_information",
    "supporting_information",
    "si",
}


class PaperWorkbenchAiPackageMixin:
    """AI reading package and DFT evidence payload helpers."""

    def _write_ai_reading_package(self, paper: Paper, dirs: dict[str, Path]) -> None:
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper.id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper.id)).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper.id)).all()
        supplementary_relationships = self.session.scalars(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == paper.id,
                PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
            )
        ).all()
        supplementary_paper_ids = [relationship.target_paper_id for relationship in supplementary_relationships]
        supplementary_papers = {
            item.id: item
            for item in (
                self.session.scalars(select(Paper).where(Paper.id.in_(supplementary_paper_ids))).all()
                if supplementary_paper_ids
                else []
            )
        }
        supplementary_tables = (
            self.session.scalars(
                select(PaperTable)
                .where(PaperTable.paper_id.in_(supplementary_paper_ids))
                .order_by(PaperTable.paper_id.asc(), PaperTable.page.asc().nulls_last())
            ).all()
            if supplementary_paper_ids
            else []
        )
        supplementary_figures_available_count = (
            self.session.scalar(
                select(func.count(PaperFigure.id)).where(PaperFigure.paper_id.in_(supplementary_paper_ids))
            )
            if supplementary_paper_ids
            else 0
        ) or 0
        display_tables = list(tables) + list(supplementary_tables)
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        dft_settings = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id == paper.id)).all()
        catalyst_samples = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper.id)).all()
        electrochemical_items = self.session.scalars(
            select(ElectrochemicalPerformance).where(ElectrochemicalPerformance.paper_id == paper.id)
        ).all()
        mechanism_claims = self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper.id)).all()
        writing_cards = self.session.scalars(select(WritingCard).where(WritingCard.paper_id == paper.id)).all()
        audit = DFTCompletenessAuditor(self.session).audit_paper(paper.id, parsed_count=len(dft_rows))
        source_documents = self._source_documents_for_ai(paper)
        content_coverage = self._build_content_coverage_summary(
            paper=paper,
            sections=sections,
            tables=display_tables,
            figures=figures,
            dft_settings=dft_settings,
            dft_rows=dft_rows,
            catalyst_samples=catalyst_samples,
            electrochemical_items=electrochemical_items,
            mechanism_claims=mechanism_claims,
            writing_cards=writing_cards,
        )

        relevant_sections = [
            {
                "id": str(section.id),
                "role": self._section_role_for_coverage(section),
                "title": section.section_title or section.section_type,
                "page_start": section.page_start,
                "page_end": section.page_end,
                "section_level": section.section_level,
                "section_number": section.section_number,
                "parent_heading": section.parent_heading,
                "heading_path": section.heading_path or [],
                "text": section.text,
            }
            for section in sections
            if self._section_role_for_coverage(section) != "context" or any(
                example.get("source_id") == str(section.id) for example in audit.get("signal_examples", [])
            )
        ]
        self._write_json(
            dirs["extraction"] / "ai_reading_package.json",
            {
                "schema_version": "ai_reading_package_v1",
                "paper": self._paper_metadata(paper),
                "source_documents": source_documents,
                "abstract": paper.abstract,
                "llm_input_policy": {
                    "table_review_scope": "main_plus_supplementary",
                    "figure_review_scope": "main_only",
                    "include_supplementary_figures": False,
                    "supplementary_figures_available_count": int(supplementary_figures_available_count),
                    "supplementary_figures_policy": (
                        "Do not automatically sweep all SI figures. Request SI figures only when the user explicitly "
                        "asks, the task cites Figure Sxx, or an evidence anchor points to an SI figure."
                    ),
                    "text_llm_scope": [
                        "paper metadata",
                        "abstract",
                        "text sections",
                        "parsed markdown tables from main and supplementary documents",
                        "existing structured candidates",
                    ],
                    "excluded_from_text_llm": [
                        "figure images",
                        "figure crops",
                        "icons",
                        "chart visual value reading",
                    ],
                    "image_or_chart_review": "Use a human reviewer or IDE visual inspection; text-only AI must not infer values from images.",
                    "web_llm_extract": "disabled",
                    "required_workflow": (
                        "prepare-ai-context / codex-item -> IDE AI -> import_analysis. Non-DFT "
                        "metadata, sections, figure metadata, writing_cards, mechanism_claims, "
                        "electrochemical_performance, catalyst_samples, notes, and relationships may be "
                        "auto-applied with PDF evidence anchors and module write locks. DFT results/settings "
                        "remain candidates until the review/export gate passes. Table object lifecycle operations "
                        "use direct MCP table tools: update_table, create_table, merge_table, and delete_table."
                    ),
                },
                "review_scope": {
                    "table_review_scope": "main_plus_supplementary",
                    "figure_review_scope": "main_only",
                    "include_supplementary_figures": False,
                    "supplementary_figures_available_count": int(supplementary_figures_available_count),
                    "figure_derived_dft_policy": (
                        "If a main-paper figure contains explicit DFT values such as adsorption energy, binding "
                        "energy, dissociation energy, decomposition barrier, reaction barrier, free energy/Delta G, "
                        "Bader charge, or charge transfer, extract them only as DFT candidates with figure/page/text/"
                        "value/unit/property/material anchors. They must not become ML_Ready until the existing DFT "
                        "second review and export safety gate pass."
                    ),
                },
                "figure_dft_candidate_extraction_policy": {
                    "allowed_signal_examples": [
                        "adsorption energy",
                        "binding energy",
                        "dissociation energy",
                        "decomposition barrier",
                        "reaction barrier",
                        "free energy / Delta G",
                        "Bader charge",
                        "charge transfer",
                    ],
                    "required_fields": [
                        "figure_id or figure_label",
                        "page",
                        "quoted_text or readable annotation",
                        "value",
                        "unit",
                        "property_type",
                        "adsorbate or reaction_step",
                        "material_identity when available",
                    ],
                    "write_path": "import_analysis object_review_audits decision=new_candidate or existing DFT candidate audit path",
                    "must_not_set_status": "ML_Ready",
                    "requires_second_review": True,
                },
                "non_dft_direct_write_policy": {
                    "ai_can_apply_without_human_confirmation": [
                        "paper metadata",
                        "sections",
                        "figure metadata/captions/content_summary",
                        "writing_cards",
                        "mechanism_claims",
                        "electrochemical_performance",
                        "catalyst_samples",
                        "notes",
                        "relationships",
                    ],
                    "must_not_auto_apply": [
                        "dft_results",
                        "dft_settings",
                        "DFT export verification",
                        "table object mutations through import_analysis",
                        "figure image recrop/create through import_analysis",
                    ],
                    "evidence_required": [
                        "page, section/section_title, quoted_text, table, figure, or bbox anchor",
                        "catalyst_samples require a material anchor beyond free-form evidence_text",
                        "section creation requires a strong text/section/table/figure/bbox anchor",
                    ],
                    "write_path": (
                        "Use import_analysis with auto_apply_review_rules=true plus a module write lock for ordinary "
                        "non-DFT field writes. Use update_table/create_table/merge_table/delete_table directly for "
                        "table object lifecycle operations, then read back table count/status/audit records. "
                        "For figure image cropping, call recrop_figure or create_figure_from_bbox directly, "
                        "then read back the updated figure record."
                    ),
                },
                "content_coverage": content_coverage,
                "dft_completeness_audit": audit,
                "sections": relevant_sections,
                "tables": [
                    {
                        "id": str(table.id),
                        "paper_id": str(table.paper_id),
                        "source_document_type": (
                            "supplementary_information" if table.paper_id != paper.id else "main_text"
                        ),
                        "related_paper_id": str(table.paper_id) if table.paper_id != paper.id else None,
                        "related_paper_code": (
                            supplementary_papers.get(table.paper_id).paper_code
                            if table.paper_id in supplementary_papers
                            else None
                        ),
                        "writeback_paper_id": str(paper.id),
                        "caption": table.caption,
                        "page": table.page,
                        "markdown_content": table.markdown_content,
                        "prov": table.prov,
                    }
                    for table in display_tables
                ],
                "figures": [
                    {
                        "id": str(figure.id),
                        "source_document_type": "main_text",
                        "writeback_paper_id": str(paper.id),
                        "figure_label": figure.figure_label,
                        "caption": figure.caption,
                        "page": figure.page,
                        "image_path": figure.image_path,
                        "text_llm_allowed": False,
                        "review_route": "human_or_ide_visual_only",
                        "text_llm_note": (
                            "Do not ask a text-only LLM to interpret this figure image/crop or read chart values. "
                            "IDE visual review may extract explicit readable DFT annotations as DFT candidates only."
                        ),
                        "crop_status": figure.crop_status,
                        "crop_confidence": figure.crop_confidence,
                        "prov": figure.prov,
                    }
                    for figure in figures
                ],
                "existing_structured_content": {
                    "dft_settings": [
                        {
                            "id": str(row.id),
                            "software": row.software,
                            "functional": row.functional,
                            "dispersion_correction": row.dispersion_correction,
                            "pseudopotential": row.pseudopotential,
                            "cutoff_energy_ev": row.cutoff_energy_ev,
                            "k_points": row.k_points,
                            "convergence_settings": row.convergence_settings,
                            "vacuum_thickness_a": row.vacuum_thickness_a,
                            "raw_json": row.raw_json,
                        }
                        for row in dft_settings
                    ],
                    "catalyst_samples": [
                        {
                            "id": str(row.id),
                            "name": row.name,
                            "catalyst_type": row.catalyst_type,
                            "metal_centers": row.metal_centers,
                            "coordination": row.coordination,
                            "support": row.support,
                            "synthesis_method": row.synthesis_method,
                            "evidence_strength": row.evidence_strength,
                        }
                        for row in catalyst_samples
                    ],
                    "electrochemical_performance": [
                        {
                            "id": str(row.id),
                            "sulfur_loading_mg_cm2": row.sulfur_loading_mg_cm2,
                            "sulfur_content_wt_percent": row.sulfur_content_wt_percent,
                            "electrolyte_sulfur_ratio": row.electrolyte_sulfur_ratio,
                            "capacity_value": row.capacity_value,
                            "cycle_number": row.cycle_number,
                            "rate": row.rate,
                            "decay_per_cycle": row.decay_per_cycle,
                            "evidence_text": row.evidence_text,
                        }
                        for row in electrochemical_items
                    ],
                    "mechanism_claims": [
                        {
                            "id": str(row.id),
                            "claim_type": row.claim_type,
                            "claim_text": row.claim_text,
                            "evidence_types": row.evidence_types,
                            "confidence": row.confidence,
                            "evidence_text": row.evidence_text,
                        }
                        for row in mechanism_claims
                    ],
                    "writing_cards": [
                        {
                            "id": str(row.id),
                            "paper_type": row.paper_type,
                            "research_gap": row.research_gap,
                            "proposed_solution": row.proposed_solution,
                            "core_hypothesis": row.core_hypothesis,
                            "evidence_chain": row.evidence_chain,
                            "section_strategy": row.section_strategy,
                            "figure_logic": row.figure_logic,
                            "abstract_logic": row.abstract_logic,
                            "introduction_logic": row.introduction_logic,
                            "discussion_logic": row.discussion_logic,
                        }
                        for row in writing_cards
                    ],
                },
                "system_candidates": [
                    {
                        "record_id": str(row.id),
                        "candidate_status": row.candidate_status or "system_candidate",
                        "adsorbate": row.adsorbate,
                        "property_type": row.property_type,
                        "value": row.value,
                        "unit": row.unit,
                        "reaction_step": row.reaction_step,
                        "source_section": row.source_section,
                        "source_figure": row.source_figure,
                        "evidence_text": row.evidence_text,
                        "confidence": row.confidence,
                        "evidence_payload": row.evidence_payload,
                    }
                    for row in dft_rows
                ],
                "ai_task": (
                    "Read the main text and any available supplementary_information source documents. First repair "
                    "non-DFT content directly through import_analysis when there is checkable PDF evidence: metadata, "
                    "sections, figure metadata/summaries, writing_cards, mechanism_claims, "
                    "electrochemical_performance, catalyst_samples, notes, and relationships. For missing sections or "
                    "writing_cards, create objects with target_path=<collection>:new:create. For existing objects, use "
                    "replace corrections with target_path=<collection>:<id>:<field>, except for tables. Every table object mutation "
                    "is a direct MCP call: update_table for table field fixes, create_table for missing tables, "
                    "merge_table for parser-split or duplicate fragments, and delete_table for invalid table objects. "
                    "Use the table object's real paper_id for these table tools, including SI related_paper_id when the "
                    "table belongs to supplementary_information. Figure image crop/create operations "
                    "must call recrop_figure or create_figure_from_bbox directly instead of import_analysis. Extract "
                    "DFT data using the explicit AI protocol only as candidates: SI data belongs to this main paper_id, "
                    "but each candidate must mark evidence_location.source_document_type=supplementary_information. "
                    "Merge repeated main-text/SI occurrences into one candidate using dedupe_signature and "
                    "supporting_evidence. Do not treat values from cited or supporting references as this paper's DFT "
                    "data; mark them source_document_type=supporting_reference and borrowed_from_reference=true or "
                    "record them only as relationship evidence. Do not estimate values from images, curves, or axis "
                    "ticks with a text-only model. DFT results/settings are not verified until the existing review/export "
                    "gate passes."
                ),
            },
        )

    def _build_content_coverage_summary(
        self,
        *,
        paper: Paper,
        sections: list[PaperSection],
        tables: list[PaperTable],
        figures: list[PaperFigure],
        dft_settings: list[DFTSetting],
        dft_rows: list[DFTResult],
        catalyst_samples: list[CatalystSample],
        electrochemical_items: list[ElectrochemicalPerformance],
        mechanism_claims: list[MechanismClaim],
        writing_cards: list[WritingCard],
    ) -> dict[str, Any]:
        role_counts = Counter(self._section_role_for_coverage(section) for section in sections)
        missing_core_sections: list[str] = []
        if not str(paper.abstract or "").strip():
            missing_core_sections.append("abstract")
        for role in ("introduction", "methods", "results_discussion", "conclusion"):
            if role_counts.get(role, 0) == 0:
                missing_core_sections.append(role)

        structured_counts = {
            "sections": len(sections),
            "tables": len(tables),
            "figures": len(figures),
            "dft_settings": len(dft_settings),
            "dft_results": len(dft_rows),
            "catalyst_samples": len(catalyst_samples),
            "electrochemical_performance": len(electrochemical_items),
            "mechanism_claims": len(mechanism_claims),
            "writing_cards": len(writing_cards),
        }
        missing_structured_modules = [
            module
            for module in (
                "writing_cards",
                "mechanism_claims",
                "electrochemical_performance",
                "catalyst_samples",
            )
            if structured_counts[module] == 0
        ]
        recommended_actions: list[str] = []
        if missing_core_sections:
            recommended_actions.append(
                "Create or replace sections with strong PDF anchors for missing abstract/introduction/methods/results/conclusion coverage."
            )
        if structured_counts["writing_cards"] == 0:
            recommended_actions.append(
                "Create evidence-backed writing_cards covering research_gap, proposed_solution, core_hypothesis, evidence_chain, section_strategy, figure_logic, abstract_logic, introduction_logic, and discussion_logic."
            )
        if structured_counts["mechanism_claims"] == 0:
            recommended_actions.append("Create mechanism_claims for shuttle suppression, catalytic conversion, adsorption, diffusion, or electronic-structure claims when the text supports them.")
        if structured_counts["electrochemical_performance"] == 0:
            recommended_actions.append("Create electrochemical_performance rows for loading, capacity, cycle, rate, decay, and electrolyte/sulfur conditions when evidence is present.")
        if structured_counts["catalyst_samples"] == 0:
            recommended_actions.append("Create catalyst_samples for named materials/comparators with material anchors before linking mechanism or performance claims.")

        return {
            "section_role_counts": dict(sorted(role_counts.items())),
            "structured_counts": structured_counts,
            "missing_core_sections": missing_core_sections,
            "missing_structured_modules": missing_structured_modules,
            "non_dft_modules_open_for_ai_write": [
                "metadata",
                "sections",
                "tables",
                "figures",
                "writing_cards",
                "mechanism_claims",
                "electrochemical_performance",
                "catalyst_samples",
                "notes",
                "relationships",
            ],
            "dft_modules_review_gated": ["dft_results", "dft_settings"],
            "recommended_ai_actions": recommended_actions,
            "rag_priority": [
                "Evidence-backed writing_cards for high-quality review writing",
                "Mechanism and electrochemical claims with page/quote anchors",
                "Normalized sections for reliable retrieval and citation insertion",
            ],
        }

    @staticmethod
    def _section_role_for_coverage(section: PaperSection) -> str:
        title = f"{section.section_title or ''} {section.section_type or ''}".lower()
        if re.search(r"\babstract\b", title):
            return "abstract"
        if re.search(r"intro|background", title):
            return "introduction"
        if re.search(r"method|comput|dft|calculation|experimental|synthesis", title):
            return "methods"
        if re.search(r"result|discussion|performance|mechanism|characteri[sz]ation", title):
            return "results_discussion"
        if re.search(r"conclusion|summary", title):
            return "conclusion"
        return "context"

    @staticmethod
    def dft_evidence_payload(item: dict[str, Any]) -> dict[str, Any]:
        location = item.get("source_location") or {}
        payload = {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "protocol": protocol_snapshot("dft_ai_protocol", fallback_version=EXTRACTION_PROTOCOL_VERSION),
            "system_extractor_protocol": protocol_snapshot("dft_results", fallback_version=EXTRACTION_PROTOCOL_VERSION),
            "source_document_type": item.get("source_document_type") or location.get("source_document_type") or "main_text",
            "source_document_label": item.get("source_document_label") or location.get("source_document_label") or "Main PDF",
            "source_locator": location.get("source_locator") or location.get("locator"),
            "page": location.get("page"),
            "table": location.get("table"),
            "section": location.get("section"),
            "quoted_text": item.get("quoted_text") or item.get("evidence_text"),
            "catalyst_name": item.get("catalyst_name") or item.get("material_identity"),
            "material_identity": item.get("material_identity") or item.get("catalyst_name"),
            "active_site_context": item.get("active_site_context"),
            "structure_context": item.get("structure_context"),
            "dft_setting_id": item.get("dft_setting_id"),
            "supporting_evidence": item.get("supporting_evidence") or [],
            "field_sources": [
                {
                    "field_name": "value",
                    "source_type": item.get("parser_source") or "extraction",
                    "page": location.get("page"),
                    "section": location.get("section"),
                    "figure": location.get("figure"),
                    "table": location.get("table"),
                    "bbox": location.get("bbox"),
                    "excerpt": item.get("evidence_text"),
                    "confidence": item.get("confidence"),
                }
            ],
            "policy": "Candidate values require assigned AI/human review and confirmation before ML export.",
            "ai_protocol_policy": (
                "System rule extraction only creates system_candidate records. Final DFT/ML data must pass "
                "PDF evidence anchoring, AI protocol extraction/review, deduplication, completeness audit, "
                "and human or second-AI confirmation."
            ),
        }
        payload["dedupe_signature"] = item.get("dedupe_signature") or build_dft_dedupe_signature(
            {
                **item,
                "evidence_payload": payload,
                "paper_id": item.get("paper_id"),
            }
        )
        return payload
