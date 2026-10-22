"""Read-only projection of stored chapters and curated visuals for the guide.

Exportability is not scientific approval: draft/reviewed/corrected status and
source metadata are retained verbatim. Broken references remain visible in the
API and prevent a misleadingly complete offline artifact from being published.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from PIL import Image
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


# Per curated asset, inclusive limits. Animation work is charged at its largest
# composited canvas for every frame, including an APNG default image. These
# deterministic bounds apply both to projection and to the bytes embedded later.
# Curated guide rasters use PNG (including APNG), JPEG or GIF; restrict Pillow
# discovery to these formats so other animation plugins cannot decode on open.
GUIDE_IMAGE_MAX_ENCODED_BYTES = 16 * 1024 * 1024
GUIDE_IMAGE_MAX_DIMENSION = 16384
GUIDE_IMAGE_MAX_FRAME_PIXELS = 16 * 1024 * 1024
GUIDE_IMAGE_MAX_FRAMES = 128
GUIDE_IMAGE_MAX_TOTAL_PIXELS = 64 * 1024 * 1024


class _ImageResourceLimit(ValueError):
    def __init__(self):
        super().__init__("image_resource_limit_exceeded")


def _check_image_budget(width, height, frames=1):
    if width <= 0 or height <= 0 or frames <= 0:
        raise ValueError("invalid_image_dimensions_or_frames")
    if (max(width, height) > GUIDE_IMAGE_MAX_DIMENSION or
            width * height > GUIDE_IMAGE_MAX_FRAME_PIXELS or
            frames > GUIDE_IMAGE_MAX_FRAMES or
            width * height * frames > GUIDE_IMAGE_MAX_TOTAL_PIXELS):
        raise _ImageResourceLimit()


def read_guide_image_bytes(path):
    # Bound allocation as well as decoding, even if the file grows after stat.
    with path.open("rb") as stream:
        raw = stream.read(GUIDE_IMAGE_MAX_ENCODED_BYTES + 1)
    if len(raw) > GUIDE_IMAGE_MAX_ENCODED_BYTES:
        raise _ImageResourceLimit()
    return raw


def _gif_budget(raw, width, height):
    """Walk descriptors/subblocks only; Pillow seek() may decode prior frames."""
    pos = 13
    frames = 0
    if raw[10] & 128:
        pos += 3 * (2 ** ((raw[10] & 7) + 1))

    def skip_blocks(pos):
        while True:
            size = raw[pos]
            pos += 1
            if not size:
                return pos
            pos += size
            if pos > len(raw):
                raise ValueError("truncated_gif")

    while pos < len(raw):
        marker = raw[pos]
        pos += 1
        if marker == 0x3B:
            _check_image_budget(width, height, frames)
            return frames
        if marker == 0x21:
            pos = skip_blocks(pos + 1)  # extension label, then data subblocks
        elif marker == 0x2C:
            descriptor = raw[pos:pos + 9]
            if len(descriptor) != 9:
                raise ValueError("truncated_gif_descriptor")
            x, y, w, h = [int.from_bytes(descriptor[i:i + 2], "little")
                           for i in (0, 2, 4, 6)]
            _check_image_budget(w, h)
            width, height = max(width, x + w), max(height, y + h)
            frames += 1
            _check_image_budget(width, height, frames)
            pos += 9
            if descriptor[8] & 128:
                pos += 3 * (2 ** ((descriptor[8] & 7) + 1))
            pos = skip_blocks(pos + 1)  # LZW code size, then compressed subblocks
        else:
            raise ValueError("invalid_gif_block")
    raise ValueError("missing_gif_trailer")


def _png_budget(raw, width, height, frames, default_image):
    # APNG n_frames is available in the header, but fcTL rectangles must also
    # be checked before seek/load. Skip compressed IDAT/fdAT without inflating.
    pos = 8
    controls = 0
    while pos < len(raw):
        size = int.from_bytes(raw[pos:pos + 4], "big")
        kind = raw[pos + 4:pos + 8]
        end = pos + 12 + size
        if end > len(raw) or len(kind) != 4:
            raise ValueError("truncated_png_chunk")
        if kind == b"fcTL":
            if size != 26:
                raise ValueError("invalid_png_frame_control")
            w, h, x, y = [int.from_bytes(raw[pos + i:pos + i + 4], "big")
                           for i in (12, 16, 20, 24)]
            _check_image_budget(w, h)
            if x + w > width or y + h > height:
                raise ValueError("png_frame_outside_canvas")
            controls += 1
            _check_image_budget(width, height, controls)
        pos = end
        if kind == b"IEND":
            if controls and controls != frames - int(default_image):
                raise ValueError("png_frame_count_mismatch")
            return
    raise ValueError("missing_png_trailer")


def validate_guide_image_bytes(raw):
    # A filename or matching digest cannot establish decodability. Reject
    # resource excess BEFORE verify or pixel decoding; metadata scans are
    # bounded by encoded size and do not decompress animation payloads.
    if len(raw) > GUIDE_IMAGE_MAX_ENCODED_BYTES:
        raise _ImageResourceLimit()
    try:
        with Image.open(io.BytesIO(raw), formats=("PNG", "JPEG", "GIF")) as image:
            width, height = image.size
            _check_image_budget(width, height)
            if image.format == "GIF":
                frames = _gif_budget(raw, width, height)
            else:
                frames = getattr(image, "n_frames", 1)
                _check_image_budget(width, height, frames)
                if image.format == "PNG":
                    _png_budget(raw, width, height, frames, image.info.get("default_image", False))
            image.verify()
        with Image.open(io.BytesIO(raw), formats=("PNG", "JPEG", "GIF")) as image:
            for frame in range(frames):
                image.seek(frame)
                image.load()
    except _ImageResourceLimit:
        raise
    except Image.DecompressionBombError:
        raise _ImageResourceLimit() from None
    except (OSError, ValueError, SyntaxError, IndexError, EOFError):
        raise ValueError("image_undecodable") from None


def stored_guide_projection(session, paper_id, settings):
    sections = list_sections(session, paper_id, preserve_missing_file=True)
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
    file_ids = {str(file_id) for file_id in files}
    for section in sections:
        reference = (section.get("provenance") or {}).get("file_id")
        if reference and reference not in file_ids:
            code = "source_file_not_found_or_wrong_paper"
            section["projection_errors"] = [code]
            issues.append({"section_id": section["id"], "code": code})
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
            raw = read_guide_image_bytes(path)
            if digest and hashlib.sha256(raw).hexdigest() != digest:
                errors.append("media_sha256_mismatch")
            validate_guide_image_bytes(raw)
            item["image_status"] = "available"
        except (ValueError, OSError, TypeError) as exc:
            errors.append(str(exc) if isinstance(exc, ValueError) else "image_unreadable")
            item["image_status"] = "unavailable"
        item["projection_errors"] = errors
        issues.extend({"asset_id": str(asset.id), "code": code} for code in errors)
        items.append(item)
    return sections, items, issues
