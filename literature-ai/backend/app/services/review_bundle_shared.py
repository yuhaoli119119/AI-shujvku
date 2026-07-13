from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Collection

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperRelationship


def linked_source_papers(
    session: Session,
    paper: Paper,
    *,
    relationship_types: Collection[str],
) -> list[dict[str, Any]]:
    """Return the main paper followed by unique, deterministically ordered SI papers."""
    relationships = session.scalars(
        select(PaperRelationship).where(
            PaperRelationship.source_paper_id == paper.id,
            PaperRelationship.relationship_type.in_(relationship_types),
        )
    ).all()
    sources: list[dict[str, Any]] = [
        {
            "paper": paper,
            "prefix": "main",
            "source_document_type": "main_text",
            "relationship_id": None,
        }
    ]
    seen = {paper.id}
    for relationship in sorted(relationships, key=lambda item: str(item.target_paper_id)):
        related = session.get(Paper, relationship.target_paper_id)
        if related is None or related.id in seen:
            continue
        seen.add(related.id)
        sources.append(
            {
                "paper": related,
                "prefix": "si",
                "source_document_type": "supplementary_information",
                "relationship_id": str(relationship.id),
            }
        )
    return sources


def compact_figure_artifact(artifact: Path) -> tuple[bytes, str, bool]:
    """Build a smaller AI-reading copy without changing the persisted source image."""
    original = artifact.read_bytes()
    original_suffix = artifact.suffix.lower() or ".png"
    try:
        from PIL import Image

        with Image.open(BytesIO(original)) as source:
            source.load()
            if source.mode == "RGBA":
                image = Image.new("RGB", source.size, "white")
                image.paste(source, mask=source.getchannel("A"))
            elif source.mode not in {"RGB", "L"}:
                image = source.convert("RGB")
            else:
                image = source.copy()
            if image.mode == "L":
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="WEBP", quality=92, method=6)
            compact = output.getvalue()
        if compact and len(compact) < len(original):
            return compact, ".webp", True
    except Exception:
        # Invalid or unsupported legacy assets remain readable in their original form.
        pass
    return original, original_suffix, False
