from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "project_library_v4_dry_run.py"

pytestmark = pytest.mark.no_test_database


def _load_tool_module():
    spec = importlib.util.spec_from_file_location("project_library_v4_dry_run", TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_database_url_adds_connect_timeout_without_overwriting_existing_value():
    tool = _load_tool_module()

    with_timeout = tool._database_url_with_timeout(
        "postgresql+psycopg://user:pass@localhost:5432/literature_ai",
        7,
    )
    assert "connect_timeout=7" in with_timeout

    existing_timeout = tool._database_url_with_timeout(
        "postgresql+psycopg://user:pass@localhost:5432/literature_ai?connect_timeout=3",
        7,
    )
    assert "connect_timeout=3" in existing_timeout


def test_dry_run_failure_report_is_read_only_and_structured(monkeypatch):
    tool = _load_tool_module()
    monkeypatch.setenv("LITAI_DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/literature_ai")

    report = tool.build_failure_report(
        context_key="li_s_sac_dac",
        library_name="Li-S test",
        tasks=("adsorption_energy",),
        connect_timeout=4,
        error=TimeoutError("connection timeout expired"),
    )

    assert report["schema_version"] == "project_library_v4_dry_run_report_v1"
    assert report["read_only"] is True
    assert report["database_write_authority"] == "none"
    assert report["submit_endpoint_called"] is False
    assert report["extraction_apply_called"] is False
    assert report["status"] == "database_unavailable"
    assert report["tasks_requested"] == ["adsorption_energy"]
    assert report["tasks"] == []
    assert report["error"]["type"] == "TimeoutError"
    assert report["error"]["connect_timeout_seconds"] == 4
    assert report["active_database"]["connection_checked_by"] == "project_library_v4_dry_run"


def test_dry_run_failure_report_can_target_server_api_without_local_db(monkeypatch):
    tool = _load_tool_module()

    def fail_get_settings():
        raise AssertionError("server API failure report must not read local database settings")

    monkeypatch.setattr(tool, "get_settings", fail_get_settings)

    report = tool.build_failure_report(
        context_key="li_s_sac_dac",
        library_name="锂硫双原子",
        tasks=("adsorption_energy",),
        connect_timeout=5,
        error=OSError("connection refused"),
        execution_mode="local_api",
        api_base_url="http://127.0.0.1:8000/",
    )

    assert report["status"] == "local_api_unavailable"
    assert report["execution_mode"] == "local_api"
    assert report["api_base_url"] == "http://127.0.0.1:8000"
    assert report["database_url_masked"] == "local_backend_api"
    assert report["active_database"]["db_url_masked"] == "local_backend_api"
    assert report["active_database"]["connection_checked_by"] == "project_library_v4_dry_run_api"


def test_dry_run_api_export_url_encodes_server_query_parameters():
    tool = _load_tool_module()

    url = tool._api_export_url(
        api_base_url="http://127.0.0.1:8000/",
        context_key="li_s_sac_dac",
        library_name="锂硫双原子",
        task="adsorption_energy",
        ready_only=True,
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "http://127.0.0.1:8000/api/dft/project-library-ml-export-v4"
    )
    assert query["context_key"] == ["li_s_sac_dac"]
    assert query["library_name"] == ["锂硫双原子"]
    assert query["task"] == ["adsorption_energy"]
    assert query["ready_only"] == ["true"]


def test_dry_run_build_api_report_is_read_only_and_uses_local_api(monkeypatch):
    tool = _load_tool_module()
    requested_urls: list[str] = []

    def fake_fetch_json(url: str, *, timeout: int):
        requested_urls.append(url)
        query = parse_qs(urlparse(url).query)
        ready_only = query["ready_only"] == ["true"]
        records = [
            {
                "record_id": "ready-1",
                "paper_id": "paper-1",
                "task": query["task"][0],
                "ml_ready": True,
                "blockers": [],
                "database_write_authority": "none",
            }
        ]
        if not ready_only:
            records.append(
                {
                    "record_id": "blocked-1",
                    "paper_id": "paper-2",
                    "task": query["task"][0],
                    "ml_ready": False,
                    "blockers": ["missing_descriptor"],
                    "descriptor_blockers": ["missing_descriptor"],
                    "structure_blockers": [],
                    "database_write_authority": "none",
                }
            )
        return {
            "manifest": {
                "task": query["task"][0],
                "ready_only": ready_only,
                "record_count": len(records),
                "returned_sample_count": 1 if ready_only else 2,
            },
            "records": records,
            "sample_records": [
                {
                    "sample_id": "sample-1",
                    "paper_id": "paper-1",
                    "task": query["task"][0],
                    "catalyst_sample_id": "cat-1",
                    "catalyst_name": "Fe-N-C",
                    "active_site_instance_key": "site-1",
                    "task_record_ids": ["ready-1"],
                    "source_record_ids": ["ready-1"],
                    "task_wide_labels": {"adsorption_energy_eV": -1.2},
                    "wide_properties": {"adsorption_energy_li2s_ev": -1.2},
                    "property_group_counts": {"adsorbate_properties": 1},
                    "ml_ready": True,
                    "blockers": [],
                },
                *(
                    []
                    if ready_only
                    else [
                        {
                            "sample_id": "sample-2",
                            "paper_id": "paper-2",
                            "task": query["task"][0],
                            "catalyst_sample_id": "cat-2",
                            "catalyst_name": "Co-N-C",
                            "active_site_instance_key": "site-2",
                            "task_record_ids": ["blocked-1"],
                            "source_record_ids": ["blocked-1"],
                            "task_wide_labels": {},
                            "wide_properties": {},
                            "property_group_counts": {"adsorbate_properties": 1},
                            "ml_ready": False,
                            "blockers": ["missing_descriptor"],
                        }
                    ]
                ),
            ],
        }

    monkeypatch.setattr(tool, "_fetch_json", fake_fetch_json)

    report = tool.build_api_report(
        api_base_url="http://127.0.0.1:8000/",
        context_key="li_s_sac_dac",
        library_name="锂硫双原子",
        tasks=("adsorption_energy",),
        example_limit=3,
        connect_timeout=6,
    )

    assert json.loads(json.dumps(report, ensure_ascii=False)) == report
    assert report["execution_mode"] == "local_api"
    assert report["api_base_url"] == "http://127.0.0.1:8000"
    assert report["read_only"] is True
    assert report["database_write_authority"] == "none"
    assert report["submit_endpoint_called"] is False
    assert report["extraction_apply_called"] is False
    assert report["active_database"]["db_url_masked"] == "local_backend_api"
    assert report["tasks"][0]["ready_record_count"] == 1
    assert report["tasks"][0]["diagnostic_record_count"] == 2
    assert report["tasks"][0]["blocked_record_count"] == 1
    assert report["tasks"][0]["ready_sample_record_count"] == 1
    assert report["tasks"][0]["diagnostic_sample_record_count"] == 2
    assert report["tasks"][0]["blocked_sample_record_count"] == 1
    assert report["tasks"][0]["blocker_counts_from_records"] == {"missing_descriptor": 1}
    assert report["tasks"][0]["sample_blocker_counts_from_records"] == {"missing_descriptor": 1}
    assert report["tasks"][0]["ready_sample_examples"][0]["sample_id"] == "sample-1"
    assert report["tasks"][0]["ready_sample_examples"][0]["wide_properties"] == {
        "adsorption_energy_li2s_ev": -1.2
    }
    assert len(requested_urls) == 2
    assert all(parse_qs(urlparse(url).query)["library_name"] == ["锂硫双原子"] for url in requested_urls)
