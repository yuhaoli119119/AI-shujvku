from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


PROJECT_LIBRARY_CONTEXT_VERSION = "project_library_contexts_v1"
UNKNOWN_PROJECT_LIBRARY_CONTEXT = "UNKNOWN"


@dataclass(frozen=True)
class ProjectLibraryContext:
    key: str
    version: str
    status: str
    display_name_zh: str
    display_name_en: str
    default_library_name: str | None
    summary: str
    reaction_types: tuple[str, ...]
    tabular_tasks: tuple[str, ...]
    semantic_focus_terms: tuple[str, ...]
    catalyst_scope_terms: tuple[str, ...]
    intermediate_terms: tuple[str, ...]
    structure_terms: tuple[str, ...]
    prompt_hints: tuple[str, ...]
    applies_to: tuple[str, ...]
    unknown_strategy: str


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").strip().lower())


_CONTEXTS = MappingProxyType(
    {
        "li_s_sac_dac": ProjectLibraryContext(
            key="li_s_sac_dac",
            version=PROJECT_LIBRARY_CONTEXT_VERSION,
            status="candidate",
            display_name_zh="锂硫双原子",
            display_name_en="Li-S SAC/DAC Project Library",
            default_library_name="锂硫双原子",
            summary=(
                "面向锂硫电池单/双原子催化剂项目库的只读上下文，"
                "优先服务 SRR_LiS 表格型机器学习与后续审核复用。"
            ),
            reaction_types=("SRR_LiS",),
            tabular_tasks=("SRR_LiS:adsorption_energy", "SRR_LiS:reaction_barrier"),
            semantic_focus_terms=(
                "Li-S",
                "lithium-sulfur",
                "SRR_LiS",
                "sulfur reduction reaction",
                "LiPS",
                "lithium polysulfide",
                "Li2S",
                "single atom",
                "dual atom",
                "SAC",
                "DAC",
            ),
            catalyst_scope_terms=(
                "single atom catalyst",
                "dual atom catalyst",
                "single-atom catalyst",
                "dual-atom catalyst",
                "heteronuclear",
                "homonuclear",
            ),
            intermediate_terms=("S8", "Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S"),
            structure_terms=(
                "metal center",
                "coordination environment",
                "support",
                "M-M distance",
                "active site",
            ),
            prompt_hints=(
                "优先保留 Li-S/SRR_LiS 语境，不把该项目库误解为全局唯一数据库模式。",
                "遇到字段缺失或证据不足时保持 UNKNOWN/null，不自动升级 verified/safe_verified。",
                "后续解析、审核、筛选、导出可复用本上下文，但本层仅提供只读配置。",
            ),
            applies_to=("parsing", "review", "filtering", "export"),
            unknown_strategy="preserve_unknown_or_null",
        )
    }
)

_ALIASES = {
    _token("li_s_sac_dac"): "li_s_sac_dac",
    _token("Li-S SAC/DAC"): "li_s_sac_dac",
    _token("lithium sulfur sac dac"): "li_s_sac_dac",
    _token("锂硫双原子"): "li_s_sac_dac",
    _token("锂硫单双原子"): "li_s_sac_dac",
}


def normalize_project_library_context(value: Any) -> str:
    return _ALIASES.get(_token(value), UNKNOWN_PROJECT_LIBRARY_CONTEXT)


def get_project_library_context(value: Any) -> ProjectLibraryContext:
    key = normalize_project_library_context(value)
    if key == UNKNOWN_PROJECT_LIBRARY_CONTEXT:
        raise KeyError(f"Unknown project library context: {value!r}")
    return _CONTEXTS[key]


def list_project_library_contexts() -> tuple[ProjectLibraryContext, ...]:
    return tuple(_CONTEXTS[key] for key in sorted(_CONTEXTS))


def build_project_library_context_payload() -> dict[str, dict[str, object]]:
    return {
        item.key: {
            "key": item.key,
            "version": item.version,
            "status": item.status,
            "display_name_zh": item.display_name_zh,
            "display_name_en": item.display_name_en,
            "default_library_name": item.default_library_name,
            "summary": item.summary,
            "reaction_types": list(item.reaction_types),
            "tabular_tasks": list(item.tabular_tasks),
            "semantic_focus_terms": list(item.semantic_focus_terms),
            "catalyst_scope_terms": list(item.catalyst_scope_terms),
            "intermediate_terms": list(item.intermediate_terms),
            "structure_terms": list(item.structure_terms),
            "prompt_hints": list(item.prompt_hints),
            "applies_to": list(item.applies_to),
            "unknown_strategy": item.unknown_strategy,
        }
        for item in list_project_library_contexts()
    }


__all__ = [
    "PROJECT_LIBRARY_CONTEXT_VERSION",
    "UNKNOWN_PROJECT_LIBRARY_CONTEXT",
    "ProjectLibraryContext",
    "build_project_library_context_payload",
    "get_project_library_context",
    "list_project_library_contexts",
    "normalize_project_library_context",
]
