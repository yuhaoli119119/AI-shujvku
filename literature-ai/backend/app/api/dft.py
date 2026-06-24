from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.dft_export import DFTDatasetContractV3, DFTMLDatasetExportV3
from app.schemas.project_library import (
    ProjectLibraryMLExportPayload,
    ProjectLibraryQualityPayload,
    ProjectLibraryQueuePayload,
)
from app.services.dft_export_service import build_dft_ml_dataset_v3, build_dft_ml_dataset_v3_csv
from app.services.project_library_ml_service import ProjectLibraryMLService
from app.services.project_library_quality_service import ProjectLibraryQualityService
from app.services.project_library_queue_service import ProjectLibraryQueueService


router = APIRouter()


def _v3_filename(task: str, suffix: str) -> str:
    safe_task = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in task).strip("_")
    return f"dft_ml_dataset_v3_{safe_task or 'task'}.{suffix}"


@router.get("/project-library-queue", response_model=ProjectLibraryQueuePayload)
def get_project_library_queue(
    context_key: str = Query(default="li_s_sac_dac", min_length=1),
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return ProjectLibraryQueueService(session).build_queue(
            context_key=context_key,
            library_name=library_name,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc


@router.get("/project-library-quality", response_model=ProjectLibraryQualityPayload)
def get_project_library_quality(
    context_key: str = Query(default="li_s_sac_dac", min_length=1),
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return ProjectLibraryQualityService(session).build_quality_panel(
            context_key=context_key,
            library_name=library_name,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc


@router.get("/project-library-ml-export", response_model=ProjectLibraryMLExportPayload)
def get_project_library_ml_export(
    context_key: str = Query(default="li_s_sac_dac", min_length=1),
    task: str = Query(default="adsorption_energy", min_length=1),
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return ProjectLibraryMLService(session).build_ml_export_summary(
            context_key=context_key,
            task=task,
            library_name=library_name,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc


@router.get("/project-library-ml-export.csv")
def get_project_library_ml_export_csv(
    context_key: str = Query(default="li_s_sac_dac", min_length=1),
    task: str = Query(default="adsorption_energy", min_length=1),
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> Response:
    try:
        csv_text, _manifest = ProjectLibraryMLService(session).build_ml_export_csv(
            context_key=context_key,
            task=task,
            library_name=library_name,
        )
        filename = ProjectLibraryMLService.csv_filename(task)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ml-dataset-v3", response_model=DFTMLDatasetExportV3)
def get_dft_ml_dataset_v3(
    task: str = Query(..., min_length=1),
    ready_only: bool = Query(default=False),
    property_type: str | None = Query(default=None),
    adsorbate: str | None = Query(default=None),
    catalyst_type: str | None = Query(default=None),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    library_name: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None),
    paper_id: UUID | None = Query(default=None),
    limit: int | None = Query(default=None, ge=0, le=10000),
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return build_dft_ml_dataset_v3(
            session,
            task=task,
            ready_only=ready_only,
            property_type=property_type,
            adsorbate=adsorbate,
            catalyst_type=catalyst_type,
            year_min=year_min,
            year_max=year_max,
            library_name=library_name,
            min_confidence=min_confidence,
            paper_id=paper_id,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc


@router.get("/ml-dataset-v3.csv")
def get_dft_ml_dataset_v3_csv(
    task: str = Query(..., min_length=1),
    ready_only: bool = Query(default=True),
    property_type: str | None = Query(default=None),
    adsorbate: str | None = Query(default=None),
    catalyst_type: str | None = Query(default=None),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    library_name: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None),
    paper_id: UUID | None = Query(default=None),
    limit: int | None = Query(default=None, ge=0, le=10000),
    session: Session = Depends(get_db_session),
) -> Response:
    try:
        csv_text, _manifest = build_dft_ml_dataset_v3_csv(
            session,
            task=task,
            ready_only=ready_only,
            property_type=property_type,
            adsorbate=adsorbate,
            catalyst_type=catalyst_type,
            year_min=year_min,
            year_max=year_max,
            library_name=library_name,
            min_confidence=min_confidence,
            paper_id=paper_id,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_v3_filename(task, "csv")}"'},
    )


@router.get("/ml-dataset-v3/manifest", response_model=DFTDatasetContractV3)
def get_dft_ml_dataset_v3_manifest(
    task: str = Query(..., min_length=1),
    ready_only: bool = Query(default=True),
    property_type: str | None = Query(default=None),
    adsorbate: str | None = Query(default=None),
    catalyst_type: str | None = Query(default=None),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    library_name: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None),
    paper_id: UUID | None = Query(default=None),
    limit: int | None = Query(default=None, ge=0, le=10000),
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        payload = build_dft_ml_dataset_v3(
            session,
            task=task,
            ready_only=ready_only,
            property_type=property_type,
            adsorbate=adsorbate,
            catalyst_type=catalyst_type,
            year_min=year_min,
            year_max=year_max,
            library_name=library_name,
            min_confidence=min_confidence,
            paper_id=paper_id,
            limit=limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc.args[0])) from exc
    return payload["manifest"]
