from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config import Settings
from app.security.files import validate_pdf_file


class ArtifactStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        for path in settings.storage_paths.values():
            path.mkdir(parents=True, exist_ok=True)

    def save_pdf_copy(self, source_path: Path, target_name: str) -> Path:
        source_path = validate_pdf_file(source_path)
        destination = self.settings.storage_paths["pdf"] / target_name
        shutil.copy2(source_path, destination)
        return destination

    async def save_upload(self, upload, target_name: str) -> Path:
        destination = self.settings.storage_paths["pdf"] / target_name
        with destination.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        try:
            validate_pdf_file(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
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
