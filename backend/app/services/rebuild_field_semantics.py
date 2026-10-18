"""Additive producer guidance; custom field names and old payloads stay valid."""
from copy import deepcopy


RECOMMENDED_FIELDS = [
    {"name": "li2s_decomposition_barrier_ev", "label": "Li₂S 分解 / 解离能垒", "unit": "eV", "kind": "numeric",
     "applies_when": "原文明示 Li2S 化学分解/解离路径的活化能垒；li2s_dissociation_barrier_ev 为既有同义字段。",
     "not_for": "物种迁移/扩散能垒、反应能或自由能变化；不能因单位相同替换。"},
    {"name": "reaction_step_delta_g_ev", "label": "反应步骤 ΔG", "unit": "eV", "kind": "numeric",
     "applies_when": "原文明示某反应步骤的 Gibbs 自由能变化，保留原反应方向、初态、终态和符号。",
     "not_for": "活化能垒或仅吸附自由能；未明确决速步时不能改成 RDS ΔG。"},
    {"name": "delta_g_rds_ev", "label": "决速步 ΔG", "unit": "eV", "kind": "numeric",
     "applies_when": "原文明确指定该步骤为 RDS/决速步且报告其 ΔG；rate_determining_step_gibbs_free_energy_ev 为既有同义字段。",
     "not_for": "不能从最大 ΔG 自行推定 RDS；不能用动力学活化能垒替代。"},
]


def field_semantics_contract():
    return {
        "version": "field_semantics_v1", "mode": "advisory_additive",
        "custom_fields": "沿用任意合法自定义 field_name；推荐字段不是必填项，不拒绝旧客户端，不迁移历史值。",
        "recommended_fields": deepcopy(RECOMMENDED_FIELDS),
        "existing_fields": {
            "migration_barrier_ev": "仅用于原文明示物种沿迁移路径的势垒；不是化学分解/解离能垒。",
            "adsorption_free_energy_ev": "仅用于原文明示吸附过程的自由能；不自动等同一般反应步骤 ΔG 或 RDS ΔG。",
        },
        "identity_and_evidence": "保留原文对象/adsorbate、reaction_step（方向/初终态）、吸附位点、材料/构型和逐值来源原句/物理页。不同对象/步骤/条件分行；不从字段名补猜对象。",
        "conditions": "condition 保留完整原始条件，包括几何优化、单点、DOS 等各阶段网格/方法、电位、温度、参考态和自由能修正。不同属性阶段设置可不同，差异本身不证明冲突。",
        "uncertainty": "对象/步骤/类型不明时保留原名、raw_value、单位、来源及未知说明，不猜成专用物种/RDS。未报告值 numeric_value=null 并写 missing_reason；矛盾声明与双方来源保留待核实，不覆盖。",
    }
