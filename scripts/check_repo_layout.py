from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT_PATHS = (
    "backups",
    "outputs",
    "test-artifacts",
    "literature-ai-security-backups",
)
PDF_EVAL_RUNTIME_ARTIFACTS = (
    "eval.sqlite",
    "ingestion_results.json",
    "storage",
)


def collect_violations() -> list[str]:
    violations: list[str] = []

    for relative in LEGACY_ROOT_PATHS:
        if (ROOT / relative).exists():
            violations.append(f"legacy root artifact path still exists: {relative}")

    pdf_eval_fixture_dir = ROOT / "local" / "test-fixtures" / "pdf-eval"
    for name in PDF_EVAL_RUNTIME_ARTIFACTS:
        if (pdf_eval_fixture_dir / name).exists():
            violations.append(
                "runtime pdf-eval artifact must not live under local/test-fixtures/pdf-eval: "
                f"{name}"
            )

    pdf_eval_pdfs_dir = pdf_eval_fixture_dir / "pdfs"
    if pdf_eval_fixture_dir.exists() and not pdf_eval_pdfs_dir.exists():
        violations.append("expected local/test-fixtures/pdf-eval/pdfs to hold reusable PDF inputs")

    pdf_eval_runtime_dir = ROOT / "local" / "test-runs" / "pdf-eval" / "legacy_snapshot"
    for name in PDF_EVAL_RUNTIME_ARTIFACTS:
        if pdf_eval_fixture_dir.exists() and not (pdf_eval_runtime_dir / name).exists():
            violations.append(
                "expected runtime pdf-eval snapshot under local/test-runs/pdf-eval/legacy_snapshot: "
                f"{name}"
            )

    return violations


def main() -> int:
    violations = collect_violations()
    if violations:
        for violation in violations:
            print(violation)
        return 1
    print("repo layout check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
