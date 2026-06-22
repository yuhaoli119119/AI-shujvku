from __future__ import annotations

from typing import Final


PROMPT_SCHEMA_VERSION: Final = "ide_review_prompt_v5"
CANONICAL_MCP_PATH: Final = "/mcp"
TARGET_LIST_TOKEN: Final = "{{TARGET_LIST}}"
SOURCE_LABEL_TOKEN: Final = "{{SOURCE_LABEL}}"

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

证据与回写：
- 用 get_codex_item 和 read_paper_page 核对具体页；页眉页脚清理不会取消 page/page_start/page_end 来源。
- object_review_audits 的 evidence_location 和 correction_proposals 的 evidence_payload 优先使用结构化 dict，至少包含 page、table、figure、section、quoted_text、bbox、evidence_text 之一；不要只给一句模糊自然语言。
- 非 DFT 修正或创建对象时，直接调用 import_analysis(auto_apply_review_rules=true)；禁止申请模块写锁，后写入的 AI 结果允许覆盖先前 AI 结果。
- 如果当前会话里的 MCP import_analysis 路径不可用，而正式 HTTP API 已覆盖同一受控写回能力，可回退到 HTTP API：POST /api/external-analysis/import。
- 仅提交候选审计时可使用 auto_apply_review_rules=false，但不得宣称已应用。
- 图像裁剪只能直接调用 recrop_figure 或 create_figure_from_bbox，不能伪装成 import_analysis correction。
- DFT 是硬安全边界：单个 AI 不得最终确认 DFT，不得解锁导出。
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
- 漏提项使用 object_review_audits 的 new_candidate；希望形成未验证候选时必须 auto_apply_review_rules=true。
- 不从曲线估读精确数值；引用文献数据必须标记 borrowed_from_reference=true。
- PASS 仍不等于 safe_verified；保持多 AI/人工审核与导出门禁。

object_review_audits DFT new_candidate 结构规范（缺一不可）：
- target_type: "dft_results"（固定值）
- target_id: "new"（字符串字面量 "new"，不是 null）
- field_name: "dft_results"（复数，固定值）
- decision: "new_candidate"
- corrected_value 必须包含以下键：
  - material_identity: str — 材料/催化剂名称（必填）
  - property_type: str — 性质类型（必填）；优先使用后端已知或推荐值，例如
    activation_energy, adsorption_energy, reaction_barrier, permeation_barrier,
    permeance, free_energy, d_band_center, cohp, bader_charge；若原文性质明确且系统可接受，不要仅因不在推荐列表示例中就丢弃
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
    "figure": """本次模块：图表专项核验
- 先按 PDF 核对图表总数、编号、子图、页码和 caption，再检查现有对象，不能只审核已有 crop。
- content_summary 描述实际视觉信息，key_elements 写具体坐标轴、曲线、结构、图例和子图。
- content_summary 不得直接重复 caption，也不要以 caption 原句开头；应直接写子图内容、视觉要素和比较关系。
- 如果目标是记录图像核对 verdict（verified / needs_repair / rejected），优先调用 review_figure；如果目标是修正 figure_role、content_summary、key_elements、page、caption 等元数据，再走 import_analysis。
- 元数据修正走 import_analysis；重裁和补图分别直接调用 recrop_figure、create_figure_from_bbox。
- 不从图像臆读精确数值。
- 图表审核完成后，必须顺带判断该文献当前系统 paper_type 是否与原 PDF 内容相符；若不相符，不要只写 note，直接通过受控写回把 paper_type 改成正确值，并附上页码与 quoted_text 证据。
""",
    "table": """本次模块：表格与章节核验
- 核对表格 caption、page、markdown_content、列对齐和跨页连续性。
- 核对章节正文与 page_start/page_end；缺失对象使用受控 create correction。
- 无需修正的表格也要留下绑定 table UUID 与页证据的 PASS object_review_audit。
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


def build_ide_review_prompt(
    kind: str = "overall",
    *,
    target_list: str = TARGET_LIST_TOKEN,
    source_label: str = SOURCE_LABEL_TOKEN,
) -> str:
    module_kinds = COMPOSITE_REVIEW_PROMPTS.get(kind, (kind if kind in _MODULE_RULES else "overall",))
    common = _COMMON_RULES.replace(TARGET_LIST_TOKEN, target_list).replace(SOURCE_LABEL_TOKEN, source_label)
    modules = "\n\n".join(_MODULE_RULES[module_kind].strip() for module_kind in module_kinds)
    return f"{common.strip()}\n\n{modules}\n"


def build_prompt_templates() -> dict[str, str]:
    return {kind: build_ide_review_prompt(kind) for kind in SUPPORTED_REVIEW_PROMPTS}


def prompt_contract() -> dict[str, object]:
    return {
        "schema_version": PROMPT_SCHEMA_VERSION,
        "canonical_mcp_path": CANONICAL_MCP_PATH,
        "target_list_token": TARGET_LIST_TOKEN,
        "source_label_token": SOURCE_LABEL_TOKEN,
        "supported_kinds": list(SUPPORTED_REVIEW_PROMPTS),
        "templates": build_prompt_templates(),
        "composite_templates": {
            kind: build_ide_review_prompt(kind) for kind in COMPOSITE_REVIEW_PROMPTS
        },
    }
