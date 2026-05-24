from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


DEFAULT_LIBRARY_NAME = "默认文献库"
LEGACY_STORAGE_MODE = "storage"
SHARED_STORAGE_MODE = "papers"
LEGACY_LIBRARY_KIND = "web_library"
SHARED_LIBRARY_KIND = "shared_project"
REGISTRY_VERSION = 2

LEGACY_STORAGE_SUBDIRS = ("pdf", "tei", "docling_json", "figures", "tables", "markdown")
SHARED_STORAGE_SUBDIRS = ("pdf", "text", "tei", "docling_json", "figures", "tables", "markdown")


class LibraryInfo(BaseModel):
    name: str
    root_path: str
    description: str = ""
    paper_count: int = 0
    is_active: bool = False
    created_at: str | None = None


class LibraryManager:
    REGISTRY_PATH = Path("data/library_registry.json")
    DEFAULT_LIBRARY_ROOT = Path("data/libraries/default")

    def __init__(self) -> None:
        self._ensure_registry()

    def list_libraries(self) -> list[LibraryInfo]:
        registry = self._read_registry()
        active_name = registry.get("active_library", DEFAULT_LIBRARY_NAME)
        result: list[LibraryInfo] = []
        for entry in registry.get("libraries", []):
            root = Path(entry["root_path"])
            result.append(
                LibraryInfo(
                    name=entry["name"],
                    root_path=entry["root_path"],
                    description=entry.get("description", ""),
                    paper_count=self._count_papers(root),
                    is_active=entry["name"] == active_name,
                    created_at=entry.get("created_at"),
                )
            )
        return result

    def create_library(self, name: str, root_path: str = "", description: str = "") -> LibraryInfo:
        library_name = name.strip()
        if not library_name:
            raise ValueError("库名称不能为空")

        root = self._resolve_create_root(library_name, root_path)
        self._validate_path_safety(root)

        registry = self._read_registry()
        if self._find_entry(registry, library_name) is not None:
            raise ValueError(f"库“{library_name}”已存在")

        now_iso = datetime.utcnow().isoformat()
        storage_mode = SHARED_STORAGE_MODE
        library_kind = SHARED_LIBRARY_KIND

        self.init_library_structure(root, storage_mode=storage_mode)
        self.init_library_db(root)
        self._write_library_meta(
            root=root,
            payload=self._build_library_meta(
                name=library_name,
                description=description,
                created_at=now_iso,
                storage_mode=storage_mode,
                library_kind=library_kind,
            ),
        )
        self._ensure_shared_project_config(root, library_name)

        registry.setdefault("libraries", []).append(
            {
                "name": library_name,
                "root_path": str(root),
                "description": description,
                "created_at": now_iso,
            }
        )
        self._write_registry(registry)
        return LibraryInfo(
            name=library_name,
            root_path=str(root),
            description=description,
            paper_count=0,
            is_active=False,
            created_at=now_iso,
        )

    def activate_library(self, name: str) -> LibraryInfo:
        registry = self._read_registry()
        entry = self._find_entry(registry, name)
        if entry is None:
            raise ValueError(f"库“{name}”不存在")

        root = Path(entry["root_path"]).resolve()
        if not root.exists():
            raise ValueError(f"库路径不存在: {root}")

        meta = self._load_library_meta(root)
        storage_mode = self._resolve_storage_mode(root, meta)
        self.init_library_structure(root, storage_mode=storage_mode)
        db_path = root / "database.sqlite"
        if not db_path.exists():
            self.init_library_db(root)

        storage_root = self._storage_root_for_mode(root, storage_mode)
        database_url = f"sqlite:///{db_path.as_posix()}"

        from app.db.session import switch_database

        switch_database(database_url, storage_root=str(storage_root))

        registry["active_library"] = name
        self._write_registry(registry)

        updated_meta = self._build_library_meta(
            name=name,
            description=entry.get("description", ""),
            created_at=entry.get("created_at"),
            storage_mode=storage_mode,
            library_kind=self._resolve_library_kind(root, meta, storage_mode),
            meta=meta,
        )
        updated_meta["last_accessed"] = datetime.utcnow().isoformat()
        self._write_library_meta(root, updated_meta)
        if storage_mode == SHARED_STORAGE_MODE:
            self._ensure_shared_project_config(root, name)

        return LibraryInfo(
            name=name,
            root_path=str(root),
            description=entry.get("description", ""),
            paper_count=self._count_papers(root),
            is_active=True,
            created_at=entry.get("created_at"),
        )

    def unregister_library(self, name: str) -> LibraryInfo:
        if name == DEFAULT_LIBRARY_NAME:
            raise ValueError("默认文献库不能移除")

        registry = self._read_registry()
        entry = self._find_entry(registry, name)
        if entry is None:
            raise ValueError(f"库“{name}”不存在")

        info = LibraryInfo(
            name=name,
            root_path=entry["root_path"],
            description=entry.get("description", ""),
            paper_count=self._count_papers(Path(entry["root_path"])),
            is_active=False,
            created_at=entry.get("created_at"),
        )
        registry["libraries"] = [item for item in registry.get("libraries", []) if item["name"] != name]

        if registry.get("active_library") == name:
            registry["active_library"] = DEFAULT_LIBRARY_NAME
            default_entry = self._find_entry(registry, DEFAULT_LIBRARY_NAME)
            if default_entry:
                default_root = Path(default_entry["root_path"]).resolve()
                default_meta = self._load_library_meta(default_root)
                default_mode = self._resolve_storage_mode(default_root, default_meta)
                self.init_library_structure(default_root, storage_mode=default_mode)
                db_path = default_root / "database.sqlite"
                if not db_path.exists():
                    self.init_library_db(default_root)
                from app.db.session import switch_database

                switch_database(
                    f"sqlite:///{db_path.as_posix()}",
                    storage_root=str(self._storage_root_for_mode(default_root, default_mode)),
                )

        self._write_registry(registry)
        return info

    def import_library(self, root_path: str) -> LibraryInfo:
        root = Path(root_path).resolve()
        self._validate_path_safety(root)
        if not root.exists():
            raise ValueError(f"路径不存在: {root}")

        meta = self._load_library_meta(root)
        project_config = self._load_project_config(root)
        storage_mode = self._resolve_storage_mode(root, meta)
        library_kind = self._resolve_library_kind(root, meta, storage_mode)
        description = str(meta.get("description") or "")
        created_at = str(meta.get("created_at") or "") or None
        name = (
            str(meta.get("name") or "").strip()
            or str(project_config.get("project_name") or "").strip()
            or root.name
        )
        if not name:
            raise ValueError("无法从目标文件夹识别库名称")

        registry = self._read_registry()
        if self._find_entry(registry, name) is not None:
            raise ValueError(f"库“{name}”已存在（路径: {root}）。若要重新导入请先移除现有注册。")

        self.init_library_structure(root, storage_mode=storage_mode)
        if not (root / "database.sqlite").exists():
            self.init_library_db(root)

        now_iso = datetime.utcnow().isoformat()
        self._write_library_meta(
            root=root,
            payload=self._build_library_meta(
                name=name,
                description=description,
                created_at=created_at or now_iso,
                storage_mode=storage_mode,
                library_kind=library_kind,
                meta=meta,
            ),
        )
        if storage_mode == SHARED_STORAGE_MODE:
            self._ensure_shared_project_config(root, name, project_config=project_config)

        registry.setdefault("libraries", []).append(
            {
                "name": name,
                "root_path": str(root),
                "description": description,
                "created_at": created_at or now_iso,
            }
        )
        self._write_registry(registry)
        return LibraryInfo(
            name=name,
            root_path=str(root),
            description=description,
            paper_count=self._count_papers(root),
            is_active=False,
            created_at=created_at or now_iso,
        )

    def get_active_library(self) -> LibraryInfo | None:
        registry = self._read_registry()
        active_name = registry.get("active_library", DEFAULT_LIBRARY_NAME)
        entry = self._find_entry(registry, active_name)
        if entry is None:
            return None
        root = Path(entry["root_path"])
        return LibraryInfo(
            name=active_name,
            root_path=entry["root_path"],
            description=entry.get("description", ""),
            paper_count=self._count_papers(root),
            is_active=True,
            created_at=entry.get("created_at"),
        )

    @staticmethod
    def init_library_structure(root: Path, storage_mode: str = LEGACY_STORAGE_MODE) -> None:
        root.mkdir(parents=True, exist_ok=True)
        storage_root = LibraryManager._storage_root_for_mode(root, storage_mode)
        storage_root.mkdir(parents=True, exist_ok=True)
        subdirs = SHARED_STORAGE_SUBDIRS if storage_mode == SHARED_STORAGE_MODE else LEGACY_STORAGE_SUBDIRS
        for subdir in subdirs:
            (storage_root / subdir).mkdir(parents=True, exist_ok=True)
        if storage_mode == SHARED_STORAGE_MODE:
            for extra_dir in ("exports", "logs", "config"):
                (root / extra_dir).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def init_library_db(root: Path) -> None:
        db_path = root / "database.sqlite"
        database_url = f"sqlite:///{db_path.as_posix()}"
        from app.db.session import init_db

        init_db(database_url)

    def _ensure_registry(self) -> None:
        self.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not self.REGISTRY_PATH.exists():
            now_iso = datetime.utcnow().isoformat()
            registry: dict[str, Any] = {
                "version": REGISTRY_VERSION,
                "active_library": DEFAULT_LIBRARY_NAME,
                "libraries": [
                    {
                        "name": DEFAULT_LIBRARY_NAME,
                        "root_path": str(self.DEFAULT_LIBRARY_ROOT.resolve()),
                        "description": DEFAULT_LIBRARY_NAME,
                        "created_at": now_iso,
                    }
                ],
            }
            self._write_registry(registry)

        registry = self._read_registry()
        default_entry = self._find_entry(registry, DEFAULT_LIBRARY_NAME)
        if default_entry is None:
            now_iso = datetime.utcnow().isoformat()
            default_entry = {
                "name": DEFAULT_LIBRARY_NAME,
                "root_path": str(self.DEFAULT_LIBRARY_ROOT.resolve()),
                "description": DEFAULT_LIBRARY_NAME,
                "created_at": now_iso,
            }
            registry.setdefault("libraries", []).append(default_entry)
            if not registry.get("active_library"):
                registry["active_library"] = DEFAULT_LIBRARY_NAME
            self._write_registry(registry)

        default_root = Path(default_entry["root_path"]).resolve()
        self.init_library_structure(default_root, storage_mode=LEGACY_STORAGE_MODE)
        if not (default_root / "database.sqlite").exists():
            self.init_library_db(default_root)
        default_meta = self._build_library_meta(
            name=DEFAULT_LIBRARY_NAME,
            description=default_entry.get("description", DEFAULT_LIBRARY_NAME),
            created_at=default_entry.get("created_at"),
            storage_mode=LEGACY_STORAGE_MODE,
            library_kind=LEGACY_LIBRARY_KIND,
            meta=self._load_library_meta(default_root),
        )
        self._write_library_meta(default_root, default_meta)

    def _resolve_create_root(self, name: str, root_path: str) -> Path:
        if root_path and root_path.strip():
            return Path(root_path).resolve()
        safe_name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        return (Path("data/libraries") / safe_name).resolve()

    def _read_registry(self) -> dict[str, Any]:
        if not self.REGISTRY_PATH.exists():
            return {"version": REGISTRY_VERSION, "active_library": DEFAULT_LIBRARY_NAME, "libraries": []}
        try:
            data = json.loads(self.REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"version": REGISTRY_VERSION, "active_library": DEFAULT_LIBRARY_NAME, "libraries": []}
        data.setdefault("version", REGISTRY_VERSION)
        data.setdefault("active_library", DEFAULT_LIBRARY_NAME)
        data.setdefault("libraries", [])
        return data

    def _write_registry(self, registry: dict[str, Any]) -> None:
        registry["version"] = REGISTRY_VERSION
        self.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.REGISTRY_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.REGISTRY_PATH)

    @staticmethod
    def _find_entry(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
        for entry in registry.get("libraries", []):
            if entry.get("name") == name:
                return entry
        return None

    @staticmethod
    def _storage_root_for_mode(root: Path, storage_mode: str) -> Path:
        return root / (SHARED_STORAGE_MODE if storage_mode == SHARED_STORAGE_MODE else LEGACY_STORAGE_MODE)

    @staticmethod
    def _meta_path(root: Path) -> Path:
        return root / "library.json"

    def _load_library_meta(self, root: Path) -> dict[str, Any]:
        meta_path = self._meta_path(root)
        if not meta_path.exists():
            return {}
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_library_meta(self, root: Path, payload: dict[str, Any]) -> None:
        self._meta_path(root).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _load_project_config(root: Path) -> dict[str, Any]:
        config_path = root / "config" / "project_config.json"
        if not config_path.exists():
            return {}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _resolve_storage_mode(root: Path, meta: dict[str, Any] | None = None) -> str:
        payload = meta or {}
        if payload.get("storage_mode") in {LEGACY_STORAGE_MODE, SHARED_STORAGE_MODE}:
            return str(payload["storage_mode"])
        if (root / "config" / "project_config.json").exists():
            return SHARED_STORAGE_MODE
        if (root / LEGACY_STORAGE_MODE).exists():
            return LEGACY_STORAGE_MODE
        if (root / SHARED_STORAGE_MODE).exists():
            return SHARED_STORAGE_MODE
        return SHARED_STORAGE_MODE

    @staticmethod
    def _resolve_library_kind(root: Path, meta: dict[str, Any] | None, storage_mode: str) -> str:
        payload = meta or {}
        if payload.get("library_kind") in {LEGACY_LIBRARY_KIND, SHARED_LIBRARY_KIND}:
            return str(payload["library_kind"])
        if storage_mode == SHARED_STORAGE_MODE or (root / "config" / "project_config.json").exists():
            return SHARED_LIBRARY_KIND
        return LEGACY_LIBRARY_KIND

    @staticmethod
    def _build_library_meta(
        name: str,
        description: str,
        created_at: str | None,
        storage_mode: str,
        library_kind: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = dict(meta or {})
        now_iso = datetime.utcnow().isoformat()
        created_value = created_at or existing.get("created_at") or now_iso
        return {
            "name": name,
            "description": description,
            "version": int(existing.get("version") or 2),
            "created_at": created_value,
            "last_accessed": existing.get("last_accessed") or now_iso,
            "paper_count": int(existing.get("paper_count") or 0),
            "tags": existing.get("tags") or [],
            "storage_mode": storage_mode,
            "library_kind": library_kind,
        }

    @staticmethod
    def _ensure_shared_project_config(
        root: Path,
        project_name: str,
        project_config: dict[str, Any] | None = None,
    ) -> None:
        config_dir = root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "project_config.json"
        payload = dict(project_config or {})
        now_iso = datetime.utcnow().isoformat()
        payload["project_name"] = project_name
        payload.setdefault("created_at", now_iso)
        payload["last_opened"] = now_iso
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _count_papers(root: Path) -> int:
        db_path = root / "database.sqlite"
        if not db_path.exists():
            return 0
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
            with engine.connect() as connection:
                count = connection.execute(text("SELECT COUNT(*) FROM papers")).scalar()
            engine.dispose()
            return int(count or 0)
        except Exception:
            return 0

    @staticmethod
    def _validate_path_safety(path: Path) -> None:
        resolved = str(path.resolve())
        dangerous = ["/", "C:\\", "C:/", os.path.expanduser("~")]
        for danger in dangerous:
            if resolved.rstrip("/\\") == danger.rstrip("/\\"):
                raise ValueError(f"路径 {path} 是系统目录，拒绝操作")
