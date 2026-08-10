from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EvidenceClaim, MechanismClaim, PaperSection, WritingCard
from app.utils.review_safety import content_object_gate, get_target_reviews


DECISION_REVIEW_STATUSES = frozenset(
    {"ai_verified", "verified", "exception", "needs_human", "rejected"}
)
EXCEPTION_REVIEW_STATUSES = frozenset({"exception", "needs_human"})
ACTIVE_REVIEW_RESOLUTIONS = frozenset({"active", "remapped"})


class ContentReviewCoverageService:
    """Authoritative source-object coverage shared by API, UI, and MCP.

    ContentEvidenceItem is deliberately absent here.  It is a search projection,
    not a review decision or admission source of truth.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def paper_coverage(self, paper_id: Any) -> dict[str, Any]:
        sections = list(
            self.session.scalars(
                select(PaperSection)
                .where(PaperSection.paper_id == paper_id)
                .order_by(PaperSection.id.asc())
            ).all()
        )
        mechanism_claims = list(
            self.session.scalars(
                select(MechanismClaim)
                .where(MechanismClaim.paper_id == paper_id)
                .order_by(MechanismClaim.id.asc())
            ).all()
        )
        writing_cards = list(
            self.session.scalars(
                select(WritingCard)
                .where(WritingCard.paper_id == paper_id)
                .order_by(WritingCard.id.asc())
            ).all()
        )
        section_page_fragments = list(
            self.session.scalars(
                select(EvidenceClaim)
                .where(EvidenceClaim.paper_id == paper_id)
                .where(EvidenceClaim.source_type == "section_page_fragment")
                .order_by(EvidenceClaim.id.asc())
            ).all()
        )
        return {
            "sections": self.summarize(
                sections,
                "sections",
                subtype_attr="section_type",
                subtype_key="by_section_type",
            ),
            "mechanism_claims": self.summarize(mechanism_claims, "mechanism_claims"),
            "writing_cards": self.summarize(writing_cards, "writing_cards"),
            "section_page_fragments": self.summarize(
                section_page_fragments,
                "section_page_fragments",
            ),
            "coverage_basis": (
                "canonical_source_objects+active_extraction_field_reviews+content_object_gate"
            ),
            "projection_cache_authoritative": False,
        }

    def summarize(
        self,
        rows: Iterable[Any],
        target_type: str,
        *,
        subtype_attr: str | None = None,
        subtype_key: str | None = None,
    ) -> dict[str, Any]:
        row_list = list(rows)
        counters: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        details: list[dict[str, Any]] = []
        subtype_counters: dict[str, Counter[str]] = defaultdict(Counter)

        for row in row_list:
            gate = content_object_gate(self.session, target_type, row)
            reviews = get_target_reviews(
                self.session,
                paper_id=row.paper_id,
                target_type=target_type,
                target_id=row.id,
            )
            active_reviews = [
                review
                for review in reviews
                if str(review.target_resolution_status or "active").strip().casefold()
                in ACTIVE_REVIEW_RESOLUTIONS
            ]
            statuses = {
                str(review.reviewer_status or "pending").strip().casefold()
                for review in active_reviews
            }
            decision_recorded = bool(statuses & DECISION_REVIEW_STATUSES)
            authoritative = gate.review_gate_status == "safe_verified"
            ai_verified = authoritative and "ai_verified" in statuses
            human_verified = authoritative and "verified" in statuses
            exception = bool(statuses & EXCEPTION_REVIEW_STATUSES) and not authoritative
            rejected = "rejected" in statuses and not authoritative
            can_write = bool(gate.can_use_for_writing)
            can_cite = bool(gate.can_use_for_citation)
            blocked = not (can_write or can_cite)
            category = (
                "ai_verified"
                if ai_verified
                else "human_verified"
                if human_verified
                else "exception"
                if exception
                else "rejected"
                if rejected
                else "reviewed_blocked"
                if decision_recorded
                else "unreviewed"
            )

            values = {
                "ai_verified": int(ai_verified),
                "human_verified": int(human_verified),
                "verified": int(authoritative),
                "exception": int(exception),
                "rejected": int(rejected),
                "decision_recorded": int(decision_recorded),
                "reviewed": int(decision_recorded),
                "unreviewed": int(not decision_recorded),
                "authoritative_reviewed": int(authoritative),
                "blocked": int(blocked),
                "can_use_for_writing": int(can_write),
                "can_use_for_citation": int(can_cite),
            }
            counters.update(values)
            for reason in gate.blocked_reasons:
                reason_counts[str(reason)] += 1

            subtype = None
            if subtype_attr:
                subtype = str(getattr(row, subtype_attr, None) or "unknown").strip() or "unknown"
                subtype_counters[subtype].update(values)

            details.append(
                {
                    "object_id": str(row.id),
                    "review_gate_status": gate.review_gate_status,
                    "locator_status": gate.locator_status,
                    "can_use_for_writing": can_write,
                    "can_use_for_citation": can_cite,
                    "verification_category": category,
                    "decision_recorded": decision_recorded,
                    "field_review_statuses": sorted(statuses),
                    "blocked_reasons": list(gate.blocked_reasons),
                    **({"subtype": subtype} if subtype is not None else {}),
                }
            )

        summary: dict[str, Any] = {
            "total": len(row_list),
            "ai_verified": counters["ai_verified"],
            "human_verified": counters["human_verified"],
            "verified": counters["verified"],
            "exception": counters["exception"],
            "rejected": counters["rejected"],
            "decision_recorded": counters["decision_recorded"],
            "reviewed": counters["reviewed"],
            "unreviewed": counters["unreviewed"],
            "pending": counters["unreviewed"],
            "authoritative_reviewed": counters["authoritative_reviewed"],
            "blocked": counters["blocked"],
            "can_use_for_writing": counters["can_use_for_writing"],
            "can_use_for_citation": counters["can_use_for_citation"],
            "blocked_reasons": dict(sorted(reason_counts.items())),
            "decision_basis": "active_extraction_field_review",
            "eligibility_basis": "content_object_gate",
            "unreviewed_semantics": "no_active_explicit_decision",
            "authoritative_reviewed_semantics": "content_object_gate_safe_verified_only",
            "details": details,
        }
        if subtype_key:
            summary[subtype_key] = {
                subtype: {
                    "total": sum(counts[key] for key in ("decision_recorded", "unreviewed")),
                    "ai_verified": counts["ai_verified"],
                    "human_verified": counts["human_verified"],
                    "verified": counts["verified"],
                    "exception": counts["exception"],
                    "rejected": counts["rejected"],
                    "decision_recorded": counts["decision_recorded"],
                    "reviewed": counts["reviewed"],
                    "unreviewed": counts["unreviewed"],
                    "authoritative_reviewed": counts["authoritative_reviewed"],
                    "blocked": counts["blocked"],
                    "can_use_for_writing": counts["can_use_for_writing"],
                    "can_use_for_citation": counts["can_use_for_citation"],
                }
                for subtype, counts in sorted(subtype_counters.items())
            }
        return summary
