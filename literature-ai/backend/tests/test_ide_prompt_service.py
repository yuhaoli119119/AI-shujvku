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
    assert set(contract["templates"]) == {"overall", "dft", "figure", "table", "sections_writing", "text_review"}
    assert set(contract["composite_templates"]) == {"figure_table"}


def test_figure_table_composite_keeps_one_common_preamble_and_both_modules():
    prompt = build_ide_review_prompt("figure_table")

    assert prompt.count("你现在是 Literature AI 的 IDE AI") == 1
    assert "本次模块：图表专项核验" in prompt
    assert "本次模块：表格与章节核验" in prompt


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
    assert "非 DFT 修正或创建对象时，直接调用 import_analysis" in prompt
    assert "非 DFT 不申请写锁" in build_ide_review_prompt("dft")
    assert "POST /api/external-analysis/import" in prompt
    assert "object_review_audits 的 evidence_location" in prompt
    assert "优先用 get_paper 或 get_codex_item 回读字段值" in prompt
    assert "不要把预览图、候选裁剪图、调试 JSON、临时分析文本写到仓库根目录" in prompt
    assert "outputs/tmp/" in prompt
    assert "outputs/exports/" in prompt
    assert "paper-uuid" in prompt
    assert "codex_overall_20260619_120000" in prompt


def test_sections_prompt_keeps_heading_hierarchy_fields():
    prompt = build_ide_review_prompt("sections_writing")

    for field in ("section_level", "section_number", "parent_heading", "heading_path"):
        assert field in prompt


def test_dft_prompt_never_allows_single_ai_final_approval():
    prompt = build_ide_review_prompt("dft")

    assert "单个 AI 不得最终确认 DFT" in prompt
    assert "PASS 仍不等于 safe_verified" in prompt


def test_figure_prompt_distinguishes_review_verdicts_from_metadata_writeback():
    prompt = build_ide_review_prompt("figure")

    assert "优先调用 review_figure" in prompt
    assert "再走 import_analysis" in prompt
    assert "不得直接重复 caption" in prompt


def test_text_review_prompt_keeps_mechanism_fields():
    prompt = build_ide_review_prompt("text_review")

    assert "本次模块：文字审核" in prompt
    assert "claim_text" in prompt
    assert "mechanism_direction" in prompt
