from __future__ import annotations

import argparse
from collections import Counter
import shutil
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "literature-ai"
BACKEND = PROJECT / "backend"
FRONTEND = PROJECT / "frontend"
NPM = shutil.which("npm.cmd") if sys.platform == "win32" else shutil.which("npm")
if NPM is None:
    NPM = "npm.cmd" if sys.platform == "win32" else "npm"


def run_step(label: str, command: list[str], *, cwd: Path, timeout: int) -> None:
    started = time.monotonic()
    print(f"\n=== {label} ===", flush=True)
    print(f"cwd: {cwd}", flush=True)
    print("command: " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(command, cwd=cwd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"{label} timed out after {timeout}s") from exc
    elapsed = time.monotonic() - started
    print(f"=== {label}: exit={completed.returncode} elapsed={elapsed:.1f}s ===", flush=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def verify_static() -> None:
    run_step("repository layout", [sys.executable, "scripts/check_repo_layout.py"], cwd=ROOT, timeout=60)
    run_step(
        "Python compile",
        [sys.executable, "-m", "compileall", "-q", "app", "findpapers", "tests"],
        cwd=BACKEND,
        timeout=180,
    )


def backend_test_shards(shard_count: int = 4) -> list[list[str]]:
    """Balance test files by collected case count so full runs show bounded progress."""
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=BACKEND,
        timeout=120,
        check=False,
        capture_output=True,
        text=True,
    )
    if collected.returncode != 0:
        print(collected.stdout, end="")
        print(collected.stderr, end="", file=sys.stderr)
        raise SystemExit(collected.returncode)
    counts = Counter()
    for line in collected.stdout.splitlines():
        normalized = line.replace("\\", "/")
        if normalized.startswith("tests/") and "::" in normalized:
            counts[normalized.split("::", 1)[0]] += 1
    if not counts:
        raise SystemExit("pytest collection returned no test node IDs")

    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0] * shard_count
    for test_file, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(shard_count), key=lambda index: totals[index])
        shards[target].append(test_file)
        totals[target] += count
    print("backend shard case counts: " + ", ".join(str(total) for total in totals), flush=True)
    return shards


def verify_fast() -> None:
    verify_static()
    run_step(
        "focused backend regression",
        [
            sys.executable,
            "-m",
            "pytest",
            "-vv",
            "--tb=short",
            "--timeout=60",
            "--durations=15",
            "tests/test_db_bootstrap.py",
            "tests/test_security_boundaries.py",
            "tests/test_dft_review_bundle.py",
            "tests/test_b0076_chart_field_review.py",
            "tests/test_papers_api.py::test_upload_failure_rolls_back_failed_session_and_preserves_original_error",
            "tests/test_papers_api.py::test_local_path_failure_rolls_back_failed_session_and_preserves_original_error",
            "tests/test_papers_api.py::test_attach_pdf_failure_rolls_back_failed_session_and_preserves_original_error",
            "tests/test_papers_api.py::test_supplementary_upload_failure_rolls_back_failed_session_and_preserves_original_error",
        ],
        cwd=BACKEND,
        timeout=600,
    )
    run_step("focused frontend regression", [NPM, "run", "test:fast"], cwd=FRONTEND, timeout=300)


def verify_full() -> None:
    verify_static()
    for index, test_files in enumerate(backend_test_shards(), start=1):
        run_step(
            f"full backend suite shard {index}/4",
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "--timeout=60", "--durations=15", *test_files],
            cwd=BACKEND,
            timeout=1200,
        )
    run_step("full frontend suite", [NPM, "run", "test:full"], cwd=FRONTEND, timeout=900)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-platform Literature AI verification runner")
    parser.add_argument("scope", choices=("fast", "full"), nargs="?", default="fast")
    args = parser.parse_args()
    if args.scope == "fast":
        verify_fast()
    else:
        verify_full()


if __name__ == "__main__":
    main()
