from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    EvidenceClaim,
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperRelationship,
    PaperTable,
    PaperSection,
)
from app.normalizers.chemistry_normalizer import get_property_taxonomy
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.services.dft_audit_issue_lifecycle_service import DFT_AUDIT_ISSUE_PENDING_STATUSES
from app.services.content_knowledge_service import ContentKnowledgeService
from app.services.evidence_page_recovery import EvidencePageRecoveryService, compact_page_text
from app.utils.artifact_paths import resolve_paper_pdf_path
from app.utils.ai_verification import (
    AI_VERIFICATION_CAPABILITY,
    AI_VERIFICATION_POLICY_VERSION,
    ai_field_snapshot,
    ai_target_fingerprint,
    build_structured_table_cell_evidence,
    build_structured_table_cell_evidence_index,
    cached_read_pdf_page_text,
    canonical_ai_target_type,
    get_ai_target,
    locator_fingerprint,
    matching_locator,
    normalize_evidence_text,
    read_pdf_page_text,
    stable_hash,
    structured_table_cell_evidence_valid,
)
from app.utils.configuration_index import extract_configuration_index
from app.services.review_target_resolver import get_dft_catalyst_identity, preload_dft_catalyst_identity
from app.utils.review_safety import (
    authoritative_gate_scope,
    is_authoritative_verified_review,
    is_safe_verified_review,
    required_review_fields,
    writing_card_authoritative_chain_gate,
)


@dataclass(frozen=True)
class AuthenticatedAIVerificationIdentity:
    source_identity: str
    source_label: str
    model_agent: str
    capabilities: frozenset[str]
    identity_verified: bool


_FEN4_P_OR_S_DG_VARIANT = re.compile(r"^fen4[ps][12][12]dg$")
_B0102_NON_CATALYST_CONTROLS = frozenset({"graphene", "2dme", "2dol", "dmedol"})
_FEN4_P2_S2_DG_FAMILY_ON_PAGE = re.compile(
    r"(?<![A-Za-z0-9])edge\s*[- ]\s*type\s+Fe\s*N\s*4\s*P\s*2\s*/\s*S\s*2\s*[- ]\s*DG(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _catalyst_name_is_supported_by_evidence(catalyst_name: str, evidence_text: str) -> bool:
    """Match an exact catalyst name, or B0102's explicitly defined FeN4 P/S family.

    The latter is deliberately narrow: the paper's main text defines eight
    edge-type FeN4P2/S2-DG isomers as one SAC family, while its SI gives the
    individual P1,1/P1,2/P2,1/P2,2 and S1,1/S1,2/S2,1/S2,2 labels.  Requiring
    every individual label on the main-text page would reject valid evidence;
    accepting an arbitrary similarly named material would be unsafe.
    """
    name = str(catalyst_name or "").strip()
    if not name:
        return False
    compact_name = re.sub(r"[^a-z0-9]", "", name.casefold())
    if compact_name in _B0102_NON_CATALYST_CONTROLS:
        return False
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", evidence_text, re.I):
        return True
    return bool(
        _FEN4_P_OR_S_DG_VARIANT.fullmatch(compact_name)
        and _FEN4_P2_S2_DG_FAMILY_ON_PAGE.search(evidence_text)
    )


class AIVerificationService:
    """Single-AI admission service with deterministic evidence rechecks.

    The caller supplies one AI judgment. This service never calls another model,
    never asks for consensus, and never maps the caller to a human/Owner identity.
    """

    MAX_TASK_PAGE_SIZE = 50
    MAX_RECORD_BUNDLE_PAGE_SIZE = 50
    MAX_SUBMISSION_BATCH_SIZE = 20
    _NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")
    _WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+./-]{2,}")
    _STOPWORDS = {
        "the", "and", "for", "that", "with", "this", "from", "were", "was", "are",
        "into", "using", "used", "study", "result", "results", "show", "shows", "can",
    }

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    @property
    def batch_limit(self) -> int:
        return max(
            1,
            min(
                self.MAX_SUBMISSION_BATCH_SIZE,
                int(self.settings.ai_verification_batch_limit),
            ),
        )

    def list_tasks(
        self,
        *,
        paper_id: UUID,
        limit: int = 20,
        offset: int = 0,
        recover_evidence: bool = True,
        target_type: str | None = None,
        include_blocked: bool = False,
    ) -> dict[str, Any]:
        with authoritative_gate_scope(self.session):
            return self._list_tasks_unscoped(
                paper_id=paper_id,
                limit=limit,
                offset=offset,
                recover_evidence=recover_evidence,
                target_type=target_type,
                include_blocked=include_blocked,
            )

    def _list_tasks_unscoped(
        self,
        *,
        paper_id: UUID,
        limit: int = 20,
        offset: int = 0,
        recover_evidence: bool = True,
        target_type: str | None = None,
        include_blocked: bool = False,
    ) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        bounded = max(1, min(int(limit), self.MAX_TASK_PAGE_SIZE))
        page_offset = int(offset)
        if page_offset < 0:
            raise ValueError("offset must be greater than or equal to zero")
        tasks: list[dict[str, Any]] = []
        supported_target_types = (
            "mechanism_claims",
            "dft_results",
            "electrochemical_performance",
            "sections",
            "section_page_fragments",
            "writing_cards",
        )
        from app.utils.ai_verification import _TARGET_MODELS  # internal policy registry

        normalized_target_type = str(target_type or "").strip() or None
        if normalized_target_type is not None:
            if normalized_target_type not in supported_target_types:
                raise ValueError(f"Unsupported AI verification target_type: {normalized_target_type}")
            active_target_types = [normalized_target_type]
        else:
            active_target_types = list(supported_target_types)

        target_rows_by_type: dict[str, list[Any]] = {}
        for current_target_type in active_target_types:
            target_model = _TARGET_MODELS[current_target_type]
            target_query = select(target_model).where(target_model.paper_id == paper_id)
            if current_target_type == "section_page_fragments":
                target_query = target_query.where(EvidenceClaim.source_type == "section_page_fragment")
            target_rows_by_type[current_target_type] = self.session.scalars(
                target_query.order_by(target_model.id.asc())
            ).all()
        preload_dft_catalyst_identity(self.session, target_rows_by_type.get("dft_results", []))

        all_target_ids = {str(target.id) for rows in target_rows_by_type.values() for target in rows}
        reviews_by_key: dict[tuple[str, str, str], ExtractionFieldReview] = {}
        if all_target_ids:
            for review in self.session.scalars(
                select(ExtractionFieldReview).where(
                    ExtractionFieldReview.paper_id == paper_id,
                    ExtractionFieldReview.target_id.in_(all_target_ids),
                )
            ).all():
                try:
                    canonical = canonical_ai_target_type(review.target_type)
                except ValueError:
                    continue
                reviews_by_key[(canonical, str(review.target_id), str(review.field_name or ""))] = review

        related_ids = self.session.scalars(
            select(PaperRelationship.source_paper_id).where(
                PaperRelationship.target_paper_id == paper_id,
                PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
            ).union_all(
                select(PaperRelationship.target_paper_id).where(
                    PaperRelationship.source_paper_id == paper_id,
                    PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                )
            )
        ).all() if recover_evidence else []
        evidence_papers = [paper]
        evidence_papers.extend(
            related for related in (self.session.get(Paper, rel_id) for rel_id in related_ids)
            if related is not None and related.id != paper.id
        )

        pending_targets: list[tuple[str, str, Any, ExtractionFieldReview | None]] = []
        for current_target_type in active_target_types:
            rows = target_rows_by_type[current_target_type]
            for target in rows:
                req_fields = required_review_fields(current_target_type, target)
                if not req_fields:
                    default_field = {
                        "mechanism_claims": "claim_text",
                        "electrochemical_performance": "capacity",
                        "sections": "text",
                        "section_page_fragments": "text",
                        "writing_cards": "evidence_chain",
                    }.get(current_target_type, "value")
                    req_fields = (default_field,)

                for field_name in req_fields:
                    existing = reviews_by_key.get((current_target_type, str(target.id), field_name))
                    if existing is not None and is_authoritative_verified_review(self.session, existing, target):
                        continue
                    if (
                        existing is not None
                        and self._ai_blocked_review_is_current(
                            paper_id=paper_id,
                            target_type=current_target_type,
                            target=target,
                            field_name=field_name,
                            review=existing,
                        )
                        and not include_blocked
                    ):
                        continue
                    pending_targets.append((current_target_type, field_name, target, existing))

        total = len(pending_targets)
        page_targets = pending_targets[page_offset : page_offset + bounded]
        recovery_service = EvidencePageRecoveryService(self.session, self.settings)
        recovery_statuses: Counter[str] = Counter()
        exact_candidate_count = 0
        candidate_count = 0
        page_fragment_count = 0

        for current_target_type, field_name, target, existing in page_targets:
            snapshot = ai_field_snapshot(current_target_type, target, field_name)
            if recover_evidence:
                recoveries = [
                    recovery_service.recover_for_target(
                        paper=evidence_paper,
                        target_type=current_target_type,
                        target_id=str(target.id),
                        field_name=field_name,
                        target_value=snapshot.get("value"),
                        evidence_text=str(snapshot.get("evidence_text") or ""),
                        evidence_types=list(getattr(target, "evidence_types", None) or []),
                        limit=3,
                    )
                    for evidence_paper in evidence_papers
                ]
                candidates = []
                for evidence_paper, source_recovery in zip(evidence_papers, recoveries, strict=True):
                    for candidate in source_recovery["candidates"]:
                        candidates.append({
                            **candidate,
                            "evidence_paper_id": str(evidence_paper.id),
                            "source_paper_id": str(evidence_paper.id),
                        })
                candidates.sort(key=lambda item: (-float(item.get("match_score") or 0), str(item["evidence_paper_id"]), int(item.get("page") or 0)))
                candidates = candidates[:3]
                recovery = {
                    "status": "recovered" if any(item.get("status") in {"recovered", "existing_exact"} for item in recoveries) else "no_supporting_evidence",
                    "candidate_count": sum(int(item.get("candidate_count") or 0) for item in recoveries),
                    "exact_candidate_count": sum(int(item.get("exact_candidate_count") or 0) for item in recoveries),
                    "blocked_reasons": sorted({reason for item in recoveries for reason in item.get("blocked_reasons", [])}),
                    "evidence_paper_ids": [str(item.id) for item in evidence_papers],
                    "database_writes": False,
                }
            else:
                locators = self._candidate_locators(
                    paper_id,
                    current_target_type,
                    str(target.id),
                    field_name,
                )
                candidates = [
                    {
                        "page": locator.page,
                        "quoted_text": locator.evidence_text,
                        "evidence_text": locator.evidence_text,
                        "locator_status": locator.locator_status,
                        "source_type": locator.source_type,
                        "extraction_source": locator.parser_source,
                        "match_method": "persisted_locator",
                        "warning_reason": None,
                        "evidence_paper_id": str(locator.paper_id),
                        "source_paper_id": str(locator.paper_id),
                    }
                    for locator in locators[:3]
                ]
                recovery = {
                    "status": "disabled",
                    "candidate_count": len(candidates),
                    "exact_candidate_count": sum(
                        item.get("locator_status") in {"exact_page", "exact_bbox"}
                        for item in candidates
                    ),
                    "candidates": candidates,
                    "blocked_reasons": [],
                    "page_text_statuses": {},
                    "database_writes": False,
                }
            recovery_statuses[str(recovery["status"])] += 1
            candidate_count += int(recovery["candidate_count"])
            exact_candidate_count += int(recovery["exact_candidate_count"])
            page_fragment_recovery = None
            if (
                recover_evidence
                and
                current_target_type == "sections"
                and str(getattr(target, "section_type", "") or "").casefold() == "body"
            ):
                page_fragment_recovery = recovery_service.recover_section_page_fragments(
                    paper=paper,
                    section=target,
                    limit=50,
                )
                page_fragment_count += int(page_fragment_recovery.get("fragment_count") or 0)
            task = {
                "paper_id": str(paper_id),
                "target_type": current_target_type,
                "target_id": str(target.id),
                "field_name": field_name,
                "target_snapshot": snapshot,
                "target_snapshot_fingerprint": ai_target_fingerprint(current_target_type, target),
                "expected_write_version": int(existing.write_version or 1) if existing else None,
                "current_status": existing.reviewer_status if existing else "pending",
                "evidence_candidates": candidates,
                "evidence_recovery": {
                    key: value for key, value in recovery.items() if key != "candidates"
                },
            }
            if page_fragment_recovery is not None:
                task["section_page_fragment_recovery"] = page_fragment_recovery
            tasks.append(task)

        returned = len(tasks)
        next_offset = page_offset + returned
        has_more = next_offset < total
        return {
            "paper_id": str(paper_id),
            "target_type": normalized_target_type,
            "total": total,
            "returned": returned,
            "offset": page_offset,
            "limit": bounded,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "task_count": returned,
            "tasks": tasks,
            "recover_evidence": recover_evidence,
            "include_blocked": include_blocked,
            "evidence_recovery_summary": {
                "status_distribution": dict(sorted(recovery_statuses.items())),
                "candidate_count": candidate_count,
                "exact_candidate_count": exact_candidate_count,
                "section_page_fragment_count": page_fragment_count,
            },
            "batch_limit": self.batch_limit,
            "max_page_size": self.MAX_TASK_PAGE_SIZE,
            "single_ai": True,
            "second_ai_used": False,
            "embedding_requests": 0,
            "embedding_role": "retrieval_only",
            "database_writes": False,
        }

    def list_dft_record_tasks(
        self,
        *,
        paper_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        include_blocked: bool = False,
    ) -> dict[str, Any]:
        """Return record-oriented, read-only DFT verification work.

        This is intentionally a separate interface from ``list_tasks``.  The
        legacy field task list remains offset-compatible; record bundles use a
        UUID keyset cursor so completing a field cannot cause later records to
        move into an already scanned offset.
        """
        with authoritative_gate_scope(self.session):
            paper = self.session.get(Paper, paper_id)
            if paper is None:
                raise LookupError("Paper not found")
            bounded = max(1, min(int(limit), self.MAX_RECORD_BUNDLE_PAGE_SIZE))
            last_id = self._decode_record_cursor(cursor, paper_id)
            # The cursor predicate deliberately belongs in SQL.  Do not load
            # all B0102 rows merely to calculate a page in Python: a sweep may
            # run for hours while fields are being completed or deferred.
            rows = self.session.scalars(
                select(DFTResult)
                .where(
                    DFTResult.paper_id == paper_id,
                    *([DFTResult.id > last_id] if last_id is not None else []),
                )
                .order_by(DFTResult.id.asc())
                .limit(bounded + 1)
            ).all()
            scanned_rows = rows[:bounded]
            has_more = len(rows) > bounded
            preload_dft_catalyst_identity(self.session, rows)
            row_ids = [str(row.id) for row in scanned_rows]
            reviews_by_key: dict[tuple[str, str], list[ExtractionFieldReview]] = {}
            if row_ids:
                for review in self.session.scalars(
                    select(ExtractionFieldReview).where(
                        ExtractionFieldReview.paper_id == paper_id,
                        ExtractionFieldReview.target_id.in_(row_ids),
                    )
                ).all():
                    try:
                        if canonical_ai_target_type(review.target_type) != "dft_results":
                            continue
                    except ValueError:
                        continue
                    reviews_by_key.setdefault((str(review.target_id), str(review.field_name)), []).append(review)

            from app.utils.review_safety import (
                _AUTHORITATIVE_LOCATORS_CACHE_KEY,
                _associated_paper_ids,
                _authoritative_locator_cache_key,
            )

            associated_ids = _associated_paper_ids(self.session, {paper_id})
            papers_by_id = {
                item.id: item
                for item in self.session.scalars(select(Paper).where(Paper.id.in_(associated_ids))).all()
            }
            locators_by_key: dict[tuple[str, str], list[EvidenceLocator]] = {}
            if row_ids:
                for locator in self.session.scalars(
                    select(EvidenceLocator).where(
                        EvidenceLocator.paper_id.in_(associated_ids),
                        EvidenceLocator.target_id.in_(row_ids),
                    ).order_by(EvidenceLocator.page.asc().nulls_last(), EvidenceLocator.id.asc())
                ).all():
                    try:
                        if canonical_ai_target_type(str(locator.target_type or "")) != "dft_results":
                            continue
                    except ValueError:
                        continue
                    locators_by_key.setdefault((str(locator.target_id), str(locator.field_name or "")), []).append(locator)
            table_ids = {locator.table_id for values in locators_by_key.values() for locator in values if locator.table_id}
            # A deferred table cell can be valid before an EvidenceLocator is
            # materialized.  Preload its table here too, but only for the
            # fields represented by this raw keyset page.
            for review_set in reviews_by_key.values():
                for review in review_set:
                    blocked = self._ai_blocked_payload(review)
                    reference = blocked.get("table_evidence") if blocked else None
                    try:
                        if isinstance(reference, dict):
                            table_ids.add(UUID(str(reference.get("table_id") or "")))
                    except (TypeError, ValueError, AttributeError):
                        continue
            tables_by_id = {
                item.id: item
                for item in self.session.scalars(select(PaperTable).where(PaperTable.id.in_(table_ids))).all()
            } if table_ids else {}
            table_evidence_indexes = {
                table_id: build_structured_table_cell_evidence_index(table)
                for table_id, table in tables_by_id.items()
            }
            # Authoritative status is still revalidated per field, but it uses
            # the one locator query above instead of issuing records×fields
            # lookups while assembling a page of bundles.
            authority_locator_cache = self.session.info.setdefault(_AUTHORITATIVE_LOCATORS_CACHE_KEY, {})
            for review_set in reviews_by_key.values():
                for review in review_set:
                    authority_locator_cache[_authoritative_locator_cache_key(review)] = list(
                        locators_by_key.get((str(review.target_id), str(review.field_name or "")), [])
                    )

            record_state: list[tuple[DFTResult, tuple[str, ...], dict[str, str], dict[str, ExtractionFieldReview | None]]] = []
            page_pending_records = 0
            page_pending_fields = 0
            page_blocked_fields = 0
            for row in scanned_rows:
                required_fields = required_review_fields("dft_results", row)
                statuses: dict[str, str] = {}
                field_reviews: dict[str, ExtractionFieldReview | None] = {}
                pending_fields = 0
                for field_name in required_fields:
                    candidates = reviews_by_key.get((str(row.id), field_name), [])
                    field_locators = locators_by_key.get((str(row.id), field_name), [])
                    scope = self._deferred_evidence_scope_fingerprint(
                        paper_id=paper_id,
                        target_type="dft_results",
                        target=row,
                        field_name=field_name,
                        associated_ids=associated_ids,
                        papers_by_id=papers_by_id,
                        locators=field_locators,
                        tables_by_id=tables_by_id,
                    )
                    blocked_scopes: dict[str, str] = {}
                    for candidate_review in candidates:
                        blocked = self._ai_blocked_payload(candidate_review)
                        reference = blocked.get("table_evidence") if blocked else None
                        if isinstance(reference, dict):
                            blocked_scopes[str(candidate_review.id)] = self._deferred_evidence_scope_fingerprint(
                                paper_id=paper_id,
                                target_type="dft_results",
                                target=row,
                                field_name=field_name,
                                associated_ids=associated_ids,
                                papers_by_id=papers_by_id,
                                locators=field_locators,
                                tables_by_id=tables_by_id,
                                table_references=[reference],
                            )
                    review, status = self._select_dft_field_review(
                        candidates, paper_id, row, field_name, scope, blocked_scopes,
                    )
                    field_reviews[field_name] = review
                    statuses[field_name] = status
                    if status == "ai_blocked":
                        page_blocked_fields += 1
                    elif status == "pending":
                        pending_fields += 1
                if pending_fields:
                    page_pending_records += 1
                    page_pending_fields += pending_fields
                record_state.append((row, required_fields, statuses, field_reviews))

            selectable = [
                state for state in record_state
                if any(status == "pending" for status in state[2].values()) or (
                    include_blocked and any(status == "ai_blocked" for status in state[2].values())
                )
            ]
            next_cursor = self._encode_record_cursor(paper_id, scanned_rows[-1].id) if has_more and scanned_rows else None

            shared_page_contexts: dict[tuple[str, int], dict[str, Any]] = {}
            bundles = [
                self._build_dft_record_bundle(
                    paper_id=paper_id,
                    row=row,
                    required_fields=required_fields,
                    statuses=statuses,
                    field_reviews=field_reviews,
                    locators_by_key=locators_by_key,
                    papers_by_id=papers_by_id,
                    tables_by_id=tables_by_id,
                    table_evidence_indexes=table_evidence_indexes,
                    shared_page_contexts=shared_page_contexts,
                )
                for row, required_fields, statuses, field_reviews in selectable
            ]
            return {
                "paper_id": str(paper_id),
                "target_type": "dft_results",
                # Exact global pending counts require evaluating PDF/locator
                # authority for every record and defeat keyset pagination.
                "total_pending_records": None,
                "total_pending_fields": None,
                "pending_count_method": "page_local_authority_checked; global_count_not_scanned",
                "total_records": self.session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)),
                "page_pending_records": page_pending_records,
                "page_pending_fields": page_pending_fields,
                "returned_records": len(bundles),
                "records": bundles,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "page_blocked_fields": page_blocked_fields,
                # Legacy name describes only this raw keyset page.  Keep it
                # temporarily for old clients, with an explicit method label.
                "current_blocked_count": page_blocked_fields,
                "blocked_count_method": "page_local",
                "include_blocked": include_blocked,
                "batch_limit": self.batch_limit,
                "single_ai": True,
                "second_ai_used": False,
                "database_writes": False,
            }

    def build_dft_direct_apply_package(self, *, paper_id: UUID) -> dict[str, Any]:
        """Build the complete, read-only direct-apply package for a web AI.

        This intentionally consumes every keyset page.  In particular, an
        empty selectable page is not completion while it has ``next_cursor``:
        terminal or blocked rows can otherwise appear before remaining pending
        rows.  The package is data, not a downloaded PDF/ZIP cache.
        """
        cursor: str | None = None
        seen_cursors: set[str] = set()
        records: list[dict[str, Any]] = []
        scanned_pages = 0
        while True:
            page = self.list_dft_record_tasks(
                paper_id=paper_id, limit=self.MAX_RECORD_BUNDLE_PAGE_SIZE, cursor=cursor,
            )
            scanned_pages += 1
            records.extend(page["records"])
            next_cursor = page.get("next_cursor")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("dft_record_task_cursor_cycle")
            seen_cursors.add(next_cursor)
            cursor = str(next_cursor)
        return {
            "schema_version": "dft_direct_apply_package.v1",
            "paper_id": str(paper_id),
            "target_type": "dft_results",
            "database_writes": False,
            "record_count": len(records),
            "keyset_pages_scanned": scanned_pages,
            "records": records,
            "apply_tool": {
                "name": "apply_ai_verification_batch",
                "parameters": {
                    "paper_id": "<this paper_id>",
                    "request_id": "<new unique request id>",
                    "submissions": [{
                        "target_type": "dft_results",
                        "target_id": "<record_id>",
                        "field_name": "<pending required field>",
                        "decision": "accept | defer | reject",
                        "confidence": 0.99,
                        "evidence_text": "<quoted PDF/table evidence when accepting>",
                        "page": 1,
                        "expected_target_fingerprint": "<field target_snapshot_fingerprint>",
                        "expected_write_version": 1,
                    }],
                },
            },
            "execution_instructions": [
                "Submit only fields whose current_status is pending; configuration_index is context-only and is never submitted.",
                "Use accept only for evidence that passes the existing deterministic server gates. Use defer with blocked_reasons for insufficient evidence; do not turn technical errors into defer.",
                "The server returns per-item format errors. Correct only those items, with a new request_id; valid items in the original request may already be committed.",
                "If a response times out after submission, call get_ai_verification_batch_receipt with the original request_id before any retry.",
                "This direct protocol replaces no legacy ZIP/import workflow; it does not ask the user to upload a JSON result.",
            ],
        }

    @staticmethod
    def _encode_record_cursor(paper_id: UUID, last_id: UUID) -> str:
        payload = json.dumps(
            {"v": 1, "paper_id": str(paper_id), "last_id": str(last_id)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_record_cursor(cursor: str | None, paper_id: UUID) -> UUID | None:
        if cursor in {None, ""}:
            return None
        try:
            encoded = str(cursor).encode("ascii")
            raw = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("paper_id") != str(paper_id)
            ):
                raise ValueError
            return UUID(str(payload["last_id"]))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid record-task cursor") from exc

    def _build_dft_record_bundle(
        self,
        *,
        paper_id: UUID,
        row: DFTResult,
        required_fields: tuple[str, ...],
        statuses: dict[str, str],
        field_reviews: dict[str, ExtractionFieldReview | None],
        locators_by_key: dict[tuple[str, str], list[EvidenceLocator]],
        papers_by_id: dict[UUID, Paper],
        tables_by_id: dict[UUID, PaperTable],
        table_evidence_indexes: dict[UUID, dict[str, list[dict[str, Any]]]],
        shared_page_contexts: dict[tuple[str, int], dict[str, Any]],
    ) -> dict[str, Any]:
        """Build one read-only bundle while reading each referenced page once."""
        shared_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        fields: list[dict[str, Any]] = []
        target_fingerprint = ai_target_fingerprint("dft_results", row)
        for field_name in required_fields:
            review = field_reviews[field_name]
            candidates: list[dict[str, Any]] = []
            for locator in locators_by_key.get((str(row.id), field_name), []):
                candidate = self._bundle_locator_candidate(
                    locator, papers_by_id, tables_by_id, shared_page_contexts, table_evidence_indexes,
                )
                candidates.append(candidate)
                if candidate["shared_page_key"] is not None:
                    shared_by_key[candidate["shared_page_key"]] = shared_page_contexts[candidate["shared_page_key"]]
            blocked = self._ai_blocked_payload(review) if statuses[field_name] == "ai_blocked" else None
            submission_templates = self._direct_apply_submission_templates(
                row=row,
                field_name=field_name,
                target_fingerprint=target_fingerprint,
                expected_write_version=int(review.write_version or 1) if review is not None else None,
                evidence_candidates=candidates,
            )
            fields.append({
                "field_name": field_name,
                "target_snapshot": ai_field_snapshot("dft_results", row, field_name),
                "target_snapshot_fingerprint": target_fingerprint,
                "expected_write_version": int(review.write_version or 1) if review is not None else None,
                "current_status": statuses[field_name],
                "required": True,
                "evidence_candidates": candidates,
                "submission_templates": submission_templates,
                "blocked_reasons": list(blocked.get("blocked_reasons", [])) if blocked else [],
            })

        value_snapshot = ai_field_snapshot("dft_results", row, "value")
        configuration_index = value_snapshot.get("configuration_index")
        if configuration_index is not None:
            value_review = field_reviews.get("value")
            fields.append({
                "field_name": "configuration_index",
                "verification_field_name": "value",
                "authoritative_with": "value",
                "target_snapshot": {"value": configuration_index},
                "target_snapshot_fingerprint": target_fingerprint,
                "expected_write_version": int(value_review.write_version or 1) if value_review is not None else None,
                "current_status": statuses.get("value", "pending"),
                "required": False,
                "writable": False,
                "evidence_candidates": [
                    self._bundle_locator_candidate(
                        locator, papers_by_id, tables_by_id, shared_page_contexts, table_evidence_indexes,
                    )
                    for locator in locators_by_key.get((str(row.id), "value"), [])
                ],
                "blocked_reasons": [],
            })

        catalyst_snapshot = ai_field_snapshot("dft_results", row, "catalyst")
        record_snapshot = {
            "record_id": str(row.id),
            "catalyst_sample": catalyst_snapshot,
            "adsorbate": row.adsorbate,
            "energy_type": row.property_type,
            "value": row.value,
            "value_upper": row.value_upper,
            "value_kind": row.value_kind,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "configuration_index": configuration_index,
        }
        return {
            "paper_id": str(paper_id),
            "target_type": "dft_results",
            "target_id": str(row.id),
            "record_id": str(row.id),
            "dft_result_snapshot": record_snapshot,
            "catalyst_sample_snapshot": catalyst_snapshot,
            "required_fields": list(required_fields),
            "fields": fields,
            "shared_evidence_candidates": list(shared_by_key.values()),
            "database_writes": False,
        }

    @staticmethod
    def _direct_apply_submission_templates(
        *,
        row: DFTResult,
        field_name: str,
        target_fingerprint: str,
        expected_write_version: int | None,
        evidence_candidates: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Provide fillable decisions with server-owned identity and locator context.

        A caller supplies only its decision-specific evidence/reasoning.  If a
        persisted locator already identifies the paper, page, or table cell,
        that context is copied rather than requiring another task lookup.
        """
        candidate = next(
            (item for item in evidence_candidates if item.get("page") is not None),
            evidence_candidates[0] if evidence_candidates else {},
        )
        common = {
            "target_type": "dft_results",
            "target_id": str(row.id),
            "field_name": field_name,
            "confidence": 0.99,
            "evidence_text": "<quote the supplied page or table-cell evidence>",
            "page": candidate.get("page") or "<source page>",
            "expected_target_fingerprint": target_fingerprint,
            "expected_write_version": expected_write_version,
        }
        for key in ("source_paper_id", "evidence_paper_id"):
            if candidate.get(key) is not None:
                common[key] = candidate[key]
        table_reference = (
            candidate.get("table_id"),
            candidate.get("source_row_index"),
            candidate.get("source_column_index"),
        )
        if candidate.get("table_reference_status") == "resolved" and all(value is not None for value in table_reference):
            common.update({
                "table_id": table_reference[0],
                "source_row_index": table_reference[1],
                "source_column_index": table_reference[2],
            })
        return {
            "accept": {**common, "decision": "accept"},
            "defer": {**common, "decision": "defer", "blocked_reasons": ["<specific missing evidence or gate reason>"]},
            "reject": {**common, "decision": "reject", "counter_evidence_text": "<same-page counter evidence>", "reasoning_summary": "<why the original field is contradicted>"},
        }

    def _bundle_locator_candidate(
        self,
        locator: EvidenceLocator,
        papers_by_id: dict[UUID, Paper],
        tables_by_id: dict[UUID, PaperTable],
        shared_by_key: dict[tuple[str, int], dict[str, Any]],
        table_evidence_indexes: dict[UUID, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        page_key: tuple[str, int] | None = None
        if locator.page is not None and locator.paper_id in papers_by_id:
            page_key = (str(locator.paper_id), int(locator.page))
            if page_key not in shared_by_key:
                page_text, error, _ = cached_read_pdf_page_text(self.session, papers_by_id[locator.paper_id], int(locator.page))
                shared_by_key[page_key] = {
                    "evidence_paper_id": str(locator.paper_id),
                    "page": int(locator.page),
                    "page_text_status": error or "ok",
                    "page_text_excerpt": str(page_text or "")[:2000],
                }
        table_reference = self._bundle_table_reference(locator, tables_by_id, table_evidence_indexes)
        return {
            "locator_id": str(locator.id),
            "evidence_paper_id": str(locator.paper_id),
            "source_paper_id": str(locator.paper_id),
            "page": locator.page,
            "quoted_text": locator.evidence_text,
            "locator_status": locator.locator_status,
            "locator_fingerprint": locator_fingerprint(locator),
            "table_id": str(locator.table_id) if locator.table_id else None,
            **table_reference,
            "source_type": locator.source_type,
            "shared_page_key": page_key,
        }

    def _bundle_table_reference(
        self,
        locator: EvidenceLocator,
        tables_by_id: dict[UUID, PaperTable],
        table_evidence_indexes: dict[UUID, dict[str, list[dict[str, Any]]] | None] | None = None,
    ) -> dict[str, Any]:
        """Expose a table cell only when the persisted locator identifies it exactly."""
        if locator.table_id is None:
            return {"table_reference_status": "not_applicable"}
        table = tables_by_id.get(locator.table_id)
        if table is None or table.paper_id != locator.paper_id or table.page != locator.page:
            return {"table_reference_status": "missing"}
        wanted = normalize_evidence_text(locator.evidence_text)
        if table_evidence_indexes is None:
            table_evidence_indexes = {}
        index = table_evidence_indexes.get(table.id)
        if index is None:
            index = build_structured_table_cell_evidence_index(table)
            table_evidence_indexes[table.id] = index
        matches = index.get(wanted, [])
        if len(matches) != 1:
            return {"table_reference_status": "ambiguous" if matches else "missing"}
        built = matches[0]
        return {
            "table_reference_status": "resolved",
            "table_reference": {
                "table_id": built["table_id"],
                "source_row_index": built["source_row_index"],
                "source_column_index": built["source_column_index"],
            },
            "source_row_index": built["source_row_index"],
            "source_column_index": built["source_column_index"],
            "table_cell": {
                "cell_value": built["cell_value"],
                "column_header": built["column_header"],
                "row_label": built["row_label"],
                "canonical_evidence_text": built["canonical_evidence_text"],
            },
        }

    def _select_dft_field_review(
        self,
        candidates: list[ExtractionFieldReview],
        paper_id: UUID,
        row: DFTResult,
        field_name: str,
        scope_fingerprint: str,
        blocked_scope_fingerprints: dict[str, str] | None = None,
    ) -> tuple[ExtractionFieldReview | None, str]:
        """Collapse historical singular/plural aliases without duplicating a task."""
        ordered = sorted(
            candidates,
            key=lambda review: (int(review.write_version or 1), review.updated_at or review.created_at, str(review.id)),
            reverse=True,
        )
        for review in ordered:
            terminal_status = self.dft_field_terminal_status(
                paper_id=paper_id,
                target_type="dft_results",
                target=row,
                field_name=field_name,
                review=review,
                blocked_scope_fingerprint=(blocked_scope_fingerprints or {}).get(str(review.id), scope_fingerprint),
            )
            if terminal_status is not None:
                return review, terminal_status
        return (ordered[0], "pending") if ordered else (None, "pending")

    def process_batch(
        self,
        *,
        paper_id: UUID,
        submissions: list[AIVerificationSubmission | dict[str, Any]],
        identity: AuthenticatedAIVerificationIdentity,
        dry_run: bool = True,
        commit: bool = True,
    ) -> dict[str, Any]:
        self._require_identity(identity)
        if not submissions:
            raise ValueError("At least one AI verification submission is required")
        if len(submissions) > self.batch_limit:
            raise ValueError(f"AI verification batch exceeds limit {self.batch_limit}")
        if self.session.get(Paper, paper_id) is None:
            raise LookupError("Paper not found")

        items: list[dict[str, Any]] = []
        for raw in submissions:
            submission = raw if isinstance(raw, AIVerificationSubmission) else AIVerificationSubmission.model_validate(raw)
            try:
                if dry_run:
                    items.append(self._process_one(paper_id, submission, identity, dry_run=True))
                    continue
                with self.session.begin_nested():
                    items.append(self._process_one(paper_id, submission, identity, dry_run=False))
            except Exception as exc:
                items.append(
                    {
                        "target_type": submission.target_type,
                        "target_id": submission.target_id,
                        "field_name": submission.field_name,
                        "outcome": "exception",
                        "status": "needs_human",
                        "blocked_reasons": [
                            f"{'validation' if dry_run else 'write'}_failed:{type(exc).__name__}"
                        ],
                        "database_writes": False,
                    }
                )
        if not dry_run:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        counts = {name: sum(item.get("outcome") == name for item in items) for name in (
            "auto_verified", "auto_repaired", "auto_rejected", "auto_deferred", "accept_validation_failed", "exception"
        )}
        return {
            "paper_id": str(paper_id),
            "dry_run": dry_run,
            "single_ai": True,
            "second_ai_used": False,
            "policy_version": AI_VERIFICATION_POLICY_VERSION,
            "capability": AI_VERIFICATION_CAPABILITY,
            "actor_type": "ai",
            "embedding_requests": 0,
            "embedding_role": "retrieval_only",
            **counts,
            "items": items,
            "database_writes": False if dry_run else any(item.get("database_writes") for item in items),
        }

    def _process_one(
        self,
        paper_id: UUID,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        canonical, target = get_ai_target(
            self.session,
            paper_id=paper_id,
            target_type=submission.target_type,
            target_id=submission.target_id,
        )
        required_core_fields = {
            "mechanism_claims": "claim_text",
            "sections": "text",
            "section_page_fragments": "text",
            "writing_cards": "evidence_chain",
        }
        required_field = required_core_fields.get(canonical)
        if required_field is not None and submission.field_name != required_field:
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception",
                status="exception" if canonical in {"sections", "section_page_fragments", "writing_cards"} else "needs_human",
                reasons=[f"{canonical}_requires_{required_field}"], dry_run=dry_run,
            )
        if canonical in {"sections", "section_page_fragments", "writing_cards"} and submission.decision == "correct":
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception", status="exception",
                reasons=["content_object_auto_repair_not_authorized"], dry_run=dry_run,
            )
        if canonical == "dft_results" and submission.field_name not in required_review_fields(canonical, target):
            # configuration_index is display-only in a bundle and is governed
            # by the value review; arbitrary fields must never create reviews.
            return self._submission_exception(canonical, submission, "dft_field_not_required")
        snapshot = ai_field_snapshot(canonical, target, submission.field_name)
        current_fingerprint = ai_target_fingerprint(canonical, target)
        existing = self._find_review(paper_id, canonical, submission.target_id, submission.field_name)
        defer_context: dict[str, Any] | None = None
        defer_scope_fingerprint: str | None = None
        if submission.decision == "defer":
            defer_context, defer_error = self._validate_defer_evidence_context(paper_id, submission)
            if defer_error is not None:
                return self._submission_exception(canonical, submission, defer_error)
            defer_scope_fingerprint = self._deferred_evidence_scope_fingerprint(
                paper_id=paper_id,
                target_type=canonical,
                target=target,
                field_name=submission.field_name,
                table_references=[defer_context["table_evidence"]] if defer_context.get("table_evidence") else [],
            )
        idempotency_key = self._idempotency_key(
            paper_id, canonical, submission, identity, evidence_scope_fingerprint=defer_scope_fingerprint,
        )
        if self._is_idempotent(existing, idempotency_key):
            return {
                "target_type": canonical,
                "target_id": submission.target_id,
                "field_name": submission.field_name,
                "outcome": (
                    "auto_verified" if existing.reviewer_status == "ai_verified"
                    else "auto_rejected" if existing.reviewer_status == "rejected"
                    else "auto_deferred" if existing.reviewer_status == "ai_blocked" else "exception"
                ),
                "status": existing.reviewer_status,
                "blocked_reasons": [],
                "idempotent": True,
                "database_writes": False,
            }
        conflict_reason = self._write_conflict_reason(existing, submission, current_fingerprint)
        if conflict_reason:
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception", status="needs_human", reasons=[conflict_reason], dry_run=dry_run,
            )
        # A label alone is not authority.  In particular, an old ``verified``
        # row without its still-valid human/PDF locator proof must stay
        # actionable, exactly as it is represented in the task package.
        if existing is not None and is_authoritative_verified_review(self.session, existing, target):
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception", status="needs_human", reasons=["human_verified_requires_human_override"], dry_run=dry_run,
            )

        if submission.decision == "defer":
            return self._finalize_deferred(
                paper_id=paper_id,
                canonical=canonical,
                target=target,
                submission=submission,
                identity=identity,
                dry_run=dry_run,
                evidence_context=defer_context or {},
                scope_fingerprint=defer_scope_fingerprint or "",
                idempotency_key=idempotency_key,
            )

        if submission.decision in {"reject", "exception"}:
            outcome = "auto_rejected" if submission.decision == "reject" else "exception"
            status = "rejected" if submission.decision == "reject" else (
                "exception" if canonical in {"sections", "section_page_fragments", "writing_cards"} else "needs_human"
            )
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome=outcome, status=status,
                reasons=["ai_rejected" if status == "rejected" else "ai_exception"], dry_run=dry_run,
            )

        paper = self.session.get(Paper, paper_id)
        reasons: list[str] = []

        if submission.source_paper_id is not None and submission.evidence_paper_id is not None:
            if str(submission.source_paper_id).strip() != str(submission.evidence_paper_id).strip():
                return self._accept_validation_failure(
                    canonical=canonical,
                    submission=submission,
                    reasons=["conflicting_evidence_paper_ids"],
                    evidence_checks={"evidence_paper_ids_consistent": False},
                    target_paper_id=paper_id,
                    source_paper_id=None,
                )

        evidence_paper_id_raw = submission.evidence_paper_id or submission.source_paper_id
        resolved_evidence_paper_id: UUID | None = paper_id
        evidence_paper = paper
        evidence_paper_authorized = True

        if evidence_paper_id_raw is not None and str(evidence_paper_id_raw).strip():
            try:
                candidate_evidence_uuid = UUID(str(evidence_paper_id_raw).strip())
            except (ValueError, AttributeError):
                candidate_evidence_uuid = None
                evidence_paper_authorized = False

            if candidate_evidence_uuid is not None:
                if candidate_evidence_uuid == paper_id:
                    resolved_evidence_paper_id = paper_id
                    evidence_paper = paper
                    evidence_paper_authorized = True
                else:
                    rel = self.session.scalar(
                        select(PaperRelationship.id).where(
                            PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                            or_(
                                and_(
                                    PaperRelationship.source_paper_id == paper_id,
                                    PaperRelationship.target_paper_id == candidate_evidence_uuid,
                                ),
                                and_(
                                    PaperRelationship.target_paper_id == paper_id,
                                    PaperRelationship.source_paper_id == candidate_evidence_uuid,
                                ),
                            ),
                        ).limit(1)
                    )
                    if rel is not None:
                        evidence_paper = self.session.get(Paper, candidate_evidence_uuid)
                        if evidence_paper is not None:
                            resolved_evidence_paper_id = candidate_evidence_uuid
                            evidence_paper_authorized = True
                        else:
                            resolved_evidence_paper_id = candidate_evidence_uuid
                            evidence_paper_authorized = False
                    else:
                        resolved_evidence_paper_id = candidate_evidence_uuid
                        evidence_paper_authorized = False

        structured_reference: dict[str, Any] | None = None
        effective_evidence_text = submission.evidence_text
        structured_locator: EvidenceLocator | None = None
        if submission.table_id is not None:
            try:
                submitted_table_id = UUID(str(submission.table_id).strip())
            except (ValueError, AttributeError):
                submitted_table_id = None
            built = None
            if (
                submitted_table_id is not None
                and resolved_evidence_paper_id is not None
                and submission.page is not None
                and submission.source_row_index is not None
                and submission.source_column_index is not None
            ):
                built = build_structured_table_cell_evidence(
                    self.session,
                    paper_id=resolved_evidence_paper_id,
                    table_id=submitted_table_id,
                    page=submission.page,
                    source_row_index=submission.source_row_index,
                    source_column_index=submission.source_column_index,
                )
            if built is not None:
                structured_reference = {
                    "kind": built["kind"],
                    "table_id": built["table_id"],
                    "source_row_index": built["source_row_index"],
                    "source_column_index": built["source_column_index"],
                }
                effective_evidence_text = built["canonical_evidence_text"]
                structured_locator = EvidenceLocator(
                    paper_id=resolved_evidence_paper_id,
                    source_type="table",
                    target_type=canonical,
                    target_id=submission.target_id,
                    field_name=submission.field_name,
                    evidence_text=effective_evidence_text,
                    page=submission.page,
                    table_id=submitted_table_id,
                    locator_status="exact_page",
                )

        evidence_checks: dict[str, bool] = {
            "target_exists": True,
            "target_belongs_to_paper": True,
            "evidence_paper_authorized": evidence_paper_authorized,
            "evidence_text_present": bool(effective_evidence_text.strip()),
            "confidence_threshold": submission.confidence >= self.settings.ai_verification_min_confidence,
            "no_unresolved_conflict": not self._has_unresolved_conflict(canonical, target),
        }
        page_text: str | None = None
        pdf_error = "missing_page" if submission.page is None else None
        if not evidence_paper_authorized:
            pdf_error = "unauthorized_evidence_paper"
        elif evidence_paper is not None and submission.page is not None:
            page_text, pdf_error, _path = read_pdf_page_text(evidence_paper, submission.page)
        evidence_checks["real_pdf_present"] = pdf_error not in {"missing_real_pdf", "unreadable_pdf", "unauthorized_evidence_paper"}
        evidence_checks["page_valid"] = pdf_error is None
        if submission.table_id is not None:
            structured_valid = bool(
                pdf_error is None
                and structured_locator is not None
                and structured_reference is not None
                and structured_table_cell_evidence_valid(
                    self.session,
                    locator=structured_locator,
                    reference=structured_reference,
                    page_text=page_text,
                )
            )
            evidence_checks["structured_table_cell_valid"] = structured_valid
            evidence_checks["evidence_on_pdf_page"] = structured_valid
        else:
            evidence_checks["evidence_on_pdf_page"] = (
                pdf_error is None
                and bool(normalize_evidence_text(effective_evidence_text))
                and normalize_evidence_text(effective_evidence_text) in normalize_evidence_text(page_text)
            )

        value_for_gate = submission.proposed_value if submission.decision == "correct" else snapshot["value"]
        evidence_checks.update(self._content_checks(
            canonical,
            target,
            submission.field_name,
            value_for_gate,
            snapshot.get("unit"),
            effective_evidence_text,
        ))
        for key, passed in evidence_checks.items():
            if not passed:
                reasons.append(key)
        if reasons:
            # An accept is a positive assertion, not a request to reject the
            # scientific record.  A deterministic evidence gate failure is
            # therefore a no-write, field-local repair response: the caller
            # can correct its page, quote, or table-cell reference and retry,
            # or explicitly defer when the source cannot support the claim.
            # Only the explicit reject branch above is allowed to create a
            # ``rejected`` review.
            return self._accept_validation_failure(
                canonical=canonical,
                submission=submission,
                reasons=reasons,
                evidence_checks=evidence_checks,
                target_paper_id=paper_id,
                source_paper_id=resolved_evidence_paper_id,
            )

        locator = matching_locator(
            self.session,
            paper_id=resolved_evidence_paper_id or paper_id,
            target_type=canonical,
            target_id=submission.target_id,
            field_name=submission.field_name,
            page=int(submission.page),
            evidence_text=effective_evidence_text,
            table_id=UUID(structured_reference["table_id"]) if structured_reference else None,
        )
        locator_recovered = locator is None
        if not dry_run and locator is None:
            locator = EvidenceLocator(
                paper_id=resolved_evidence_paper_id or paper_id,
                source_type="table" if structured_reference else "pdf",
                target_type=canonical,
                target_id=submission.target_id,
                field_name=submission.field_name,
                evidence_text=effective_evidence_text,
                page=int(submission.page),
                table_id=UUID(structured_reference["table_id"]) if structured_reference else None,
                locator_status="exact_page",
                locator_confidence=submission.confidence,
                parser_source="single_ai_verification",
                bbox=None,
            )
            self.session.add(locator)
            self.session.flush()

        corrected = submission.decision == "correct"
        if corrected and not dry_run:
            self._apply_correction(canonical, target, submission.field_name, submission.proposed_value)
            self.session.add(target)
            self.session.flush()
        final_fingerprint = self._projected_fingerprint(canonical, target, submission) if dry_run else ai_target_fingerprint(canonical, target)
        effective_locator_fingerprint = (
            locator_fingerprint(locator)
            if locator is not None
            else stable_hash({
                "paper_id": str(resolved_evidence_paper_id or paper_id),
                "target_type": canonical,
                "target_id": submission.target_id,
                "field_name": submission.field_name,
                "page": submission.page,
                "table_id": structured_reference["table_id"] if structured_reference else None,
                "evidence_text": normalize_evidence_text(effective_evidence_text),
                "locator_status": "exact_page",
            })
        )
        locator_checks = {
            "exact_page_or_bbox": True,
            "locator_matches_target": True,
            "locator_matches_field": True,
            "locator_snapshot_current": True,
        }
        outcome = "auto_repaired" if corrected or locator_recovered else "auto_verified"
        if not dry_run:
            review = self._upsert_review(paper_id, canonical, submission.target_id, submission.field_name)
            review.original_value = snapshot["value"]
            review.reviewed_value = submission.proposed_value if corrected else snapshot["value"]
            review.unit = snapshot.get("unit")
            review.evidence_text = effective_evidence_text
            review.reviewer_status = "ai_verified"
            review.reviewer = identity.model_agent
            review.reviewer_note = submission.reasoning_summary
            review.target_resolution_status = "active"
            review.last_resolved_target_id = submission.target_id
            review.target_fingerprint = final_fingerprint
            review.review_payload = {
                "ai_verification": self._verification_payload(
                    submission, identity,
                    decision="corrected" if corrected else "verified",
                    target_fingerprint=final_fingerprint,
                    locator_fingerprint_value=effective_locator_fingerprint,
                    evidence_checks=evidence_checks,
                    locator_checks=locator_checks,
                    idempotency_key=idempotency_key,
                    outcome=outcome,
                    target_paper_id=paper_id,
                    source_paper_id=resolved_evidence_paper_id or paper_id,
                    evidence_text_value=effective_evidence_text,
                    table_evidence=structured_reference,
                )
            }
            self.session.add(review)
            self.session.flush()
            self._add_audit(paper_id, canonical, submission, identity, review.reviewer_status, outcome, [], review.review_payload)
            self._sync_content_projection(canonical, target)
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": outcome,
            "status": "ai_verified",
            "blocked_reasons": [],
            "evidence_checks": evidence_checks,
            "locator_checks": locator_checks,
            "target_snapshot_fingerprint": final_fingerprint,
            "idempotent": False,
            "database_writes": not dry_run,
            "target_paper_id": str(paper_id),
            "source_paper_id": str(resolved_evidence_paper_id or paper_id),
            "evidence_paper_id": str(resolved_evidence_paper_id or paper_id),
            "table_evidence": structured_reference,
        }

    @staticmethod
    def _submission_exception(
        canonical: str,
        submission: AIVerificationSubmission,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": "exception",
            "status": "needs_human",
            "blocked_reasons": [reason],
            "database_writes": False,
        }

    @staticmethod
    def _accept_validation_failure(
        *,
        canonical: str,
        submission: AIVerificationSubmission,
        reasons: list[str],
        evidence_checks: dict[str, bool],
        target_paper_id: UUID,
        source_paper_id: UUID | None,
    ) -> dict[str, Any]:
        """Return a repairable accept failure without changing review state."""
        resolved_source = source_paper_id or target_paper_id
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": "accept_validation_failed",
            "status": "pending",
            "blocked_reasons": list(dict.fromkeys(reasons)),
            "evidence_checks": evidence_checks,
            "evidence_location": {
                "source_paper_id": str(resolved_source),
                "page": submission.page,
                "table_id": submission.table_id,
                "source_row_index": submission.source_row_index,
                "source_column_index": submission.source_column_index,
            },
            "idempotent": False,
            "database_writes": False,
            "target_paper_id": str(target_paper_id),
            "source_paper_id": str(resolved_source),
            "evidence_paper_id": str(resolved_source),
        }

    def _validate_defer_evidence_context(
        self,
        paper_id: UUID,
        submission: AIVerificationSubmission,
    ) -> tuple[dict[str, Any], str | None]:
        """Authorize a defer source without requiring an exact locator verdict."""
        if submission.source_paper_id and submission.evidence_paper_id and (
            str(submission.source_paper_id).strip() != str(submission.evidence_paper_id).strip()
        ):
            return {}, "conflicting_evidence_paper_ids"
        raw_id = submission.evidence_paper_id or submission.source_paper_id
        evidence_id = paper_id
        if raw_id:
            try:
                evidence_id = UUID(str(raw_id).strip())
            except (ValueError, AttributeError):
                return {}, "invalid_evidence_paper_id"
        if evidence_id != paper_id:
            related = self.session.scalar(
                select(PaperRelationship.id).where(
                    PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                    or_(
                        and_(PaperRelationship.source_paper_id == paper_id, PaperRelationship.target_paper_id == evidence_id),
                        and_(PaperRelationship.target_paper_id == paper_id, PaperRelationship.source_paper_id == evidence_id),
                    ),
                ).limit(1)
            )
            if related is None:
                return {}, "unauthorized_evidence_paper"
        evidence_paper = self.session.get(Paper, evidence_id)
        if evidence_paper is None:
            return {}, "missing_evidence_paper"
        if submission.page is not None:
            _text, error, _path = read_pdf_page_text(evidence_paper, submission.page)
            if error is not None:
                return {}, "invalid_evidence_page"
        table_evidence: dict[str, Any] | None = None
        if submission.table_id is not None:
            try:
                table_id = UUID(str(submission.table_id))
            except (ValueError, AttributeError):
                return {}, "invalid_table_reference"
            built = build_structured_table_cell_evidence(
                self.session,
                paper_id=evidence_id,
                table_id=table_id,
                page=int(submission.page or 0),
                source_row_index=int(submission.source_row_index) if submission.source_row_index is not None else -1,
                source_column_index=int(submission.source_column_index) if submission.source_column_index is not None else -1,
            )
            if built is None:
                return {}, "invalid_table_reference"
            table_evidence = {
                "table_id": built["table_id"],
                "source_row_index": built["source_row_index"],
                "source_column_index": built["source_column_index"],
            }
        return {
            "evidence_paper_id": evidence_id,
            "source_paper_id": evidence_id,
            "table_evidence": table_evidence,
        }, None

    def _finalize_deferred(
        self,
        *,
        paper_id: UUID,
        canonical: str,
        target: Any,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        dry_run: bool,
        evidence_context: dict[str, Any],
        scope_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist evidence insufficiency without creating a rejection or authority."""
        reasons = list(dict.fromkeys(reason.strip() for reason in submission.blocked_reasons if reason.strip()))
        target_fingerprint = ai_target_fingerprint(canonical, target)
        if not dry_run:
            review = self._upsert_review(paper_id, canonical, submission.target_id, submission.field_name)
            review.original_value = ai_field_snapshot(canonical, target, submission.field_name)["value"]
            review.reviewed_value = None
            review.unit = ai_field_snapshot(canonical, target, submission.field_name).get("unit")
            review.evidence_text = submission.evidence_text or None
            review.reviewer_status = "ai_blocked"
            review.reviewer = identity.model_agent
            review.reviewer_note = submission.reasoning_summary
            review.target_resolution_status = "active"
            review.last_resolved_target_id = submission.target_id
            review.target_fingerprint = target_fingerprint
            payload = {
                "ai_blocked": {
                    "schema": "ai_blocked.v1",
                    "actor_type": "ai",
                    "identity_verified": True,
                    "source_identity": identity.source_identity,
                    "source_label": identity.source_label,
                    "model_agent": identity.model_agent,
                    "capability": AI_VERIFICATION_CAPABILITY,
                    "policy_version": AI_VERIFICATION_POLICY_VERSION,
                    "decision": "defer",
                    "blocked_reasons": reasons,
                    "target_snapshot_fingerprint": target_fingerprint,
                    "evidence_scope_fingerprint": scope_fingerprint,
                    "idempotency_key": idempotency_key,
                    "source_paper_id": str(evidence_context["source_paper_id"]),
                    "evidence_paper_id": str(evidence_context["evidence_paper_id"]),
                    "page": submission.page,
                    "evidence_text": submission.evidence_text,
                    "table_evidence": evidence_context.get("table_evidence"),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            }
            review.review_payload = payload
            self.session.add(review)
            self.session.flush()
            self._add_audit(paper_id, canonical, submission, identity, "ai_blocked", "auto_deferred", reasons, payload)
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": "auto_deferred",
            "status": "ai_blocked",
            "blocked_reasons": reasons,
            "target_snapshot_fingerprint": target_fingerprint,
            "evidence_scope_fingerprint": scope_fingerprint,
            "database_writes": not dry_run,
        }

    def _finalize_failure(
        self,
        paper_id: UUID,
        canonical: str,
        target: Any,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        outcome: str,
        status: str,
        reasons: list[str],
        dry_run: bool,
        evidence_checks: dict[str, bool] | None = None,
        target_paper_id: UUID | None = None,
        source_paper_id: UUID | None = None,
    ) -> dict[str, Any]:
        target_paper = target_paper_id or paper_id
        resolved_source = source_paper_id or paper_id
        # A stale request is diagnostic-only.  It must never be converted into
        # a ``needs_human`` (or other) review write that masks the current
        # authoritative field state.
        if any(str(reason).startswith("write_conflict:") for reason in reasons):
            return {
                "target_type": canonical,
                "target_id": submission.target_id,
                "field_name": submission.field_name,
                "outcome": "write_conflict",
                "status": "pending",
                "blocked_reasons": list(dict.fromkeys(reasons)),
                "evidence_checks": evidence_checks or {},
                "idempotent": False,
                "database_writes": False,
                "target_paper_id": str(target_paper),
                "source_paper_id": str(resolved_source),
                "evidence_paper_id": str(resolved_source),
            }
        if not dry_run:
            review = self._upsert_review(paper_id, canonical, submission.target_id, submission.field_name)
            if review.reviewer_status != "verified":
                snapshot = ai_field_snapshot(canonical, target, submission.field_name)
                review.original_value = snapshot["value"]
                review.reviewed_value = None
                review.unit = snapshot.get("unit")
                review.evidence_text = submission.evidence_text or snapshot.get("evidence_text")
                review.reviewer_status = status
                review.reviewer = identity.model_agent
                review.reviewer_note = submission.reasoning_summary
                review.target_resolution_status = "active"
                review.last_resolved_target_id = submission.target_id
                review.target_fingerprint = ai_target_fingerprint(canonical, target)
                payload = {
                    "ai_verification": self._verification_payload(
                        submission, identity, decision=status,
                        target_fingerprint=review.target_fingerprint,
                        locator_fingerprint_value="",
                        evidence_checks=evidence_checks or {reason: False for reason in reasons},
                        locator_checks={"exact_page_or_bbox": False},
                        idempotency_key=self._idempotency_key(paper_id, canonical, submission, identity),
                        outcome=outcome,
                        target_paper_id=target_paper,
                        source_paper_id=resolved_source,
                    )
                }
                review.review_payload = payload
                self.session.add(review)
                self.session.flush()
                self._add_audit(paper_id, canonical, submission, identity, status, outcome, reasons, payload)
                self._sync_content_projection(canonical, target)
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": outcome,
            "status": status,
            "blocked_reasons": list(dict.fromkeys(reasons)),
            "evidence_checks": evidence_checks or {},
            "idempotent": False,
            "database_writes": not dry_run,
            "target_paper_id": str(target_paper),
            "source_paper_id": str(resolved_source),
            "evidence_paper_id": str(resolved_source),
        }

    def _verification_payload(
        self,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        decision: str,
        target_fingerprint: str,
        locator_fingerprint_value: str,
        evidence_checks: dict[str, bool],
        locator_checks: dict[str, bool],
        idempotency_key: str,
        outcome: str,
        target_paper_id: UUID | None = None,
        source_paper_id: UUID | None = None,
        evidence_text_value: str | None = None,
        table_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence_paper_str = str(source_paper_id) if source_paper_id else (
            str(submission.evidence_paper_id or submission.source_paper_id or target_paper_id or "")
        )
        return {
            "actor_type": "ai",
            "identity_verified": True,
            "source_identity": identity.source_identity,
            "source_label": identity.source_label,
            "model_agent": identity.model_agent,
            "capability": AI_VERIFICATION_CAPABILITY,
            "policy_version": AI_VERIFICATION_POLICY_VERSION,
            "single_ai": True,
            "second_ai_used": False,
            "confidence": submission.confidence,
            "decision": decision,
            "request_decision": submission.decision,
            "outcome": outcome,
            "reasoning_summary": submission.reasoning_summary,
            "target_paper_id": str(target_paper_id) if target_paper_id else None,
            "source_paper_id": evidence_paper_str or None,
            "evidence_paper_id": evidence_paper_str or None,
            "page": submission.page,
            "evidence_text": evidence_text_value if evidence_text_value is not None else submission.evidence_text,
            "counter_evidence_text": submission.counter_evidence_text or None,
            "table_evidence": table_evidence,
            "evidence_checks": evidence_checks,
            "locator_checks": locator_checks,
            "target_snapshot_fingerprint": target_fingerprint,
            "locator_fingerprint": locator_fingerprint_value,
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def _content_checks(
        self,
        canonical: str,
        target: Any,
        field_name: str,
        value: Any,
        unit: Any,
        evidence_text: str,
    ) -> dict[str, bool]:
        evidence = normalize_evidence_text(evidence_text)
        value_text = normalize_evidence_text(value)
        checks = {"content_supported_by_evidence": True}
        if canonical == "writing_cards":
            chain = target.evidence_chain if isinstance(target.evidence_chain, list) else []
            submitted = normalize_evidence_text(evidence_text)
            checks["evidence_matches_chain_item"] = bool(submitted) and any(
                submitted == normalize_evidence_text(item.get("text"))
                for item in chain
                if isinstance(item, dict)
            )
            chain_gate = writing_card_authoritative_chain_gate(self.session, target)
            checks["authoritative_evidence_chain"] = chain_gate.can_use_for_writing
            return checks
        if canonical == "sections":
            complete_single_page_text = (
                bool(compact_page_text(value))
                and compact_page_text(value) == compact_page_text(evidence_text)
            )
            if str(getattr(target, "section_type", "") or "").casefold() == "body":
                checks["content_supported_by_evidence"] = complete_single_page_text
                checks["complete_section_page_coverage"] = complete_single_page_text
                checks["numeric_value_matches"] = complete_single_page_text
                checks["key_entities_consistent"] = complete_single_page_text
                return checks
            if complete_single_page_text:
                checks["content_supported_by_evidence"] = True
                checks["numeric_value_matches"] = True
                checks["key_entities_consistent"] = True
                return checks
        if canonical == "section_page_fragments":
            exact_fragment = (
                bool(compact_page_text(value))
                and compact_page_text(value) == compact_page_text(evidence_text)
            )
            parent = (
                self.session.get(PaperSection, target.section_id)
                if target.section_id is not None
                else None
            )
            checks.update(
                {
                    "content_supported_by_evidence": exact_fragment,
                    "single_physical_pdf_page": (
                        target.page_start is not None
                        and target.page_start == target.page_end
                        and target.page_start >= 1
                    ),
                    "parent_section_bound": bool(
                        parent is not None
                        and parent.paper_id == target.paper_id
                        and compact_page_text(target.evidence_text)
                        in compact_page_text(parent.text)
                    ),
                    "numeric_value_matches": exact_fragment,
                    "unit_matches": exact_fragment,
                    "key_entities_consistent": exact_fragment,
                    "direction_consistent": exact_fragment,
                }
            )
            return checks
        if canonical in {"mechanism_claims", "sections"}:
            tokens = {token.casefold() for token in self._WORD_RE.findall(str(value or "")) if token.casefold() not in self._STOPWORDS}
            evidence_tokens = {token.casefold() for token in self._WORD_RE.findall(evidence_text)}
            checks["content_supported_by_evidence"] = bool(value_text) and (
                value_text in evidence or len(tokens & evidence_tokens) / max(1, len(tokens)) >= 0.45
            )
        if canonical != "dft_results" or field_name == "value":
            numbers = self._NUMBER_RE.findall(str(value or ""))
            if isinstance(value, (int, float)):
                numbers = [str(value)]
            if numbers:
                evidence_number_tokens = self._NUMBER_RE.findall(evidence_text)
                evidence_numbers = [float(item) for item in evidence_number_tokens]
                checks["numeric_value_matches"] = all(
                    any(
                        abs(float(number) - candidate) <= max(1e-8, abs(float(number)) * 1e-6)
                        # Preserve a claimed negative sign; + and unsigned positive are equivalent.
                        and (float(number) >= 0 or token.lstrip().startswith("-"))
                        for candidate, token in zip(evidence_numbers, evidence_number_tokens)
                    )
                    for number in numbers
                )
            if canonical == "dft_results" and field_name == "value":
                expected_values = [value]
                value_upper = getattr(target, "value_upper", None)
                if value_upper is not None:
                    expected_values.append(value_upper)
                normalized_unit = normalize_evidence_text(unit).replace(" ", "") if unit else ""
                def unit_pattern() -> str:
                    if normalized_unit == "e":
                        return r"(?<![A-Za-z0-9])e(?![A-Za-z0-9])|\belectrons?\b"
                    if normalized_unit in {"å", "a", "angstrom"}:
                        return r"[ÅÅ]|\bangstroms?\b"
                    return rf"(?<![A-Za-z0-9]){re.escape(normalized_unit)}(?![A-Za-z0-9])"
                # Table rows/quoted fragments are line- or delimiter-bounded.  Each
                # expected value must share one such data item with its unit; a value
                # from one row and a unit from another cannot authorize the record.
                evidence_items = [
                    normalize_evidence_text(item)
                    for item in re.split(r"[\r\n;|]+", evidence_text)
                    if normalize_evidence_text(item)
                ]
                def signed_value_in_item(expected: Any, item: str) -> bool:
                    try:
                        expected_float = float(expected)
                    except (TypeError, ValueError):
                        return False
                    for match in self._NUMBER_RE.finditer(item):
                        token = match.group(0)
                        context = item[max(0, match.start() - 24):match.start()]
                        if re.search(r"\b(config(?:uration)?|conf|structure|page|table|figure)\s*[:#-]?\s*$", context, re.I):
                            continue
                        if (
                            abs(expected_float - float(token)) <= max(1e-8, abs(expected_float) * 1e-6)
                            and (expected_float >= 0 or token.lstrip().startswith("-"))
                        ):
                            return True
                    return False
                def unit_in_item(item: str) -> bool:
                    return bool(normalized_unit) and bool(re.search(unit_pattern(), item, re.I))
                value_kind = str(getattr(target, "value_kind", None) or "").casefold()
                def range_semantics(item: str) -> bool:
                    if value_upper is None:
                        return True
                    if value_kind in {"range", "interval"}:
                        return bool(re.search(r"\b(range|between|from)\b|\d\s*[-–—]\s*[-+]?\d", item, re.I))
                    if value_kind in {"upper_bound", "upper", "maximum", "max"}:
                        return bool(re.search(r"<=|≤|\b(upper bound|at most|up to|maximum|max)\b", item, re.I))
                    return False
                checks["value_unit_same_evidence_item"] = bool(normalized_unit) and all(
                    any(
                        signed_value_in_item(expected, item)
                        and unit_in_item(item)
                        and range_semantics(item)
                        for item in evidence_items
                    )
                    for expected in expected_values
                )
            if unit:
                normalized_unit = normalize_evidence_text(unit).replace(" ", "")
                if normalized_unit == "e":
                    unit_regex = r"(?<![A-Za-z0-9])e(?![A-Za-z0-9])|\belectrons?\b"
                elif normalized_unit in {"å", "a", "angstrom"}:
                    unit_regex = r"[ÅÅ]|\bangstroms?\b"
                else:
                    unit_regex = rf"(?<![A-Za-z0-9]){re.escape(normalized_unit)}(?![A-Za-z0-9])"
                checks["unit_matches"] = bool(re.search(unit_regex, evidence, re.I))
            elif canonical == "dft_results" and field_name == "value":
                checks["unit_matches"] = False
        if canonical == "dft_results":
            checks["material_identity_present"] = bool(target.catalyst_sample_id) or bool((target.evidence_payload or {}).get("material_identity"))
            if field_name == "catalyst":
                catalyst, active_sites = get_dft_catalyst_identity(
                    self.session,
                    target.catalyst_sample_id,
                )
                supported_types = {"single_atom", "dual_atom", "bimetallic"}
                if (
                    catalyst is None
                    or not normalize_evidence_text(catalyst.name)
                    or str(catalyst.catalyst_type or "").casefold() not in supported_types
                ):
                    checks["catalyst_consistent"] = False
                else:
                    required_phrases: list[str] = []
                    if catalyst.coordination:
                        required_phrases.append(normalize_evidence_text(catalyst.coordination))
                    if catalyst.support:
                        required_phrases.append(normalize_evidence_text(catalyst.support))
                    phrase_matches = lambda phrase: bool(re.search(
                        rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", evidence_text, re.I
                    ))
                    if catalyst.catalyst_type == "single_atom":
                        type_matches = any(phrase_matches(term) for term in ("single atom", "single-atom", "sac"))
                    elif catalyst.catalyst_type in {"dual_atom", "bimetallic"}:
                        type_matches = any(phrase_matches(term) for term in ("dual atom", "dual-atom", "dac", "bimetallic"))
                    else:
                        type_matches = normalize_evidence_text(catalyst.catalyst_type) in evidence
                    metals = catalyst.metal_centers if isinstance(catalyst.metal_centers, list) else [catalyst.metal_centers]
                    metal_matches = all(
                        bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(str(metal))}(?![A-Za-z0-9])", evidence_text, re.I))
                        for metal in metals if metal
                    )
                    active_site_matches = all(
                        phrase_matches(normalize_evidence_text(site.active_site_key))
                        for site in active_sites
                    )
                    checks["catalyst_consistent"] = (
                        _catalyst_name_is_supported_by_evidence(str(catalyst.name), evidence_text)
                        and all(phrase and phrase_matches(phrase) for phrase in required_phrases)
                        and type_matches
                        and metal_matches
                        and active_site_matches
                    )
            elif field_name == "energy_type":
                prop = str(target.property_type or "").casefold()
                tax = get_property_taxonomy(prop)
                canonical_prop = str(tax.get("canonical_property_type") or "").casefold()
                synonyms = {
                    "gibbs_free_energy_change": ("gibbs free energy", "free energy change", "free energy barrier", "delta g", "自由能变化", "吉布斯自由能"),
                    "adsorption_energy": ("adsorption energy", "adsorption energies", "adsorption free energy"),
                    "binding_energy": ("binding energy",),
                    "reaction_barrier": ("reaction barrier", "energy barrier", "free energy barrier", "activation energy", "activation barrier"),
                    "bond_length": ("bond length", "bond lengths"),
                    "zero_point_energy": ("zero point energy", "zero-point energy", "zpe"),
                    "zero_point_energy_correction": ("zero point energy", "zero-point energy", "zpe"),
                    "entropy_contribution": ("entropy contribution", "entropy", " ts "),
                    "entropy_correction_ts": ("entropy contribution", "entropy", " ts "),
                    "charge_transfer": ("charge transfer", "charge distribution", "electron loss", "electron gain"),
                    "bader_charge_transfer": ("bader charge", "charge transfer", "charge distribution", "electron loss", "electron gain"),
                }
                if prop == "adsorption_energy_solvated":
                    concepts = ("solvation effect", "solvated adsorption energy", "e a sol", "ea sol")
                elif prop == "adsorption_configuration_energy":
                    concepts = ("adsorption configurations and energies", "adsorption configuration energy")
                else:
                    concepts = synonyms.get(
                        canonical_prop,
                        synonyms.get(prop, (prop.replace("_", " "), canonical_prop.replace("_", " "))),
                    )
                checks["energy_type_consistent"] = any(term and term in evidence for term in concepts)
            elif field_name == "reaction_step":
                entity = re.sub(r"\s+", "", str(value or "").replace("→", "->").replace("⟶", "->").replace("⟶", "->"))
                if entity:
                    normalized_page = re.sub(r"\s+", "", evidence_text.replace("→", "->").replace("⟶", "->").replace("⟶", "->"))
                    checks["reaction_step_consistent"] = entity.casefold() in normalized_page.casefold()
            elif field_name == "adsorbate":
                entity = normalize_evidence_text(value)
                if entity:
                    checks["adsorbate_consistent"] = bool(
                        re.search(rf"(?<![A-Za-z0-9]){re.escape(entity)}(?![A-Za-z0-9])", evidence, re.I)
                    )
            elif field_name == "value":
                config_idx = extract_configuration_index(getattr(target, "evidence_payload", None) if not isinstance(target, dict) else target.get("evidence_payload"))
                if config_idx is not None:
                    checks["configuration_consistent"] = (
                        bool(re.search(rf"\b(config(?:uration)?|conf|structure)[-_ #]?{config_idx}\b", evidence, re.I))
                    )
        return checks

    def _sync_content_projection(self, canonical: str, target: Any) -> None:
        source_type_by_target = {
            "sections": "section",
            "section_page_fragments": "section_page_fragment",
            "writing_cards": "writing_card",
        }
        source_type = source_type_by_target.get(canonical)
        if source_type is None:
            return
        ContentKnowledgeService(self.session).sync_items(
            paper_id=target.paper_id,
            include_candidates=True,
            source_types=[source_type],
            source_ids=[str(target.id)],
        )

    def _has_unresolved_conflict(self, canonical: str, target: Any) -> bool:
        if canonical != "dft_results":
            return False
        return self.session.scalar(
            select(DFTAuditIssue.id).where(
                DFTAuditIssue.paper_id == target.paper_id,
                DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)),
                or_(DFTAuditIssue.result_id == target.id, DFTAuditIssue.target_id == str(target.id)),
            ).limit(1)
        ) is not None

    @staticmethod
    def _apply_correction(canonical: str, target: Any, field_name: str, proposed_value: Any) -> None:
        if proposed_value is None:
            raise ValueError("correct decision requires proposed_value")
        field_map = {
            "mechanism_claims": {"claim_text": "claim_text", "claim_type": "claim_type", "key_species": "evidence_types"},
            "dft_results": {"adsorbate": "adsorbate", "energy_type": "property_type", "value": "value", "reaction_step": "reaction_step"},
            "electrochemical_performance": {
                "sulfur_loading": "sulfur_loading_mg_cm2", "sulfur_content": "sulfur_content_wt_percent",
                "electrolyte_sulfur_ratio": "electrolyte_sulfur_ratio", "capacity": "capacity_value",
                "cycle_number": "cycle_number", "rate": "rate", "decay_per_cycle": "decay_per_cycle",
            },
            "sections": {"text": "text"},
            "writing_cards": {"research_gap": "research_gap", "proposed_solution": "proposed_solution", "core_hypothesis": "core_hypothesis", "evidence_chain": "evidence_chain"},
        }
        attr = field_map.get(canonical, {}).get(field_name)
        if not attr:
            raise ValueError(f"AI auto-repair is not supported for {canonical}.{field_name}")
        setattr(target, attr, proposed_value)

    def _projected_fingerprint(self, canonical: str, target: Any, submission: AIVerificationSubmission) -> str:
        if submission.decision != "correct":
            return ai_target_fingerprint(canonical, target)
        attr_map = {
            ("mechanism_claims", "claim_text"): "claim_text",
            ("sections", "text"): "text",
            ("writing_cards", "research_gap"): "research_gap",
            ("writing_cards", "proposed_solution"): "proposed_solution",
            ("writing_cards", "core_hypothesis"): "core_hypothesis",
        }
        attr = attr_map.get((canonical, submission.field_name))
        if not attr:
            return stable_hash({"before": ai_target_fingerprint(canonical, target), "field": submission.field_name, "value": submission.proposed_value})
        original = getattr(target, attr)
        try:
            setattr(target, attr, submission.proposed_value)
            return ai_target_fingerprint(canonical, target)
        finally:
            setattr(target, attr, original)

    def _candidate_locators(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> list[EvidenceLocator]:
        from app.utils.review_safety import _associated_paper_ids
        associated_ids = _associated_paper_ids(self.session, {paper_id})
        rows = self.session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id.in_(associated_ids),
                EvidenceLocator.target_id == target_id,
                EvidenceLocator.field_name == field_name,
            ).order_by(EvidenceLocator.page.asc().nulls_last()).limit(10)
        ).all()
        return [row for row in rows if self._same_target_type(target_type, row.target_type)]

    @staticmethod
    def _ai_blocked_payload(review: ExtractionFieldReview | None) -> dict[str, Any] | None:
        payload = review.review_payload if review is not None and isinstance(review.review_payload, dict) else None
        blocked = payload.get("ai_blocked") if isinstance(payload, dict) else None
        return blocked if isinstance(blocked, dict) else None

    def _ai_blocked_review_is_current(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target: Any,
        field_name: str,
        review: ExtractionFieldReview,
        scope_fingerprint: str | None = None,
    ) -> bool:
        blocked = self._ai_blocked_payload(review)
        if str(review.reviewer_status or "").casefold() != "ai_blocked" or blocked is None:
            return False
        if blocked.get("schema") != "ai_blocked.v1" or blocked.get("decision") != "defer":
            return False
        current_target = ai_target_fingerprint(target_type, target)
        if blocked.get("target_snapshot_fingerprint") != current_target:
            return False
        current_scope = scope_fingerprint or self._deferred_evidence_scope_fingerprint(
            paper_id=paper_id,
            target_type=target_type,
            target=target,
            field_name=field_name,
            table_references=[blocked["table_evidence"]] if isinstance(blocked.get("table_evidence"), dict) else [],
        )
        return blocked.get("evidence_scope_fingerprint") == current_scope

    def dft_field_terminal_status(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target: Any,
        field_name: str,
        review: ExtractionFieldReview | None,
        blocked_scope_fingerprint: str | None = None,
    ) -> str | None:
        """Return the one current terminal state shared by tasks and apply."""
        if review is None:
            return None
        if is_authoritative_verified_review(self.session, review, target):
            return str(review.reviewer_status)
        if self._ai_blocked_review_is_current(
            paper_id=paper_id, target_type=target_type, target=target,
            field_name=field_name, review=review,
            scope_fingerprint=blocked_scope_fingerprint,
        ):
            return "ai_blocked"
        if self._explicit_rejection_is_current(paper_id, target, review):
            return "rejected"
        return None

    def _explicit_rejection_is_current(
        self, paper_id: UUID, target: Any, review: ExtractionFieldReview,
    ) -> bool:
        """Accept only explicit, still-current rejections with source-page proof."""
        if str(review.reviewer_status or "").casefold() != "rejected":
            return False
        payload = review.review_payload if isinstance(review.review_payload, dict) else {}
        ai = payload.get("ai_verification") if isinstance(payload, dict) else None
        if not isinstance(ai, dict) or ai.get("request_decision") != "reject" or ai.get("decision") != "rejected":
            return False
        if ai.get("target_snapshot_fingerprint") != ai_target_fingerprint("dft_results", target):
            return False
        try:
            evidence_paper_id = UUID(str(ai.get("evidence_paper_id") or ai.get("source_paper_id")))
            page = int(ai.get("page"))
        except (TypeError, ValueError):
            return False
        counter = normalize_evidence_text(ai.get("counter_evidence_text"))
        if not counter:
            return False
        probe = type("RejectionProbe", (), {
            "source_paper_id": str(evidence_paper_id), "evidence_paper_id": str(evidence_paper_id),
            "page": page, "table_id": None, "source_row_index": None, "source_column_index": None,
        })()
        context, error = self._validate_defer_evidence_context(paper_id, probe)
        if error is not None:
            return False
        paper = self.session.get(Paper, context["evidence_paper_id"])
        if paper is None:
            return False
        page_text, read_error, _path = read_pdf_page_text(paper, page)
        return read_error is None and counter in normalize_evidence_text(page_text or "")

    def _deferred_evidence_scope_fingerprint(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target: Any,
        field_name: str,
        associated_ids: set[UUID] | None = None,
        papers_by_id: dict[UUID, Paper] | None = None,
        locators: list[EvidenceLocator] | None = None,
        tables_by_id: dict[UUID, PaperTable] | None = None,
        table_references: list[dict[str, Any]] | None = None,
    ) -> str:
        """Fingerprint all evidence identities which can legitimately unblock a field."""
        from app.utils.review_safety import _associated_paper_ids

        association_ids = associated_ids or _associated_paper_ids(self.session, {paper_id})
        if papers_by_id is None:
            papers_by_id = {
                item.id: item
                for item in self.session.scalars(select(Paper).where(Paper.id.in_(association_ids))).all()
            }
        if locators is None:
            locators = self._candidate_locators(paper_id, target_type, str(target.id), field_name)
        table_ids = {locator.table_id for locator in locators if locator.table_id}
        for reference in table_references or []:
            try:
                table_ids.add(UUID(str(reference.get("table_id") or "")))
            except (ValueError, AttributeError):
                continue
        if tables_by_id is None:
            tables_by_id = {
                item.id: item
                for item in self.session.scalars(select(PaperTable).where(PaperTable.id.in_(table_ids))).all()
            } if table_ids else {}
        else:
            # A page-level preload is deliberately broader for query
            # efficiency.  The fingerprint itself must still contain only
            # the target field's actual tables.
            tables_by_id = {table_id: table for table_id, table in tables_by_id.items() if table_id in table_ids}

        def pdf_revision(item: Paper) -> tuple[Any, ...]:
            resolved = resolve_paper_pdf_path(item.pdf_path, self.settings.storage_root)
            if resolved is None:
                return (str(item.id), None)
            try:
                stat = resolved.stat()
                return (str(item.id), str(resolved), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            except OSError:
                return (str(item.id), str(resolved), None, None, None)

        return stable_hash({
            "paper_id": str(paper_id),
            "target_type": target_type,
            "target_id": str(target.id),
            "field_name": field_name,
            "target_evidence_text": getattr(target, "evidence_text", None),
            "target_evidence_payload": getattr(target, "evidence_payload", None),
            "associated_paper_ids": sorted(str(item) for item in association_ids),
            "pdf_revisions": [pdf_revision(papers_by_id[item]) for item in sorted(papers_by_id, key=str)],
            "locators": sorted(locator_fingerprint(locator) for locator in locators),
            "paper_tables": [
                {
                    "id": str(table.id),
                    "paper_id": str(table.paper_id),
                    "page": table.page,
                    "caption": table.caption,
                    "markdown_content": table.markdown_content,
                }
                for table in sorted(tables_by_id.values(), key=lambda item: str(item.id))
            ],
            "table_references": sorted(
                [
                    {
                        "table_id": str(reference.get("table_id") or ""),
                        "source_row_index": reference.get("source_row_index"),
                        "source_column_index": reference.get("source_column_index"),
                    }
                    for reference in (table_references or []) if isinstance(reference, dict)
                ],
                key=lambda item: (item["table_id"], str(item["source_row_index"]), str(item["source_column_index"])),
            ),
        })

    @staticmethod
    def _same_target_type(left: str, right: str | None) -> bool:
        try:
            return canonical_ai_target_type(left) == canonical_ai_target_type(str(right or ""))
        except ValueError:
            return False

    def _find_review(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> ExtractionFieldReview | None:
        candidates = self.session.scalars(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_id == target_id,
            ExtractionFieldReview.field_name == field_name,
        )).all()
        canonical_matches = []
        for review in candidates:
            try:
                if canonical_ai_target_type(review.target_type) == canonical_ai_target_type(target_type):
                    canonical_matches.append(review)
            except ValueError:
                continue
        return max(
            canonical_matches,
            key=lambda review: (int(review.write_version or 1), review.updated_at or review.created_at, str(review.id)),
            default=None,
        )

    def _upsert_review(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> ExtractionFieldReview:
        review = self._find_review(paper_id, target_type, target_id, field_name)
        if review is None:
            review = ExtractionFieldReview(
                paper_id=paper_id, target_type=target_type, target_id=target_id,
                field_name=field_name, target_resolution_status="active", last_resolved_target_id=target_id,
            )
            self.session.add(review)
            self.session.flush()
        return review

    @staticmethod
    def _is_idempotent(review: ExtractionFieldReview | None, key: str) -> bool:
        payload = review.review_payload if review is not None and isinstance(review.review_payload, dict) else {}
        ai = payload.get("ai_verification") if isinstance(payload, dict) else None
        blocked = payload.get("ai_blocked") if isinstance(payload, dict) else None
        return bool(
            (isinstance(ai, dict) and ai.get("idempotency_key") == key)
            or (isinstance(blocked, dict) and blocked.get("idempotency_key") == key)
        )

    @staticmethod
    def _write_conflict_reason(
        review: ExtractionFieldReview | None,
        submission: AIVerificationSubmission,
        current_fingerprint: str,
    ) -> str | None:
        if submission.expected_target_fingerprint != current_fingerprint:
            return "write_conflict:target_snapshot_stale"
        if review is not None:
            if submission.expected_write_version is None:
                return "write_conflict:review_version_required"
            if int(review.write_version or 1) != submission.expected_write_version:
                return "write_conflict:review_version_stale"
        return None

    @staticmethod
    def _idempotency_key(
        paper_id: UUID,
        canonical: str,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        evidence_scope_fingerprint: str | None = None,
    ) -> str:
        evidence_paper = str(submission.evidence_paper_id or submission.source_paper_id or paper_id)
        return stable_hash({
            "paper_id": str(paper_id),
            "evidence_paper_id": evidence_paper,
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "decision": submission.decision,
            "confidence": submission.confidence,
            "evidence_text": normalize_evidence_text(submission.evidence_text),
            "page": submission.page,
            "proposed_value": submission.proposed_value,
            "expected_target_fingerprint": submission.expected_target_fingerprint,
            "blocked_reasons": sorted(str(reason).strip() for reason in submission.blocked_reasons if str(reason).strip()),
            "table_reference": {
                "table_id": submission.table_id,
                "source_row_index": submission.source_row_index,
                "source_column_index": submission.source_column_index,
            },
            "evidence_scope_fingerprint": evidence_scope_fingerprint,
            "source_identity": identity.source_identity,
            "policy_version": AI_VERIFICATION_POLICY_VERSION,
        })

    @staticmethod
    def _require_identity(identity: AuthenticatedAIVerificationIdentity) -> None:
        if not identity.identity_verified or not identity.source_identity.strip():
            raise PermissionError("AI verification identity is not server-authenticated")
        if AI_VERIFICATION_CAPABILITY not in identity.capabilities:
            raise PermissionError(f"Missing capability: {AI_VERIFICATION_CAPABILITY}")

    def _add_audit(
        self,
        paper_id: UUID,
        canonical: str,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        status: str,
        outcome: str,
        reasons: list[str],
        payload: dict[str, Any],
    ) -> None:
        self.session.add(AuditLog(
            paper_id=paper_id,
            action="single_ai_verification_decision",
            source=identity.source_identity,
            target_type=canonical,
            target_id=submission.target_id,
            payload={
                "actor_type": "ai", "source_identity": identity.source_identity,
                "source_label": identity.source_label, "model_agent": identity.model_agent,
                "capability": AI_VERIFICATION_CAPABILITY, "policy_version": AI_VERIFICATION_POLICY_VERSION,
                "field_name": submission.field_name, "status": status, "outcome": outcome,
                "blocked_reasons": reasons, "single_ai": True, "second_ai_used": False,
                **payload,
            },
        ))
