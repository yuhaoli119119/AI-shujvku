"""Read-only catalog for conversation-owned batches; never repairs paper codes."""
from __future__ import annotations

import hashlib
import json
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper
from app.utils.library_names import normalize_library_name


def batch_catalog(
    session: Session, *, scope: str, libraries: list[str] | None = None,
    limit: int = 200, offset: int = 0, snapshot: str | None = None,
) -> dict[str, Any]:
    if scope not in {"all", "selected"}:
        raise ValueError("explicit_library_scope_required")
    requested = sorted({normalize_library_name(name) for name in libraries or [] if name.strip()})
    if (scope == "selected" and not requested) or (scope == "all" and libraries):
        raise ValueError("invalid_library_scope")
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("invalid_pagination")
    # One SQL snapshot per page. Projected columns only, no code backfill, jobs,
    # relationship access, commit, file inspection or AI-extraction read-through.
    with session.no_autoflush:
        rows = session.execute(select(
            Paper.id, Paper.paper_code, Paper.title, Paper.library_name,
            Paper.created_at, Paper.pdf_path,
        ).order_by(Paper.id.asc())).all()
    available = sorted({normalize_library_name(row.library_name) for row in rows})
    if set(requested) - set(available):
        raise ValueError("unknown_library:" + ",".join(sorted(set(requested) - set(available))))
    items = [{
        "paper_id": str(row.id), "paper_code": row.paper_code, "title": row.title,
        "library_name": normalize_library_name(row.library_name),
        # Paper.created_at is TIMESTAMP WITHOUT TIME ZONE; models.utcnow
        # explicitly writes naive UTC. Expose that existing convention.
        "created_at": (row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None
                       else row.created_at).isoformat() if row.created_at else None,
        "has_pdf": bool(row.pdf_path),
    } for row in rows if scope == "all" or normalize_library_name(row.library_name) in requested]
    token = hashlib.sha256(json.dumps(
        {"scope": scope, "libraries": requested, "available_libraries": available, "items": items},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if snapshot is not None and token != snapshot:
        raise ValueError("catalog_changed_restart_selection")
    return {
        "snapshot": token, "scope": scope, "requested_libraries": requested,
        "available_libraries": available,
        "actual_libraries": sorted({item["library_name"] for item in items}),
        "total": len(items), "offset": offset, "limit": limit,
        "items": items[offset:offset + limit],
    }
