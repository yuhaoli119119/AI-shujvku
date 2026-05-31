from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from app.utils.paper_type import normalize_paper_type_filter


class PaperWriterPromptBuilder:
    """Loads writer prompt config and prepares backend-ready prompt payloads."""

    SECTION_EVIDENCE_ORDER: dict[str, list[str]] = {
        "outline": ["writing_cards", "mechanism_claims", "dft_results", "electrochemical_performance"],
        "introduction": ["writing_cards", "mechanism_claims", "sections"],
        "dft_results": ["dft_results", "mechanism_claims", "sections"],
        "discussion": ["mechanism_claims", "dft_results", "electrochemical_performance", "writing_cards"],
        "figure_storyline": ["writing_cards", "dft_results", "electrochemical_performance", "mechanism_claims"],
    }

    def __init__(self, prompt_path: Path | None = None) -> None:
        self.prompt_path = prompt_path or Path(__file__).resolve().parents[3] / "prompts" / "paper_writer.yaml"
        self.config = self._load_config()

    def build(
        self,
        topic: str,
        user_notes: str | None,
        requested_sections: list[str],
        retrieved: dict[str, list[dict[str, Any]]],
        target_paper_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "instruction": self.config.get("instruction", ""),
            "style": self.config.get("style", {}),
            "section_specs": {name: self.config.get("sections", {}).get(name, {}) for name in requested_sections},
            "guardrails": self.config.get("guardrails", []),
            "llm_output_contract": self.config.get("llm_output_contract", {}),
            "topic": topic,
            "user_notes": user_notes,
            "requested_sections": requested_sections,
            "retrieved": retrieved,
            "evidence_pack": self._build_evidence_pack(requested_sections, retrieved),
            "numeric_guardrails": self._build_numeric_guardrails(retrieved),
            "target_paper_type": target_paper_type,
        }

    # 论文分类与对应写作指导
    _TYPE_HINTS: dict[str, str] = {
        "A": "纯计算/理论论文（DFT/MD/Monte Carlo 等），侧重计算方法、结果与机理阐释",
        "B": "计算+实验混合论文，需兼顾计算预测与实验验证的对应关系",
        "C": "纯实验论文，侧重实验设计、材料表征与性能对比分析",
        "R": "综述论文，需全面覆盖领域进展、系统对比不同方法、指出挑战与前景",
    }

    def render_messages(self, prompt_payload: dict[str, Any], rule_sections: dict[str, Any]) -> list[dict[str, str]]:
        system = "\n".join(
            filter(
                None,
                [
                    prompt_payload.get("instruction"),
                    f"Style: {prompt_payload.get('style', {})}",
                    f"Guardrails: {prompt_payload.get('guardrails', [])}",
                    f"Output contract: {prompt_payload.get('llm_output_contract', {})}",
                ],
            )
        )

        # 注入论文分类上下文到 system prompt
        target_paper_type = prompt_payload.get("target_paper_type")
        normalized_filter = normalize_paper_type_filter(target_paper_type)
        if target_paper_type:
            type_key = normalized_filter[0] if normalized_filter else None
            hint = self._TYPE_HINTS.get(type_key, "")
            if hint:
                system += f"\n\n[论文类型上下文] 本篇目标论文分类: {target_paper_type}。{hint}"

        user = yaml.safe_dump(
            self._json_safe(
                {
                    "topic": prompt_payload.get("topic"),
                    "user_notes": prompt_payload.get("user_notes"),
                    "requested_sections": prompt_payload.get("requested_sections"),
                    "section_specs": prompt_payload.get("section_specs"),
                    "evidence_pack": prompt_payload.get("evidence_pack"),
                    "numeric_guardrails": prompt_payload.get("numeric_guardrails"),
                    "target_paper_type": prompt_payload.get("target_paper_type"),
                    "rule_draft_seed": rule_sections,
                }
            ),
            allow_unicode=True,
            sort_keys=False,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _load_config(self) -> dict[str, Any]:
        if self.prompt_path.exists():
            with open(self.prompt_path, "r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        return {}

    def _build_evidence_pack(
        self,
        requested_sections: list[str],
        retrieved: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        pack: dict[str, list[dict[str, Any]]] = {}
        for section_name in requested_sections:
            items: list[dict[str, Any]] = []
            for evidence_type in self.SECTION_EVIDENCE_ORDER.get(section_name, []):
                typed_items = retrieved.get(evidence_type, [])
                sorted_items = sorted(typed_items, key=lambda x: x.get("score", 0), reverse=True)
                for item in sorted_items[:4]:
                    items.append(self._compact_item(evidence_type, item))
            items = self._rank_and_dedup(items)
            items = self._round_robin_by_paper(items)
            pack[section_name] = items[:8]
        return pack

    @staticmethod
    def _rank_and_dedup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_values: set[str] = set()
        seen_summaries: set[str] = set()
        numeric_first: list[dict[str, Any]] = []
        descriptive: list[dict[str, Any]] = []
        for item in items:
            num_key = frozenset(item.get("numeric_values") or [])
            if num_key:
                if num_key in seen_values:
                    continue
                seen_values.add(num_key)
                numeric_first.append(item)
            else:
                summary = (item.get("summary") or "")[:120].lower()
                if summary in seen_summaries:
                    continue
                seen_summaries.add(summary)
                descriptive.append(item)
        numeric_first.sort(key=lambda x: x.get("score") or 0, reverse=True)
        descriptive.sort(key=lambda x: x.get("score") or 0, reverse=True)
        return numeric_first + descriptive

    @staticmethod
    def _round_robin_by_paper(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reorder items so the same paper_id does not appear consecutively.

        Uses a round-robin strategy: group items by paper_id (each group
        pre-sorted by score descending), then interleave groups by picking
        the next item from the group with the highest next-score, skipping
        groups whose last-picked index would make them consecutive with
        the previously emitted item.
        """
        if not items:
            return items

        # Group by paper_id preserving score-descending order within each group
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []  # track first-seen order of paper_ids
        for item in items:
            pid = str(item.get("paper_id") or "")
            if pid not in groups:
                groups[pid] = []
                order.append(pid)
            groups[pid].append(item)

        # Each group is already score-sorted; build cursor map
        cursors: dict[str, int] = {pid: 0 for pid in groups}
        result: list[dict[str, Any]] = []
        last_pid: str | None = None

        while True:
            # Pick the group with the highest next-item score that isn't the same as last_pid
            best_pid: str | None = None
            best_score: float = -1.0
            for pid in order:
                idx = cursors.get(pid, 0)
                if idx >= len(groups[pid]):
                    continue
                # If this pid was just emitted, try to pick another first
                if pid == last_pid and len([p for p in order if cursors.get(p, 0) < len(groups.get(p, []))]) > 1:
                    continue
                score = groups[pid][idx].get("score") or 0.0
                if score > best_score:
                    best_score = score
                    best_pid = pid

            # If all remaining groups are the same as last_pid, allow it
            if best_pid is None:
                for pid in order:
                    idx = cursors.get(pid, 0)
                    if idx < len(groups[pid]):
                        score = groups[pid][idx].get("score") or 0.0
                        if score > best_score:
                            best_score = score
                            best_pid = pid

            if best_pid is None:
                break

            result.append(groups[best_pid][cursors[best_pid]])
            cursors[best_pid] += 1
            last_pid = best_pid

        return result

    def _build_numeric_guardrails(self, retrieved: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        guardrails: list[dict[str, Any]] = []
        for evidence_type in ["dft_results", "electrochemical_performance", "mechanism_claims", "sections"]:
            for item in retrieved.get(evidence_type, [])[:10]:
                numeric_values = self._extract_numeric_literals(item)
                if not numeric_values:
                    continue
                guardrails.append(
                    {
                        "source_type": evidence_type,
                        "paper_id": self._json_safe(item.get("paper_id")),
                        "numeric_values": numeric_values,
                        "evidence_excerpt": (item.get("evidence_text") or item.get("text") or "")[:240],
                    }
                )
        return guardrails[:20]

    def _compact_item(self, evidence_type: str, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_type": evidence_type,
            "paper_id": self._json_safe(item.get("paper_id")),
            "score": item.get("score"),
            "summary": self._summarize_item(evidence_type, item),
            "evidence_excerpt": (item.get("evidence_text") or item.get("text") or "")[:320],
            "numeric_values": self._extract_numeric_literals(item),
        }

    def _summarize_item(self, evidence_type: str, item: dict[str, Any]) -> str:
        if evidence_type == "dft_results":
            descriptor = item.get("property_type") or "descriptor"
            adsorbate = item.get("adsorbate") or "intermediate"
            value = item.get("value")
            unit = item.get("unit") or ""
            return f"{adsorbate}: {descriptor}" if value is None else f"{adsorbate}: {descriptor} = {value} {unit}".strip()
        if evidence_type == "electrochemical_performance":
            bits = []
            if item.get("capacity_value") is not None:
                bits.append(f"capacity {item['capacity_value']} mAh/g")
            if item.get("rate"):
                bits.append(f"rate {item['rate']}")
            if item.get("cycle_number") is not None:
                bits.append(f"{item['cycle_number']} cycles")
            return ", ".join(bits) or "electrochemical evidence"
        if evidence_type == "mechanism_claims":
            return item.get("text") or "mechanism claim"
        if evidence_type == "writing_cards":
            return " | ".join(
                filter(None, [item.get("research_gap"), item.get("proposed_solution"), item.get("core_hypothesis")])
            )[:240] or "writing guidance"
        if evidence_type == "sections":
            title = item.get("section_title") or item.get("section_type") or "section"
            return f"{title}: {(item.get('text') or '')[:180]}".strip()
        return (item.get("text") or item.get("evidence_text") or "")[:240]

    @staticmethod
    def _extract_numeric_literals(item: dict[str, Any]) -> list[str]:
        literals: list[str] = []
        value = item.get("value")
        unit = item.get("unit")
        if value is not None and unit:
            literals.append(f"{value} {unit}".strip())
        if item.get("capacity_value") is not None:
            literals.append(f"{item['capacity_value']} mAh/g")
        if item.get("rate"):
            literals.append(str(item["rate"]))
        if item.get("cycle_number") is not None:
            literals.append(f"{item['cycle_number']} cycles")
        return literals[:6]

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, dict):
            return {str(k): self._json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._json_safe(item) for item in value]
        return value
