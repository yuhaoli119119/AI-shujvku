from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, EvidenceLocator, MechanismClaim, PaperFigure, WritingCard
from app.rag.eligibility import figure_has_safe_review, figure_is_rag_eligible
from app.utils.writing_card_content import normalized_evidence_chain


_FIGURE_REFERENCE_RE = re.compile(
    r"\bfig(?:ure)?\.?\s*(?P<label>S?\d+)(?P<panel>[A-Za-z])?\b",
    re.IGNORECASE,
)


class ContentFigureLinkService:
    """Read-only bridge from reviewed text evidence to reviewed figures.

    It consumes the existing figure review/RAG gate and never writes figure
    metadata or review state.  Links are rebuilt from current rows on every
    request, so unreviewed figures are not exposed and figure changes produce a
    different snapshot fingerprint.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        # One service instance represents one read request. Reuse the costly
        # figure-review gate within that request; a new request builds a new
        # service and therefore observes current review state.
        self._eligible_by_paper: dict[str, dict[str, PaperFigure]] = {}
        self._serialized_by_figure: dict[str, dict[str, Any]] = {}

    def links_for_writing_card(self, card: WritingCard) -> list[dict[str, Any]]:
        locators = list(
            self.session.scalars(
                select(EvidenceLocator).where(
                    EvidenceLocator.paper_id == card.paper_id,
                    EvidenceLocator.target_id == str(card.id),
                    EvidenceLocator.target_type.in_({"writing_card", "writing_cards"}),
                    EvidenceLocator.figure_id.is_not(None),
                )
            ).all()
        )
        references: list[tuple[str, str, str | None]] = [
            (
                str(locator.figure_id),
                "evidence_locator",
                self._writing_locator_evidence_id(locator),
            )
            for locator in locators
        ]
        for evidence in normalized_evidence_chain(card.evidence_chain, limit=8):
            for reference in self._explicit_references(
                f"{evidence.get('source') or ''} {evidence.get('text') or ''}"
            ):
                references.append((reference, "explicit_reference", evidence["evidence_id"]))
        links = self._resolve(card.paper_id, references)
        self._attach_figure_logic_context(links, card.figure_logic)
        return links

    def links_for_mechanism_claim(self, claim: MechanismClaim) -> list[dict[str, Any]]:
        locators = list(
            self.session.scalars(
                select(EvidenceLocator).where(
                    EvidenceLocator.paper_id == claim.paper_id,
                    EvidenceLocator.target_id == str(claim.id),
                    EvidenceLocator.target_type.in_({"mechanism_claim", "mechanism_claims"}),
                    EvidenceLocator.figure_id.is_not(None),
                )
            ).all()
        )
        references: list[tuple[str, str, str | None]] = [
            (
                str(locator.figure_id),
                "evidence_locator",
                f"evidence_locator:{locator.id}",
            )
            for locator in locators
        ]
        for reference in self._explicit_references(
            f"{claim.claim_text or ''} {claim.evidence_text or ''}"
        ):
            references.append((reference, "explicit_reference", None))
        return self._resolve(claim.paper_id, references)

    def links_for_content_source(
        self,
        *,
        paper_id: Any,
        source_type: str | None,
        source_id: Any,
    ) -> list[dict[str, Any]]:
        """Rebuild links from the canonical source row and current figure gate."""

        try:
            object_id = UUID(str(source_id))
            canonical_paper_id = UUID(str(paper_id))
        except (TypeError, ValueError):
            return []
        normalized_type = str(source_type or "").strip().casefold()
        if normalized_type in {"writing_card", "writing_cards"}:
            card = self.session.get(WritingCard, object_id)
            if card is None or card.paper_id != canonical_paper_id:
                return []
            return self.links_for_writing_card(card)
        if normalized_type in {"mechanism_claim", "mechanism_claims"}:
            claim = self.session.get(MechanismClaim, object_id)
            if claim is None or claim.paper_id != canonical_paper_id:
                return []
            return self.links_for_mechanism_claim(claim)
        return []

    def _resolve(
        self,
        paper_id: Any,
        references: Iterable[tuple[str, str, str | None]],
    ) -> list[dict[str, Any]]:
        eligible = self._eligible_figures(paper_id)
        if not eligible:
            return []
        by_label: dict[str, PaperFigure] = {}
        for figure in eligible.values():
            for label in self._figure_aliases(figure.figure_label, figure.caption):
                by_label.setdefault(label, figure)

        linked: dict[str, dict[str, Any]] = {}
        for reference, matched_by, evidence_id in references:
            figure = eligible.get(reference)
            if figure is None:
                for alias in self._reference_aliases(reference):
                    figure = by_label.get(alias)
                    if figure is not None:
                        break
            if figure is None:
                continue
            key = str(figure.id)
            current = linked.get(key)
            if current is None:
                current = self._serialize(figure, matched_by=matched_by)
                linked[key] = current
            if evidence_id:
                current["evidence_ids"] = list(
                    dict.fromkeys([*(current.get("evidence_ids") or []), evidence_id])
                )
            if matched_by == "evidence_locator":
                current["matched_by"] = "evidence_locator"
        return list(linked.values())

    def _serialize(self, figure: PaperFigure, *, matched_by: str) -> dict[str, Any]:
        cached = self._serialized_by_figure.get(str(figure.id))
        if cached is not None:
            return {**cached, "matched_by": matched_by, "evidence_ids": []}
        latest_review = self.session.scalars(
            select(AuditLog)
            .where(
                AuditLog.paper_id == figure.paper_id,
                AuditLog.action == "review_figure",
                AuditLog.target_id == str(figure.id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        ).first()
        snapshot = {
            "figure_id": str(figure.id),
            "write_version": int(figure.write_version or 1),
            "figure_label": figure.figure_label,
            "caption": figure.caption,
            "page": figure.page,
            "image_path": figure.image_path,
            "figure_role": figure.figure_role,
            "content_summary": figure.content_summary,
            "key_elements": figure.key_elements or [],
            "crop_status": figure.crop_status,
            "review_id": str(latest_review.id) if latest_review is not None else None,
            "review_payload": latest_review.payload if latest_review is not None else None,
        }
        fingerprint = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        payload = {
            "figure_id": str(figure.id),
            "figure_label": figure.figure_label,
            "page": figure.page,
            "caption": figure.caption,
            "figure_role": figure.figure_role,
            "content_summary": figure.content_summary,
            "image_path": figure.image_path,
            "asset_url": f"/api/papers/assets/{figure.image_path}" if figure.image_path else None,
            "relation": "supports",
            "review_snapshot_fingerprint": fingerprint,
        }
        self._serialized_by_figure[str(figure.id)] = payload
        return {**payload, "matched_by": matched_by, "evidence_ids": []}

    def _eligible_figures(self, paper_id: Any) -> dict[str, PaperFigure]:
        cache_key = str(paper_id)
        cached = self._eligible_by_paper.get(cache_key)
        if cached is not None:
            return cached
        figures = list(
            self.session.scalars(
                select(PaperFigure)
                .where(PaperFigure.paper_id == paper_id)
                .order_by(PaperFigure.page, PaperFigure.figure_label, PaperFigure.id)
            ).all()
        )
        eligible = {
            str(figure.id): figure
            for figure in figures
            if figure_is_rag_eligible(self.session, figure)
            and figure_has_safe_review(self.session, figure)
        }
        self._eligible_by_paper[cache_key] = eligible
        return eligible

    @staticmethod
    def _figure_logic(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if isinstance(value, dict):
            value = [value]
        return [item for item in (value or []) if isinstance(item, dict)]

    @staticmethod
    def _writing_locator_evidence_id(locator: EvidenceLocator) -> str:
        match = re.search(r":evidence:(\d+)$", str(locator.chunk_id or ""))
        if match:
            return f"evidence_chain:{match.group(1)}"
        return f"evidence_locator:{locator.id}"

    @classmethod
    def _attach_figure_logic_context(
        cls,
        links: list[dict[str, Any]],
        figure_logic: Any,
    ) -> None:
        """Add purpose text only after evidence has independently created a link."""

        logic_items = cls._figure_logic(figure_logic)
        for link in links:
            link_aliases = {
                str(link.get("figure_id") or "").strip(),
                *cls._figure_aliases(link.get("figure_label"), link.get("caption")),
            }
            for item in logic_items:
                reference = str(item.get("figure_id") or item.get("fig_id") or "").strip()
                if not reference:
                    continue
                reference_aliases = {reference, *cls._reference_aliases(reference)}
                if link_aliases.isdisjoint(reference_aliases):
                    continue
                purpose = str(item.get("purpose") or "").strip()
                supports_claim = str(item.get("supports_claim") or "").strip()
                if purpose:
                    link["purpose"] = purpose
                if supports_claim:
                    link["supports_claim"] = supports_claim
                break

    @staticmethod
    def _explicit_references(text: str) -> list[str]:
        return [f"Figure {match.group('label')}" for match in _FIGURE_REFERENCE_RE.finditer(text or "")]

    @classmethod
    def _figure_aliases(cls, label: str | None, caption: str | None) -> set[str]:
        aliases: set[str] = set()
        for value in (label, caption):
            aliases.update(cls._reference_aliases(value or ""))
        return aliases

    @staticmethod
    def _reference_aliases(value: str) -> set[str]:
        normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
        aliases = {normalized} if normalized else set()
        match = _FIGURE_REFERENCE_RE.search(str(value or ""))
        if match:
            label = match.group("label").casefold()
            aliases.add(f"figure{label}")
            aliases.add(f"fig{label}")
        return aliases
