from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceLocator,
    EvidenceSpan,
    MechanismClaim,
    PaperFigure,
    PaperSection,
    PaperTable,
)
from app.schemas.evidence import EvidenceBBox, EvidenceLocatorResponse, EvidenceRef, PageSpan
from app.utils.locator_degradation import locator_degradation


TARGET_MODEL_MAP = {
    "catalyst_samples": CatalystSample,
    "dft_settings": DFTSetting,
    "dft_results": DFTResult,
    "mechanism_claims": MechanismClaim,
    "electrochemical_performance": ElectrochemicalPerformance,
}

OBJECT_TYPE_ALIASES = {
    "dft_setting": "dft_settings",
    "dft_result": "dft_results",
    "mechanism_claim": "mechanism_claims",
    "catalyst_sample": "catalyst_samples",
    "electrochemical_performance": "electrochemical_performance",
    "figure_data": "figure_data",
    "writing_card": "writing_card",
}

LOCATOR_WARNING_CODES = {
    "missing_locator": "evidence_locator_missing",
    "missing_page": "evidence_locator_missing_page",
    "text_only": "evidence_locator_text_only",
    "approximate": "evidence_locator_approximate",
    "unresolved": "evidence_locator_unresolved",
}


class EvidenceLocatorService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_locator_for_claim(self, claim: EvidenceClaim, evidence: EvidenceRef) -> EvidenceLocator:
        return self.upsert_locator(
            paper_id=claim.paper_id or evidence.paper_id,
            claim_id=claim.id,
            chunk_id=evidence.chunk_id,
            source_type=self._normalize_source_type(evidence.source),
            page=evidence.page_span.page_start,
            bbox=evidence.bbox.model_dump(mode="json") if evidence.bbox else None,
            section=evidence.section_title,
            target_type=evidence.target_type or claim.target_type,
            target_id=evidence.target_id or claim.target_id,
            field_name=None,
            evidence_text=evidence.evidence_text,
            char_start=evidence.page_span.span_start,
            char_end=evidence.page_span.span_end,
            parser_source=evidence.parser_source,
        )

    def create_locator_for_span(
        self,
        *,
        paper_id: UUID,
        object_type: str,
        object_id: str,
        evidence_text: str,
        page: int | None,
        section: str | None,
        figure: str | None,
        table: str | None,
        confidence: float | None,
        bbox: dict[str, Any] | None = None,
        parser_source: str = "unknown",
        field_name: str | None = None,
    ) -> EvidenceLocator:
        source_type = self._normalize_source_type(object_type)
        canonical_target_type = self._canonical_target_type(object_type)
        figure_id = self._resolve_figure_id(paper_id, figure)
        table_id = self._resolve_table_id(paper_id, table)
        locator = self.upsert_locator(
            paper_id=paper_id,
            claim_id=None,
            chunk_id=object_id,
            source_type=source_type,
            page=page,
            bbox=bbox,
            section=section,
            figure_id=figure_id,
            table_id=table_id,
            target_type=canonical_target_type,
            target_id=object_id,
            field_name=field_name,
            evidence_text=evidence_text,
            char_start=None,
            char_end=None,
            parser_source=parser_source,
            locator_confidence=confidence,
        )
        return locator

    def upsert_locator(
        self,
        *,
        paper_id: UUID | None,
        claim_id: UUID | None,
        chunk_id: str | None,
        source_type: str,
        page: int | None,
        bbox: dict[str, Any] | None,
        section: str | None,
        evidence_text: str,
        parser_source: str = "unknown",
        figure_id: UUID | None = None,
        table_id: UUID | None = None,
        equation_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        field_name: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
        locator_confidence: float | None = None,
        warning_reason: str | None = None,
    ) -> EvidenceLocator:
        if paper_id is None:
            raise ValueError("Evidence locator requires paper_id")
        evidence_text = evidence_text or ""
        canonical_target_type = self._canonical_target_type(target_type)
        locator = None
        if claim_id is not None:
            locator = self.session.scalar(select(EvidenceLocator).where(EvidenceLocator.claim_id == claim_id))
        if locator is None and canonical_target_type and target_id and field_name:
            locator = self.session.scalar(
                select(EvidenceLocator).where(
                    EvidenceLocator.paper_id == paper_id,
                    EvidenceLocator.target_type == canonical_target_type,
                    EvidenceLocator.target_id == target_id,
                    EvidenceLocator.field_name == field_name,
                )
            )
        if locator is None and chunk_id and canonical_target_type and target_id:
            locator = self.session.scalar(
                select(EvidenceLocator).where(
                    EvidenceLocator.paper_id == paper_id,
                    EvidenceLocator.chunk_id == chunk_id,
                    EvidenceLocator.target_type == canonical_target_type,
                    EvidenceLocator.target_id == target_id,
                    EvidenceLocator.field_name.is_(None),
                )
            )
        if locator is None:
            locator = EvidenceLocator(paper_id=paper_id, evidence_text=evidence_text)

        status, confidence, reason = self._status_from_parts(
            page=page,
            bbox=bbox,
            evidence_text=evidence_text,
            parser_source=parser_source,
            explicit_confidence=locator_confidence,
            warning_reason=warning_reason,
        )
        locator.claim_id = claim_id
        locator.chunk_id = chunk_id
        locator.source_type = source_type
        locator.page = page
        locator.bbox = self._normalize_bbox_dict(bbox)
        locator.section = section
        locator.figure_id = figure_id
        locator.table_id = table_id
        locator.equation_id = equation_id
        locator.target_type = canonical_target_type
        locator.target_id = target_id
        locator.field_name = field_name
        locator.evidence_text = evidence_text
        locator.char_start = char_start
        locator.char_end = char_end
        locator.locator_status = status
        locator.locator_confidence = confidence
        locator.parser_source = parser_source or "unknown"
        locator.warning_reason = reason
        self.session.add(locator)
        self.session.flush()
        return locator

    def get_claim_locator(self, claim_id: UUID) -> EvidenceLocatorResponse:
        locator = self.session.scalar(select(EvidenceLocator).where(EvidenceLocator.claim_id == claim_id))
        if locator is not None:
            return self._serialize(locator)
        claim = self.session.get(EvidenceClaim, claim_id)
        if claim is None:
            raise LookupError("Evidence claim not found")
        return self._fallback_claim_locator(claim)

    def list_locators_for_paper(self, paper_id: UUID) -> list[EvidenceLocatorResponse]:
        rows = self.session.scalars(
            select(EvidenceLocator).where(EvidenceLocator.paper_id == paper_id).order_by(EvidenceLocator.created_at.asc())
        ).all()
        locators = [self._serialize(row) for row in rows]
        seen_claim_ids = {item.claim_id for item in locators if item.claim_id is not None}
        seen_chunks = {(item.chunk_id, item.target_type, item.target_id) for item in locators}
        for row in self.session.scalars(select(EvidenceClaim).where(EvidenceClaim.paper_id == paper_id)).all():
            if row.id in seen_claim_ids:
                continue
            locators.append(self._fallback_claim_locator(row))
        for span in self.session.scalars(select(EvidenceSpan).where(EvidenceSpan.paper_id == paper_id)).all():
            key = (span.object_id, OBJECT_TYPE_ALIASES.get(span.object_type, span.object_type), span.object_id)
            if key in seen_chunks:
                continue
            locators.append(self._fallback_span_locator(span))
        for section in self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper_id)).all():
            key = (str(section.id), "section", str(section.id))
            if key in seen_chunks:
                continue
            locators.append(self._fallback_section_locator(section))
        return locators

    def list_extraction_locators(
        self,
        paper_id: UUID,
        results_payload: dict[str, list[dict[str, Any]]],
    ) -> list[EvidenceLocatorResponse]:
        items: list[EvidenceLocatorResponse] = []
        for rows in results_payload.values():
            for row in rows:
                target_type = row.get("target_type")
                target_id = row.get("target_id")
                for field_name, field_value in row.items():
                    if not isinstance(field_value, dict):
                        continue
                    locator = field_value.get("evidence_locator")
                    if not isinstance(locator, dict):
                        continue
                    degradation = locator_degradation(
                        page=locator.get("page"),
                        locator_status=locator.get("locator_status"),
                        evidence_text=locator.get("evidence_text") or "",
                        bbox=locator.get("bbox") if isinstance(locator.get("bbox"), dict) else None,
                        warning_reason=locator.get("warning_reason"),
                    )
                    items.append(
                        EvidenceLocatorResponse.model_validate(
                            {
                                **locator,
                                "paper_id": paper_id,
                                "target_type": target_type,
                                "target_id": target_id,
                                "field_name": field_name,
                                "claim_id": None,
                                "locator_status": degradation.locator_status,
                                "provenance_level": degradation.provenance_level,
                                "can_jump_to_pdf_page": degradation.can_jump_to_pdf_page,
                                "can_highlight_in_pdf": degradation.can_highlight_in_pdf,
                                "warning_reason": degradation.warning_reason,
                            }
                        )
                    )
        return items

    def resolve_field_locator(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        evidence_text: str,
        source_section: str | None,
        page_span: PageSpan,
    ) -> EvidenceLocatorResponse:
        row = self.session.scalar(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.target_type == self._canonical_target_type(target_type),
                EvidenceLocator.target_id == target_id,
                EvidenceLocator.field_name == field_name,
            )
        )
        if row is not None:
            return self._serialize(row)

        generic = self.session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.target_type == self._canonical_target_type(target_type),
                EvidenceLocator.target_id == target_id,
                EvidenceLocator.field_name.is_(None),
            )
        ).all()
        if generic:
            match = self._best_text_match(evidence_text, generic)
            if match is not None:
                return self._serialize(match, field_name=field_name)

        span = self.session.scalar(
            select(EvidenceSpan).where(
                EvidenceSpan.paper_id == paper_id,
                EvidenceSpan.object_id == target_id,
            )
        )
        if span is not None:
            return self._serialize_fallback(
                paper_id=paper_id,
                claim_id=None,
                chunk_id=span.object_id,
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
                evidence_text=evidence_text or span.text,
                page=span.page or page_span.page_start,
                bbox=None,
                section=source_section or span.section,
                source_type=self._normalize_source_type(span.object_type),
                parser_source="fallback",
            )

        return self._serialize_fallback(
            paper_id=paper_id,
            claim_id=None,
            chunk_id=target_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            evidence_text=evidence_text,
            page=page_span.page_start,
            bbox=None,
            section=source_section,
            source_type=self._normalize_source_type(target_type),
            parser_source="fallback",
        )

    def ensure_legacy_backfill(self, paper_id: UUID) -> int:
        created = 0
        for claim in self.session.scalars(select(EvidenceClaim).where(EvidenceClaim.paper_id == paper_id)).all():
            existing = self.session.scalar(select(EvidenceLocator).where(EvidenceLocator.claim_id == claim.id))
            if existing is not None:
                continue
            self._fallback_claim_locator(claim, persist=True)
            created += 1
        for span in self.session.scalars(select(EvidenceSpan).where(EvidenceSpan.paper_id == paper_id)).all():
            existing = self.session.scalar(
                select(EvidenceLocator).where(
                    EvidenceLocator.paper_id == span.paper_id,
                    EvidenceLocator.chunk_id == span.object_id,
                    EvidenceLocator.evidence_text == span.text,
                    EvidenceLocator.target_type == self._canonical_target_type(span.object_type),
                )
            )
            if existing is not None:
                continue
            self._fallback_span_locator(span, persist=True)
            created += 1
        return created

    def _fallback_claim_locator(self, claim: EvidenceClaim, persist: bool = False) -> EvidenceLocatorResponse:
        page = claim.page_start or claim.page_end
        response = self._serialize_fallback(
            paper_id=claim.paper_id,
            claim_id=claim.id,
            chunk_id=claim.chunk_id,
            target_type=claim.target_type,
            target_id=claim.target_id,
            field_name=None,
            evidence_text=claim.evidence_text,
            page=page,
            bbox=None,
            section=self._section_title(claim.section_id),
            source_type=self._normalize_source_type(claim.source_type),
            parser_source="unknown",
            char_start=claim.span_start,
            char_end=claim.span_end,
        )
        if persist and claim.paper_id is not None:
            self.upsert_locator(
                paper_id=claim.paper_id,
                claim_id=claim.id,
                chunk_id=claim.chunk_id,
                source_type=response.source_type,
                page=response.page,
                bbox=response.bbox.model_dump(mode="json") if response.bbox else None,
                section=response.section,
                target_type=claim.target_type,
                target_id=claim.target_id,
                field_name=None,
                evidence_text=claim.evidence_text,
                char_start=claim.span_start,
                char_end=claim.span_end,
                parser_source=response.parser_source,
                locator_confidence=response.locator_confidence,
                warning_reason=response.warning_reason,
            )
        return response

    def _fallback_span_locator(self, span: EvidenceSpan, persist: bool = False) -> EvidenceLocatorResponse:
        response = self._serialize_fallback(
            paper_id=span.paper_id,
            claim_id=None,
            chunk_id=span.object_id,
            target_type=self._canonical_target_type(span.object_type),
            target_id=span.object_id,
            field_name=None,
            evidence_text=span.text,
            page=span.page,
            bbox=None,
            section=span.section,
            source_type=self._normalize_source_type(span.object_type),
            parser_source="fallback",
        )
        if persist:
            self.upsert_locator(
                paper_id=span.paper_id,
                claim_id=None,
                chunk_id=span.object_id,
                source_type=response.source_type,
                page=response.page,
                bbox=None,
                section=response.section,
                target_type=self._canonical_target_type(span.object_type),
                target_id=span.object_id,
                field_name=None,
                evidence_text=span.text,
                parser_source=response.parser_source,
                locator_confidence=response.locator_confidence,
                warning_reason=response.warning_reason,
            )
        return response

    def _fallback_section_locator(self, section: PaperSection) -> EvidenceLocatorResponse:
        return self._serialize_fallback(
            paper_id=section.paper_id,
            claim_id=None,
            chunk_id=str(section.id),
            target_type="section",
            target_id=str(section.id),
            field_name=None,
            evidence_text=(section.text or "")[:1200],
            page=section.page_start or section.page_end,
            bbox=None,
            section=section.section_title,
            source_type="text",
            parser_source="fallback",
        )

    def _serialize(self, row: EvidenceLocator, field_name: str | None = None) -> EvidenceLocatorResponse:
        bbox = self._bbox_model(row.bbox)
        degradation = locator_degradation(
            page=row.page,
            locator_status=row.locator_status,
            evidence_text=row.evidence_text,
            bbox=bbox.model_dump(mode="json") if bbox else None,
            warning_reason=row.warning_reason,
        )
        return EvidenceLocatorResponse(
            id=row.id,
            paper_id=row.paper_id,
            claim_id=row.claim_id,
            chunk_id=row.chunk_id,
            target_type=row.target_type,
            target_id=row.target_id,
            field_name=field_name or row.field_name,
            evidence_text=row.evidence_text,
            page=row.page,
            bbox=bbox,
            section=row.section,
            source_type=row.source_type,
            locator_status=degradation.locator_status,
            provenance_level=degradation.provenance_level,
            can_jump_to_pdf_page=degradation.can_jump_to_pdf_page,
            can_highlight_in_pdf=degradation.can_highlight_in_pdf,
            locator_confidence=row.locator_confidence,
            parser_source=row.parser_source,
            figure_id=row.figure_id,
            table_id=row.table_id,
            equation_id=row.equation_id,
            char_start=row.char_start,
            char_end=row.char_end,
            warning_reason=degradation.warning_reason,
        )

    def _serialize_fallback(
        self,
        *,
        paper_id: UUID | None,
        claim_id: UUID | None,
        chunk_id: str | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
        evidence_text: str,
        page: int | None,
        bbox: dict[str, Any] | None,
        section: str | None,
        source_type: str,
        parser_source: str,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> EvidenceLocatorResponse:
        if paper_id is None:
            raise ValueError("Fallback locator requires paper_id")
        status, confidence, reason = self._status_from_parts(
            page=page,
            bbox=bbox,
            evidence_text=evidence_text,
            parser_source=parser_source,
            explicit_confidence=None,
            warning_reason=None,
        )
        normalized_bbox = self._bbox_model(bbox)
        degradation = locator_degradation(
            page=page,
            locator_status=status,
            evidence_text=evidence_text,
            bbox=normalized_bbox.model_dump(mode="json") if normalized_bbox else None,
            warning_reason=reason,
        )
        return EvidenceLocatorResponse(
            paper_id=paper_id,
            claim_id=claim_id,
            chunk_id=chunk_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            evidence_text=evidence_text,
            page=page,
            bbox=normalized_bbox,
            section=section,
            source_type=source_type,
            locator_status=degradation.locator_status,
            provenance_level=degradation.provenance_level,
            can_jump_to_pdf_page=degradation.can_jump_to_pdf_page,
            can_highlight_in_pdf=degradation.can_highlight_in_pdf,
            locator_confidence=confidence,
            parser_source=parser_source,
            char_start=char_start,
            char_end=char_end,
            warning_reason=degradation.warning_reason,
        )

    @staticmethod
    def _status_from_parts(
        *,
        page: int | None,
        bbox: dict[str, Any] | None,
        evidence_text: str,
        parser_source: str,
        explicit_confidence: float | None,
        warning_reason: str | None,
    ) -> tuple[str, float, str | None]:
        normalized_bbox = EvidenceLocatorService._normalize_bbox_dict(bbox)
        if normalized_bbox and page is not None:
            return "exact_page", explicit_confidence if explicit_confidence is not None else 0.98, warning_reason
        if page is not None:
            return "exact_page", explicit_confidence if explicit_confidence is not None else 0.72, warning_reason or "bbox unavailable"
        safe_text = evidence_text or ""
        if safe_text.strip():
            status = "missing_page" if parser_source == "fallback" else "text_only"
            confidence = explicit_confidence if explicit_confidence is not None else (0.35 if status == "text_only" else 0.2)
            reason = warning_reason or ("page missing from parser output" if status == "text_only" else "page missing; reparsing may recover page")
            return status, confidence, reason
        return "missing_locator", explicit_confidence if explicit_confidence is not None else 0.0, warning_reason or "no locator evidence available"

    @staticmethod
    def _normalize_bbox_dict(bbox: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(bbox, dict):
            return None
        try:
            if {"x0", "y0", "x1", "y1"} <= set(bbox):
                return {
                    "x0": float(bbox["x0"]),
                    "y0": float(bbox["y0"]),
                    "x1": float(bbox["x1"]),
                    "y1": float(bbox["y1"]),
                    "width": float(bbox["width"]) if bbox.get("width") is not None else None,
                    "height": float(bbox["height"]) if bbox.get("height") is not None else None,
                    "coordinate_system": bbox.get("coordinate_system") or "pdf_points",
                }
            if {"l", "t", "r", "b"} <= set(bbox):
                width = bbox.get("width")
                height = bbox.get("height")
                return {
                    "x0": float(bbox["l"]),
                    "y0": float(bbox["t"]),
                    "x1": float(bbox["r"]),
                    "y1": float(bbox["b"]),
                    "width": float(width) if width is not None else None,
                    "height": float(height) if height is not None else None,
                    "coordinate_system": bbox.get("coordinate_system") or bbox.get("coord_origin") or "pdf_points",
                }
        except (TypeError, ValueError, KeyError):
            return None
        return None

    @staticmethod
    def _bbox_model(bbox: dict[str, Any] | None) -> EvidenceBBox | None:
        normalized = EvidenceLocatorService._normalize_bbox_dict(bbox)
        if normalized is None:
            return None
        return EvidenceBBox.model_validate(normalized)

    @staticmethod
    def _normalize_source_type(value: str | None) -> str:
        normalized = (value or "unknown").lower()
        if "figure" in normalized:
            return "figure"
        if "table" in normalized:
            return "table"
        if "equation" in normalized or "formula" in normalized:
            return "equation"
        if normalized in {"text", "section", "evidence_claim", "evidence_span", "derived", "manual", "writer"}:
            return "text"
        return "unknown"

    @staticmethod
    def _canonical_target_type(value: str | None) -> str | None:
        if value is None:
            return None
        return OBJECT_TYPE_ALIASES.get(value, value)

    def _resolve_figure_id(self, paper_id: UUID, caption: str | None) -> UUID | None:
        if not caption:
            return None
        row = self.session.scalar(select(PaperFigure).where(PaperFigure.paper_id == paper_id, PaperFigure.caption == caption))
        return row.id if row is not None else None

    def _resolve_table_id(self, paper_id: UUID, caption: str | None) -> UUID | None:
        if not caption:
            return None
        row = self.session.scalar(select(PaperTable).where(PaperTable.paper_id == paper_id, PaperTable.caption == caption))
        return row.id if row is not None else None

    def _section_title(self, section_id: UUID | None) -> str | None:
        if section_id is None:
            return None
        row = self.session.get(PaperSection, section_id)
        if row is not None:
            return row.section_title
        return None

    @staticmethod
    def _best_text_match(evidence_text: str, rows: list[EvidenceLocator]) -> EvidenceLocator | None:
        normalized = evidence_text.strip()
        if not normalized:
            return rows[0] if rows else None
        for row in rows:
            if row.evidence_text == normalized:
                return row
        for row in rows:
            if normalized in row.evidence_text or row.evidence_text in normalized:
                return row
        return rows[0] if rows else None
