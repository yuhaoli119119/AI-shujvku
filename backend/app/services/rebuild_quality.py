"""Read-only integrity checklist of stored outputs, never a scientific verdict."""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import io
import math
import stat as stat_mode
import warnings

import fitz
from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_, select

from app.db.models import (RebuildDataRow, RebuildDataValue, RebuildValueSource,
                           RebuildVisualAsset, RebuildPaperFile, PaperSection, AuditLog)
from app.services.rebuild_acceptance import hash_bound_file
from app.services.rebuild_file_preview import _format, preview_cache_path
from app.services.rebuild_workflow_service import RebuildWorkflowError, resolve_file_path, resolve_asset_image_path

VERSION = "stored_output_quality_v1"

# Per-report physical work is bounded separately from issue pagination. These
# conservative limits leave large/late images unknown, never implicitly passed.
IMAGE_CHECK_LIMIT = 32
IMAGE_BYTE_LIMIT = 16 * 1024 * 1024
IMAGE_TOTAL_BYTES = 64 * 1024 * 1024
IMAGE_PIXEL_LIMIT = 16_000_000
IMAGE_TOTAL_PIXELS = 32_000_000
BBOX_REGION_LIMIT = 100


def _bbox_check(bbox):
    """Only the explicitly declared normalized contract is interpretable."""
    if not isinstance(bbox, dict) or bbox.get("units") != "normalized_page_fraction":
        return "unknown", "asset_bbox_unknown"
    if "regions" in bbox:
        regions = bbox["regions"]
        if not isinstance(regions, list) or not regions:
            return "unknown", "asset_bbox_unknown"
        if len(regions) > BBOX_REGION_LIMIT:
            return "unknown", "asset_bbox_budget_exceeded"
    else:
        regions = [bbox]
    unknown = False
    for region in regions:
        if not isinstance(region, dict) or region.get("units", bbox["units"]) != bbox["units"]:
            unknown = True
            continue
        coordinates = region.get("bbox") if "regions" in bbox else [region.get(key) for key in ("x0", "y0", "x1", "y1")]
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 4:
            unknown = True
            continue
        if not all(type(value) in (int, float) for value in coordinates):
            unknown = True
            continue
        # Avoid float conversion overflow for large JSON integers.
        if not all(not isinstance(value, float) or math.isfinite(value) for value in coordinates):
            return "error", "asset_bbox_nonfinite"
        x0, y0, x1, y1 = coordinates
        if not (0 <= x0 <= 1 and 0 <= x1 <= 1 and 0 <= y0 <= 1 and 0 <= y1 <= 1):
            return "error", "asset_bbox_out_of_bounds"
        if not (x0 < x1 and y0 < y1):
            return "error", "asset_bbox_reversed_or_empty"
    return ("unknown", "asset_bbox_unknown") if unknown else (None, None)


@lru_cache(maxsize=256)
def _image_at_stat(path, device, inode, size, mtime_ns, ctime_ns, pixel_allowance):
    """Bounded raster decode; cache successes and failures by file identity."""
    pixels = 0
    try:
        with open(path, "rb") as source:
            encoded = source.read(min(size, IMAGE_BYTE_LIMIT) + 1)
        if len(encoded) != size:
            return "unknown", "asset_image_changed_during_check", 0
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(encoded)) as image:
                # Only raster decoders; never invoke document/EPS rendering.
                if image.format not in {"PNG", "JPEG", "WEBP", "TIFF", "GIF", "BMP"}:
                    return "unknown", "asset_image_format_unknown", 0
                width, height = image.size
                if width <= 0 or height <= 0:
                    return "error", "asset_image_dimensions_invalid", 0
                pixels = width * height
                if pixels > min(IMAGE_PIXEL_LIMIT, pixel_allowance):
                    return "unknown", "asset_image_budget_exceeded", 0
                # Multi-frame validation is not a bounded single raster check.
                if getattr(image, "n_frames", 1) != 1:
                    return "unknown", "asset_image_multiframe_unknown", 0
                image.verify()
            with Image.open(io.BytesIO(encoded)) as image:
                image.load()
        return None, None, pixels
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return "unknown", "asset_image_budget_exceeded", 0
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        return "error", "asset_image_corrupt", pixels


def _asset_image_check(asset, settings, budget):
    if not getattr(asset, "image_path", None):
        return (None, None) if asset.asset_type == "table" else ("unknown", "asset_image_unknown")
    if budget["checks"] >= IMAGE_CHECK_LIMIT:
        return "unknown", "asset_image_budget_exceeded"
    budget["checks"] += 1
    try:
        root = settings.storage_root.resolve()
        candidate = (root / asset.image_path).resolve()
        if not candidate.is_relative_to(root):
            return "error", "asset_image_outside_storage_root"
        path = resolve_asset_image_path(asset, settings)
        current = path.stat()
        if not stat_mode.S_ISREG(current.st_mode):
            return "unknown", "asset_image_unavailable"
        if current.st_size > IMAGE_BYTE_LIMIT or budget["bytes"] + current.st_size > IMAGE_TOTAL_BYTES:
            return "unknown", "asset_image_budget_exceeded"
        budget["bytes"] += current.st_size
        severity, code, pixels = _image_at_stat(str(path), current.st_dev, current.st_ino,
            current.st_size, current.st_mtime_ns, current.st_ctime_ns, IMAGE_TOTAL_PIXELS - budget["pixels"])
        budget["pixels"] += pixels
        after = path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return "unknown", "asset_image_changed_during_check"
        return severity, code
    except RebuildWorkflowError:
        return "error", "asset_image_missing"
    except (OSError, ValueError, TypeError, RuntimeError):
        return "unknown", "asset_image_unavailable"


def _asset_mechanics(assets, settings, *, requested=False, offset=0, limit=32):
    """Optional slice diagnostics, outside the established acceptance contract."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= IMAGE_CHECK_LIMIT:
        raise RebuildWorkflowError("asset_mechanics_range_invalid")
    selected = assets[offset:offset + limit] if requested else []
    budget = {"checks": 0, "bytes": 0, "pixels": 0}
    issues = []
    for asset in selected:
        checks = []
        bbox = getattr(asset, "bbox", None)
        if bbox is not None or asset.asset_type != "table":
            checks.append(_bbox_check(bbox))
        checks.append(_asset_image_check(asset, settings, budget))
        for severity, code in checks:
            if code:
                issues.append({"severity": severity, "code": code, "kind": "asset", "id": str(asset.id),
                    "action": "Review original coordinates and the stored raster within storage root; preserve unknown units. Mechanical validity does not establish complete axes, legends or scientific content; do not recrop automatically."})
    counts = dict(Counter(item["severity"] for item in issues))
    end = min(offset + len(selected), len(assets)) if requested else 0
    return {"requested": requested,
        "status": ("not_requested" if not requested else "not_checked" if not selected else "errors" if counts.get("error") else
                   "unknown" if counts.get("unknown") else "slice_checks_passed"),
        "counts": counts, "issues": issues, "issues_total": len(issues),
        "scope": {"ordering": "asset_id_ascending", "asset_total": len(assets),
            "asset_offset": offset, "asset_limit": limit,
            "selected_asset_ids": [str(asset.id) for asset in selected], "selected_count": len(selected),
            "not_checked_outside_slice_count": len(assets) - len(selected),
            "not_checked_before_count": min(offset, len(assets)) if requested else 0,
            "not_checked_after_count": len(assets) - end,
            "has_more": end < len(assets), "next_asset_offset": end if end < len(assets) else None,
            "unverified_selected_asset_ids": sorted({item["id"] for item in issues}),
            "image_attempts": budget["checks"], "image_attempt_limit": IMAGE_CHECK_LIMIT,
            "image_byte_limit": IMAGE_BYTE_LIMIT, "image_total_byte_limit": IMAGE_TOTAL_BYTES,
            "image_pixel_limit": IMAGE_PIXEL_LIMIT, "image_total_pixel_limit": IMAGE_TOTAL_PIXELS,
            "bbox_region_limit": BBOX_REGION_LIMIT},
        "scientific_visual_completeness": "not_checked", "formal_acceptance": "not_evaluated",
        "summary_rule": "Optional diagnostics only for selected asset IDs. Not requested/outside-slice/budget-limited assets are unverified. Slice success and stored_checks_passed do not assert full asset mechanics, scientific completeness or formal acceptance."}


@lru_cache(maxsize=512)
def _pages(path, device, inode, size, mtime_ns, ctime_ns):
    with fitz.open(path) as document:
        return document.page_count


def file_inventory(files, settings, *, force=False):
    result = {}
    for file in files:
        item = {"file_id": str(file.id), "role": file.role, "source_sha256": file.sha256,
                "original_filename": file.original_filename,
                "status": "unknown", "physical_pages": None}
        try:
            path = resolve_file_path(file, settings)
            source_digest = hash_bound_file(path, force=force)
            if source_digest != file.sha256:
                raise RebuildWorkflowError("source_sha256_mismatch")
            if path.stat().st_size != file.file_size:
                raise RebuildWorkflowError("source_file_size_mismatch")
            source_format = _format(path)
            if source_format not in {"pdf", "docx"}:
                raise RebuildWorkflowError("source_preview_format_unsupported")
            source_is_pdf = source_format == "pdf"
            if not source_is_pdf:
                path = preview_cache_path(file, settings)
                item["page_basis"] = "existing_derived_pdf"
            else:
                item["page_basis"] = "original_pdf"
            stat = path.stat()
            item["reading_pdf_sha256"] = source_digest if source_is_pdf else hash_bound_file(path, force=force)
            item["physical_pages"] = _pages(str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            item["status"] = "verified" if item["physical_pages"] > 0 else "unknown"
        except (RebuildWorkflowError, OSError, ValueError, RuntimeError) as exc:
            item["reason"] = str(exc) if isinstance(exc, RebuildWorkflowError) else "physical_pdf_unavailable"
        result[file.id] = item
    return result


def integrity_report(session, paper_id, settings, *, offset=0, limit=100, file_states=None,
                     include_asset_mechanics=False, asset_offset=0, asset_limit=32):
    """Batch graph reads. Missing source knowledge stays unknown, not invented.

    Save endpoints still allow drafts. Errors block a completeness claim;
    warnings/unknowns guide a reader and never imply PDF-wide coverage.
    """
    from app.services.rebuild_workflow_service import _required_paper
    _required_paper(session, paper_id)
    rows = list(session.scalars(select(RebuildDataRow).where(RebuildDataRow.paper_id == paper_id).order_by(RebuildDataRow.id)))
    row_ids = [row.id for row in rows]
    values = list(session.scalars(select(RebuildDataValue).where(or_(
        RebuildDataValue.paper_id == paper_id, RebuildDataValue.row_id.in_(row_ids))).order_by(RebuildDataValue.id)))
    value_ids = [value.id for value in values]
    sources = list(session.scalars(select(RebuildValueSource).where(or_(
        RebuildValueSource.paper_id == paper_id, RebuildValueSource.row_id.in_(row_ids),
        RebuildValueSource.value_id.in_(value_ids))).order_by(RebuildValueSource.id)))
    assets = list(session.scalars(select(RebuildVisualAsset).where(RebuildVisualAsset.paper_id == paper_id).order_by(RebuildVisualAsset.id)))
    files = list(session.scalars(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id).order_by(RebuildPaperFile.id)))
    row_map, value_map, asset_map = ({row.id: row for row in rows}, {value.id: value for value in values}, {asset.id: asset for asset in assets})
    file_map = {file.id: file for file in files}
    from app.services.rebuild_file_lifecycle import file_set, active_files
    lifecycle = file_set(session, paper_id)
    current_files = sorted(active_files(session, paper_id), key=lambda file: str(file.id))
    active_ids = {file.id for file in current_files}
    physical = file_inventory(current_files, settings) if file_states is None else {
        file.id: file_states[str(file.id)] for file in current_files}
    parent_labels, parent_groups = defaultdict(list), defaultdict(list)
    for parent in assets:
        if parent.asset_type == "figure":
            if parent.figure_label: parent_labels[(parent.file_id, parent.figure_label)].append(parent.id)
            if parent.logical_group_key: parent_groups[(parent.file_id, parent.logical_group_key)].append(parent.id)
    issues = []
    def issue(severity, code, kind, record_id, action, **extra):
        issues.append({"severity": severity, "code": code, "kind": kind, "id": str(record_id), "action": action, **extra})
    if lifecycle['ambiguous_main']:
        issue('error', 'preferred_main_source_ambiguous', 'paper', paper_id,
              'Explicitly select one current main using the file lifecycle contract; retain every original file and scientific binding.')
    def binding_check(file_id, kind, record_id):
        if file_id in file_map and file_id not in active_ids:
            code = ("source_file_superseded_requires_scientific_review" if str(file_id) in lifecycle["scientific_review_required_file_ids"]
                    else "source_file_preference_unresolved_requires_scientific_review")
            issue("error", code, kind, record_id,
                  "Retain the original file UUID, position and scientific values. Independently review the evidence before an explicit scientific correction; replacement never remaps existing objects.",
                  file_id=str(file_id), scientific_review_required=True)
    def page_check(file_id, page, kind, record_id):
        if type(page) is not int or page < 1:
            issue("error", "physical_page_invalid", kind, record_id, "Read the source PDF and supply its actual positive physical page.", file_id=str(file_id) if file_id else None)
        elif file_id in physical and physical[file_id]["physical_pages"] is not None and page > physical[file_id]["physical_pages"]:
            issue("error", "physical_page_out_of_range", kind, record_id, "Correct the source file/page from the actual PDF.", file_id=str(file_id), page=page)
    def ooxml_check(file_id, identity, kind, record_id, page_number=None, pages=None):
        if not isinstance(identity, dict) or identity.get("page_basis") != "ooxml":
            return False
        from app.services.rebuild_file_preview import validate_page_identity
        try:
            if file_id not in file_map or pages:
                raise RebuildWorkflowError("ooxml_identity_owner_or_page_mismatch")
            validate_page_identity(file_map[file_id], settings, {**identity, "page_number": page_number})
        except (RebuildWorkflowError, OSError) as exc:
            issue("error", "ooxml_identity_unverified", kind, record_id,
                  "Verify same-file DOCX bytes/hash and exact OOXML locator; do not invent physical pages.", reason=str(exc))
        return True  # OOXML failures are location failures, never missing PDF pages.
    if any(file.role == "main" for file in files) and lifecycle["preferred_main_id"] is None:
        issue("unknown", "preferred_main_source_ambiguous", "paper", paper_id,
              "Select the preferred active main through the authenticated CAS file lifecycle endpoint; do not infer the preferred source from insertion order.")
    for file in current_files:
        item = physical[file.id]
        if item["status"] != "verified":
            issue("error" if item.get("reason") in {"source_sha256_mismatch", "source_file_size_mismatch"} else "unknown", item.get("reason", "physical_source_unknown"),
                  "file", file.id, "Verify the registered original bytes and existing reading PDF; do not reparse or replace evidence automatically.")
    for asset in assets:
        binding_check(asset.file_id, "asset", asset.id)
        if asset.file_id not in file_map:
            issue("error" if asset.file_id else "unknown", "asset_file_identity_unverified", "asset", asset.id,
                  "Bind the asset to this paper's actual registered file with evidence.", file_id=str(asset.file_id) if asset.file_id else None)
        pages = asset.page_numbers or []
        if ooxml_check(asset.file_id, (asset.provenance or {}).get("page_identity"), "asset", asset.id, pages=pages):
            pass
        elif not isinstance(pages, list) or not pages:
            issue("unknown", "asset_physical_pages_unknown", "asset", asset.id, "Read the source and record actual physical pages.")
        else:
            for page in pages: page_check(asset.file_id, page, "asset", asset.id)
        if asset.asset_type == "subfigure":
            parents = set(parent_groups.get((asset.file_id, asset.logical_group_key), [])) | set(parent_labels.get((asset.file_id, asset.figure_label), []))
            if len(parents) != 1:
                issue("warning", "subfigure_parent_missing_or_ambiguous", "asset", asset.id,
                      "A separate whole-figure record is not mandatory for a saved crop. Review the same-file label/group if a whole-figure link is intended; this warning does not assert missing science or block by itself.")
    from app.services.rebuild_source_lifecycle import source_is_current
    sources_by_value = defaultdict(list)
    for source in sources:
        if source.value_id in value_map and source_is_current(value_map[source.value_id], source.id):
            sources_by_value[source.value_id].append(source)
        value, row = value_map.get(source.value_id), row_map.get(source.row_id)
        if value is None or row is None or source.paper_id != paper_id or value.paper_id != paper_id or value.row_id != source.row_id:
            issue("error", "source_value_row_paper_mismatch", "source", source.id,
                  "Compare the source, value, row and paper identities; repair through explicit reviewed correction, never borrow another paper's evidence.")
        asset = asset_map.get(source.asset_id) if source.asset_id else None
        if source.asset_id and asset is None:
            issue("error", "source_asset_identity_mismatch", "source", source.id, "Use an asset from the same paper.")
        if source.file_id and source.file_id not in file_map:
            issue("error", "source_file_identity_mismatch", "source", source.id, "Use this paper's registered evidence file.")
        if asset and source.file_id and asset.file_id != source.file_id:
            issue("error", "source_asset_file_mismatch", "source", source.id, "Verify which PDF owns the asset; source and asset must agree.")
        file_id = source.file_id or (asset.file_id if asset else None)
        binding_check(file_id, "source", source.id)
        if source.page_number is not None:
            page_check(file_id, source.page_number, "source", source.id)
            if asset and isinstance(asset.page_numbers, list) and asset.page_numbers and source.page_number not in asset.page_numbers:
                issue("warning", "source_page_outside_asset_pages", "source", source.id, "Check caption/body cross-page locators against the original PDF; do not rewrite automatically.")
        if not file_id:
            issue("unknown", "source_file_unknown", "source", source.id, "Locate the original registered file; a quote alone does not establish file identity.")
        from app.services.rebuild_file_preview import stored_source_identity
        is_ooxml = ooxml_check(file_id, stored_source_identity(source), "source", source.id, page_number=source.page_number)
        if source.page_number is None and not source.asset_id and not is_ooxml:
            issue("unknown", "source_physical_page_unknown", "source", source.id, "Record an actual physical page once verified.")
    for value in values:
        if value.row_id not in row_map or value.paper_id != paper_id:
            issue("error", "value_row_paper_mismatch", "value", value.id, "Correct the value's row/paper association using reviewed evidence.")
        if value.value_type == "missing":
            if value.raw_value is not None or value.numeric_value is not None or not (value.missing_reason or "").strip():
                issue("error", "missing_value_reason_or_payload_invalid", "value", value.id, "Keep unreported values missing and give an honest missing_reason; do not fabricate a measurement.")
        elif not sources_by_value[value.id]:
            issue("error", "value_source_missing", "value", value.id, "Attach original evidence or explicitly report the item as unreported/unknown, without inventing values.")
        if value.numeric_value is not None and not math.isfinite(value.numeric_value):
            issue("error", "nonfinite_numeric_value", "value", value.id, "Use a finite reported number or an explicit missing/non-numeric raw representation; never substitute an invented number.")
        if value.value_type in {"estimated", "derived"} and any(not (source.estimate_basis or "").strip() for source in sources_by_value[value.id]):
            issue("error", "estimate_basis_missing", "value", value.id, "Record the explicit estimate/derivation basis for each supporting source, or keep the result unresolved.")
        if value.preferred_source_id and value.preferred_source_id not in {s.id for s in sources_by_value[value.id]}:
            issue("error", "preferred_source_wrong_value", "value", value.id, "Choose an existing source belonging to this same value.")
    # Duplicated imported aliases are ambiguous, never silently merged.
    aliases = defaultdict(list)
    values_by_row = defaultdict(list)
    for value in values: values_by_row[value.row_id].append(value)
    for row in rows:
        from app.services.rebuild_workflow_service import _stored_identity
        try:
            _stored_identity(session, row, values=values_by_row[row.id])
        except RebuildWorkflowError:
            issue("error", "stored_row_identity_conflict", "row", row.id, "Compare contradictory identity declarations with their evidence; preserve unresolved alternatives, do not normalize by guessing.")
        raw_aliases = (row.properties or {}).get("_rebuild_import", {}).get("aliases", [])
        for alias in set([row.row_key, *[alias for alias in raw_aliases if isinstance(alias, str)]]):
            aliases[alias].append(str(row.id))
    for alias, ids in aliases.items():
        if len(ids) > 1:
            issue("warning", "duplicate_row_alias", "row", ids[0], "Use the returned canonical row ID/key and review the exact identity before importing; do not auto-deduplicate.", row_ids=ids)
    from app.services.rebuild_sections import EVENT, VERSION as SECTION_VERSION, SECTION_FIELDS, _checked_snapshot
    from app.schemas.rebuild import RebuildSectionInput
    from pydantic import ValidationError
    # Read complete original text for digest comparison, never embeddings.
    sections = list(session.execute(select(PaperSection.id, *[getattr(PaperSection, field) for field in SECTION_FIELDS])
        .where(PaperSection.paper_id == paper_id).order_by(PaperSection.id)))
    events = {event.target_id: event for event in session.scalars(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == EVENT, AuditLog.source == SECTION_VERSION)
        .order_by(AuditLog.created_at, AuditLog.id))}
    legacy_sections, section_pages = [], defaultdict(set)
    for section in sections:
        event = events.get(str(section.id))
        if event is None:
            legacy_sections.append(str(section.id))
            continue  # Existing unaudited chapters are unknown, not forged imports.
        try:
            snapshot = _checked_snapshot(section, event)
            declared = RebuildSectionInput.model_validate(snapshot)
            from uuid import UUID
            file_id = UUID(snapshot["file_id"])
            if file_id not in file_map or snapshot.get("source_sha256") != file_map[file_id].sha256:
                raise RebuildWorkflowError("section_file_hash_mismatch")
            binding_check(file_id, "section", section.id)
            actual = physical.get(file_id, {})
            if actual.get("status") == "verified":
                expected_basis = "source_pdf" if actual["page_basis"] == "original_pdf" else "derived_pdf"
                if (declared.page_basis != expected_basis or
                        event.payload.get("reading_pdf_sha256") != actual["reading_pdf_sha256"] or
                        declared.reading_pdf_sha256 is not None and declared.reading_pdf_sha256 != actual["reading_pdf_sha256"]):
                    raise RebuildWorkflowError("section_reading_pdf_sha256_mismatch")
            for page in [snapshot.get("page_start"), snapshot.get("page_end")]:
                page_check(file_id, page, "section", section.id)
            for source in snapshot.get("sources", []):
                if source.get("file_id") and str(source["file_id"]) != str(file_id):
                    raise RebuildWorkflowError("section_source_file_mismatch")
                page = source.get("page_number")
                page_check(file_id, page, "section", section.id)
                if type(page) is int and section.page_start <= page <= section.page_end:
                    if file_id in active_ids:
                        section_pages[file_id].add(page)
        except (RebuildWorkflowError, KeyError, ValueError, TypeError, ValidationError):
            issue("error", "section_source_or_record_changed", "section", section.id,
                  "Compare the existing imported original chapter and its immutable source event; report conflicts without overwriting mature text.")
    counts = dict(Counter(item["severity"] for item in issues))
    return {"version": VERSION, "paper_id": str(paper_id), "status": "errors" if counts.get("error") else "unknown" if counts.get("unknown") else "warnings" if issues else "stored_checks_passed",
            "counts": counts, "issues_total": len(issues), "issues": issues if limit is None else issues[offset:offset + limit], "offset": offset, "limit": limit,
            "has_more": limit is not None and offset + limit < len(issues), "next_offset": offset + limit if limit is not None and offset + limit < len(issues) else None,
            "stored_counts": {"rows": len(rows), "values": len(values), "sources": len(sources), "assets": len(assets)},
            "files": list(physical.values()), "file_lifecycle": lifecycle,
            "file_history": [item for item in lifecycle["items"] if item["id"] not in {str(fid) for fid in active_ids}],
            "asset_mechanics": _asset_mechanics(assets, settings,
                requested=include_asset_mechanics, offset=asset_offset, limit=asset_limit),
            "full_pdf_completeness": "unknown_until_explicit_physical_page_coverage_and_independent_review",
            "section_scope": {"stored_sections": len(sections), "legacy_without_file_provenance": len(legacy_sections),
                "legacy_section_ids": legacy_sections, "file_page_evidence": [{"file_id": str(file_id), "stored_evidence_pages": sorted(pages)} for file_id, pages in section_pages.items()],
                "rule": "Stored section evidence only; unknown legacy source coverage is not a new mandatory scientific field or proof that pages were fully extracted."},
            "unreported_fields": "not_required_without_source_evidence", "scientific_acceptance": "not_implied", "read_only": True}


def quality_report(session, paper_id, settings, snapshot, *, offset=0, limit=100, coverage_plan_report=None,
                   include_asset_mechanics=False, asset_offset=0, asset_limit=32):
    """One paginated checklist across integrity, tables and known coverage gaps.

    Reuse the caller's table snapshot. No extra table/PDF representation scan,
    native polling, scientific writes or history promotion is performed.
    """
    from app.db.models import WorkflowJob
    from app.services.rebuild_acceptance import JOB_TYPE
    result = integrity_report(session, paper_id, settings, limit=None, file_states=snapshot.get("file_reading_identity"),
        include_asset_mechanics=include_asset_mechanics, asset_offset=asset_offset, asset_limit=asset_limit)
    graph_counts = result["counts"]
    issues = result["issues"]
    tables = snapshot.get("table_completeness", [])
    pending = [table for table in tables if not table["complete"]]
    for table in pending:
        issues.append({"severity": "unknown", "code": "table_representation_incomplete", "kind": "table",
                       "id": table.get("asset_id") or table.get("legacy_id"), "file_id": table.get("file_id"),
                       "physical_pages": table.get("physical_pages", []), "reason": table["reason"],
                       "action": "Read the original table and submit a verified formal representation plus same-file physical caption/body sources; never infer cells from raw text.",
                       "action_zh": "原始文本仍可查看复制。请按原文核实后补正式表格表示与同文件表题/正文页来源；不能猜测行列，也不自动回填历史记录。"})
    for section_id in result["section_scope"]["legacy_section_ids"]:
        issues.append({"severity": "unknown", "code": "legacy_section_file_scope_unknown", "kind": "section", "id": section_id,
                       "action": "Legacy source coverage is unknown; retain original text, do not fabricate historical audit or scientific content.",
                       "action_zh": "历史章节的文件覆盖范围未知；保留现有原文，不要求补造历史审计或科学内容。后续只能依据实际 PDF 核对来源范围。"})
    job = session.scalar(select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE,
        WorkflowJob.payload.op("->>")("paper_id") == str(paper_id)).order_by(WorkflowJob.created_at.desc(), WorkflowJob.job_id.desc()))
    coverage = {}
    if job and job.payload.get("snapshot", {}).get("revision") == snapshot["revision"]:
        coverage = {item["file_id"]: item for item in job.payload.get("coverage", [])}
    coverage_summary = []
    for file in result["files"]:
        declared = coverage.get(file["file_id"], {})
        pages = declared.get("covered_pages", [])
        total = file["physical_pages"]
        valid = (file["status"] == "verified" and declared.get("source_sha256") == file["source_sha256"] and
                 declared.get("reading_pdf_sha256") == file.get("reading_pdf_sha256") and isinstance(pages, list) and
                 all(type(page) is int and 1 <= page <= total for page in pages))
        complete = bool(valid and len(set(pages)) == total)
        coverage_summary.append({"file_id": file["file_id"], "role": file["role"], "physical_pages": total,
                                 "status": "declared_full_not_independently_accepted" if complete else "unknown_or_partial",
                                 "declared_covered_pages": pages if valid else [], "scope_kind":"formal_acceptance_declaration", "scientific_acceptance": "not_implied"})
        if not complete:
            issues.append({"severity": "unknown", "code": "file_extraction_coverage_unknown", "kind": "file", "id": file["file_id"],
                           "file_id": file["file_id"], "action": "Formal acceptance coverage is unknown/partial and separate from the extraction plan declaration. This stage does not activate freeze/review; do not re-extract science merely to clear this issue.",
                           "action_zh": "正式验收范围声明与原文清单范围声明分别记录；清单声明全页不代表科学验收通过，无需因此重复提取已有结果。"})
    from app.services.extraction_coverage import report as coverage_report
    plan = coverage_plan_report if coverage_plan_report is not None else coverage_report(session, paper_id, snapshot, limit=None)
    if plan['legacy_without_plan']:
        issues.append({'severity':'unknown', 'code':'coverage_plan_missing', 'kind':'coverage_plan', 'id':str(paper_id),
                       'action_zh':'尚无原文对象与材料条件清单。请先阅读正文和SI，声明实际对象和已核查范围；不能用已有结果数量反推全篇完整。'})
    for scope in plan['summary']['files']:
        if scope['status']=='unknown':
            issues.append({'severity':'unknown', 'code':'coverage_plan_scope_unknown', 'kind':'coverage_plan', 'id':scope['file_id'],
                           'file_id':scope['file_id'], 'action_zh':'本文件仍有未声明或无法核实的物理页范围；请只读核对原文后补范围声明，不据文本层为空断言无数据。'})
    for item in plan['items']:
        if item['effective_state'] in {'pending','unknown'}:
            issues.append({'severity':'unknown', 'code':'coverage_plan_check_stale' if item['freshness']=='stale' else 'coverage_plan_item_missing',
                           'kind':item['kind'], 'id':item['item_key'], 'file_id':item['source']['file_id'],
                           'title':item['title'], 'declared_state':item['state'], 'effective_state':item['effective_state'],
                           'reason':item['reason'], 'problems':item['problems'],
                           'action_zh':('对应结果或原文件已变化，请读回当前输出并重新核对本项，不能沿用旧提取完成声明。' if item['freshness']=='stale' else
                                        '请核对原文中本对象及材料条件，仅补所列缺项；原文未报告、未知与不适用须分别注明依据，图像或表格缺项不能用章节文字抵消。')})
    actions = {"physical_page_out_of_range": "核对该文件的真实物理页数，按原始证据修正对应来源页；不要自动换到邻页。",
               "source_asset_file_mismatch": "核对图表实际所属 PDF，来源与图表应绑定同一文件；保留冲突，不能借用另一文件。",
               "missing_value_reason_or_payload_invalid": "原文未报告项保持 missing，并填写真实 missing_reason；不要补造数值。",
               "subfigure_parent_missing_or_ambiguous": "单独裁出的子图允许保存；若要关联整图，请核对同文件的表号/组标识。本项仅提示，不要求每个子图都新增独立父记录。",
               "section_source_or_record_changed": "核对现有章节原文、每页原句/位置及对应阅读 PDF 哈希。保留成熟原文和冲突，不覆盖或补造历史。"}
    for item in issues:
        item.setdefault("action_zh", actions.get(item["code"], "请打开对应原文件，核对所列对象、文件归属与真实物理页；仅按已核实证据修正，保留原值和未裁决冲突。"))
    counts = dict(Counter(item["severity"] for item in issues))
    status = "partial" if counts.get("error") or pending else "unknown" if counts.get("unknown") else "warnings" if issues else "stored_checks_passed"
    return {**result, "status": status, "counts": counts, "issues_total": len(issues), "issues": issues[offset:offset + limit],
            "offset": offset, "limit": limit, "has_more": offset + limit < len(issues),
            "next_offset": offset + limit if offset + limit < len(issues) else None,
            "output_revision": snapshot["revision"], "quality_revision": plan['revision'],
            "coverage_plan": {key:plan[key] for key in ('plan_revision','revision','summary','legacy_without_plan','checked_output_revision','next_page')},
            "table_completeness": tables,
            "dimensions": {"stored_integrity": graph_counts, "table_representation": {"total": len(tables), "incomplete": len(pending)},
                           "section_file_scope": {"total": result["section_scope"]["stored_sections"], "legacy_unknown": len(result["section_scope"]["legacy_section_ids"])},
                           "file_extraction_coverage": coverage_summary},
            "summary_rule": "Totals include known table, file-coverage and legacy-section unknowns. Unknown is not absent/unreported; no top-level result asserts full PDF extraction or scientific acceptance."}


def producer_contract():
    from app.services.rebuild_field_semantics import field_semantics_contract
    from app.services.extraction_coverage import contract as coverage_contract
    from app.services.staged_extraction import contract as staged_contract
    return {"version": VERSION, "drafts_allowed": True, "field_semantics": field_semantics_contract(), "coverage_plan":coverage_contract(), "staged_checks":staged_contract(),
            "row_import": {"preflight_endpoint": "/api/rebuild/rows/import/preflight", "import_endpoint": "/api/rebuild/rows/import",
                           "preflight": "Same body/auth/CSRF as import; rolled-back simulation, no receipt/IDs/reservation. Revalidates current source/identity/conflicts. State can change afterwards.",
                           "result": "Check complete/outcome/summary and every errors row_index/row_key/field_index; HTTP200 is not all-success. Pending/conflict may already be persisted.",
                           "recovery": "Read back current canonical row first. recovery is advisory only; explicitly fix rejected fields, preserve full identity/condition and use NEW request_id. Conflicts/pending/uncertain identity block automatic recovery.",
                           "unknown_result": "Retry EXACT original body and SAME request_id; replay returns original receipt, never a current-state guarantee."}, "complete_requires": "Check known stored integrity, full registered source coverage and formal table representations; never infer unseen PDF contents from counts.",
            "table_example": {"asset_type": "table", "file_id": "registered-file-uuid", "figure_label": "Table S13", "page_numbers": [71, 72],
                "provenance": {"table_data": {"columns": ["Object", "Time"], "rows": [["s", "1.00s"], ["unreported", None]]},
                    "table_sources": [{"file_id": "same-registered-file-uuid", "page_number": 71, "role": "caption", "locator": "exact caption location"},
                                      {"file_id": "same-registered-file-uuid", "page_number": 72, "role": "body", "locator": "exact body location"}]}},
            "table_rule": "Submit original table_data (all cells strings/null) or original table_markdown plus table_sources for real caption/body pages. context_text is readable draft only; never guess cells, precision, units or missing values.",
            "asset_mechanics": {
                "endpoint_template": "/api/rebuild/papers/{paper_id}/quality?include_asset_mechanics=true&asset_offset=0&asset_limit=32",
                "default": "not_requested", "result_key": "asset_mechanics",
                "continuation": "Use asset_mechanics.scope.next_asset_offset and expected_revision=output_revision; asset_limit is at most 32. Existing offset/limit paginate legacy issues only.",
                "scope": "Independent per-slice status/issues; does not change stored-integrity counts or formal acceptance rules. Outside-slice, budget-limited and unknown records remain unverified. No scientific visual completeness claim."},
            "figures": "Keep whole figure/subfigure identities in the same paper/file with actual physical pages and explicit labels; no cross-file inheritance.",
            "pageless_docx": {"page_basis": "ooxml", "page_number": None, "page_numbers": [],
                "ooxml_locator": "word/document.xml#body-child[2]/w:tbl[1]",
                "source_sha256": "exact registered DOCX SHA256, checked against original bytes",
                "locator_rule": "1-based body-child and w:element indices; first element selects within the body child including itself, later steps select direct XML children. Target must exist in word/document.xml. DOCX only; no docProps page inference or borrowed PDF pages.",
                "storage": "asset provenance.page_identity and value conflict.source_page_identities; original download, no physical-page link; does not certify scientific acceptance"},
            "asset_metadata_cas": {"fields": ["page_numbers", "y_axis_unit", "material_mapping"],
                "rule": "Exactly one field per expected_revision/request_id correction. material_mapping is a nonempty bounded JSON object; full before/after snapshots, semantic changed_fields only, immutable audit and same-body replay. Pageless OOXML assets cannot acquire PDF pages through metadata CAS."},
            "values": "One scientific fact per row identity/condition; source row/value/paper/asset/file must agree. Unreported items use missing_reason, not invented required numbers.",
            "sections": "Follow section_write_contract for complete original text, separate explanation and exact source/hash/pages. Unknown legacy coverage stays unknown.",
            "completion": "Use quality endpoint to identify errors/warnings/unknowns. Fix only authorized evidence-backed gaps; incomplete extraction remains partial. No counts, HTTP200 or model completion proves acceptance."}
