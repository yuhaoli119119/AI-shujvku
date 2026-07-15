from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample, DFTResult, DFTSetting, Paper
from app.normalizers.chemistry_normalizer import canonicalize_adsorbate, get_property_taxonomy
from app.services.dft_export_service import build_dft_ml_dataset
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results


SCHEMA_VERSION = "dft_catalyst_correlation_v1"
_ADSORBATES = ("S8", "Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S")
_RDS_MARKERS = re.compile(r"rds|rate[- ]determining|rate determining|limiting step|决速步骤", re.I)
_LI2S_DISSOCIATION_MARKERS = re.compile(r"li\s*2\s*s|lithium\s+sulfide|锂硫化物", re.I)
_DISSOCIATION_MARKERS = re.compile(r"dissociat|decompos|解离|分解", re.I)


def _field(
    key: str,
    label_zh: str,
    unit: str | None,
    value_type: str,
    selection_rule: str,
    *,
    category: str = "numeric",
) -> dict[str, Any]:
    return {
        "key": key,
        "field": key,
        "label_zh": label_zh,
        "label": label_zh,
        "unit": unit,
        "type": value_type,
        "category": category,
        "selection_rule": selection_rule,
        "selection_policy": selection_rule,
    }


_FIELD_REGISTRY: tuple[dict[str, Any], ...] = (
    _field("catalyst_name", "催化剂名称", None, "string", "来自明确绑定的 catalyst_sample；不做名称合并", category="metadata"),
    _field("catalyst_sample_id", "催化剂样品 ID", None, "uuid", "必须来自 DFTResult.catalyst_sample_id；缺失即排除", category="metadata"),
    _field("paper_id", "论文 ID", None, "uuid", "来自 DFT 来源记录所属论文", category="metadata"),
    _field("paper_code", "论文编号", None, "string", "来自 Paper.paper_code；不以 UUID 替代", category="metadata"),
    _field("doi", "DOI", None, "string", "来自 Paper.doi", category="metadata"),
    _field("catalyst_type", "催化剂类型", None, "string", "来自 catalyst_sample；上下文不一致时不猜测", category="metadata"),
    _field("metal_centers", "金属中心", None, "array[string]", "来自 catalyst_sample；保留原样并排序展示", category="metadata"),
    _field("coordination", "配位环境", None, "string", "来自 catalyst_sample", category="metadata"),
    _field("support", "载体", None, "string", "来自 catalyst_sample", category="metadata"),
    _field("functional", "交换关联泛函", None, "string", "只使用结果明确关联的 DFT setting；多设置无法唯一关联时不选", category="metadata"),
    *(
        _field(
            f"{adsorbate.lower()}_adsorption_energy",
            f"{adsorbate} 吸附能",
            "eV",
            "number",
            f"canonical_adsorbate={adsorbate} 且 canonical property=adsorption_energy；同一语义上下文冲突留空",
        )
        for adsorbate in _ADSORBATES
    ),
    _field(
        "li2s_dissociation_barrier",
        "Li2S 解离/分解能垒",
        "eV",
        "number",
        "专用 li2s_decomposition_barrier，或 generic reaction_barrier 且证据明确为 Li2S dissociation/decomposition；同一上下文取可比路径最大值",
    ),
    _field(
        "li2s_bader_charge_transfer",
        "Li2S Bader 电荷转移",
        "e",
        "number",
        "只接受 Li2S 的 bader_charge_transfer、aggregate charge transfer 或语义等价记录；不接受其他吸附物或原子 Bader 电荷",
    ),
    _field("li1_s_bond_length", "Li1-S 键长", "Å", "number", "只接受 atom_pair=Li1-S 的 bond length；不混入其他键对"),
    _field("li2_s_bond_length", "Li2-S 键长", "Å", "number", "只接受 atom_pair=Li2-S 的 bond length；不混入其他键对"),
    _field("li_s_bond_max", "Li-S 最大键长", "Å", "number", "仅由已分别选中的 Li1-S 与 Li2-S 取最大值；不得直接读取其他键长"),
    _field("d_band_center", "d 带中心", "eV", "number", "canonical property=d_band_center；上下文不可比时留空"),
    _field("rds_delta_g", "RDS ΔG", "eV", "number", "canonical property=gibbs_free_energy_change 且 reaction/evidence 明确为 RDS；不接受总体自由能"),
)
FIELD_REGISTRY = {item["field"]: item for item in _FIELD_REGISTRY}
FIELD_ALIASES = {"li2s_decomposition_barrier": "li2s_dissociation_barrier"}


@dataclass
class _ReadyRow:
    row: DFTResult
    paper: Paper
    record: dict[str, Any]
    catalyst: CatalystSample


@dataclass
class _Candidate:
    ready: _ReadyRow
    value: float
    unit: str | None
    context: dict[str, Any]
    context_key: str
    source_record_ids: tuple[str, ...] = ()

    @property
    def record_id(self) -> str:
        return str(self.ready.row.id)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return self.source_record_ids or (self.record_id,)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _text(value).casefold()).strip("_")


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return _text(value)


def _identity_subject(row: DFTResult) -> dict[str, Any]:
    payload = row.identity_payload if isinstance(row.identity_payload, dict) else {}
    subject = payload.get("subject")
    return subject if isinstance(subject, dict) else {}


def _row_text(row: DFTResult) -> str:
    return " ".join(
        value
        for value in (
            row.property_type,
            row.adsorbate,
            row.reaction_step,
            row.evidence_text,
            _json_text(row.evidence_payload),
            _json_text(row.identity_payload),
        )
        if _text(value)
    ).casefold()


def _canonical_property(row: DFTResult) -> str:
    taxonomy = get_property_taxonomy(row.property_type)
    return _norm(taxonomy.get("canonical_property_type") or row.property_type)


def _property_subtype(row: DFTResult) -> str:
    taxonomy = get_property_taxonomy(row.property_type)
    return _norm(taxonomy.get("property_subtype") or "")


def _adsorbate(row: DFTResult) -> str | None:
    return canonicalize_adsorbate(row.adsorbate)


def _atom_pair(row: DFTResult) -> str:
    subject = _identity_subject(row)
    payload = row.identity_payload if isinstance(row.identity_payload, dict) else {}
    atom_pair = payload.get("atom_pair") if isinstance(payload.get("atom_pair"), dict) else {}
    return _norm(subject.get("canonical_atom_pair") or atom_pair.get("canonical"))


def _is_li2s_barrier(row: DFTResult) -> bool:
    subtype = _property_subtype(row)
    raw = _norm(row.property_type)
    text = _row_text(row)
    dedicated = subtype == "li2s_decomposition_barrier" or raw in {
        "li2s_decomposition_barrier",
        "li2s_dissociation_barrier",
    }
    generic = _canonical_property(row) in {"reaction_barrier", "activation_energy"}
    return dedicated or (generic and bool(_LI2S_DISSOCIATION_MARKERS.search(text)) and bool(_DISSOCIATION_MARKERS.search(text)))


def _is_li2s_charge_transfer(row: DFTResult) -> bool:
    if _adsorbate(row) != "Li2S":
        return False
    raw = _norm(row.property_type)
    payload = _json_text(row.identity_payload).casefold()
    return (
        "bader_charge_transfer" in raw
        or "aggregate_charge_transfer" in raw
        or raw in {"charge_transfer", "aggregate_charge"}
        or "aggregate_support_adsorbate_charge_transfer" in payload
        or (raw == "bader_charge" and "charge transfer" in _row_text(row))
    )


def _bond_atom_pair(row: DFTResult) -> str:
    pair = _atom_pair(row)
    text = _row_text(row).replace("-", "_")
    if pair in {"li1_s", "s_li1", "li_1_s", "s_li_1"} or re.search(r"li\s*_?1\s*_?s", text):
        return "li1_s"
    if pair in {"li2_s", "s_li2", "li_2_s", "s_li_2"} or re.search(r"li\s*_?2\s*_?s", text):
        return "li2_s"
    return ""


def _is_bond(row: DFTResult) -> bool:
    raw = _norm(row.property_type)
    subtype = _property_subtype(row)
    return "bond_length" in raw or subtype in {"li_s_bond_length", "bond_length_li_s"} or "bond length" in _row_text(row)


def _is_rds(row: DFTResult) -> bool:
    raw = _row_text(row)
    return _canonical_property(row) == "gibbs_free_energy_change" and bool(_RDS_MARKERS.search(raw)) and not bool(
        re.search(r"overall|全局|总体", raw, re.I)
    )


def _setting_payload(record: dict[str, Any]) -> dict[str, Any]:
    setting = record.get("linked_dft_setting")
    return setting if isinstance(setting, dict) else {}


def _context(ready: _ReadyRow) -> dict[str, Any]:
    row = ready.row
    subject = _identity_subject(row)
    property_context = subject.get("property_context") if isinstance(subject.get("property_context"), dict) else {}
    setting = _setting_payload(ready.record)
    context: dict[str, Any] = {
        "dft_setting_id": setting.get("dft_setting_id"),
        "functional": setting.get("functional") or property_context.get("functional"),
        "dispersion_correction": setting.get("dispersion_correction") or property_context.get("dispersion_correction"),
        "configuration": property_context.get("configuration"),
        "pathway": property_context.get("pathway"),
        "initial_state": property_context.get("initial_state"),
        "transition_state": property_context.get("transition_state"),
        "final_state": property_context.get("final_state"),
        "state_context": subject.get("state_context"),
        "active_site_instance_key": subject.get("active_site_instance_key"),
        "canonical_atom_pair": subject.get("canonical_atom_pair"),
        "site_label": subject.get("site_label"),
        "reaction_step": row.reaction_step or subject.get("reaction_step"),
        "facet": property_context.get("facet"),
        "coverage": property_context.get("coverage"),
        "termination": property_context.get("termination"),
    }
    return {key: value for key, value in context.items() if value not in (None, "", [])}


def _context_key(context: dict[str, Any]) -> str:
    return json.dumps({key: _norm(value) if isinstance(value, str) else value for key, value in sorted(context.items())}, sort_keys=True, ensure_ascii=False)


def _contexts_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in set(left) & set(right):
        if _norm(left[key]) != _norm(right[key]):
            return False
    return True


def _numeric_value(record: dict[str, Any], row: DFTResult, field: str) -> tuple[float, str | None] | None:
    target = record.get("target") if isinstance(record.get("target"), dict) else {}
    value = target.get("normalized_value")
    if value is None:
        value = row.value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    unit = target.get("normalized_unit") or row.unit
    if field in {"li1_s_bond_length", "li2_s_bond_length"}:
        unit_key = _norm(_text(unit).casefold().replace("å", "a"))
        if unit_key in {"nm", "nanometer", "nanometers"}:
            number *= 10.0
        elif unit_key not in {"a", "angstrom", "angstroms"}:
            return None
        unit = "Å"
    return number, unit


def _field_matches(field: str, ready: _ReadyRow) -> bool:
    row = ready.row
    prop = _canonical_property(row)
    adsorbate = _adsorbate(row)
    if field.endswith("_adsorption_energy"):
        return prop == "adsorption_energy" and adsorbate == next(item for item in _ADSORBATES if item.lower() == field.removesuffix("_adsorption_energy"))
    if field == "li2s_dissociation_barrier":
        return _is_li2s_barrier(row)
    if field == "li2s_bader_charge_transfer":
        return _is_li2s_charge_transfer(row)
    if field in {"li1_s_bond_length", "li2_s_bond_length"}:
        return _is_bond(row) and _bond_atom_pair(row) == field.removesuffix("_bond_length")
    if field == "d_band_center":
        return prop == "d_band_center"
    if field == "rds_delta_g":
        return _is_rds(row)
    return False


def _candidate_groups(field: str, rows: list[_ReadyRow]) -> list[list[_Candidate]]:
    if field == "li_s_bond_max":
        left = _candidate_groups("li1_s_bond_length", rows)
        right = _candidate_groups("li2_s_bond_length", rows)
        groups: list[list[_Candidate]] = []
        for left_group in left:
            for right_group in right:
                left_context = {key: value for key, value in left_group[0].context.items() if key != "canonical_atom_pair"} if left_group else {}
                right_context = {key: value for key, value in right_group[0].context.items() if key != "canonical_atom_pair"} if right_group else {}
                if not left_group or not right_group or not _contexts_compatible(left_context, right_context):
                    continue
                values = [max(left_group[0].value, right_group[0].value)]
                context = {**left_context, **right_context}
                representative = _Candidate(
                    left_group[0].ready,
                    values[0],
                    "Å",
                    context,
                    _context_key(context),
                    source_record_ids=tuple(sorted(set(left_group[0].source_ids + right_group[0].source_ids))),
                )
                groups.append([representative])
        return groups

    grouped: dict[str, list[_Candidate]] = defaultdict(list)
    for ready in rows:
        if not _field_matches(field, ready):
            continue
        normalized = _numeric_value(ready.record, ready.row, field)
        if normalized is None:
            continue
        value, unit = normalized
        context = _context(ready)
        if field == "li2s_dissociation_barrier":
            # Different Li2S dissociation paths are the candidates for the
            # approved max rule; calculation context remains comparable.
            context.pop("reaction_step", None)
        grouped[_context_key(context)].append(
            _Candidate(ready, value, FIELD_REGISTRY[field]["unit"] or unit, context, _context_key(context))
        )
    return list(grouped.values())


def _candidate_payload(candidate: _Candidate) -> dict[str, Any]:
    subject = _identity_subject(candidate.ready.row)
    property_context = subject.get("property_context") if isinstance(subject.get("property_context"), dict) else {}
    pathway = property_context.get("pathway") or candidate.ready.row.reaction_step
    return {
        "value": candidate.value,
        "unit": candidate.unit,
        "property_type": candidate.ready.row.property_type,
        "reaction_step": candidate.ready.row.reaction_step,
        "pathway": pathway,
        "source_record_id": candidate.record_id,
        "source_record_ids": list(candidate.source_ids),
        "paper_id": str(candidate.ready.paper.id),
        "paper_code": candidate.ready.paper.paper_code,
        "paper": _paper_payload(candidate.ready.paper),
        "context": candidate.context,
        "selected_for_summary": False,
        "selected_for_regression": False,
    }


def _resolve_group(field: str, group: list[_Candidate]) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    if not group:
        return None, "missing_value", []
    values = [item.value for item in group]
    candidates = [_candidate_payload(item) for item in group]
    if field == "li2s_dissociation_barrier":
        maximum = max(values)
        selected_ids = [
            source_id
            for item in group
            if math.isclose(item.value, maximum, rel_tol=1e-12, abs_tol=1e-12)
            for source_id in item.source_ids
        ]
        selected_id_set = set(selected_ids)
        for candidate, item in zip(candidates, group):
            selected = bool(selected_id_set.intersection(item.source_ids))
            candidate["selected_for_summary"] = selected
            candidate["selected_for_regression"] = selected
        return {
            "value": maximum,
            "unit": group[0].unit,
            "source_record_ids": sorted(selected_ids),
            "selection_reason": "maximum_of_comparable_li2s_paths",
            "context": group[0].context,
            "candidates": candidates,
        }, None, candidates
    if all(math.isclose(value, values[0], rel_tol=1e-12, abs_tol=1e-12) for value in values[1:]):
        reason = "unique_comparable_value" if len(group) == 1 else "deduplicated_equal_values"
        return {
            "value": values[0],
            "unit": group[0].unit,
            "source_record_ids": sorted({source_id for item in group for source_id in item.source_ids}),
            "selection_reason": reason,
            "context": group[0].context,
            "candidates": candidates,
        }, None, candidates
    return None, "conflicting_values", candidates


def _resolve_field(field: str, groups: list[list[_Candidate]]) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    if not groups:
        return None, "missing_value", []
    resolved: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    for group in groups:
        selected, reason, candidates = _resolve_group(field, group)
        all_candidates.extend(candidates)
        if selected is None:
            return None, reason, all_candidates
        resolved.append(selected)
    if len(resolved) != 1:
        return None, "incomparable_contexts", all_candidates
    return resolved[0], None, all_candidates


def _paper_payload(paper: Paper) -> dict[str, Any]:
    return {
        "paper_id": str(paper.id),
        "paper_code": paper.paper_code,
        "doi": paper.doi,
        "title": paper.title,
        "year": paper.year,
        "journal": paper.journal,
    }


def _catalyst_payload(catalyst: CatalystSample) -> dict[str, Any]:
    return {
        "catalyst_sample_id": str(catalyst.id),
        "catalyst_name": catalyst.name,
        "catalyst_type": catalyst.catalyst_type,
        "metal_centers": catalyst.metal_centers or [],
        "coordination": catalyst.coordination,
        "support": catalyst.support,
    }


def _rank(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for position in range(index, end):
            ranks[ordered[position][0]] = rank
        index = end
    return ranks


def _stats(points: list[dict[str, Any]]) -> dict[str, float | None]:
    x = [float(point["x"]["value"]) for point in points]
    y = [float(point["y"]["value"]) for point in points]
    n = len(x)
    if n < 2:
        return {"pearson": None, "spearman": None, "r_squared": None, "slope": None, "intercept": None}
    mx, my = sum(x) / n, sum(y) / n
    ssx = sum((value - mx) ** 2 for value in x)
    ssy = sum((value - my) ** 2 for value in y)
    covariance = sum((xv - mx) * (yv - my) for xv, yv in zip(x, y))
    if ssx == 0 or ssy == 0:
        pearson = None
        slope = None
        intercept = None
        r_squared = 0.0
    else:
        pearson = covariance / math.sqrt(ssx * ssy)
        slope = covariance / ssx
        intercept = my - slope * mx
        residual = sum((yv - (slope * xv + intercept)) ** 2 for xv, yv in zip(x, y))
        r_squared = max(0.0, min(1.0, 1.0 - residual / ssy))
    rx, ry = _rank(x), _rank(y)
    rmx, rmy = sum(rx) / n, sum(ry) / n
    rank_x_var = sum((value - rmx) ** 2 for value in rx)
    rank_y_var = sum((value - rmy) ** 2 for value in ry)
    spearman = (
        sum((a - rmx) * (b - rmy) for a, b in zip(rx, ry)) / math.sqrt(rank_x_var * rank_y_var)
        if rank_x_var and rank_y_var
        else None
    )
    return {
        "pearson": pearson,
        "spearman": spearman,
        "r_squared": r_squared,
        "slope": slope,
        "intercept": intercept,
    }


class CatalystAnalysisService:
    """Read-only, catalyst-sample keyed analysis built on the export safety gate."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def field_registry() -> list[dict[str, Any]]:
        return [dict(item) for item in _FIELD_REGISTRY]

    def _load_ready_rows(self, library_name: str | None) -> tuple[list[_ReadyRow], Counter[str], dict[str, int]]:
        stmt = select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id)
        if library_name:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, normalize_library_name(library_name)))
        source_rows = self.session.execute(stmt).all()
        gates = bulk_export_gate_results(self.session, [row for row, _paper in source_rows], target_type="dft_results")
        exclusion_reasons: Counter[str] = Counter()
        for row, _paper in source_rows:
            gate = gates.get(str(row.id))
            if gate and not gate.eligible:
                for reason in gate.reasons or ("export_safety_gate",):
                    exclusion_reasons[f"safety_gate:{reason}"] += 1
            if not row.catalyst_sample_id:
                exclusion_reasons["missing_catalyst_sample_id"] += 1
            if row.identity_version != 2:
                exclusion_reasons["identity_v2_required"] += 1

        if not source_rows:
            return [], exclusion_reasons, {
                "total_dft_rows": 0,
                "exportable_dft_rows": 0,
                "v2_row_ready_numeric_rows": 0,
                "distinct_exportable_catalysts": 0,
            }
        paper_ids = {paper.id for _row, paper in source_rows}
        catalysts = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id.in_(paper_ids))).all()
        settings = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id.in_(paper_ids))).all()
        catalyst_by_id = {str(item.id): item for item in catalysts}
        dataset = build_dft_ml_dataset(
            self.session,
            library_name=normalize_library_name(library_name) if library_name else None,
            _source_rows=source_rows,
            _gate_by_id=gates,
            _catalysts=catalysts,
            _settings=settings,
        )
        rows_by_id = {str(row.id): (row, paper) for row, paper in source_rows}
        ready: list[_ReadyRow] = []
        exportable = 0
        exportable_catalysts: set[str] = set()
        for row, paper in source_rows:
            gate = gates.get(str(row.id))
            if gate and gate.eligible:
                exportable += 1
                if row.catalyst_sample_id:
                    exportable_catalysts.add(str(row.catalyst_sample_id))
        for record in dataset.get("records") or []:
            record_id = str(record.get("record_id"))
            row_paper = rows_by_id.get(record_id)
            if row_paper is None:
                continue
            row, paper = row_paper
            if row.identity_version != 2:
                continue
            if not bool(record.get("is_ml_ready")):
                exclusion_reasons["identity_v2_not_ml_ready"] += 1
                continue
            if record.get("setting_link_status") != "clear_primary" or not record.get("linked_dft_setting"):
                exclusion_reasons["missing_or_ambiguous_calculation_context"] += 1
                continue
            catalyst = catalyst_by_id.get(str(row.catalyst_sample_id)) if row.catalyst_sample_id else None
            if catalyst is None:
                continue
            ready.append(_ReadyRow(row=row, paper=paper, record=record, catalyst=catalyst))
        return ready, exclusion_reasons, {
            "total_dft_rows": len(source_rows),
            "exportable_dft_rows": exportable,
            "v2_row_ready_numeric_rows": len(ready),
            "distinct_exportable_catalysts": len(exportable_catalysts),
        }

    def overview_counts(self, library_name: str | None) -> dict[str, Any]:
        ready, exclusions, counts = self._load_ready_rows(library_name)
        papers = {str(item.paper.id) for item in ready}
        paper_codes = {item.paper.paper_code for item in ready if item.paper.paper_code}
        return {
            **counts,
            "distinct_exportable_catalysts": counts["distinct_exportable_catalysts"],
            "contributing_papers": len(papers),
            "contributing_paper_ids": sorted(papers),
            "contributing_paper_codes": sorted(paper_codes),
            "excluded_counts": dict(sorted(exclusions.items())),
            "analysis_policy": "Only safety-gate-eligible, identity_version=2, is_ml_ready rows with explicit catalyst_sample_id are eligible.",
        }

    def correlation(self, *, library_name: str | None, x_field: str, y_field: str, min_n: int = 3) -> dict[str, Any]:
        x_field = FIELD_ALIASES.get(x_field, x_field)
        y_field = FIELD_ALIASES.get(y_field, y_field)
        if x_field not in FIELD_REGISTRY:
            raise ValueError(f"unknown x_field: {x_field}")
        if y_field not in FIELD_REGISTRY:
            raise ValueError(f"unknown y_field: {y_field}")
        if FIELD_REGISTRY[x_field]["type"] != "number" or FIELD_REGISTRY[y_field]["type"] != "number":
            raise ValueError("x_field and y_field must be numeric analysis fields")
        if min_n < 3:
            raise ValueError("min_n must be at least 3")
        ready, exclusions, _counts = self._load_ready_rows(library_name)
        by_catalyst: dict[str, list[_ReadyRow]] = defaultdict(list)
        for item in ready:
            by_catalyst[str(item.catalyst.id)].append(item)

        points: list[dict[str, Any]] = []
        details: list[dict[str, Any]] = []
        for catalyst_id in sorted(by_catalyst):
            rows = by_catalyst[catalyst_id]
            x_groups = _candidate_groups(x_field, rows)
            y_groups = _candidate_groups(y_field, rows)
            selected_x: dict[str, Any] | None = None
            selected_y: dict[str, Any] | None = None
            reason: str | None = None
            if x_field == y_field:
                selected_x, reason, _ = _resolve_field(x_field, x_groups)
                selected_y = selected_x
            else:
                compatible = [
                    (x_group, y_group)
                    for x_group in x_groups
                    for y_group in y_groups
                    if x_group and y_group and _contexts_compatible(x_group[0].context, y_group[0].context)
                ]
                if len(compatible) != 1:
                    reason = "context_mismatch" if not compatible else "multiple_comparable_contexts"
                else:
                    selected_x, x_reason, _ = _resolve_group(x_field, compatible[0][0])
                    selected_y, y_reason, _ = _resolve_group(y_field, compatible[0][1])
                    reason = x_reason or y_reason
            if reason or selected_x is None or selected_y is None:
                exclusions[reason or "missing_field_value"] += 1
                details.append(
                    {
                        "catalyst_sample_id": catalyst_id,
                        "catalyst_name": rows[0].catalyst.name,
                        "paper": _paper_payload(rows[0].paper),
                        "reason": reason or "missing_field_value",
                        "x_candidates": [_candidate_payload(item) for group in x_groups for item in group],
                        "y_candidates": [_candidate_payload(item) for group in y_groups for item in group],
                    }
                )
                continue
            point = {
                "catalyst_sample_id": catalyst_id,
                "catalyst_name": rows[0].catalyst.name,
                "catalyst": _catalyst_payload(rows[0].catalyst),
                "paper": _paper_payload(rows[0].paper),
                "x": {"field": x_field, **selected_x},
                "y": {"field": y_field, **selected_y},
                "x_source_record_ids": selected_x["source_record_ids"],
                "y_source_record_ids": selected_y["source_record_ids"],
                "semantic_context": selected_x["context"],
            }
            points.append(point)

        paper_ids = sorted({point["paper"]["paper_id"] for point in points})
        paper_codes = sorted({point["paper"]["paper_code"] for point in points if point["paper"].get("paper_code")})
        warnings: list[str] = []
        if len(points) < min_n:
            warnings.append("min_n_not_reached")
        if len(paper_ids) < 2:
            warnings.append("fewer_than_two_contributing_papers")
        stats = _stats(points) if len(points) >= min_n else {"pearson": None, "spearman": None, "r_squared": None, "slope": None, "intercept": None}
        return {
            "schema_version": SCHEMA_VERSION,
            "x": dict(FIELD_REGISTRY[x_field]),
            "y": dict(FIELD_REGISTRY[y_field]),
            "x_field": x_field,
            "y_field": y_field,
            "points": points,
            "n_catalysts": len(points),
            "n_papers": len(paper_ids),
            "paper_ids": paper_ids,
            "paper_codes": paper_codes,
            "excluded_count": len(details),
            "excluded_row_reason_count": sum(exclusions.values()),
            "excluded_reasons": dict(sorted(exclusions.items())),
            "excluded_details": details,
            "warnings": warnings,
            "selection_policy": "One point per catalyst_sample_id. Safety gate, Identity V2, and is_ml_ready are required; semantic contexts must be comparable. Conflicts are null/excluded, never averaged or Cartesian-paired.",
            "ready": len(points) >= min_n,
            "pearson": stats["pearson"],
            "pearson_r": stats["pearson"],
            "spearman": stats["spearman"],
            "spearman_rho": stats["spearman"],
            "r_squared": stats["r_squared"],
            "r2": stats["r_squared"],
            "slope": stats["slope"],
            "intercept": stats["intercept"],
            "statistics": {
                "ready": len(points) >= min_n,
                "min_n": min_n,
                "pearson": stats["pearson"],
                "pearson_r": stats["pearson"],
                "spearman": stats["spearman"],
                "spearman_rho": stats["spearman"],
                "r_squared": stats["r_squared"],
                "r2": stats["r_squared"],
                "slope": stats["slope"],
                "intercept": stats["intercept"],
            },
        }
