"""Libraries API — 文献库的创建、列表、激活、导入、移除、目录浏览。"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.schemas.api import LibraryCreateRequest, LibraryImportRequest, LibraryInfoResponse
from app.services.library_manager import LibraryManager

router = APIRouter()

_manager: LibraryManager | None = None


def _get_manager() -> LibraryManager:
    global _manager
    if _manager is None:
        _manager = LibraryManager()
    return _manager


# 允许浏览的根目录（白名单，防止越权访问）
_BROWSE_ROOTS = [
    "/host/users",     # Docker 映射的宿主机用户目录
    "/data",           # 容器内数据目录
    "/legacy",         # 遗留数据目录
]


def _resolve_and_validate(path: str) -> Path:
    """解析路径并校验是否在允许的浏览范围内。"""
    p = Path(path).resolve()
    for root in _BROWSE_ROOTS:
        root_resolved = Path(root).resolve()
        if root_resolved.exists():
            try:
                p.relative_to(root_resolved)
                return p
            except ValueError:
                continue
    raise ValueError(f"路径 {path} 不在允许的浏览范围内")


@router.get("/browse", response_model=dict)
async def browse_directory(
    path: str = Query(default="/host/users", description="要浏览的目录路径"),
) -> dict:
    """浏览后端可见的目录树，返回子目录列表。用于前端文件夹选择器。"""
    try:
        resolved = _resolve_and_validate(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {path}")

    # 收集子目录
    subdirs = []
    try:
        for child in sorted(resolved.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                try:
                    has_children = any(
                        c.is_dir() and not c.name.startswith(".")
                        for c in child.iterdir()
                    )
                except PermissionError:
                    has_children = False
                subdirs.append({
                    "name": child.name,
                    "path": str(child),
                    "has_children": has_children,
                })
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问: {path}")

    # 获取父目录（如果在白名单范围内）
    parent_path = None
    parent = resolved.parent
    if parent != resolved:
        try:
            _resolve_and_validate(str(parent))
            parent_path = str(parent)
        except ValueError:
            pass

    return {
        "current_path": str(resolved),
        "parent_path": parent_path,
        "subdirs": subdirs,
    }


@router.get("/browse-roots", response_model=list[dict])
async def list_browse_roots() -> list[dict]:
    """列出允许浏览的根目录。"""
    roots = []
    for root in _BROWSE_ROOTS:
        p = Path(root).resolve()
        if p.exists():
            roots.append({"name": root, "path": str(p)})
    # 如果没有可用的根目录，至少返回 /data
    if not roots:
        roots.append({"name": "/data", "path": "/data"})
    return roots


@router.get("", response_model=list[LibraryInfoResponse])
async def list_libraries() -> list[LibraryInfoResponse]:
    """列出所有已注册的库。"""
    mgr = _get_manager()
    libs = mgr.list_libraries()
    return [
        LibraryInfoResponse(
            name=lib.name,
            root_path=lib.root_path,
            description=lib.description,
            paper_count=lib.paper_count,
            is_active=lib.is_active,
            created_at=lib.created_at,
        )
        for lib in libs
    ]


@router.post("", response_model=LibraryInfoResponse, status_code=201)
async def create_library(payload: LibraryCreateRequest) -> LibraryInfoResponse:
    """创建新库（初始化目录 + 空 DB）。"""
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
    """切换到指定库（切换 DB + storage_root）。"""
    mgr = _get_manager()
    try:
        lib = mgr.activate_library(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return LibraryInfoResponse(**lib.model_dump())


@router.post("/import", response_model=LibraryInfoResponse, status_code=201)
async def import_library(payload: LibraryImportRequest) -> LibraryInfoResponse:
    """导入已有的库文件夹（可补全缺失结构）。"""
    mgr = _get_manager()
    try:
        lib = mgr.import_library(payload.root_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LibraryInfoResponse(**lib.model_dump())


@router.delete("/{name}", response_model=dict)
async def unregister_library(name: str) -> dict:
    """从注册表移除库（不删除文件）。"""
    mgr = _get_manager()
    try:
        lib = mgr.unregister_library(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "unregistered",
        "name": lib.name,
        "root_path": lib.root_path,
        "note": f"文件未删除，请手动处理: {lib.root_path}",
    }
