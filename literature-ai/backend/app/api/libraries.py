from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.schemas.api import LibraryCreateRequest, LibraryImportRequest, LibraryInfoResponse
from app.services.library_manager import LibraryManager

router = APIRouter()

_manager: LibraryManager | None = None


def _get_manager() -> LibraryManager:
    global _manager
    if _manager is None:
        _manager = LibraryManager()
    return _manager


def _browse_roots() -> list[Path]:
    configured = [item.strip() for item in get_settings().browse_roots.split(",") if item.strip()]
    roots = [Path(item).resolve() for item in configured]
    home = Path.home().resolve()
    if home not in roots:
        roots.append(home)
    return roots


def _resolve_and_validate(path: str) -> Path:
    resolved = Path(path).resolve()
    for root in _browse_roots():
        if root.exists():
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
    raise ValueError(f"Path {path} is outside the allowed browse roots")


@router.get("/browse", response_model=dict)
async def browse_directory(
    path: str | None = Query(default=None, description="Directory path to browse"),
) -> dict:
    if path is None:
        roots = _browse_roots()
        path = str(roots[0]) if roots else str(Path.home())

    try:
        resolved = _resolve_and_validate(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {path}")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    subdirs = []
    try:
        for child in sorted(resolved.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                try:
                    has_children = any(
                        nested.is_dir() and not nested.name.startswith(".")
                        for nested in child.iterdir()
                    )
                except PermissionError:
                    has_children = False
                subdirs.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "has_children": has_children,
                    }
                )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}") from exc

    parent_path = None
    parent = resolved.parent
    if parent != resolved:
        try:
            _resolve_and_validate(str(parent))
            parent_path = str(parent)
        except ValueError:
            parent_path = None

    return {
        "current_path": str(resolved),
        "parent_path": parent_path,
        "subdirs": subdirs,
    }


@router.get("/browse-roots", response_model=list[dict])
async def list_browse_roots() -> list[dict]:
    roots = []
    for root in _browse_roots():
        if root.exists():
            roots.append({"name": root.name or str(root), "path": str(root)})
    if not roots:
        home = Path.home().resolve()
        roots.append({"name": home.name or str(home), "path": str(home)})
    return roots


@router.get("", response_model=list[LibraryInfoResponse])
async def list_libraries() -> list[LibraryInfoResponse]:
    mgr = _get_manager()
    libs = mgr.list_libraries()
    return [LibraryInfoResponse(**lib.model_dump()) for lib in libs]


@router.post("", response_model=LibraryInfoResponse, status_code=201)
async def create_library(payload: LibraryCreateRequest) -> LibraryInfoResponse:
    mgr = _get_manager()
    try:
        lib = mgr.create_library(
            name=payload.name,
            root_path=payload.root_path,
            description=payload.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LibraryInfoResponse(**lib.model_dump())


@router.post("/{name}/activate", response_model=LibraryInfoResponse)
async def activate_library(name: str) -> LibraryInfoResponse:
    mgr = _get_manager()
    try:
        lib = mgr.activate_library(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return LibraryInfoResponse(**lib.model_dump())


@router.post("/import", response_model=LibraryInfoResponse, status_code=201)
async def import_library(payload: LibraryImportRequest) -> LibraryInfoResponse:
    mgr = _get_manager()
    try:
        lib = mgr.import_library(payload.root_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LibraryInfoResponse(**lib.model_dump())


@router.delete("/{name}", response_model=dict)
async def unregister_library(name: str) -> dict:
    mgr = _get_manager()
    try:
        lib = mgr.unregister_library(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "unregistered",
        "name": lib.name,
        "root_path": lib.root_path,
        "note": f"Files were not deleted: {lib.root_path}",
    }
