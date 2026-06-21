from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.utils.library_names import (
    DEFAULT_LIBRARY_ALIASES,
    DEFAULT_LIBRARY_NAME,
    normalize_library_name as normalize_shared_library_name,
)
from app.utils.project_paths import DEFAULT_LIBRARY_ROOT as CANONICAL_DEFAULT_LIBRARY_ROOT
from app.utils.project_paths import PROJECT_ROOT as CANONICAL_PROJECT_ROOT
from app.utils.project_paths import canonical_registry_path


LEGACY_STORAGE_MODE = "storage"
SHARED_STORAGE_MODE = "papers"
LEGACY_LIBRARY_KIND = "web_library"
SHARED_LIBRARY_KIND = "shared_project"
REGISTRY_VERSION = 2

LEGACY_STORAGE_SUBDIRS = ("pdf", "tei", "docling_json", "figures", "tables", "markdown")
SHARED_STORAGE_SUBDIRS = ("pdf", "text", "tei", "docling_json", "figures", "tables", "markdown")

logger = logging.getLogger(__name__)


class LibraryInfo(BaseModel):
    name: str
    root_path: str
    description: str = ""
    paper_count: int = 0
    is_active: bool = False
    created_at: str | None = None


class LibraryManager:
    PROJECT_ROOT = CANONICAL_PROJECT_ROOT
    REGISTRY_PATH = canonical_registry_path()
    DEFAULT_LIBRARY_ROOT = CANONICAL_DEFAULT_LIBRARY_ROOT

    def __init__(self) -> None:
        self._ensure_registry()

    @classmethod
    def project_root(cls) -> Path:
        return Path(cls.PROJECT_ROOT).resolve()

    @classmethod
    def registry_path(cls) -> Path:
        return Path(cls.REGISTRY_PATH).resolve()

    @classmethod
    def default_library_root(cls) -> Path:
        return Path(cls.DEFAULT_LIBRARY_ROOT).resolve()

    @classmethod
    def normalize_library_name(cls, value: Any) -> str:
        return normalize_shared_library_name(None if value is None else str(value))

    @classmethod
    def library_name_variants(cls, value: Any) -> tuple[str, ...]:
        normalized = cls.normalize_library_name(value)
        if normalized == DEFAULT_LIBRARY_NAME:
            return tuple(sorted(DEFAULT_LIBRARY_ALIASES))
        return (normalized,)

    @staticmethod
    def _force_configured_database() -> bool:
        from app.config import get_settings

        return bool(getattr(get_settings(), "force_configured_database", False))

    def list_libraries(self) -> list[LibraryInfo]:
        registry = self._read_registry()
        active_name = self.normalize_library_name(registry.get("active_library"))
        result: list[LibraryInfo] = []
        for entry in registry.get("libraries", []):
            root = self._resolve_runtime_path(entry["root_path"])
            name = self.normalize_library_name(entry.get("name"))
            result.append(
                LibraryInfo(
                    name=name,
                    root_path=entry["root_path"],
                    description=str(entry.get("description") or ""),
                    paper_count=self._count_papers(root),
                    is_active=name == active_name,
                    created_at=entry.get("created_at"),
                )
            )
        return result

    def create_library(self, name: str, root_path: str = "", description: str = "") -> LibraryInfo:
        library_name = self.normalize_library_name(name)
        if not library_name:
            raise ValueError("Library name cannot be empty")

        root = self._resolve_create_root(library_name, root_path)
        self._validate_path_safety(root)

        registry = self._read_registry()
        if self._find_entry(registry, library_name) is not None:
            raise ValueError(f"Library '{library_name}' already exists")

        now_iso = datetime.utcnow().isoformat()
        self.init_library_structure(root, storage_mode=SHARED_STORAGE_MODE)
        if not self._force_configured_database():
            self.init_library_db(root)
        self._write_library_meta(
            root=root,
            payload=self._build_library_meta(
                name=library_name,
                description=description,
                created_at=now_iso,
                storage_mode=SHARED_STORAGE_MODE,
                library_kind=SHARED_LIBRARY_KIND,
            ),
        )
        self._ensure_shared_project_config(root, library_name)

        registry.setdefault("libraries", []).append(
            {
                "name": library_name,
                "root_path": self._persisted_root_path(root),
                "description": description,
                "created_at": now_iso,
            }
        )
        self._write_registry(registry)
        return LibraryInfo(
            name=library_name,
            root_path=str(root.resolve()),
            description=description,
            paper_count=0,
            is_active=False,
            created_at=now_iso,
        )

    def activate_library(self, name: str) -> LibraryInfo:
        registry = self._read_registry()
        normalized_name = self.normalize_library_name(name)
        entry = self._find_entry(registry, normalized_name)
        if entry is None:
            raise ValueError(f"Library '{normalized_name}' does not exist")

        root = Path(entry["root_path"]).resolve()
        root = self._resolve_runtime_path(entry["root_path"])
        if not root.exists():
            raise ValueError(f"Library path does not exist: {root}")

        meta = self._load_library_meta(root)
        storage_mode = self._resolve_storage_mode(root, meta)
        self.init_library_structure(root, storage_mode=storage_mode)
        if not self._force_configured_database():
            db_path = root / "database.sqlite"
            if not db_path.exists():
                self.init_library_db(root)
            from app.db.session import switch_database

            switch_database(
                f"sqlite:///{db_path.as_posix()}",
                storage_root=str(self._storage_root_for_mode(root, storage_mode)),
            )

        registry["active_library"] = normalized_name
        self._write_registry(registry)

        updated_meta = self._build_library_meta(
            name=normalized_name,
            description=str(entry.get("description") or ""),
            created_at=entry.get("created_at"),
            storage_mode=storage_mode,
            library_kind=self._resolve_library_kind(root, meta, storage_mode),
            meta=meta,
        )
        updated_meta["last_accessed"] = datetime.utcnow().isoformat()
        self._best_effort_write_library_meta(root, updated_meta)
        if storage_mode == SHARED_STORAGE_MODE:
            self._best_effort_ensure_shared_project_config(root, normalized_name)

        return LibraryInfo(
            name=normalized_name,
            root_path=str(root),
            description=str(entry.get("description") or ""),
            paper_count=self._count_papers(root),
            is_active=True,
            created_at=entry.get("created_at"),
        )

    def unregister_library(self, name: str) -> LibraryInfo:
        normalized_name = self.normalize_library_name(name)
        if normalized_name == DEFAULT_LIBRARY_NAME:
            raise ValueError("Default library cannot be removed")

        registry = self._read_registry()
        entry = self._find_entry(registry, normalized_name)
        if entry is None:
            raise ValueError(f"Library '{normalized_name}' does not exist")

        info = LibraryInfo(
            name=normalized_name,
            root_path=entry["root_path"],
            description=str(entry.get("description") or ""),
            paper_count=self._count_papers(Path(entry["root_path"])),
            is_active=False,
            created_at=entry.get("created_at"),
        )
        registry["libraries"] = [
            item
            for item in registry.get("libraries", [])
            if self.normalize_library_name(item.get("name")) != normalized_name
        ]

        if self.normalize_library_name(registry.get("active_library")) == normalized_name:
            registry["active_library"] = DEFAULT_LIBRARY_NAME
            default_entry = self._find_entry(registry, DEFAULT_LIBRARY_NAME)
            if default_entry:
                default_root = Path(default_entry["root_path"]).resolve()
                default_meta = self._load_library_meta(default_root)
                default_mode = self._resolve_storage_mode(default_root, default_meta)
                self.init_library_structure(default_root, storage_mode=default_mode)
                if not self._force_configured_database():
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
        root = self._resolve_runtime_path(root_path)
        self._validate_path_safety(root)
        if not root.exists():
            raise ValueError(f"Path does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")
        if not self._looks_like_existing_library_root(root):
            raise ValueError(
                "Selected path is not an existing library root. "
                "Please choose a folder that already contains database.sqlite, library.json, config/project_config.json, storage/, or papers/."
            )

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
        name = self.normalize_library_name(name)
        if not name:
            raise ValueError("Unable to infer library name from the target folder")

        registry = self._read_registry()
        if self._find_entry(registry, name) is not None:
            raise ValueError(f"Library '{name}' already exists for path {root}")

        now_iso = datetime.utcnow().isoformat()

        registry.setdefault("libraries", []).append(
            {
                "name": name,
                "root_path": self._persisted_root_path(root),
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
        active_name = self.normalize_library_name(registry.get("active_library"))
        entry = self._find_entry(registry, active_name)
        if entry is None:
            return None
        root = Path(entry["root_path"])
        return LibraryInfo(
            name=active_name,
            root_path=entry["root_path"],
            description=str(entry.get("description") or ""),
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
        from app.db.session import init_db

        init_db(f"sqlite:///{db_path.as_posix()}")

    def _ensure_registry(self) -> None:
        registry_path = self.registry_path()
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        if not registry_path.exists():
            self._write_registry(
                {
                    "version": REGISTRY_VERSION,
                    "active_library": DEFAULT_LIBRARY_NAME,
                    "libraries": [
                        {
                            "name": DEFAULT_LIBRARY_NAME,
                            "root_path": str(self.default_library_root()),
                            "description": DEFAULT_LIBRARY_NAME,
                            "created_at": datetime.utcnow().isoformat(),
                        }
                    ],
                }
            )

        registry = self._read_registry()
        default_entry = self._find_entry(registry, DEFAULT_LIBRARY_NAME)
        if default_entry is None:
            registry.setdefault("libraries", []).insert(
                0,
                {
                    "name": DEFAULT_LIBRARY_NAME,
                    "root_path": str(self.default_library_root()),
                    "description": DEFAULT_LIBRARY_NAME,
                    "created_at": datetime.utcnow().isoformat(),
                },
            )
            registry["active_library"] = DEFAULT_LIBRARY_NAME
            self._write_registry(registry)
            default_entry = self._find_entry(registry, DEFAULT_LIBRARY_NAME)

        assert default_entry is not None
        default_root = Path(default_entry["root_path"]).resolve()
        self.init_library_structure(default_root, storage_mode=LEGACY_STORAGE_MODE)
        if not self._force_configured_database() and not (default_root / "database.sqlite").exists():
            self.init_library_db(default_root)
        default_meta = self._build_library_meta(
            name=DEFAULT_LIBRARY_NAME,
            description=str(default_entry.get("description") or DEFAULT_LIBRARY_NAME),
            created_at=default_entry.get("created_at"),
            storage_mode=LEGACY_STORAGE_MODE,
            library_kind=LEGACY_LIBRARY_KIND,
            meta=self._load_library_meta(default_root),
        )
        self._write_library_meta(default_root, default_meta)

    def _resolve_create_root(self, name: str, root_path: str) -> Path:
        safe_name = self._safe_library_dir_name(name)
        if root_path and root_path.strip():
            selected_parent = self._resolve_runtime_path(root_path)
            if selected_parent.name == safe_name:
                return selected_parent
            return (selected_parent / safe_name).resolve()
        return (self.default_library_root().parent / safe_name).resolve()

    @staticmethod
    def _safe_library_dir_name(name: str) -> str:
        return name.replace(" ", "_").replace("/", "_").replace("\\", "_")

    @staticmethod
    def _looks_like_existing_library_root(root: Path) -> bool:
        markers = (
            root / "database.sqlite",
            root / "library.json",
            root / "config" / "project_config.json",
            root / LEGACY_STORAGE_MODE,
            root / SHARED_STORAGE_MODE,
        )
        return any(path.exists() for path in markers)

    def _read_registry(self) -> dict[str, Any]:
        registry_path = self.registry_path()
        if not registry_path.exists():
            return {"version": REGISTRY_VERSION, "active_library": DEFAULT_LIBRARY_NAME, "libraries": []}
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": REGISTRY_VERSION, "active_library": DEFAULT_LIBRARY_NAME, "libraries": []}
        if not isinstance(data, dict):
            return {"version": REGISTRY_VERSION, "active_library": DEFAULT_LIBRARY_NAME, "libraries": []}
        normalized = self._normalize_registry_payload(data)
        if normalized != data:
            self._write_registry(normalized)
        return normalized

    def _write_registry(self, registry: dict[str, Any]) -> None:
        registry_path = self.registry_path()
        normalized = self._normalize_registry_payload(registry)
        normalized["version"] = REGISTRY_VERSION
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = registry_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(registry_path)

    @classmethod
    def _normalize_registry_payload(cls, registry: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {
            "version": int(registry.get("version") or REGISTRY_VERSION),
            "active_library": cls.normalize_library_name(registry.get("active_library")),
            "libraries": [],
        }
        seen: set[str] = set()
        for raw_entry in registry.get("libraries", []):
            if not isinstance(raw_entry, dict):
                continue
            name = cls.normalize_library_name(raw_entry.get("name"))
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized["libraries"].append(
                {
                    "name": name,
                    "root_path": cls._normalize_registry_root_path(name, raw_entry.get("root_path")),
                    "description": str(raw_entry.get("description") or name),
                    "created_at": raw_entry.get("created_at"),
                }
            )
        if cls._find_entry(normalized, DEFAULT_LIBRARY_NAME) is None:
            normalized["libraries"].insert(
                0,
                {
                    "name": DEFAULT_LIBRARY_NAME,
                    "root_path": str(cls.default_library_root()),
                    "description": DEFAULT_LIBRARY_NAME,
                    "created_at": datetime.utcnow().isoformat(),
                },
            )
        if cls._find_entry(normalized, normalized.get("active_library")) is None:
            normalized["active_library"] = DEFAULT_LIBRARY_NAME
        return normalized

    @classmethod
    def _normalize_registry_root_path(cls, library_name: str, root_path: Any) -> str:
        text = str(root_path or "").strip()
        if cls.normalize_library_name(library_name) == DEFAULT_LIBRARY_NAME and cls._is_default_library_root_residue(text):
            return str(cls.default_library_root())
        if not text:
            return str(cls.default_library_root() if cls.normalize_library_name(library_name) == DEFAULT_LIBRARY_NAME else "")
        data_suffix = cls._data_mount_suffix(text)
        if data_suffix is not None:
            return cls._container_data_path(data_suffix)
        return cls._persisted_root_path(Path(text).resolve())

    @classmethod
    def _resolve_runtime_path(cls, path: Any) -> Path:
        text = str(path or "").strip()
        data_suffix = cls._data_mount_suffix(text)
        if data_suffix is not None:
            return (cls._data_root() / data_suffix).resolve()
        return Path(text).resolve()

    @classmethod
    def _data_root(cls) -> Path:
        return cls.default_library_root().parent.parent.resolve()

    @classmethod
    def _data_mount_suffix(cls, path: Any) -> str | None:
        text = str(path or "").strip()
        normalized = text.replace("\\", "/")
        lowered = normalized.lower()
        if normalized == "/data":
            return ""
        if normalized.startswith("/data/"):
            return normalized.removeprefix("/data/").strip("/")
        marker = "/literature-ai/data/"
        marker_index = lowered.rfind(marker)
        if marker_index >= 0:
            return normalized[marker_index + len(marker) :].strip("/")
        marker = "literature-ai/data/"
        marker_index = lowered.rfind(marker)
        if marker_index >= 0:
            return normalized[marker_index + len(marker) :].strip("/")
        return None

    @classmethod
    def _container_data_path(cls, suffix: str) -> str:
        suffix = suffix.strip("/")
        return "/data" + (f"/{suffix}" if suffix else "")

    @classmethod
    def _persisted_root_path(cls, path: Path) -> str:
        resolved = path.resolve()
        try:
            suffix = resolved.relative_to(cls._data_root()).as_posix()
            return cls._container_data_path(suffix)
        except ValueError:
            return str(resolved)

    @classmethod
    def _is_default_library_root_residue(cls, root_path: str) -> bool:
        text = str(root_path or "").strip()
        if not text:
            return True

        normalized = text.replace("\\", "/").lower()
        historical_markers = (
            "backend/data/libraries/default",
            "literature-ai/backend/data/libraries/default",
            "ai-shujvku/literature-ai/backend/data/libraries/default",
            "ai检索数据库/literature-ai/backend/data/libraries/default",
            "/app/d:",
            "d:/desktop/",
            "\uf03a",
            "\uf05c",
            "娴狅絿",
            "瀵偓",
        )
        if any(marker in normalized for marker in historical_markers):
            return True

        try:
            resolved = Path(text).resolve()
        except OSError:
            return True

        if resolved == cls.default_library_root().resolve():
            return True

        resolved_parts = [part.lower() for part in resolved.parts]
        suffix = ["data", "libraries", "default"]
        return len(resolved_parts) >= len(suffix) and resolved_parts[-len(suffix) :] == suffix

    @staticmethod
    def _find_entry(registry: dict[str, Any], name: str | None) -> dict[str, Any] | None:
        target = LibraryManager.normalize_library_name(name)
        for entry in registry.get("libraries", []):
            if LibraryManager.normalize_library_name(entry.get("name")) == target:
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
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_library_meta(self, root: Path, payload: dict[str, Any]) -> None:
        self._meta_path(root).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _best_effort_write_library_meta(self, root: Path, payload: dict[str, Any]) -> None:
        try:
            self._write_library_meta(root, payload)
        except PermissionError:
            logger.warning("skip updating library metadata for read-only root: %s", root)
        except OSError as exc:
            logger.warning("skip updating library metadata for %s: %s", root, exc)

    @staticmethod
    def _load_project_config(root: Path) -> dict[str, Any]:
        config_path = root / "config" / "project_config.json"
        if not config_path.exists():
            return {}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

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

    @classmethod
    def _best_effort_ensure_shared_project_config(
        cls,
        root: Path,
        project_name: str,
        project_config: dict[str, Any] | None = None,
    ) -> None:
        try:
            cls._ensure_shared_project_config(root, project_name, project_config)
        except PermissionError:
            logger.warning("skip updating shared project config for read-only root: %s", root)
        except OSError as exc:
            logger.warning("skip updating shared project config for %s: %s", root, exc)

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
                raise ValueError(f"Refusing to operate on system path: {path}")
