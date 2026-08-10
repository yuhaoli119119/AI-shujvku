from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import EvidenceClaim, Paper, PaperSection
from app.schemas.ai_verification import SectionPageFragmentCandidateRef
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.services.evidence_page_recovery import EvidencePageRecoveryService
from app.utils.review_safety import compact_page_text, content_object_gate


class SectionPageFragmentMaterializationService:
    """Safely materialize deterministic PDF page-fragment candidates.

    Clients submit only opaque IDs and fingerprints.  All authoritative text,
    page, parent, and scientific-consistency checks are rebuilt from the stored
    paper, parent section, and real PDF before any row is staged.
    """

    MAX_BATCH_SIZE = 20
    RECOVERY_LIMIT = 100
    REQUIRED_RECOVERY_CHECKS = frozenset(
        {
            "single_physical_pdf_page",
            "text_exists_on_pdf_page",
            "exact_alphanumeric_sequence",
            "numeric_consistency",
            "unit_consistency",
            "chemical_and_entity_sequence_consistent",
            "direction_consistency",
        }
    )

    def __init__(self, session: Session) -> None:
        self.session = session

    def materialize(
        self,
        *,
        paper_id: UUID,
        parent_section_id: UUID,
        candidates: Iterable[SectionPageFragmentCandidateRef | dict[str, Any]],
        identity: AuthenticatedAIVerificationIdentity,
        dry_run: bool = True,
        commit: bool = True,
    ) -> dict[str, Any]:
        AIVerificationService._require_identity(identity)
        refs = [
            item
            if isinstance(item, SectionPageFragmentCandidateRef)
            else SectionPageFragmentCandidateRef.model_validate(item)
            for item in candidates
        ]
        if not refs:
            raise ValueError("At least one section page-fragment candidate is required")
        if len(refs) > self.MAX_BATCH_SIZE:
            raise ValueError(f"Section page-fragment batch exceeds limit {self.MAX_BATCH_SIZE}")

        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        section = self.session.get(PaperSection, parent_section_id)
        if section is None or section.paper_id != paper.id:
            raise LookupError("Parent section not found for paper")

        duplicate_ids: set[str] = set()
        seen_refs: dict[str, str] = {}
        for ref in refs:
            previous = seen_refs.setdefault(ref.fragment_id, ref.fragment_fingerprint)
            if previous != ref.fragment_fingerprint:
                return self._rejected(
                    paper_id,
                    parent_section_id,
                    refs,
                    reason="conflicting_duplicate_fragment_reference",
                    dry_run=dry_run,
                )
            if previous == ref.fragment_fingerprint and ref.fragment_id in duplicate_ids:
                continue
            if sum(item.fragment_id == ref.fragment_id for item in refs) > 1:
                duplicate_ids.add(ref.fragment_id)

        recovered = EvidencePageRecoveryService(self.session).recover_section_page_fragments(
            paper=paper,
            section=section,
            limit=self.RECOVERY_LIMIT,
        )
        recovered_by_id = {
            str(item["fragment_id"]): item for item in recovered.get("fragments", [])
        }

        validated: list[tuple[SectionPageFragmentCandidateRef, dict[str, Any], EvidenceClaim | None]] = []
        validation_errors: list[dict[str, Any]] = []
        processed_ids: set[str] = set()
        for ref in refs:
            if ref.fragment_id in processed_ids:
                continue
            processed_ids.add(ref.fragment_id)
            fragment = recovered_by_id.get(ref.fragment_id)
            reasons = self._candidate_rejection_reasons(
                paper=paper,
                section=section,
                ref=ref,
                fragment=fragment,
            )
            existing: EvidenceClaim | None = None
            try:
                fragment_uuid = UUID(ref.fragment_id)
            except (TypeError, ValueError):
                reasons.append("invalid_fragment_id")
            else:
                existing = self.session.get(EvidenceClaim, fragment_uuid)
                if existing is not None and fragment is not None:
                    reasons.extend(self._existing_claim_mismatch_reasons(existing, paper, section, fragment))
            if reasons:
                validation_errors.append(
                    {
                        "fragment_id": ref.fragment_id,
                        "status": "rejected",
                        "blocked_reasons": list(dict.fromkeys(reasons)),
                        "database_writes": False,
                    }
                )
            elif fragment is not None:
                validated.append((ref, fragment, existing))

        if validation_errors:
            rejected_ids = {item["fragment_id"] for item in validation_errors}
            items = validation_errors + [
                {
                    "fragment_id": ref.fragment_id,
                    "status": "not_materialized",
                    "blocked_reasons": ["atomic_batch_rejected"],
                    "database_writes": False,
                }
                for ref, _fragment, _existing in validated
                if ref.fragment_id not in rejected_ids
            ]
            return self._result(
                paper_id,
                parent_section_id,
                dry_run=dry_run,
                status="rejected",
                items=items,
                recovered=recovered,
                database_writes=False,
            )

        new_claims: list[EvidenceClaim] = []
        for _ref, fragment, existing in validated:
            if existing is not None:
                continue
            claim = EvidencePageRecoveryService.fragment_claim(
                paper=paper,
                section=section,
                fragment=fragment,
            )
            claim.meta = {
                **(claim.meta or {}),
                "candidate_status": "pending",
                "materialization_policy": "section_page_fragment_materialization.v1",
                "single_ai": True,
                "second_ai_used": False,
                "embedding_role": "retrieval_only",
            }
            new_claims.append(claim)
        if dry_run:
            items = [
                self._item(existing, fragment, dry_run=True, existed_before=existing is not None)
                for _ref, fragment, existing in validated
            ]
            return self._result(
                paper_id,
                parent_section_id,
                dry_run=True,
                status="dry_run",
                items=items,
                recovered=recovered,
                database_writes=False,
            )

        if new_claims:
            with self.session.begin_nested():
                self.session.add_all(new_claims)
                self.session.flush()
        if commit:
            self.session.commit()

        persisted_by_id = {
            str(claim.id): claim
            for claim in self.session.query(EvidenceClaim).filter(
                EvidenceClaim.id.in_([UUID(ref.fragment_id) for ref, _fragment, _existing in validated])
            )
        }
        items = [
            self._item(
                persisted_by_id[str(fragment["fragment_id"])],
                fragment,
                dry_run=False,
                existed_before=existing is not None,
            )
            for _ref, fragment, existing in validated
        ]
        return self._result(
            paper_id,
            parent_section_id,
            dry_run=False,
            status="materialized",
            items=items,
            recovered=recovered,
            database_writes=bool(new_claims),
        )

    def _candidate_rejection_reasons(
        self,
        *,
        paper: Paper,
        section: PaperSection,
        ref: SectionPageFragmentCandidateRef,
        fragment: dict[str, Any] | None,
    ) -> list[str]:
        if fragment is None:
            return ["candidate_not_recovered_or_stale"]
        reasons: list[str] = []
        if str(fragment.get("fragment_fingerprint") or "") != ref.fragment_fingerprint:
            reasons.append("stale_fragment_fingerprint")
        if str(fragment.get("source_type") or "") != "section_page_fragment":
            reasons.append("invalid_source_type")
        if str(fragment.get("parent_section_id") or "") != str(section.id):
            reasons.append("parent_section_mismatch")
        if paper.id != section.paper_id:
            reasons.append("paper_section_mismatch")
        page = fragment.get("page")
        if not isinstance(page, int) or page < 1 or page != fragment.get("page_start") or page != fragment.get("page_end"):
            reasons.append("invalid_physical_page")
        if str(fragment.get("locator_status") or "") != "exact_page":
            reasons.append("approximate_candidate_not_materializable")
        text = str(fragment.get("text") or "")
        if not text or compact_page_text(text) not in compact_page_text(section.text):
            reasons.append("fragment_not_in_parent_section")
        checks = fragment.get("checks") if isinstance(fragment.get("checks"), dict) else {}
        for check in sorted(self.REQUIRED_RECOVERY_CHECKS):
            if checks.get(check) is not True:
                reasons.append(f"recovery_check_failed:{check}")
        return reasons

    @staticmethod
    def _existing_claim_mismatch_reasons(
        claim: EvidenceClaim,
        paper: Paper,
        section: PaperSection,
        fragment: dict[str, Any],
    ) -> list[str]:
        meta = claim.meta if isinstance(claim.meta, dict) else {}
        checks = {
            "existing_fragment_paper_mismatch": claim.paper_id == paper.id,
            "existing_fragment_parent_mismatch": claim.section_id == section.id,
            "existing_fragment_source_type_mismatch": claim.source_type == "section_page_fragment",
            "existing_fragment_target_type_mismatch": claim.target_type == "sections",
            "existing_fragment_target_id_mismatch": claim.target_id == str(section.id),
            "existing_fragment_text_mismatch": (
                claim.claim_text == fragment["text"] and claim.evidence_text == fragment["text"]
            ),
            "existing_fragment_page_mismatch": (
                claim.page_start == fragment["page"] and claim.page_end == fragment["page"]
            ),
            "existing_fragment_fingerprint_mismatch": (
                meta.get("fragment_fingerprint") == fragment["fragment_fingerprint"]
            ),
        }
        return [reason for reason, passed in checks.items() if not passed]

    def _item(
        self,
        claim: EvidenceClaim | None,
        fragment: dict[str, Any],
        *,
        dry_run: bool,
        existed_before: bool,
    ) -> dict[str, Any]:
        persisted = claim is not None
        gate = content_object_gate(self.session, "section_page_fragments", claim) if persisted else None
        return {
            "fragment_id": str(fragment["fragment_id"]),
            "fragment_fingerprint": fragment["fragment_fingerprint"],
            "source_type": "section_page_fragment",
            "parent_section_id": fragment["parent_section_id"],
            "page": fragment["page"],
            "status": "existing" if existed_before else ("would_materialize_pending" if dry_run else "pending"),
            "validation_status": claim.validation_status if persisted else "unverified",
            "review_status": (
                gate.review_gate_status if gate is not None else "unreviewed"
            ),
            "can_use_for_writing": bool(gate.can_use_for_writing) if gate is not None else False,
            "can_use_for_citation": bool(gate.can_use_for_citation) if gate is not None else False,
            "idempotent": existed_before,
            "database_writes": False if dry_run or existed_before else True,
        }

    @staticmethod
    def _result(
        paper_id: UUID,
        parent_section_id: UUID,
        *,
        dry_run: bool,
        status: str,
        items: list[dict[str, Any]],
        recovered: dict[str, Any],
        database_writes: bool,
    ) -> dict[str, Any]:
        return {
            "paper_id": str(paper_id),
            "parent_section_id": str(parent_section_id),
            "status": status,
            "dry_run": dry_run,
            "requested": len(items),
            "materialized": sum(item.get("status") == "pending" for item in items),
            "would_materialize": sum(item.get("status") == "would_materialize_pending" for item in items),
            "idempotent": sum(bool(item.get("idempotent")) for item in items),
            "rejected": sum(item.get("status") == "rejected" for item in items),
            "items": items,
            "recovery_status": recovered.get("status"),
            "single_ai": True,
            "second_ai_used": False,
            "embedding_requests": 0,
            "embedding_role": "retrieval_only",
            "candidate_state": "pending_unreviewed",
            "database_writes": database_writes,
        }

    @classmethod
    def _rejected(
        cls,
        paper_id: UUID,
        parent_section_id: UUID,
        refs: list[SectionPageFragmentCandidateRef],
        *,
        reason: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        return cls._result(
            paper_id,
            parent_section_id,
            dry_run=dry_run,
            status="rejected",
            items=[
                {
                    "fragment_id": ref.fragment_id,
                    "status": "rejected",
                    "blocked_reasons": [reason],
                    "database_writes": False,
                }
                for ref in refs
            ],
            recovered={"status": "not_run"},
            database_writes=False,
        )
