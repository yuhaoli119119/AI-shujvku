"""Service for Evidence-Based Figure Reading and Guide Generation.

This service supports deep scientific interpretation of paper figures using
main-text context, referencing sections, and linked SI data, without altering
scientific review status or DFT candidate gates.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import Settings, get_settings
from app.db.models import (
    CatalystSample,
    DFTResult,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
)
from app.utils.artifact_paths import resolve_persisted_artifact_path

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text_for_matching(text: str) -> str:
    """Normalize text by collapsing whitespace, removing soft hyphens, and un-hyphenating line breaks."""
    if not text:
        return ""
    # Remove soft hyphens and invisible markers
    s = text.replace("\u00ad", "")
    # Fix hyphenated line breaks: word-\nword -> wordword
    s = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1\2', s)
    # Also handle regular hyphen followed by spaces
    s = re.sub(r'(\w+)-\s+(\w+)', r'\1-\2', s)
    # Collapse all whitespace/newlines to single space
    s = " ".join(s.split())
    return s.lower()


def compute_content_fingerprint(
    caption: str | None,
    image_bytes: bytes | None,
    sections_content: list[tuple[str, str]] | None = None,
) -> str:
    """Compute deterministic SHA-256 fingerprint for figure caption, image bytes, and actual section contents."""
    h = hashlib.sha256()
    h.update((caption or "").strip().encode("utf-8"))
    if image_bytes:
        h.update(image_bytes)
    if sections_content:
        # Sort deterministically by section_id
        for sid, text in sorted(sections_content, key=lambda x: str(x[0])):
            h.update(str(sid).encode("utf-8"))
            # Hash actual text content of the section
            h.update(hashlib.sha256((text or "").encode("utf-8")).digest())
    return h.hexdigest()


class FigureReadingService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def get_figure_reading_context(self, paper_id: UUID, figure_id: UUID) -> dict[str, Any]:
        """Fetch figure image, caption, referencing sections, and linked SI for reading."""
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise LookupError(f"Paper {paper_id} not found")

        fig = self.session.get(PaperFigure, figure_id)
        if not fig:
            raise LookupError(f"Figure {figure_id} not found")
        if fig.paper_id != paper_id:
            raise LookupError(f"Figure {figure_id} does not belong to paper {paper_id}")

        # Resolve image file and read raw bytes
        image_path = fig.image_path
        image_exists = False
        raw_bytes = None
        if image_path:
            resolved = resolve_persisted_artifact_path(
                image_path,
                category="figures",
                settings=self.settings,
            )
            if resolved and resolved.is_file():
                image_exists = True
                try:
                    raw_bytes = resolved.read_bytes()
                except Exception as exc:
                    logger.warning("Failed to read image bytes: %s", exc)

        # Referencing sections & content fingerprint
        referenced_sections = self._find_referencing_sections(paper_id, fig.figure_label)
        sections_content = [(s["section_id"], s.get("section_text", "")) for s in referenced_sections]
        image_fingerprint = compute_content_fingerprint(fig.caption, raw_bytes, sections_content)

        # Linked SI context
        si_context = self._get_supplementary_context(paper_id)

        # Existing reading explanation & dynamic staleness check
        existing_reading = getattr(fig, "reading_explanation", None)
        if isinstance(existing_reading, dict):
            existing_fp = existing_reading.get("source_fingerprint")
            existing_reading["is_stale"] = bool(existing_fp and existing_fp != image_fingerprint)

        return {
            "paper_id": str(paper_id),
            "paper_title": paper.title,
            "paper_doi": paper.doi,
            "figure_id": str(figure_id),
            "figure_label": fig.figure_label,
            "page": fig.page,
            "caption": fig.caption,
            "figure_role": fig.figure_role,
            "content_summary": fig.content_summary,
            "image_path": image_path,
            "image_exists": image_exists,
            "source_fingerprint": image_fingerprint,
            "referenced_sections": referenced_sections,
            "supplementary_context": si_context,
            "existing_reading": existing_reading,
        }

    def _find_referencing_sections(self, paper_id: UUID, figure_label: str | None) -> list[dict[str, Any]]:
        """Find sections in the main paper that explicitly cite this figure."""
        if not figure_label:
            return []

        m = re.search(r"(?:Figure|Fig\.?)\s*([S]?)(\d+)", figure_label, re.IGNORECASE)
        if not m:
            return []
        is_si = bool(m.group(1))
        target_num = m.group(2)

        sections = self.session.scalars(
            select(PaperSection)
            .where(PaperSection.paper_id == paper_id)
            .order_by(PaperSection.page_start.asc().nulls_last())
        ).all()

        block_pattern = re.compile(
            r'\b(?:Figures?|Figs?)\.?\s*([A-Za-z0-9\s,\-\(\)/–]+?)(?=[.;:]|\band\b\s+[A-Z]|\bwere\b|\bwas\b|\bis\b|\bare\b|\bdepicted\b|\bshown\b|\bexhibit\b|\bfor\b|\bwith\b|\bin\b|\bFigure|\bFig|\n|\Z)',
            re.IGNORECASE,
        )

        results = []
        for sec in sections:
            content = sec.text or ""
            snippets = []
            for block in block_pattern.finditer(content):
                block_text = block.group(0)
                tokens = re.findall(r'\b([Ss]?)(\d+)(?:[a-zA-Z]|\([a-zA-Z]\))?\b', block_text)
                matched = False
                for tok_si, tok_num in tokens:
                    if tok_num == target_num:
                        if is_si and tok_si.upper() == "S":
                            matched = True
                            break
                        elif not is_si and not tok_si:
                            matched = True
                            break
                if matched:
                    start = max(0, block.start() - 120)
                    end = min(len(content), block.end() + 200)
                    snippets.append("..." + content[start:end].replace("\n", " ").strip() + "...")

            if snippets:
                results.append({
                    "section_id": str(sec.id),
                    "section_title": sec.section_title or sec.section_type or "Section",
                    "section_type": sec.section_type,
                    "page_start": sec.page_start,
                    "page_end": sec.page_end,
                    "snippets": snippets[:3],
                    "content_length": len(content),
                    "section_text": content,
                })
        return results

    def _get_supplementary_context(self, paper_id: UUID) -> dict[str, Any]:
        """Fetch summary index of linked Supporting Information."""
        rels = self.session.scalars(
            select(PaperRelationship)
            .where(PaperRelationship.source_paper_id == paper_id)
            .where(PaperRelationship.relationship_type == "supplementary")
        ).all()

        if not rels:
            return {"has_si": False, "items": []}

        items = []
        for rel in rels:
            si_paper = self.session.get(Paper, rel.target_paper_id)
            if not si_paper:
                continue

            si_figs = self.session.scalars(
                select(PaperFigure)
                .where(PaperFigure.paper_id == si_paper.id)
                .order_by(PaperFigure.page.asc().nulls_last())
            ).all()

            si_tables = self.session.scalars(
                select(PaperTable).where(PaperTable.paper_id == si_paper.id)
            ).all()

            items.append({
                "si_paper_id": str(si_paper.id),
                "si_title": si_paper.title,
                "figures_count": len(si_figs),
                "tables_count": len(si_tables),
                "figures_index": [
                    {
                        "figure_id": str(f.id),
                        "label": f.figure_label,
                        "page": f.page,
                        "caption_preview": (f.caption or "")[:80],
                    }
                    for f in si_figs
                ],
            })
        return {"has_si": True, "items": items}

    def validate_figure_reading(self, paper_id: UUID, figure_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate figure reading without writing to database.

        Strict structural and locator validation:
        1. Target figure exists and belongs to requested paper_id.
        2. Non-empty summary and detailed_explanation.
        3. Non-empty evidence_locators list, rejecting empty objects `{}`.
        4. Explicit validation of paper_id and section_id existence and ownership.
        5. Disambiguation of section titles (ambiguous names rejected).
        6. Page numbers strictly verified against matched section and PDF page bounds.
        7. Verbatim quote resolution with normalization; non-matching quotes rejected as unresolved.
        8. Clear distinction between caption evidence and body text evidence.
        9. Returns structural_validation_only=True.
        """
        errors: list[str] = []
        warnings: list[str] = []
        validated_locators: list[dict[str, Any]] = []

        fig = self.session.get(PaperFigure, figure_id)
        if not fig:
            errors.append(f"Figure {figure_id} not found in database")
            return {"valid": False, "errors": errors, "warnings": warnings, "structural_validation_only": True}

        if fig.paper_id != paper_id:
            errors.append(f"Figure {figure_id} belongs to paper {fig.paper_id}, not requested paper {paper_id}")
            return {"valid": False, "errors": errors, "warnings": warnings, "structural_validation_only": True}

        paper = self.session.get(Paper, paper_id)
        if not paper:
            errors.append(f"Paper {paper_id} not found in database")
            return {"valid": False, "errors": errors, "warnings": warnings, "structural_validation_only": True}

        # Determine true PDF page count from quality report or document sections
        true_max_page = None
        if isinstance(paper.pdf_quality_report, dict):
            true_max_page = paper.pdf_quality_report.get("metrics", {}).get("page_count")

        sections = self.session.scalars(
            select(PaperSection).where(PaperSection.paper_id == paper_id)
        ).all()
        sec_by_id = {s.id: s for s in sections}

        if not true_max_page:
            all_figs = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).all()
            true_max_page = max(
                [s.page_end or s.page_start or 1 for s in sections] + [f.page or 1 for f in all_figs],
                default=1,
            )

        summary = str(payload.get("summary") or "").strip()
        if not summary:
            errors.append("Missing 'summary' (concise one-line synthesis of figure meaning)")
        elif len(summary) > 400:
            warnings.append("Summary is quite long (>400 chars); consider keeping it concise.")

        detailed = str(payload.get("detailed_explanation") or payload.get("core_interpretation") or "").strip()
        if not detailed:
            errors.append("Missing 'detailed_explanation' (core evidence-backed scientific interpretation)")

        subfigures = payload.get("subfigures")
        if subfigures is not None:
            if not isinstance(subfigures, list):
                errors.append("'subfigures' must be a list of objects")
            else:
                for idx, sub in enumerate(subfigures):
                    if not isinstance(sub, dict) or not sub.get("label"):
                        errors.append(f"subfigures[{idx}] must be a dict with at least a 'label' (e.g. '(a)')")

        # Associated SI paper IDs for relationship boundary check
        si_rels = self.session.scalars(
            select(PaperRelationship)
            .where(PaperRelationship.source_paper_id == paper_id)
            .where(PaperRelationship.relationship_type == "supplementary")
        ).all()
        allowed_paper_ids = {paper_id} | {r.target_paper_id for r in si_rels}

        locators = payload.get("evidence_locators")
        if locators is None or not isinstance(locators, list) or len(locators) == 0:
            errors.append("'evidence_locators' must be a non-empty list of locator objects")
        else:
            has_body_text_locator = False

            for idx, loc in enumerate(locators):
                if not isinstance(loc, dict) or not loc:
                    errors.append(f"evidence_locators[{idx}] is empty or not an object; empty locators are prohibited")
                    continue

                # Check if locator has any substantial identifying fields
                if not loc.get("section_id") and not loc.get("section") and not loc.get("quote"):
                    errors.append(f"evidence_locators[{idx}] lacks mandatory identification fields (section_id, section, or quote)")
                    continue

                # 1. Validate paper_id attribution if provided
                raw_loc_paper = loc.get("paper_id")
                if raw_loc_paper:
                    try:
                        loc_pid = UUID(str(raw_loc_paper))
                        if loc_pid not in allowed_paper_ids:
                            errors.append(
                                f"evidence_locators[{idx}].paper_id '{raw_loc_paper}' does not match paper {paper_id} or its associated supplementary documents"
                            )
                    except (ValueError, TypeError):
                        errors.append(f"evidence_locators[{idx}].paper_id is not a valid UUID")

                # 2. Section resolution (via section_id or unique section title)
                target_sec: PaperSection | None = None
                raw_sec_id = loc.get("section_id")

                if raw_sec_id:
                    try:
                        sid = UUID(str(raw_sec_id))
                        target_sec = sec_by_id.get(sid)
                        if not target_sec:
                            errors.append(
                                f"evidence_locators[{idx}].section_id '{raw_sec_id}' does not exist or does not belong to paper {paper_id}"
                            )
                    except (ValueError, TypeError):
                        errors.append(f"evidence_locators[{idx}].section_id is not a valid UUID")

                # If no section_id or not found, try matching by section name
                raw_sec_name = str(loc.get("section") or "").strip()
                if raw_sec_name:
                    norm_query = raw_sec_name.lower()
                    # Find all candidate sections
                    matched_secs = []
                    for s in sections:
                        title_norm = (s.section_title or "").strip().lower()
                        type_norm = (s.section_type or "").strip().lower()
                        if norm_query == title_norm or norm_query == type_norm:
                            matched_secs.append(s)
                        elif norm_query in title_norm or norm_query in type_norm:
                            matched_secs.append(s)

                    if len(matched_secs) == 0 and not target_sec:
                        errors.append(
                            f"evidence_locators[{idx}].section '{raw_sec_name}' does not match any section in paper {paper_id}"
                        )
                    elif len(matched_secs) > 1 and not target_sec:
                        # Ambiguous match: multiple sections match query; do NOT pick arbitrarily
                        candidate_titles = [s.section_title or s.section_type for s in matched_secs[:3]]
                        errors.append(
                            f"evidence_locators[{idx}].section '{raw_sec_name}' is ambiguous (matches {len(matched_secs)} sections: {candidate_titles}). Please provide an explicit 'section_id'."
                        )
                    elif len(matched_secs) == 1:
                        if target_sec and target_sec.id != matched_secs[0].id:
                            warnings.append(
                                f"evidence_locators[{idx}]: section_id specifies '{target_sec.section_title}' while section name matches '{matched_secs[0].section_title}'; using section_id"
                            )
                        elif not target_sec:
                            target_sec = matched_secs[0]

                # 3. Page validation against PDF bounds and section page range
                loc_page = loc.get("page")
                if loc_page is not None:
                    try:
                        p = int(loc_page)
                        if p < 1 or p > true_max_page:
                            errors.append(
                                f"evidence_locators[{idx}].page ({p}) exceeds true PDF page bounds (1 to {true_max_page})"
                            )
                        elif target_sec and target_sec.page_start is not None:
                            sec_p_start = target_sec.page_start
                            sec_p_end = target_sec.page_end or target_sec.page_start
                            if p < sec_p_start or p > sec_p_end:
                                errors.append(
                                    f"evidence_locators[{idx}].page ({p}) is outside target section '{target_sec.section_title or target_sec.section_type}' page range ({sec_p_start}-{sec_p_end})"
                                )
                    except (ValueError, TypeError):
                        errors.append(f"evidence_locators[{idx}].page must be an integer")

                # 4. Verbatim Quote verification (normalized)
                quote = str(loc.get("quote") or "").strip()
                if not quote:
                    errors.append(f"evidence_locators[{idx}] missing quote; evidence locators must provide verifiable citation quotes")
                else:
                    norm_quote = normalize_text_for_matching(quote)
                    search_corpus = ""
                    if target_sec and target_sec.text:
                        search_corpus = normalize_text_for_matching(target_sec.text)
                    else:
                        search_corpus = normalize_text_for_matching(" ".join([s.text or "" for s in sections]))

                    if norm_quote not in search_corpus:
                        errors.append(
                            f"evidence_locators[{idx}].quote '{quote[:40]}...' cannot be resolved in section text (unresolved)"
                        )

                # 5. Distinguish caption evidence vs body text evidence
                is_caption = False
                if target_sec:
                    sec_type = (target_sec.section_type or "").lower()
                    sec_title = (target_sec.section_title or "").lower()
                    if "caption" in sec_type or "caption" in sec_title:
                        is_caption = True
                    elif sec_title.startswith(("figure", "fig.", "table", "chart")):
                        is_caption = True

                if not is_caption and target_sec is not None:
                    has_body_text_locator = True

                validated_loc = dict(loc)
                if target_sec:
                    validated_loc["section_id"] = str(target_sec.id)
                    validated_loc["section"] = target_sec.section_title or target_sec.section_type
                    validated_loc["evidence_type"] = "caption" if is_caption else "body_text"
                validated_locators.append(validated_loc)

            # Check that reading is supported by main body text, not solely figure captions
            if not has_body_text_locator and not any("cannot be resolved" in e for e in errors):
                errors.append(
                    "Figure reading evidence must contain at least one locator from main body text (cannot be solely figure/table captions)"
                )

        return {
            "valid": len(errors) == 0,
            "figure_id": str(figure_id),
            "paper_id": str(paper_id),
            "errors": errors,
            "warnings": warnings,
            "validated_locators": validated_locators if len(errors) == 0 else [],
            "structural_validation_only": True,
        }

    def apply_figure_reading(
        self,
        paper_id: UUID,
        figure_id: UUID,
        payload: dict[str, Any],
        model_name: str = "ai_reading_v1",
    ) -> dict[str, Any]:
        """Apply validated figure reading to database.

        CRITICAL REVIEW BOUNDARY GATE:
        - Must NOT update fig.review_status!
        - Must NOT record scientific review audit logs (verified/rejected)!
        - Must NOT alter DFT candidate gates or verification states!
        - Idempotent: identical payload will not bump version.
        - Source change refresh: if source fingerprint changed, updates source fingerprint without blocking.
        """
        val_result = self.validate_figure_reading(paper_id, figure_id, payload)
        if not val_result["valid"]:
            raise ValueError("Validation failed: " + "; ".join(val_result["errors"]))

        fig = self.session.get(PaperFigure, figure_id)
        if not fig:
            raise LookupError(f"Figure {figure_id} not found")

        # Compute live source fingerprint (including actual text contents of referenced sections)
        raw_bytes = None
        if fig.image_path:
            resolved = resolve_persisted_artifact_path(
                fig.image_path,
                category="figures",
                settings=self.settings,
            )
            if resolved and resolved.is_file():
                try:
                    raw_bytes = resolved.read_bytes()
                except Exception as exc:
                    logger.warning("Could not read image for fingerprint: %s", exc)

        ref_sections = self._find_referencing_sections(paper_id, fig.figure_label)
        sections_content = [(s["section_id"], s.get("section_text", "")) for s in ref_sections]
        live_fingerprint = compute_content_fingerprint(fig.caption, raw_bytes, sections_content)

        # Source conflict detection (distinguished from concurrent reading version conflicts)
        expected_fp = payload.get("expected_source_fingerprint")
        if expected_fp and expected_fp != live_fingerprint:
            raise ValueError(
                f"Source content conflict: expected source fingerprint {expected_fp} does not match current {live_fingerprint}. Source text or figure may have changed."
            )

        new_summary = str(payload.get("summary") or "").strip()
        new_detailed = str(payload.get("detailed_explanation") or payload.get("core_interpretation") or "").strip()
        new_subfigures = payload.get("subfigures") or []
        # Use canonical validated locators with bound section_ids and evidence_type
        new_locators = val_result.get("validated_locators") or payload.get("evidence_locators") or []
        new_uncertainties = payload.get("uncertainties") or []
        new_methods = payload.get("methods") or []

        existing = getattr(fig, "reading_explanation", None)
        if isinstance(existing, dict):
            locators_unchanged = (existing.get("evidence_locators") == new_locators)
            content_unchanged = (
                existing.get("summary") == new_summary
                and existing.get("detailed_explanation") == new_detailed
                and existing.get("subfigures") == new_subfigures
                and existing.get("uncertainties") == new_uncertainties
                and existing.get("methods") == new_methods
                and locators_unchanged
            )
            source_changed = bool(existing.get("source_fingerprint") != live_fingerprint)

            if content_unchanged:
                if source_changed:
                    # Source changed, but interpretation was re-evaluated and confirmed.
                    # Update source fingerprint and reset is_stale without incrementing version.
                    updated = dict(existing)
                    updated["source_fingerprint"] = live_fingerprint
                    updated["is_stale"] = False
                    updated["evidence_locators"] = new_locators
                    updated["updated_at"] = utcnow().isoformat()
                    fig.reading_explanation = updated
                    flag_modified(fig, "reading_explanation")
                    self.session.commit()
                    logger.info("Figure %s reading source refreshed (v%s, stale cleared)", figure_id, updated.get("version", 1))
                    return {
                        "figure_id": str(figure_id),
                        "paper_id": str(paper_id),
                        "status": "source_refreshed",
                        "version": updated.get("version", 1),
                        "reading": updated,
                    }
                else:
                    logger.info("Figure %s reading is unchanged; skipping update", figure_id)
                    return {
                        "figure_id": str(figure_id),
                        "paper_id": str(paper_id),
                        "status": "unchanged",
                        "version": existing.get("version", 1),
                        "reading": existing,
                    }

            current_version = int(existing.get("version", 0)) + 1
        else:
            current_version = 1

        clean_reading = {
            "version": current_version,
            "summary": new_summary,
            "detailed_explanation": new_detailed,
            "subfigures": new_subfigures,
            "evidence_locators": new_locators,
            "uncertainties": new_uncertainties,
            "methods": new_methods,
            "source_fingerprint": live_fingerprint,
            "is_stale": False,
            "model": model_name,
            "updated_at": utcnow().isoformat(),
        }

        fig.reading_explanation = clean_reading
        flag_modified(fig, "reading_explanation")
        self.session.commit()

        logger.info(
            "Applied figure reading to %s (paper %s, v%s)",
            figure_id,
            paper_id,
            current_version,
        )

        return {
            "figure_id": str(figure_id),
            "paper_id": str(paper_id),
            "status": "applied",
            "version": current_version,
            "reading": clean_reading,
        }

    def get_paper_reading_guide_data(self, paper_id: UUID) -> dict[str, Any]:
        """Aggregate full paper reading guide data: metadata, logical thread, figures, SI, and DFT."""
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise LookupError(f"Paper {paper_id} not found")

        figures = self.session.scalars(
            select(PaperFigure)
            .where(PaperFigure.paper_id == paper_id)
            .order_by(PaperFigure.page.asc().nulls_last(), PaperFigure.id.asc())
        ).all()

        figure_items = []
        for f in figures:
            reading = getattr(f, "reading_explanation", None)
            figure_items.append({
                "figure_id": str(f.id),
                "figure_label": f.figure_label,
                "page": f.page,
                "caption": f.caption,
                "figure_role": f.figure_role,
                "content_summary": f.content_summary,
                "reading": reading,
                "has_reading": isinstance(reading, dict) and bool(reading.get("detailed_explanation")),
                "image_path": f.image_path,
            })

        si_context = self._get_supplementary_context(paper_id)

        # Real distribution of DFT candidate_status
        status_rows = self.session.execute(
            select(DFTResult.candidate_status, func.count(DFTResult.id))
            .where(DFTResult.paper_id == paper_id)
            .group_by(DFTResult.candidate_status)
        ).all()
        breakdown = {str(st or "unknown"): cnt for st, cnt in status_rows}
        dft_count = sum(breakdown.values())

        catalysts = self.session.scalars(
            select(CatalystSample.name)
            .where(CatalystSample.paper_id == paper_id)
            .distinct()
        ).all()

        dft_summary = {
            "total_records": dft_count,
            "breakdown": breakdown,
            "system_candidate": breakdown.get("system_candidate", 0),
            "ai_verified_ml_ready": breakdown.get("ai_verified_ml_ready", 0),
            "rejected": breakdown.get("Rejected", breakdown.get("rejected", 0)),
            "catalysts": [c for c in catalysts if c],
            "has_dft_data": dft_count > 0,
        }

        saved_guide = getattr(paper, "reading_guide", None)
        localized = paper.comprehensive_analysis if isinstance(paper.comprehensive_analysis, dict) else {}

        return {
            "paper_id": str(paper_id),
            "title": paper.title,
            "doi": paper.doi,
            "journal": paper.journal,
            "year": paper.year,
            "authors": paper.authors,
            "abstract": paper.abstract,
            "abstract_zh": localized.get("abstract_zh") if isinstance(localized.get("abstract_zh"), str) else None,
            "figures": figure_items,
            "supplementary": si_context,
            "dft_summary": dft_summary,
            "saved_guide": saved_guide,
        }

    def save_paper_reading_guide(self, paper_id: UUID, guide_payload: dict[str, Any]) -> dict[str, Any]:
        """Save whole-paper reading guide structure to database."""
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise LookupError(f"Paper {paper_id} not found")

        version = 1
        existing = getattr(paper, "reading_guide", None)
        if isinstance(existing, dict) and "version" in existing:
            version = int(existing["version"]) + 1

        clean_guide = {
            "version": version,
            "research_background": guide_payload.get("research_background", ""),
            "research_motivation": guide_payload.get("research_motivation", ""),
            "design_strategy": guide_payload.get("design_strategy", ""),
            "figures_synthesis": guide_payload.get("figures_synthesis", []),
            "key_si_evidence": guide_payload.get("key_si_evidence", []),
            "dft_mechanism_insights": guide_payload.get("dft_mechanism_insights", ""),
            "scientific_conclusions": guide_payload.get("scientific_conclusions", ""),
            "limitations_and_uncertainties": guide_payload.get("limitations_and_uncertainties", []),
            "updated_at": utcnow().isoformat(),
        }

        paper.reading_guide = clean_guide
        self.session.commit()
        return {"paper_id": str(paper_id), "status": "saved", "guide": clean_guide}

    def render_offline_html(self, paper_id: UUID) -> str:
        """Render a completely self-contained single-file HTML reading guide."""
        data = self.get_paper_reading_guide_data(paper_id)
        paper_title = html.escape(data.get("title") or "文献导读")
        paper_doi = html.escape(data.get("doi") or "")
        paper_journal = html.escape(data.get("journal") or "")
        paper_year = html.escape(str(data.get("year") or ""))
        raw_authors = data.get("authors") or []
        if isinstance(raw_authors, list):
            author_names = [
                (item.get("name") if isinstance(item, dict) else str(item))
                for item in raw_authors
            ]
            paper_authors = html.escape(", ".join(name for name in author_names if name))
        else:
            paper_authors = html.escape(str(raw_authors))
        paper_abstract = html.escape(data.get("abstract_zh") or data.get("abstract") or "暂无摘要")

        # Whole-paper guide overview block
        guide_overview_html = ""
        sg = data.get("saved_guide") or {}
        if isinstance(sg, dict) and any(sg.get(k) for k in [
            "research_background", "research_motivation", "design_strategy",
            "figures_synthesis", "scientific_conclusions", "dft_mechanism_insights"
        ]):
            bg = html.escape(sg.get("research_background") or "").replace("\n", "<br>")
            mot = html.escape(sg.get("research_motivation") or "").replace("\n", "<br>")
            strat = html.escape(sg.get("design_strategy") or "").replace("\n", "<br>")
            concl = html.escape(sg.get("scientific_conclusions") or "").replace("\n", "<br>")
            dft_insight = html.escape(sg.get("dft_mechanism_insights") or "").replace("\n", "<br>")

            synth_items = []
            for item in sg.get("figures_synthesis") or []:
                if isinstance(item, dict):
                    fig_label = item.get("figure", "")
                    takeaway = item.get("key_takeaway", "")
                    synth_items.append(f"<li><strong>{html.escape(fig_label)}:</strong> {html.escape(takeaway)}</li>")
                else:
                    synth_items.append(f"<li>{html.escape(str(item))}</li>")

            limit_items = []
            for item in sg.get("limitations_and_uncertainties") or []:
                limit_items.append(f"<li>{html.escape(str(item))}</li>")

            blocks = []
            if bg or mot:
                mot_html = f'<p style="margin-top:6px;"><strong>科学动机：</strong>{mot}</p>' if mot else ""
                blocks.append(f"<div class='guide-subblock'><h4>研究背景与科学动机</h4><p>{bg}</p>{mot_html}</div>")
            if strat:
                blocks.append(f"<div class='guide-subblock'><h4>核心设计策略与催化体系</h4><p>{strat}</p></div>")
            if synth_items:
                synth_str = "".join(synth_items)
                blocks.append(f"<div class='guide-subblock'><h4>图表论证逻辑综合</h4><ul>{synth_str}</ul></div>")
            if dft_insight or concl:
                dft_html = f"<p>{dft_insight}</p>" if dft_insight else ""
                concl_html = f'<p style="margin-top:6px;"><strong>核心结论：</strong>{concl}</p>' if concl else ""
                blocks.append(f"<div class='guide-subblock'><h4>计算机理洞察与科学结论</h4>{dft_html}{concl_html}</div>")
            if limit_items:
                limit_str = "".join(limit_items)
                blocks.append(f"<div class='guide-subblock'><h4>局限性与不确定性分析</h4><ul>{limit_str}</ul></div>")

            if blocks:
                blocks_rendered = "".join(blocks)
                guide_overview_html = f"""
                <section class="card guide-overview-card" id="guide-overview">
                  <h2>全文论证导读与科学脉络综合</h2>
                  {blocks_rendered}
                </section>
                """

        figures_html = []
        for fig in data["figures"]:
            label = html.escape(fig.get("figure_label") or "Figure")
            caption = html.escape(fig.get("caption") or "")
            page = fig.get("page")
            reading = fig.get("reading") or {}

            img_b64 = None
            if fig.get("image_path"):
                resolved = resolve_persisted_artifact_path(
                    fig["image_path"],
                    category="figures",
                    settings=self.settings,
                )
                if resolved and resolved.is_file():
                    try:
                        raw = resolved.read_bytes()
                        mime = mimetypes.guess_type(resolved.name)[0] or "image/png"
                        img_b64 = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                    except Exception as exc:
                        logger.warning("Failed to base64-encode image: %s", exc)

            summary = html.escape(reading.get("summary") or fig.get("content_summary") or "尚未生成解读")
            detailed = html.escape(reading.get("detailed_explanation") or "").replace("\n", "<br>")

            subfigs_html = ""
            subfigs = reading.get("subfigures") or []
            if subfigs:
                sub_items_list = []
                for s in subfigs:
                    label_str = html.escape(s.get("label", ""))
                    desc_str = html.escape(s.get("description", ""))
                    m_tag = f"<span class='tag'>方法: {html.escape(s.get('methods', ''))}</span>" if s.get("methods") else ""
                    f_tag = f"<span class='finding'>{html.escape(s.get('findings', ''))}</span>" if s.get("findings") else ""
                    sub_items_list.append(
                        f"<div class='subfig-item'><strong>{label_str}</strong> {desc_str} {m_tag} {f_tag}</div>"
                    )
                subfigs_str = "".join(sub_items_list)
                subfigs_html = f"<div class='subfigs-box'><h4>子图说明</h4>{subfigs_str}</div>"

            locators_html = ""
            locators = reading.get("evidence_locators") or []
            if locators:
                loc_chips = []
                for l in locators:
                    q = html.escape(l.get("quote", ""))
                    sec = html.escape(l.get("section", ""))
                    p_num = l.get("page", "-")
                    loc_chips.append(f"<span class='chip' title='{q}'>P.{p_num} · {sec}</span>")
                chips_str = "".join(loc_chips)
                locators_html = f"<div class='locators-box'><h4>证据定位</h4><div class='chip-row'>{chips_str}</div></div>"

            # Check whether figure has deep evidence-backed reading
            has_deep_reading = (
                isinstance(reading, dict)
                and bool(reading.get("detailed_explanation"))
                and bool(reading.get("evidence_locators"))
            )
            is_stale = bool(reading.get("is_stale")) if isinstance(reading, dict) else False

            badge_html = '<span class="badge ok">AI 深度科学解读</span>' if has_deep_reading else '<span class="badge muted">基础图注概括</span>'
            stale_badge_html = '<span class="badge warn">来源变动可能过期</span>' if is_stale else ''

            image_element = f'<img src="{img_b64}" alt="{label}">' if img_b64 else '<div class="no-img">图片未内嵌或暂不可用</div>'
            body_element = f'<div class="reading-body">{detailed}</div>' if detailed else ''
            caption_element = f'<details class="caption-details"><summary>原文图注</summary><p>{caption}</p></details>' if caption else ''

            figures_html.append(f"""
            <article class="figure-block" id="fig-{fig['figure_id']}">
              <div class="figure-head">
                <h3>{label}</h3>
                <span class="page-badge">Page {page or '-'}</span>
              </div>
              <div class="figure-media">
                {image_element}
              </div>
              <div class="reading-card">
                <div class="reading-tag-row">
                  {badge_html}
                  {stale_badge_html}
                </div>
                <div class="reading-summary">{summary}</div>
                {body_element}
                {subfigs_html}
                {locators_html}
              </div>
              {caption_element}
            </article>
            """)

        si_summary_html = ""
        si_data = data.get("supplementary", {})
        if si_data.get("has_si"):
            si_items = []
            for item in si_data.get("items", []):
                figs_cnt = item.get("figures_count", 0)
                tbls_cnt = item.get("tables_count", 0)
                si_items.append(
                    f"<li><strong>关联补充材料:</strong> 收录 {figs_cnt} 张补充图 · {tbls_cnt} 个补充表</li>"
                )
            si_items_str = "".join(si_items)
            si_summary_html = f"""
            <div class='card si-card'>
              <h3>关联补充材料 (Supporting Information)</h3>
              <ul>{si_items_str}</ul>
              <div class="si-disclaimer">注：按本次极简处理规范，SI 图表维持原始图注与短概括索引，离线导读不嵌入 SI 图片与深度阅读解读。</div>
            </div>
            """

        dft_section_html = ""
        dft_info = data.get("dft_summary", {})
        if dft_info.get("has_dft_data"):
            cats = ", ".join([html.escape(c) for c in dft_info.get("catalysts", [])])
            tot_cnt = dft_info.get("total_records", 0)
            sc_cnt = dft_info.get("system_candidate", 0)
            vr_cnt = dft_info.get("ai_verified_ml_ready", 0)
            rej_cnt = dft_info.get("rejected", 0)

            dft_section_html = f"""
            <section class="card dft-card" id="dft-section">
              <h2>已收录 DFT 计算数据与状态分布</h2>
              <div class="dft-stat-grid">
                <div class="dft-stat-item"><span class="num">{tot_cnt}</span><span class="lbl">收录总数</span></div>
                <div class="dft-stat-item"><span class="num">{sc_cnt}</span><span class="lbl">初筛待复核 (system_candidate)</span></div>
                <div class="dft-stat-item"><span class="num">{vr_cnt}</span><span class="lbl">已核验候选 (ai_verified_ml_ready)</span></div>
                <div class="dft-stat-item"><span class="num">{rej_cnt}</span><span class="lbl">已拒绝 (Rejected)</span></div>
              </div>
              <p style="margin-top:12px;"><strong>覆盖催化剂构型:</strong> {cats or '各类设计模型与对照体系'}</p>
              <div class="note">注：本节仅展示当前数据库中已收录的 DFT 计算记录及其处理状态分布。数据尚未完成最终批量人工科研裁决、去重与标准化导出。</div>
            </section>
            """

        figures_str = "".join(figures_html)
        export_time_str = utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{paper_title} - 文献导读 (离线版)</title>
<style>
:root {{
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-alt: #f1f5f9;
  --text: #0f172a;
  --text-muted: #64748b;
  --primary: #0284c7;
  --primary-dark: #0369a1;
  --border: #e2e8f0;
  --radius: 8px;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.65;
  padding: 24px;
}}
.container {{
  max-width: 1080px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 24px;
}}
header.guide-header {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 28px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}}
.guide-badge {{
  display: inline-block;
  background: #e0f2fe;
  color: #0369a1;
  font-size: 12px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 9999px;
  margin-bottom: 12px;
}}
h1.paper-title {{
  font-size: 24px;
  line-height: 1.4;
  margin-bottom: 10px;
}}
.meta-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 13.5px;
  color: var(--text-muted);
  margin-bottom: 18px;
}}
.abstract-box {{
  background: var(--surface-alt);
  border-radius: var(--radius);
  padding: 16px;
  font-size: 13.5px;
  border-left: 4px solid var(--primary);
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}}
h2 {{ font-size: 19px; margin-bottom: 14px; color: var(--primary-dark); }}
h3 {{ font-size: 16px; margin-bottom: 6px; }}
.guide-subblock {{
  margin-bottom: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}}
.guide-subblock:last-child {{
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}}
.guide-subblock h4 {{
  font-size: 14px;
  color: var(--primary-dark);
  margin-bottom: 6px;
}}
.guide-subblock p {{
  font-size: 13.5px;
  color: var(--text);
  line-height: 1.7;
}}
.guide-subblock ul {{
  margin-left: 20px;
  margin-top: 4px;
  font-size: 13.5px;
  color: var(--text);
  line-height: 1.7;
}}
.figure-block {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}}
.figure-head {{
  padding: 14px 20px;
  background: var(--surface-alt);
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.page-badge {{
  font-size: 12px;
  background: #fff;
  border: 1px solid var(--border);
  padding: 2px 8px;
  border-radius: 4px;
  color: var(--text-muted);
}}
.figure-media {{
  padding: 20px;
  text-align: center;
  background: #fdfdfd;
  border-bottom: 1px solid var(--border);
}}
.figure-media img {{
  max-width: 100%;
  max-height: 540px;
  object-fit: contain;
  border-radius: 4px;
}}
.no-img {{
  padding: 40px;
  color: var(--text-muted);
  font-size: 13px;
}}
.reading-card {{
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}}
.reading-tag-row {{
  display: flex;
  align-items: center;
  gap: 6px;
}}
.badge.ok {{
  background: #dcfce7;
  color: #15803d;
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
}}
.badge.warn {{
  background: #fef3c7;
  color: #b45309;
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
}}
.badge.muted {{
  background: #f1f5f9;
  color: #64748b;
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 500;
}}
.reading-summary {{
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.5;
}}
.reading-body {{
  font-size: 14px;
  color: var(--text);
  line-height: 1.7;
}}
.subfigs-box {{
  background: var(--surface-alt);
  padding: 12px 14px;
  border-radius: var(--radius);
  margin-top: 6px;
}}
.subfigs-box h4 {{
  font-size: 12.5px;
  margin-bottom: 8px;
  color: var(--primary-dark);
}}
.subfig-item {{
  font-size: 13px;
  margin-bottom: 6px;
  padding: 6px 8px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 4px;
}}
.subfig-item strong {{ color: var(--primary); }}
.subfig-item .tag {{ font-size: 11px; background: #f1f5f9; padding: 2px 6px; border-radius: 3px; margin-left: 6px; }}
.subfig-item .finding {{ font-size: 11.5px; color: var(--primary-dark); display: block; margin-top: 3px; }}
.locators-box {{
  margin-top: 4px;
}}
.locators-box h4 {{
  font-size: 12px;
  margin-bottom: 4px;
  color: var(--text-muted);
}}
.chip-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}}
.chip {{
  font-size: 11px;
  background: #e0f2fe;
  color: #0369a1;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid #bae6fd;
}}
.caption-details {{
  padding: 12px 20px;
  border-top: 1px dashed var(--border);
  font-size: 13px;
  color: var(--text-muted);
}}
.caption-details summary {{
  cursor: pointer;
  font-weight: 600;
  color: var(--text-muted);
}}
.caption-details p {{
  margin-top: 8px;
  line-height: 1.6;
}}
.si-card ul {{ padding-left: 20px; margin-top: 8px; font-size: 13.5px; }}
.si-disclaimer {{
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 10px;
  padding: 8px 12px;
  background: var(--surface-alt);
  border-radius: 4px;
}}
.dft-card {{ border-left: 4px solid #10b981; }}
.dft-stat-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-top: 12px;
}}
.dft-stat-item {{
  background: var(--surface-alt);
  padding: 12px;
  border-radius: var(--radius);
  text-align: center;
}}
.dft-stat-item .num {{
  display: block;
  font-size: 22px;
  font-weight: 700;
  color: var(--primary-dark);
}}
.dft-stat-item .lbl {{
  font-size: 11.5px;
  color: var(--text-muted);
}}
.dft-card .note {{ font-size: 12px; color: var(--text-muted); margin-top: 12px; }}
footer {{
  text-align: center;
  font-size: 12px;
  color: var(--text-muted);
  padding: 24px 0;
}}
</style>
</head>
<body>
<div class="container">
  <header class="guide-header">
    <span class="guide-badge">Literature AI · 文献精读与科学导读</span>
    <h1 class="paper-title">{paper_title}</h1>
    <div class="meta-row">
      <span>作者: {paper_authors or '未知'}</span>
      <span>·</span>
      <span>期刊: {paper_journal or '未知'}</span>
      <span>·</span>
      <span>年份: {paper_year or '-'}</span>
      <span>·</span>
      <span>DOI: {paper_doi or '-'}</span>
    </div>
    <div class="abstract-box">
      <strong>论文摘要：</strong><br>{paper_abstract}
    </div>
  </header>

  {guide_overview_html}

  <section class="figures-container" style="display:flex; flex-direction:column; gap:24px;">
    <h2>正文关键图片深度解读与论证脉络</h2>
    {figures_str}
  </section>

  {si_summary_html}
  {dft_section_html}

  <footer>
    本导读由 Literature AI 系统基于论文正文切图、原文图注、引用上下文及已核验科学数据生成。<br>
    导出时间: {export_time_str} · 单文件离线自包含版本
  </footer>
</div>
</body>
</html>"""


    def export_offline_html(self, paper_id: UUID) -> dict[str, Any]:
        """Atomically persist the self-contained guide on the server and return traceable metadata."""
        html_content = self.render_offline_html(paper_id)
        output_dir = self.settings.storage_paths["root"] / "exports" / "reading_guides" / str(paper_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "reading_guide.html"
        temporary = output_dir / ".reading_guide.html.tmp"
        temporary.write_text(html_content, encoding="utf-8")
        temporary.replace(target)
        raw = target.read_bytes()
        runtime_path = str(target)
        host_path = runtime_path
        if runtime_path == "/data" or runtime_path.startswith("/data/"):
            host_path = "/opt/literature-ai" + runtime_path
        return {
            "paper_id": str(paper_id),
            "status": "exported",
            "server_path": host_path,
            "runtime_path": runtime_path,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "self_contained": True,
            "supplementary_images_embedded": False,
        }
