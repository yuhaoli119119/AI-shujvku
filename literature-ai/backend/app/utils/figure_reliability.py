from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.utils.artifact_paths import resolve_persisted_artifact_path


def build_figure_image_review(
    figure: Any,
    *,
    settings: Settings,
    check_asset_exists: bool = True,
) -> dict[str, Any]:
    """Build a read-only reliability summary for a parsed figure crop."""
    image_path = _get(figure, "image_path")
    prov = _get(figure, "prov") or []
    bbox = first_bbox(prov)
    bbox_size = bbox_size_points(bbox)
    full_page_image_path = first_prov_value(prov, "full_page_image_path")
    asset_path = (
        resolve_persisted_artifact_path(image_path, category="figures", settings=settings)
        if check_asset_exists
        else None
    )
    pixel_size, file_size = image_file_summary(asset_path)
    pixel_size = pixel_size or first_prov_value(prov, "pixel_size")
    flags: list[str] = []

    if not image_path:
        flags.append("missing_image_path")
    elif check_asset_exists and asset_path is None:
        flags.append("missing_image_file")
    if bbox is None:
        flags.append("missing_parser_bbox")
    if _get(figure, "page") is None:
        flags.append("missing_pdf_page")
    if not full_page_image_path:
        flags.append("missing_full_page_snapshot")
    if is_small_crop(pixel_size, bbox_size):
        flags.append("small_crop_or_subfigure")
    if is_extreme_aspect(pixel_size):
        flags.append("extreme_aspect_ratio")

    stored_crop_status = _get(figure, "crop_status")
    if stored_crop_status in {"needs_recrop", "caption_only", "needs_review"}:
        flags.append(str(stored_crop_status))

    crop_status = stored_crop_status or ("parser_bbox_crop_candidate" if bbox is not None else "caption_only_candidate")
    if flags:
        crop_status = "needs_review"

    return {
        "crop_status": crop_status,
        "stored_crop_status": stored_crop_status,
        "review_required": bool(flags),
        "flags": list(dict.fromkeys(flags)),
        "local_path": str(asset_path) if asset_path else None,
        "full_page_image_path": full_page_image_path,
        "file_size_bytes": file_size,
        "pixel_size": pixel_size if isinstance(pixel_size, dict) else None,
        "bbox_points": bbox,
        "bbox_size_points": bbox_size,
        "crop_confidence": _get(figure, "crop_confidence"),
        "crop_source": _get(figure, "crop_source"),
        "locator_reliability": "needs_review" if flags else "candidate_reliable",
        "note": "Verify this figure against the PDF page before using it as evidence."
        if flags
        else "Parser crop is available; still treat it as an unverified figure candidate.",
    }


def first_bbox(prov: list[Any] | None) -> dict[str, Any] | None:
    if not prov:
        return None
    for item in prov:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if isinstance(bbox, dict):
            return bbox
    return None


def first_prov_value(prov: list[Any] | None, key: str) -> Any:
    if not prov:
        return None
    for item in prov:
        if isinstance(item, dict) and item.get(key):
            return item.get(key)
    return None


def bbox_size_points(bbox: dict[str, Any] | None) -> dict[str, float] | None:
    if not bbox:
        return None
    try:
        width = abs(float(bbox.get("r", 0)) - float(bbox.get("l", 0)))
        height = abs(float(bbox.get("t", 0)) - float(bbox.get("b", 0)))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"width": round(width, 2), "height": round(height, 2)}


def image_file_summary(path: Path | None) -> tuple[dict[str, int] | None, int | None]:
    if path is None:
        return None, None
    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = None
    try:
        from PIL import Image

        with Image.open(path) as image:
            return {"width": int(image.width), "height": int(image.height)}, file_size
    except Exception:
        return None, file_size


def is_small_crop(pixel_size: dict[str, Any] | None, bbox_size: dict[str, float] | None) -> bool:
    if pixel_size:
        width = int(pixel_size.get("width") or 0)
        height = int(pixel_size.get("height") or 0)
        if width and height and (width < 280 or height < 160):
            return True
    if bbox_size:
        width = float(bbox_size.get("width") or 0)
        height = float(bbox_size.get("height") or 0)
        if width and height and (width < 140 or height < 80):
            return True
    return False


def is_extreme_aspect(pixel_size: dict[str, Any] | None) -> bool:
    if not pixel_size:
        return False
    width = int(pixel_size.get("width") or 0)
    height = int(pixel_size.get("height") or 0)
    if width <= 0 or height <= 0:
        return False
    aspect = width / height
    return aspect > 6.0 or aspect < 0.17


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
