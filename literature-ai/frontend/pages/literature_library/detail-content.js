// General paper content, knowledge, translation, and readable-field renderers.
function renderWorkspaceHeader(paper) {
    const counts = paper.counts || {};
    const stablePaperId = paper.paper_id || paper.id || "";
    const displayCode = paper.paper_code || "";
    const titleEl = $("paperTitle");
    const metaEl = $("paperMeta");
    const badgesEl = $("paperHeaderBadges");
    const topicEl = $("writerTopic");
    const pdfBtn = $("pdfEvidenceHeaderBtn");
    if (titleEl) {
        const titleStr = paper.title_zh || paper.title || "未命名文献";
        titleEl.textContent = titleStr;
    }
    if (metaEl) {
        const freshness = state.paperResourceFreshness && state.paperResourceFreshness[String(stablePaperId || "")];
        metaEl.innerHTML = [
            esc(paper.year || "-"),
            esc(paper.journal || "-"),
            esc(paperTypeLabel(paper.paper_type)),
            renderDoiMeta(paper.doi),
            displayCode ? ('文献短号: <code>' + esc(displayCode) + '</code>') : "",
            freshness && freshness.updatedAt ? ('<span class="subtle">' + esc(formatPaperResourceFreshness(freshness)) + '</span>') : ""
        ].filter(Boolean).join(" | ");
    }
    if (pdfBtn) {
        const hasPdf = paperHasPdf(paper);
        pdfBtn.textContent = hasPdf ? "查看 PDF" : "PDF 未上传";
        pdfBtn.disabled = !hasPdf;
        pdfBtn.title = hasPdf
            ? "打开当前文献的 PDF 预览。精确证据跳转请使用下方“PDF 证据定位”卡片。"
            : "当前文献尚未上传 PDF，暂时无法预览或进行基于 PDF 页码的证据跳转。";
    }
    if (badgesEl) {
        badgesEl.innerHTML =
            (displayCode ? '<span class="serial-chip" title="文献短号，仅用于人类沟通；API/MCP 仍使用 paper_id">' + esc(displayCode) + "</span>" : "") +
            paperStatusChip(paper) +
            badge(counts.figures, "\u56fe\u7247\u6570\u91cf") +
            badge(counts.dft_results, "DFT \u6570\u91cf") +
            badge(counts.sections, "\u7ae0\u8282\u6570\u91cf") +
            badge(counts.mechanism_claims, "\u673a\u7406\u58e0\u660e\u6570\u91cf") +
            badge(counts.writing_cards, "\u5199\u4f5c\u5361\u7247\u6570\u91cf");
    }
    if (topicEl) topicEl.value = paper.title_zh || paper.title || "";
}

function renderListBlock(title, items, formatter, titleFormatter) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    return items.map(function(item, index) {
        var h3Title = titleFormatter ? titleFormatter(item, index) : (esc(title) + " " + (items.length > 1 ? (index + 1) : ""));
        return '<details class="section-card"><summary><h3 style="margin:0;">' + h3Title + '</h3></summary><div style="margin-top:10px;">' + formatter(item) + '</div></details>';
    }).join("");
}

function renderJSONCards(title, items, options) {
    return renderReadableCards(title, items, options);
}

function tableReviewChipHtml(item) {
    const status = String(item && item.table_review_status || "").trim().toLowerCase();
    const auditCount = Number(item && (item.object_review_audit_count || 0));
    if (status === "verified") {
        return '<span class="status-chip ok" title="' + escAttr("\u8868\u683c\u5df2\u6838\u9a8c") + '">' + esc("\u5df2\u9a8c\u8bc1") + '</span>';
    }
    if (status === "reviewed_empty_content") {
        return '<span class="status-chip warn" title="' + escAttr("已有审核记录，但表格内容为空；需要补回 markdown_content。") + '">已审核但内容为空</span>';
    }
    if (status === "rejected") {
        return '<span class="status-chip danger" title="' + escAttr("\u8868\u683c\u5df2\u88ab\u62d2\u7edd") + '">' + esc("\u5df2\u4e22\u5f03") + '</span>';
    }
    if (status === "pending_correction") {
        return '<span class="status-chip warn" title="' + escAttr("\u8868\u683c\u5df2\u6709 AI \u4fee\u6b63\uff0c\u5f85\u7cfb\u7edf\u5e94\u7528\u6216\u9700\u8981\u89e3\u51b3") + '">' + esc("AI\u5df2\u4fee\u6b63\u5f85\u5e94\u7528") + '</span>';
    }
    if (status === "review_candidate") {
        return '<span class="status-chip meta" title="' + escAttr("\u6709\u8868\u683c\u5ba1\u6838\u610f\u89c1\uff0c\u5f85\u786e\u8ba4") + '">' + esc("\u5f85\u6838\u9a8c") + '</span>';
    }
    if (auditCount > 0) {
        return '<span class="status-chip meta" title="' + escAttr("\u6709\u8868\u683c\u5ba1\u6838\u610f\u89c1\uff0c\u5f85\u786e\u8ba4") + '">' + esc("\u5f85\u6838\u9a8c") + '</span>';
    }
    return "";
}

function tableSourceChipHtml(item) {
    const sourceType = String(item && item.source_document_type || "").trim().toLowerCase();
    if (sourceType === "supplementary_information" || sourceType === "si") {
        const code = item && item.related_paper_code ? " " + item.related_paper_code : "";
        return '<span class="status-chip ok" title="' + escAttr("该表格来自已绑定支撑文献，审核/写回仍归属主文献。") + '">SI' + esc(code) + '</span>';
    }
    if (sourceType === "main_text") {
        return '<span class="status-chip" title="' + escAttr("该表格来自主文献正文。") + '">主文</span>';
    }
    return "";
}

function compactText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
}

function cleanPdfExtractedText(value) {
    return String(value || "")
        .replace(/\/uniFB00\s*/g, "ff")
        .replace(/\/uniFB01\s*/g, "fi")
        .replace(/\/uniFB02\s*/g, "fl")
        .replace(/\/uniFB03\s*/g, "ffi")
        .replace(/\/uniFB04\s*/g, "ffl")
        .replace(/\u00ee\u0084\u0080/g, "ff")
        .replace(/\u00ee\u0084\u0081/g, "fi")
        .replace(/\u00ee\u0084\u0082/g, "fl")
        .replace(/\u00ee\u0084\u0083/g, "fi")
        .replace(/\u00ee\u0084\u0084/g, "fl")
        .replace(/\uE100/g, "ff")
        .replace(/\uE101/g, "fi")
        .replace(/\uE102/g, "fl")
        .replace(/\uE103/g, "fi")
        .replace(/\uE104/g, "fl")
        .replace(/\uE000|\uE001|\uE002|\uE003|\uE004|\uE005/g, "")
        .replace(/\s+([,.;:])/g, "$1")
        .replace(/\s+/g, " ")
        .trim();
}

function isDisplayBodySection(item) {
    if (!item) return false;
    const sectionType = String(item.section_type || "").trim().toLowerCase();
    if (["table", "figure", "figure_caption", "caption", "reference", "references", "deprecated_stale"].includes(sectionType)) return false;
    const title = cleanPdfExtractedText(item.section_title || "");
    const text = cleanPdfExtractedText(item.text || "");
    const titleLower = title.toLowerCase();
    const textLower = text.toLowerCase().slice(0, 500);
    if (/^page\s+\d+\b/.test(titleLower)) return false;
    if (titleLower.startsWith("[deprecated]") || titleLower.includes("replaced by")) return false;
    if (/^(fig(?:ure)?\.?|scheme|table)\s*\d+/.test(titleLower)) return false;
    if (["system", "row", "entry"].includes(titleLower)) {
        if (/(donor nbo|acceptor nbo|homo|lumo|e homo|e lumo|gibbs free energy|enthalpy|entropy|row:)/.test(textLower)) return false;
    }
    if ((textLower.match(/\s\|\s/g) || []).length >= 3) return false;
    return Boolean(text);
}

function sectionDisplaySortKey(item) {
    const sectionType = String(item && item.section_type || "").trim().toLowerCase();
    const title = cleanPdfExtractedText(item && item.section_title || "").toLowerCase();
    let typeRank = {
        abstract: 0,
        introduction: 1,
        methods: 2,
        method: 2,
        experimental: 2,
        computational: 2,
        results: 3,
        discussion: 3,
        "results and discussion": 3,
        body: 4,
        conclusion: 9,
        conclusions: 9
    }[sectionType];
    if (typeof typeRank !== "number") typeRank = 5;
    if (title.includes("introduction")) typeRank = Math.min(typeRank, 1);
    else if (title.includes("method") || title.includes("computational") || title.includes("calculation")) typeRank = Math.min(typeRank, 2);
    else if (title.includes("result") || title.includes("discussion")) typeRank = Math.min(typeRank, 3);
    else if (title.includes("conclusion")) typeRank = 9;
    const pageRank = Number(item && item.page_start || 9999);
    return [typeRank, pageRank, title];
}

function compareDisplaySections(a, b) {
    const left = sectionDisplaySortKey(a);
    const right = sectionDisplaySortKey(b);
    for (let i = 0; i < left.length; i += 1) {
        if (left[i] < right[i]) return -1;
        if (left[i] > right[i]) return 1;
    }
    return 0;
}

function clipText(value, maxChars) {
    const text = compactText(value);
    if (!text) return "";
    return text.length <= maxChars ? text : text.slice(0, Math.max(0, maxChars - 1)).trim() + "…";
}

function bestSentencePreview(value, maxChars) {
    const text = compactText(value);
    if (!text) return "";
    const parts = text.split(/(?<=[。！？.!?;；])s+/).filter(Boolean);
    const preview = parts.slice(0, 2).join(" ");
    return clipText(preview || text, maxChars);
}

function prettifyToken(value) {
    return compactText(value).replace(/_/g, " ");
}

function knowledgeCategoryMeta(category) {
    const mapping = {
        mechanism: { label: "机理解释", group: "最适合写讨论", use: "讨论部分的机理解释", order: 1 },
        mechanism_context: { label: "机理线索", group: "最适合写讨论", use: "讨论部分的背景或机理补充", order: 1 },
        research_gap: { label: "研究空白", group: "最适合写引言", use: "引言中的问题铺垫", order: 0 },
        research_context: { label: "研究背景", group: "最适合写引言", use: "引言中的背景铺垫", order: 0 },
        proposed_solution: { label: "拟解决方案", group: "最适合写引言", use: "引言或摘要中的方案概括", order: 0 },
        writing_logic: { label: "写作逻辑", group: "写作辅助", use: "组织摘要、引言或讨论的写作顺序", order: 3 },
        computational_method: { label: "计算方法", group: "最适合写方法", use: "方法部分的计算设置说明", order: 2 },
        synthesis_method: { label: "制备方法", group: "最适合写方法", use: "方法部分的实验或制备说明", order: 2 },
        conclusion: { label: "结论启发", group: "最适合写结论", use: "结论或讨论收束", order: 4 },
        abstract: { label: "摘要候选", group: "快速总览", use: "快速理解整篇文章", order: 5 },
        external_analysis: { label: "外部解析", group: "待核对补充", use: "作为补充线索，不可直接引用", order: 6 },
        correction_candidate: { label: "修正建议", group: "待核对补充", use: "校对已有解析结果", order: 6 },
        citation_relationship: { label: "引用关系", group: "待核对补充", use: "梳理文献关联", order: 6 },
        curation_note: { label: "人工笔记", group: "待核对补充", use: "作为整理备注参考", order: 6 }
    };
    return mapping[category] || { label: prettifyToken(category || "unknown"), group: "其他候选", use: "待人工判断用途", order: 7 };
}

function knowledgeSourceMeta(sourceType) {
    const mapping = {
        mechanism_claim: { label: "结构化机理提取", tip: "系统从已解析的机理字段中抽出的候选。" },
        writing_card: { label: "写作卡片", tip: "系统生成的写作草稿或写作逻辑。" },
        paper_section: { label: "正文节选", tip: "直接来自正文解析的章节片段。" },
        paper_abstract: { label: "摘要原文", tip: "直接来自论文摘要。" },
        external_analysis_candidate: { label: "IDE AI 回写", tip: "来自 IDE AI 回写，通常优先走 MCP；若当时会话未暴露 MCP 工具，也可能来自仓库内 `app.mcp.*` 后备路径，仍必须再核对。" },
        paper_note: { label: "人工笔记", tip: "来自人工或 Codex 整理的笔记。" }
    };
    return mapping[sourceType] || { label: prettifyToken(sourceType || "unknown"), tip: "系统内部来源类型。" };
}

function knowledgeConfidenceMeta(confidence) {
    if (typeof confidence !== "number" || !isFinite(confidence)) {
        return { label: "待判断", className: "unknown", tip: "当前没有明确的可信度分数，需要结合证据判断。" };
    }
    if (confidence >= 0.82) {
        return { label: "较高可信", className: "high", tip: "候选与原始结构化结果较一致，但仍应看证据。" };
    }
    if (confidence >= 0.62) {
        return { label: "中等可信", className: "medium", tip: "可以作为线索使用，写入前应先核对证据。" };
    }
    return { label: "较低可信", className: "low", tip: "更像线索草稿，不宜直接用于写作。" };
}

function knowledgeEvidenceStateLabel(state) {
    const mapping = {
        structured_extraction_candidate: "结构化候选",
        parsed_source_text: "解析原文",
    external_ai_import_unverified: "IDE 回写待核对",
        note_with_optional_quote: "笔记候选",
        writing_candidate: "写作草稿",
        text_only_candidate: "文本候选"
    };
    return mapping[state] || prettifyToken(state || "unknown");
}

function knowledgeLocationText(item) {
    const pages = [];
    if (item.page_start && item.page_end && item.page_start !== item.page_end) pages.push("第 " + item.page_start + "-" + item.page_end + " 页");
    else if (item.page_start) pages.push("第 " + item.page_start + " 页");
    if (item.section_title) pages.push(item.section_title);
    return pages.join(" · ");
}

function knowledgeDisplayTitle(item, meta) {
    const raw = compactText(item && item.title);
    if (!raw) return meta.label;
    const titleMapping = {
        "Research gap": "研究空白",
        "Proposed solution": "拟解决方案",
        "Core hypothesis": "核心假设",
        "Abstract logic": "摘要写法",
        "Introduction logic": "引言写法",
        "Discussion logic": "讨论写法",
        "Mechanism claim": "机理说明",
        "Abstract candidate": "摘要候选"
    };
    return titleMapping[raw] || prettifyToken(raw);
}

function knowledgeSummaryText(item, meta) {
    const content = compactText(item && item.content);
    if (!content) return "当前没有可直接阅读的候选摘要。";
    const preview = bestSentencePreview(content, 150);
    return preview || clipText(content, 150) || meta.use;
}

function knowledgeRawDetails(item) {
    const blocks = [];
    const evidence = compactText(item && item.evidence_text);
    const content = compactText(item && item.content);
    const location = knowledgeLocationText(item);
    const source = knowledgeSourceMeta(item && item.source_type);
    const stateLabel = knowledgeEvidenceStateLabel(item && item.evidence_state);
    if (location || source.label || stateLabel) {
        blocks.push(
            '<div class="knowledge-support-grid">' +
                (location ? '<div class="key-value"><div class="k">定位</div><div class="v">' + esc(location) + '</div></div>' : "") +
                '<div class="key-value"><div class="k">来源</div><div class="v" title="' + esc(source.tip) + '">' + esc(source.label) + '</div></div>' +
                '<div class="key-value"><div class="k">证据状态</div><div class="v">' + esc(stateLabel) + '</div></div>' +
            '</div>'
        );
    }
    if (evidence) {
        blocks.push('<div class="knowledge-detail-block"><div class="knowledge-detail-title">证据原文</div><div class="knowledge-detail-text">' + esc(evidence) + '</div></div>');
    }
    if (content && content !== evidence) {
        blocks.push('<div class="knowledge-detail-block"><div class="knowledge-detail-title">候选原文</div><div class="knowledge-detail-text">' + esc(content) + '</div></div>');
    }
    return blocks.join("");
}

function knowledgeJumpAction(item) {
    const page = Number(item && item.page_start || 0);
    if (!page || !(item && item.paper_id)) return "";
    const evidencePreview = clipText(item.evidence_text || item.content || "", 160);
    return buildPdfJumpButtonHtml({
        paperId: item.paper_id,
        page: page,
        evidenceText: evidencePreview,
        label: "\u67e5\u770b\u539f\u9875"
    });
}
function renderKnowledgeCandidateCard(item, index) {
    const meta = knowledgeCategoryMeta(item && item.category);
    const source = knowledgeSourceMeta(item && item.source_type);
    const confidence = knowledgeConfidenceMeta(item && item.confidence);
    const title = knowledgeDisplayTitle(item, meta);
    const summary = knowledgeSummaryText(item, meta);
    const details = knowledgeRawDetails(item);
    const jumpButton = knowledgeJumpAction(item);
    const locatorHint = knowledgeLocationText(item);
    return '<article class="knowledge-card">' +
        '<div class="knowledge-card-head">' +
            '<div>' +
                '<div class="knowledge-card-title">' + esc(title || ("候选 " + (index + 1))) + '</div>' +
                '<div class="knowledge-card-use">可用于：' + esc(meta.use) + '</div>' +
            '</div>' +
            '<div class="knowledge-card-actions">' + jumpButton + '</div>' +
        '</div>' +
        '<div class="knowledge-tag-row">' +
            '<span class="status-chip meta" title="候选类型">' + esc(meta.label) + '</span>' +
            '<span class="status-chip confidence-' + esc(confidence.className) + '" title="' + esc(confidence.tip + (typeof item.confidence === "number" ? (" 分数: " + item.confidence.toFixed(2)) : "")) + '">' + esc(confidence.label) + '</span>' +
            '<span class="status-chip" title="' + esc(source.tip) + '">' + esc(source.label) + '</span>' +
            (locatorHint ? '<span class="status-chip">' + esc(locatorHint) + '</span>' : '') +
        '</div>' +
        '<div class="knowledge-summary">' + esc(summary) + '</div>' +
        '<details class="knowledge-details">' +
            '<summary>展开证据与来源</summary>' +
            details +
        '</details>' +
    '</article>';
}

function renderKnowledgeGroup(groupName, items) {
    return '<details class="section-card knowledge-group-section">' +
        '<summary class="knowledge-group-head" style="justify-content:flex-start; gap:10px;">' +
            '<h3 style="margin:0;">' + esc(groupName) + '</h3>' +
            '<span class="status-chip">' + items.length + ' 条</span>' +
        '</summary>' +
        '<div class="knowledge-group-list" style="margin-top:16px;">' + items.map(renderKnowledgeCandidateCard).join("") + '</div>' +
    '</details>';
}

function renderKnowledgeContext(detail) {
    const knowledge = detail.knowledge_context || {};
    const candidates = knowledge.candidates || [];
    if (!candidates.length) {
    return '<div class="section-card"><h3>知识候选</h3><div class="muted">暂无知识候选。可先用 IDE AI 优先通过 MCP 回写；如果当前会话没暴露 MCP 工具，也可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径，或先刷新 IDE AI 材料。</div></div>';
    }
    const meta = knowledge.metadata || {};
    const counts = meta.category_counts || {};
    const grouped = {};
    candidates.forEach(function(item) {
        const info = knowledgeCategoryMeta(item.category);
        const groupName = info.group;
        if (!grouped[groupName]) grouped[groupName] = { order: info.order, items: [] };
        grouped[groupName].items.push(item);
    });
    const summary = Object.keys(counts).map(function(key) {
        const info = knowledgeCategoryMeta(key);
        return '<div class="knowledge-summary-pill"><strong>' + esc(info.label) + '</strong><span>' + esc(counts[key]) + ' 条</span></div>';
    }).join("");
    const groupHtml = Object.keys(grouped)
        .sort(function(a, b) { return grouped[a].order - grouped[b].order; })
        .map(function(groupName) {
            return renderKnowledgeGroup(groupName, grouped[groupName].items);
        }).join("");
    return '<div class="section-card">' +
        '<h3>Codex 知识候选</h3>' +
        '<div class="subtle">这里展示的是可供你写作和核对的候选线索，不是最终事实。默认先看短摘要，只有需要时再展开证据原文。</div>' +
        (summary ? '<div class="knowledge-summary-grid">' + summary + '</div>' : '') +
        '</div>' +
        groupHtml;
}

function renderLocalizedSummary(detail) {
    const titleZh = detail.title_zh || (detail.comprehensive_analysis && detail.comprehensive_analysis.title_zh) || "";
    const abstractZh = detail.abstract_zh || (detail.comprehensive_analysis && detail.comprehensive_analysis.abstract_zh) || "";
    if (!titleZh && !abstractZh) return "";
    return '<details class="section-card localized-summary-card">' +
        '<summary><h3>' + esc("\u4e2d\u6587\u9898\u76ee\u4e0e\u6458\u8981") + '</h3></summary>' +
        (titleZh ? '<div class="localized-title">' + esc(titleZh) + '</div>' : '') +
        (detail.title ? '<div class="subtle original-title">' + esc("\u82f1\u6587\u9898\u76ee\uff1a") + esc(detail.title) + '</div>' : '') +
        (abstractZh ? '<h4>' + esc("\u4e2d\u6587\u6458\u8981") + '</h4><div class="prewrap">' + esc(abstractZh) + '</div>' : '') +
        (detail.abstract ? '<details class="original-abstract"><summary>' + esc("\u67e5\u770b\u82f1\u6587\u6458\u8981") + '</summary><div class="prewrap">' + esc(detail.abstract) + '</div></details>' : '') +
        '</details>';
}

function translationBlockTitle(rawTitle, index) {
    const title = (rawTitle || "").trim();
    if (!title) return "\u8bd1\u6587\u7247\u6bb5 " + (index + 1);
    if (/abstract/i.test(title) || title === "\u6458\u8981") return "\u6458\u8981";
    const figureMatch = title.match(/^(?:Fig\.?|Figure)\s*([0-9]+[a-z]?(?:\([a-z]\))?)/i);
    if (figureMatch) return "\u56fe " + figureMatch[1];
    const sectionMatch = title.match(/^Section\s+(.+)$/i);
    if (sectionMatch) return "\u7ae0\u8282 " + sectionMatch[1];
    return title;
}

function extractTranslationBlocks(raw) {
    const withoutHeader = String(raw || "")
        .replace(/^Translation preview generated by API\.[^\n]*\n?/i, "")
        .replace(/^Title:[^\n]*\n?/im, "")
        .trim();
    const blocks = [];
    const pattern = /(?:^|\n)##\s+([^\n]+)\n([\s\S]*?)(?=\n##\s+|$)/g;
    let match;
    while ((match = pattern.exec(withoutHeader)) !== null) {
        const body = (match[2] || "").trim();
        const marker = body.lastIndexOf("TRANSLATION:");
        if (marker < 0) continue;
        let text = body.slice(marker + "TRANSLATION:".length).trim();
        text = text.replace(/\n+SOURCE:\n[\s\S]*$/i, "").trim();
        if (text) blocks.push({ title: translationBlockTitle(match[1], blocks.length), text });
    }
    if (blocks.length) return blocks;
    const marker = withoutHeader.lastIndexOf("TRANSLATION:");
    const fallback = marker >= 0 ? withoutHeader.slice(marker + "TRANSLATION:".length).trim() : withoutHeader;
    const text = fallback.replace(/\n+SOURCE:\n[\s\S]*$/i, "").trim();
    return text ? [{ title: "\u5168\u6587\u8bd1\u6587", text }] : [];
}

function renderFullTranslation(detail) {
    const translation = detail.full_translation_zh || "";
    if (!translation) {
        return '<div class="section-card"><h3>' + esc("\u4e2d\u6587\u8bd1\u6587") + '</h3><div class="muted">' + esc("\u6682\u65e0\u5df2\u4fdd\u5b58\u7684\u5168\u6587\u8bd1\u6587\u3002\u8bf7\u5148\u8fd0\u884c\u7ffb\u8bd1\u5e76\u4fdd\u5b58\u5230\u5e93\u3002") + '</div></div>';
    }
    const blocks = extractTranslationBlocks(translation);
    const body = blocks.map(function(block) {
        return '<section class="translation-section">' +
            '<h4>' + esc(block.title) + '</h4>' +
            '<div class="prewrap translation-body">' + esc(block.text) + '</div>' +
            '</section>';
    }).join("");
    return '<div class="section-card full-translation-card">' +
        '<h3>' + esc("\u5168\u6587\u4e2d\u6587\u8bd1\u6587") + '</h3>' +
        '<div class="subtle">' + esc("\u5df2\u8bfb\u53d6\u6570\u636e\u5e93\u4e2d\u4fdd\u5b58\u7684\u4e2d\u6587\u8bd1\u6587\u3002") + '</div>' +
        (body || '<div class="muted">' + esc("\u8bd1\u6587\u5185\u5bb9\u4e3a\u7a7a\u3002") + '</div>') +
        '</div>';
}

const DETAIL_FIELD_LABELS = {
    software: "计算软件",
    functional: "泛函",
    dispersion_correction: "色散校正",
    pseudopotential: "赝势",
    cutoff_energy_ev: "截断能",
    cutoff_energy: "截断能",
    k_points: "K 点",
    convergence_settings: "收敛设置",
    vacuum_thickness_a: "真空层厚度",
    vacuum_thickness: "真空层厚度",
    catalyst: "催化剂",
    catalyst_sample_id: "关联催化剂样本 ID",
    candidate_status: "候选状态",
    adsorbate: "吸附物",
    energy_type: "能量类型",
    property_type: "能量类型",
    value: "数值",
    unit: "单位",
    reaction_step: "反应步骤",
    source_section: "来源章节",
    source_figure: "来源图/表",
    evidence_text: "证据原文",
    confidence: "置信度",
    name: "名称",
    catalyst_type: "催化剂类型",
    metal_centers: "金属中心",
    coordination: "配位结构",
    support: "载体",
    synthesis_method: "合成方法",
    sulfur_loading: "硫载量",
    sulfur_content: "硫含量",
    electrolyte_sulfur_ratio: "电解液/硫比",
    capacity: "容量",
    cycle_number: "循环圈数",
    rate: "倍率",
    decay_per_cycle: "容量衰减",
    claim_type: "机理类型",
    claim_text: "机理描述",
    key_species: "关键物种",
    mechanism_direction: "作用方向",
    paper_type: "论文类型",
    research_gap: "研究空白",
    proposed_solution: "解决方案",
    core_hypothesis: "核心假设",
    title: "标题",
    doi: "DOI",
    authors: "作者",
    year: "年份",
    journal: "期刊",
    one_sentence_takeaway: "一句话结论",
    real_world_impact: "实际意义",
    conclusion_mapping: "结论对应",
    source_document_type: "来源文档",
    related_paper_code: "关联短号",
    related_paper_title: "关联文献",
    writeback_paper_id: "写回主文献"
};

function readableFieldLabel(key) {
    return DETAIL_FIELD_LABELS[key] || String(key || "").replace(/_/g, " ");
}

function unwrapEvidenceValue(value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
        if ("value" in value || "unit" in value || "evidence_text" in value) {
            const parts = [];
            if (value.value !== null && value.value !== undefined && value.value !== "") parts.push(value.value);
            if (value.unit) parts.push(value.unit);
            if (!parts.length && value.evidence_text) parts.push(value.evidence_text);
            return parts.join(" ");
        }
    }
    return value;
}

function readableValue(value) {
    value = unwrapEvidenceValue(value);
    if (value === null || value === undefined || value === "") return "-";
    if (Array.isArray(value)) {
        if (!value.length) return "-";
        return value.map(function(item) {
            if (item && typeof item === "object") return readableValue(item.value || item.text || item.name || item.title || item.caption || item.reason || "");
            return String(item);
        }).join("；");
    }
    if (typeof value === "number") return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)));
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") return "";
    return String(value);
}

function renderReadableFields(item, keys) {
    const fields = [];
    keys.forEach(function(key) {
        const rawValue = item ? item[key] : null;
        const value = readableValue(rawValue);
        if (value && value !== "-") {
            if (key === "markdown_content" && typeof rawValue === "string") {
                fields.push('<div class="readable-field field-' + esc(key) + '"><div class="k">' + esc(readableFieldLabel(key)) + '</div><div class="v">' + renderPipeTable(rawValue) + '</div></div>');
            } else {
                fields.push('<div class="readable-field field-' + esc(key) + '"><div class="k">' + esc(readableFieldLabel(key)) + '</div><div class="v">' + esc(value) + '</div></div>');
            }
        }
    });
    return fields.length ? '<div class="readable-grid">' + fields.join("") + '</div>' : '<div class="muted">暂无可读字段。</div>';
}
