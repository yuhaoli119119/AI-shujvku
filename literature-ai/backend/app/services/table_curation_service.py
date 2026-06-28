from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Paper, PaperCorrection, PaperTable, utcnow
from app.services.review_service import ReviewService
from app.utils.evidence_anchors import has_evidence_anchor


class TableCurationService:
    """Evidence-gated, idempotent lifecycle operations for parsed tables."""

    ALLOWED_FIELDS = frozenset(
        {"caption", "markdown_content", "page", "extraction_source", "prov"}
    )

    def __init__(self, session: Session, *, reviewer: str) -> None:
        self.session = session
        self.reviewer = str(reviewer or "").strip() or "system"
        self.review_service = ReviewService(session)

    def update_table(
        self,
        *,
        paper_id: UUID,
        table_id: UUID,
        updates: dict[str, Any],
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> dict[str, Any]:
        reason_text = self._validate_request(reason, evidence_payload)
        cleaned = self._validated_fields(updates, require_nonempty=True)
        table = self._locked_table(table_id)
        self._require_table_owner(table, paper_id)

        before = self._table_summary(table)
        changed = {
            field: value
            for field, value in cleaned.items()
            if getattr(table, field) != value
        }
        if not changed:
            audit = self._latest_audit(
                paper_id=paper_id,
                action="update_table",
                target_id=str(table_id),
            )
            return self._result(
                paper_id=paper_id,
                action="update_table",
                table=table,
                correction_ids=self._audit_correction_ids(audit),
                audit=audit,
                idempotent=True,
            )

        correction_ids = self._apply_updates(
            paper_id=paper_id,
            table_id=table_id,
            updates=changed,
            reason=reason_text,
            evidence_payload=evidence_payload,
        )
        self.session.flush()
        self.session.refresh(table)
        audit = self._add_audit(
            paper_id=paper_id,
            action="update_table",
            target_id=str(table_id),
            payload={
                "table_id": str(table_id),
                "reason": reason_text,
                "changed_fields": sorted(changed),
                "before": before,
                "after": self._table_summary(table),
                "correction_ids": correction_ids,
                "evidence_payload": evidence_payload,
            },
        )
        return self._result(
            paper_id=paper_id,
            action="update_table",
            table=table,
            correction_ids=correction_ids,
            audit=audit,
            idempotent=False,
        )

    def create_table(
        self,
        *,
        paper_id: UUID,
        table_payload: dict[str, Any],
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> dict[str, Any]:
        reason_text = self._validate_request(reason, evidence_payload)
        cleaned = self._validated_fields(table_payload, require_nonempty=True)
        if not (
            str(cleaned.get("caption") or "").strip()
            or str(cleaned.get("markdown_content") or "").strip()
        ):
            raise ValueError("create_table requires caption or markdown_content.")

        self._locked_paper(paper_id)
        existing = self._find_exact_table(paper_id, cleaned)
        if existing is not None:
            audit = self._latest_audit(
                paper_id=paper_id,
                action="create_table",
                target_id=str(existing.id),
            )
            return self._result(
                paper_id=paper_id,
                action="create_table",
                table=existing,
                correction_ids=self._audit_correction_ids(audit),
                audit=audit,
                idempotent=True,
            )

        correction = self._approve_correction(
            paper_id=paper_id,
            target_path="tables:new:create",
            operation="create",
            proposed_value=cleaned,
            reason=reason_text,
            evidence_payload=evidence_payload,
        )
        structured_create = (
            correction.evidence_payload.get("structured_create")
            if isinstance(correction.evidence_payload, dict)
            else None
        )
        table_id = (
            UUID(str(structured_create["target_id"]))
            if isinstance(structured_create, dict) and structured_create.get("target_id")
            else None
        )
        table = self.session.get(PaperTable, table_id) if table_id else None
        if table is None or table.paper_id != paper_id:
            raise RuntimeError("Approved table creation did not materialize the expected table.")
        correction_ids = [str(correction.id)]
        audit = self._add_audit(
            paper_id=paper_id,
            action="create_table",
            target_id=str(table.id),
            payload={
                "table_id": str(table.id),
                "reason": reason_text,
                "table": self._table_summary(table),
                "correction_ids": correction_ids,
                "evidence_payload": evidence_payload,
            },
        )
        return self._result(
            paper_id=paper_id,
            action="create_table",
            table=table,
            correction_ids=correction_ids,
            audit=audit,
            idempotent=False,
        )

    def delete_table(
        self,
        *,
        paper_id: UUID,
        table_id: UUID,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> dict[str, Any]:
        reason_text = self._validate_request(reason, evidence_payload)
        table = self._locked_table(table_id)
        if table is None:
            audit = self._latest_audit(
                paper_id=paper_id,
                action="delete_table",
                target_id=str(table_id),
            )
            if audit is None:
                raise ValueError("Table not found for this paper.")
            snapshot = self._audit_table_snapshot(audit)
            return self._result(
                paper_id=paper_id,
                action="delete_table",
                table=snapshot,
                table_id=table_id,
                correction_ids=self._audit_correction_ids(audit),
                audit=audit,
                idempotent=True,
            )
        self._require_table_owner(table, paper_id)
        snapshot = self._table_summary(table)
        correction = self._approve_correction(
            paper_id=paper_id,
            target_path=f"tables:{table_id}:delete",
            operation="delete",
            proposed_value=None,
            reason=reason_text,
            evidence_payload=evidence_payload,
        )
        correction_ids = [str(correction.id)]
        audit = self._add_audit(
            paper_id=paper_id,
            action="delete_table",
            target_id=str(table_id),
            payload={
                "table_id": str(table_id),
                "reason": reason_text,
                "table": snapshot,
                "correction_ids": correction_ids,
                "evidence_payload": evidence_payload,
            },
        )
        return self._result(
            paper_id=paper_id,
            action="delete_table",
            table=snapshot,
            table_id=table_id,
            correction_ids=correction_ids,
            audit=audit,
            idempotent=False,
        )

    def merge_table(
        self,
        *,
        paper_id: UUID,
        source_table_id: UUID,
        target_table_id: UUID,
        target_updates: dict[str, Any] | None,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> dict[str, Any]:
        if source_table_id == target_table_id:
            raise ValueError("source_table_id and target_table_id must be different.")
        reason_text = self._validate_request(reason, evidence_payload)
        cleaned_updates = self._validated_fields(
            target_updates or {},
            require_nonempty=False,
        )
        rows = self.session.scalars(
            select(PaperTable)
            .where(PaperTable.id.in_([source_table_id, target_table_id]))
            .order_by(PaperTable.id)
            .with_for_update()
        ).all()
        by_id = {row.id: row for row in rows}
        source = by_id.get(source_table_id)
        target = by_id.get(target_table_id)
        if source is None:
            audit = self._latest_merge_audit(
                paper_id=paper_id,
                source_table_id=source_table_id,
                target_table_id=target_table_id,
            )
            if audit is None:
                raise ValueError("Source table not found for this paper.")
            if target is None or target.paper_id != paper_id:
                raise ValueError("Target table not found for this paper.")
            return self._result(
                paper_id=paper_id,
                action="merge_table",
                table=target,
                table_id=target_table_id,
                target_table_id=target_table_id,
                source_table_id=source_table_id,
                correction_ids=self._audit_correction_ids(audit),
                audit=audit,
                idempotent=True,
            )
        if target is None:
            raise ValueError("Target table not found for this paper.")
        self._require_table_owner(source, paper_id)
        self._require_table_owner(target, paper_id)

        source_before = self._table_summary(source)
        target_before = self._table_summary(target)
        changed_updates = {
            field: value
            for field, value in cleaned_updates.items()
            if getattr(target, field) != value
        }
        correction_ids = self._apply_updates(
            paper_id=paper_id,
            table_id=target_table_id,
            updates=changed_updates,
            reason=reason_text,
            evidence_payload=evidence_payload,
        )
        delete_correction = self._approve_correction(
            paper_id=paper_id,
            target_path=f"tables:{source_table_id}:delete",
            operation="delete",
            proposed_value=None,
            reason=reason_text,
            evidence_payload=evidence_payload,
        )
        correction_ids.append(str(delete_correction.id))
        self.session.flush()
        self.session.refresh(target)
        audit = self._add_audit(
            paper_id=paper_id,
            action="merge_table",
            target_id=str(target_table_id),
            payload={
                "source_table_id": str(source_table_id),
                "target_table_id": str(target_table_id),
                "reason": reason_text,
                "source_before": source_before,
                "target_before": target_before,
                "target_after": self._table_summary(target),
                "changed_fields": sorted(changed_updates),
                "correction_ids": correction_ids,
                "evidence_payload": evidence_payload,
            },
        )
        return self._result(
            paper_id=paper_id,
            action="merge_table",
            table=target,
            table_id=target_table_id,
            target_table_id=target_table_id,
            source_table_id=source_table_id,
            correction_ids=correction_ids,
            audit=audit,
            idempotent=False,
        )

    @classmethod
    def table_correction_matches_current(
        cls,
        session: Session,
        *,
        paper_id: UUID,
        target_path: str,
        operation: str,
        proposed_value: Any,
    ) -> tuple[bool, PaperTable | None]:
        if str(operation or "replace").strip().lower() != "replace":
            return False, None
        parts = [part.strip() for part in str(target_path or "").split(":")]
        if len(parts) != 3 or parts[0] != "tables" or parts[2] not in cls.ALLOWED_FIELDS:
            return False, None
        try:
            table_id = UUID(parts[1])
        except (TypeError, ValueError):
            return False, None
        table = session.get(PaperTable, table_id)
        if table is None or table.paper_id != paper_id:
            return False, table
        return getattr(table, parts[2]) == proposed_value, table

    def _apply_updates(
        self,
        *,
        paper_id: UUID,
        table_id: UUID,
        updates: dict[str, Any],
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> list[str]:
        correction_ids: list[str] = []
        for field in sorted(updates):
            correction = self._approve_correction(
                paper_id=paper_id,
                target_path=f"tables:{table_id}:{field}",
                operation="replace",
                proposed_value=updates[field],
                reason=reason,
                evidence_payload=evidence_payload,
            )
            correction_ids.append(str(correction.id))
        return correction_ids

    def _approve_correction(
        self,
        *,
        paper_id: UUID,
        target_path: str,
        operation: str,
        proposed_value: Any,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> PaperCorrection:
        correction = PaperCorrection(
            paper_id=paper_id,
            source=self.reviewer,
            field_name="tables",
            target_path=target_path,
            operation=operation,
            proposed_value=proposed_value,
            reason=reason,
            evidence_payload=evidence_payload,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        return self.review_service.approve_correction(
            correction.id,
            reviewer=self.reviewer,
        )

    def _locked_paper(self, paper_id: UUID) -> Paper:
        paper = self.session.scalar(
            select(Paper).where(Paper.id == paper_id).with_for_update()
        )
        if paper is None:
            raise ValueError("Paper not found.")
        return paper

    def _locked_table(self, table_id: UUID) -> PaperTable | None:
        return self.session.scalar(
            select(PaperTable).where(PaperTable.id == table_id).with_for_update()
        )

    @staticmethod
    def _require_table_owner(table: PaperTable | None, paper_id: UUID) -> None:
        if table is None or table.paper_id != paper_id:
            raise ValueError("Table not found for this paper.")

    def _find_exact_table(
        self,
        paper_id: UUID,
        proposed: dict[str, Any],
    ) -> PaperTable | None:
        rows = self.session.scalars(
            select(PaperTable)
            .where(PaperTable.paper_id == paper_id)
            .order_by(PaperTable.id)
            .with_for_update()
        ).all()
        for row in rows:
            if all(
                getattr(row, field) == proposed.get(field)
                for field in self.ALLOWED_FIELDS
            ):
                return row
        return None

    @classmethod
    def _validated_fields(
        cls,
        values: dict[str, Any],
        *,
        require_nonempty: bool,
    ) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Table fields must be provided as an object.")
        unknown = sorted(set(values) - cls.ALLOWED_FIELDS)
        if unknown:
            raise ValueError(
                "Unsupported table fields: " + ", ".join(unknown)
            )
        if require_nonempty and not values:
            raise ValueError("At least one table field is required.")
        cleaned = dict(values)
        for field in ("caption", "markdown_content", "extraction_source"):
            value = cleaned.get(field)
            if field in cleaned and value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or null.")
        if "page" in cleaned:
            page = cleaned["page"]
            if page is not None and (not isinstance(page, int) or isinstance(page, bool)):
                raise ValueError("page must be an integer or null.")
        if "prov" in cleaned and cleaned["prov"] is not None and not isinstance(cleaned["prov"], list):
            raise ValueError("prov must be an array or null.")
        return cleaned

    @staticmethod
    def _validate_request(
        reason: str,
        evidence_payload: dict[str, Any] | list[Any],
    ) -> str:
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise ValueError("A table curation reason is required.")
        if not isinstance(evidence_payload, (dict, list)) or not has_evidence_anchor(
            evidence_payload
        ):
            raise ValueError(
                "Table curation requires a structured evidence_payload with "
                "page, table, quoted_text, table_id, or bbox."
            )
        return reason_text

    def _add_audit(
        self,
        *,
        paper_id: UUID,
        action: str,
        target_id: str,
        payload: dict[str, Any],
    ) -> AuditLog:
        audit = AuditLog(
            paper_id=paper_id,
            action=action,
            source=self.reviewer,
            target_type="paper_table",
            target_id=target_id,
            payload=payload,
            created_at=utcnow(),
        )
        self.session.add(audit)
        self.session.flush()
        return audit

    def _latest_audit(
        self,
        *,
        paper_id: UUID,
        action: str,
        target_id: str,
    ) -> AuditLog | None:
        return self.session.scalars(
            select(AuditLog)
            .where(
                AuditLog.paper_id == paper_id,
                AuditLog.action == action,
                AuditLog.target_id == target_id,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        ).first()

    def _latest_merge_audit(
        self,
        *,
        paper_id: UUID,
        source_table_id: UUID,
        target_table_id: UUID,
    ) -> AuditLog | None:
        rows = self.session.scalars(
            select(AuditLog)
            .where(
                AuditLog.paper_id == paper_id,
                AuditLog.action == "merge_table",
                AuditLog.target_id == str(target_table_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        ).all()
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if (
                payload.get("source_table_id") == str(source_table_id)
                and payload.get("target_table_id") == str(target_table_id)
            ):
                return row
        return None

    @staticmethod
    def _table_summary(table: PaperTable | dict[str, Any]) -> dict[str, Any]:
        if isinstance(table, dict):
            return dict(table)
        return {
            "id": str(table.id),
            "paper_id": str(table.paper_id),
            "caption": table.caption,
            "markdown_content": table.markdown_content,
            "page": table.page,
            "extraction_source": table.extraction_source,
            "prov": table.prov,
        }

    @staticmethod
    def _audit_correction_ids(audit: AuditLog | None) -> list[str]:
        payload = audit.payload if audit is not None and isinstance(audit.payload, dict) else {}
        values = payload.get("correction_ids")
        return [str(value) for value in values] if isinstance(values, list) else []

    @staticmethod
    def _audit_table_snapshot(audit: AuditLog) -> dict[str, Any]:
        payload = audit.payload if isinstance(audit.payload, dict) else {}
        table = payload.get("table")
        return dict(table) if isinstance(table, dict) else {}

    @classmethod
    def _result(
        cls,
        *,
        paper_id: UUID,
        action: str,
        table: PaperTable | dict[str, Any],
        correction_ids: list[str],
        audit: AuditLog | None,
        idempotent: bool,
        table_id: UUID | None = None,
        target_table_id: UUID | None = None,
        source_table_id: UUID | None = None,
    ) -> dict[str, Any]:
        summary = cls._table_summary(table)
        resolved_table_id = table_id or (
            UUID(str(summary["id"])) if summary.get("id") else None
        )
        result: dict[str, Any] = {
            "paper_id": str(paper_id),
            "table_id": str(resolved_table_id) if resolved_table_id else None,
            "action": action,
            "idempotent": idempotent,
            "correction_ids": correction_ids,
            "audit_log_id": str(audit.id) if audit is not None else None,
            "table": summary,
        }
        if target_table_id is not None:
            result["target_table_id"] = str(target_table_id)
        if source_table_id is not None:
            result["source_table_id"] = str(source_table_id)
        return result
