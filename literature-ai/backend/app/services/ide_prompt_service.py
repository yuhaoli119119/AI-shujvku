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


PROMPT_SCHEMA_VERSION: Final = "ide_review_prompt_v8"
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

COMPOSITE_REVIEW_PROMPTS: Final = {
    "figure_table": ("figure", "table"),
}


_COMMON_RULES = """你现在是 Literature AI 的 IDE AI。你的任务是依据原 PDF 证据核验并通过系统受控入口回写，不要只输出文字总结。

目标文献：
{{TARGET_LIST}}

身份与编号：
- human_ref 只用于沟通；调用 MCP/API 必须使用对应 paper_id(UUID)。
- 本次 source_label={{SOURCE_LABEL}}；reviewer 使用实际模型或窗口名。

调用顺序：
1. 优先使用当前 IDE 会话已经暴露的 literature-ai MCP 工具。
2. 若 IDE 未注入这些工具，允许使用受控 in-process MCP 兜底：仅通过 literature-ai/backend 的 app.mcp.context.mcp_auth_context 建立明确身份，再调用 app.mcp.server 已公开的 MCP 工具入口。
3. 正式 HTTP API 只作为补充，用于它已覆盖的材料准备、读取、状态查询或明确支持的操作；它不能替代尚未提供 HTTP 等价入口的 MCP 工具。
4. 只有 IDE MCP、受控 in-process MCP 和相关正式 HTTP API 都不可用时，才报告 blocked_by_evidence_api_unavailable。

禁止事项：
- 不要自行改写 mcp_config.json，不要虚构工具成功。
- 禁止直接导入 service/session/model，禁止执行 SQL 或直接写数据库。
- 禁止绕过 import_analysis 校验或 DFT 的 verified/safe_verified/export gate。
- 禁止用 pdftotext、自写脚本或下载副本替代 read_paper_page 的受控页证据入口。

每篇文献的前置门：
- 先读取 get_codex_context，至少确认 context.external_audit_precondition.status、context.paper.pdf_quality_status、context.artifact_status；若返回里还有 parse_allowed、source_assets、library_name 也一并检查。
- 只有 external_audit_precondition.status=ready，且 pdf_quality_status 属于 A_text_readable 或 B_text_partial，并且 parse_allowed 不是 false 时才继续正文核验。
- C/D/Broken、parse_allowed=false、PDF 缺失或证据入口不可用时停止正文回写，并报告 blocked_by_pdf_quality 或 blocked_by_evidence_api_unavailable。

主文献与支撑文献（SI）：
- 读取 context.source_documents 和 context.relationships；发现 supplementary / supplementary_information / si 时，表格核验和 DFT 文本/表格证据必须把关联 SI 作为主文献的证据源一并核验。
- 图片核验默认只核验主文 paper_id 的 figures；不要自动全量扫描 SI figures。只有用户显式要求 include_supplementary_figures=true、任务明确引用 Figure Sxx，或 DFT/机制候选 evidence anchor 指向 SI figure 时，才读取相关 SI figure。
- 主文使用 source_documents[*].read_paper_page_paper_id=主文 paper_id；SI 使用其 related_paper_id / read_paper_page_paper_id 调用 read_paper_page。
- SI 中抽取的对象与数据统一回写到 source_documents[*].writeback_paper_id（主文 paper_id），证据标记 source_document_type="supplementary_information" 并保留 related_paper_id。
- 不把 SI 当独立论文审核或引用；主文和 SI 重复报告的同一数据必须去重，保留双方证据锚点。
- relationship_summary 显示有 SI 但 source_documents 未列出可用 SI 时，先通过正式 prepare-ai-context 重建材料后重读上下文；仍无法读取时报告 blocked_by_supplementary_evidence_unavailable，不得宣称已完成 SI 核验。

证据与回写：
- 用 get_codex_item 和 read_paper_page 核对具体页；页眉页脚清理不会取消 page/page_start/page_end 来源。
- object_review_audits 的 evidence_location 和 correction_proposals 的 evidence_payload 优先使用结构化 dict，至少包含 page、table、figure、section、quoted_text、bbox、evidence_text 之一；不要只给一句模糊自然语言。
- 非 DFT 普通文本/结构化字段修正或创建对象时，直接调用 import_analysis(auto_apply_review_rules=true)；禁止申请模块写锁，后写入的 AI 结果允许覆盖先前 AI 结果。
- 表格对象生命周期是直接 MCP 工具路径：修改表格用 update_table，漏表用 create_table，重复/无效表用 delete_table，跨页拆分或尾行表优先用 merge_table。不要只写“后台请求”笔记，也不要把表格删除/合并伪装成 import_analysis JSON。
- 调用表格工具时必须使用该表对象真实归属的 paper_id；主文详情里显示的 SI 表通常属于 related_paper_id / read_paper_page_paper_id，不要误用主文 paper_id 删除或修改 SI 表对象。DFT/机制等从 SI 抽出的科学候选仍按 writeback_paper_id 归属主文。
- update_table/create_table/delete_table/merge_table 都必须提供结构化 evidence_payload，至少包含 page、table、quoted_text、table_id 或 bbox 之一；完成后必须回读 get_paper 或 get_codex_item，确认表格数量、markdown_content、table_review_status 和 AuditLog/PaperCorrection 结果。
- 如果当前会话里的 MCP import_analysis 路径不可用，而正式 HTTP API 已覆盖同一受控写回能力，可回退到 HTTP API：POST /api/external-analysis/import。
- 仅提交候选审计时可使用 auto_apply_review_rules=false，但不得宣称已应用。
- 图像裁剪只能直接调用 recrop_figure 或 create_figure_from_bbox，不能伪装成 import_analysis correction。
- DFT 是硬安全边界：单个 AI 不得最终确认 DFT，不得解锁导出。
- DFT 核验 AI 只能提交审核意见、问题或候选；即使双 AI 意见一致，也不得自动 verify/reject、不得把 DFTResult 推到 ML_Ready、不得写 human_verification。
- 主 AI 可以调用 get_dft_audit_issues 读取 DFT audit issue 队列，并仅按单个 issue_id 调用 repair_dft_audit_issue 做受控修复；审核 AI 禁止调用 repair_dft_audit_issue。
- repair_dft_audit_issue 的结果仍是 AI 修复状态，需要后续复核或人工最终确认；不得描述为 human_verified、safe_verified 或 ML_Ready。
- DFTResult 的最终 verify/reject 只能来自显式人工确认或用户授权的专门审核工具；AI consensus 结果只是待处理审核意见，不能伪装成人工确认。
- 图片中出现明确 DFT 数值或可读标注时，只能提取为 DFT 候选/object_review_audit，不得直接写成 ML_Ready；必须带 figure_id/figure_label、page、quoted_text 或图中可读标注、value、unit、property_type、adsorbate 或 reaction_step、material_identity（如能判断），并进入现有 DFT 二审/安全门。
- 每次写入后必须通过 MCP/API 回读对象、审核状态和审计记录；优先用 get_paper 或 get_codex_item 回读字段值、object_review_audits、approved_corrections，get_review_coverage 只作辅助概览。

工作区产物规范：
- 不要把预览图、候选裁剪图、调试 JSON、临时分析文本写到仓库根目录。
- 临时产物统一写入 outputs/tmp/ 或 literature-ai/backend/scratch/。
- 正式导出物统一写入 outputs/exports/。
- 不要把候选图、预览图、调试输出当成数据库正式数据或长期资产。
- 如产生临时文件，优先复用现有目录与清理约定，避免新增散落路径。

最终状态只能是：
- completed：已回写并回读验证；
- needs_manual_review：仅 DFT 证据冲突或安全门要求人工裁决；
- blocked：说明具体 blocked_by_* 原因和失败入口。
"""


_MODULE_RULES = {
    "overall": """本次模块：总体解析与核验
- 核对元数据、摘要、章节、表格、figure 元信息、写作卡和机制声明的覆盖情况。
- parser candidate 不是可信知识；只有带页证据并形成 ai_reviewed/ai_applied 记录后，才能进入默认 RAG/写作使用范围。
- 每篇文献至少形成一次有效 import_analysis 回写，或明确给出 blocked/needs_manual_review。
""",
    "dft": """本次模块：DFT 专项核验
- 每条结果必须绑定材料/结构/位点、性质或反应步、数值、单位、方法以及页码/表格证据。
- 硬规则：RDS 对应吉布斯自由能属于自由能变化（gibbs_free_energy_change），不属于反应能垒；如原文写 RDS Gibbs free energy、ΔG of RDS、决速步骤自由能等，property_type 记为 gibbs_free_energy_change，并在 reaction_step 标注 RDS/决速步骤。
- 硬规则：自由能变化、反应能垒、迁移能垒、Li2S 分解能垒不得混用；只有原文明确为 barrier / activation energy / ΔG‡ / 活化能 / 反应能垒时才记为 reaction_barrier；migration 或 diffusion barrier 记为 migration_barrier；Li2S decomposition barrier 记为 li2s_decomposition_barrier。
- 必须同时检查主文与已关联 SI 的 DFT 表格/文本；SI 候选仍归属主文 paper_id，并标记 source_document_type="supplementary_information"。
- 漏提项使用 object_review_audits 的 new_candidate；后端会沉淀为 DFT audit issue / missing_dft_result 草案，供主 AI 或用户后续处理，不是最终真值。
- DFT object_review_audits / new_candidate 属于高风险审核输入；调用 import_analysis 时必须通过 MCP/HTTP 受控入口取得或传入 dft_results 模块写锁，缺锁返回 409 时不要绕过系统。
- 不从曲线估读精确数值；引用文献数据必须标记 borrowed_from_reference=true。
- PASS 仍不等于 safe_verified；保持多 AI/人工审核与导出门禁。
- 双 AI DFT consensus 只会被后端记录为 DFT audit issue / 待处理事项；不要宣称已经自动应用、人工确认、verified、rejected 或 ML_Ready。
- 主 AI 处理 DFT audit issue 时先调用 get_dft_audit_issues；只对单个 issue_id 调用 repair_dft_audit_issue，不做批量自动推进。用户不需要复制审核 AI 长文本，给 issue_id 或让主 AI 读队列即可。
- AI 不得向 review_payload.human_verification 写入或暗示人工确认；需要最终判断时交给主 AI 修复候选，无法判断再交前端人工处理。

object_review_audits DFT new_candidate 结构规范（缺一不可）：
- target_type: "dft_results"（固定值）
- target_id: "new"（字符串字面量 "new"，不是 null）
- field_name: "dft_results"（复数，固定值）
- decision: "new_candidate"
- corrected_value 必须包含以下键：
  - material_identity: str — 材料/催化剂名称（必填）
  - property_type: str — 性质类型（必填）；优先使用后端已知或推荐值，例如
    gibbs_free_energy_change, activation_energy, adsorption_energy, reaction_barrier,
    migration_barrier, li2s_decomposition_barrier, permeation_barrier, permeance,
    d_band_center, cohp, bader_charge；若原文性质明确且系统可接受，不要仅因不在推荐列表示例中就丢弃
  - value: float — 数值，必须是数字类型，不能是字符串（必填）
  - unit: str — 单位，如 "eV"、"|e|"（必填）
  - adsorbate: str — 吸附物（可选）
  - reaction_step: str — 反应步描述（可选）
  - method: str — 计算方法（可选）
- evidence_location 必须为结构化 dict，至少包含 page 和 figure 或 quoted_text 之一
- reason: str — 简短说明

回写规则：
- DFT 仍走专门审核/冲突流程；非 DFT 不申请写锁，直接通过 import_analysis(auto_apply_review_rules=true) 写回
- 如果 MCP import_analysis 不可用，允许回退到 HTTP API：
  POST /api/external-analysis/import 提交
- 如果无法从论文文本、表格或图内明确文字获取精确数值，不要用占位数值 materialize 成 DFTResult；此时应保留 candidate / needs_manual_review，并在 reason 中明确标注需要人工或图表数据提取
""",
    "figure": """本次模块：图片专项核验
- Figure review defaults to main paper only：默认只核验主文 paper_id 的 figures，不自动全量核验已关联 SI figures。
- SI figures 只在显式触发时纳入：用户/请求 include_supplementary_figures=true、任务明确引用 Figure Sxx，或 DFT/机制候选 evidence anchor 指向 SI figure。触发后用 related_paper_id 读取相关 SI figure，修正和审核结果仍按证据归属写回。
- 先按 PDF 核对 figure 总数、编号、子图、页码和 caption，再检查现有对象，不能只审核已有 crop。
- content_summary 描述实际视觉信息，key_elements 写具体坐标轴、曲线、结构、图例和子图。
- content_summary 不得直接重复 caption，也不要以 caption 原句开头；应直接写子图内容、视觉要素和比较关系。
- 如果目标是记录图像核对 verdict（verified / needs_repair / rejected），优先调用 review_figure；如果目标是修正 figure_role、content_summary、key_elements、page、caption 等元数据，再走 import_analysis。
- 元数据修正走 import_analysis；重裁和补图分别直接调用 recrop_figure、create_figure_from_bbox。
- 不从图像臆读精确数值；但如果图中出现明确可读 DFT 数值或标注（adsorption energy、binding energy、dissociation energy、decomposition barrier、reaction barrier、free energy/ΔG、Bader charge、charge transfer 等），提取为 DFT candidate/object_review_audit，必须带 figure_id/figure_label、page、quoted_text 或图中可读标注、value、unit、property_type、adsorbate 或 reaction_step、material_identity（如能判断）。
- Figure-derived DFT values become candidates and require second review/safety gate；不得直接标记 ML_Ready、safe_verified 或 verified。
- 图片审核完成后，必须顺带判断该文献当前系统 paper_type 是否与原 PDF 内容相符；若不相符，不要只写 note，直接通过受控写回把 paper_type 改成正确值，并附上页码与 quoted_text 证据。
""",
    "table": """本次模块：表格专项核验
- 分别核对主文与已关联 SI 中的表格；SI 表格用 related_paper_id / read_paper_page_paper_id 读取。修改、删除、合并表格对象时使用该表对象真实 paper_id；从 SI 表格抽出的 DFT/机制等科学候选才按 writeback_paper_id 归属主文。
- 核对表格 caption、page、markdown_content、列对齐和跨页连续性。
- 可读取表格前后章节用于判断列语义和条件，但本模块不重复审核章节对象，章节问题转交文字审核。
- 表格内容需要修正时调用 update_table，不要只提交 import_analysis 修正 JSON。
- 系统漏掉整张表时调用 create_table，必须带 page/table/quoted_text 等 evidence_payload。
- 解析器把跨页表、尾行表或重复表拆成多个对象时，优先调用 merge_table(source_table_id, target_table_id)；如无需更新 target markdown_content，也要用 merge_table/delete_table 删除多余 source，并保留证据。
- 确认表对象无效或重复时调用 delete_table；禁止直接 SQL 删除，禁止只写“后台请求”笔记。
- 无需修正的表格可以留下绑定 table UUID 与页证据的 PASS object_review_audit；但对象级修改/新建/删除/合并必须走表格 MCP 工具。
- 表格核验完成后回读 get_paper/get_codex_item：确认保留表 table_review_status 已由 approved correction 或 finalized positive audit 派生为系统 verified，重复表不再显示或有删除审计快照。
- 疑似 DFT 列映射错误只转交 DFT 专项，不在本模块确认 DFT。
""",
    "sections_writing": """本次模块：章节与写作卡核验
- 核对 abstract、section_title、section_type、text、page_start/page_end。
- 同时核对 section_level、section_number、parent_heading、heading_path；不得把多级标题重新压平。
- 新建章节必须保留上述层级字段和 PDF 页证据。
- 写作卡只能引用已核对证据；未审核 parser section/写作卡不得进入默认 RAG 或正式写作。
- 涉及 DFT 数值时只标记转交 DFT 专项，不在本模块解除 DFT 门禁。
""",
    "text_review": """本次模块：文字审核
- 核对 abstract、section_title、section_type、text、page_start/page_end。
- 同时核对 section_level、section_number、parent_heading、heading_path；不得把多级标题重新压平。
- 新建章节必须保留上述层级字段和 PDF 页证据。
- 写作卡只能引用已核对证据；未审核 parser section/写作卡不得进入默认 RAG 或正式写作。
- 机理声明必须核对 claim_text、claim_type、key_species、mechanism_direction、evidence_text，并补齐页码与原文证据锚点。
- 涉及 DFT 数值时只标记转交 DFT 专项，不在本模块解除 DFT 门禁。
""",
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
        return """目标反应上下文（ReactionProfile）
- 本次 target_reaction=未指定；这是通用 DFT 审核任务，不按单一反应 profile 预先限制物种或性质。
- 每条 DFT 结果必须依据 PDF 证据独立判断真实 reaction_type；可使用 SRR_LiS、HER、OER、ORR、CO2RR 或 UNKNOWN。
- 如果证据不足或跨反应歧义，写 reaction_type="UNKNOWN" 并说明 ambiguous/needs_manual_review，不能猜测。
- corrected_value 尽量包含 reaction_type、property_type、adsorbate、reaction_step、material_identity、value、unit、method 或 calculation_setting。
- 面向表格型 ML 的候选还要说明是否能绑定结构/位点/材料身份、DFT setting、页码/表格/quoted_text 证据。
- 缺 reaction_type、adsorbate/reaction_step、材料身份、setting 或证据定位时，不要 PASS；写 PROPOSED、NEEDS_HUMAN 或 new_candidate，并把缺口写进 reason。
"""

    key = normalize_reaction_type(target_reaction)
    profile = get_reaction_profile(key)
    target_display = str(target_reaction or "未指定").strip() or "未指定"
    return f"""目标反应上下文（ReactionProfile）
- 本次 target_reaction={target_display}；规范化 profile={profile.key}；profile_status={profile.status}；profile_version={profile.version}。
- 这个 target_reaction 只是本次审核任务上下文，不是强制归类。每条 DFT 结果仍必须依据 PDF 证据独立判断真实 reaction_type。
- 如果候选明确属于其它反应，应在 corrected_value 中写实际 reaction_type；如果证据不足或跨反应歧义，写 reaction_type="UNKNOWN" 并说明 ambiguous/needs_manual_review，不能硬改成目标反应。
- 允许中间体/吸附物：{_format_list(profile.allowed_intermediates)}。
- 允许性质类型：{_format_list(profile.allowed_properties)}。
- 推荐单位：{", ".join(f"{name}={unit}" for name, unit in sorted(profile.canonical_units.items())) or "无"}。
- 反应步参考图：{_format_step_graph(profile.step_graph)}。
- 正向语境词：{_format_list(profile.required_context_terms)}。
- 排除语境词：{_format_list(profile.exclusion_context_terms)}。

DFT new_candidate / PROPOSED corrected_value 额外要求：
- 尽量包含 reaction_type、property_type、adsorbate、reaction_step、material_identity、value、unit、method 或 calculation_setting。
- 面向表格型 ML 的候选还要说明是否能绑定结构/位点/材料身份、DFT setting、页码/表格/quoted_text 证据。
- 缺 reaction_type、adsorbate/reaction_step、材料身份、setting 或证据定位时，不要 PASS；写 PROPOSED、NEEDS_HUMAN 或 new_candidate，并把缺口写进 reason；这些输出是 DFT audit issue 草案，不是最终数据库真值。
"""


def _project_library_prompt_fragment(context_key: Any, target_reaction: Any = None) -> str:
    context = get_project_library_context(context_key)
    field_map = {
        field.canonical_key: field
        for field in list_topic_field_definitions(context.key)
    }
    structure_keys = [
        key
        for key in (
            "metal_centers",
            "catalyst_scope",
            "metal_pairing_type",
            "support_material",
            "coordination_environment",
            "metal_metal_distance",
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
    target_display = str(target_reaction or "").strip() or "未指定"
    return f"""专题项目库上下文（ProjectLibraryContext）
- 当前专题库={context.display_name_zh}；context_key={context.key}；context_version={context.version}；target_reaction={target_display}。
- 本专题面向 Li-S 单/双原子催化剂项目库，重点语义：{_format_list(context.semantic_focus_terms)}。
- 重点中间体/物种：{_format_list(context.intermediate_terms)}；Plain S8 仍需结合 Li-S/SRR 语境，不要把补充材料编号误当反应物种。
- 重点结构字段：{_format_list(structure_keys)}。审核时优先明确材料身份、活性位点身份、metal centers、SAC/DAC、同核/异核、support、coordination、M-M distance；证据不足保持 UNKNOWN/null。
- 重点 DFT 标签：{_format_list(dft_keys)}。重点检查 Li2Sx / Li2S 吸附、Li2S 成核/分解、迁移/反应能垒，以及材料级描述符与其证据锚点。
- 证据锚点至少要能回连 page、table、figure、quoted_text、evidence_text 之一；没有明确锚点时不要把候选写成 ready/verified。
- 不自动升级 safe_verified/verified；target_reaction 只是专题上下文，不能强制套到所有 DFT 行。证据不足、字段缺失或跨反应歧义时保持 UNKNOWN/null，并把 blocker 写清楚。
"""


def build_project_library_prompt_templates() -> dict[str, dict[str, str]]:
    return {
        "li_s_sac_dac": {
            "dft": _project_library_prompt_fragment("li_s_sac_dac", "SRR_LiS"),
        }
    }


def _module_rule(
    module_kind: str,
    *,
    target_reaction: Any = None,
    project_library_context: Any = None,
) -> str:
    rule = _MODULE_RULES[module_kind].strip()
    if module_kind == "dft":
        additions = [_reaction_profile_context(target_reaction).strip()]
        if project_library_context is not None and str(project_library_context).strip():
            additions.append(_project_library_prompt_fragment(project_library_context, target_reaction).strip())
        return f"{rule}\n\n" + "\n\n".join(additions)
    return rule


def build_ide_review_prompt(
    kind: str = "overall",
    *,
    target_list: str = TARGET_LIST_TOKEN,
    source_label: str = SOURCE_LABEL_TOKEN,
    target_reaction: Any = None,
    project_library_context: Any = None,
) -> str:
    module_kinds = COMPOSITE_REVIEW_PROMPTS.get(kind, (kind if kind in _MODULE_RULES else "overall",))
    common = _COMMON_RULES.replace(TARGET_LIST_TOKEN, target_list).replace(SOURCE_LABEL_TOKEN, source_label)
    modules = "\n\n".join(
        _module_rule(
            module_kind,
            target_reaction=target_reaction,
            project_library_context=project_library_context,
        )
        for module_kind in module_kinds
    )
    return f"{common.strip()}\n\n{modules}\n"


def build_prompt_templates(*, target_reaction: Any = None) -> dict[str, str]:
    return {kind: build_ide_review_prompt(kind, target_reaction=target_reaction) for kind in SUPPORTED_REVIEW_PROMPTS}


def build_reaction_profile_templates() -> dict[str, dict[str, str]]:
    return {
        reaction_type: {"dft": build_ide_review_prompt("dft", target_reaction=reaction_type)}
        for reaction_type in REACTION_TYPES
    }


def build_reaction_profile_contexts() -> dict[str, str]:
    return {
        reaction_type: _reaction_profile_context(reaction_type)
        for reaction_type in REACTION_TYPES
    }


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
        "composite_templates": {
            kind: build_ide_review_prompt(kind) for kind in COMPOSITE_REVIEW_PROMPTS
        },
    }
