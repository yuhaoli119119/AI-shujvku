from __future__ import annotations
import hashlib
import json
from typing import Any
def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
def table_version(row) -> str:
    return canonical_hash({"id": str(row.id), "paper_id": str(row.paper_id), "caption": row.caption, "page": row.page, "markdown_content": row.markdown_content, "prov": row.prov})[:24]
def task_fingerprint(payload: dict[str, Any]) -> str:
    return canonical_hash({"paper_id": payload["paper_id"], "source_documents": payload["source_documents"], "figures": payload["figures"], "tables": payload["tables"], "registry_version": payload["figure_type_registry"]["version"], "prompt_version": payload["prompt_version"]})
