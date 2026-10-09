from __future__ import annotations

from pathlib import Path
import re

import pytest


pytestmark = pytest.mark.no_test_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _compose_service_block(compose_text: str, service_name: str) -> str:
    lines = compose_text.splitlines()
    start = lines.index(f"  {service_name}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  [A-Za-z0-9_-]+:$", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def test_worker_import_never_runs_database_schema_bootstrap() -> None:
    worker_source = (
        PROJECT_ROOT / "backend" / "app" / "workers" / "celery_app.py"
    ).read_text(encoding="utf-8")
    backend_source = (PROJECT_ROOT / "backend" / "app" / "main.py").read_text(
        encoding="utf-8"
    )

    assert "activate_active_library_database" not in worker_source
    assert "init_db" not in worker_source
    assert "activate_active_library_database()" in backend_source


@pytest.mark.parametrize("service_name", ["worker", "worker-pdf"])
def test_compose_workers_wait_for_healthy_backend(service_name: str) -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service_block = _compose_service_block(compose_text, service_name)

    assert re.search(
        r"(?m)^      backend:\n        condition: service_healthy$",
        service_block,
    )
