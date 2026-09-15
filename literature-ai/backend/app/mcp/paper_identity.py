"""app.mcp.paper_identity — 单篇「身份 / 去重 / 归类 / 标准化」的 MCP 衔接。

本模块是 **MCP 与既有服务之间的最小接线**，不新建任何检测/去重/重算逻辑：

只读检测  ``review_paper_identity(paper_id)``
    复用 ``DFTAuditIssueLifecycleService``（身份 v2 权威读取与只读派生）、
    ``ReviewConflictAggregationService``（字段级冲突聚合）、
    ``app.domain.reaction_taxonomy``（反应分类与属性/单位规范）。
    整个查询在 PostgreSQL 只读事务中执行，不写库、不写审计、不落盘。

受控应用  ``apply_paper_identity_rematerialization(paper_id, expectations, reason, dry_run)``
    直接复用 ``DFTIdentityRematerializationService``（plan/apply/readback）。

如实声明（不得被调用方误解为“已完成”）：
* 本模块 **不判定科学验收**，不做人工裁决；
* **身份键不同不等于科学上无重复**：同一 ``subject_key`` 下的多个
  ``observation_key`` 只说明“同一主体、多个观测键”，是否重复必须由人核对原文；
* **规则检测**（平台身份策略 / 反应分类口径 / 单位表推导）与
  **原文依据**（存储证据里的页码、表格单元、逐字引文）分开标注；
* **单位非空不等于单位已核验**；
* 不新增自动删除、自动合并；不改科学字段；不做 SQL 直改。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CatalystSample, DFTResult, Paper
from app.db.session import session_scope
from app.domain.reaction_taxonomy import classify_reaction_record, validate_reaction_record
from app.mcp.auth import require_mcp_capability, require_mcp_capability_any
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_identity_rematerialization_service import (
    DFTIdentityRematerializationService,
)
from app.services.review_conflict_service import ReviewConflictAggregationService

try:  # Python 3.11+
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover - 仅在无 mcp 包环境导入本模块时使用
    ToolAnnotations = None


SCHEMA_VERSION = "paper_identity_review_v1"
APPLY_SCHEMA_VERSION = "paper_identity_apply_v1"

DEFAULT_MAX_ITEMS = 30
HARD_MAX_ITEMS = 200

# 只有这些字段会被身份重算写入；其余科学字段一律不动。
REMATERIALIZED_FIELDS = (
    "identity_version",
    "subject_key",
    "observation_key",
    "identity_payload",
)

_QUOTE_PREVIEW_CHARS = 300

# 身份策略报出的错误码 → 说明（规则检测，非原文证据）。
_IDENTITY_ERROR_LABELS = {
    "missing_material_identity": "缺少材料身份（无法唯一确定主体）",
    "missing_adsorbate_identity": "缺少吸附物（无法唯一确定主体）",
    "missing_reaction_step_identity": "缺少反应步骤（无法唯一确定主体）",
    "missing_active_site_identity": "缺少活性位点身份",
    "missing_atom_pair_identity": "缺少原子对身份（该性质必须指定原子对）",
    "conflicting_atom_pair_aliases": "原子对别名互相冲突（证据内部不一致）",
    "missing_value_identity": "缺少数值（无观测值 → 不能判定重复）",
    "invalid_numeric_identity": "数值无法规范化为十进制（不能判定重复）",
    "unsupported_value_kind_identity": "value_kind 不受支持（不能判定重复）",
    "missing_value_upper_identity": "区间型数值缺少上界",
    "invalid_value_upper_identity": "区间上界无法规范化",
    "missing_property_type_identity": "缺少性质类型（property_type）",
    "unsupported_unit_identity": "单位不在该性质的规范单位表内 → 不做规范化",
}

_WELL_KNOWN_IDENTITY_ERRORS = frozenset(_IDENTITY_ERROR_LABELS)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _enforce_postgres_read_only_transaction(session: Any) -> bool:
    """把本查询事务设为只读，保证检测路径零写入（与状态/导出工具同机制）。"""
    try:
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            return False
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        return True
    except AttributeError:
        return False


# ---------------------------------------------------------------------------
# 证据锚点：把“规则检测”与“原文依据”分开
# ---------------------------------------------------------------------------
def _first_present(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def _evidence_anchor(row: DFTResult, catalyst_name: str | None) -> dict[str, Any]:
    payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
    nested = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
    merged = {**nested, **payload}

    page = _first_present(merged, ("page", "pdf_page", "page_number", "page_index"))
    table_id = _first_present(merged, ("table_id", "source_table_id"))
    figure_id = _first_present(merged, ("figure_id", "source_figure", "figure"))
    locator = _first_present(
        merged,
        ("locator", "evidence_locator", "evidence_locators", "evidence_locator_index"),
    )
    quote = _text(row.evidence_text)

    has_quote = bool(quote)
    has_locator = any(value is not None for value in (page, table_id, figure_id, locator))
    evidence_backed = has_quote and has_locator
    return {
        "source_section": _text(row.source_section),
        "source_figure": _text(row.source_figure),
        "page": page,
        "table_id": table_id,
        "figure_id": figure_id,
        "locator": locator,
        "evidence_text_present": has_quote,
        "evidence_text_preview": (quote or "")[:_QUOTE_PREVIEW_CHARS] or None,
        "detection": "evidence_backed" if evidence_backed else "rule_based",
        "detection_note": (
            "存储证据含逐字引文与定位锚点"
            if evidence_backed
            else "仅由结构化字段与平台口径推导；是否与原文一致仍需回查 PDF"
        ),
    }


def _identity_errors(identity: Any) -> list[str]:
    codes = [str(code) for code in (getattr(identity, "error_codes", ()) or ()) if str(code).strip()]
    return list(dict.fromkeys(codes))


def _describe_errors(codes: Iterable[str]) -> list[dict[str, str]]:
    return [
        {
            "error_code": code,
            "label": _IDENTITY_ERROR_LABELS.get(code, "身份必需字段缺失（未登记的 error_code）"),
            "kind": "rule_based",
        }
        for code in codes
    ]


def _head(rows: list[Any], cap: int) -> tuple[list[Any], int, bool]:
    """返回（明细列表, 总数, 是否被截断）。"""
    return rows[:cap], len(rows), len(rows) > cap


def _candidate_payload(row: DFTResult) -> dict[str, Any]:
    return {
        "reaction_type": row.reaction_type,
        "property_type": row.property_type,
        "adsorbate": row.adsorbate,
        "intermediate": row.adsorbate,
        "reaction_step": row.reaction_step,
        "evidence_text": row.evidence_text,
    }


def build_paper_identity_review(
    session: Session,
    *,
    paper_id: UUID,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    """只读：返回本论文的身份 / 去重 / 归类 / 标准化问题清单及证据与建议。"""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ValueError("Paper not found")

    lifecycle = DFTAuditIssueLifecycleService(session)

    rows = list(
        session.scalars(
            select(DFTResult).where(DFTResult.paper_id == paper_id).order_by(DFTResult.id.asc())
        ).all()
    )

    samples = list(
        session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()
    )
    sample_by_id = {sample.id: sample for sample in samples}
    sample_name = {sid: _text(sample.name) for sid, sample in sample_by_id.items()}

    def _catalyst_name(row: DFTResult) -> str | None:
        return sample_name.get(row.catalyst_sample_id) if row.catalyst_sample_id else None

    # ---- 1) 身份：逐条只读派生（对 legacy 行绝不落库） ----
    identity_by_row: dict[UUID, Any] = {
        row.id: lifecycle.identity_for_result(row) for row in rows
    }

    never_materialized: list[DFTResult] = []
    incomplete: list[DFTResult] = []
    complete: list[DFTResult] = []
    error_code_counter: Counter[str] = Counter()

    for row in rows:
        identity = identity_by_row[row.id]
        codes = _identity_errors(identity)
        if codes:
            incomplete.append(row)
            error_code_counter.update(codes)
            continue
        if row.identity_version != 2 or not _text(row.subject_key):
            never_materialized.append(row)
        complete.append(row)

    # ---- 2) 重复：确认重复（同 observation_key） vs 同一主体多观测键 ----
    by_observation: dict[str, list[DFTResult]] = defaultdict(list)
    by_subject: dict[str, dict[str, list[DFTResult]]] = defaultdict(lambda: defaultdict(list))
    for row in complete:
        identity = identity_by_row[row.id]
        subject_key = str(identity.subject_key)
        observation_key = _text(identity.observation_key)
        if observation_key:
            by_observation[observation_key].append(row)
        by_subject[subject_key][observation_key or ""].append(row)

    exact_groups = sorted(
        ((key, group) for key, group in by_observation.items() if len(group) > 1),
        key=lambda item: (-len(item[1]), item[0]),
    )
    multi_observation = sorted(
        (
            (key, obs_map)
            for key, obs_map in by_subject.items()
            if len([obs for obs, group in obs_map.items() if group]) > 1
        ),
        key=lambda item: (-len(item[1]), item[0]),
    )

    def _row_brief(row: DFTResult) -> dict[str, Any]:
        return {
            "dft_result_id": str(row.id),
            "catalyst_sample_id": str(row.catalyst_sample_id) if row.catalyst_sample_id else None,
            "catalyst_name": _catalyst_name(row),
            "property_type": row.property_type,
            "value": row.value,
            "value_upper": row.value_upper,
            "value_kind": row.value_kind,
            "unit": row.unit,
            "adsorbate": row.adsorbate,
            "reaction_step": row.reaction_step,
            "reaction_type": row.reaction_type,
            "candidate_status": row.candidate_status,
            "identity_version": row.identity_version,
            "subject_key": row.subject_key,
            "observation_key": row.observation_key,
            "ml_ready": row.ml_ready_at is not None,
            "evidence": _evidence_anchor(row, _catalyst_name(row)),
        }

    dup_head, dup_total_rows, dup_more = _head([g for _, g in exact_groups], max_items)
    dup_items = []
    for observation_key, group in zip([k for k, _ in exact_groups], dup_head):
        dup_items.append(
            {
                "observation_key": observation_key,
                "record_count": len(group),
                "duplicate_record_ids": [str(row.id) for row in group],
                "records": [_row_brief(row) for row in group],
                "basis": "identity v2：observation_key 完全相同",
                "detection": "rule_based",
                "note": "同一论文内 observation_key 相同 ⇒ 平台身份语义下的确认重复；"
                "是否删改仍须人工核对原文，本工具不做任何删除或合并",
            }
        )

    multi_head, multi_total, multi_more = _head(multi_observation, max_items)
    multi_items = []
    for subject_key, obs_map in multi_head:
        observations = []
        for observation_key, group in sorted(obs_map.items(), key=lambda item: item[0]):
            if not group:
                continue
            observations.append(
                {
                    "observation_key": observation_key or None,
                    "record_count": len(group),
                    "record_ids": [str(row.id) for row in group],
                    "distinguishing_fields": {
                        "value": group[0].value,
                        "value_kind": group[0].value_kind,
                        "unit": group[0].unit,
                        "adsorbate": group[0].adsorbate,
                        "reaction_step": group[0].reaction_step,
                        "property_type": group[0].property_type,
                    },
                }
            )
        multi_items.append(
            {
                "subject_key": subject_key,
                "observation_count": len(observations),
                "observations": observations,
                "basis": "identity v2：同一 subject_key 下出现多个 observation_key",
                "detection": "rule_based",
                "note": "**身份键不同不等于科学上无重复**：这表示同一主体下存在多个观测键"
                "（可能来自不同构型、反应步骤、条件或来源）；是否重复必须由人回查原文证据",
            }
        )

    # ---- 3) 字段级冲突（复用既有聚合服务） ----
    conflict_counter = ReviewConflictAggregationService(session).count_conflicts_by_paper({paper_id})
    field_conflicts = int(conflict_counter.get(str(paper_id), 0))

    # ---- 4) 归类（反应分类） ----
    unclassified: list[DFTResult] = []
    classification_reasons: Counter[str] = Counter()
    for row in rows:
        stored = str(row.reaction_type or "").strip()
        if stored and stored.upper() != "UNKNOWN":
            continue
        unclassified.append(row)
        verdict = classify_reaction_record(_candidate_payload(row), paper.title)
        classification_reasons[str(verdict.get("reason") or "unknown")] += 1

    reaction_type_counter: Counter[str] = Counter(str(row.reaction_type or "UNKNOWN") for row in rows)

    unclassified_head, unclassified_total, unclassified_more = _head(unclassified, max_items)
    unclassified_items = []
    for row in unclassified_head:
        verdict = classify_reaction_record(_candidate_payload(row), paper.title)
        unclassified_items.append(
            {
                **_row_brief(row),
                "suggested_reaction_type": verdict.get("reaction_type"),
                "suggested_status": verdict.get("status"),
                "suggested_confidence": verdict.get("confidence"),
                "suggested_reason": verdict.get("reason"),
                "detection": "rule_based",
                "note": "建议由平台反应分类口径推导；落库需走 assign_dft_reaction_label，"
                "且证据不足时必须保持未分类",
            }
        )

    # ---- 5) 标准化（单位 / 性质规范性） ----
    unit_missing: list[DFTResult] = []
    unit_mismatch: list[dict[str, Any]] = []
    out_of_scope_reasons: Counter[str] = Counter()
    out_of_scope_items: list[dict[str, Any]] = []

    for row in rows:
        if row.value is not None and not _text(row.unit):
            unit_missing.append(row)

        validation = validate_reaction_record(row.reaction_type, _candidate_payload(row))
        reasons = [str(item) for item in (validation.get("reasons") or [])]
        out_of_scope_reasons.update(reasons)
        if reasons and len(out_of_scope_items) < max_items:
            out_of_scope_items.append(
                {
                    **_row_brief(row),
                    "reasons": reasons,
                    "canonical_unit_suggestion": validation.get("canonical_unit"),
                    "detection": "rule_based",
                    "note": "由反应 profile 的允许属性/规范单位表推导；不等于原文核验结论。"
                    "reaction_type=UNKNOWN 的行会同时给出 unknown_reaction_type",
                }
            )

        canonical_unit = validation.get("canonical_unit")
        current_unit = _text(row.unit)
        if canonical_unit and current_unit and str(canonical_unit) != current_unit:
            if len(unit_mismatch) < max_items:
                unit_mismatch.append(
                    {
                        **_row_brief(row),
                        "suggested_unit": canonical_unit,
                        "detection": "rule_based",
                        "note": "当前单位与规范单位不同；**不得自动改写**，需回原文确认后再走纠错流程",
                    }
                )

    unit_missing_head, unit_missing_total, unit_missing_more = _head(unit_missing, max_items)

    # ---- 6) 按催化剂汇总受影响记录 ----
    per_catalyst: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.catalyst_sample_id) if row.catalyst_sample_id else "__unbound__"
        entry = per_catalyst.setdefault(
            key,
            {
                "catalyst_sample_id": None if key == "__unbound__" else key,
                "catalyst_name": _catalyst_name(row),
                "catalyst_type": (
                    sample_by_id[row.catalyst_sample_id].catalyst_type
                    if row.catalyst_sample_id in sample_by_id
                    else None
                ),
                "record_count": 0,
                "identity_incomplete_records": 0,
                "identity_not_materialized_records": 0,
                "exact_duplicate_records": 0,
                "unclassified_records": 0,
                "unit_missing_records": 0,
            },
        )
        entry["record_count"] += 1

    incomplete_ids = {row.id for row in incomplete}
    never_ids = {row.id for row in never_materialized}
    dup_ids = {row.id for _, group in exact_groups for row in group}
    unclassified_ids = {row.id for row in unclassified}
    unit_missing_ids = {row.id for row in unit_missing}
    for row in rows:
        key = str(row.catalyst_sample_id) if row.catalyst_sample_id else "__unbound__"
        entry = per_catalyst[key]
        if row.id in incomplete_ids:
            entry["identity_incomplete_records"] += 1
        if row.id in never_ids:
            entry["identity_not_materialized_records"] += 1
        if row.id in dup_ids:
            entry["exact_duplicate_records"] += 1
        if row.id in unclassified_ids:
            entry["unclassified_records"] += 1
        if row.id in unit_missing_ids:
            entry["unit_missing_records"] += 1

    catalyst_items = sorted(
        per_catalyst.values(),
        key=lambda item: (-item["record_count"], str(item["catalyst_sample_id"])),
    )
    catalyst_head, catalyst_total, catalyst_more = _head(catalyst_items, HARD_MAX_ITEMS)

    # ---- 7) 汇总 ----
    unresolved = [
        {
            "kind": "identity_incomplete",
            "count": len(incomplete),
            "reason": "必需身份字段缺失或不可规范化 ⇒ 平台拒绝派生 observation_key"
            "（防止把科学上不确定的行当成重复）",
            "detail_tool": "review_paper_identity",
        },
        {
            "kind": "identity_not_materialized",
            "count": len(never_materialized),
            "reason": "行存在但 identity_version≠2 / subject_key 为空（历史行），"
            "需经受控重算入口显式物化",
            "detail_tool": "apply_paper_identity_rematerialization",
        },
        {
            "kind": "exact_duplicate_groups",
            "count": len(exact_groups),
            "reason": "同一 observation_key 出现在多条记录上（平台身份语义下的确认重复）",
            "detail_tool": "get_dft_audit_issues",
        },
        {
            "kind": "same_subject_multi_observation",
            "count": len(multi_observation),
            "reason": "同一主体多个观测键；**不构成重复结论**，需人工核对原文区分构型/条件/步骤",
            "detail_tool": "get_dft_review_task",
        },
        {
            "kind": "field_conflicts",
            "count": field_conflicts,
            "reason": "多来源对同一字段取值不一致（字段级冲突，聚合口径）",
            "detail_tool": "get_review_conflicts",
        },
        {
            "kind": "unclassified_records",
            "count": len(unclassified),
            "reason": "反应类型缺失或为 UNKNOWN（缺上下文 / 上下文冲突 / 仅有共享中间体信号）",
            "detail_tool": "assign_dft_reaction_label",
        },
        {
            "kind": "unit_missing_records",
            "count": len(unit_missing),
            "reason": "有数值但单位为空；单位非空亦不等于单位已核验",
            "detail_tool": "get_ai_verification_record_tasks",
        },
        {
            "kind": "property_or_intermediate_out_of_scope",
            "count": sum(out_of_scope_reasons.values()),
            "reason": "性质/中间体不在该反应 profile 的允许集合内（逐条原因见 standardization.out_of_scope.items）",
            "detail_tool": "review_paper_identity",
        },
    ]

    next_steps = [
        {
            "stage": "identity_dedup",
            "action": "受控身份重算（显式 allowlist；本模块提供的唯一入口）",
            "tool": "apply_paper_identity_rematerialization",
            "tool_params_source": "paper_id 取自本响应 paper.paper_id；expectations[] 字段取自 "
            "identity.records[] / identity.not_materialized.items[]"
            "（dft_result_id + 当前 identity_version/subject_key/observation_key 原值）；reason 必填",
            "side_effects": "仅写 dft_results 的 identity_version/subject_key/observation_key/identity_payload，"
            "并每行写一条 audit_logs(action=rematerialized_dft_identity_v2)；无改动返回 no_changes 且零写入",
            "note": "先用 dry_run=true 复核；stale 快照、碰撞、跨论文、空 allowlist 一律整体拒绝",
        },
        {
            "stage": "identity_dedup",
            "action": "核对确认重复与同一主体多观测键（只读）",
            "tool": "get_dft_audit_issues",
            "tool_params_source": "paper_id；issue_types / statuses 可选",
            "note": "duplicates.exact_duplicates 为平台身份语义下的确认重复；"
            "same_subject_multi_observation 不构成重复结论",
        },
        {
            "stage": "identity_dedup",
            "action": "核对字段级冲突（只读）",
            "tool": "get_review_conflicts",
            "tool_params_source": "paper_id",
            "note": f"本论文当前字段级冲突计数 = {field_conflicts}",
        },
        {
            "stage": "classification",
            "action": "为未分类记录指定反应标签",
            "tool": "assign_dft_reaction_label",
            "tool_params_source": "paper_id + dft_result_id（取自 classification.unclassified.items[].dft_result_id）"
            "+ reaction_type",
            "note": "证据不足时必须保持未分类（不得为推进流程强制分类）",
        },
        {
            "stage": "standardization",
            "action": "带证据的字段更正（既有纠错链路，不在本模块内）",
            "tool": "propose_dft_result_correction",
            "tool_params_source": "paper_id + dft_result_id + 字段 + corrected_value + 证据定位",
            "follow_up_tool": "approve_correction",
            "note": "单位/数值/材料身份的改正是科学字段变更，必须走纠错提案与人工批准；"
            "本模块只给建议，**不直接 SQL 改科学字段**",
        },
        {
            "stage": "ml_export",
            "action": "任务级机器学习数据导出",
            "tool": "export_paper_ml_dataset",
            "tool_params_source": "paper_id；task 取自 list_ml_export_tasks",
        },
    ]

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "as_of": _utc_now_iso(),
        "read_only_transaction": True,
        "paper": {
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "library_name": paper.library_name,
        },
        "scope": {
            "dft_records": len(rows),
            "registered_catalyst_samples": len(samples),
            "unbound_records": sum(1 for row in rows if not row.catalyst_sample_id),
            "identity_version_distribution": dict(
                Counter(str(row.identity_version) for row in rows)
            ),
        },
        "identity": {
            "detection": "rule_based",
            "basis": "identity v2 平台策略（build_dft_identity_v2 + 逐性质 requirements）；只读派生，"
            "绝不为 legacy 行落库",
            "complete_records": len(complete),
            "incomplete": {
                "count": len(incomplete),
                "by_error_code": dict(error_code_counter),
                "error_legend": _describe_errors(sorted(error_code_counter)),
                "returned": len(_head(incomplete, max_items)[0]),
                "has_more": _head(incomplete, max_items)[2],
                "items": [
                    {
                        **_row_brief(row),
                        "error_codes": _identity_errors(identity_by_row[row.id]),
                        "error_details": _describe_errors(_identity_errors(identity_by_row[row.id])),
                    }
                    for row in _head(incomplete, max_items)[0]
                ],
                "note": "这些行的 observation_key 恒为 NULL（平台拒绝把科学上不确定的行当作重复）；"
                "缺失原因由身份策略给出，属规则检测，仍需回原文补齐证据",
            },
            "not_materialized": {
                "count": len(never_materialized),
                "returned": len(_head(never_materialized, max_items)[0]),
                "has_more": _head(never_materialized, max_items)[2],
                "items": [_row_brief(row) for row in _head(never_materialized, max_items)[0]],
                "note": "identity_version≠2 或 subject_key 为空的历史行；只能经受控重算入口显式物化",
            },
            "records": [_row_brief(row) for row in _head(complete, max_items)[0]],
            "records_returned": len(_head(complete, max_items)[0]),
            "records_has_more": _head(complete, max_items)[2],
            "unknown_error_codes": sorted(
                code for code in error_code_counter if code not in _WELL_KNOWN_IDENTITY_ERRORS
            ),
        },
        "duplicates": {
            "detection": "rule_based",
            "basis": "identity v2：observation_key 完全相同 ⇒ 确认重复；"
            "subject_key 相同但 observation_key 不同 ⇒ 非重复结论",
            "exact_duplicates": {
                "group_count": len(exact_groups),
                "record_count": dup_total_rows,
                "returned": len(dup_items),
                "has_more": dup_more,
                "groups": dup_items,
                "note": "组内记录共享同一 observation_key；这是平台身份语义下的确认重复，"
                "不是删除授权。本工具不删除、不合并任何记录。",
            },
            "same_subject_multi_observation": {
                "subject_count": multi_total,
                "returned": len(multi_items),
                "has_more": multi_more,
                "groups": multi_items,
                "note": "只表示同一主体下存在多个观测键；"
                "身份键不同 ≠ 科学上无重复，须回查原文区分构型、反应步骤、条件与来源。",
            },
        },
        "field_conflicts": {
            "count": field_conflicts,
            "detection": "rule_based",
            "basis": "ReviewConflictAggregationService（与工作台页面「冲突 N」同源聚合）",
            "detail_tool": "get_review_conflicts",
            "note": "聚合口径：多来源对同一字段取值不一致；本工具只报计数，不下裁决",
        },
        "classification": {
            "detection": "rule_based",
            "basis": "app.domain.reaction_taxonomy.classify_reaction_record（平台反应分类口径）",
            "reaction_type_distribution": dict(reaction_type_counter),
            "unclassified": {
                "count": unclassified_total,
                "by_reason": dict(classification_reasons),
                "returned": len(unclassified_items),
                "has_more": unclassified_more,
                "items": unclassified_items,
            },
            "note": "suggested_* 是规则建议，不是原文结论；落库走 assign_dft_reaction_label，"
            "证据不足必须保持未分类",
        },
        "standardization": {
            "detection": "rule_based",
            "basis": "app.domain.reaction_taxonomy.validate_reaction_record（允许属性 + 规范单位表）",
            "unit_missing": {
                "count": unit_missing_total,
                "returned": len(unit_missing_head),
                "has_more": unit_missing_more,
                "items": [_row_brief(row) for row in unit_missing_head],
            },
            "unit_mismatch": {
                "count": len(unit_mismatch),
                "items": unit_mismatch,
                "note": "当前单位与规范单位不同；**不得自动改写**",
            },
            "out_of_scope": {
                "count": sum(out_of_scope_reasons.values()),
                "by_reason": dict(out_of_scope_reasons),
                "items": out_of_scope_items,
            },
            "note": "单位非空不等于单位已核验；规范性建议只用于定位问题，改写必须走纠错提案",
        },
        "catalysts": {
            "grouping": "catalyst_sample_id（稳定 UUID），绝不按名称合并；缺失绑定归 __unbound__",
            "total": catalyst_total,
            "returned": len(catalyst_head),
            "has_more": catalyst_more,
            "items": catalyst_head,
            "note": "完整催化剂分组与分类（目标/对照/待确认）见 get_paper_processing_status；"
            "此处只给每个催化剂受身份/重复/归类/标准化问题影响的记录数",
        },
        "unresolved": {
            "counts": {item["kind"]: item["count"] for item in unresolved},
            "items": unresolved,
        },
        "evidence_legend": {
            "rule_based": "由结构化字段与平台身份策略/反应分类口径/单位表推导；**不等于原文结论**",
            "evidence_backed": "存储证据里确有逐字引文与定位锚点（页码/表格单元/图表）",
            "never": "本工具不判定科学验收，不替代人工裁决",
        },
        "not_computed": [
            "科学验收（人工裁决）状态",
            "任务级训练集就绪行数（见 export_paper_ml_dataset 的 counts.exported_rows）",
            "证据闸门逐行复算（见 get_paper_processing_status.stages.evidence_verification）",
        ],
        "next_steps": next_steps,
        "limits": {
            "max_items_requested": max_items,
            "hard_max_items": HARD_MAX_ITEMS,
            "note": "groups/items 明细最多返回 max_items 条（硬上限 200）；"
            "各类 count 为全量计数，不受截断影响",
        },
    }
    return payload


def build_paper_identity_apply(
    session: Session,
    *,
    paper_id: UUID,
    expectations: list[dict[str, Any]],
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    """受控应用：调用既有 DFTIdentityRematerializationService（不新增任何逻辑）。"""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ValueError("Paper not found")

    if not isinstance(expectations, list) or not expectations:
        raise ValueError(
            "expectations must be a non-empty list of {dft_result_id, expected_identity_version, "
            "expected_subject_key, expected_observation_key}"
        )

    normalised: list[dict[str, Any]] = []
    for index, item in enumerate(expectations):
        if not isinstance(item, dict):
            raise ValueError(f"expectations[{index}] must be an object")
        raw_id = _text(item.get("dft_result_id"))
        if not raw_id:
            raise ValueError(f"expectations[{index}].dft_result_id is required")
        try:
            record_id = UUID(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expectations[{index}].dft_result_id is not a UUID") from exc
        normalised.append(
            {
                "dft_result_id": record_id,
                "expected_identity_version": item.get("expected_identity_version"),
                "expected_subject_key": item.get("expected_subject_key"),
                "expected_observation_key": item.get("expected_observation_key"),
            }
        )

    wanted = [item["dft_result_id"] for item in normalised]
    found = set(
        session.scalars(
            select(DFTResult.id)
            .where(DFTResult.paper_id == paper_id)
            .where(DFTResult.id.in_(wanted))
        ).all()
    )
    missing = [str(record_id) for record_id in wanted if record_id not in found]
    if missing:
        raise ValueError(
            "cross_paper_or_unknown_record: every dft_result_id must belong to the requested "
            f"paper_id; refused {len(missing)} id(s): {missing[:10]}"
        )

    service = DFTIdentityRematerializationService(session)
    service_expectations = [
        {
            "dft_result_id": str(item["dft_result_id"]),
            "expected_identity_version": item["expected_identity_version"],
            "expected_subject_key": item["expected_subject_key"],
            "expected_observation_key": item["expected_observation_key"],
        }
        for item in normalised
    ]

    applied = service.apply(
        paper_id=paper_id,
        expectations=service_expectations,
        reason=str(reason or "").strip(),
        dry_run=bool(dry_run),
    )

    readback: dict[str, Any] | None = None
    if not dry_run and applied.get("written"):
        readback = service.readback(paper_id=paper_id, record_ids=wanted)

    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "as_of": _utc_now_iso(),
        "paper": {
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "title": paper.title,
        },
        "dry_run": bool(dry_run),
        "reason": str(reason or "").strip(),
        "scope": {
            "requested_records": len(normalised),
            "paper_scoped": True,
            "fields_written": list(REMATERIALIZED_FIELDS),
            "scientific_fields_untouched": True,
        },
        "result": applied,
        "readback": readback,
        "policy": {
            "delegate": "DFTIdentityRematerializationService.plan/apply/readback（既有生产服务，未改动）",
            "guarantees": [
                "paper 作用域：每条记录必须属于请求的 paper_id（本入口额外做跨论文前置拒绝）",
                "显式 allowlist：只处理 expectations 中列出的 dft_result_id",
                "stale 拒绝：expected_* 与当前值不一致时整批中止，不写任何一行",
                "碰撞拒绝：新 observation_key 与他人相同或批内重复 ⇒ 跳过，绝不合并/覆盖",
                "NULL 安全：身份仍不完整则保持 observation_key=NULL 并如实上报",
                "幂等：已是目标身份记为 idempotent 且不写（重试不重复写入）",
                "审计：每个实际变更的行写一条 audit_logs(action=rematerialized_dft_identity_v2)",
            ],
            "not_doing": [
                "不删除任何记录",
                "不合并任何记录",
                "不修改任何科学字段（值/单位/材料/性质/反应/证据）",
                "不做直接 SQL 改库",
                "无改动时返回 no_changes / dry_run 且零写入",
            ],
            "known_limitation": "底层服务的审计 source 字符串仍为 b0102_identity_rematerialization"
            "（历史命名）；本轮未改动该生产服务",
        },
    }


def register_paper_identity_tools(server: Any) -> dict[str, str]:
    """注册单篇身份 / 去重检测与受控身份重算两个工具。"""

    review_description = (
        "Read-only identity / dedup / classification / standardization review for ONE paper. "
        "Separates what is RULE-BASED (derived from stored structured fields plus the platform "
        "identity policy, reaction taxonomy and canonical-unit tables) from what is EVIDENCE-BACKED "
        "(a verbatim quote plus a page/table/figure anchor actually stored in the row evidence). "
        "Returns: per-record identity v2 status; identity_incomplete rows with the exact policy error "
        "codes that blocked a usable observation_key; EXACT duplicate groups (records sharing an "
        "observation_key = platform-semantic confirmed duplicates); same-subject-multiple-observation "
        "groups, and it states explicitly that a DIFFERENT identity key does NOT mean there is no "
        "scientific duplicate; the field-level conflict count; unclassified records with a rule-based "
        "reaction-type suggestion; unit-missing / unit-mismatch / out-of-scope standardization findings; "
        "and a per-catalyst impact breakdown keyed by stable catalyst_sample_id. Never deletes, merges or "
        "rewrites anything; does not decide scientific acceptance; does not advance any workflow. "
        "Requires capability: read_papers."
    )

    apply_description = (
        "Controlled, audited identity-v2 re-materialization for ONE paper and an EXPLICIT record "
        "allowlist. Thin MCP entry over the existing DFTIdentityRematerializationService (no new logic). "
        "Every record must belong to paper_id (cross-paper ids are refused up front). The caller must "
        "pass the CURRENT identity_version / subject_key / observation_key by value; any drift aborts "
        "the whole batch before a single row is written (stale_identity_snapshot). A record whose new "
        "observation_key collides with another row, or twice inside the batch, is skipped and never "
        "merged or overwritten. A record whose identity is still incomplete keeps observation_key=NULL "
        "and is reported, not faked. Records already holding the target identity are reported idempotent "
        "and not written, so retries never double-write; when nothing needs changing the call returns "
        "no_changes with zero writes. Only 4 derived identity fields are written (identity_version / "
        "subject_key / observation_key / identity_payload) plus one audit row per changed record; no "
        "scientific field, no deletion, no merge, no direct SQL. Use dry_run=true first. "
        "Requires capability: repair_dft_issues or propose_corrections or review_dft."
    )

    read_annotations = None
    write_annotations = None
    if ToolAnnotations is not None:
        read_annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
        write_annotations = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    @server.tool(
        name="review_paper_identity",
        title="单篇身份/去重/归类/标准化检测（只读）",
        description=review_description,
        annotations=read_annotations,
    )
    def review_paper_identity(
        paper_id: str,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> dict[str, Any]:
        require_mcp_capability("read_papers")
        try:
            pid = UUID(str(paper_id or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid paper_id: expected a UUID string") from exc
        requested = max(1, min(int(max_items or DEFAULT_MAX_ITEMS), HARD_MAX_ITEMS))
        settings = get_settings()
        with session_scope(settings.database_url) as session:
            _enforce_postgres_read_only_transaction(session)
            return build_paper_identity_review(session, paper_id=pid, max_items=requested)

    @server.tool(
        name="apply_paper_identity_rematerialization",
        title="受控身份重算（显式清单 + 版本检查 + 审计）",
        description=apply_description,
        annotations=write_annotations,
    )
    def apply_paper_identity_rematerialization(
        paper_id: str,
        expectations: list[dict[str, Any]],
        reason: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        require_mcp_capability_any("repair_dft_issues", "propose_corrections", "review_dft")
        try:
            pid = UUID(str(paper_id or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid paper_id: expected a UUID string") from exc
        if not str(reason or "").strip():
            raise ValueError("reason is required: identity re-materialization must be justified")
        settings = get_settings()
        with session_scope(settings.database_url) as session:
            return build_paper_identity_apply(
                session,
                paper_id=pid,
                expectations=expectations,
                reason=reason,
                dry_run=bool(dry_run),
            )

    return {
        "review_paper_identity": "registered",
        "apply_paper_identity_rematerialization": "registered",
    }
