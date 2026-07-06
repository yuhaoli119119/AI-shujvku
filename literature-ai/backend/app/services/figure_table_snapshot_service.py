from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PaperFigure, PaperRelationship, PaperTable
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES


def compute_figure_table_snapshot(
    session: Session,
    paper_id: UUID,
) -> dict[str, Any]:
    """Return the canonical, reproducible figure/table content snapshot.

    The fingerprint intentionally contains only persisted object content and
    stable identifiers. It excludes review payloads, operation ids, audit rows,
    timestamps, and generated bundle metadata so every workflow can recompute
    the exact same value from the current database state.
    """

    related_ids = session.scalars(
        select(PaperRelationship.target_paper_id).where(
            PaperRelationship.source_paper_id == paper_id,
            PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
        )
    ).all()
    source_ids = {paper_id, *related_ids}
    tables = session.scalars(
        select(PaperTable)
        .where(PaperTable.paper_id.in_(source_ids))
        .order_by(PaperTable.paper_id, PaperTable.id)
    ).all()
    figures = session.scalars(
        select(PaperFigure)
        .where(PaperFigure.paper_id.in_(source_ids))
        .order_by(PaperFigure.paper_id, PaperFigure.id)
    ).all()
    payload = {
        "schema_version": "figure_table_content_snapshot_v1",
        "paper_id": str(paper_id),
        "tables": [
            {
                "id": str(row.id),
                "paper_id": str(row.paper_id),
                "caption": row.caption,
                "page": row.page,
                "markdown_content": row.markdown_content,
                "extraction_source": row.extraction_source,
            }
            for row in tables
        ],
        "figures": [
            {
                "id": str(row.id),
                "paper_id": str(row.paper_id),
                "figure_label": row.figure_label,
                "caption": row.caption,
                "page": row.page,
                "figure_role": row.figure_role,
                "role_confidence": row.role_confidence,
                "content_summary": row.content_summary,
                "key_elements": row.key_elements,
                "crop_status": row.crop_status,
                "crop_confidence": row.crop_confidence,
                "crop_source": row.crop_source,
                "image_path": row.image_path,
            }
            for row in figures
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {
        **payload,
        "fingerprint": hashlib.sha256(canonical).hexdigest(),
    }
