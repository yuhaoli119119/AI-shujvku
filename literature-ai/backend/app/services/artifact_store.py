from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config import Settings


class ArtifactStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        root = settings.storage_paths["root"]
        if settings.force_configured_database and not root.exists():
            raise RuntimeError(
                f"Configured storage root does not exist: {root}. "
                "Mount the B-computer shared storage before ingesting files."
            )
        root.mkdir(parents=not settings.force_configured_database, exist_ok=True)
        for key, path in settings.storage_paths.items():
            if key == "root":
                continue
            path.mkdir(parents=False, exist_ok=True)

    def save_pdf_copy(self, source_path: Path, target_name: str) -> Path:
        destination = self.settings.storage_paths["pdf"] / target_name
        shutil.copy2(source_path, destination)
        return destination

    async def save_upload(self, upload, target_name: str) -> Path:
        destination = self.settings.storage_paths["pdf"] / target_name
        with destination.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        return destination

    def write_text(self, category: str, target_name: str, content: str) -> Path:
        destination = self.settings.storage_paths[category] / target_name
        destination.write_text(content, encoding="utf-8")
        return destination

    def write_json(self, category: str, target_name: str, payload: dict) -> Path:
        destination = self.settings.storage_paths[category] / target_name
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return destination
