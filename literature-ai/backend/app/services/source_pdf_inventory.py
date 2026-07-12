from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_source_pdf_inventory(
    source_documents: list[dict[str, Any]],
    *,
    max_count: int,
    max_total_bytes: int,
) -> list[dict[str, Any]]:
    """Deterministically decide which source PDFs enter an offline bundle.

    The returned private `_pdf_abs_path` is only for the server to write the ZIP.
    Every other field is safe to serialize into manifests and parsed metadata.
    """

    inventory: list[dict[str, Any]] = []
    included_count = 0
    included_bytes = 0
    ordered = sorted(
        source_documents,
        key=lambda item: (str(item.get("role") or "") != "main", str(item.get("paper_id") or "")),
    )
    for source in ordered:
        role = str(source.get("role") or "source")
        paper_id = str(source.get("paper_id") or "")
        paper_code = source.get("paper_code")
        raw_path = source.get("_pdf_abs_path")
        path = Path(str(raw_path)) if raw_path else None
        available = bool(path and path.is_file())
        item: dict[str, Any] = {
            "paper_id": paper_id,
            "paper_code": paper_code,
            "role": role,
            "pdf_available": available,
            "size_bytes": None,
            "sha256": None,
            "included_in_bundle": False,
            "bundle_file": None,
            "omitted_reason": None,
            "_pdf_abs_path": str(path) if path is not None else None,
        }
        if not available:
            item["omitted_reason"] = "missing_pdf"
        else:
            size = path.stat().st_size
            item["size_bytes"] = size
            item["sha256"] = _file_sha256(path)
            safe_code = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(paper_code or paper_id)).strip("._") or role
            bundle_file = "source/main.pdf" if role == "main" else f"source/{role}/{safe_code}.pdf"
            if included_count >= max_count:
                item["omitted_reason"] = "source_pdf_file_limit_reached"
            elif included_bytes + size > max_total_bytes:
                item["omitted_reason"] = "source_pdf_byte_limit_reached"
            else:
                item["included_in_bundle"] = True
                item["bundle_file"] = bundle_file
                included_count += 1
                included_bytes += size
        inventory.append(item)
    return inventory


def public_source_pdf_inventory(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in inventory]
