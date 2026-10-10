from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
import csv
import io
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.db.models import RebuildValueSource
from app.schemas.rebuild import (
    RebuildAssetsImportRequest,
    RebuildAnalysisRequest,
    RebuildRowsImportRequest,
    RebuildDataRowInput,
    RebuildVisualAssetCropRequest,
    RebuildVisualAssetRequest,
    RebuildValueCorrectionRequest,
    RebuildRowIdentityCorrectionRequest,
)
from app.services.artifact_store import ArtifactStore
from app.services.batch_catalog import batch_catalog
from app.services.rebuild_file_preview import resolve_original_download, resolve_reading_pdf
from app.services.rebuild_workflow_service import (
    RebuildWorkflowError,
    analyze,
    associate_pdf_file,
    get_asset,
    get_analysis_run,
    get_file,
    get_paper,
    import_data_row,
    list_assets,
    list_files,
    list_papers,
    list_rows,
    reaction_templates,
    resolve_asset_image_path,
    resolve_file_path,
    resolve_paper_source_pdf,
    serialize_asset,
    serialize_file,
    serialize_value,
    _required_paper,
    upsert_visual_asset,
    prepare_visual_asset,
    update_data_value,
    correct_row_identity,
)
from app.security.session_auth import require_session, SessionState, origin_allowed, csrf_token_valid
from app.services.ai_extract_service import (
    JOB_TYPE,
    create_ai_extract_job,
    get_latest_job,
    has_running_job,
    serialize_job,
    sync_job_status,
)


router = APIRouter(tags=["rebuild"])
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


def _http_error(exc: RebuildWorkflowError) -> HTTPException:
    status = 404 if str(exc) in {"paper_not_found", "file_not_found", "asset_not_found"} else (
        409 if str(exc) in {"derived_pdf_preview_unavailable", "source_sha256_mismatch"} else 400
    )
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/papers/{paper_id}/ai-extract/jobs")
async def create_ai_extract_job_endpoint(
    paper_id: UUID,
    request: Request,
    model: Literal["gpt-6-luna", "cn:deepseek-v4.1-flash"] | None = Body(default=None, embed=True),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _auth: SessionState = Depends(require_session),
) -> dict[str, Any]:
    """Create and dispatch a one-click AI extract job for one paper.

    Requires an authenticated workbench session. Creates a WorkflowJob of
    type ``ai_extract_rebuild`` and dispatches a Codex-web autonomous agent.
    If a job is already running for this paper, returns that job instead of
    creating a duplicate.
    """
    try:
        return await create_ai_extract_job(session, paper_id, settings, model=model)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"ai_extract_job_failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/papers/{paper_id}/ai-extract/jobs/latest")
async def get_latest_ai_extract_job_endpoint(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the most recent AI extract job for this paper.

    If the job is still running, performs a read-through check of the
    Codex-web thread status and updates the job accordingly.
    """
    job = get_latest_job(session, paper_id)
    if job is None:
        return {
            "job_id": None,
            "type": JOB_TYPE,
            "status": "not_started",
            "paper_id": str(paper_id),
            "result": None,
            "error": None,
        }
    # Read-through: sync status from Codex-web if running
    if job.status in ("queued", "dispatching", "running"):
        try:
            job = await sync_job_status(session, job, settings)
            session.commit()
        except Exception:
            session.rollback()
    return serialize_job(job, session)


@router.get("/templates")
async def rebuild_templates() -> dict[str, Any]:
    return reaction_templates()


@router.get("/batch-catalog")
async def rebuild_batch_catalog(
    scope: str = Query(..., pattern="^(all|selected)$"),
    library: list[str] | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    snapshot: str | None = None,
    session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(require_session),
) -> dict[str, Any]:
    try:
        return batch_catalog(session, scope=scope, libraries=library,
                             limit=limit, offset=offset, snapshot=snapshot)
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "catalog_changed_restart_selection" else 400,
                            detail=str(exc)) from exc


@router.get("/papers")
async def rebuild_papers(
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return list_papers(session, query=q, limit=limit, offset=offset)


@router.get("/papers/{paper_id}")
async def rebuild_paper(paper_id: UUID, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    try:
        return get_paper(session, paper_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/ai-work-package")
async def rebuild_ai_work_package(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Give an external AI one read call for paper context, assets and rows."""
    try:
        return {
            "paper": get_paper(session, paper_id),
            "templates": reaction_templates(),
            "files": list_files(session, paper_id),
            "assets": list_assets(session, paper_id),
            "rows": list_rows(session, paper_id=paper_id, limit=500),
            "write_endpoints": {
                "asset_explanations": "/api/rebuild/papers/{paper_id}/assets/import",
                "asset_crop": "/api/rebuild/papers/{paper_id}/assets/crop",
                "data_rows": "/api/rebuild/rows/import",
                "row_identity_correction": "/api/rebuild/rows/{row_id}/identity",
            },
            "row_identity_contract": {
                "dimensions": ["adsorbate", "intermediate", "reaction_step", "adsorption_site", "identity_context"],
                "rule": "A dimension declared in row top-level, condition, properties or a non-missing value must match exactly. Omit redundant declarations or make every declaration identical from evidence. Contradictions are rejected; no synonym normalization is applied.",
                "pending_recovery": "Use the returned canonical row_key at rows/import to supply all pending dimension values; keep existing identity and conditions unchanged.",
                "confirmed_correction": "PUT row_identity_correction with paper_id, canonical row_key, fresh identity_revision as expected_revision, new request_id, whitelisted changes, reason and same-paper registered file/asset evidence. condition replaces the entire condition object. Requires workbench session and X-CSRF-Token.",
                "retry": "Persist the exact request before sending; retry identical body and request_id after unknown outcomes. request_id is scoped to row_id. GET and compare the current row after a replay; applied_revision may be older than current_revision.",
            },
        }
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.post("/papers/{paper_id}/files")
async def associate_rebuild_pdf(
    paper_id: UUID,
    role: str = Form(pattern="^(main|si)$"),
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="only_pdf_uploads_are_supported")
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file_too_large")
    await file.seek(0)
    safe_name = Path(file.filename).name
    store = ArtifactStore(settings)
    saved_path = await store.save_upload(file, f"rebuild_{uuid4()}_{role}_{safe_name}")
    try:
        record, created = associate_pdf_file(
            session,
            paper_id=paper_id,
            role=role,
            saved_path=saved_path,
            original_filename=safe_name,
            sha256=digest.hexdigest(),
            file_size=size,
            settings=settings,
        )
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    session.commit()
    return {"file": serialize_file(record), "created": created, "automatic_parsing_started": False}


@router.get("/papers/{paper_id}/files")
async def rebuild_files(paper_id: UUID, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    try:
        return {"items": list_files(session, paper_id)}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{file_id}/preview")
async def rebuild_file_preview(
    file_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        record = get_file(session, file_id)
        path = resolve_reading_pdf(record, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{Path(record.original_filename).stem}.pdf",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/files/{file_id}/download")
async def rebuild_file_download(
    file_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        record = get_file(session, file_id)
        path, media_type = resolve_original_download(record, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type=media_type,
        filename=record.original_filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/papers/{paper_id}/source-pdf/preview")
async def rebuild_paper_source_pdf(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        paper = _required_paper(session, paper_id)
        path = resolve_paper_source_pdf(paper, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    filename = f"{paper.paper_code or paper.id}.pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="inline",
    )


@router.get("/papers/{paper_id}/assets")
async def rebuild_assets(paper_id: UUID, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    try:
        return {"items": list_assets(session, paper_id)}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.post("/papers/{paper_id}/assets/import")
async def import_rebuild_assets(
    paper_id: UUID,
    payload: RebuildAssetsImportRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, asset_payload in enumerate(payload.assets):
        try:
            with session.begin_nested():
                asset, created = upsert_visual_asset(
                    session,
                    paper_id=paper_id,
                    payload=asset_payload,
                    settings=settings,
                )
                results.append(
                    {
                        "index": index,
                        "asset_key": asset.asset_key,
                        "asset_id": str(asset.id),
                        "created": created,
                    }
                )
        except RebuildWorkflowError as exc:
            errors.append({"index": index, "error": str(exc)})
    session.commit()
    return {
        "paper_id": str(paper_id),
        "assets_submitted": len(payload.assets),
        "assets_imported": len(results),
        "results": results,
        "errors": errors,
    }

@router.post("/papers/{paper_id}/assets")
async def create_or_update_rebuild_asset(
    paper_id: UUID,
    payload: RebuildVisualAssetRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        asset, created = upsert_visual_asset(session, paper_id=paper_id, payload=payload, settings=settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    session.commit()
    return {"asset": serialize_asset(asset), "created": created}


@router.post("/papers/{paper_id}/assets/crop")
async def create_rebuild_asset_from_pdf_crop(
    paper_id: UUID,
    payload: RebuildVisualAssetCropRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        if payload.file_id is None:
            raise HTTPException(status_code=400, detail="file_id_required_for_crop")
        file_record = get_file(session, payload.file_id)
        if file_record.paper_id != paper_id:
            raise HTTPException(status_code=400, detail="file_belongs_to_other_paper")
        pdf_path = resolve_file_path(file_record, settings)
        # Validate complete metadata/reading before rendering or touching the fixed PNG.
        base_payload = payload.model_dump(exclude={"regions"})
        if "reading_explanation" not in payload.model_fields_set:
            base_payload.pop("reading_explanation", None)
        base_payload["page_numbers"] = sorted({region.page_number for region in payload.regions})
        source_payload = RebuildVisualAssetRequest(**base_payload)
        prepare_visual_asset(session, paper_id=paper_id, payload=source_payload, settings=settings)

        import fitz
        from PIL import Image

        images: list[Image.Image] = []
        with fitz.open(pdf_path) as document:
            for region in payload.regions:
                if region.page_number > document.page_count:
                    raise HTTPException(status_code=400, detail="crop_page_out_of_range")
                page = document[region.page_number - 1]
                x0, y0, x1, y1 = region.bbox
                if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                    raise HTTPException(status_code=400, detail="crop_bbox_must_be_normalized")
                rectangle = fitz.Rect(
                    page.rect.x0 + x0 * page.rect.width,
                    page.rect.y0 + y0 * page.rect.height,
                    page.rect.x0 + x1 * page.rect.width,
                    page.rect.y0 + y1 * page.rect.height,
                )
                pixmap = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), clip=rectangle, alpha=False)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                images.append(image.convert("RGB"))

        if len(images) == 1:
            output_image = images[0]
        else:
            width = max(image.width for image in images)
            total_height = sum(image.height for image in images) + 12 * (len(images) - 1)
            output_image = Image.new("RGB", (width, total_height), "white")
            offset = 0
            for image in images:
                output_image.paste(image, (0, offset))
                offset += image.height + 12

        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", payload.asset_key).strip(".-") or "asset"
        target_dir = settings.storage_paths["figures"] / "rebuild" / str(paper_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        image_path = target_dir / f"{safe_key}.png"
        relative_path = image_path.resolve().relative_to(settings.storage_root.resolve()).as_posix()
        buffer = io.BytesIO()
        output_image.save(buffer, format="PNG", optimize=True)
        png_bytes = buffer.getvalue()
        pixels_changed = not image_path.is_file() or image_path.read_bytes() != png_bytes
        base_payload["image_path"] = relative_path
        source_payload = RebuildVisualAssetRequest(**base_payload)
        prepared = prepare_visual_asset(session, paper_id=paper_id, payload=source_payload,
                                        settings=settings, force_source_changed=pixels_changed)
        # Source/reading rejection above leaves original PNG and DB untouched.
        image_path.write_bytes(png_bytes)
        asset, created = upsert_visual_asset(session, paper_id=paper_id, payload=source_payload,
                                            settings=settings, prepared=prepared)
    except HTTPException:
        raise
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"crop_failed:{type(exc).__name__}") from exc
    session.commit()
    return {"asset": serialize_asset(asset), "created": created, "rendered_pages": len(payload.regions)}


@router.get("/assets/{asset_id}")
async def rebuild_asset_image(
    asset_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        asset = get_asset(session, asset_id)
        path = resolve_asset_image_path(asset, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="asset_image_not_available")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.get("/rows")
async def rebuild_data_rows(
    reaction: str | None = None,
    material: str | None = None,
    material_family: str | None = None,
    active_site_type: str | None = None,
    data_type: str | None = None,
    paper_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return list_rows(
        session,
        reaction=reaction,
        material=material,
        material_family=material_family,
        active_site_type=active_site_type,
        data_type=data_type,
        paper_id=paper_id,
        limit=limit,
        offset=offset,
    )


@router.post("/rows/import")
async def import_rebuild_rows(
    payload: RebuildRowsImportRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        _required_paper(session, payload.paper_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.rows):
        try:
            with session.begin_nested():
                row_payload = RebuildDataRowInput.model_validate(raw)
                result = import_data_row(session, payload.paper_id, row_payload)
            result["row_index"] = index
            results.append(result)
            for part in ("values", "properties"):
                for detail in result[part]:
                    if detail["outcome"] in {"rejected", "conflict", "pending"}:
                        errors.append({"row_index": index, "row_key": result["row_key"],
                                       "part": part, **detail})
            if result.get("identity_status") == "pending":
                reason = {
                    "row_index": index, "row_key": result["row_key"], "part": "row",
                    "outcome": "pending", "error": "identity_pending:correct_dimension_using_returned_row_key",
                    "pending_identity_fields": result["pending_identity_fields"],
                }
                result["pending_reason"] = reason["error"]
                errors.append(reason)
        except (ValidationError, RebuildWorkflowError, DataError, IntegrityError) as exc:
            reason = "database_constraint_rejected" if isinstance(exc, (DataError, IntegrityError)) else str(exc)
            detail = {"row_index": index, "row_key": raw.get("row_key") if isinstance(raw, dict) else None,
                      "outcome": "rejected", "complete": False, "error": reason}
            results.append(detail)
            errors.append(detail)
    session.commit()
    counts = {name: sum(r["outcome"] == name for r in results)
              for name in ("written", "duplicate", "partial", "rejected")}
    return {
        "paper_id": str(payload.paper_id),
        "rows_submitted": len(payload.rows),
        "rows_imported": counts["written"],
        "rows_duplicate": counts["duplicate"],
        "rows_partial": counts["partial"],
        "rows_rejected": counts["rejected"],
        "complete": all(result["complete"] for result in results),
        "results": results,
        "errors": errors,
    }


async def _require_identity_correction_session(request: Request) -> SessionState:
    # Adapt the helper's optional Settings argument explicitly. Exposing it
    # directly to Depends would make FastAPI treat Settings as a second body.
    auth = await require_session(request, settings=get_settings())
    # Python's JSON parser accepts NaN/Infinity. Reject them before FastAPI's
    # validation error handler tries to serialize a non-finite error input.
    try:
        json.dumps(await request.json(), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="identity_body_must_be_finite_json") from exc
    return auth


@router.put("/rows/{row_id}/identity")
async def correct_rebuild_row_identity(
    row_id: UUID,
    payload: RebuildRowIdentityCorrectionRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_identity_correction_session),
) -> dict[str, Any]:
    """Workbench owners may correct one confirmed row with evidence and CAS.

    Explicit Origin/CSRF checks are required even where the application has
    not enabled global session middleware. Share/MCP-only callers are denied.
    """
    if not origin_allowed(request, get_settings()):
        raise HTTPException(status_code=403, detail="cross_origin_request_blocked")
    if not csrf_token_valid(request, auth):
        raise HTTPException(status_code=403, detail="csrf_token_invalid")
    try:
        result = correct_row_identity(session, row_id=row_id, payload=payload, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        error = str(exc)
        if error in {"paper_not_found", "row_not_found", "file_not_found", "asset_not_found"}:
            status = 404
        elif error.startswith(("identity_revision_conflict", "identity_target_collision:",
                               "identity_target_ambiguous_collision:",
                               "identity_request_id_reused", "canonical_row_key_mismatch",
                               "identity_pending:")):
            status = 409
        else:
            status = 400
        raise HTTPException(status_code=status, detail=error) from exc
    except ValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail="invalid_corrected_identity") from exc


@router.post("/analysis")
async def rebuild_analysis(
    payload: RebuildAnalysisRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        result = analyze(session, payload)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    session.commit()
    return result


@router.put("/values/{value_id}")
async def correct_rebuild_value(
    value_id: UUID,
    payload: RebuildValueCorrectionRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        value = update_data_value(session, value_id=value_id, payload=payload)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    sources = session.scalars(
        select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)
    ).all()
    session.commit()
    return serialize_value(value, sources)


@router.get("/analysis/{run_id}/csv")
async def rebuild_analysis_csv(run_id: UUID, session: Session = Depends(get_db_session)) -> StreamingResponse:
    try:
        run = get_analysis_run(session, run_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "paper_id",
            "material",
            "reaction",
            "data_type",
            "condition_json",
            run.result.get("x_field"),
            run.result.get("x_unit") or "",
            run.result.get("y_field"),
            run.result.get("y_unit") or "",
            "x_value_type",
            "y_value_type",
        ]
    )
    for point in run.result.get("points", []):
        writer.writerow(
            [
                point["paper_id"],
                point["material"],
                point["reaction"],
                point["data_type"],
                json.dumps(point.get("condition") or {}, ensure_ascii=False, sort_keys=True),
                point["x"],
                point["y"],
                point["x_value_type"],
                point["y_value_type"],
            ]
        )
    filename = f"literature-ai-analysis-{run_id}.csv"
    content = buffer.getvalue()
    buffer.seek(0)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Analysis-Sample-Count": str(run.sample_count),
            "X-Analysis-Run-Id": str(run.id),
        },
    )
