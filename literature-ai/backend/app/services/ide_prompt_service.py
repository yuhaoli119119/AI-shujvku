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


PROMPT_SCHEMA_VERSION: Final = "ide_review_prompt_v19"
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
6. review note 只能作为说明，不能授予 RAG、写作或引用资格；正式写作对象必须留下绑定真实对象 UUID 的对象级审核和 PDF 页证据。
7. import_analysis 只导入候选或审核意见。非受信任的 propose_correction 直接写入中，服务端只对顶层 abstract 及 sections、mechanism_claims、writing_cards 强制模块锁；title、year、journal、authors 等其他允许字段不属于必然强制锁范围。表格、图像遵守专用工具的 capability/evidence 契约。
8. 写回后使用 get_codex_item、get_paper 或 retrieve_evidence 回读；只有安全门通过才能报告 completed，仍未通过时必须报告 candidate，证据冲突报告 needs_manual_review，工具或证据不可用报告 blocked。"""


_DFT_SHARED_RULES = """目标文献：
{{TARGET_LIST}}

本次 source_label={{SOURCE_LABEL}}。human_ref 只用于沟通；调用工具必须使用对应 paper_id。

范围与证据：
- 只处理当前 paper_id 的 dft_results、DFT candidate 和 DFT audit issue；禁止修改图片、表格、章节、元数据或其他对象。
- 普通审核主流程为：get_dft_review_task -> get_codex_item/read_paper_page -> import_analysis 导入意见或 new_candidate -> readback。权威自动验收由专用 ai_verify_content 身份执行 get_ai_verification_tasks -> submit_ai_verification_batch；离线 DFT 审阅 ZIP 只产生 proposal/candidate。
- 先读取 get_dft_review_task，再用 get_codex_item、get_dft_audit_issues 和 read_paper_page 核对主文及已关联 SI 中与 DFT 直接相关的正文和表格证据。
- SI 中的 DFT 数据写回 writeback_paper_id，并保留 source_paper_id、source_document_type、页码和原文证据。
- 每条数据必须能定位到材料/结构/位点、性质或反应步、数值、单位及证据；不得猜测，不得把 ML prediction 当作 DFT 结果。
- 只使用当前会话的 Literature AI MCP 工具或 app.mcp.context.mcp_auth_context + app.mcp.server 受控后备路径；禁止直接调用 service/session/model、执行 SQL 或直接写数据库。
- 写入后必须回读 DFT rows、审核记录和 issue 状态。"""


_DFT_REVIEW_RULES = """任务：审核并处理当前论文的 DFT 数据。

职责：
- 核验已有 DFT candidate，并检查主文和 SI 是否漏提 DFT 数据。
- 已有数据提交 PASS、REVISE、REJECT 或 NEEDS_HUMAN；漏项提交 new_candidate。
- 普通 PASS、REVISE、REJECT 意见不能通过 import_analysis 完成最终验收；new_candidate 只物化未验证 DFTResult candidate。
- 唯一自动权威验收路径由一个专用 ai_verify_content 身份调用 get_ai_verification_tasks 和 submit_ai_verification_batch；无法通过确定性门禁的 exception 才进入 Owner-session 人工处理。
- 不使用第二模型、投票、共识或第三 AI 仲裁。
- 普通本地/网页 AI 的 PASS、REVISE、REJECT、recommended_action 或“无冲突”都不构成导出授权；只有专用验收服务写入 ai_verified 且导出安全门通过后才允许导出。
- new_candidate、NEEDS_HUMAN、REJECT、exception 或缺少权威验收的对象都不得导出；单位缺失、占位单位或键相关性质缺少 bond/bond_pair 时也不得建议导出。

执行：
1. 调用 get_dft_review_task 取得当前任务，再读取 DFT candidates、audit issues 和主文/SI 证据；issue_count=0 不代表无需审核。
2. 对每条已有 candidate 写入带证据的 object_review_audit。
3. 漏项使用 target_type="dft_results"、target_id="new"、field_name="dft_results"、decision="new_candidate"；corrected_value 至少包含 material_identity、property_type、value、unit，能确认时补充 adsorbate、reaction_step、method。
4. 使用 import_analysis 写入普通审核意见；只有 new_candidate 可通过 auto_apply_review_rules=true 进入受控物化，并保持未验证。
5. 专用验收身份读取 get_ai_verification_tasks 后，以每批最多 20 项调用 submit_ai_verification_batch；提交前后均核对目标 fingerprint、DFT row、证据和 export_safety。
6. accept/correct/reject 由权威验收服务处理；无法通过确定性门禁时提交 exception 并留给 Owner session。"""


_MODULE_RULES = {
    "overall": """任务：总体质量检查。
- 只核对论文身份信息、PDF 可用性、解析覆盖情况和各专项模块是否需要处理。
- 元数据错误按 PDF 证据使用 propose_correction 修正；顶层字段中只有 abstract 的非受信任直接写入由服务端强制模块锁，title、year、journal、authors 等其他允许字段不属于必然强制锁范围。import_analysis 只用于候选或审核意见。专项对象问题只列入对应专项任务，不在本任务中处理。
- 回读元数据和模块状态后，简要列出已修正项及待处理专项。""",
    "figure": """任务：审核当前唯一目标文献的图片。
- 按 PDF 核对图片总数、编号、页码、caption、子图、裁剪范围、figure_role、content_summary 和 key_elements；不处理表格或其他模块。
- 审核结论使用 review_figure；元数据修正使用带证据的 propose_correction；重裁使用 recrop_figure；漏图使用 create_figure_from_bbox。图像相关写入遵守各专用工具的 capability/evidence 契约，不描述为服务端必然强制 figures 锁。
- 每张科学图必须写回具体 figure_role，不能保留 unknown/unclassified/other。常用类型包括 structural_model、characterization、electrochemical_performance、computational_results、dft_calculation、electronic_property、free_energy_diagram、mechanism_diagram、schematic_illustration、property_data。
- 非科学图片必须明确标为 noise、noisy、decorative 或 publisher_logo；不要把噪声图标成 verified scientific figure。
- content_summary 应描述实际视觉内容，不能照抄 caption；所有修改必须附 page、figure、quoted_text 或 bbox 证据。
- verified 图必须同时具备有效 figure_role、content_summary 和具体 key_elements；缺任一项不得报告图片审核完成。
- 不从曲线估读、插值或推算精确数值。
- 只有图片或图注明确给出 DFT 结果，且能确认数值、单位、property_type 及对应材料/结构/吸附物或反应步骤时，才用 import_analysis(auto_apply_review_rules=true) 创建未验证 DFT 候选：target_type="dft_results"、target_id="new"、field_name="dft_results"、decision="new_candidate"。候选必须保留 figure_id/figure_label、page、图中标注或 bbox；不得将其改为已验收、已拒绝或可导出状态。
- 写入后回读图片对象；创建 DFT 候选时还要回读候选是否已进入 DFT 专项处理链路。""",
    "table": """任务：审核当前主文献及其已关联 SI 的表格。
- 只核对表格的 caption、page、markdown_content、列对齐、单位和跨页连续性。
- 修改表格用 update_table，漏表用 create_table，重复或跨页拆分用 merge_table，无效表用 delete_table；禁止用 import_analysis 修改表格对象。
- SI 表格必须使用该表真实 paper_id。每次写入提供 page、table、quoted_text 或 bbox 证据，并回读表格确认结果。
- 发现非表格问题时交给对应专项任务，不在本任务中处理。""",
    "sections_writing": """任务：审核章节与写作卡。
- 章节核对 section_title、section_type、text、page_start/page_end、section_level、section_number、parent_heading 和 heading_path。
- 写作卡只能引用已核对的 PDF 证据；sections 和 writing_cards 的非受信任直接修正或创建使用带对应模块写入锁的 propose_correction，并附 page、quoted_text 和 source_pdf。
- 每个要用于正式写作的 section 和 writing_card 都必须有对象 UUID、对象级受控 correction 审核以及精确 PDF 页证据；已有对象无须改字时也要对真实字段提交值不变的受控 replace，不能只写 [AI_REVIEWED] note。
- 只处理 sections 和 writing_cards；其他问题交给对应专项任务。写入后用 get_codex_item/get_paper/retrieve_evidence 回读，安全门未通过时明确报告仍为候选。""",
    "text_review": """任务：审核摘要与机理声明。
- 摘要必须忠实于原文；机理声明核对 claim_text、claim_type、key_species、mechanism_direction 和 evidence_text。
- abstract 和 mechanism_claims 的非受信任直接修正或创建使用带对应模块写入锁的 propose_correction，并附 page、quoted_text 和 source_pdf。
- 每个要用于正式写作或引用的 mechanism_claim 都必须有对象 UUID、对象级受控 correction 审核以及精确 PDF 页证据；已有对象无须改字时也要对真实字段提交值不变的受控 replace，不能只写 [AI_REVIEWED] note。
- 只处理 abstract 和 mechanism_claims；其他问题交给对应专项任务。写入后用 get_codex_item/get_paper/retrieve_evidence 回读，安全门未通过时明确报告仍为候选。""",
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
