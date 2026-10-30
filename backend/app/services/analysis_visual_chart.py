"""Compact and server-rendered payloads for the analysis scatter plot."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


GROUP_COLORS = ("#6d45de", "#0e9b94", "#df8b28", "#df5785", "#397dde", "#819b30")
CHART_RENDERER_VERSION = "server-svg-v1"
CHART_CACHE_FILES = 128
CHART_CACHE_BYTES = 64 * 1024 * 1024


def _axis_value(axis: Any) -> float | None:
    raw = axis.get("value") if isinstance(axis, dict) else axis
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _compact_candidate(candidate: Any) -> Any:
    if not isinstance(candidate, dict):
        return candidate
    keys = (
        "value",
        "unit",
        "pathway",
        "label",
        "reaction_step",
        "source_record_id",
        "source_record_ids",
        "selected_for_summary",
        "selected_for_regression",
    )
    return {key: candidate[key] for key in keys if key in candidate}


def _compact_axis(axis: Any) -> Any:
    if not isinstance(axis, dict):
        return axis
    result = {key: axis[key] for key in ("value", "unit", "source_record_ids", "selection_reason") if key in axis}
    if isinstance(axis.get("candidates"), list):
        result["candidates"] = [_compact_candidate(item) for item in axis["candidates"]]
    return result


def _compact_point(point: dict[str, Any]) -> dict[str, Any]:
    paper = point.get("paper") if isinstance(point.get("paper"), dict) else {}
    return {
        "analysis_entity_id": point.get("analysis_entity_id"),
        "pair_observation_id": point.get("pair_observation_id"),
        "catalyst_entity_id": point.get("catalyst_entity_id"),
        "catalyst_name": point.get("catalyst_name"),
        "catalyst_sample_id": point.get("catalyst_sample_id"),
        "paper": {key: paper[key] for key in ("paper_id", "paper_code", "title", "doi", "journal", "year") if key in paper},
        "x": _compact_axis(point.get("x")),
        "y": _compact_axis(point.get("y")),
        "x_source_record_ids": point.get("x_source_record_ids") or [],
        "y_source_record_ids": point.get("y_source_record_ids") or [],
        "pairing_rule": point.get("pairing_rule"),
        "source_family": point.get("source_family"),
        "assessment_status": point.get("assessment_status"),
        "group_id": point.get("group_id"),
    }


def compact_correlation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop source-record blobs used only by full audit/detail consumers.

    The first paint needs pair coordinates, compact statistics, comparable-group
    labels and the small candidate list rendered in the pair table. The large
    ``record_sources`` and ``excluded_details`` projections remain available on
    the unchanged full endpoint for audit drill-down.
    """
    omitted = {"points", "record_sources", "excluded_details"}
    compact = {key: value for key, value in payload.items() if key not in omitted}
    compact["points"] = [_compact_point(point) for point in payload.get("points", [])]
    groups = []
    for group in payload.get("comparability_groups", []):
        groups.append({key: group[key] for key in ("group_id", "label", "n", "statistics") if key in group})
    compact["comparability_groups"] = groups
    compact["candidate_counts"] = {
        axis: sum(len((point.get(axis) or {}).get("candidates") or []) for point in payload.get("points", []))
        for axis in ("x", "y")
    }
    compact["detail_mode"] = "compact"
    return compact


def _nice_scale(values: Iterable[float], target_count: int = 5) -> tuple[float, float, list[float]]:
    values = list(values)
    low, high = min(values), max(values)
    if math.isclose(low, high):
        low -= 0.5
        high += 0.5
    span = high - low
    low -= span * 0.06
    high += span * 0.06
    raw_step = (high - low) / target_count
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    step = (1 if normalized < 1.5 else 2 if normalized < 3 else 5 if normalized < 7 else 10) * magnitude
    start = math.floor(low / step) * step
    end = math.ceil(high / step) * step
    ticks = []
    value = start
    while value <= end + step * 0.001:
        ticks.append(round(value, 12))
        value += step
    return start, end, ticks


def _tick(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000 or abs(value) < 0.01:
        return f"{value:.1e}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def render_correlation_svg(payload: dict[str, Any], *, x_field: str, y_field: str) -> str:
    """Render a deterministic, dependency-free SVG scatter plot."""
    width, height = 960, 640
    left, right, top, bottom = 92, 925, 42, 450
    plot_width, plot_height = right - left, bottom - top
    points = [
        point
        for point in payload.get("points", [])
        if _axis_value(point.get("x")) is not None and _axis_value(point.get("y")) is not None
    ]
    x_values = [_axis_value(point.get("x")) for point in points]
    y_values = [_axis_value(point.get("y")) for point in points]
    x_definition = payload.get("x") if isinstance(payload.get("x"), dict) else {}
    y_definition = payload.get("y") if isinstance(payload.get("y"), dict) else {}
    x_label = x_definition.get("label") or x_definition.get("label_zh") or x_field
    y_label = y_definition.get("label") or y_definition.get("label_zh") or y_field
    x_unit = x_definition.get("unit") or ""
    y_unit = y_definition.get("unit") or ""
    x_title = f"{x_label} / {x_unit}" if x_unit else str(x_label)
    y_title = f"{y_label} / {y_unit}" if y_unit else str(y_label)

    result = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(str(x_label))} 与 {html.escape(str(y_label))} 静态散点图">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<g font-family="Noto Sans CJK SC, Noto Sans SC, Microsoft YaHei, sans-serif" fill="#263445">',
    ]
    if not points:
        result.append(f'<text x="480" y="250" text-anchor="middle" font-size="18">{html.escape(str(payload.get("insufficient_reason") or "暂无可绘制的有效配对"))}</text>')
        result.append("</g></svg>")
        return "".join(result)

    x_min, x_max, x_ticks = _nice_scale(x_values)
    y_min, y_max, y_ticks = _nice_scale(y_values)

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min or 1) * plot_width

    def sy(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min or 1) * plot_height

    for tick in y_ticks:
        y = sy(tick)
        result.append(f'<line x1="{left}" x2="{right}" y1="{y:.2f}" y2="{y:.2f}" stroke="#e7ebf0" stroke-width="1"/>')
        result.append(f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" font-size="12" fill="#64748b">{_tick(tick)}</text>')
    for tick in x_ticks:
        x = sx(tick)
        result.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{top}" y2="{bottom}" stroke="#e7ebf0" stroke-width="1"/>')
        result.append(f'<text x="{x:.2f}" y="{bottom + 21}" text-anchor="middle" font-size="12" fill="#64748b">{_tick(tick)}</text>')
    result.append(f'<line x1="{left}" x2="{left}" y1="{top}" y2="{bottom}" stroke="#64748b" stroke-width="1.2"/>')
    result.append(f'<line x1="{left}" x2="{right}" y1="{bottom}" y2="{bottom}" stroke="#64748b" stroke-width="1.2"/>')
    result.append(f'<text x="{(left + right) / 2:.2f}" y="{bottom + 43}" text-anchor="middle" font-size="14">{html.escape(str(x_title))}</text>')
    result.append(f'<text x="23" y="{(top + bottom) / 2:.2f}" text-anchor="middle" font-size="14" transform="rotate(-90 23 {(top + bottom) / 2:.2f})">{html.escape(str(y_title))}</text>')

    groups = payload.get("comparability_groups") if isinstance(payload.get("comparability_groups"), list) else []
    result.append(f'<clipPath id="plot-clip"><rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}"/></clipPath>')
    group_index = {str(group.get("group_id")): index for index, group in enumerate(groups)}
    group_points: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        group_points.setdefault(str(point.get("group_id") or ""), []).append(point)
    for group in groups:
        statistics = group.get("statistics") if isinstance(group.get("statistics"), dict) else {}
        slope, intercept = statistics.get("slope"), statistics.get("intercept")
        if slope is None or intercept is None:
            continue
        members = group_points.get(str(group.get("group_id"))) or []
        if not members:
            continue
        xs = [_axis_value(item.get("x")) for item in members]
        lo, hi = min(xs), max(xs)
        color = GROUP_COLORS[group_index.get(str(group.get("group_id")), 0) % len(GROUP_COLORS)]
        result.append(
            '<g clip-path="url(#plot-clip)">'
            f'<line x1="{sx(lo):.2f}" y1="{sy(float(slope) * lo + float(intercept)):.2f}" '
            f'x2="{sx(hi):.2f}" y2="{sy(float(slope) * hi + float(intercept)):.2f}" '
            f'stroke="{color}" stroke-width="2.3" stroke-linecap="round"/></g>'
        )

    for point in points:
        x = sx(float(_axis_value(point.get("x"))))
        y = sy(float(_axis_value(point.get("y"))))
        color = GROUP_COLORS[group_index.get(str(point.get("group_id") or ""), 0) % len(GROUP_COLORS)]
        title = html.escape(str(point.get("catalyst_name") or "催化剂"))
        xv, yv = _axis_value(point.get("x")), _axis_value(point.get("y"))
        result.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="10" fill="{color}" fill-opacity=".13"/>'
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5.8" fill="{color}" stroke="#ffffff" stroke-width="1.6">'
            f'<title>{title}: {xv:.4g}, {yv:.4g}</title></circle>'
        )

    if groups:
        legend_y = 525
        visible_groups = groups[:9] if len(groups) <= 9 else groups[:8]
        for index, group in enumerate(visible_groups):
            column, row_number = index % 3, index // 3
            x = 92 + column * 280
            y = legend_y + row_number * 25
            color = GROUP_COLORS[index % len(GROUP_COLORS)]
            label = str(group.get("label") or f"可比组 {index + 1}")
            label = label if len(label) <= 30 else label[:29] + "…"
            statistics = group.get("statistics") if isinstance(group.get("statistics"), dict) else {}
            pearson = statistics.get("pearson_r", statistics.get("pearson"))
            suffix = f" · n={group.get('n')}" + (f" · r={float(pearson):.2f}" if pearson is not None else "")
            result.append(f'<circle cx="{x}" cy="{y - 4}" r="5" fill="{color}"/>')
            result.append(f'<text x="{x + 12}" y="{y}" font-size="11.5">{html.escape(label + suffix)}</text>')
        if len(groups) > 9:
            summary_index = 8
            summary_x = 92 + (summary_index % 3) * 280
            summary_y = legend_y + (summary_index // 3) * 25
            result.append(f'<text x="{summary_x}" y="{summary_y}" font-size="11.5" fill="#64748b">另有 {len(groups) - 8} 个可比组</text>')
    result.append("</g></svg>")
    return "".join(result)


def chart_cache_key(parts: dict[str, Any]) -> str:
    parts = {**parts, "renderer_version": CHART_RENDERER_VERSION}
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_cached_svg(cache_dir: Path, key: str) -> str | None:
    path = cache_dir / f"{key}.svg"
    try:
        if not path.is_file():
            return None
        svg = path.read_text(encoding="utf-8")
        return svg if svg.startswith("<svg") and svg.endswith("</svg>") else None
    except OSError:
        return None


def write_cached_svg(cache_dir: Path, key: str, svg: str) -> None:
    temporary = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.chmod(0o700)
        path = cache_dir / f"{key}.svg"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_dir, prefix=f"{key}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(svg)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        _prune_cache(cache_dir)
    except OSError:
        # A read-only or full cache volume must not prevent returning the chart.
        return
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _prune_cache(cache_dir: Path) -> None:
    files = sorted(
        (path for path in cache_dir.glob("*.svg") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    for index, path in enumerate(files):
        try:
            total += path.stat().st_size
            if index >= CHART_CACHE_FILES or total > CHART_CACHE_BYTES:
                path.unlink(missing_ok=True)
        except OSError:
            continue
