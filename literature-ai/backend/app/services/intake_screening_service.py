"""intake_screening_service.py — Phase 4: AI 筛选最小可用版

职责：
  接受一批检索原始结果 + 用户研究需求，输出 ScreeningResult 列表。
  每个 ScreeningResult 包含：
    - relevance_score (0~1)
    - screening_tier  (recommended / maybe / weak)
    - screening_reason (可解释文本)
    - risk_flags (列表)

筛选层次：
  1. 规则评分：关键词命中、年份、期刊、PDF 可得性、去重。
  2. LLM 增强（可选）：有 Writer LLM 配置时生成 screening_reason；否则 fallback 到规则理由。

注意：本服务不写数据库，只返回评分结果；由 intake API 负责落库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper
from app.utils.library_names import build_library_name_clause


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

VALID_TIERS = {"recommended", "maybe", "weak"}


@dataclass
class ScreeningResult:
    identifier: str
    relevance_score: float
    screening_tier: str          # recommended / maybe / weak
    screening_reason: str
    risk_flags: list[str] = field(default_factory=list)
    # 去重信息
    is_duplicate: bool = False
    duplicate_paper_id: str | None = None


# ---------------------------------------------------------------------------
# 规则评分
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or",
    "is", "are", "was", "were", "with", "that", "this", "at", "by",
}


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 1}


def _keyword_overlap_score(query_tokens: set[str], text: str) -> float:
    """标题/摘要关键词命中率，返回 0~1。"""
    if not text or not query_tokens:
        return 0.0
    text_tokens = _tokenize(text)
    hits = query_tokens & text_tokens
    return min(1.0, len(hits) / max(1, len(query_tokens)))


def _score_candidate(
    item: dict[str, Any],
    query_tokens: set[str],
    *,
    target_types: list[str] | None = None,
    preferred_year_min: int | None = None,
    preferred_year_max: int | None = None,
) -> tuple[float, list[str]]:
    """对单篇候选计算规则评分，返回 (score 0~1, risk_flags)。"""
    score = 0.0
    risk_flags: list[str] = []

    title = str(item.get("title") or "")
    abstract = str(item.get("abstract") or "")
    year = item.get("year")
    pdf_url = item.get("pdf_url") or item.get("oa_url")
    doi = item.get("doi")

    # 1. 标题命中（权重 0.45）
    title_score = _keyword_overlap_score(query_tokens, title)
    score += title_score * 0.45

    # 2. 摘要命中（权重 0.30）
    if abstract:
        abs_score = _keyword_overlap_score(query_tokens, abstract)
        score += abs_score * 0.30
    else:
        risk_flags.append("no_abstract")

    # 3. 年份偏好（权重 0.10）
    if year:
        if preferred_year_min and year < preferred_year_min:
            score += 0.02
            risk_flags.append("older_than_preferred")
        elif preferred_year_max and year > preferred_year_max:
            score += 0.05
        else:
            score += 0.10

    # 4. DOI 存在（权重 0.08）
    if doi:
        score += 0.08
    else:
        risk_flags.append("no_doi")

    # 5. PDF 可得性（不作为主依据，只标 flag）
    if not pdf_url:
        risk_flags.append("pdf_unavailable")

    # 6. 目标类型匹配（权重 0.07）
    if target_types:
        item_type = str(item.get("paper_type") or item.get("type") or "").lower()
        if any(t.lower() in item_type or item_type in t.lower() for t in target_types):
            score += 0.07
        else:
            risk_flags.append("type_mismatch")

    return min(1.0, max(0.0, score)), risk_flags


def _tier_from_score(score: float) -> str:
    if score >= 0.60:
        return "recommended"
    elif score >= 0.35:
        return "maybe"
    else:
        return "weak"


def _rule_reason(
    item: dict[str, Any],
    score: float,
    tier: str,
    risk_flags: list[str],
    query_tokens: set[str],
) -> str:
    """用中文生成可解释的规则筛选理由。"""
    title = item.get("title") or "（未知标题）"
    parts: list[str] = []

    title_tokens = _tokenize(str(title))
    hits = query_tokens & title_tokens
    if hits:
        parts.append(f"标题命中关键词：{', '.join(sorted(hits)[:5])}")
    else:
        parts.append("标题与研究需求关键词无直接命中")

    if item.get("abstract"):
        abs_tokens = _tokenize(str(item["abstract"]))
        abs_hits = query_tokens & abs_tokens
        if abs_hits:
            parts.append(f"摘要命中：{', '.join(sorted(abs_hits)[:5])}")

    flag_msgs = {
        "no_abstract": "无摘要，相关性评估受限",
        "no_doi": "无 DOI，去重可靠性降低",
        "pdf_unavailable": "PDF 暂不可直接获取（不影响元数据入库）",
        "older_than_preferred": "年份早于偏好范围",
        "type_mismatch": "文献类型与目标类型不完全匹配",
    }
    for flag in risk_flags:
        if flag in flag_msgs:
            parts.append(flag_msgs[flag])

    tier_label = {"recommended": "推荐", "maybe": "待定", "weak": "弱相关"}[tier]
    reason = f"[{tier_label}，得分 {score:.2f}] " + "；".join(parts) + "。"
    return reason


# ---------------------------------------------------------------------------
# 去重检测
# ---------------------------------------------------------------------------

def _detect_duplicate(
    session: Session,
    doi: str | None,
    title: str | None,
    library_name: str | None = None,
) -> str | None:
    """在 papers 表中检测是否有重复条目，返回已有 paper_id（字符串）或 None。"""
    if doi:
        doi_clean = doi.strip().lower().lstrip("https://doi.org/").lstrip("doi:")
        stmt = select(Paper.id).where(Paper.doi.ilike(f"%{doi_clean}%"))
        if library_name:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, library_name))
        stmt = stmt.limit(1)
        result = session.scalar(stmt)
        if result:
            return str(result)
    if title:
        # 模糊标题去重：取前 60 字符做 ilike
        title_prefix = title.strip()[:60]
        stmt = select(Paper.id).where(Paper.title.ilike(f"%{title_prefix}%"))
        if library_name:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, library_name))
        stmt = stmt.limit(1)
        result = session.scalar(stmt)
        if result:
            return str(result)
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

class IntakeScreeningService:
    """候选文献规则筛选服务（MVP 版，无需外部 AI 调用）。

    可选 LLM 增强：如果传入 llm_client（符合 llm_service 接口），
    会尝试为 recommended/maybe 候选生成更丰富的 screening_reason。
    失败时自动 fallback 到规则理由，不抛出异常。
    """

    def __init__(
        self,
        session: Session,
        *,
        llm_client=None,
        library_name: str | None = None,
    ) -> None:
        self._session = session
        self._llm = llm_client
        self._library_name = library_name

    def screen(
        self,
        items: list[dict[str, Any]],
        *,
        user_need: str,
        query: str,
        target_types: list[str] | None = None,
        preferred_year_min: int | None = None,
        preferred_year_max: int | None = None,
    ) -> list[ScreeningResult]:
        """对检索结果列表进行 AI/规则筛选，返回带评分的结果列表。

        结果按 relevance_score 降序排列。
        """
        combined_text = f"{user_need} {query}"
        query_tokens = _tokenize(combined_text)

        results: list[ScreeningResult] = []

        for item in items:
            identifier = (
                item.get("doi")
                or item.get("identifier")
                or item.get("url")
                or item.get("title")
                or ""
            )
            doi = item.get("doi")
            title = item.get("title")

            # 1. 去重
            dup_id = _detect_duplicate(self._session, doi, title, self._library_name)
            is_dup = dup_id is not None

            # 2. 规则评分
            score, risk_flags = _score_candidate(
                item,
                query_tokens,
                target_types=target_types,
                preferred_year_min=preferred_year_min,
                preferred_year_max=preferred_year_max,
            )
            if is_dup:
                risk_flags.append("possible_duplicate")
                score = max(0.0, score - 0.15)

            tier = _tier_from_score(score)

            # 3. 生成理由（LLM 优先，fallback 到规则）
            reason = self._generate_reason(
                item, score, tier, risk_flags, query_tokens, user_need=user_need
            )

            results.append(
                ScreeningResult(
                    identifier=str(identifier),
                    relevance_score=round(score, 4),
                    screening_tier=tier,
                    screening_reason=reason,
                    risk_flags=risk_flags,
                    is_duplicate=is_dup,
                    duplicate_paper_id=dup_id,
                )
            )

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results

    def _generate_reason(
        self,
        item: dict[str, Any],
        score: float,
        tier: str,
        risk_flags: list[str],
        query_tokens: set[str],
        *,
        user_need: str,
    ) -> str:
        """生成筛选理由；有 LLM 时增强，否则纯规则。"""
        rule_reason = _rule_reason(item, score, tier, risk_flags, query_tokens)
        if not self._llm or tier == "weak":
            return rule_reason
        try:
            title = item.get("title") or "未知标题"
            abstract_snippet = (item.get("abstract") or "")[:300]
            prompt = (
                f"研究需求：{user_need}\n"
                f"论文标题：{title}\n"
                f"摘要片段：{abstract_snippet}\n"
                f"规则评分：{score:.2f}（{tier}）\n"
                "请用1-2句话简洁说明该论文与研究需求的相关性，以及是否值得纳入文献库。"
            )
            llm_reason = self._llm.generate(prompt, max_tokens=120)
            if llm_reason and len(llm_reason.strip()) > 10:
                return f"[LLM] {llm_reason.strip()}"
        except Exception:
            pass
        return rule_reason
