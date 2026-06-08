from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts import codex_acceptance_gate as codex_gate
from scripts import fresh_realpaper_chain_acceptance as fresh_gate


SCHEMA_VERSION = "fresh_chain_repeatability_acceptance_v1"
REPEATABILITY_AUDIT_SOURCE = "codex_fresh_repeatability_audit"
REPEATABILITY_AUDIT_SOURCE_LABEL = "Codex fresh chain repeatability audit"
LEGACY_GATE_PAPER_IDS = (
    "2d977b15-7715-4a27-87e3-985dc77c4da1,"
    "d5d5c467-8a91-4f9a-9c93-4e4c84a30bab,"
    "e636ff33-55fc-436d-b4ec-1b4f064f4050"
)
LEGACY_GATE_LIBRARY_NAME = "chain_realpaper_smoke_20260608"

ALLOWED_ROOT_CAUSES = {
    "real_pdf_source_unavailable",
    "ingestion_failed",
    "parse_failed",
    "artifact_refs_not_persisted_to_active_postgres",
    "artifact_files_not_present_in_api_storage",
    "workspace_not_created",
    "ai_reading_package_missing",
    "external_audit_import_failed",
    "external_audit_candidate_not_created",
    "coverage_not_visible",
    "review_center_not_visible",
    "api_artifact_status_uses_different_code_path",
    "api_server_not_reloaded_or_running_old_code",
    "storage_root_mismatch_between_cli_and_api",
    "legacy_sqlite_used_as_runtime_source",
    "repeatability_inconsistent_results",
    "unknown",
}

REPEATABILITY_DISCOVERY_QUERIES = [
    "oxygen reduction reaction single atom catalyst density functional theory",
    "metal nitrogen carbon oxygen reduction catalyst DFT",
    "electrocatalyst density functional theory oxygen reduction",
    "single atom catalyst oxygen evolution reaction DFT",
    "nitrogen doped graphene oxygen reduction density functional theory catalyst",
    "oxygen evolution reaction single atom catalyst density functional theory",
    "hydrogen evolution reaction single atom catalyst DFT",
    "CO2 reduction single atom catalyst density functional theory",
    "metal nitrogen carbon catalyst density functional theory",
]


@dataclass(frozen=True)
class Options:
    library_prefix: str
    api_base: str
    rounds: int
    min_real_papers_per_round: int
    target_real_papers_per_round: int
    output: Path
    markdown: Path


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description="Run repeated fresh real-paper chain acceptance rounds.")
    parser.add_argument("--library-prefix", required=True)
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--min-real-papers-per-round", type=int, default=1)
    parser.add_argument("--target-real-papers-per-round", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.rounds < 2:
        parser.error("--rounds must be at least 2")
    if args.min_real_papers_per_round <= 0:
        parser.error("--min-real-papers-per-round must be positive")
    if args.target_real_papers_per_round < args.min_real_papers_per_round:
        parser.error("--target-real-papers-per-round must be >= --min-real-papers-per-round")
    return Options(
        library_prefix=args.library_prefix,
        api_base=args.api_base.rstrip("/"),
        rounds=args.rounds,
        min_real_papers_per_round=args.min_real_papers_per_round,
        target_real_papers_per_round=args.target_real_papers_per_round,
        output=args.output,
        markdown=args.markdown,
    )


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(item) for item in value]
    return value


def patch_fresh_gate_for_repeatability() -> dict[str, Any]:
    previous = {
        "AUDIT_SOURCE": fresh_gate.AUDIT_SOURCE,
        "AUDIT_SOURCE_LABEL": fresh_gate.AUDIT_SOURCE_LABEL,
        "DISCOVERY_QUERIES": list(fresh_gate.DISCOVERY_QUERIES),
    }
    fresh_gate.AUDIT_SOURCE = REPEATABILITY_AUDIT_SOURCE
    fresh_gate.AUDIT_SOURCE_LABEL = REPEATABILITY_AUDIT_SOURCE_LABEL
    fresh_gate.DISCOVERY_QUERIES = REPEATABILITY_DISCOVERY_QUERIES
    return previous


def restore_fresh_gate(previous: dict[str, Any]) -> None:
    fresh_gate.AUDIT_SOURCE = previous["AUDIT_SOURCE"]
    fresh_gate.AUDIT_SOURCE_LABEL = previous["AUDIT_SOURCE_LABEL"]
    fresh_gate.DISCOVERY_QUERIES = previous["DISCOVERY_QUERIES"]


def run_fresh_round(options: Options, round_number: int) -> dict[str, Any]:
    round_prefix = f"{options.library_prefix}_round{round_number:02d}"
    round_options = fresh_gate.Options(
        library_prefix=round_prefix,
        api_base=options.api_base,
        min_real_papers=options.min_real_papers_per_round,
        target_real_papers=options.target_real_papers_per_round,
        output=Path("-"),
        markdown=Path("-"),
    )
    started_at = datetime.now(timezone.utc)
    try:
        fresh_report = fresh_gate.build_report(round_options)
    except Exception as exc:
        fresh_report = {
            "schema_version": fresh_gate.SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "library_prefix": round_prefix,
            "library_name": round_prefix,
            "fresh_realpaper_chain_acceptance": "FAIL",
            "root_cause": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
            "paper_ids": [],
            "real_pdf_source": "unavailable",
            "verification": {"items": []},
        }
    return {
        "round": round_number,
        "requested_library_prefix": round_prefix,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "fresh_gate": fresh_report,
    }


def run_legacy_gate(api_base: str) -> dict[str, Any]:
    args = argparse.Namespace(
        paper_ids=LEGACY_GATE_PAPER_IDS,
        library_name=LEGACY_GATE_LIBRARY_NAME,
        api_base=api_base,
        output=Path("-"),
        markdown=Path("-"),
    )
    try:
        report = codex_gate.build_report(args)
    except Exception as exc:
        report = {
            "schema_version": "codex_acceptance_gate_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "library_name": LEGACY_GATE_LIBRARY_NAME,
            "paper_ids": [item.strip() for item in LEGACY_GATE_PAPER_IDS.split(",")],
            "api_base": api_base,
            "acceptance_gate": "FAIL",
            "root_cause": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return report


def item_ready(item: dict[str, Any]) -> bool:
    local_status = ((item.get("local") or {}).get("artifact_status") or {})
    checks = item.get("checks") or {}
    db_row = item.get("postgres_external_audit") or {}
    return all(
        [
            local_status.get("pdf_exists") is True,
            int(local_status.get("pdf_file_size") or 0) > 0,
            local_status.get("markdown_has_content") is True,
            local_status.get("docling_json_has_content") is True,
            local_status.get("workspace_exists") is True,
            local_status.get("ai_reading_package_exists") is True,
            local_status.get("artifact_ready_for_external_audit") is True,
            local_status.get("blocking_errors") == [],
            checks.get("api_get_paper_ready") is True,
            checks.get("api_get_codex_context_ready") is True,
            checks.get("api_review_center_ready") is True,
            checks.get("coverage_visible") is True,
            checks.get("review_center_visible") is True,
            int(db_row.get("external_audit_candidate_count") or 0) >= 1,
            checks.get("verified_count_zero") is True,
            checks.get("safe_verified_count_zero") is True,
        ]
    )


def first_failed_paper_id(round_report: dict[str, Any]) -> str | None:
    items = ((round_report.get("fresh_gate") or {}).get("verification") or {}).get("items") or []
    for item in items:
        if not item_ready(item):
            return str(item.get("paper_id") or "") or None
    return None


def paper_count(round_report: dict[str, Any]) -> int:
    return len(((round_report.get("fresh_gate") or {}).get("paper_ids") or []))


def round_summary(round_report: dict[str, Any], options: Options) -> dict[str, Any]:
    fresh = round_report.get("fresh_gate") or {}
    verification = fresh.get("verification") or {}
    items = verification.get("items") or []
    db_audit = verification.get("postgres_external_audit") or {}
    legacy = round_report.get("legacy_codex_gate") or {}
    all_items_ready = bool(items) and all(item_ready(item) for item in items)
    return {
        "round": round_report.get("round"),
        "status": fresh.get("fresh_realpaper_chain_acceptance"),
        "root_cause": fresh.get("root_cause"),
        "library_name": fresh.get("library_name"),
        "paper_ids": fresh.get("paper_ids") or [],
        "paper_count": len(fresh.get("paper_ids") or []),
        "real_pdf_source": fresh.get("real_pdf_source"),
        "all_papers_ready": all_items_ready,
        "below_target_count": len(fresh.get("paper_ids") or []) < options.target_real_papers_per_round,
        "external_audit_run_count": db_audit.get("run_count"),
        "external_audit_candidate_count": db_audit.get("candidate_count"),
        "verified_count": db_audit.get("verified_count"),
        "safe_verified_count": db_audit.get("safe_verified_count"),
        "legacy_codex_gate": legacy.get("acceptance_gate"),
        "legacy_codex_gate_root_cause": legacy.get("root_cause"),
        "failed_paper_id": first_failed_paper_id(round_report),
    }


def classify_round_root_cause(round_report: dict[str, Any], options: Options) -> tuple[str | None, str | None]:
    fresh = round_report.get("fresh_gate") or {}
    if fresh.get("fresh_realpaper_chain_acceptance") != "PASS":
        cause = fresh.get("root_cause") or "unknown"
        return (cause if cause in ALLOWED_ROOT_CAUSES else "unknown", first_failed_paper_id(round_report))
    if fresh.get("real_pdf_source") != "downloaded_by_pipeline":
        return "real_pdf_source_unavailable", first_failed_paper_id(round_report)
    if paper_count(round_report) < options.min_real_papers_per_round:
        return "real_pdf_source_unavailable", first_failed_paper_id(round_report)
    for item in ((fresh.get("verification") or {}).get("items") or []):
        if not item_ready(item):
            local_status = ((item.get("local") or {}).get("artifact_status") or {})
            checks = item.get("checks") or {}
            if local_status.get("pdf_exists") is not True or not local_status.get("pdf_file_size"):
                return "artifact_files_not_present_in_api_storage", str(item.get("paper_id"))
            if local_status.get("markdown_has_content") is not True or local_status.get("docling_json_has_content") is not True:
                return "parse_failed", str(item.get("paper_id"))
            if local_status.get("workspace_exists") is not True:
                return "workspace_not_created", str(item.get("paper_id"))
            if local_status.get("ai_reading_package_exists") is not True:
                return "ai_reading_package_missing", str(item.get("paper_id"))
            if checks.get("api_get_paper_ready") is not True or checks.get("api_get_codex_context_ready") is not True:
                return "api_artifact_status_uses_different_code_path", str(item.get("paper_id"))
            if checks.get("coverage_visible") is not True:
                return "coverage_not_visible", str(item.get("paper_id"))
            if checks.get("review_center_visible") is not True:
                return "review_center_not_visible", str(item.get("paper_id"))
            return "unknown", str(item.get("paper_id"))
    legacy = round_report.get("legacy_codex_gate") or {}
    if legacy.get("acceptance_gate") != "PASS":
        cause = legacy.get("root_cause") or "unknown"
        return (cause if cause in ALLOWED_ROOT_CAUSES else "unknown", None)
    return None, None


def classify_report(report: dict[str, Any], options: Options) -> tuple[str | None, int | None, str | None]:
    rounds = report.get("rounds_detail") or []
    if len(rounds) < options.rounds:
        return "repeatability_inconsistent_results", len(rounds) + 1, None
    seen_paper_ids: set[str] = set()
    for round_report in rounds:
        cause, failed_paper_id = classify_round_root_cause(round_report, options)
        if cause:
            return cause, int(round_report.get("round") or 0) or None, failed_paper_id
        paper_ids = set(((round_report.get("fresh_gate") or {}).get("paper_ids") or []))
        if seen_paper_ids.intersection(paper_ids):
            repeated = sorted(seen_paper_ids.intersection(paper_ids))[0]
            return "repeatability_inconsistent_results", int(round_report.get("round") or 0) or None, repeated
        seen_paper_ids.update(paper_ids)
    return None, None, None


def build_report(options: Options) -> dict[str, Any]:
    previous = patch_fresh_gate_for_repeatability()
    rounds_detail: list[dict[str, Any]] = []
    try:
        for round_number in range(1, options.rounds + 1):
            round_report = run_fresh_round(options, round_number)
            round_report["legacy_codex_gate"] = run_legacy_gate(options.api_base)
            rounds_detail.append(round_report)
            cause, _failed_paper_id = classify_round_root_cause(round_report, options)
            if cause:
                break
    finally:
        restore_fresh_gate(previous)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library_prefix": options.library_prefix,
        "api_base": options.api_base,
        "requested_rounds": options.rounds,
        "executed_rounds": len(rounds_detail),
        "min_real_papers_per_round": options.min_real_papers_per_round,
        "target_real_papers_per_round": options.target_real_papers_per_round,
        "audit_source": REPEATABILITY_AUDIT_SOURCE,
        "legacy_gate": {
            "paper_ids": LEGACY_GATE_PAPER_IDS.split(","),
            "library_name": LEGACY_GATE_LIBRARY_NAME,
        },
        "round_summaries": [round_summary(item, options) for item in rounds_detail],
        "rounds_detail": rounds_detail,
    }
    root_cause, failed_round, failed_paper_id = classify_report(report, options)
    if root_cause not in ALLOWED_ROOT_CAUSES and root_cause is not None:
        root_cause = "unknown"
    report["fresh_chain_repeatability_acceptance"] = "FAIL" if root_cause else "PASS"
    report["root_cause"] = root_cause
    report["failed_round"] = failed_round
    report["failed_paper_id"] = failed_paper_id
    report["consistency"] = {
        "all_rounds_passed": root_cause is None,
        "real_pdf_source_distribution": dict(
            Counter(item.get("real_pdf_source") for item in report["round_summaries"])
        ),
        "paper_count_distribution": dict(Counter(item.get("paper_count") for item in report["round_summaries"])),
        "total_new_paper_count": sum(int(item.get("paper_count") or 0) for item in report["round_summaries"]),
        "below_target_rounds": [
            item.get("round")
            for item in report["round_summaries"]
            if item.get("below_target_count")
        ],
    }
    return json_safe(report)


def escape_md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    status = report.get("fresh_chain_repeatability_acceptance")
    root_cause = report.get("root_cause")
    lines = [
        "# Fresh Chain Repeatability Acceptance",
        "",
        f"FRESH_CHAIN_REPEATABILITY_ACCEPTANCE={status}",
    ]
    if root_cause:
        lines.extend(
            [
                f"root_cause={root_cause}",
                f"failed_round={report.get('failed_round')}",
                f"failed_paper_id={report.get('failed_paper_id')}",
            ]
        )
    lines.extend(
        [
            "",
            f"- Created at: `{report.get('created_at')}`",
            f"- API base: `{report.get('api_base')}`",
            f"- Requested rounds: `{report.get('requested_rounds')}`",
            f"- Executed rounds: `{report.get('executed_rounds')}`",
            f"- Min real papers per round: `{report.get('min_real_papers_per_round')}`",
            f"- Target real papers per round: `{report.get('target_real_papers_per_round')}`",
            f"- Audit source: `{report.get('audit_source')}`",
            f"- Total new paper count: `{(report.get('consistency') or {}).get('total_new_paper_count')}`",
            f"- real_pdf_source distribution: `{(report.get('consistency') or {}).get('real_pdf_source_distribution')}`",
            f"- Below-target rounds: `{(report.get('consistency') or {}).get('below_target_rounds')}`",
            "",
            "## Rounds",
            "",
            "| Round | Status | Library | Paper IDs | real_pdf_source | All Ready | Runs | Candidates | Verified | Safe Verified | Legacy Gate | Failed Paper |",
            "| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for item in report.get("round_summaries") or []:
        lines.append(
            "| {round} | {status} | {library} | {paper_ids} | {source} | {ready} | {runs} | {candidates} | {verified} | {safe_verified} | {legacy} | {failed_paper} |".format(
                round=escape_md(item.get("round")),
                status=escape_md(item.get("status")),
                library=escape_md(item.get("library_name")),
                paper_ids=escape_md(", ".join(item.get("paper_ids") or [])),
                source=escape_md(item.get("real_pdf_source")),
                ready=escape_md(item.get("all_papers_ready")),
                runs=escape_md(item.get("external_audit_run_count")),
                candidates=escape_md(item.get("external_audit_candidate_count")),
                verified=escape_md(item.get("verified_count")),
                safe_verified=escape_md(item.get("safe_verified_count")),
                legacy=escape_md(item.get("legacy_codex_gate")),
                failed_paper=escape_md(item.get("failed_paper_id")),
            )
        )
    lines.extend(["", "## Per-Paper Checks", ""])
    for round_detail in report.get("rounds_detail") or []:
        round_number = round_detail.get("round")
        fresh = round_detail.get("fresh_gate") or {}
        lines.extend(["", f"### Round {round_number}", ""])
        lines.extend(
            [
                "| Paper ID | Title | PDF | PDF Size | Markdown | Docling | Workspace | AI Package | Local Ready | API Detail | API Codex | API Review Center | Coverage | Review Center | Candidates | Blocking Errors |",
                "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for item in ((fresh.get("verification") or {}).get("items") or []):
            local_status = ((item.get("local") or {}).get("artifact_status") or {})
            checks = item.get("checks") or {}
            db_row = item.get("postgres_external_audit") or {}
            lines.append(
                "| {paper_id} | {title} | {pdf} | {pdf_size} | {markdown} | {docling} | {workspace} | {ai_pkg} | {local_ready} | {api_detail} | {api_codex} | {api_review} | {coverage} | {review_center} | {candidates} | {errors} |".format(
                    paper_id=escape_md(item.get("paper_id")),
                    title=escape_md(item.get("title")),
                    pdf=escape_md(local_status.get("pdf_exists")),
                    pdf_size=escape_md(local_status.get("pdf_file_size")),
                    markdown=escape_md(local_status.get("markdown_has_content")),
                    docling=escape_md(local_status.get("docling_json_has_content")),
                    workspace=escape_md(local_status.get("workspace_exists")),
                    ai_pkg=escape_md(local_status.get("ai_reading_package_exists")),
                    local_ready=escape_md(checks.get("local_ready")),
                    api_detail=escape_md(checks.get("api_get_paper_ready")),
                    api_codex=escape_md(checks.get("api_get_codex_context_ready")),
                    api_review=escape_md(checks.get("api_review_center_ready")),
                    coverage=escape_md(checks.get("coverage_visible")),
                    review_center=escape_md(checks.get("review_center_visible")),
                    candidates=escape_md(db_row.get("external_audit_candidate_count")),
                    errors=escape_md(", ".join(local_status.get("blocking_errors") or [])),
                )
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    report = build_report(options)
    write_json(options.output, report)
    write_markdown(options.markdown, report)
    print(
        json.dumps(
            {
                "status": report["fresh_chain_repeatability_acceptance"],
                "root_cause": report.get("root_cause"),
                "failed_round": report.get("failed_round"),
                "failed_paper_id": report.get("failed_paper_id"),
                "executed_rounds": report.get("executed_rounds"),
                "round_libraries": [item.get("library_name") for item in report.get("round_summaries") or []],
                "output": str(options.output),
                "markdown": str(options.markdown),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["fresh_chain_repeatability_acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
