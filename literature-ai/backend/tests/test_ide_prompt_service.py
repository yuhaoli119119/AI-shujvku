from app.services.ide_prompt_service import (
    CANONICAL_MCP_PATH,
    PROMPT_SCHEMA_VERSION,
    build_ide_review_prompt,
    prompt_contract,
)


NON_DFT_KINDS = {"overall", "figure", "table", "sections_writing", "text_review"}


def test_prompt_contract_has_separate_templates_and_no_composite_prompt():
    contract = prompt_contract()

    assert contract["schema_version"] == PROMPT_SCHEMA_VERSION == "ide_review_prompt_v18"
    assert contract["canonical_mcp_path"] == CANONICAL_MCP_PATH == "/mcp"
    assert set(contract["supported_kinds"]) == {
        "overall",
        "dft",
        "figure",
        "table",
        "sections_writing",
        "text_review",
    }
    assert set(contract["templates"]) == set(contract["supported_kinds"])
    assert contract["composite_templates"] == {}
    assert contract["target_reaction_token"] == "{{TARGET_REACTION}}"
    assert set(contract["reaction_profile_templates"]) == {"SRR_LiS", "HER", "OER", "ORR", "CO2RR", "UNKNOWN"}
    assert all(set(templates) == {"dft"} for templates in contract["reaction_profile_templates"].values())
    assert set(contract["project_library_prompt_templates"]) == {"li_s_sac_dac"}


def test_prompts_are_concise_and_keep_target_tokens():
    contract = prompt_contract()

    for kind, prompt in contract["templates"].items():
        assert "{{TARGET_LIST}}" in prompt
        assert "{{SOURCE_LABEL}}" in prompt
        assert len(prompt) < (4200 if kind == "dft" else 3000)


def test_common_rules_keep_only_shared_execution_safety():
    prompt = build_ide_review_prompt(
        "overall",
        target_list="- human_ref=A0042 | paper_id=paper-uuid",
        source_label="codex_overall_20260703_120000",
    )

    assert "只处理本提示词指定的模块" in prompt
    assert "app.mcp.context.mcp_auth_context" in prompt
    assert "禁止直接调用 service/session/model、执行 SQL 或直接写数据库" in prompt
    assert "get_codex_context" in prompt
    assert "read_paper_page" in prompt
    assert "回读确认" in prompt
    assert "paper-uuid" in prompt
    assert "codex_overall_20260703_120000" in prompt
    assert "repair_dft" not in prompt
    assert "update_table" not in prompt
    assert "recrop_figure" not in prompt


def test_figure_prompt_is_figure_only_and_creates_only_explicit_dft_candidates():
    prompt = build_ide_review_prompt("figure")

    assert "任务：审核当前唯一目标文献的图片" in prompt
    assert "不处理表格或其他模块" in prompt
    assert "review_figure" in prompt
    assert "recrop_figure" in prompt
    assert "create_figure_from_bbox" in prompt
    assert "不从曲线估读、插值或推算精确数值" in prompt
    assert "只有图片或图注明确给出 DFT 结果" in prompt
    assert "数值、单位、property_type" in prompt
    assert 'decision="new_candidate"' in prompt
    assert "import_analysis(auto_apply_review_rules=true)" in prompt
    assert "未验证 DFT 候选" in prompt
    assert "进入 DFT 专项处理链路" in prompt
    assert "repair_dft" not in prompt
    assert "主 AI" not in prompt
    assert "审核 AI" not in prompt
    assert "update_table" not in prompt


def test_table_prompt_uses_only_table_tools():
    prompt = build_ide_review_prompt("table")

    assert "任务：审核当前主文献及其已关联 SI 的表格" in prompt
    for tool in ("update_table", "create_table", "merge_table", "delete_table"):
        assert tool in prompt
    assert "该表真实 paper_id" in prompt
    assert "发现非表格问题时交给对应专项任务" in prompt
    assert "review_figure" not in prompt
    assert "recrop_figure" not in prompt
    assert 'decision="new_candidate"' not in prompt
    assert "DFT" not in prompt


def test_text_modules_have_non_overlapping_scopes():
    sections = build_ide_review_prompt("sections_writing")
    text = build_ide_review_prompt("text_review")

    assert "只处理 sections 和 writing_cards" in sections
    assert "claim_text" not in sections
    assert "只处理 abstract 和 mechanism_claims" in text
    assert "claim_text" in text
    for prompt in (sections, text):
        assert "update_table" not in prompt
        assert "review_figure" not in prompt
        assert "repair_dft" not in prompt


def test_dft_review_prompt_is_dft_only_and_directly_applies_one_ai_opinion():
    prompt = build_ide_review_prompt("dft")

    assert "任务：审核并处理当前论文的 DFT 数据" in prompt
    assert "只处理当前 paper_id 的 dft_results" in prompt
    assert "禁止修改图片、表格、章节、元数据或其他对象" in prompt
    assert "主文及已关联 SI" in prompt
    assert "PASS、REVISE、REJECT 或 NEEDS_HUMAN" in prompt
    assert 'decision="new_candidate"' in prompt
    assert "import_analysis(auto_apply_review_rules=true)" in prompt
    assert "一份证据合格的 AI 意见" in prompt
    assert "不需要第二个 AI" in prompt
    assert "PASS 会确认当前值" in prompt
    assert "NEEDS_HUMAN 保持待人工" in prompt
    assert "update_table" not in prompt
    assert "review_figure" not in prompt


def test_reaction_profile_is_dft_only_and_does_not_force_classification():
    dft = build_ide_review_prompt("dft", target_reaction="SRR_LiS")
    figure = build_ide_review_prompt("figure", target_reaction="SRR_LiS")

    assert "target_reaction=SRR_LiS" in dft
    assert "profile=SRR_LiS" in dft
    assert "不得覆盖 PDF 证据" in dft
    assert "Li2S8" in dft
    assert "target_reaction" not in figure


def test_project_library_context_stays_inside_dft_prompt():
    prompt = build_ide_review_prompt(
        "dft",
        target_reaction="SRR_LiS",
        project_library_context="li_s_sac_dac",
    )

    assert "专题库：锂硫双原子" in prompt
    assert "metal_centers" in prompt
    assert "li2s_decomposition_barrier" in prompt
    assert "证据不足保持 UNKNOWN/null" in prompt
