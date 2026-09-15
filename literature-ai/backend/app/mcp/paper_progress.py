"""app.mcp.paper_progress — 按催化剂组织的单篇处理状态（只读）。

工具：``get_paper_processing_status(paper_id)``

设计约束（与 AGENTS.md 单篇流程对齐）：
1. **只读**：整个查询在 PostgreSQL 只读事务中执行（与 submit(dry_run=true)
   相同的 ``SET TRANSACTION READ ONLY`` 机制），不写库、不写审计、不生成文件。
2. **不重活**：不重建训练集、不加载 PDF、不做逐记录核验复算。图表阶段状态
   复用 EvidenceReviewBundleService（图表审核权威服务），导出闸门复用
   bulk_export_gate_results（review_safety 导出闸门，与队列/导出同源）。
3. **状态口径分层**，绝不混淆：
   - candidate_status / ml_ready_*：材料化状态（提取与物化层面）；
   - evidence_gate（eligible/blocked + reasons）：证据闸门（导出安全层面）；
   - 任务就绪：任务级就绪由 ``export_paper_ml_dataset`` 按 task 计算（本工具不计算）；
   - 科学验收：人工裁决状态，本工具只报计数，**不判定、不推进**。
4. **身份键存在不等于科学去重完成**；**单位非空不等于单位原文已核验**；
   无可追溯结果的阶段一律返回 ``"未确认"`` / ``unknown``，不编造完成百分比。
5. **催化剂组织**：分两层——已登记样本（``catalyst_samples`` 表全部）与其中实际
   承载 DFT 记录的样本（record_count>0）。主概览只把“承载 DFT 性能数据的目标
   催化剂”当作已提取催化剂数，不把无数据样本混入。``catalyst_sample_id``（稳定
   UUID）是唯一分组身份，绝不按名称合并；分类口径与 paper_detail 页面
   dftCatalystCategory 一致：
   - target：catalyst_type = single_atom（本部署目标类）；
   - reference：已绑定样本但未归类为目标类（对照/参考材料）；
   - pending_confirmation：未绑定样本，或样本名称形态跨多材料（and/series，
     保守降级，不猜测归属）。
   每个条目同时返回原始 catalyst_type / scope，调用方可复核分类依据。
6. **ml_ready 口径**：``ml_ready_at`` 来自 ``dft_results.ml_ready_at IS NOT NULL``
   （提取/核验流程的材料化时间戳字段），**不等于任务级就绪**；任务级就绪需按
   task 的专用导出入口计算（当前无 MCP 入口，未实际计算），与 ``evidence_gate.eligible``
   （导出证据闸门）口径不同。
7. **输出上限**：``dft_bearing.<category>.items[]`` 与 ``no_dft_record_samples.items[]``
   各自默认 30（硬上限 100）、每催化剂 property_type 前 10、阻断原因前 10；
   截断时给出 total / returned / has_more，且 ``sample_count``/``record_count``/
   ``categories_by_*`` 为全量计数不受截断影响。不返回虚假完成度。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
    ParseJob,
)
from app.db.session import session_scope
from app.mcp.auth import require_mcp_capability
from app.services.dft_export_service import _catalyst_scope
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.review_safety import bulk_export_gate_results

try:  # Python 3.11+
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover - 仅在无 mcp 包环境导入本模块时使用
    ToolAnnotations = None

SCHEMA_VERSION = "paper_processing_status_v1"

DEFAULT_MAX_CATALYSTS = 30
HARD_MAX_CATALYSTS = 100
MAX_PROPERTY_TYPES_PER_CATALYST = 10
MAX_BLOCKED_REASONS = 10
MAX_UNBOUND_RECORD_IDS = 10

# 与 paper_detail 页面 dftCatalystCategory 相同的保守降级：名称形态跨多材料
# （and / series）→ 归属待确认；这是降级而非猜测性归属。
_AMBIGUOUS_NAME_PATTERNS = (re.compile(r"\band\b", re.IGNORECASE), re.compile(r"series", re.IGNORECASE))
TARGET_CATALYST_TYPES = frozenset({"single_atom"})

CATALYST_CATEGORY_LABELS = {
    "target": "目标催化剂",
    "reference": "对照 / 参考材料",
    "pending_confirmation": "归属待确认",
}


def _enforce_postgres_read_only_transaction(session: Any) -> bool:
    """与 app.mcp.server._enforce_postgres_read_only_transaction 语义一致：
    把本查询事务设为只读，保证状态查询零写入。"""
    try:
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            return False
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        return True
    except AttributeError:
        return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_catalyst_binding(row: DFTResult) -> str | None:
    """解析记录的催化剂绑定。列优先，其次 evidence_payload 内的引用路径
    （与 paper_detail 页面 dftCatalystGroupIdentity 的 readPath 口径一致）。"""
    if row.catalyst_sample_id is not None:
        return str(row.catalyst_sample_id)
    payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
    direct = payload.get("catalyst_sample_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    active_site_ref = payload.get("active_site_ref")
    if isinstance(active_site_ref, dict):
        nested = active_site_ref.get("catalyst_sample_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _classify_catalyst(sample: CatalystSample) -> str:
    """与 paper_detail 运行态页面 dftCatalystCategory 一致：
    single_atom → target；名称跨多材料（and/series）→ pending_confirmation；
    其余已绑定 → reference。只降级、不猜测。"""
    catalyst_type = str(sample.catalyst_type or "").strip().lower()
    if catalyst_type in TARGET_CATALYST_TYPES:
        return "target"
    name = str(sample.name or "").strip()
    if any(pattern.search(name) for pattern in _AMBIGUOUS_NAME_PATTERNS):
        return "pending_confirmation"
    return "reference"


def _top_counter(counter: Counter[str], limit: int) -> dict[str, int]:
    """取计数器前 limit 项 + ``__other__`` 汇总（有剩余时）。"""
    out: dict[str, int] = {}
    for key, count in counter.most_common(limit):
        out[str(key)] = int(count)
    remaining = sum(counter.values()) - sum(out.values())
    if remaining > 0:
        out["__other__"] = int(remaining)
    return out


def _dft_record_count_for(session: Session, paper_id_obj: UUID) -> int:
    """该文档自身的 DFT 记录数（仅当前 paper_id，不跨主文/SI 累加）。"""
    return int(
        session.scalar(
            select(func.count()).select_from(DFTResult).where(DFTResult.paper_id == paper_id_obj)
        )
        or 0
    )


def _parse_status_for_document(session: Session, paper: Paper) -> dict[str, Any]:
    section_count = session.scalar(
        select(func.count()).select_from(PaperSection).where(PaperSection.paper_id == paper.id)
    ) or 0
    figure_count = session.scalar(
        select(func.count()).select_from(PaperFigure).where(PaperFigure.paper_id == paper.id)
    ) or 0
    table_count = session.scalar(
        select(func.count()).select_from(PaperTable).where(PaperTable.paper_id == paper.id)
    ) or 0
    latest_job = session.scalars(
        select(ParseJob).where(ParseJob.paper_id == paper.id).order_by(ParseJob.created_at.desc())
    ).first()
    if section_count > 0 or figure_count > 0 or table_count > 0:
        status = "parsed"
    elif latest_job is not None and str(latest_job.status) == "completed":
        status = "parsed_no_extracted_content"
    elif latest_job is not None:
        status = f"parse_job_{latest_job.status}"
    else:
        status = "unknown"
    return {
        "status": status,
        "has_docling_json": bool(paper.docling_json_path),
        "has_markdown": bool(paper.markdown_path),
        "section_count": int(section_count),
        "figure_count": int(figure_count),
        "table_count": int(table_count),
        "latest_parse_job": (
            {
                "id": str(latest_job.id),
                "status": str(latest_job.status),
                "error_message": latest_job.error_message,
                "updated_at": latest_job.updated_at.isoformat() if latest_job.updated_at else None,
            }
            if latest_job is not None
            else None
        ),
        "detail_tool": "get_parse_status",
        "detail_tool_params": {"job_id": str(latest_job.id)} if latest_job is not None else None,
    }


def _chart_review_stage(session: Session, settings: Any, paper_id: UUID) -> dict[str, Any]:
    """图表阶段状态复用 EvidenceReviewBundleService（图表审核权威服务）。"""
    try:
        task = EvidenceReviewBundleService(session, settings).get_review_task(paper_id)
    except Exception as exc:  # 服务失败时如实返回未知，不编造状态
        return {
            "status": "unknown",
            "error": f"chart review service failed: {type(exc).__name__}",
            "detail_tool": "get_chart_review_task",
            "detail_tool_params": {"paper_id": str(paper_id)},
        }
    scope_completion = task.get("scope_completion") or {}
    return {
        "status": str(task.get("stage_status") or "unknown"),
        "apply_ready": bool(task.get("apply_ready")),
        "rag_quality_status": str(task.get("rag_quality_status") or "unknown"),
        "unresolved_count": int(task.get("unresolved_count") or 0),
        "scope_complete": bool(scope_completion.get("complete")),
        "expected_figures": len(scope_completion.get("expected_figure_ids") or []),
        "expected_tables": len(scope_completion.get("expected_table_ids") or []),
        "counts": task.get("counts") or {},
        "reviewed_at": task.get("reviewed_at"),
        "current_snapshot_fingerprint": task.get("current_snapshot_fingerprint"),
        "completed_snapshot_fingerprint": task.get("completed_snapshot_fingerprint"),
        "detail_tool": "get_chart_review_task",
        "detail_tool_params": {"paper_id": str(paper_id)},
    }


def build_paper_processing_status(
    session: Session,
    *,
    paper_id: UUID,
    max_catalysts: int,
) -> dict[str, Any]:
    """组装状态载荷。所有计数均为当前数据库实时聚合，不使用历史快照数字。"""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ValueError("Paper not found")

    # ---------- 文档包（主文 + 明确关联 SI） ----------
    relations = session.scalars(
        select(PaperRelationship).where(
            or_(
                PaperRelationship.source_paper_id == paper_id,
                PaperRelationship.target_paper_id == paper_id,
            ),
            PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
        )
    ).all()
    related_ids: dict[UUID, str] = {}
    for rel in relations:
        other = rel.target_paper_id if rel.source_paper_id == paper_id else rel.source_paper_id
        role = "supplementary" if rel.source_paper_id == paper_id else "parent_main_paper"
        related_ids.setdefault(other, role)

    documents: list[dict[str, Any]] = []
    parse_ok = True
    documents.append(
        {
            "role": "requested",
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "title": paper.title,
            "doi": paper.doi,
            "dft_record_count": _dft_record_count_for(session, paper.id),
            "parse": _parse_status_for_document(session, paper),
        }
    )
    if not documents[0]["parse"]["status"].startswith("parsed"):
        parse_ok = False
    for doc_id, role in sorted(related_ids.items(), key=lambda item: str(item[0])):
        doc_paper = session.get(Paper, doc_id)
        if doc_paper is None:
            continue
        doc_parse = _parse_status_for_document(session, doc_paper)
        if not doc_parse["status"].startswith("parsed"):
            parse_ok = False
        documents.append(
            {
                "role": role,
                "paper_id": str(doc_paper.id),
                "paper_code": doc_paper.paper_code,
                "title": doc_paper.title,
                "doi": doc_paper.doi,
                "dft_record_count": _dft_record_count_for(session, doc_paper.id),
                "parse": doc_parse,
            }
        )

    # ---------- DFT 记录与导出闸门 ----------
    rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
    dft_total = len(rows)
    gates: dict[str, Any] = {}
    if rows:
        gates = bulk_export_gate_results(session, list(rows), target_type="dft_results")

    candidate_status_counts: Counter[str] = Counter()
    property_type_counts: Counter[str] = Counter()
    identity_version_counts: Counter[str] = Counter()
    ml_ready_source_counts: Counter[str] = Counter()
    blocked_reason_counts: Counter[str] = Counter()
    review_gate_status_counts: Counter[str] = Counter()
    eligible_count = 0
    blocked_count = 0
    unit_present = 0
    subject_key_present = 0
    observation_key_present = 0
    ml_ready_at_present = 0
    subject_observations: dict[str, set[str]] = defaultdict(set)

    per_row: list[tuple[DFTResult, str | None, Any]] = []
    for row in rows:
        candidate_status_counts[str(row.candidate_status or "unknown")] += 1
        property_type_counts[str(row.property_type or "unknown")] += 1
        identity_version_counts[str(row.identity_version) if row.identity_version is not None else "none"] += 1
        if row.unit is not None and str(row.unit).strip():
            unit_present += 1
        if row.subject_key:
            subject_key_present += 1
        if row.observation_key:
            observation_key_present += 1
            if row.subject_key:
                subject_observations[row.subject_key].add(row.observation_key)
        if row.ml_ready_at is not None:
            ml_ready_at_present += 1
            ml_ready_source_counts[str(row.ml_ready_source or "unknown")] += 1
        binding = _resolve_catalyst_binding(row)
        per_row.append((row, binding, gates.get(str(row.id))))
        gate = gates.get(str(row.id))
        if gate is not None:
            review_gate_status_counts[str(gate.review_gate_status or "unknown")] += 1
            if gate.eligible:
                eligible_count += 1
            else:
                blocked_count += 1
                for reason in gate.reasons or ():
                    blocked_reason_counts[str(reason)] += 1

    multi_observation_subjects = sum(1 for obs in subject_observations.values() if len(obs) > 1)

    # ---------- 催化剂组织 ----------
    # 分两层：已登记样本（catalyst_samples 表全部）与其中实际承载 DFT 记录的样本。
    # 主概览只把“承载 DFT 性能数据的目标催化剂”当作已提取催化剂数，不混入无数据样本。
    samples = session.scalars(
        select(CatalystSample).where(CatalystSample.paper_id == paper_id).order_by(CatalystSample.id.asc())
    ).all()
    samples_by_id = {str(sample.id): sample for sample in samples}

    catalyst_rows: dict[str, list[tuple[DFTResult, Any]]] = defaultdict(list)
    unbound_records: list[DFTResult] = []
    dangling_bindings: Counter[str] = Counter()
    for row, binding, gate in per_row:
        if binding is None:
            unbound_records.append(row)
        elif binding in samples_by_id:
            catalyst_rows[binding].append((row, gate))
        else:
            # 绑定引用指向不属于本论文的样本：归未绑定，不猜测归属
            unbound_records.append(row)
            dangling_bindings[binding] += 1

    catalyst_items: list[dict[str, Any]] = []
    for sample_id, sample in samples_by_id.items():
        group = catalyst_rows.get(sample_id, [])
        group_status_counts: Counter[str] = Counter()
        group_property_types: Counter[str] = Counter()
        group_blocked_reasons: Counter[str] = Counter()
        group_eligible = 0
        group_blocked = 0
        group_ml_ready = 0
        for row, gate in group:
            group_status_counts[str(row.candidate_status or "unknown")] += 1
            group_property_types[str(row.property_type or "unknown")] += 1
            if row.ml_ready_at is not None:
                group_ml_ready += 1
            if gate is None:
                continue
            if gate.eligible:
                group_eligible += 1
            else:
                group_blocked += 1
                for reason in gate.reasons or ():
                    group_blocked_reasons[str(reason)] += 1
        item = {
            "catalyst_sample_id": sample_id,
            "name": sample.name,
            "catalyst_type": sample.catalyst_type,
            "catalyst_scope": _catalyst_scope(sample.catalyst_type),
            "category": _classify_catalyst(sample),
            "category_label": CATALYST_CATEGORY_LABELS[_classify_catalyst(sample)],
            "metal_centers": sample.metal_centers or [],
            "coordination": sample.coordination,
            "support": sample.support,
            "record_count": len(group),
            "property_types": _top_counter(group_property_types, MAX_PROPERTY_TYPES_PER_CATALYST),
            "candidate_status": dict(group_status_counts),
            "evidence_gate": {
                "eligible": group_eligible,
                "blocked": group_blocked,
                "top_blocked_reasons": _top_counter(group_blocked_reasons, MAX_BLOCKED_REASONS),
            },
            "ml_ready": group_ml_ready,
        }
        catalyst_items.append(item)

    categories_order = ("target", "reference", "pending_confirmation")
    # 已登记样本（含无数据样本）
    registered_by_cat: dict[str, list[dict[str, Any]]] = {c: [] for c in categories_order}
    # 实际承载 DFT 记录的样本（record_count > 0）
    dft_bearing_by_cat: dict[str, list[dict[str, Any]]] = {c: [] for c in categories_order}
    no_record_items: list[dict[str, Any]] = []
    for item in catalyst_items:
        registered_by_cat[item["category"]].append(item)
        if item["record_count"] > 0:
            dft_bearing_by_cat[item["category"]].append(item)
        else:
            no_record_items.append(item)

    returned_cap = max(1, min(int(max_catalysts or DEFAULT_MAX_CATALYSTS), HARD_MAX_CATALYSTS))

    def _capped_category(items: list[dict[str, Any]], category: str) -> dict[str, Any]:
        keep = items[:returned_cap]
        return {
            "label": CATALYST_CATEGORY_LABELS[category],
            "sample_count": len(items),  # 全量计数，不受 cap 影响
            "record_count": sum(i["record_count"] for i in items),
            "returned": len(keep),
            "has_more": len(items) > len(keep),
            "items": keep,
        }

    dft_bearing_categories = {
        c: _capped_category(dft_bearing_by_cat[c], c) for c in categories_order
    }
    no_record_keep = no_record_items[:returned_cap]
    no_record_by_cat = {c: len(registered_by_cat[c]) - len(dft_bearing_by_cat[c]) for c in categories_order}

    total_samples = len(samples)
    unbound_count = len(unbound_records)
    dft_bearing_sample_count = sum(len(v) for v in dft_bearing_by_cat.values())
    no_dft_record_sample_count = len(no_record_items)
    bound_dft_records = dft_total - unbound_count
    target_bearing_dft = len(dft_bearing_by_cat["target"])

    # ---------- 证据核验 / 冲突 / 审计 / 纠错 ----------
    audit_issue_status_counts = dict(
        session.execute(
            select(DFTAuditIssue.status, func.count())
            .where(DFTAuditIssue.paper_id == paper_id)
            .group_by(DFTAuditIssue.status)
        ).all()
    )
    open_audit_issues = sum(
        int(count)
        for status, count in audit_issue_status_counts.items()
        if str(status) != "closed"
    )

    candidate_lifecycle_counts = Counter()
    for status, archived, count in session.execute(
        select(
            ExternalAnalysisCandidate.status,
            ExternalAnalysisCandidate.archived_at.isnot(None),
            func.count(),
        )
        .where(ExternalAnalysisCandidate.paper_id == paper_id)
        .group_by(
            ExternalAnalysisCandidate.status,
            ExternalAnalysisCandidate.archived_at.isnot(None),
        )
    ).all():
        key = f"{status}" + ("_archived" if archived else "_active")
        candidate_lifecycle_counts[key] = int(count)

    external_run_count = session.scalar(
        select(func.count()).select_from(ExternalAnalysisRun).where(ExternalAnalysisRun.paper_id == paper_id)
    ) or 0

    conflicts_by_paper = ReviewConflictAggregationService(session).count_conflicts_by_paper({paper_id})
    field_conflicts = int(conflicts_by_paper.get(paper_id, conflicts_by_paper.get(str(paper_id), 0)) or 0)

    pending_corrections = session.scalar(
        select(func.count())
        .select_from(PaperCorrection)
        .where(PaperCorrection.paper_id == paper_id, PaperCorrection.status == "pending")
    ) or 0

    settings = get_settings()
    chart_stage = _chart_review_stage(session, settings, paper_id)

    # ---------- 未解决项摘要（真实原因，不粉饰） ----------
    unresolved_items: list[dict[str, Any]] = []
    if blocked_count:
        unresolved_items.append(
            {
                "kind": "export_blocked_records",
                "count": blocked_count,
                "reason": "导出闸门未通过（证据/评审/定位器等原因，见 evidence_gate.blocked_reasons）",
                "detail_tool": "get_dft_review_queue",
                "detail_tool_params": {"paper_id": str(paper_id), "status": "needs_review"},
            }
        )
    if open_audit_issues:
        unresolved_items.append(
            {
                "kind": "open_dft_audit_issues",
                "count": open_audit_issues,
                "reason": "存在未关闭的 DFT 审计问题",
                "detail_tool": "get_dft_audit_issues",
                "detail_tool_params": {"paper_id": str(paper_id)},
            }
        )
    if field_conflicts:
        unresolved_items.append(
            {
                "kind": "field_review_conflicts",
                "count": field_conflicts,
                "reason": "多评审来源对同一字段取值不一致，需人工裁决（不得自动处置）",
                "detail_tool": "get_review_conflicts",
                "detail_tool_params": {
                    "paper_id": str(paper_id),
                    "target_type": "dft_results",
                    "include_non_conflicts": False,
                },
            }
        )
    if unbound_count:
        unresolved_items.append(
            {
                "kind": "unbound_catalyst_records",
                "count": unbound_count,
                "reason": "记录未绑定 catalyst_sample_id（或绑定引用指向不存在样本），不按名称猜测归属",
                "detail_tool": "get_dft_review_task",
                "detail_tool_params": {"paper_id": str(paper_id)},
            }
        )
    if chart_stage.get("unresolved_count"):
        unresolved_items.append(
            {
                "kind": "chart_review_unresolved_actions",
                "count": int(chart_stage.get("unresolved_count") or 0),
                "reason": "图表审核存在未解决动作",
                "detail_tool": "get_chart_review_task",
                "detail_tool_params": {"paper_id": str(paper_id)},
            }
        )
    if pending_corrections:
        unresolved_items.append(
            {
                "kind": "pending_corrections",
                "count": int(pending_corrections),
                "reason": "存在待处理的纠错提案",
                "detail_tool": "get_correction_queue",
                "detail_tool_params": {"status": "pending"},
            }
        )

    # ---------- 建议下一步（只引用已注册的真实工具名；缺口如实标注） ----------
    next_steps: list[dict[str, Any]] = []
    if not parse_ok:
        next_steps.append(
            {
                "stage": "pdf_parse",
                "action": "核查解析任务（失败则重新入队，需要 request_parse 能力）",
                "tool": "get_parse_status",
                "tool_params_source": "documents[].parse.latest_parse_job.id",
            }
        )
    if chart_stage.get("status") not in {"completed", "not_required"}:
        next_steps.append(
            {
                "stage": "figure_review",
                "action": "读取图表审核任务并完成本地核验",
                "tool": "get_chart_review_task",
                "tool_params_source": "paper_id",
            }
        )
    if blocked_count or candidate_status_counts.get("system_candidate"):
        next_steps.append(
            {
                "stage": "evidence_verification",
                "action": "读取记录级核验任务（分页主入口；需要 ai_verify_content 能力）",
                "tool": "get_ai_verification_record_tasks",
                "tool_params_source": "paper_id, cursor（响应内分页游标）",
            }
        )
        top_blocked_catalyst = next(
            (item for item in catalyst_items if item["evidence_gate"]["blocked"] > 0),
            None,
        )
        if top_blocked_catalyst is not None:
            next_steps.append(
                {
                    "stage": "evidence_verification",
                    "action": "按催化剂读取核验任务（只读）",
                    "tool": "get_dft_review_task",
                    "tool_params_source": "paper_id + catalysts.dft_bearing.<category>.items[].catalyst_sample_id",
                }
            )
    next_steps.append(
        {
            "stage": "identity_dedup",
            "action": "身份 / 去重检测（只读）",
            "tool": "review_paper_identity",
            "tool_params_source": "paper_id 取自本响应 paper.paper_id（max_items 可选，默认 30，硬上限 200）",
            "side_effects": "只读（PostgreSQL 只读事务）；不写库、不写审计、不落盘",
            "follow_up_tool": "apply_paper_identity_rematerialization",
            "follow_up_params_source": "paper_id + expectations[]（dft_result_id 与当前 identity_version/"
            "subject_key/observation_key 原值，取自 review_paper_identity 响应）+ reason；先用 dry_run=true",
            "note": "返回确认重复（同 observation_key）、同一主体多观测键（**身份键不同不等于科学上无重复**）、"
            "身份缺失（含具体 error_code）、字段级冲突计数、未分类记录与标准化问题，并区分规则检测与原文依据；"
            "需要 read_papers 能力（受控重算入口需要 repair_dft_issues / propose_corrections / review_dft）",
        }
    )
    next_steps.append(
        {
            "stage": "ml_export",
            "action": "任务级（按 task）机器学习数据导出",
            "tool": "export_paper_ml_dataset",
            "tool_params_source": "paper_id 取自本响应 paper.paper_id；task 取自 list_ml_export_tasks "
            "响应的 tasks[].task（list_ml_export_tasks 无需参数，先调它枚举全部已注册任务）",
            "discovery_tool": "list_ml_export_tasks",
            "follow_up_tool": "read_ml_export_artifact",
            "follow_up_params_source": "paper_id / artifact_id（取自导出响应 downloads.artifact.artifact_id）"
            " / fmt（csv|json|manifest）/ offset（utf-8 字节偏移）/ limit_bytes（≤65536）",
            "side_effects": "导出会在受控存储生成 4 个产物文件（dataset.csv / dataset.json / manifest.json / "
            "artifact.json，TTL 默认 24h），并机会式清理本论文名下已过期产物（不可恢复）；"
            "读取已有产物不重新计算、不重新执行 v3 构建器（recomputed=false、v3_builder_invocations=0）",
            "note": "需要 export_data 能力；legacy export_ml_dataset 为全库 v2 旧版入口（无 task 参数）；"
            "REST /api/dft/ml-dataset-v3 存在但 task 仅支持 3 个枚举值，产物读取通道不受该限制",
        }
    )

    # ---------- 组装 ----------
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
            "journal": paper.journal,
            "library_name": paper.library_name,
            "workflow_status": paper.workflow_status,
            "paper_type": paper.paper_type,
            "source_document_count": len(documents),
        },
        "documents": documents,
        "stages": {
            "pdf_parse": {
                "status": "completed" if parse_ok else "incomplete",
                "documents": [
                    {"paper_id": doc["paper_id"], "role": doc["role"], "parse_status": doc["parse"]["status"]}
                    for doc in documents
                ],
            },
            "figure_review": chart_stage,
            "dft_extraction": {
                "status": "extracted" if dft_total else "no_records",
                "total_results": dft_total,
                "candidate_status": dict(candidate_status_counts),
                "external_runs": int(external_run_count),
                "supplementary_candidate_lifecycle": dict(candidate_lifecycle_counts),
                "note": "requires_resolution 属显式处置状态，不计为未处理；pending_* 才是需要处置的候选",
            },
            "evidence_verification": {
                "status": "未确认" if not dft_total else ("has_blocked" if blocked_count else "all_eligible"),
                "evidence_gate": {
                    "eligible": eligible_count,
                    "blocked": blocked_count,
                    "blocked_reasons": _top_counter(blocked_reason_counts, MAX_BLOCKED_REASONS),
                },
                "review_gate_status": dict(review_gate_status_counts),
                "audit_issues": {
                    "open": open_audit_issues,
                    "by_status": {str(k): int(v) for k, v in audit_issue_status_counts.items()},
                },
                "field_conflicts": field_conflicts,
                "ml_ready_materialized": ml_ready_at_present,
                "note": "candidate_status 是材料化状态，evidence_gate 是导出证据闸门，两者口径不同；"
                "科学验收（人工裁决）状态不由本工具判定",
            },
            "identity_dedup": {
                "status": "未确认",
                "identity_version": dict(identity_version_counts),
                "subject_key_present": subject_key_present,
                "observation_key_present": observation_key_present,
                "multi_observation_subjects": multi_observation_subjects,
                "detail_tool": "review_paper_identity",
                "note": "身份键存在不等于科学去重完成；逐条检测（确认重复 / 同一主体多观测键 / 身份缺失 / "
                "冲突 / 归类 / 标准化）见 review_paper_identity（只读）；受控身份重算见 "
                "apply_paper_identity_rematerialization",
            },
            "standardization": {
                "status": "未确认" if not dft_total else "partial_info",
                "unit_present": unit_present,
                "unit_missing": dft_total - unit_present,
                "ml_ready_at": ml_ready_at_present,
                "ml_ready_source": dict(ml_ready_source_counts),
                "ml_ready_field": "dft_results.ml_ready_at IS NOT NULL（提取/核验流程的材料化时间戳）",
                "note": "ml_ready_at 来自 dft_results.ml_ready_at 字段（材料化标记），不等于任务级就绪；"
                "任务级就绪需按 task 的专用导出入口计算（见 next_steps 的 export_paper_ml_dataset，"
                "本工具不计算）；"
                "与 evidence_gate.eligible（导出证据闸门）口径不同；"
                "单位非空不等于单位原文已核验",
            },
            "ml_export": {
                "status": "未确认",
                "evidence_gate_eligible": eligible_count,
                "task_level_readiness_computed": False,
                "detail_tool": "export_paper_ml_dataset",
                "note": "证据闸门可导出数（eligible）不等于任务就绪；任务级就绪由 export_paper_ml_dataset "
                "按 task 计算（本工具不计算任务级就绪数）",
                "legacy_tool": "export_ml_dataset",
                "legacy_tool_note": "全库 v2 旧版导出入口（无 task 参数）",
            },
        },
        "catalysts": {
            "grouping": "catalyst_sample_id（稳定 UUID），绝不按名称合并；缺失绑定归 unbound_records",
            "classification_basis": "catalyst_type 字段 + 保守名称形态降级（and/series → 归属待确认），"
            "与 paper_detail 页面 dftCatalystCategory 口径一致；不凭名称猜测分类",
            "target_catalyst_types": sorted(TARGET_CATALYST_TYPES),
            "registered_sample_count": total_samples,
            "dft_bearing_sample_count": dft_bearing_sample_count,
            "no_dft_record_sample_count": no_dft_record_sample_count,
            "unbound_record_count": unbound_count,
            "total_dft_records": dft_total,
            "bound_dft_records": bound_dft_records,
            "headline": f"承载 DFT 性能数据的目标催化剂 = {target_bearing_dft}（不含无数据样本；"
            f"已登记目标样本 {len(registered_by_cat['target'])}，其中 {no_record_by_cat['target']} 个无 DFT 记录）",
            "categories_by_registered_samples": {
                c: len(registered_by_cat[c]) for c in categories_order
            },
            "categories_by_dft_bearing_samples": {
                c: len(dft_bearing_by_cat[c]) for c in categories_order
            },
            "dft_bearing": {
                "target": dft_bearing_categories["target"],
                "reference": dft_bearing_categories["reference"],
                "pending_confirmation": {
                    **dft_bearing_categories["pending_confirmation"],
                    "unbound_records": {
                        "record_count": unbound_count,
                        "sample_ids": [],
                        "record_ids": [str(row.id) for row in unbound_records[:MAX_UNBOUND_RECORD_IDS]],
                        "has_more_record_ids": unbound_count > MAX_UNBOUND_RECORD_IDS,
                        "dangling_binding_references": dict(dangling_bindings),
                        "reason": "未绑定 catalyst_sample_id 或绑定引用无效；不按名称猜测归属",
                    },
                },
            },
            "no_dft_record_samples": {
                "count": no_dft_record_sample_count,
                "by_category": no_record_by_cat,
                "returned": len(no_record_keep),
                "has_more": no_dft_record_sample_count > len(no_record_keep),
                "items": no_record_keep,
                "note": "已登记但无 DFT 记录承载的样本（多为吸附物特定变体未物化为 DFT 行）；不计入已提取催化剂数",
            },
            "how_to_inspect": "get_dft_review_task(paper_id, catalyst_sample_id=<id>) 查看单催化剂核验任务；"
            "dft_bearing.<category>.items[] 仅含实际承载 DFT 记录的样本",
        },
        "property_types_overall": _top_counter(property_type_counts, 15),
        "unresolved": {
            "counts": {item["kind"]: item["count"] for item in unresolved_items},
            "items": unresolved_items,
        },
        "next_steps": next_steps,
        "limits": {
            "max_catalysts_requested": returned_cap,
            "hard_max_catalysts": HARD_MAX_CATALYSTS,
            "property_types_per_catalyst": MAX_PROPERTY_TYPES_PER_CATALYST,
            "blocked_reasons": MAX_BLOCKED_REASONS,
            "note": "dft_bearing.<category>.items[] 与 no_dft_record_samples.items[] 各自最多返回 max_catalysts 条；"
            "sample_count/record_count/categories_by_* 为全量计数，不受截断影响；"
            "被截断时 has_more=true，用 get_dft_review_task(paper_id, catalyst_sample_id) 查看单催化剂详情",
        },
    }
    return payload


def register_paper_progress_tools(server: Any) -> dict[str, str]:
    """把单篇状态工具注册到给定 FastMCP 实例。

    候选阶段由候选版 server.py 调用；生产接入时在 mcp_http_app 构建前后
    任意模块导入期调用一次均可（工具注册发生在 streamable_http_app 构建之后
    同样生效，与现有 68 个工具的注册时序一致）。
    """

    description = (
        "Read-only processing status for one paper, organized by stable catalyst_sample_id. "
        "Covers: main paper + explicitly linked SI parse status, figure review stage, DFT extraction, "
        "evidence verification gate, identity/dedup stored state, standardization materialization, "
        "and ML export readiness. Distinguishes candidate status, evidence gate, task readiness and "
        "scientific acceptance; identity keys present does NOT mean dedup complete; unit present does "
        "NOT mean unit verified against source; ml_ready_at (materialization timestamp) is NOT task-level "
        "readiness (task-level readiness is computed by export_paper_ml_dataset, not by this tool). "
        "Catalysts are split into registered samples vs DFT-bearing samples (record_count>0); the headline "
        "count is 'target catalysts bearing DFT performance data', excluding no-data samples. Returns "
        "target / reference (control) / pending_confirmation categories, no_dft_record_samples, and "
        "unbound_records. Output is capped (total/returned/has_more; summary counts are full, not truncated). "
        "Does not rebuild training sets, load PDFs, or advance any workflow. Requires capability: read_papers."
    )

    annotations = None
    if ToolAnnotations is not None:
        annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    @server.tool(
        name="get_paper_processing_status",
        title="按催化剂组织的单篇处理状态",
        description=description,
        annotations=annotations,
    )
    def get_paper_processing_status(
        paper_id: str,
        max_catalysts: int = DEFAULT_MAX_CATALYSTS,
    ) -> dict[str, Any]:
        require_mcp_capability("read_papers")
        try:
            pid = UUID(str(paper_id or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid paper_id: expected a UUID string") from exc
        requested = max(1, min(int(max_catalysts or DEFAULT_MAX_CATALYSTS), HARD_MAX_CATALYSTS))
        settings = get_settings()
        with session_scope(settings.database_url) as session:
            _enforce_postgres_read_only_transaction(session)
            return build_paper_processing_status(session, paper_id=pid, max_catalysts=requested)

    return {"get_paper_processing_status": "registered"}
