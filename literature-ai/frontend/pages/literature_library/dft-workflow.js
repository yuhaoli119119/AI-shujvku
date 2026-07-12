// DFT review prompts, automated settlement, and imported opinions.
function buildBlockedDftFallbackPrompt(row, index) {
    const blockedReasons = Array.isArray(row && row.blocked_reasons) ? row.blocked_reasons : [];
    return [
        "## 候选 " + (index + 1),
        "Candidate ID: " + (row.record_id || row.id || "-"),
        "Property: " + (row.property_type || "-"),
        "Adsorbate: " + (row.adsorbate || "-"),
        "Value: " + (row.value == null ? "-" : row.value) + " " + (row.unit || ""),
        "Blocked reasons: " + (blockedReasons.length ? blockedReasons.join(", ") : "none"),
        "Recommended action: " + (row.recommended_action || "review_candidate"),
        "Evidence excerpt: " + (row.evidence_text || row.evidence_preview || "-"),
        "Source section/figure: " + (row.source_section || "-") + " / " + (row.source_figure || "-"),
    ].join("\n");
}

function inferReactionProfileFromText(text) {
    const value = String(text || "").toLowerCase();
    if (!value) return "UNKNOWN";
    if (
        /\b(li[-\s]?s|lithium[-\s]?sulfur|srr)\b/.test(value) ||
        value.includes("锂硫") ||
        value.includes("多硫化") ||
        value.includes("硫还原") ||
        value.includes("shuttle effect")
    ) return "SRR_LiS";
    if (/\b(her|hydrogen evolution)\b/.test(value)) return "HER";
    if (/\b(oer|oxygen evolution)\b/.test(value)) return "OER";
    if (/\b(orr|oxygen reduction)\b/.test(value)) return "ORR";
    if (/\b(co2rr|co2 reduction|carbon dioxide reduction)\b/.test(value)) return "CO2RR";
    return "UNKNOWN";
}

function inferPromptTargetReactionForSelectedPaper(kind) {
    if (kind !== "dft") return "UNKNOWN";
    const paper = state.selectedPaper || {};
    const dftItems = Array.isArray(paper.dft_results_items) ? paper.dft_results_items : [];
    const dftText = dftItems.map(function(item) {
        return [
            item && item.reaction_type,
            item && item.reaction_step,
            item && item.adsorbate,
            item && item.property_type,
            item && item.evidence_text
        ].filter(Boolean).join(" ");
    }).join(" ");
    return inferReactionProfileFromText([
        paper.library_name,
        paper.paper_code,
        paper.title,
        paper.title_zh,
        paper.abstract,
        paper.keywords,
        dftText
    ].filter(Boolean).join(" "));
}

async function canonicalIdePromptForSelectedPaper(kind) {
    const guide = await fetchJSON("/api/system/agent-guide");
    const contract = guide && guide.prompt_contract ? guide.prompt_contract : {};
    const templates = contract.templates && typeof contract.templates === "object" ? contract.templates : {};
    const reactionProfileTemplates = contract.reaction_profile_templates && typeof contract.reaction_profile_templates === "object"
        ? contract.reaction_profile_templates
        : {};
    const targetReaction = inferPromptTargetReactionForSelectedPaper(kind);
    const profileTemplates = reactionProfileTemplates[targetReaction] && typeof reactionProfileTemplates[targetReaction] === "object"
        ? reactionProfileTemplates[targetReaction]
        : {};
    const template = profileTemplates[kind] || templates[kind] || templates.overall || guide.suggested_client_prompt || "";
    if (!template) return "";

    const paper = state.selectedPaper || {};
    const paperId = paper.paper_id || paper.id || state.selectedPaperId || "-";
    const humanRef = paper.paper_code || paperId;
    const libraryName = paper.library_name ||
        (typeof getCurrentLibraryName === "function" ? getCurrentLibraryName() : "") || "-";
    const targetList = "- human_ref=" + humanRef + " | paper_id=" + paperId + " | library_name=" + libraryName;
    const now = new Date();
    const pad = function(value) { return String(value).padStart(2, "0"); };
    const runTag = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "_" +
        pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    const sourceLabel = "<agent_name>_" + kind + "_" + runTag;
    return String(template)
        .split(contract.target_list_token || "{{TARGET_LIST}}").join(targetList)
        .split(contract.source_label_token || "{{SOURCE_LABEL}}").join(sourceLabel)
        .split(contract.target_reaction_token || "{{TARGET_REACTION}}").join(targetReaction);
}

function buildBlockedDftBatchPrompt(rows) {
    const paper = state.selectedPaper || {};
    const title = paper.title_zh || paper.title || "Untitled paper";
    const doi = paper.doi || "-";
    const paperId = paper.paper_id || paper.id || state.selectedPaperId || "-";
    const paperCode = paper.paper_code || "";
    const header = [
        "任务：只处理当前论文里“需处理 / 不可导出”的 DFT 候选，不要重编数据，也不要碰已可导出的记录。",
        "要求：你必须逐条核对 PDF 证据、材料身份、性质类型、数值、单位、证据原文和页码/表格/图号定位。",
        "输出：每条候选只能给出 accept / reject / needs_fix / suspected_duplicate / suspected_missing 之一，并说明理由与证据位置；无法确认时不要 accept。",
        "",
        "Paper title: " + title,
        "DOI: " + doi,
        "paper_id: " + paperId,
        paperCode ? ("paper_code: " + paperCode) : "",
        "Blocked candidate count: " + rows.length,
    ].filter(Boolean).join("\n");
    const body = rows.map(function(row, index) {
        return row.review_prompt || buildBlockedDftFallbackPrompt(row, index);
    }).join("\n\n");
    return header + "\n\n" + body;
}

function buildCompactBlockedDftRow(row, index) {
    const blockedReasons = Array.isArray(row && row.blocked_reasons) ? row.blocked_reasons : [];
    const evidencePage = row && row.evidence_check ? row.evidence_check.primary_page : null;
    return [
        "候选 " + (index + 1) + " | target_id=" + (row.record_id || row.id || "-"),
        "property=" + (row.property_type || "-") +
            " | adsorbate=" + (row.adsorbate || "-") +
            " | value=" + (row.value == null ? "-" : row.value) + " " + (row.unit || ""),
        "blocked=" + (blockedReasons.length ? blockedReasons.join(", ") : "none") +
            " | action=" + (row.recommended_action || "review_candidate") +
            " | page=" + (evidencePage == null ? "-" : evidencePage),
        "workflow=" + (row.dft_workflow_label || row.dft_workflow_state || "-") +
            " | next_required_action=" + (row.next_required_action || "-"),
        "workflow_reason=" + clipText(row.dft_workflow_reason || "-", 180),
        "source=" + (row.source_section || "-") + " / " + (row.source_figure || "-"),
        "evidence=\"" + clipText(row.evidence_text || row.evidence_preview || "-", 140) + "\"",
    ].join("\n");
}

function buildCompactBlockedDftBatchPrompt(rows) {
    const paper = state.selectedPaper || {};
    const title = paper.title_zh || paper.title || "Untitled paper";
    const doi = paper.doi || "-";
    const paperId = paper.paper_id || paper.id || state.selectedPaperId || "-";
    const paperCode = paper.paper_code || "";
    const sourcePdf = (
        paper.codex_context &&
        paper.codex_context.source_assets &&
        paper.codex_context.source_assets.pdf_path
    ) || "<source_pdf>";
    const header = [
        "任务：在一次处理中完成已有 DFT 候选核验和全文证据查漏；不要把清单内候选重新当成新数据提交。",
        "要求：先核对 PDF 证据，再逐条给出完整意见。系统按独立 candidate_id 审核提交计票，不按 AI、模型或 source_label 去重；同一模型可以再次提交下一轮审核。",
        "强制规则：清单内每条候选都必须使用该行给出的 target_id；禁止对清单内候选输出 target_id='new' 或 decision='new_candidate'。",
        "有效 AI 意见必须在 evidence_location 同时填写 page 和 quoted_text；缺任一项都不会计入第二意见或裁决依据。",
        "不要输出长解释；只输出一个可直接用于 import_analysis 的 JSON，顶层只保留 object_review_audits。",
        "如果当前 IDE 没有暴露 MCP 工具，不要直接停下；请通过仓库内 `app.mcp.context.mcp_auth_context` 建立明确身份，再受控调用 `app.mcp.server` 已公开的 MCP 工具。禁止直接调用 service/session/model 或数据库。",
        "",
        "决策规则：",
        "- 证据、材料身份、数值、单位、定位都能确认且无需改字段时，用 PASS。",
        "- 候选明显错误、重复、无证据支持时，用 REJECT。",
        "- 候选基本正确但字段需要修正/补全时，用 PROPOSED，并填写完整 corrected_value。",
        "- 无法从 PDF 确认时，用 NEEDS_HUMAN，不要硬判。",
        "- 清单内候选只允许 PASS / REJECT / PROPOSED / NEEDS_HUMAN，不能用 new_candidate。",
        "- 处理完清单后必须继续扫描可读取的 PDF 正文、已审核图表和 SI 证据；发现清单外确实漏提的额外 DFT 行时，追加 decision='new_candidate'、target_id='new'、field_name='dft_results'。",
        "- 追加漏提行后，不要只停在 candidate-only JSON；实际调用 import_analysis 时应使用 auto_apply_review_rules=true，让 new_candidate 自动进入未验证 DFT 候选队列。",
        "- 缺 material identity、缺证据原文、缺准确页码定位时，不要 PASS。",
        "- 即使是 duplicate / REJECT，也必须给出 evidence_location.page 和 quoted_text；重复项还要在 reason 或 corrected_value.duplicate_of 写明保留项 target_id。",
        "- 单位标准：能量统一为 eV，meV 除以 1000；渗透率统一为 GPU，10^3 GPU 乘以 1000；原始表达写入 raw_value/raw_unit 或 evidence_location.quoted_text。",
        "",
        "输出模板：",
        "{",
        '  "object_review_audits": [',
        "    {",
        '      "paper_id": "' + paperId + '",',
        '      "target_type": "dft_results",',
        '      "target_id": "<必须填写候选清单中该行的 target_id；清单外漏提项才允许 new>",',
        '      "field_name": "<例如 value / unit / catalyst_sample_id / dft_results>",',
        '      "decision": "PASS | REJECT | PROPOSED | NEEDS_HUMAN；清单外漏提项才允许 new_candidate",',
        '      "corrected_value": {"property": "<标准性质>", "adsorbate": "<标准吸附物>", "material": "<标准材料/结构>", "method": "<方法/条件>", "value": 0.0, "unit": "eV/GPU/%", "raw_value": "<原文数值>", "raw_unit": "<原文单位>"},',
        '      "confidence": 0.0,',
        '      "reason": "<简短理由>",',
        '      "normalized_material": "<标准化材料/结构>",',
        '      "normalized_energy_type": "<标准化性质/能量类型>",',
        '      "evidence_location": {"page": <页码或 null>, "table": "<表号，可省略>", "quoted_text": "<证据短句>", "source_document_type": "main | si | supporting_reference", "source_pdf": "' + sourcePdf + '"}',
        "    }",
        "  ]",
        "}",
        "",
        "论文：",
        "title=" + title,
        "doi=" + doi,
        "paper_id=" + paperId,
        paperCode ? ("paper_code=" + paperCode) : "",
        "新数据审核数量=" + rows.length,
        "",
        "新数据审核候选清单：",
    ].filter(Boolean).join("\n");
    const body = rows.map(function(row, index) {
        const sources = (row.object_review_audits || []).map(dftOpinionSource).filter(function(source, sourceIndex, all) {
            return all.indexOf(source) === sourceIndex;
        });
        return "existing_review_sources=" + JSON.stringify(sources) + "\n" + buildCompactBlockedDftRow(row, index);
    }).join("\n\n");
    return header + "\n\n" + body;
}

async function settleDftConsensusBeforePrompt() {
    return fetchJSON(
        API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/settle-ai-dft-reviews",
        { method: "POST" }
    );
}

async function copyNewDftReviewPrompt() {
    showToast("正式 DFT 数据审核提示词请从审核中心按单篇文献复制。", "info");
}

async function autoProcessLowRiskDftRows() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    showToast("低风险自动处理入口已停用；DFT final truth 需在 DFT 详情页 verify/reject。", "info");
}

function decorateDftReadinessPanel(detail) {
    const panel = $("dftContent");
    if (!panel) return;
    const card = panel.querySelector('[data-role="dft-status-panel"]');
    if (!card || card.querySelector('[data-role="dft-readiness-actions"]')) return;
    const actions = document.createElement("div");
    actions.setAttribute("data-role", "dft-readiness-actions");
    actions.style.display = "flex";
    actions.style.gap = "8px";
    actions.style.flexWrap = "wrap";
    actions.style.margin = "0 0 10px";
    actions.innerHTML =
        '<button class="btn ghost small" type="button" onclick="settleAiDftReviews()">刷新审核状态</button>' +
        '<button class="btn ghost small" type="button" onclick="resetDftAiReviewsForPaper()">清除 AI 审核重来</button>' +
        '<button class="btn ghost small" type="button" onclick="openSelectedReviewCenter()">打开审核中心</button>';
    const firstSubtle = card.querySelector(".subtle");
    if (firstSubtle && firstSubtle.parentNode === card) {
        card.insertBefore(actions, firstSubtle);
    } else {
        card.appendChild(actions);
    }
}

function copyPaperIdentity() {
    if (!state.selectedPaper) return;
    const stablePaperId = state.selectedPaper.paper_id || state.selectedPaper.id || "";
    const displayCode = state.selectedPaper.paper_code || "";
    const value = [
        displayCode ? ("文献短号: " + displayCode) : "",
        stablePaperId ? ("paper_id: " + stablePaperId) : "",
        state.selectedPaper.title || "",
        state.selectedPaper.doi || ""
    ].filter(Boolean).join("\n");
    navigator.clipboard.writeText(value).then(function() {
        showToast("已复制标题和 DOI。", "success");
    }).catch(function() {
        showToast("复制失败，请手动复制。", "error");
    });
}

async function copyCodexContext() {
    closeDropdowns();
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在生成 Codex 文献包...", "info");
        const data = await fetchJSON(API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/codex-context");
        const value = data && data.markdown ? data.markdown : JSON.stringify(data, null, 2);
        await navigator.clipboard.writeText(value);
        showToast("Codex 文献包已复制。", "success");
    } catch (error) {
        showToast("Codex 文献包生成失败：" + error.message, "error");
    }
}

async function copyCodexItem(itemType, itemId) {
    if (!state.selectedPaperId || !itemType || !itemId) {
        showToast("当前项目无法复制审核提示。", "error");
        return;
    }
    try {
        showToast("正在生成 AI 审核包...", "info");
        const data = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/codex-item/" + encodeURIComponent(itemType) + "/" + encodeURIComponent(itemId)
        );
        const value = data && data.markdown ? data.markdown : JSON.stringify(data, null, 2);
        await navigator.clipboard.writeText(value);
        showToast("审核提示已复制，可发给指定 AI 审核。", "success");
    } catch (error) {
        showToast("审核包生成失败：" + error.message, "error");
    }
}

function chartRunTargetSignature(run) {
    const raw = run && run.target_signature;
    if (!raw) return {};
    if (typeof raw === "object") return raw;
    try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
        return {};
    }
}

async function ensureSelectedChartReviewScopes() {
    if (!state.selectedPaperId || !state.selectedPaper) return {};
    const current = state.selectedPaper.chart_review_status;
    if (current && Array.isArray(current.chart_runs)) return current;
    const scopes = await fetchJSON(
        API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/chart-review-scopes"
    );
    if (state.selectedPaper) state.selectedPaper.chart_review_status = scopes || {};
    return scopes || {};
}

function pendingChartRunForFigure(figureId) {
    const chartStatus = state.selectedPaper && state.selectedPaper.chart_review_status;
    const targetId = String(figureId || "").trim();
    if (!targetId || !chartStatus || !Array.isArray(chartStatus.chart_runs)) return null;
    return chartStatus.chart_runs.find(function(run) {
        const stage = String(run && run.stage_status || "");
        const figures = chartRunTargetSignature(run).figures;
        return !["completed", "not_required"].includes(stage) &&
            Array.isArray(figures) && figures.map(String).includes(targetId);
    }) || null;
}

async function copyFigureChartReviewPrompt(figureId) {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    await ensureSelectedChartReviewScopes();
    const run = pendingChartRunForFigure(figureId);
    const runId = String(run && (run.chart_run_id || run.run_id || run.id) || "").trim();
    if (!runId) {
        showToast("当前图片没有待补充的图表审核范围，请刷新详情页后重试。", "error");
        return;
    }
    try {
        const task = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/chart-review-task?run_id=" + encodeURIComponent(runId)
        );
        const figure = Array.isArray(task.figures)
            ? task.figures.find(function(item) { return String(item && item.source_record_id || "") === String(figureId); })
            : null;
        if (!figure || !figure.evidence_id) {
            throw new Error("当前审核范围没有返回图片证据，不能生成图表审核提示。");
        }
        const prompt = [
            "这是一次单图片图表审核闭环，不是图片说明/元数据修订任务。",
            "",
            "目标论文号：" + String(task.paper_code || "-"),
            "paper_id：" + String(task.paper_id || state.selectedPaperId),
            "图片 ID：" + String(figureId),
            "证据 ID：" + String(figure.evidence_id),
            "PDF 页：" + String(figure.page || "-"),
            "审核范围由系统生成；run_id：" + runId,
            "bundle_fingerprint：" + String(task.bundle_fingerprint || "-"),
            "",
            "必须完成以下流程：",
            "1. 调用 get_chart_review_task(paper_id, run_id)、get_codex_item 和 read_paper_page 核对这张图及原始 PDF 页。",
            "2. 不要调用 import_analysis、propose_correction 或仅修改 caption/content_summary/key_elements 来代替审核；这些写入不改变图表审核状态。",
            "3. 用 resolve_chart_review_actions 的公开工具契约构造完整 review_result：只覆盖这张图，引用上述真实 evidence_id 和当前 bundle_fingerprint。图、图注和 PDF 一致时使用 KEEP；有问题时使用对应修正动作；证据不足则 NEEDS_HUMAN。",
            "4. 先执行 validate（只校验）；校验成功后才 resolve/apply；没有未解决项后才调用 finalize_chart_review。",
            "5. 不要把 AI 结论称为 verified；最后仅报告 stage_status、unresolved_count 和 completed_snapshot_fingerprint。",
            "6. 如果无法构造通过校验的 review_result，停止并报告 blocked，不要只改图片说明后宣称审核完成。"
        ].join("\n");
        await navigator.clipboard.writeText(prompt);
        showToast("图表审核提示已复制；请发给本地 AI 完成校验、应用和完成步骤。", "success");
    } catch (error) {
        showToast("生成图表审核提示失败：" + error.message, "error");
    }
}
