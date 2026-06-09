from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models import ExternalAnalysisCandidate, ExternalAnalysisRun
from app.db.session import session_scope
from app.mcp.auth import parse_mcp_api_keys


SMOKE_SOURCE = "mcp_ai_workflow_smoke"
logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)


def _resolve_api_key(explicit_key: str | None) -> str:
    if explicit_key:
        return explicit_key
    configs = parse_mcp_api_keys(get_settings().mcp_api_keys)
    for config in configs.values():
        if "review_corrections" not in config.capabilities:
            return config.raw_key
    raise RuntimeError("No ordinary non-admin MCP API key found. Pass --api-key explicitly.")


def _json_from_tool_result(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", None) or []
    if not content:
        return {}
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, dict):
        text = first.get("text")
    if not text:
        return {}
    return json.loads(text)


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments=arguments)
    return _json_from_tool_result(result)


def _first_item(context: dict[str, Any]) -> tuple[str | None, str | None]:
    payload = context.get("context") or {}
    content = payload.get("content") or {}
    for item_type, collection in (
        ("dft_result", (payload.get("structured_candidates") or {}).get("dft_results") or []),
        ("writing_card", (payload.get("structured_candidates") or {}).get("writing_cards") or []),
        ("mechanism_claim", (payload.get("structured_candidates") or {}).get("mechanism_claims") or []),
        ("figure", content.get("figures") or []),
        ("table", content.get("tables") or []),
        ("section", content.get("sections") or []),
    ):
        if collection and collection[0].get("id"):
            return item_type, collection[0]["id"]
    return None, None


def _cleanup_run(run_id: str | None) -> bool:
    if not run_id:
        return False
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        run = session.get(ExternalAnalysisRun, UUID(run_id))
        if run is None:
            return False
        if run.source != SMOKE_SOURCE:
            raise RuntimeError(f"Refusing to delete non-smoke run {run_id} from source {run.source}")
        session.execute(delete(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == run.id))
        session.delete(run)
        session.commit()
        return True


def _candidate_status(run_id: str | None) -> dict[str, Any]:
    if not run_id:
        return {}
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == UUID(run_id))
        ).all()
        return {
            "candidate_count_db": len(candidates),
            "external_audit_opinion_created": any(
                item.candidate_type == "external_audit_opinion" for item in candidates
            ),
            "verification_statuses": [
                (item.normalized_payload or {}).get("verification_status") for item in candidates
            ],
        }


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    api_key = _resolve_api_key(args.api_key)
    headers = {"Authorization": f"Bearer {api_key}"}
    output: dict[str, Any] = {
        "mcp_url": args.mcp_url,
        "source": SMOKE_SOURCE,
        "api_key_mode": "explicit" if args.api_key else "auto_non_admin",
        "rollback": False,
    }

    async with streamablehttp_client(args.mcp_url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()

            papers = await _call(
                client,
                "query_papers",
                {
                    "q": args.query,
                    "sort_by": "created_at",
                    "sort_order": "desc",
                    "limit": args.limit,
                },
            )
            output["query_returned"] = papers.get("returned", 0)

            selected_context: dict[str, Any] | None = None
            selected_paper: dict[str, Any] | None = None
            for paper in papers.get("items") or []:
                context = await _call(
                    client,
                    "get_codex_context",
                    {
                        "paper_id": paper["id"],
                        "max_sections": 4,
                        "max_figures": 6,
                        "max_tables": 4,
                        "max_candidates": 12,
                    },
                )
                gate = ((context.get("context") or {}).get("external_audit_precondition") or {})
                if args.allow_not_ready or gate.get("status") == "ready":
                    selected_context = context
                    selected_paper = paper
                    break

            if selected_context is None or selected_paper is None:
                output.update(
                    {
                        "status": "skipped",
                        "reason": "no paper with ready external audit artifacts found",
                    }
                )
                return output

            context_payload = selected_context.get("context") or {}
            content = context_payload.get("content") or {}
            gate = context_payload.get("external_audit_precondition") or {}
            paper_id = selected_paper["id"]
            output.update(
                {
                    "status": "running",
                    "paper_id": paper_id,
                    "title": selected_paper.get("title"),
                    "artifact_gate": gate,
                    "sections": len(content.get("sections") or []),
                    "figures": len(content.get("figures") or []),
                    "tables": len(content.get("tables") or []),
                }
            )

            item_type, item_id = _first_item(selected_context)
            if item_type and item_id:
                item_context = await _call(
                    client,
                    "get_codex_item",
                    {"paper_id": paper_id, "item_type": item_type, "item_id": item_id},
                )
                output["item_probe"] = {
                    "item_type": item_type,
                    "item_id": item_id,
                    "schema_version": item_context.get("schema_version"),
                }
            else:
                output["item_probe"] = {"status": "skipped", "reason": "no supported item found"}

            imported = await _call(
                client,
                "import_analysis",
                {
                    "paper_id": paper_id,
                    "source": SMOKE_SOURCE,
                    "source_label": "MCP AI workflow smoke",
                    "raw_payload": {
                        "paper_id": paper_id,
                        "agent_role": "smoke_external_audit",
                        "verdict": "WARN",
                        "recommended_action": "smoke_probe_no_action_required",
                        "suspected_missing": [],
                        "metadata_status": "ok",
                        "section_structure_status": "ok",
                        "table_status": "ok",
                        "figure_status": "ok",
                        "dft_status": "warn",
                        "evidence_examples": [
                            {"text": "Rollback smoke import; do not use as a scientific review."}
                        ],
                        "confidence": 0.01,
                    },
                },
            )
            run_id = imported.get("run_id")
            output["import_analysis"] = imported
            output.update(_candidate_status(run_id))

            if not args.keep:
                output["rollback"] = _cleanup_run(run_id)

            output["status"] = "passed"
            return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the IDE AI MCP paper-review workflow over HTTP.")
    parser.add_argument("--mcp-url", default="http://localhost:8000/mcp/", help="Streamable HTTP MCP URL.")
    parser.add_argument("--api-key", default=None, help="Ordinary IDE AI MCP bearer token. Defaults to the first configured non-admin key.")
    parser.add_argument("--query", default="", help="Optional paper search query.")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent papers to scan.")
    parser.add_argument("--allow-not-ready", action="store_true", help="Allow probing the first paper even if artifact gate is not ready.")
    parser.add_argument("--keep", action="store_true", help="Keep the smoke external analysis run instead of deleting it.")
    args = parser.parse_args()

    try:
        result = asyncio.run(run_smoke(args))
    except BaseExceptionGroup as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "sub_errors": [
                        {
                            "type": type(item).__name__,
                            "message": str(item),
                        }
                        for item in exc.exceptions
                    ],
                    "traceback": "".join(traceback.format_exception(exc)),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "type": type(exc).__name__,
                    "traceback": "".join(traceback.format_exception(exc)),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"passed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
