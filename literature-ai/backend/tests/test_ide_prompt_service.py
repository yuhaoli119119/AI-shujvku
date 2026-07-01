from app.services.ide_prompt_service import (
    CANONICAL_MCP_PATH,
    PROMPT_SCHEMA_VERSION,
    build_ide_review_prompt,
    prompt_contract,
)


def test_prompt_contract_has_one_canonical_mcp_path_and_all_modules():
    contract = prompt_contract()

    assert contract["schema_version"] == PROMPT_SCHEMA_VERSION
    assert contract["canonical_mcp_path"] == CANONICAL_MCP_PATH == "/mcp"
    assert set(contract["supported_kinds"]) == {
        "overall",
        "dft",
        "dft_primary",
        "figure",
        "table",
        "sections_writing",
        "text_review",
    }
    assert set(contract["templates"]) == set(contract["supported_kinds"])
    assert set(contract["composite_templates"]) == {"figure_table"}
    assert contract["target_reaction_token"] == "{{TARGET_REACTION}}"
    assert set(contract["reaction_profile_templates"]) == {"SRR_LiS", "HER", "OER", "ORR", "CO2RR", "UNKNOWN"}
    assert all("dft_primary" in templates for templates in contract["reaction_profile_templates"].values())
    assert set(contract["reaction_profile_contexts"]) == {"SRR_LiS", "HER", "OER", "ORR", "CO2RR", "UNKNOWN"}
    assert set(contract["project_library_contexts"]) == {"li_s_sac_dac"}
    assert set(contract["topic_field_dictionaries"]) == {"li_s_sac_dac"}
    assert set(contract["project_library_prompt_templates"]) == {"li_s_sac_dac"}
    assert "dft_primary" in contract["project_library_prompt_templates"]["li_s_sac_dac"]


def test_figure_table_composite_keeps_one_common_preamble_and_both_modules():
    prompt = build_ide_review_prompt("figure_table")

    assert prompt.count("你现在是 Literature AI 的 IDE AI") == 1
    assert "本次模块：图片专项核验" in prompt
    assert "本次模块：表格专项核验" in prompt


def test_figure_and_table_prompts_are_separate_and_si_aware():
    figure_prompt = build_ide_review_prompt("figure")
    table_prompt = build_ide_review_prompt("table")

    assert "本次模块：图片专项核验" in figure_prompt
    assert "本次模块：表格专项核验" not in figure_prompt
    assert "主文图片审核和支撑文献图片审核是审核中心的两个独立入口" in figure_prompt
    assert "不能把主文图片、SI 图片和表格混成同一个任务" in figure_prompt
    assert "Figure review defaults to main paper only" in figure_prompt
    assert "不自动全量核验已关联 SI figures" in figure_prompt
    assert "本次模块：表格专项核验" in table_prompt
    assert "本次模块：图片专项核验" not in table_prompt
    assert "只允许从审核中心选择一篇主文献发起" in table_prompt
    assert "同时检查主文表格和已关联 SI 表格" in table_prompt
    assert "已关联 SI 中的表格" in table_prompt


def test_common_prompt_preserves_controlled_in_process_fallback_and_safety_gates():
    prompt = build_ide_review_prompt(
        "overall",
        target_list="- human_ref=A0042 | paper_id=paper-uuid | library_name=library",
        source_label="codex_overall_20260619_120000",
    )

    assert "app.mcp.context.mcp_auth_context" in prompt
    assert "app.mcp.server" in prompt
    assert "禁止直接导入 service/session/model" in prompt
    assert "pdf_quality_status 属于 A_text_readable 或 B_text_partial" in prompt
    assert "blocked_by_pdf_quality" in prompt
    assert "DFT 的 verified/safe_verified/export gate" in prompt
    assert "后写入的 AI 结果允许覆盖先前 AI 结果" in prompt
    assert "禁止申请模块写锁" in build_ide_review_prompt("figure")
    assert "非 DFT 普通文本/结构化字段修正或创建对象时，直接调用 import_analysis" in prompt
    assert "表格对象生命周期是直接 MCP 工具路径" in prompt
    assert "update_table" in prompt
    assert "create_table" in prompt
    assert "delete_table" in prompt
    assert "merge_table" in prompt
    assert "不要只写“后台请求”笔记" in prompt
    assert "调用表格工具时必须使用该表对象真实归属的 paper_id" in prompt
    assert "本轮允许写入的 target_type 只有 dft_results" in build_ide_review_prompt("dft")
    assert "POST /api/external-analysis/import" in prompt
    assert "object_review_audits 的 evidence_location" in prompt
    assert "优先用 get_paper 或 get_codex_item 回读字段值" in prompt
    assert "不要把预览图、候选裁剪图、调试 JSON、临时分析文本写到仓库根目录" in prompt
    assert "outputs/tmp/" in prompt
    assert "outputs/exports/" in prompt
    assert "paper-uuid" in prompt
    assert "codex_overall_20260619_120000" in prompt
    assert "context.source_documents" in prompt
    assert 'source_document_type="supplementary_information"' in prompt
    assert "related_paper_id / read_paper_page_paper_id" in prompt
    assert "writeback_paper_id" in prompt
    assert "blocked_by_supplementary_evidence_unavailable" in prompt
    assert "图片核验默认只核验主文 paper_id 的 figures" in prompt
    assert "include_supplementary_figures=true" in prompt
    assert "英文专业系统词、状态码、字段名或工具名第一次出现时" in prompt
    assert "candidate（候选数据）" in prompt
    assert "JSON、代码块、工具参数和数据库字段值必须保持原样" in prompt


def test_sections_prompt_keeps_heading_hierarchy_fields():
    prompt = build_ide_review_prompt("sections_writing")

    for field in ("section_level", "section_number", "parent_heading", "heading_path"):
        assert field in prompt


def test_dft_prompt_never_allows_single_ai_final_approval():
    prompt = build_ide_review_prompt("dft")

    assert "你是 Literature AI 的 DFT 数据审核员" in prompt
    assert "你的职责：" in prompt
    assert "审核员工具与权限：" in prompt
    assert "审核员执行规则：" in prompt
    assert "主 AI" not in prompt
    assert "数据处理员" not in prompt
    assert "dft_primary_repair" not in prompt
    assert "needs_ordinary_ai_review" not in prompt
    assert "双 AI" not in prompt
    assert "多 AI" not in prompt
    assert "第一审" not in prompt
    assert "第二审" not in prompt
    assert "本轮唯一目标：只核验 DFT 数据" in prompt
    assert "本轮允许写入的 target_type 只有 dft_results" in prompt
    assert "读取证据时必须允许读取主文和已关联 SI 中与 DFT 直接相关的正文、表格" in prompt
    assert "DFT 漏项常在普通 tables 对象或 PDF 表格里" in prompt
    assert "禁止处理或修改 figure、writing_card、mechanism_claim、metadata、普通表格对象" in prompt
    assert "不得修改、合并、删除或新建任何普通表格对象" in prompt
    assert "本轮不得写任何非 DFT correction_proposals、object_review_audits 或普通对象修正" in prompt
    assert "非 DFT 普通文本/结构化字段修正或创建对象时" not in prompt
    assert "表格对象生命周期是直接 MCP 工具路径" not in prompt
    assert "核对元数据、摘要、章节、表格、figure 元信息" not in prompt
    assert "本任务只处理审核中心选中的当前这一篇主文献" in prompt
    assert "检查这篇主文献已有 DFT 数据有没有" in prompt
    assert "同时检查主文与已关联 SI 的 DFT 表格/文本是否还有漏掉" in prompt
    assert 'decision="new_candidate"' in prompt
    assert "DFT 数据审核员不能调用 repair_dft_audit_issue" in prompt
    assert "不能调用 verify_dft_result / reject_dft_result" in prompt
    assert "不能写 human_verified、safe_verified 或 ML_Ready" in prompt
    assert "不能说数据已经可导出" in prompt
    assert "PASS 仍不等于 safe_verified" in prompt
    assert "ML-predicted 数量代替" in prompt
    assert "不是 DFT 计算结果" in prompt
    assert "RDS 对应吉布斯自由能属于自由能变化" in prompt
    assert "自由能变化、反应能垒、迁移能垒、Li2S 分解能垒不得混用" in prompt
    assert "本次 target_reaction=未指定" in prompt
    assert "不按单一反应 profile 预先限制物种或性质" in prompt
    assert "同时检查主文与已关联 SI 的 DFT 表格/文本" in prompt
    assert "DFT AI 审核层已重置" in prompt
    assert "从当前 PDF、SI 与系统候选重新审核" in prompt
    assert "只生成本地 JSON" in prompt
    assert "API 中仍为 0 条 AI 意见，都不得报告 completed" in prompt
    assert "不得把计划提交数当成实际写入数" in prompt
    assert "candidate（候选数据）" in prompt


def test_dft_primary_prompt_is_paper_scoped_and_uses_fast_processing_path():
    prompt = build_ide_review_prompt("dft_primary")

    assert "你是 Literature AI 的 DFT 数据处理员" in prompt
    assert "你的职责：" in prompt
    assert "数据处理工具与权限：" in prompt
    assert "数据处理执行规则：" in prompt
    assert "普通 AI" not in prompt
    assert "数据审核员" not in prompt
    assert "审核员" not in prompt
    assert "双 AI" not in prompt
    assert "第一审" not in prompt
    assert "第二审" not in prompt
    assert "本轮唯一目标：只核验 DFT 数据" in prompt
    assert "本轮允许写入的 target_type 只有 dft_results" in prompt
    assert "issue_count=0 不是阻塞原因" in prompt
    assert "不得切换到 overall、figure、table、sections 或 text_review 任务" in prompt
    assert "非 DFT 普通文本/结构化字段修正或创建对象时" not in prompt
    assert "表格对象生命周期是直接 MCP 工具路径" not in prompt
    assert "核对元数据、摘要、章节、表格、figure 元信息" not in prompt
    assert "本次模块：DFT 数据处理员快速处理" in prompt
    assert "当前这一篇主文献的 DFT audit issue / DFT candidate" in prompt
    assert "不要跨 paper_id 抓取全库队列" in prompt
    assert "get_dft_audit_issues(paper_id=<当前 paper_id>)" in prompt
    assert "blocked_by_missing_current_paper_id" in prompt
    assert "已有 propose_corrections、review_dft 或 repair_dft_issues 任一 DFT 写权限即可处理" in prompt
    assert "repair_dft_audit_issues_batch(paper_id=<当前 paper_id>, auto_finalize=true)" in prompt
    assert "单条重试才使用 repair_dft_audit_issue" in prompt
    assert "不得因为没有 dft_primary_repair key 或 repair_dft_issues capability 报告阻断" in prompt
    assert "blocked_by_missing_primary_repair_identity" not in prompt
    assert "issue_count=0 不是阻塞原因" in prompt
    assert "object_review_audits 为 0 也不阻塞" in prompt
    assert "context.dft_review_handoff" in prompt
    assert "apply_analysis_review_rules" in prompt
    assert "不得把已导入但尚未 apply 的审核意见误判为“0 条审核意见”" in prompt
    assert "needs_ordinary_ai_review" not in prompt
    assert "不因缺少第二身份、专用 repair key 或人工终审而停止" in prompt
    assert "修复后直接调用确认或拒绝工具完成收口" in prompt
    assert "只有批量返回的失败项才逐条重试" in prompt
    assert "单项失败只记录该项错误并继续其余对象" in prompt
    assert "详情页人工确认" not in prompt
    assert "前置门可进入且存在 DFT candidates 时必须逐条写入 PASS/REVISE/REJECT/NEEDS_HUMAN" not in prompt


def test_dft_prompt_can_inject_srr_lis_reaction_profile_without_forcing_labels():
    prompt = build_ide_review_prompt("dft", target_reaction="SRR_LiS")

    assert "本次 target_reaction=SRR_LiS" in prompt
    assert "profile=SRR_LiS" in prompt
    assert "Li2S8" in prompt
    assert "adsorption_energy" in prompt
    assert "li2s_decomposition_barrier" in prompt
    assert "每条 DFT 结果仍必须依据 PDF 证据独立判断真实 reaction_type" in prompt
    assert "不能硬改成目标反应" in prompt
    assert "reaction_type" in prompt
    assert "tabular ML" not in prompt


def test_non_dft_prompt_does_not_receive_reaction_profile_context():
    prompt = build_ide_review_prompt("figure", target_reaction="SRR_LiS")

    assert "本次模块：图片专项核验" in prompt
    assert "目标反应上下文" not in prompt


def test_dft_prompt_can_include_project_library_topic_requirements_without_forcing_verification():
    prompt = build_ide_review_prompt(
        "dft",
        target_reaction="SRR_LiS",
        project_library_context="li_s_sac_dac",
    )

    assert "专题项目库上下文（ProjectLibraryContext）" in prompt
    assert "锂硫双原子" in prompt
    assert "Li2S8" in prompt
    assert "li2s_decomposition_barrier" in prompt
    assert "metal_centers" in prompt
    assert "coordination_environment" in prompt
    assert "证据不足保持 UNKNOWN/null" in prompt
    assert "不自动升级 safe_verified/verified" in prompt


def test_figure_prompt_distinguishes_review_verdicts_from_metadata_writeback():
    prompt = build_ide_review_prompt("figure")

    assert "优先调用 review_figure" in prompt
    assert "再走 import_analysis" in prompt
    assert "不得直接重复 caption" in prompt
    assert "Figure-derived DFT values become candidates and require second review/safety gate" in prompt
    assert "不得直接标记 ML_Ready" in prompt
    assert "adsorption energy" in prompt
    assert "Bader charge" in prompt


def test_table_prompt_requires_direct_table_mcp_tools():
    prompt = build_ide_review_prompt("table")

    assert "表格内容需要修正时调用 update_table" in prompt
    assert "系统漏掉整张表时调用 create_table" in prompt
    assert "优先调用 merge_table" in prompt
    assert "确认表对象无效或重复时调用 delete_table" in prompt
    assert "禁止直接 SQL 删除" in prompt
    assert "不要只提交 import_analysis 修正 JSON" in prompt
    assert "该表对象真实 paper_id" in prompt


def test_text_review_prompt_keeps_mechanism_fields():
    prompt = build_ide_review_prompt("text_review")

    assert "本次模块：文字审核" in prompt
    assert "claim_text" in prompt
    assert "mechanism_direction" in prompt
