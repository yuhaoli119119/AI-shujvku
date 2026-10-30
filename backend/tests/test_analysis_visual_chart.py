import json
from types import SimpleNamespace

import pytest

from app.api import analysis_readonly as api
from app.services.analysis_visual_chart import (
    compact_correlation_payload,
    read_cached_svg,
    render_correlation_svg,
    write_cached_svg,
)


pytestmark = pytest.mark.no_test_database


def _payload():
    point = {
        "analysis_entity_id": "entity-1",
        "pair_observation_id": "pair-1",
        "catalyst_entity_id": "catalyst-1",
        "catalyst_name": "Ni-Mo <DAC>",
        "catalyst_sample_id": None,
        "paper": {"paper_id": "paper-1", "paper_code": "B026", "title": "Evidence", "doi": None, "year": 2026},
        "x": {
            "value": -3.6,
            "unit": "eV",
            "source_record_ids": ["x-source"],
            "candidates": [{"value": -3.6, "unit": "eV", "source_record_id": "x-source", "evidence_payload": {"blob": "x" * 1000}}],
        },
        "y": {
            "value": 0.569,
            "unit": "eV",
            "source_record_ids": ["y-source"],
            "candidates": [{"value": 0.569, "unit": "eV", "pathway": "Li2S decomposition", "source_record_id": "y-source", "selected_for_regression": True}],
        },
        "x_source_record_ids": ["x-source"],
        "y_source_record_ids": ["y-source"],
        "paper_code": "B026",
        "semantic_context": {"large": "y" * 1000},
        "group_id": "group-1",
    }
    return {
        "x_field": "li2s_adsorption_energy",
        "y_field": "li2s_dissociation_barrier",
        "x": {"key": "li2s_adsorption_energy", "label": "Li2S adsorption", "unit": "eV"},
        "y": {"key": "li2s_dissociation_barrier", "label": "Li2S barrier", "unit": "eV"},
        "points": [point],
        "record_sources": {"x-source": {"blob": "r" * 1000}},
        "excluded_details": [{"large": "e" * 1000}],
        "distribution": {"x": [{"value": -3.6}], "y": [{"value": 0.569}]},
        "comparability_groups": [{"group_id": "group-1", "label": "B026 · condition 1", "n": 1, "context": {"large": "c" * 1000}, "statistics": {"slope": 1, "intercept": 4, "pearson": 1}}],
        "statistics": {"slope": 1, "intercept": 4, "pearson_r": 1},
        "ready": True,
    }


def test_compact_correlation_keeps_plot_and_candidate_fields():
    compact = compact_correlation_payload(_payload())
    assert compact["detail_mode"] == "compact"
    assert "record_sources" not in compact
    assert "excluded_details" not in compact
    assert compact["distribution"] == {"x": [{"value": -3.6}], "y": [{"value": 0.569}]}
    assert "semantic_context" not in compact["points"][0]
    assert compact["points"][0]["x"]["candidates"] == [
        {"value": -3.6, "unit": "eV", "source_record_id": "x-source"}
    ]
    assert compact["points"][0]["y"]["candidates"][0]["selected_for_regression"] is True
    assert compact["candidate_counts"] == {"x": 1, "y": 1}
    assert len(json.dumps(compact, ensure_ascii=False).encode()) < len(json.dumps(_payload(), ensure_ascii=False).encode()) / 2


def test_static_svg_is_deterministic_escaped_and_honors_cache(tmp_path):
    payload = _payload()
    svg = render_correlation_svg(payload, x_field=payload["x_field"], y_field=payload["y_field"])
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "Li2S adsorption" in svg and "Li2S barrier" in svg
    assert "Ni-Mo &lt;DAC&gt;" in svg and "<DAC>" not in svg
    assert "B026 · condition 1" in svg
    assert svg == render_correlation_svg(payload, x_field=payload["x_field"], y_field=payload["y_field"])

    cache_dir = tmp_path / "visual-chart-cache"
    write_cached_svg(cache_dir, "known-key", svg)
    assert read_cached_svg(cache_dir, "known-key") == svg
    assert (cache_dir.stat().st_mode & 0o777) == 0o700
    assert ((cache_dir / "known-key.svg").stat().st_mode & 0o777) == 0o600
    assert read_cached_svg(cache_dir, "missing") is None
    (cache_dir / "corrupt.svg").write_text("<svg><path>", encoding="utf-8")
    assert read_cached_svg(cache_dir, "corrupt") is None


def test_compact_http_view_uses_existing_correlation_cache(monkeypatch):
    payload = _payload()
    calls = []

    class FakeService:
        def cached_output(self, name, key, build):
            calls.append((name, key))
            return build()

        def correlation(self, **kwargs):
            return payload

    monkeypatch.setattr(api, "service", lambda args, session: FakeService())
    request = SimpleNamespace(headers={"accept-encoding": ""})
    response = api.correlation(
        request=request,
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
        view="compact",
        args={"source": "rebuild", "library_name": "锂硫双原子"},
        session=object(),
    )
    body = json.loads(response.body)
    assert body["detail_mode"] == "compact"
    assert "record_sources" not in body
    full_response = api.correlation(
        request=request,
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
        view="full",
        args={"source": "rebuild", "library_name": "锂硫双原子"},
        session=object(),
    )
    assert "record_sources" in json.loads(full_response.body)
    assert calls == [
        ("correlation", {"x": "li2s_adsorption_energy", "y": "li2s_dissociation_barrier", "min_n": 3}),
        ("correlation", {"x": "li2s_adsorption_energy", "y": "li2s_dissociation_barrier", "min_n": 3}),
    ]


def test_static_svg_http_contract_and_etag_revalidation(monkeypatch, tmp_path):
    payload = _payload()

    class FakeService:
        def cached_output(self, name, key, build):
            return build()

        def correlation(self, **kwargs):
            return payload

    class Bind:
        dialect = SimpleNamespace(name="postgresql")

    class Session:
        def get_bind(self):
            return Bind()

    monkeypatch.setattr(api, "service", lambda args, session: FakeService())
    monkeypatch.setattr(api, "_content_revision", lambda session: ("revision-1",))
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(storage_root=tmp_path))
    args = {
        "source": "rebuild",
        "library_name": "锂硫双原子",
        "filters": {"data_type": "dft"},
        "include_estimated": True,
        "include_unlabelled": True,
    }
    request = SimpleNamespace(headers={})
    response = api.correlation_svg(
        request=request,
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
        args=args,
        session=Session(),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"].startswith("private, no-cache")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.body.startswith(b"<svg")

    cached = api.correlation_svg(
        request=SimpleNamespace(headers={"if-none-match": response.headers["etag"]}),
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
        args=args,
        session=Session(),
    )
    assert cached.status_code == 304
    assert cached.body == b""
    weak = api.correlation_svg(
        request=SimpleNamespace(headers={"if-none-match": "W/" + response.headers["etag"]}),
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
        args=args,
        session=Session(),
    )
    assert weak.status_code == 304


def test_static_svg_revision_change_retries_with_fresh_service(monkeypatch, tmp_path):
    stale = _payload()
    stale["points"][0]["catalyst_name"] = "Stale projection"
    fresh = _payload()
    fresh["points"][0]["catalyst_name"] = "Fresh projection"
    instances = []

    class FakeService:
        def __init__(self, payload):
            self.payload = payload

        def cached_output(self, name, key, build):
            return self.payload

        def correlation(self, **kwargs):
            return self.payload

    def fake_service(args, session):
        payload = stale if not instances else fresh
        service = FakeService(payload)
        instances.append(service)
        return service

    class Bind:
        dialect = SimpleNamespace(name="postgresql")

    class Session:
        def get_bind(self):
            return Bind()

    revisions = iter((("revision-1",), ("revision-2",), ("revision-2",)))
    monkeypatch.setattr(api, "service", fake_service)
    monkeypatch.setattr(api, "_content_revision", lambda session: next(revisions))
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(storage_root=tmp_path))
    response = api.correlation_svg(
        request=SimpleNamespace(headers={}),
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
        args={
            "source": "rebuild",
            "library_name": "锂硫双原子",
            "filters": {"data_type": "dft"},
            "include_estimated": True,
            "include_unlabelled": True,
        },
        session=Session(),
    )
    assert response.status_code == 200
    assert b"Fresh projection" in response.body
    assert b"Stale projection" not in response.body
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert len(list((tmp_path / ".visual-render-cache").glob("*.svg"))) == 1
