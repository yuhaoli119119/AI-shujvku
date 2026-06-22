from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AuditLog, ModuleWriteLock, Paper, utcnow


CANONICAL_MODULES = {
    "all_non_dft",
    "content",
    "metadata",
    "sections",
    "writing_cards",
    "mechanism_claims",
    "electrochemical_performance",
    "catalyst_samples",
    "figures",
    "tables",
    "notes",
    "relationships",
    "dft_results",
    "dft_settings",
}

MODULE_ALIASES = {
    "all": "all_non_dft",
    "all_non_dft_modules": "all_non_dft",
    "non_dft": "all_non_dft",
    "paper": "content",
    "paper_content": "content",
    "chapter": "sections",
    "chapters": "sections",
    "section": "sections",
    "paper_sections": "sections",
    "writing": "writing_cards",
    "writing_card": "writing_cards",
    "writer": "writing_cards",
    "mechanism": "mechanism_claims",
    "mechanism_claim": "mechanism_claims",
    "claims": "mechanism_claims",
    "electrochemical": "electrochemical_performance",
    "electrochem": "electrochemical_performance",
    "performance": "electrochemical_performance",
    "catalyst": "catalyst_samples",
    "catalysts": "catalyst_samples",
    "catalyst_sample": "catalyst_samples",
    "materials": "catalyst_samples",
    "sample": "catalyst_samples",
    "image": "figures",
    "images": "figures",
    "figure": "figures",
    "paper_figures": "figures",
    "screenshot": "figures",
    "screenshots": "figures",
    "table": "tables",
    "paper_tables": "tables",
    "note": "notes",
    "paper_note": "notes",
    "relationship": "relationships",
    "paper_relationship": "relationships",
    "dft": "dft_results",
    "dft_result": "dft_results",
    "dft_results": "dft_results",
    "dft_setting": "dft_settings",
    "dft_settings": "dft_settings",
    "title": "metadata",
    "abstract": "metadata",
    "authors": "metadata",
    "author": "metadata",
    "doi": "metadata",
    "year": "metadata",
    "journal": "metadata",
    "oa_status": "metadata",
    "license": "metadata",
    "paper_type": "metadata",
    "type_confidence": "metadata",
    "classification_source": "metadata",
}

SCOPE_MEMBERS = {
    "all_non_dft": {
        "content",
        "metadata",
        "sections",
        "writing_cards",
        "mechanism_claims",
        "electrochemical_performance",
        "catalyst_samples",
        "figures",
        "tables",
        "notes",
        "relationships",
    },
    "content": {
        "metadata",
        "sections",
        "writing_cards",
        "mechanism_claims",
        "electrochemical_performance",
        "catalyst_samples",
        "notes",
    },
}


@dataclass(frozen=True)
class ModuleWriteLockCheck:
    valid: bool
    required_modules: list[str]
    covered_modules: list[str]
    missing_modules: list[str]
    lock_ids: list[str]


class ModuleWriteLockError(ValueError):
    """Stable domain error used by HTTP/MCP boundaries for lock conflicts."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class ModuleWriteLockService:
    """Lease-based guard for direct AI writes to paper modules."""

    DEFAULT_TTL_MINUTES = 30
    MAX_TTL_MINUTES = 240
    AUDIT_SOURCE_MAX_LENGTH = 64

    def __init__(self, session: Session) -> None:
        self.session = session

    @classmethod
    def normalize_module_name(cls, module_name: str | None) -> str:
        normalized = str(module_name or "").strip().lower().replace("-", "_").replace(" ", "_")
        normalized = MODULE_ALIASES.get(normalized, normalized)
        if normalized not in CANONICAL_MODULES:
            raise ValueError(f"Unsupported module_name: {module_name}")
        return normalized

    @classmethod
    def module_from_field(cls, field_name: str | None, target_path: str | None = None) -> str:
        field = str(field_name or "").strip().lower()
        target = str(target_path or "").strip().lower()
        top = target.split(":", 1)[0] if target else field
        if top:
            return cls.normalize_module_name(top)
        return "metadata"

    def acquire(
        self,
        *,
        paper_id: UUID,
        module_name: str,
        locked_by: str,
        ttl_minutes: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ModuleWriteLock:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        self._lock_paper_scope(paper_id)
        module = self.normalize_module_name(module_name)
        owner = str(locked_by or "").strip()
        if not owner:
            raise ValueError("locked_by is required")
        ttl = max(1, min(int(ttl_minutes or self.DEFAULT_TTL_MINUTES), self.MAX_TTL_MINUTES))
        now = utcnow()
        self.expire_stale_locks(now=now)

        conflicts = self._active_conflicts(paper_id=paper_id, module_name=module, now=now)
        owner_conflicts = [lock for lock in conflicts if lock.locked_by == owner and lock.module_name == module]
        if owner_conflicts and len(conflicts) == len(owner_conflicts):
            lock = owner_conflicts[0]
            lock.expires_at = now + timedelta(minutes=ttl)
            lock.meta = {**(lock.meta or {}), **(meta or {})} or None
            self.session.add(lock)
            self._audit(lock, action="renew_module_write_lock", source=owner)
            self.session.flush()
            return lock
        if conflicts:
            raise ModuleWriteLockError(
                "module_write_lock_conflict",
                ",".join(f"{item.module_name}:{item.locked_by}:{item.expires_at.isoformat()}" for item in conflicts),
            )

        lock = ModuleWriteLock(
            paper_id=paper_id,
            module_name=module,
            locked_by=owner,
            expires_at=now + timedelta(minutes=ttl),
            meta=meta or None,
        )
        self.session.add(lock)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ModuleWriteLockError("module_write_lock_conflict", "active lock already exists") from exc
        self._audit(lock, action="acquire_module_write_lock", source=owner)
        self.session.flush()
        return lock

    def release(self, *, lock_token: str, released_by: str | None = None) -> ModuleWriteLock:
        token = str(lock_token or "").strip()
        if not token:
            raise ValueError("lock_token is required")
        lock = self.session.scalar(select(ModuleWriteLock).where(ModuleWriteLock.lock_token == token))
        if lock is None:
            raise LookupError("Module write lock not found")
        releaser = str(released_by or lock.locked_by or "system").strip() or "system"
        if released_by and lock.locked_by != released_by:
            raise ValueError("module_write_lock_owner_mismatch")
        if lock.status == "active":
            lock.status = "released"
            lock.released_at = utcnow()
            self.session.add(lock)
            self.session.flush()
        self._audit(lock, action="release_module_write_lock", source=releaser)
        self.session.flush()
        return lock

    def validate_write(
        self,
        *,
        paper_id: UUID,
        module_names: list[str] | set[str],
        lock_tokens: list[str] | set[str] | None,
        locked_by: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> ModuleWriteLockCheck:
        required = sorted({self.normalize_module_name(item) for item in module_names if str(item or "").strip()})
        if not required:
            return ModuleWriteLockCheck(True, [], [], [], [])
        now = utcnow()
        self.expire_stale_locks(now=now)
        tokens = [str(item or "").strip() for item in (lock_tokens or []) if str(item or "").strip()]
        if not tokens:
            return ModuleWriteLockCheck(False, required, [], required, [])
        locks = self.session.scalars(
            select(ModuleWriteLock).where(
                ModuleWriteLock.paper_id == paper_id,
                ModuleWriteLock.lock_token.in_(tokens),
                ModuleWriteLock.status == "active",
                ModuleWriteLock.expires_at > now,
            )
        ).all()
        allowed_owners = {
            str(item or "").strip()
            for item in (
                locked_by
                if isinstance(locked_by, (list, set, tuple))
                else [locked_by]
            )
            if str(item or "").strip()
        }
        if allowed_owners:
            locks = [lock for lock in locks if lock.locked_by in allowed_owners]
        covered = sorted({module for module in required if any(self._covers(lock.module_name, module) for lock in locks)})
        missing = sorted(set(required) - set(covered))
        return ModuleWriteLockCheck(
            valid=not missing,
            required_modules=required,
            covered_modules=covered,
            missing_modules=missing,
            lock_ids=[str(lock.id) for lock in locks],
        )

    def require_write(
        self,
        *,
        paper_id: UUID,
        module_names: list[str] | set[str],
        lock_tokens: list[str] | set[str] | None,
        locked_by: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> ModuleWriteLockCheck:
        check = self.validate_write(
            paper_id=paper_id,
            module_names=module_names,
            lock_tokens=lock_tokens,
            locked_by=locked_by,
        )
        if not check.valid:
            raise ValueError("module_write_lock_required:" + ",".join(check.missing_modules))
        return check

    def list_locks(
        self,
        *,
        paper_id: UUID | None = None,
        status: str | None = "active",
    ) -> list[ModuleWriteLock]:
        self.expire_stale_locks()
        stmt = select(ModuleWriteLock).order_by(ModuleWriteLock.created_at.desc())
        if paper_id is not None:
            stmt = stmt.where(ModuleWriteLock.paper_id == paper_id)
        if status:
            stmt = stmt.where(ModuleWriteLock.status == status)
        return self.session.scalars(stmt).all()

    def expire_stale_locks(self, *, now: datetime | None = None) -> int:
        now = now or utcnow()
        locks = self.session.scalars(
            select(ModuleWriteLock).where(ModuleWriteLock.status == "active", ModuleWriteLock.expires_at <= now)
        ).all()
        for lock in locks:
            lock.status = "expired"
            self.session.add(lock)
            self._audit(lock, action="expire_module_write_lock", source="system")
        if locks:
            self.session.flush()
        return len(locks)

    def _active_conflicts(self, *, paper_id: UUID, module_name: str, now: datetime) -> list[ModuleWriteLock]:
        active = self.session.scalars(
            select(ModuleWriteLock).where(
                ModuleWriteLock.paper_id == paper_id,
                ModuleWriteLock.status == "active",
                ModuleWriteLock.expires_at > now,
            )
        ).all()
        return [lock for lock in active if self._scopes_overlap(lock.module_name, module_name)]

    def _lock_paper_scope(self, paper_id: UUID) -> None:
        bind = self.session.get_bind()
        if bind is None:
            return
        if bind.dialect.name != "postgresql":
            raise ModuleWriteLockError("module_write_lock_unsupported_database", bind.dialect.name)
        # Transaction-scoped and keyed by paper: failures/rollbacks release it
        # automatically, while unrelated papers remain fully concurrent.
        key = int.from_bytes(paper_id.bytes[:8], byteorder="big", signed=True)
        acquired = self.session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}).scalar()
        if not acquired:
            raise ModuleWriteLockError("module_write_lock_conflict", "paper_transaction_busy")

    @classmethod
    def _scope_set(cls, module_name: str) -> set[str]:
        module = cls.normalize_module_name(module_name)
        return {module, *SCOPE_MEMBERS.get(module, set())}

    @classmethod
    def _covers(cls, holder_module: str, required_module: str) -> bool:
        return cls.normalize_module_name(required_module) in cls._scope_set(holder_module)

    @classmethod
    def _scopes_overlap(cls, left: str, right: str) -> bool:
        return bool(cls._scope_set(left) & cls._scope_set(right))

    def _audit(self, lock: ModuleWriteLock, *, action: str, source: str) -> None:
        normalized_source = self._normalize_audit_source(source)
        self.session.add(
            AuditLog(
                paper_id=lock.paper_id,
                action=action,
                source=normalized_source,
                target_type="module_write_lock",
                target_id=str(lock.id),
                payload={
                    "module_name": lock.module_name,
                    "locked_by": lock.locked_by,
                    "status": lock.status,
                    "expires_at": lock.expires_at.isoformat(),
                    "released_at": lock.released_at.isoformat() if lock.released_at else None,
                    "lock_token_present": bool(lock.lock_token),
                    "metadata": lock.meta or {},
                },
            )
        )

    @classmethod
    def _normalize_audit_source(cls, source: str | None) -> str:
        value = str(source or "").strip() or "system"
        if len(value) <= cls.AUDIT_SOURCE_MAX_LENGTH:
            return value
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
        keep = cls.AUDIT_SOURCE_MAX_LENGTH - len(digest) - 1
        if keep <= 0:
            return digest[: cls.AUDIT_SOURCE_MAX_LENGTH]
        return f"{value[:keep]}:{digest}"
