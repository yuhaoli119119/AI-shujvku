from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import EvidenceClaim, EvidenceLocator, Paper, PaperSection
from app.utils.artifact_paths import resolve_paper_pdf_path


_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+./-]{1,}")
_UNIT_RE = re.compile(r"(?:eV|meV|mAh\s*/?\s*g(?:-1)?|mg\s*/?\s*cm(?:2|-2)|wt\s*%|%|V)\b", re.I)
_STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "were", "was", "are",
    "into", "using", "used", "study", "result", "results", "show", "shows", "can",
}
_DIRECTION_WORD_RE = re.compile(
    r"\b(?:accelerat(?:e|es|ed|ing)|decreas(?:e|es|ed|ing)|enhanc(?:e|es|ed|ing)|"
    r"higher|increase(?:s|d|ing)?|inhibit(?:s|ed|ing)?|lower|negative|positive|"
    r"promot(?:e|es|ed|ing)|reduc(?:e|es|ed|ing)|slower|stronger|suppress(?:es|ed|ing)?|"
    r"weaker)\b",
    re.I,
)
_DIRECTION_SYMBOLS = ("→", "←", "↑", "↓", "≤", "≥", "<", ">", "+", "−")

_PDF_GLYPH_NAMES = {
    "/uniFB00": "ff",
    "/uniFB01": "fi",
    "/uniFB02": "fl",
    "/uniFB03": "ffi",
    "/uniFB04": "ffl",
}


def _replace_pdf_glyph_names(value: Any) -> str:
    text = str(value or "")
    for glyph_name, replacement in _PDF_GLYPH_NAMES.items():
        text = text.replace(glyph_name, replacement)
    return text


def normalize_page_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _replace_pdf_glyph_names(value))
    text = text.replace("\u00ad", "").replace("‐", "-").replace("‑", "-")
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def compact_page_text(value: Any) -> str:
    return "".join(character for character in normalize_page_text(value) if character.isalnum())


def _compact_with_source_map(value: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    source_indices: list[int] = []
    for source_index, character in enumerate(str(value or "")):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for emitted in normalized:
            if emitted.isalnum():
                compact.append(emitted)
                source_indices.append(source_index)
    return "".join(compact), source_indices


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PageTextRecord:
    paper_id: str
    page: int
    text: str
    source_type: str
    extraction_source: str
    extraction_method: str
    character_count: int
    status: str
    pdf_path: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperPageTextProvider:
    """Read physical PDF pages without mutating paper or evidence state.

    Public page numbers are always 1-based. PyMuPDF's 0-based page index is
    converted only at the load boundary.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[tuple[str, str, int], tuple[PageTextRecord, ...]] = {}

    def read_pages(self, paper: Paper) -> tuple[PageTextRecord, ...]:
        pdf_path = resolve_paper_pdf_path(paper.pdf_path, self.settings.storage_root)
        if pdf_path is None:
            return (
                PageTextRecord(
                    paper_id=str(paper.id), page=0, text="", source_type="pdf",
                    extraction_source="none", extraction_method="none", character_count=0,
                    status="missing_real_pdf", error="missing_real_pdf", pdf_path=None,
                ),
            )
        try:
            signature = pdf_path.stat().st_mtime_ns
        except OSError:
            signature = 0
        cache_key = (str(paper.id), str(pdf_path), signature)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            records: list[PageTextRecord] = []
            with fitz.open(pdf_path) as document:
                for zero_based_index in range(document.page_count):
                    text = document.load_page(zero_based_index).get_text("text") or ""
                    records.append(
                        PageTextRecord(
                            paper_id=str(paper.id),
                            page=zero_based_index + 1,
                            text=text,
                            source_type="pdf",
                            extraction_source="pymupdf_text_layer",
                            extraction_method="fitz.Page.get_text(text)",
                            character_count=len(text),
                            status="ok" if text.strip() else "no_page_text",
                            pdf_path=str(pdf_path),
                        )
                    )
            result = tuple(records)
        except Exception as exc:
            result = (
                PageTextRecord(
                    paper_id=str(paper.id), page=0, text="", source_type="pdf",
                    extraction_source="pymupdf_text_layer", extraction_method="fitz.open",
                    character_count=0, status="extraction_failed", error=type(exc).__name__,
                    pdf_path=str(pdf_path),
                ),
            )
        self._cache[cache_key] = result
        return result

    def read_page(self, paper: Paper, page: int) -> PageTextRecord:
        records = self.read_pages(paper)
        if len(records) == 1 and records[0].page == 0:
            return records[0]
        if page < 1 or page > len(records):
            return PageTextRecord(
                paper_id=str(paper.id), page=page, text="", source_type="pdf",
                extraction_source="pymupdf_text_layer", extraction_method="fitz.Page.get_text(text)",
                character_count=0, status="invalid_pdf_page", error="invalid_pdf_page",
                pdf_path=records[0].pdf_path if records else None,
            )
        return records[page - 1]


class EvidencePageRecoveryService:
    """Deterministically recover in-memory exact-page evidence candidates."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        page_provider: PaperPageTextProvider | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.page_provider = page_provider or PaperPageTextProvider(self.settings)

    def recover_for_target(
        self,
        *,
        paper: Paper,
        target_type: str,
        target_id: str,
        field_name: str,
        target_value: Any,
        evidence_text: str,
        evidence_types: list[Any] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 5))
        page_records = self.page_provider.read_pages(paper)
        usable_pages = [record for record in page_records if record.page >= 1 and record.status == "ok"]
        extraction_failures = [record for record in page_records if record.status == "extraction_failed"]
        if not usable_pages:
            status = "extraction_failed" if extraction_failures else "no_page_text"
            return self._result(status, [], page_records, [status])

        existing = self._existing_exact_candidates(
            paper=paper,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            page_records=usable_pages,
        )
        if existing:
            return self._result("existing_exact", existing[:bounded], page_records, [])

        queries = self._exact_queries(target_value=target_value, evidence_text=evidence_text)
        exact: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for match_method, query in queries:
            query_compact = compact_page_text(query)
            if len(query_compact) < 12:
                continue
            for record in usable_pages:
                page_compact, source_map = _compact_with_source_map(record.text)
                start = page_compact.find(query_compact)
                if start < 0:
                    continue
                end = start + len(query_compact) - 1
                quote = record.text[source_map[start] : source_map[end] + 1].strip()
                key = (record.page, compact_page_text(quote))
                if not quote or key in seen:
                    continue
                seen.add(key)
                exact.append(
                    self._candidate(
                        paper=paper,
                        target_type=target_type,
                        target_id=target_id,
                        field_name=field_name,
                        page=record.page,
                        quoted_text=quote,
                        target_value=target_value,
                        evidence_text=evidence_text,
                        evidence_types=evidence_types,
                        extraction_source=record.extraction_source,
                        match_method=match_method,
                        match_score=1.0 if match_method == "evidence_text_full" else 0.98,
                        locator_status="exact_page",
                        warning_reason=None,
                    )
                )
        if exact:
            exact.sort(key=lambda item: (-float(item["match_score"]), int(item["page"]), item["candidate_fingerprint"]))
            return self._result("recovered", exact[:bounded], page_records, [])

        approximate = self._approximate_candidates(
            paper=paper,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            target_value=target_value,
            evidence_text=evidence_text,
            evidence_types=evidence_types,
            page_records=usable_pages,
            limit=bounded,
        )
        if approximate:
            return self._result(
                "approximate_only",
                approximate,
                page_records,
                ["approximate_candidates_cannot_authorize_ai_verified"],
            )
        return self._result("no_supporting_evidence", [], page_records, ["no_supporting_evidence"])

    def recover_section_page_fragments(
        self,
        *,
        paper: Paper,
        section: PaperSection,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Split a body section into exact, single-physical-page evidence atoms.

        The method is read-only.  It never edits PaperSection/PaperChunk and it
        never treats a partial page match as authorization for the parent.
        Returned fragment IDs are deterministic UUIDs suitable for EvidenceClaim
        subobjects when a later, explicitly authorized write workflow is used.
        """

        if section.paper_id != paper.id:
            raise ValueError("section_paper_mismatch")
        bounded = max(1, min(int(limit), 100))
        page_records = self.page_provider.read_pages(paper)
        usable_pages = [record for record in page_records if record.page >= 1 and record.status == "ok"]
        if not usable_pages:
            status = "extraction_failed" if any(
                record.status == "extraction_failed" for record in page_records
            ) else "no_page_text"
            return {
                "status": status,
                "paper_id": str(paper.id),
                "section_id": str(section.id),
                "fragment_count": 0,
                "fragments": [],
                "parent_section_can_be_verified": False,
                "parent_section_gate_action": "remain_blocked",
                "blocked_reasons": [status],
                "physical_page_numbering": "1_based_pdf",
                "database_writes": False,
            }

        source_atoms = self._section_text_atoms(section.text)
        fragments: list[dict[str, Any]] = []
        unmatched: list[int] = []
        ambiguous: list[int] = []
        skipped_short: list[int] = []
        matched_source_characters = 0

        for atom_index, (source_start, source_end, atom_text) in enumerate(source_atoms):
            atom_compact = compact_page_text(atom_text)
            if len(atom_compact) < 32:
                skipped_short.append(atom_index)
                continue
            matches: list[tuple[PageTextRecord, str]] = []
            seen_matches: set[tuple[int, str]] = set()
            for record in usable_pages:
                page_compact, source_map = _compact_with_source_map(record.text)
                compact_start = page_compact.find(atom_compact)
                while compact_start >= 0:
                    compact_end = compact_start + len(atom_compact) - 1
                    quote = record.text[
                        source_map[compact_start] : source_map[compact_end] + 1
                    ].strip()
                    match_key = (record.page, compact_page_text(quote))
                    if quote and match_key not in seen_matches:
                        seen_matches.add(match_key)
                        matches.append((record, quote))
                    compact_start = page_compact.find(atom_compact, compact_start + 1)
            if not matches:
                unmatched.append(atom_index)
                continue
            if len(matches) != 1:
                ambiguous.append(atom_index)
                continue
            record, quote = matches[0]
            exact_alphanumeric = compact_page_text(atom_text) == compact_page_text(quote)
            numeric_consistency = self._numeric_consistency(atom_text, quote)
            unit_consistency = self._unit_consistency(atom_text, quote)
            direction_consistency = self._direction_consistency(atom_text, quote)
            if not all(
                (exact_alphanumeric, numeric_consistency, unit_consistency, direction_consistency)
            ):
                unmatched.append(atom_index)
                continue
            identity = {
                "paper_id": str(paper.id),
                "parent_section_id": str(section.id),
                "page": int(record.page),
                "text": normalize_page_text(quote),
            }
            fragment_uuid = uuid5(NAMESPACE_URL, _stable_hash(identity))
            fragment_fingerprint = _stable_hash(
                {
                    **identity,
                    "fragment_id": str(fragment_uuid),
                    "source_start": source_start,
                    "source_end": source_end,
                }
            )
            fragments.append(
                {
                    "fragment_id": str(fragment_uuid),
                    "target_type": "section_page_fragments",
                    "field_name": "text",
                    "source_type": "section_page_fragment",
                    "parent_section_id": str(section.id),
                    "section_title": section.section_title,
                    "section_type": section.section_type,
                    "page": int(record.page),
                    "page_start": int(record.page),
                    "page_end": int(record.page),
                    "text": quote,
                    "claim_text": quote,
                    "evidence_text": quote,
                    "locator_status": "exact_page",
                    "source_start": source_start,
                    "source_end": source_end,
                    "fragment_fingerprint": fragment_fingerprint,
                    "checks": {
                        "single_physical_pdf_page": True,
                        "text_exists_on_pdf_page": True,
                        "exact_alphanumeric_sequence": exact_alphanumeric,
                        "numeric_consistency": numeric_consistency,
                        "unit_consistency": unit_consistency,
                        "chemical_and_entity_sequence_consistent": exact_alphanumeric,
                        "direction_consistency": direction_consistency,
                    },
                    "can_use_for_writing": False,
                    "can_use_for_citation": False,
                    "review_status": "unreviewed",
                }
            )
            matched_source_characters += source_end - source_start

        fragments.sort(
            key=lambda item: (
                int(item["page"]),
                int(item["source_start"]),
                str(item["fragment_id"]),
            )
        )
        truncated = len(fragments) > bounded
        returned_fragments = fragments[:bounded]
        matched_atom_count = len(fragments)
        eligible_atom_count = len(source_atoms) - len(skipped_short)
        pages = sorted({int(item["page"]) for item in fragments})
        pages_continuous = not pages or pages == list(range(pages[0], pages[-1] + 1))
        complete_parent_coverage = bool(source_atoms) and (
            matched_atom_count == eligible_atom_count
            and not unmatched
            and not ambiguous
            and not skipped_short
            and not truncated
            and pages_continuous
        )
        blocked_reasons: list[str] = []
        if not complete_parent_coverage:
            blocked_reasons.append("incomplete_body_section_page_coverage")
        if ambiguous:
            blocked_reasons.append("ambiguous_page_matches")
        if unmatched:
            blocked_reasons.append("unmatched_section_text")
        if skipped_short:
            blocked_reasons.append("short_atoms_not_authoritative")
        if truncated:
            blocked_reasons.append("fragment_limit_truncated")
        return {
            "status": "recovered" if fragments else "no_exact_page_fragments",
            "paper_id": str(paper.id),
            "section_id": str(section.id),
            "section_type": section.section_type,
            "source_text_character_count": len(section.text or ""),
            "matched_source_character_count": matched_source_characters,
            "source_atom_count": len(source_atoms),
            "eligible_atom_count": eligible_atom_count,
            "matched_atom_count": matched_atom_count,
            "unmatched_atom_indexes": unmatched,
            "ambiguous_atom_indexes": ambiguous,
            "skipped_short_atom_indexes": skipped_short,
            "physical_pages": pages,
            "pages_continuous": pages_continuous,
            "fragment_count": len(returned_fragments),
            "total_fragment_count": len(fragments),
            "fragments": returned_fragments,
            "parent_section_can_be_verified": complete_parent_coverage,
            "parent_section_gate_action": (
                "full_coverage_may_be_reviewed"
                if complete_parent_coverage
                else "remain_blocked"
            ),
            "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
            "physical_page_numbering": "1_based_pdf",
            "embedding_requests": 0,
            "embedding_role": "retrieval_only",
            "database_writes": False,
        }

    @staticmethod
    def fragment_claim(
        *,
        paper: Paper,
        section: PaperSection,
        fragment: dict[str, Any],
    ) -> EvidenceClaim:
        """Build an unsaved EvidenceClaim page-fragment subobject."""

        page = int(fragment["page"])
        text = str(fragment["text"])
        return EvidenceClaim(
            id=UUID(str(fragment["fragment_id"])),
            paper_id=paper.id,
            section_id=section.id,
            source_type="section_page_fragment",
            target_type="sections",
            target_id=str(section.id),
            claim_text=text,
            evidence_text=text,
            page_start=page,
            page_end=page,
            validation_status="unverified",
            meta={
                "parent_section_id": str(section.id),
                "fragment_fingerprint": fragment["fragment_fingerprint"],
                "physical_page_numbering": "1_based_pdf",
                "source_start": fragment["source_start"],
                "source_end": fragment["source_end"],
            },
        )

    @staticmethod
    def _section_text_atoms(value: Any) -> list[tuple[int, int, str]]:
        text = str(value or "")
        atoms: list[tuple[int, int, str]] = []
        cursor = 0
        for match in re.finditer(r".*?(?:[.!?](?=\s|$)|\Z)", text, flags=re.S):
            raw = match.group(0)
            if not raw:
                continue
            leading = len(raw) - len(raw.lstrip())
            trailing = len(raw.rstrip())
            start = match.start() + leading
            end = match.start() + trailing
            atom = text[start:end]
            if atom:
                atoms.append((start, end, atom))
            cursor = match.end()
        if not atoms and text.strip():
            start = len(text) - len(text.lstrip())
            end = len(text.rstrip())
            atoms.append((start, end, text[start:end]))
        return atoms

    @staticmethod
    def _exact_queries(*, target_value: Any, evidence_text: str) -> list[tuple[str, str]]:
        queries: list[tuple[str, str]] = []
        if str(evidence_text or "").strip():
            queries.append(("evidence_text_full", str(evidence_text).strip()))
        if isinstance(target_value, list):
            for index, item in enumerate(target_value):
                if not isinstance(item, dict):
                    continue
                item_text = str(item.get("text") or item.get("evidence_text") or "").strip()
                if item_text:
                    queries.append((f"target_value_item:{index}", item_text))
        else:
            value_text = str(target_value or "").strip()
            if value_text:
                queries.append(("target_value_full", value_text))
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for match_method, query in queries:
            fingerprint = compact_page_text(query)
            if fingerprint and fingerprint not in seen:
                seen.add(fingerprint)
                result.append((match_method, query))
        return result

    def _existing_exact_candidates(
        self,
        *,
        paper: Paper,
        target_type: str,
        target_id: str,
        field_name: str,
        page_records: list[PageTextRecord],
    ) -> list[dict[str, Any]]:
        by_page = {record.page: record for record in page_records}
        rows = self.session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper.id,
                EvidenceLocator.target_id == str(target_id),
                EvidenceLocator.field_name == field_name,
                EvidenceLocator.page.is_not(None),
                EvidenceLocator.locator_status.in_(["exact_page", "exact_bbox"]),
            )
        ).all()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                if self._canonical_target_type(str(row.target_type or "")) != self._canonical_target_type(target_type):
                    continue
            except ValueError:
                continue
            record = by_page.get(int(row.page or 0))
            quote = str(row.evidence_text or "").strip()
            if record is None or not quote or compact_page_text(quote) not in compact_page_text(record.text):
                continue
            candidates.append(
                self._candidate(
                    paper=paper,
                    target_type=target_type,
                    target_id=target_id,
                    field_name=field_name,
                    page=int(row.page),
                    quoted_text=quote,
                    target_value=quote,
                    evidence_text=quote,
                    evidence_types=[],
                    extraction_source=str(row.parser_source or record.extraction_source),
                    match_method="existing_exact_locator",
                    match_score=float(row.locator_confidence or 1.0),
                    locator_status=str(row.locator_status),
                    warning_reason=None,
                )
            )
        candidates.sort(key=lambda item: (int(item["page"]), item["candidate_fingerprint"]))
        return candidates

    def _approximate_candidates(
        self,
        *,
        paper: Paper,
        target_type: str,
        target_id: str,
        field_name: str,
        target_value: Any,
        evidence_text: str,
        evidence_types: list[Any] | None,
        page_records: list[PageTextRecord],
        limit: int,
    ) -> list[dict[str, Any]]:
        query = " ".join((str(target_value or ""), str(evidence_text or ""), " ".join(map(str, evidence_types or []))))
        query_tokens = self._tokens(query)
        if not query_tokens:
            return []
        candidates: list[dict[str, Any]] = []
        for record in page_records:
            segments = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+|\n{2,}", record.text) if segment.strip()]
            for segment in segments:
                segment_tokens = self._tokens(segment)
                overlap = len(query_tokens & segment_tokens) / max(1, len(query_tokens))
                if overlap < 0.25:
                    continue
                numeric_ok = self._numeric_consistency(query, segment)
                unit_ok = self._unit_consistency(query, segment)
                score = round(overlap * (1.0 if numeric_ok else 0.5) * (1.0 if unit_ok else 0.5), 6)
                candidates.append(
                    self._candidate(
                        paper=paper,
                        target_type=target_type,
                        target_id=target_id,
                        field_name=field_name,
                        page=record.page,
                        quoted_text=segment,
                        target_value=target_value,
                        evidence_text=evidence_text,
                        evidence_types=evidence_types,
                        extraction_source=record.extraction_source,
                        match_method="deterministic_entity_overlap",
                        match_score=score,
                        locator_status="approximate",
                        warning_reason="approximate_entity_match_not_safe_for_ai_verified",
                    )
                )
        candidates.sort(key=lambda item: (-float(item["match_score"]), int(item["page"]), item["candidate_fingerprint"]))
        return candidates[:limit]

    def _candidate(
        self,
        *,
        paper: Paper,
        target_type: str,
        target_id: str,
        field_name: str,
        page: int,
        quoted_text: str,
        target_value: Any,
        evidence_text: str,
        evidence_types: list[Any] | None,
        extraction_source: str,
        match_method: str,
        match_score: float,
        locator_status: str,
        warning_reason: str | None,
    ) -> dict[str, Any]:
        query = " ".join((str(target_value or ""), str(evidence_text or ""), " ".join(map(str, evidence_types or []))))
        entity_overlap = len(self._tokens(query) & self._tokens(quoted_text)) / max(1, len(self._tokens(query)))
        payload = {
            "paper_id": str(paper.id),
            "target_type": target_type,
            "target_id": str(target_id),
            "field_name": field_name,
            "page": int(page),
            "quoted_text": quoted_text,
            "evidence_text": quoted_text,
            "normalized_text": normalize_page_text(quoted_text),
            "locator_status": locator_status,
            "source_type": "pdf",
            "extraction_source": extraction_source,
            "match_method": match_method,
            "match_score": round(float(match_score), 6),
            "entity_overlap": round(float(entity_overlap), 6),
            "numeric_consistency": self._numeric_consistency(query, quoted_text),
            "unit_consistency": self._unit_consistency(query, quoted_text),
            "can_jump_to_pdf_page": locator_status in {"exact_page", "exact_bbox"},
            "warning_reason": warning_reason,
        }
        payload["candidate_fingerprint"] = _stable_hash(payload)
        return payload

    @staticmethod
    def _tokens(value: Any) -> set[str]:
        return {
            token.casefold()
            for token in _TOKEN_RE.findall(unicodedata.normalize("NFKC", str(value or "")))
            if token.casefold() not in _STOPWORDS
        }

    @staticmethod
    def _canonical_target_type(value: str) -> str:
        normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {
            "mechanism_claim": "mechanism_claims",
            "dft_result": "dft_results",
            "electrochemical_performance_item": "electrochemical_performance",
            "paper_section": "sections",
            "section": "sections",
            "section_page_fragment": "section_page_fragments",
            "writing_card": "writing_cards",
        }
        canonical = aliases.get(normalized, normalized)
        if canonical not in {
            "mechanism_claims", "dft_results", "electrochemical_performance", "sections",
            "section_page_fragments", "writing_cards",
        }:
            raise ValueError("unsupported_target_type")
        return canonical

    @staticmethod
    def _numeric_consistency(query: Any, candidate: Any) -> bool:
        expected = [float(value) for value in _NUMBER_RE.findall(str(query or ""))]
        if not expected:
            return True
        observed = [float(value) for value in _NUMBER_RE.findall(str(candidate or ""))]
        return all(any(abs(item - value) <= max(1e-8, abs(item) * 1e-6) for value in observed) for item in expected)

    @staticmethod
    def _unit_consistency(query: Any, candidate: Any) -> bool:
        expected = {normalize_page_text(value).replace(" ", "") for value in _UNIT_RE.findall(str(query or ""))}
        if not expected:
            return True
        observed = normalize_page_text(candidate).replace(" ", "")
        return all(unit in observed for unit in expected)

    @staticmethod
    def _direction_consistency(query: Any, candidate: Any) -> bool:
        def signature(value: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
            normalized = unicodedata.normalize("NFKC", str(value or "")).replace("-", "−")
            words = tuple(sorted(item.casefold() for item in _DIRECTION_WORD_RE.findall(normalized)))
            symbols = tuple(symbol for symbol in _DIRECTION_SYMBOLS if symbol in normalized)
            return words, symbols

        return signature(query) == signature(candidate)

    @staticmethod
    def _result(
        status: str,
        candidates: list[dict[str, Any]],
        page_records: tuple[PageTextRecord, ...] | list[PageTextRecord],
        blocked_reasons: list[str],
    ) -> dict[str, Any]:
        return {
            "status": status,
            "candidate_count": len(candidates),
            "exact_candidate_count": sum(
                item.get("locator_status") in {"exact_page", "exact_bbox"} for item in candidates
            ),
            "candidates": candidates,
            "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
            "page_text_statuses": {
                str(record.page): {
                    "status": record.status,
                    "character_count": record.character_count,
                    "extraction_source": record.extraction_source,
                }
                for record in page_records
            },
            "database_writes": False,
        }
