from __future__ import annotations

from typing import Any, Final

from app.domain.lis_sac_dac_field_dictionary import (
    build_topic_field_dictionary_payload,
    list_topic_field_definitions,
)
from app.domain.project_library_context import (
    build_project_library_context_payload,
    get_project_library_context,
)
from app.domain.reaction_taxonomy import REACTION_TYPES, get_reaction_profile, normalize_reaction_type


PROMPT_SCHEMA_VERSION: Final = "ide_review_prompt_v18"
CANONICAL_MCP_PATH: Final = "/mcp"
TARGET_LIST_TOKEN: Final = "{{TARGET_LIST}}"
SOURCE_LABEL_TOKEN: Final = "{{SOURCE_LABEL}}"
TARGET_REACTION_TOKEN: Final = "{{TARGET_REACTION}}"


SUPPORTED_REVIEW_PROMPTS: Final = (
    "overall",
    "dft",
    "figure",
    "table",
    "text_review",
    "sections_writing",
)

# Different modules must use different prompts. Combined prompts are intentionally disabled.
COMPOSITE_REVIEW_PROMPTS: Final[dict[str, tuple[str, ...]]] = {}


_USER_FACING_TERMINOLOGY_RULES = """输出要求：
- 用简洁中文汇报；必要的英文系统词第一次出现时补一个简短中文括注。
- JSON、字段名、状态值和工具参数保持原样。"""


_COMMON_RULES = """你正在执行 Literature AI 的单篇论文审核任务。

目标文献：
{{TARGET_LIST}}

本次 source_label={{SOURCE_LABEL}}。human_ref 只用于沟通；调用工具必须使用对应 paper_id。

通用规则：
1. 只处理本提示词指定的模块，不顺手修改其他模块。
2. 优先使用当前会话的 Literature AI MCP 工具；未注入时可通过 app.mcp.context.mcp_auth_context 受控调用 app.mcp.server。禁止直接调用 service/session/model、执行 SQL 或直接写数据库。
3. 先调用 get_codex_context；只有 pdf_quality_status 为 A_text_readable 或 B_text_partial、parse_allowed 不是 false 且证据入口可用时继续，否则报告明确的 blocked_by_* 原因。
4. 用 get_codex_item 和 read_paper_page 核对原 PDF；禁止用本地下载、pdftotext 或自写脚本绕过证据入口。
5. 所有修改必须带页码和本模块对应的结构化证据，并在写入后回读确认。
6. 只有回读确认成功才能报告 completed；证据冲突报告 needs_manual_review，工具或证据不可用报告 blocked。"""


_DFT_SHARED_RULES = """目标文献：
{{TARGET_LIST}}

本次 source_label={{SOURCE_LABEL}}。human_ref 只用于沟通；调用工具必须使用对应 paper_id。

范围与证据：
- 只处理当前 paper_id 的 dft_results、DFT candidate 和 DFT audit issue；禁止修改图片、表格、章节、元数据或其他对象。
- 本地 AI 主流程固定为：get_dft_review_task -> get_codex_item/read_paper_page -> acquire dft_results lock -> import_analysis(auto_apply_review_rules=true) -> readback。离线 DFT 审阅 ZIP 仅用于 web AI、第三方或离线审阅。
- 先读取 get_dft_review_task，再用 get_codex_item、get_dft_audit_issues 和 read_paper_page 核对主文及已关联 SI 中与 DFT 直接相关的正文和表格证据。
- SI 中的 DFT 数据写回 writeback_paper_id，并保留 source_paper_id、source_document_type、页码和原文证据。
- 每条数据必须能定位到材料/结构/位点、性质或反应步、数值、单位及证据；不得猜测，不得把 ML prediction 当作 DFT 结果。
- 只使用当前会话的 Literature AI MCP 工具或 app.mcp.context.mcp_auth_context + app.mcp.server 受控后备路径；禁止直接调用 service/session/model、执行 SQL 或直接写数据库。
- 写入后必须回读 DFT rows、审核记录和 issue 状态。"""


_DFT_REVIEW_RULES = """任务：审核并处理当前论文的 DFT 数据。

职责：
- 核验已有 DFT candidate，并检查主文和 SI 是否漏提 DFT 数据。
- 已有数据提交 PASS、REVISE、REJECT 或 NEEDS_HUMAN；漏项提交 new_candidate。
- 一份证据合格的 AI 意见即可通过 import_analysis 的受控校验和写入入口直接确认、修正、拒绝或新增 DFT 数据；不需要第二个 AI，也不按 AI 身份计票。
- 系统不得根据“没有报错”“已验证”或“没有冲突”自行推断可导出。只有本地 AI 或网页 AI 明确给出 PASS/REVISE，并同时设置 recommended_action="ready_for_ml_export"，才构成导出授权。
- new_candidate、NEEDS_HUMAN、REJECT、缺少 recommended_action 或其他模糊建议都不得导出；单位缺失、占位单位或键相关性质缺少 bond/bond_pair 时也不得建议导出。

执行：
1. 调用 get_dft_review_task 取得当前任务，再读取 DFT candidates、audit issues 和主文/SI 证据；issue_count=0 不代表无需审核。
2. 对每条已有 candidate 写入带证据的 object_review_audit。
3. 漏项使用 target_type="dft_results"、target_id="new"、field_name="dft_results"、decision="new_candidate"；corrected_value 至少包含 material_identity、property_type、value、unit，能确认时补充 adsorbate、reaction_step、method。
4. 写入前获取 dft_results 模块写锁，然后使用 import_analysis(auto_apply_review_rules=true) 写入审核结果。PASS 会确认当前值，REVISE 会修改后确认，REJECT 会直接拒绝，new_candidate 会新增后确认；NEEDS_HUMAN 保持待人工，不得猜测。
5. 回读本轮 object_review_audits、DFT rows、审核状态和失败记录；确认实际值、candidate_status 与 export_safety 后才能报告 completed。"""


_MODULE_RULES = {
    "overall": """任务：总体质量检查。
- 只核对论文身份信息、PDF 可用性、解析覆盖情况和各专项模块是否需要处理。
- 元数据错误可通过 import_analysis 修正；专项对象问题只列入对应专项任务，不在本任务中处理。
- 回读元数据和模块状态后，简要列出已修正项及待处理专项。""",
    "figure": """任务：审核当前唯一目标文献的图片。
- 按 PDF 核对图片总数、编号、页码、caption、子图、裁剪范围、figure_role、content_summary 和 key_elements；不处理表格或其他模块。
- 审核结论使用 review_figure；元数据修正使用 import_analysis；重裁使用 recrop_figure；漏图使用 create_figure_from_bbox。
- 每张科学图必须写回具体 figure_role，不能保留 unknown/unclassified/other。常用类型包括 structural_model、characterization、electrochemical_performance、computational_results、dft_calculation、electronic_property、free_energy_diagram、mechanism_diagram、schematic_illustration、property_data。
- 非科学图片必须明确标为 noise、noisy、decorative 或 publisher_logo；不要把噪声图标成 verified scientific figure。
- content_summary 应描述实际视觉内容，不能照抄 caption；所有修改必须附 page、figure、quoted_text 或 bbox 证据。
- verified 图必须同时具备有效 figure_role、content_summary 和具体 key_elements；缺任一项不得报告图片审核完成。
- 不从曲线估读、插值或推算精确数值。
- 只有图片或图注明确给出 DFT 结果，且能确认数值、单位、property_type 及对应材料/结构/吸附物或反应步骤时，才用 import_analysis(auto_apply_review_rules=true) 创建未验证 DFT 候选：target_type="dft_results"、target_id="new"、field_name="dft_results"、decision="new_candidate"。候选必须保留 figure_id/figure_label、page、图中标注或 bbox；不得直接确认、拒绝或标记为可导出。
- 写入后回读图片对象；创建 DFT 候选时还要回读候选是否已进入 DFT 专项处理链路。""",
    "table": """任务：审核当前主文献及其已关联 SI 的表格。
- 只核对表格的 caption、page、markdown_content、列对齐、单位和跨页连续性。
- 修改表格用 update_table，漏表用 create_table，重复或跨页拆分用 merge_table，无效表用 delete_table；禁止用 import_analysis 修改表格对象。
- SI 表格必须使用该表真实 paper_id。每次写入提供 page、table、quoted_text 或 bbox 证据，并回读表格确认结果。
- 发现非表格问题时交给对应专项任务，不在本任务中处理。""",
    "sections_writing": """任务：审核章节与写作卡。
- 章节核对 section_title、section_type、text、page_start/page_end、section_level、section_number、parent_heading 和 heading_path。
- 写作卡只能引用已核对的 PDF 证据；修正或创建使用 import_analysis，并附 page、quoted_text 和 source_pdf。
- 只处理 sections 和 writing_cards；其他问题交给对应专项任务。写入后回读确认。""",
    "text_review": """任务：审核摘要与机理声明。
- 摘要必须忠实于原文；机理声明核对 claim_text、claim_type、key_species、mechanism_direction 和 evidence_text。
- 修正或创建使用 import_analysis，并附 page、quoted_text 和 source_pdf。
- 只处理 abstract 和 mechanism_claims；其他问题交给对应专项任务。写入后回读确认。""",
}


def _format_list(items: Any, *, empty: str = "无") -> str:
    values = sorted(str(item) for item in items if str(item or "").strip())
    return ", ".join(values) if values else empty


def _format_step_graph(step_graph: Any) -> str:
    rows = []
    for source, targets in sorted((step_graph or {}).items()):
        rows.append(f"{source} -> {_format_list(targets)}")
    return "; ".join(rows) if rows else "未定义"


def _reaction_profile_context(target_reaction: Any = None) -> str:
    if target_reaction is None or not str(target_reaction).strip():
        return """反应上下文：target_reaction=未指定。依据 PDF 判断真实 reaction_type；证据不足时写 UNKNOWN，不得猜测。"""

    key = normalize_reaction_type(target_reaction)
    profile = get_reaction_profile(key)
    target_display = str(target_reaction).strip()
    return f"""反应上下文：target_reaction={target_display}，profile={profile.key}。
- 该 profile 只提供参考，不得覆盖 PDF 证据；不匹配时写实际 reaction_type，证据不足写 UNKNOWN。
- 允许物种：{_format_list(profile.allowed_intermediates)}。
- 允许性质：{_format_list(profile.allowed_properties)}；推荐单位：{", ".join(f"{name}={unit}" for name, unit in sorted(profile.canonical_units.items())) or "无"}；反应步：{_format_step_graph(profile.step_graph)}。"""


def _project_library_prompt_fragment(context_key: Any, target_reaction: Any = None) -> str:
    context = get_project_library_context(context_key)
    field_map = {field.canonical_key: field for field in list_topic_field_definitions(context.key)}
    structure_keys = [
        key
        for key in (
            "metal_centers",
            "catalyst_scope",
            "metal_pairing_type",
            "support_material",
            "coordination_environment",
            "metal_metal_distance",
            "li_s_bond_length",
        )
        if key in field_map
    ]
    dft_keys = [
        key
        for key in (
            "srr_lis_intermediate",
            "adsorption_energy",
            "gibbs_free_energy_change",
            "reaction_barrier",
            "li2s_nucleation_barrier",
            "li2s_decomposition_barrier",
            "migration_barrier",
            "d_band_center",
            "bader_charge",
            "charge_transfer",
        )
        if key in field_map
    ]
    return f"""专题库：{context.display_name_zh}（{context.key}）。
- 重点结构字段：{_format_list(structure_keys)}。
- 重点 DFT 字段：{_format_list(dft_keys)}。
- 字段只能依据 PDF 证据填写；证据不足保持 UNKNOWN/null，不得自动升级 verified 或 safe_verified。"""


def build_project_library_prompt_templates() -> dict[str, dict[str, str]]:
    fragment = _project_library_prompt_fragment("li_s_sac_dac", "SRR_LiS")
    return {"li_s_sac_dac": {"dft": fragment}}


def _module_rule(
    module_kind: str,
    *,
    target_reaction: Any = None,
    project_library_context: Any = None,
) -> str:
    if module_kind == "dft":
        parts = [_DFT_REVIEW_RULES.strip(), _reaction_profile_context(target_reaction).strip()]
    else:
        return _MODULE_RULES[module_kind].strip()
    if project_library_context is not None and str(project_library_context).strip():
        parts.append(_project_library_prompt_fragment(project_library_context, target_reaction).strip())
    return "\n\n".join(parts)


def build_ide_review_prompt(
    kind: str = "overall",
    *,
    target_list: str = TARGET_LIST_TOKEN,
    source_label: str = SOURCE_LABEL_TOKEN,
    target_reaction: Any = None,
    project_library_context: Any = None,
) -> str:
    module_kind = kind if kind in SUPPORTED_REVIEW_PROMPTS else "overall"
    common_rules = _DFT_SHARED_RULES if module_kind == "dft" else _COMMON_RULES
    common = common_rules.replace(TARGET_LIST_TOKEN, target_list).replace(SOURCE_LABEL_TOKEN, source_label)
    module = _module_rule(
        module_kind,
        target_reaction=target_reaction,
        project_library_context=project_library_context,
    )
    return f"{common.strip()}\n\n{module}\n\n{_USER_FACING_TERMINOLOGY_RULES.strip()}\n"


def build_prompt_templates(*, target_reaction: Any = None) -> dict[str, str]:
    return {kind: build_ide_review_prompt(kind, target_reaction=target_reaction) for kind in SUPPORTED_REVIEW_PROMPTS}


def build_reaction_profile_templates() -> dict[str, dict[str, str]]:
    return {
        reaction_type: {
            "dft": build_ide_review_prompt("dft", target_reaction=reaction_type),
        }
        for reaction_type in REACTION_TYPES
    }


def build_reaction_profile_contexts() -> dict[str, str]:
    return {reaction_type: _reaction_profile_context(reaction_type) for reaction_type in REACTION_TYPES}


def prompt_contract() -> dict[str, object]:
    return {
        "schema_version": PROMPT_SCHEMA_VERSION,
        "canonical_mcp_path": CANONICAL_MCP_PATH,
        "target_list_token": TARGET_LIST_TOKEN,
        "source_label_token": SOURCE_LABEL_TOKEN,
        "target_reaction_token": TARGET_REACTION_TOKEN,
        "supported_kinds": list(SUPPORTED_REVIEW_PROMPTS),
        "templates": build_prompt_templates(),
        "reaction_profile_contexts": build_reaction_profile_contexts(),
        "reaction_profile_templates": build_reaction_profile_templates(),
        "project_library_contexts": build_project_library_context_payload(),
        "topic_field_dictionaries": build_topic_field_dictionary_payload(),
        "project_library_prompt_templates": build_project_library_prompt_templates(),
        "composite_templates": {},
    }
