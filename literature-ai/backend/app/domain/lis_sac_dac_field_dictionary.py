from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


LI_S_SAC_DAC_FIELD_DICTIONARY_VERSION = "li_s_sac_dac_field_dictionary_v2"


@dataclass(frozen=True)
class TopicFieldDefinition:
    canonical_key: str
    zh_label: str
    en_label: str
    category: str
    value_type: str
    unit_suggestion: str | None
    multi_value: bool
    ml_relevant: bool
    applies_to: tuple[str, ...]
    missing_strategy: str
    unknown_strategy: str
    notes: str


def _field(
    canonical_key: str,
    zh_label: str,
    en_label: str,
    category: str,
    value_type: str,
    unit_suggestion: str | None,
    multi_value: bool,
    ml_relevant: bool,
    applies_to: tuple[str, ...],
    notes: str,
) -> TopicFieldDefinition:
    return TopicFieldDefinition(
        canonical_key=canonical_key,
        zh_label=zh_label,
        en_label=en_label,
        category=category,
        value_type=value_type,
        unit_suggestion=unit_suggestion,
        multi_value=multi_value,
        ml_relevant=ml_relevant,
        applies_to=applies_to,
        missing_strategy="preserve_null",
        unknown_strategy="mark_unknown_when_evidence_is_ambiguous",
        notes=notes,
    )


_FIELD_GROUPS = MappingProxyType(
    {
        "li_s_sac_dac": (
            _field("metal_centers", "金属中心", "metal centers", "structure", "string_list", None, True, True, ("DFT", "experiment"), "用于记录单/双原子活性中心元素；未能唯一确认时保持 UNKNOWN，不从弱缩写猜测。"),
            _field("catalyst_scope", "SAC/DAC 类型", "SAC/DAC scope", "structure", "enum", None, False, True, ("DFT", "experiment"), "仅接受 SAC、DAC 或 UNKNOWN；不把一般 atomically dispersed 表述强行映射到 DAC。"),
            _field("metal_pairing_type", "同核/异核", "homo-/heteronuclear type", "structure", "enum", None, False, True, ("DFT", "experiment"), "主要适用于 DAC；SAC 或证据不足时留空或 UNKNOWN。"),
            _field("support_material", "载体", "support material", "structure", "string", None, False, True, ("DFT", "experiment"), "记录载体或基底名称，如 graphene、N-doped carbon；不要推断隐含载体。"),
            _field("coordination_environment", "配位环境", "coordination environment", "structure", "string", None, False, True, ("DFT",), "如 Fe-N4、CoN3S1；结构未定型或只见示意图时保持 UNKNOWN。"),
            _field("metal_metal_distance", "M-M 距离", "M-M distance", "structure", "number", "angstrom", False, True, ("DFT",), "仅在文中明确给出金属-金属距离时记录，单位建议 Angstrom。"),
            _field("srr_lis_intermediate", "LiPS/Li2S 中间体", "LiPS/Li2S intermediate", "dft_label", "enum", None, False, True, ("DFT",), "推荐使用 S8、Li2S8、Li2S6、Li2S4、Li2S2、Li2S；共享语境不足时不要强行归类。"),
            _field("adsorption_energy", "吸附能", "adsorption energy", "dft_label", "number", "eV", False, True, ("DFT",), "默认用于 LiPS/Li2S 吸附；若原文语义更接近 binding energy，应保留原 canonical property 再另行映射。"),
            _field("gibbs_free_energy_change", "自由能变化", "Gibbs free energy change", "dft_label", "number", "eV", False, True, ("DFT",), "用于反应步骤或转化过程的自由能变化；若原文写 RDS Gibbs free energy、ΔG of RDS、决速步骤自由能，仍归入本字段，并在 reaction_step 标明 RDS/决速步骤；不得与 reaction_barrier、migration_barrier、li2s_decomposition_barrier 混用。"),
            _field("reaction_barrier", "反应能垒", "reaction barrier", "dft_label", "number", "eV", False, True, ("DFT",), "仅在原文明确为 reaction barrier、activation energy、energy barrier、ΔG‡、活化能或反应能垒时使用；这是通用动力学能垒字段，不覆盖迁移能垒或 Li2S 分解能垒。"),
            _field("li2s_nucleation_barrier", "Li2S 成核能垒", "Li2S nucleation barrier", "dft_label", "number", "eV", False, True, ("DFT",), "仅在原文明确为 Li2S nucleation barrier 时使用，不与一般 reaction barrier 混用。"),
            _field("li2s_decomposition_barrier", "Li2S 分解能垒", "Li2S decomposition barrier", "dft_label", "number", "eV", False, True, ("DFT",), "仅在原文明确为 Li2S decomposition barrier 或 Li2S 分解能垒时使用；前端/导出必须保留这一小类，不能回落伪装成普通 reaction_barrier。"),
            _field("migration_barrier", "迁移能垒", "migration barrier", "dft_label", "number", "eV", False, True, ("DFT",), "适用于 Li+、LiPS 或扩散/迁移过程；迁移对象不清楚时应保留未知；前端/导出必须保留 migration_barrier 小类，不能回落成普通 reaction_barrier。"),
            _field("d_band_center", "d 带中心", "d-band center", "dft_label", "number", "eV", False, True, ("DFT",), "材料级描述符，可在缺少具体中间体时存在，但仍需明确材料身份。"),
            _field("bader_charge", "Bader 电荷", "Bader charge", "dft_label", "number", "e", False, True, ("DFT",), "材料级或吸附态电荷分析字段；转移对象不明时不自动补充解释。"),
            _field("charge_transfer", "电荷转移", "charge transfer", "dft_label", "number", "e", False, True, ("DFT",), "建议保留数值和方向说明；若只有定性描述，可先保持 UNKNOWN。"),
            _field("specific_capacity", "比容量", "specific capacity", "experimental_performance", "number", "mAh g^-1", False, True, ("experiment",), "记录放电/充电容量时需结合原文语境；未说明倍率或循环节点时不做标准化推断。"),
            _field("rate_c_value", "倍率", "rate (C value)", "experimental_performance", "number", "C", False, True, ("experiment",), "仅在原文明确使用 C-rate 表述时记录；若使用 mA g^-1，应留给后续扩展字段，不在本批强转。"),
            _field("cycling_stability_cycles", "循环稳定性", "cycling stability", "experimental_performance", "number", "cycles", False, True, ("experiment",), "用于记录容量保持或稳定运行对应的循环数；若仅有定性描述则保持 UNKNOWN。"),
            _field("capacity_decay_rate", "衰减率", "capacity decay rate", "experimental_performance", "number", "% per cycle", False, True, ("experiment",), "建议按每循环衰减率记录；若原文只给总 retention，不做自动换算。"),
            _field("sulfur_loading", "硫负载", "sulfur loading", "experimental_performance", "number", "mg cm^-2", False, True, ("experiment",), "仅在原文明确给出面负载时记录；体积分数或质量分数不在本批自动折算。"),
            _field("electrolyte_to_sulfur_ratio", "电解液/硫比", "electrolyte-to-sulfur ratio", "experimental_performance", "number", "uL mg^-1", False, True, ("experiment",), "建议按 E/S 比记录；若原文单位不同，保持原始信息等待后续标准化。"),
        )
    }
)


def list_topic_field_definitions(context_key: str) -> tuple[TopicFieldDefinition, ...]:
    return tuple(_FIELD_GROUPS.get(context_key, ()))


def build_topic_field_dictionary_payload() -> dict[str, dict[str, object]]:
    payload: dict[str, dict[str, object]] = {}
    for context_key, fields in _FIELD_GROUPS.items():
        payload[context_key] = {
            "context_key": context_key,
            "version": LI_S_SAC_DAC_FIELD_DICTIONARY_VERSION,
            "fields": [
                {
                    "canonical_key": field.canonical_key,
                    "zh_label": field.zh_label,
                    "en_label": field.en_label,
                    "category": field.category,
                    "value_type": field.value_type,
                    "unit_suggestion": field.unit_suggestion,
                    "multi_value": field.multi_value,
                    "ml_relevant": field.ml_relevant,
                    "applies_to": list(field.applies_to),
                    "missing_strategy": field.missing_strategy,
                    "unknown_strategy": field.unknown_strategy,
                    "notes": field.notes,
                }
                for field in fields
            ],
        }
    return payload


__all__ = [
    "LI_S_SAC_DAC_FIELD_DICTIONARY_VERSION",
    "TopicFieldDefinition",
    "build_topic_field_dictionary_payload",
    "list_topic_field_definitions",
]
