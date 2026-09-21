"""反应类型 × 维度能力矩阵.

这个模块把「我们到底稳定支持什么」写成可执行、可测试的单一事实来源，避免
把 experimental 的反应体系当成 production 能力对外宣称。

三个维度彼此独立，必须分开声明：

1. ``reaction_type`` —— 反应体系（SRR_LiS / HER / OER / ORR / CO2RR / UNKNOWN）。
   只有 SRR_LiS 是 production；其余仍是 experimental，既没有完整的 tabular
   task profile，也没有对应的 ML 导出回归测试，因此不得宣称稳定支持。
2. ``material`` —— 材料/载体身份（graphdiyne、graphene、C2N …）。材料永远
   不是 reaction_type；它属于 catalyst/material 维度。
3. ``ml_export`` —— 是否具备可用的 ML 导出画像与门禁。没有 task profile 的
   性质只能停留在「字段已核验、待 ML 就绪」，不得进入 ML 数据集。
"""

from __future__ import annotations

from typing import Any

from app.domain.reaction_taxonomy import MATERIAL_DIMENSION_TERMS, REACTION_TYPES
from app.domain.tabular_task_profiles import list_tabular_task_profiles

CAPABILITY_MATRIX_VERSION = "capability_matrix_v1"

#: 反应体系能力声明。``ml_export`` 只有在同时具备 task profile 与对应导出
#: 回归测试时才允许是 "supported"。
REACTION_CAPABILITIES: dict[str, dict[str, Any]] = {
    "SRR_LiS": {
        "status": "production",
        "ml_export": "supported",
        "requires_task_profile": True,
        "notes": "锂硫双原子主库；吸附能/RDS 自由能/反应能垒/结构/电子描述符均有 task profile。",
    },
    "HER": {
        "status": "experimental",
        "ml_export": "not_supported",
        "requires_task_profile": True,
        "notes": "只有反应画像与中间体校验，没有 tabular task profile 与 ML 导出回归测试。",
    },
    "OER": {
        "status": "experimental",
        "ml_export": "not_supported",
        "requires_task_profile": True,
        "notes": "同上；不得对宣称 ML 数据集稳定支持。",
    },
    "ORR": {
        "status": "experimental",
        "ml_export": "not_supported",
        "requires_task_profile": True,
        "notes": "同上；不得对宣称 ML 数据集稳定支持。",
    },
    "CO2RR": {
        "status": "experimental",
        "ml_export": "not_supported",
        "requires_task_profile": True,
        "notes": "同上；不得对宣称 ML 数据集稳定支持。",
    },
    "UNKNOWN": {
        "status": "quarantine",
        "ml_export": "not_supported",
        "requires_task_profile": True,
        "notes": "未分类/歧义记录，一律不进入 ML 导出。",
    },
}


def build_capability_matrix() -> dict[str, Any]:
    """构造当前真实能力矩阵（从注册表推导，不写死乐观值）。"""
    profile_reactions: dict[str, list[str]] = {}
    for profile in list_tabular_task_profiles():
        profile_reactions.setdefault(profile.reaction_type, []).append(profile.key)

    reactions: dict[str, dict[str, Any]] = {}
    for reaction_type in REACTION_TYPES:
        declared = dict(REACTION_CAPABILITIES.get(reaction_type, {}))
        profiles = sorted(profile_reactions.get(reaction_type, []))
        declared["reaction_type"] = reaction_type
        declared["task_profiles"] = profiles
        declared["task_profile_count"] = len(profiles)
        if declared.get("requires_task_profile") and not profiles:
            # 没有 task profile 就不可能有可用的 ML 导出画像。
            declared["ml_export"] = "not_supported"
        reactions[reaction_type] = declared

    return {
        "version": CAPABILITY_MATRIX_VERSION,
        "reactions": reactions,
        "materials": {
            "dimension": "material",
            "note": "材料/载体身份不是 reaction_type；石墨炔属于材料维度。",
            "terms": sorted(MATERIAL_DIMENSION_TERMS),
        },
    }


__all__ = [
    "CAPABILITY_MATRIX_VERSION",
    "REACTION_CAPABILITIES",
    "build_capability_matrix",
]
