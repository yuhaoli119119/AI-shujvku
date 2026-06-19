from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AuditLog, Paper, ParseJob
from app.services.paper_ingestion import PaperIngestionService
from app.security.files import validate_local_ingest_directory


class LocalPdfService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def scan_folder(
        self,
        folder_path: str,
        recursive: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        folder = self._resolve_folder(folder_path)
        pdf_paths = self._collect_pdf_paths(folder, recursive=recursive, limit=limit)
        existing_by_source = self._find_existing_papers(pdf_paths)

        items = []
        for path in pdf_paths:
            resolved = str(path.resolve())
            existing = existing_by_source.get(resolved)
            items.append(
                {
                    "path": resolved,
                    "filename": path.name,
                    "already_ingested": existing is not None,
                    "paper_id": str(existing.id) if existing else None,
                    "paper_title": existing.title if existing else None,
                }
            )

        return {
            "folder_path": str(folder),
            "recursive": recursive,
            "returned": len(items),
            "items": items,
        }

    async def ingest_folder(
        self,
        folder_path: str,
        requested_by: str,
        recursive: bool = True,
        limit: int = 20,
        only_unparsed: bool = True,
    ) -> dict[str, Any]:
        scan = self.scan_folder(folder_path=folder_path, recursive=recursive, limit=limit)
        ingestion = PaperIngestionService(session=self.session, settings=self.settings)

        results: list[dict[str, Any]] = []
        for item in scan["items"]:
            if item["already_ingested"] and only_unparsed:
                results.append(
                    {
                        "path": item["path"],
                        "status": "already_ingested",
                        "paper_id": item["paper_id"],
                        "paper_title": item["paper_title"],
                    }
                )
                continue

            job = ParseJob(
                identifier=item["path"],
                providers=["local_pdf"],
                requested_by=requested_by,
                status="running",
            )
            self.session.add(job)
            self.session.flush()

            try:
                paper = await ingestion.ingest_pdf(
                    source_path=Path(item["path"]),
                    original_filename=Path(item["path"]).name,
                    copy_pdf=True,
                    external_metadata=None,
                    source_reference=item["path"],
                    ingest_source="local_pdf",
                )
                job.status = "completed"
                job.paper_id = paper.id
                job.error_message = None
                self.session.add(
                    AuditLog(
                        paper_id=paper.id,
                        action="parse_local_pdf",
                        source=requested_by,
                        target_type="parse_job",
                        target_id=str(job.id),
                        payload={"path": item["path"]},
                    )
                )
                results.append(
                    {
                        "path": item["path"],
                        "status": "completed",
                        "paper_id": str(paper.id),
                        "paper_title": paper.title,
                        "job_id": str(job.id),
                    }
                )
            except Exception as exc:
                job.status = "failed"
                job.error_message = str(exc)
                self.session.add(
                    AuditLog(
                        action="parse_local_pdf_failed",
                        source=requested_by,
                        target_type="parse_job",
                        target_id=str(job.id),
                        payload={"path": item["path"], "error": str(exc)},
                    )
                )
                results.append(
                    {
                        "path": item["path"],
                        "status": "failed",
                        "error": str(exc),
                        "job_id": str(job.id),
                    }
                )

        return {
            "folder_path": scan["folder_path"],
            "recursive": recursive,
            "only_unparsed": only_unparsed,
            "requested": len(scan["items"]),
            "results": results,
        }

    def _resolve_folder(self, folder_path: str) -> Path:
        return validate_local_ingest_directory(Path(folder_path), self.settings)

    @staticmethod
    def _collect_pdf_paths(folder: Path, recursive: bool, limit: int) -> list[Path]:
        iterator = folder.rglob("*.pdf") if recursive else folder.glob("*.pdf")
        items = sorted(path for path in iterator if path.is_file())
        return items[: max(limit, 0)]

    def _find_existing_papers(self, paths: list[Path]) -> dict[str, Paper]:
        if not paths:
            return {}
        resolved_paths = [str(path.resolve()) for path in paths]
        rows = self.session.scalars(select(Paper).where(Paper.source_path.in_(resolved_paths))).all()
        return {row.source_path: row for row in rows if row.source_path}
