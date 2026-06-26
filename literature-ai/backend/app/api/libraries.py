from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Paper
from app.db.session import session_scope
from app.schemas.api import LibraryCreateRequest, LibraryImportRequest, LibraryInfoResponse
from app.services.library_manager import LibraryManager
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.active_database import activate_active_library_database, get_active_database_info
from app.utils.project_paths import resolve_data_mount_path

router = APIRouter()

_manager: LibraryManager | None = None


def _get_manager() -> LibraryManager:
    global _manager
    if _manager is None:
        _manager = LibraryManager()
    return _manager


def _database_library_counts(library_names: list[str]) -> dict[str, int]:
    settings = get_settings()
    normalized_names = [normalize_library_name(name) for name in library_names]
    if not normalized_names:
        return {}
    with session_scope(settings.database_url) as session:
        counts: dict[str, int] = {}
        for library_name in normalized_names:
            stmt = select(func.count(Paper.id)).where(build_library_name_clause(Paper.library_name, library_name))
            counts[library_name] = int(session.scalar(stmt) or 0)
        return counts


def _effective_active_library_response(
    lib,
    *,
    active_db_info: dict | None = None,
    configured_counts: dict[str, int] | None = None,
) -> LibraryInfoResponse:
    payload = lib.model_dump()
    info = active_db_info or get_active_database_info()
    active_name = normalize_library_name(info.get("active_library"))
    library_name = normalize_library_name(str(payload.get("name") or ""))
    configured_count = configured_counts.get(library_name) if configured_counts is not None else 0

    payload["paper_count"] = configured_count
    payload["is_active"] = bool(active_name and library_name == active_name)
    return LibraryInfoResponse(**payload)


def _browse_roots() -> list[Path]:
    roots: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in roots:
            roots.append(resolved)

    add(LibraryManager.default_library_root().parent)

    configured = [item.strip() for item in get_settings().browse_roots.split(",") if item.strip()]
    for item in configured:
        add(resolve_data_mount_path(item))

    add(Path.home())
    return roots


def _resolve_and_validate(path: str) -> Path:
    resolved = resolve_data_mount_path(path)
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
def list_libraries() -> list[LibraryInfoResponse]:
    mgr = _get_manager()
    libs = mgr.list_libraries()
    active_db_info = get_active_database_info()
    configured_counts = _database_library_counts([str(lib.name) for lib in libs])
    return [
        _effective_active_library_response(
            lib,
            active_db_info=active_db_info,
            configured_counts=configured_counts,
        )
        for lib in libs
    ]


@router.post("", response_model=LibraryInfoResponse, status_code=201)
async def create_library(payload: LibraryCreateRequest) -> LibraryInfoResponse:
    mgr = _get_manager()
    try:
        lib = mgr.create_library(
            name=payload.name,
            root_path=payload.root_path,
            description=payload.description or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {exc.filename or payload.root_path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Unable to create library at the selected location: {exc}") from exc
    return LibraryInfoResponse(**lib.model_dump())


@router.post("/{name}/activate", response_model=LibraryInfoResponse)
async def activate_library(name: str) -> LibraryInfoResponse:
    mgr = _get_manager()
    try:
        lib = mgr.activate_library(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=500, detail=f"激活文献库失败：{exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"激活文献库失败：{exc}") from exc
    try:
        active_db_info = activate_active_library_database()
        configured_counts = _database_library_counts([str(lib.name)])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"激活文献库失败：{exc}") from exc
    return _effective_active_library_response(
        lib,
        active_db_info=active_db_info,
        configured_counts=configured_counts,
    )


@router.post("/import", response_model=LibraryInfoResponse, status_code=201)
async def import_library(payload: LibraryImportRequest) -> LibraryInfoResponse:
    mgr = _get_manager()
    try:
        lib = mgr.import_library(payload.root_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {exc.filename or payload.root_path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Unable to import library from the selected location: {exc}") from exc
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
