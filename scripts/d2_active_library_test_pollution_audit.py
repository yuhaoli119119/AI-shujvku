from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "literature-ai"
    / "backend"
    / "scripts"
    / "d2_active_library_test_pollution_audit.py"
)


def _load_backend_script():
    spec = importlib.util.spec_from_file_location("backend_d2_active_library_test_pollution_audit", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend test pollution audit script: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = _load_backend_script()
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
