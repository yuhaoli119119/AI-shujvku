from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import AuditLog, ContentEvidenceItem, ContentReviewBundle, ExternalAnalysisRun, Paper
from app.services.task_log_service import TaskLogService
from app.utils.artifact_paths import resolve_paper_pdf_path


RESULT_SCHEMA = "content_evidence_review_result_v1"
ALLOWED_DECISIONS = {"approve_citable", "writing_only", "needs_human", "reject"}


class ContentReviewBundleService:
    """Shared validate -> apply -> finalize workflow for content evidence.

    It intentionally does not trust an AI's claimed identity or claimed PDF read.
    The server rechecks the current item, paper code, fingerprint and locator.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def generate(self, *, paper_id: UUID, run_id: UUID | None = None, created_by: str = "user") -> dict[str, Any]:
        paper = self._paper(paper_id)
        scope_type = "external_analysis_run" if run_id else "paper"
        if run_id:
            run = self.session.get(ExternalAnalysisRun, run_id)
            if run is None or run.paper_id != paper.id:
                raise ValueError("content_review_run_not_found_for_paper")
        items = self._scoped_items(paper.id, run_id)
        if run_id and not items:
            raise ValueError("content_review_no_items_for_run")
        fingerprint = self._fingerprint(paper, items)
        manifest = {
            "schema_version": "content_evidence_review_bundle_v1",
            "bundle_type": "content_evidence_review",
            "scope_type": scope_type,
            "paper_id": str(paper.id), "paper_code": paper.paper_code,
            "run_id": str(run_id) if run_id else None,
            "item_count": len(items),
            "snapshot_fingerprint": fingerprint,
            "items": [self._item_manifest(item) for item in items],
            "evidence_refs": [self._evidence_ref(item) for item in items if item.evidence_text],
            "checklist": [
                "Only quote supplied PDF evidence; do not infer values from a chart.",
                "A citable approval requires a real evidence fragment and page or section locator.",
                "Unknown item_id/evidence_id, different paper_code, or stale fingerprint is rejected.",
                "Candidate and needs_review material cannot be upgraded from AI assertion alone.",
            ],
            "instructions": self._instructions(paper, fingerprint, run_id=run_id, item_count=len(items)),
        }
        bundle = ContentReviewBundle(
            paper_id=paper.id, run_id=run_id, snapshot_fingerprint=fingerprint,
            manifest=manifest, status="generated", created_by=created_by,
        )
        self.session.add(bundle)
        self.session.flush()
        return {
            "bundle_id": str(bundle.id), "manifest": manifest,
            "return_template": self._return_template(paper, fingerprint, run_id=run_id, scope_type=scope_type),
            "format_example": self._format_example(paper, fingerprint, items, run_id=run_id, scope_type=scope_type),
        }

    def validate_result(
        self,
        bundle_id: UUID,
        result: dict[str, Any],
        *,
        authenticated_identity: str | None = None,
        authenticated_identity_verified: bool = False,
    ) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        paper = self._paper(bundle.paper_id)
        self._assert_snapshot_current(bundle, paper)
        self._validate_envelope(bundle, paper, result)
        current = {str(item.id): item for item in self._scoped_items(paper.id, bundle.run_id)}
        seen: set[str] = set()
        errors: list[str] = []
        actions = result.get("items")
        if not isinstance(actions, list) or not actions:
            errors.append("items must be a non-empty list")
            actions = []
        for action in actions:
            if not isinstance(action, dict):
                errors.append("item action must be an object")
                continue
            item_id = str(action.get("item_id") or "")
            if item_id in seen:
                errors.append(f"duplicate_item_id:{item_id}")
                continue
            seen.add(item_id)
            item = current.get(item_id)
            if item is None:
                errors.append(f"unknown_item_id:{item_id}")
                continue
            decision = str(action.get("decision") or "")
            if decision not in ALLOWED_DECISIONS:
                errors.append(f"invalid_decision:{item_id}")
            evidence_id = str(action.get("evidence_id") or "")
            if evidence_id and evidence_id != f"evidence:{item.id}":
                errors.append(f"unknown_evidence_id:{evidence_id}")
            if decision == "approve_citable":
                if item.category == "figure_table_evidence":
                    errors.append(f"figure_field_review_requires_chart_bundle:{item_id}")
                elif not self._has_real_pdf_evidence(paper, item, action):
                    errors.append(f"citable_requires_real_pdf_evidence:{item_id}")
            quoted = str(action.get("evidence_text") or "").strip()
            if quoted and item.evidence_text and quoted not in item.evidence_text:
                errors.append(f"forged_evidence_text:{item_id}")
        if errors:
            raise ValueError("content_review_validation_failed:" + ";".join(errors))
        trusted_source = self._trusted_review_source(
            result.get("review_source"),
            authenticated_identity=authenticated_identity,
            authenticated_identity_verified=authenticated_identity_verified,
        )
        result = {**result, "review_source": trusted_source}
        bundle.result_payload = result
        bundle.manifest = {**(bundle.manifest or {}), "review_identity": trusted_source}
        bundle.status = "validated"
        self.session.add(bundle)
        self.session.add(AuditLog(
            paper_id=paper.id, action="validate_content_review_result", source="content_review",
            target_type="content_review_bundle", target_id=str(bundle.id),
            payload={"snapshot_fingerprint": bundle.snapshot_fingerprint, "item_count": len(actions), "review_identity": trusted_source},
        ))
        self.session.flush()
        if bundle.run_id:
            TaskLogService(self.session).refresh_external_analysis_task(bundle.run_id, last_action="validated", lifecycle="validated")
        return {"valid": True, "bundle_id": str(bundle.id), "item_count": len(actions), "snapshot_fingerprint": bundle.snapshot_fingerprint}

    def apply_result(self, bundle_id: UUID, *, reviewer: str) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        if bundle.status not in {"validated", "applied"} or not isinstance(bundle.result_payload, dict):
            raise ValueError("content_review_result_must_be_validated_before_apply")
        paper = self._paper(bundle.paper_id)
        self._assert_snapshot_current(bundle, paper)
        applied = needs_human = 0
        for action in bundle.result_payload.get("items", []):
            item = self.session.get(ContentEvidenceItem, UUID(str(action["item_id"])))
            if item is None:
                raise ValueError("content_review_item_disappeared")
            decision = action["decision"]
            if decision == "approve_citable":
                if item.category == "figure_table_evidence":
                    raise ValueError(f"figure_field_review_requires_chart_bundle:{item.id}")
                if not self._has_real_pdf_evidence(paper, item, action):
                    raise ValueError(f"citable_requires_real_pdf_evidence:{item.id}")
            item.reviewer = reviewer
            item.reviewed_at = datetime.utcnow()
            if decision == "approve_citable":
                item.review_status, item.citation_status = "validated", "citable"
            elif decision == "writing_only":
                item.review_status, item.citation_status = "validated", "writing_only"
            elif decision == "reject":
                item.review_status, item.citation_status = "rejected", "blocked"
            else:
                item.review_status, item.citation_status = "needs_human", "needs_review"
                needs_human += 1
            item.risk_flags = list(dict.fromkeys([*(item.risk_flags or []), *(action.get("risk_flags") or [])]))
            self.session.add(item)
            applied += 1
        bundle.status = "applied"
        bundle.manifest = {
            **(bundle.manifest or {}),
            "applied_by": reviewer,
            "review_identity": (bundle.manifest or {}).get("review_identity"),
        }
        self.session.add(bundle)
        self.session.add(AuditLog(
            paper_id=paper.id, action="apply_content_review_result", source=reviewer,
            target_type="content_review_bundle", target_id=str(bundle.id),
            payload={"applied": applied, "needs_human": needs_human, "snapshot_fingerprint": bundle.snapshot_fingerprint,
                     "review_identity": (bundle.manifest or {}).get("review_identity")},
        ))
        if bundle.run_id:
            TaskLogService(self.session).refresh_external_analysis_task(
                bundle.run_id, last_action="applied", lifecycle="needs_human" if needs_human else "applied"
            )
        self.session.flush()
        return {"applied": applied, "needs_human": needs_human, "bundle_id": str(bundle.id)}

    def finalize_review(self, bundle_id: UUID, *, reviewer: str) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        if bundle.status != "applied":
            raise ValueError("content_review_result_must_be_applied_before_finalize")
        paper = self._paper(bundle.paper_id)
        unresolved_stmt = select(ContentEvidenceItem.id).where(
            ContentEvidenceItem.paper_id == bundle.paper_id,
            ContentEvidenceItem.review_status.in_(("needs_review", "needs_human")),
        )
        if bundle.run_id:
            unresolved_stmt = unresolved_stmt.where(ContentEvidenceItem.run_id == bundle.run_id)
        unresolved = self.session.scalars(unresolved_stmt).all()
        if unresolved:
            raise ValueError("content_review_unresolved_items:" + ",".join(str(item) for item in unresolved[:8]))
        bundle.status = "finalized"
        self.session.add(bundle)
        self.session.add(AuditLog(
            paper_id=bundle.paper_id, action="finalize_content_review", source=reviewer,
            target_type="content_review_bundle", target_id=str(bundle.id), payload={"finalized": True},
        ))
        if bundle.run_id:
            TaskLogService(self.session).refresh_external_analysis_task(bundle.run_id, last_action="finalized", lifecycle="finalized")
        self.session.flush()
        return {"finalized": True, "bundle_id": str(bundle.id)}

    def _validate_envelope(self, bundle, paper, result):
        if result.get("schema_version") != RESULT_SCHEMA:
            raise ValueError("content_review_validation_failed:unsupported_schema_version")
        if str(result.get("bundle_fingerprint") or "") != bundle.snapshot_fingerprint:
            raise ValueError("content_review_validation_failed:stale_snapshot")
        if str(result.get("paper_id") or "") != str(paper.id) or str(result.get("paper_code") or "") != str(paper.paper_code):
            raise ValueError("content_review_validation_failed:wrong_paper_code")
        if bundle.run_id:
            if str(result.get("run_id") or "") != str(bundle.run_id):
                raise ValueError("content_review_validation_failed:wrong_run_scope")
            if str(result.get("scope_type") or "") != "external_analysis_run":
                raise ValueError("content_review_validation_failed:wrong_scope_type")
        source = result.get("review_source") if isinstance(result.get("review_source"), dict) else {}
        if source.get("source_identity_verified") is True:
            raise ValueError("content_review_validation_failed:forged_source_identity")

    def _assert_snapshot_current(self, bundle: ContentReviewBundle, paper: Paper) -> None:
        manifest_items = (bundle.manifest or {}).get("items")
        expected_ids = [str(item.get("item_id") or "") for item in manifest_items if isinstance(item, dict)]
        if not expected_ids or len(expected_ids) != len(set(expected_ids)):
            raise ValueError("content_review_validation_failed:stale_snapshot")
        rows = [item for item in self._scoped_items(paper.id, bundle.run_id) if str(item.id) in expected_ids]
        by_id = {str(item.id): item for item in rows}
        if len(by_id) != len(expected_ids):
            raise ValueError("content_review_validation_failed:stale_snapshot")
        current_items = [by_id[item_id] for item_id in expected_ids]
        if self._fingerprint(paper, current_items) != bundle.snapshot_fingerprint:
            raise ValueError("content_review_validation_failed:stale_snapshot")

    @staticmethod
    def _trusted_review_source(
        value: Any,
        *,
        authenticated_identity: str | None,
        authenticated_identity_verified: bool,
    ) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        # HTTP JSON is a declaration only.  Only the server-side auth context
        # may elevate the authenticated identity flag.
        return {
            "declared_source_type": str(source.get("review_source_type") or "unknown").strip() or "unknown",
            "declared_reviewer_label": str(source.get("reviewer_label") or "").strip() or None,
            "authenticated_identity": authenticated_identity if authenticated_identity_verified else None,
            "identity_verified": bool(authenticated_identity_verified and authenticated_identity),
        }

    def _has_real_pdf_evidence(self, paper, item, action):
        locator = item.evidence_locator or {}
        has_locator = bool(item.page_start or item.section_title or locator.get("page") or locator.get("page_start") or locator.get("section_title"))
        return bool(
            resolve_paper_pdf_path(paper.pdf_path, self.settings.storage_root) is not None
            and item.evidence_text and has_locator
            and action.get("evidence_id") == f"evidence:{item.id}"
        )

    @staticmethod
    def _fingerprint(paper, items):
        payload = {"paper_id": str(paper.id), "paper_code": paper.paper_code, "items": [
            {"id": str(i.id), "content": i.content, "evidence": i.evidence_text, "locator": i.evidence_locator,
             "page_start": i.page_start, "page_end": i.page_end, "section_title": i.section_title,
             "review_status": i.review_status, "citation_status": i.citation_status} for i in items
        ]}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _item_manifest(item):
        return {"item_id": str(item.id), "category": item.category, "content": item.content,
                "review_status": item.review_status, "citation_status": item.citation_status,
                "risk_flags": item.risk_flags or [], "evidence_id": f"evidence:{item.id}" if item.evidence_text else None}

    @staticmethod
    def _evidence_ref(item):
        return {"evidence_id": f"evidence:{item.id}", "item_id": str(item.id), "text": item.evidence_text,
                "locator": item.evidence_locator, "page_start": item.page_start, "page_end": item.page_end,
                "section_title": item.section_title}

    @staticmethod
    def _return_template(paper, fingerprint, *, run_id=None, scope_type="paper"):
        return {"schema_version": RESULT_SCHEMA, "bundle_fingerprint": fingerprint, "paper_id": str(paper.id),
                "paper_code": paper.paper_code, "scope_type": scope_type,
                "run_id": str(run_id) if run_id else None,
                "review_source": {"review_source_type": "ide_ai", "reviewer_label": "", "source_identity_verified": False}, "items": []}

    @staticmethod
    def _format_example(paper, fingerprint, items, *, run_id=None, scope_type="paper"):
        item = items[0] if items else None
        return {"schema_version": RESULT_SCHEMA, "bundle_fingerprint": fingerprint, "paper_id": str(paper.id),
                "scope_type": scope_type, "run_id": str(run_id) if run_id else None,
                "paper_code": paper.paper_code, "review_source": {"review_source_type": "web_ai", "reviewer_label": "external AI", "source_identity_verified": False},
                "items": [] if item is None else [{"item_id": str(item.id), "decision": "needs_human", "evidence_id": f"evidence:{item.id}", "risk_flags": ["example_only"]}]}

    @staticmethod
    def _instructions(paper, fingerprint, *, run_id=None, item_count=None):
        scope = (
            f"Review only external-analysis run {run_id} ({item_count} items); do not include other runs or paper-level content."
            if run_id
            else "Review the paper-level content scope."
        )
        return f"Return JSON only. Review paper {paper.paper_code}; {scope} Preserve snapshot {fingerprint}. Do not claim verified identity or PDF evidence you cannot quote."

    def _scoped_items(self, paper_id: UUID, run_id: UUID | None = None) -> list[ContentEvidenceItem]:
        stmt = select(ContentEvidenceItem).where(ContentEvidenceItem.paper_id == paper_id)
        if run_id:
            stmt = stmt.where(ContentEvidenceItem.run_id == run_id)
        return list(self.session.scalars(stmt.order_by(ContentEvidenceItem.created_at.asc())).all())

    def _paper(self, paper_id):
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise ValueError("content_review_paper_not_found")
        return paper

    def _bundle(self, bundle_id):
        bundle = self.session.get(ContentReviewBundle, bundle_id)
        if bundle is None:
            raise ValueError("content_review_bundle_not_found")
        return bundle
