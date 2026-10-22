"""Read-only projection of stored chapters and curated visuals for the guide.

Exportability is not scientific approval: draft/reviewed/corrected status and
source metadata are retained verbatim. Broken references remain visible in the
API and prevent a misleadingly complete offline artifact from being published.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import select

from app.db.models import RebuildPaperFile, RebuildVisualAsset
from app.services.rebuild_sections import list_sections
from app.utils.artifact_paths import resolve_persisted_artifact_path


class ReadingGuideProjectionError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("reading_guide_projection_incomplete")


def guide_image_path(item, settings):
    """Curated references are exact storage pointers, never basename searches."""
    raw = item.get("image_path")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("missing_or_malformed_image_pointer")
    if item.get("record_type") != "rebuild_visual_asset":
        path = resolve_persisted_artifact_path(raw, category="figures", settings=settings)
    else:
        pointer = Path(raw)
        if ".." in pointer.parts or "://" in raw or "\\" in raw:
            raise ValueError("malformed_image_pointer")
        root = settings.storage_root.resolve()
        path = (root / pointer).resolve()
        if not path.is_relative_to(root):
            raise ValueError("image_pointer_outside_storage")
    if path is None or not path.is_file():
        raise ValueError("image_not_found")
    return path


def stored_guide_projection(session, paper_id, settings):
    sections = list_sections(session, paper_id)
    # The reader's source-bound ordering contract is shared, including history
    # and explicit unverified-order labels. No new scientific order is inferred.
    sections = [{**s, "order": index} for index, s in enumerate(sections)]
    files = {f.id: f for f in session.scalars(select(RebuildPaperFile).where(
        RebuildPaperFile.paper_id == paper_id))}
    assets = session.scalars(select(RebuildVisualAsset).where(
        RebuildVisualAsset.paper_id == paper_id,
        RebuildVisualAsset.asset_type != "table").order_by(
            RebuildVisualAsset.figure_label.asc().nulls_last(),
            RebuildVisualAsset.subfigure_label.asc().nulls_first(),
            RebuildVisualAsset.asset_key, RebuildVisualAsset.id)).all()
    items, issues = [], []
    for asset in assets:
        provenance = asset.provenance if isinstance(asset.provenance, dict) else {}
        identity = provenance.get("page_identity")
        identity = identity if isinstance(identity, dict) else {}
        file = files.get(asset.file_id)
        reading = provenance.get("reading_explanation") or provenance.get("scientific_reading")
        pages = asset.page_numbers
        valid_pages = isinstance(pages, list) and all(type(p) is int and p > 0 for p in pages)
        item = {
            "figure_id": str(asset.id), "asset_id": str(asset.id),
            "record_type": "rebuild_visual_asset", "paper_id": str(paper_id),
            "file_id": str(asset.file_id) if asset.file_id else None,
            "asset_key": asset.asset_key, "asset_type": asset.asset_type,
            "logical_group_key": asset.logical_group_key,
            "figure_label": asset.figure_label, "subfigure_label": asset.subfigure_label,
            "caption": asset.caption, "page_numbers": asset.page_numbers,
            "page": pages[0] if valid_pages and pages and identity.get("page_basis") != "ooxml" else None,
            "page_identity": identity, "provenance": asset.provenance,
            "page_basis": identity.get("page_basis"),
            "image_path": asset.image_path,
            "image_url": f"/api/rebuild/assets/{asset.id}" if asset.image_path else None,
            "download_url": f"/api/rebuild/files/{file.id}/download" if file else None,
            "source_file": {"id": str(file.id), "role": file.role,
                "sha256": file.sha256, "original_filename": file.original_filename} if file else None,
            "status": asset.status, "version": asset.version,
            "reading": reading if isinstance(reading, dict) else None,
            "has_reading": isinstance(reading, dict) and bool(reading.get("detailed_explanation") or reading.get("detailed_explanation_zh")),
            "content_summary": asset.explanation, "figure_role": asset.asset_type,
            "context_text": asset.context_text,
        }
        errors = []
        if not valid_pages:
            errors.append("malformed_page_numbers")
        if reading is not None and not isinstance(reading, dict):
            errors.append("malformed_reading_pointer")
        if asset.provenance is not None and not isinstance(asset.provenance, dict):
            errors.append("malformed_provenance")
        if asset.file_id and not file:
            errors.append("source_file_not_found_or_wrong_paper")
        if "page_identity" in provenance and not isinstance(provenance["page_identity"], dict):
            errors.append("malformed_page_identity")
        source_sha = identity.get("source_sha256") or provenance.get("source_sha256")
        if source_sha and (not file or source_sha != file.sha256):
            errors.append("source_sha256_mismatch")
        if identity.get("page_basis") == "ooxml" and (
            not file or identity.get("source_sha256") != file.sha256 or
            asset.page_numbers or identity.get("page_number") is not None or
            identity.get("reading_pdf_sha256") is not None or
            not isinstance(identity.get("ooxml_locator"), str) or
            not re.fullmatch(r"word/document\.xml#body-child\[[1-9][0-9]*\](?:/w:[A-Za-z][A-Za-z0-9]*\[[1-9][0-9]*\])+", identity["ooxml_locator"])
        ):
            errors.append("malformed_ooxml_pointer")
        # Legacy DOCX provenance uses media relationships and paragraph XML
        # locators rather than page_identity; retain it without inventing pages.
        media = provenance.get("media")
        if media is not None and not isinstance(media, dict):
            errors.append("malformed_media_pointer")
        if not identity and isinstance(media, dict) and not asset.page_numbers:
            member = media.get("zip_member")
            if (not file or not file.original_filename.lower().endswith(".docx") or
                not isinstance(member, str) or not member.startswith("word/media/") or ".." in member):
                errors.append("malformed_media_pointer")
            else:
                item["page_basis"] = "ooxml"
        try:
            path = guide_image_path(item, settings)
            digest = media.get("sha256") if isinstance(media, dict) else None
            if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                errors.append("media_sha256_mismatch")
            item["image_status"] = "available"
        except (ValueError, OSError, TypeError) as exc:
            errors.append(str(exc) if isinstance(exc, ValueError) else "image_unreadable")
            item["image_status"] = "unavailable"
        item["projection_errors"] = errors
        issues.extend({"asset_id": str(asset.id), "code": code} for code in errors)
        items.append(item)
    return sections, items, issues
