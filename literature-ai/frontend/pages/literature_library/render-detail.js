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
        metaEl.innerHTML = [
            esc(paper.year || "-"),
            esc(paper.journal || "-"),
            esc(paperTypeLabel(paper.paper_type)),
            renderDoiMeta(paper.doi),
            displayCode ? ('文献短号: <code>' + esc(displayCode) + '</code>') : ""
        ].join(" | ");
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

function renderJSONCards(title, items) {
    return renderReadableCards(title, items);
}

function tableReviewChipHtml(item) {
    const status = String(item && item.table_review_status || "").trim().toLowerCase();
    const auditCount = Number(item && (item.object_review_audit_count || 0));
    if (status === "verified") {
        return '<span class="status-chip ok" title="' + escAttr("\u8868\u683c\u5df2\u6838\u9a8c") + '">' + esc("\u5df2\u9a8c\u8bc1") + '</span>';
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
    return '<div class="section-card localized-summary-card">' +
        '<h3>中文题目与摘要</h3>' +
        (titleZh ? '<div class="localized-title">' + esc(titleZh) + '</div>' : '') +
        (detail.title ? '<div class="subtle original-title">英文题目：' + esc(detail.title) + '</div>' : '') +
        (abstractZh ? '<h4>中文摘要</h4><div class="prewrap">' + esc(abstractZh) + '</div>' : '') +
        (detail.abstract ? '<details class="original-abstract"><summary>查看英文摘要</summary><div class="prewrap">' + esc(detail.abstract) + '</div></details>' : '') +
        '</div>';
}

function renderFullTranslation(detail) {
    const translation = detail.full_translation_zh || "";
    if (!translation) {
        return '<div class="section-card"><h3>中文译文</h3><div class="muted">暂无已保存的全文译文。请先运行翻译并保存到库。</div></div>';
    }
    const cleaned = translation
        .replace(/^Translation preview generated by API\\.[^\\n]*\\n?/i, "")
        .replace(/^Title:.*\\n?/im, "")
        .trim();
    return '<div class="section-card full-translation-card">' +
        '<h3>全文中文译文</h3>' +
        '<div class="subtle">已从数据库中的 translation_preview 记录读取，不是临时预览。</div>' +
        '<div class="prewrap translation-body">' + esc(cleaned || translation) + '</div>' +
        '</div>';
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
    adsorbate: "吸附物",
    energy_type: "能量类型",
    property_type: "能量类型",
    value: "数值",
    unit: "单位",
    reaction_step: "反应步骤",
    source_section: "来源章节",
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
    conclusion_mapping: "结论对应"
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

const CODEX_ITEM_TYPE_BY_CARD_TITLE = {
    "DFT 设置": "dft_setting",
    "催化剂样本": "catalyst_sample",
    "DFT 结果": "dft_result",
    "候选 DFT 数据": "dft_result",
    "DFT 候选结果": "dft_result",
    "电化学性能": "electrochemical_performance",
    "机理声明": "mechanism_claim",
    "写作卡片": "writing_card",
    "表格": "table"
};

const DFT_BLOCK_REASON_LABELS = {
    missing_review: "缺少人工核验",
    unsafe_review: "核验状态不安全",
    missing_evidence: "缺少证据引用",
    missing_evidence_text: "缺少证据原文",
    unsafe_locator: "缺少准确 PDF 定位"
};
DFT_BLOCK_REASON_LABELS.missing_material_identity = "缺少材料/结构绑定";

DFT_BLOCK_REASON_LABELS.missing_review = "尚未完成审核";
DFT_BLOCK_REASON_LABELS.unsafe_review = "审核状态不安全";
DFT_BLOCK_REASON_LABELS.missing_evidence = "缺证据引用/定位";
DFT_BLOCK_REASON_LABELS.missing_evidence_text = "缺证据原文";
DFT_BLOCK_REASON_LABELS.unsafe_locator = "PDF 定位不可靠";
DFT_BLOCK_REASON_LABELS.missing_material_identity = "缺材料/结构绑定";

function codexItemActionHtml(itemType, item) {
    if (!itemType || !item || !item.id) return "";
    return '<button class="btn ghost small" type="button" title="复制此项、证据定位、邻近正文和 AI 审核协议" onclick="event.stopPropagation(); copyCodexItem(\'' +
        escAttr(itemType) + '\', \'' + escAttr(item.id) + '\')">复制审核提示</button>';
}

function figureReviewSummaryHtml(item) {
    const imageReview = item.image_review || {};
    const cropStatus = item.crop_status || imageReview.crop_status || "unknown";
    const flags = Array.isArray(item.flags) && item.flags.length ? item.flags : (Array.isArray(imageReview.flags) ? imageReview.flags : []);
    const reliabilityStatus = item.figure_reliability_status || (imageReview.review_required ? "needs_review" : "reliable");
    const reliabilityWarnings = Array.isArray(item.figure_reliability_warnings) && item.figure_reliability_warnings.length
        ? item.figure_reliability_warnings
        : figureIssuesFromFlags(flags);
    const reviewRequired = item.review_required === true || imageReview.review_required === true;
    const auditCount = Number(item.object_review_audit_count || (item.object_review_audits && item.object_review_audits.length) || 0);
    const conflictCount = Number(item.conflict_count || (item.field_conflicts && item.field_conflicts.length) || 0);
    const latest = item.latest_object_review_audit || ((item.object_review_audits || [])[0]) || null;
    const latestHtml = latest
        ? '<div class="figure-review-latest"><strong>Latest audit:</strong> ' +
            esc(latest.source_label || latest.source || "unknown") +
            ' | decision=' + esc(latest.decision || "-") +
            ' | confidence=' + esc(latest.confidence == null ? "-" : latest.confidence) +
            ' | verification=' + esc(latest.verification_status || "unverified") +
            '</div>'
        : '<div class="subtle">Latest audit: none</div>';
    const conflictHtml = conflictCount
        ? '<div class="subtle">Conflict fields: ' + esc((item.field_conflicts || []).map(function(row) { return row.field_name || "-"; }).join(", ")) + '</div>'
        : "";
    const issueChips = reliabilityWarnings.length
        ? reliabilityWarnings.map(function(code) {
            return '<span class="status-chip danger" title="' + esc(code) + '">' + esc(figureIssueLabel(code)) + '</span>';
        }).join("")
        : '<span class="status-chip ok">no figure warnings</span>';
    const sizeBits = [
        imageReview.pixel_size ? "pixel " + imageReview.pixel_size.width + "x" + imageReview.pixel_size.height : null,
        imageReview.bbox_size_points ? "bbox " + imageReview.bbox_size_points.width + "x" + imageReview.bbox_size_points.height : null,
        imageReview.full_page_image_path ? "full-page snapshot present" : "missing full-page snapshot"
    ].filter(Boolean).join(" | ");
    const auditChecklist = '<div class="subtle">Figure audit checklist: confirm the paper&apos;s total figure/subfigure coverage matches the PDF with no missing figures, check whether the crop is too large or too small, whether the crop matches the correct figure/subfigure, whether axes/legends/labels/panels are cut off, and whether the summary explains the visual content instead of repeating the caption.</div>';
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<span class="status-chip">Page ' + esc(item.page || "-") + '</span>' +
            '<span class="status-chip">Crop status: ' + esc(figureCropStatusLabel(cropStatus)) + '</span>' +
            '<span class="status-chip ' + (reliabilityWarnings.length ? 'danger' : 'ok') + '">Figure reliability: ' + esc(figureReliabilityLabel(reliabilityStatus)) + '</span>' +
            '<span class="status-chip ' + (reviewRequired ? 'danger' : 'ok') + '">Image review: ' + (reviewRequired ? 'required' : 'not required') + '</span>' +
            '<span class="status-chip">Object audits ' + auditCount + '</span>' +
            '<span class="status-chip ' + (conflictCount ? 'danger' : '') + '">Conflicts ' + conflictCount + '</span>' +
        '</div>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;">' + issueChips + '</div>' +
        (flags.length ? '<div class="subtle">Flags: ' + esc(flags.join(", ")) + '</div>' : '<div class="subtle">Flags: 0</div>') +
        (sizeBits ? '<div class="subtle">Figure artifact detail: ' + esc(sizeBits) + '</div>' : '') +
        auditChecklist +
        latestHtml +
        conflictHtml +
    '</div>';
}

function figureCropStatusLabel(status) {
    const mapping = {
        unknown: "未分类/待核对",
        candidate_crop: "候选截图",
        needs_review: "待核对",
        verified: "已核对",
        missing: "缺失"
    };
    return mapping[status] || status || "未分类/待核对";
}

function figureReliabilityLabel(status) {
    const mapping = {
        reliable: "reliable candidate",
        candidate_reliable: "reliable candidate",
        needs_review: "needs review",
        unknown: "未分类/待核对"
    };
    return mapping[status] || status || "未分类/待核对";
}

function figureIssueLabel(code) {
    const mapping = {
        missing_full_page_snapshot: "missing full-page snapshot",
        small_crop: "small crop",
        missing_bbox: "missing bbox",
        extreme_aspect_ratio: "extreme aspect ratio",
        caption_only: "caption only",
        missing_image: "missing image",
        missing_page: "missing page"
    };
    return mapping[code] || code;
}

function figureIssuesFromFlags(flags) {
    const mapping = {
        missing_full_page_snapshot: "missing_full_page_snapshot",
        small_crop_or_subfigure: "small_crop",
        missing_parser_bbox: "missing_bbox",
        extreme_aspect_ratio: "extreme_aspect_ratio",
        caption_only: "caption_only",
        missing_image_path: "missing_image",
        missing_image_file: "missing_image",
        missing_pdf_page: "missing_page"
    };
    const issues = [];
    (Array.isArray(flags) ? flags : []).forEach(function(flag) {
        const issue = mapping[flag] || null;
        if (issue && !issues.includes(issue)) issues.push(issue);
    });
    return issues;
}

function dftMissingReviewLabel(item) {
    if (!item) return DFT_BLOCK_REASON_LABELS.missing_review;
    const audits = (Array.isArray(item.object_review_audits) ? item.object_review_audits : [])
        .filter(dftOpinionHasAnchor);
    const sources = new Set(audits.map(dftOpinionSource));
    if (sources.size === 0) return "尚无 AI 对象审核";
    if (sources.size === 1) return "仅有一个 AI 意见，等待第二 AI";
    const classified = classifyDftAutomationRows([item]);
    if (classified.consensus.length) return "双 AI 一致，待系统写回";
    if (classified.conflicts.length) return "多 AI 意见有冲突，待裁决";
    return "多 AI 审核尚未形成可写回结论";
}

function dftBlockedReasonText(reasons, item) {
    return (Array.isArray(reasons) ? reasons : []).map(function(reason) {
        if (reason === "missing_review") return dftMissingReviewLabel(item);
        return DFT_BLOCK_REASON_LABELS[reason] || reason;
    }).join("、");
}

function dftEvidencePayload(item) {
    return item && item.evidence_payload && typeof item.evidence_payload === "object" ? item.evidence_payload : {};
}

function dftSourceLabel(sourceType) {
    const value = String(sourceType || "unknown");
    const labels = {
        main_text: "正文",
        supplementary_information: "SI",
        supporting_reference: "支撑文献",
        unknown: "未知"
    };
    return labels[value] || value;
}

function dftEvidenceSourceMeta(item) {
    const payload = dftEvidencePayload(item);
    const location = payload.evidence_location && typeof payload.evidence_location === "object" ? payload.evidence_location : {};
    const sourceType = payload.source_document_type || location.source_document_type || "unknown";
    const locator = payload.source_locator || location.source_locator || location.locator || item.source_section || item.source_figure || "";
    const page = payload.page || location.page || "";
    const table = payload.table || location.table || "";
    const supporting = Array.isArray(payload.supporting_evidence) ? payload.supporting_evidence : [];
    return {
        sourceType: sourceType,
        sourceLabel: dftSourceLabel(sourceType),
        locator: locator || table,
        page: page,
        supportingCount: supporting.length,
        borrowed: sourceType === "supporting_reference" || payload.borrowed_from_reference === true
    };
}

function renderDftEvidenceSource(item) {
    const meta = dftEvidenceSourceMeta(item || {});
    const locatorText = [meta.locator, meta.page ? "p." + meta.page : ""].filter(Boolean).join(", ");
    return '<div class="knowledge-detail-block"><div class="knowledge-detail-title">证据来源</div>' +
        '<div class="knowledge-detail-text">' +
            '来源：' + esc(meta.sourceLabel) +
            (locatorText ? '；定位：' + esc(locatorText) : '') +
            '；重复证据：' + esc(meta.supportingCount) + ' 处' +
        '</div></div>' +
        (meta.borrowed
            ? '<div class="figure-warning" style="margin-top:10px;"><strong>支撑文献数据</strong><div>不计入当前主文献导出，需单独入库/核验原文。</div></div>'
            : '');
}

function renderDftItemSafety(item) {
    const safety = item && item.export_safety;
    if (!safety) {
        return '<div class="figure-warning" style="margin-top:12px;">' +
            '<strong>安全状态待加载</strong>' +
            '<div>这条 DFT 记录暂未拿到导出安全门详情；仍可人工拒绝，接受入库时后端会重新校验。</div>' +
            renderDftDecisionActions(item, false) +
        '</div>';
    }
    const exportable = safety.is_exportable === true || safety.eligible === true;
    const reasons = dftBlockedReasonText(safety.blocked_reasons, item);
    return '<div class="figure-warning" style="margin-top:12px;">' +
        '<strong>' + (exportable ? "已审核可导出" : "候选不可进入正式数据库") + '</strong>' +
        '<div>' + (exportable
            ? "该条记录已满足人工核验、证据原文和准确 PDF 定位要求。"
            : "阻断原因：" + (reasons || "待按 AI 协议和 PDF 证据检查")) + '</div>' +
        renderDftDecisionActions(item, exportable) +
    '</div>';
}

function isNegativeDftDecision(decision) {
    const value = String(decision || "").trim().toUpperCase();
    return ["REJECT", "REJECTED", "BLOCK", "DENY", "DROP"].includes(value);
}

function sortDftAuditsNewestFirst(a, b) {
    return String(b && b.created_at || "").localeCompare(String(a && a.created_at || ""));
}

function importedDftAcceptanceOpinions(item) {
    const audits = item && Array.isArray(item.object_review_audits) ? item.object_review_audits.slice() : [];
    const hasWholeRowProposed = audits.some(function(audit) {
        return audit &&
            String(audit.decision || "").trim().toUpperCase() === "PROPOSED" &&
            String(audit.field_name || "").trim() === "dft_results";
    });
    const seen = {};
    return audits
        .filter(function(audit) {
            const decision = dftOpinionDecision(audit);
            if (!audit || isNegativeDftDecision(audit.decision) || !decision) return false;
            if (!["PASS", "PROPOSED"].includes(decision)) return false;
            if (
                hasWholeRowProposed &&
                decision === "PASS" &&
                String(audit.field_name || "").trim() === "value"
            ) {
                return false;
            }
            return true;
        })
        .sort(function(a, b) {
            const aWholeRow = String(a.field_name || "") === "dft_results" ? 0 : 1;
            const bWholeRow = String(b.field_name || "") === "dft_results" ? 0 : 1;
            if (aWholeRow !== bWholeRow) return aWholeRow - bWholeRow;
            return sortDftAuditsNewestFirst(a, b);
        })
        .filter(function(audit) {
            const key = [
                audit.field_name || "",
                audit.decision || "",
                JSON.stringify(audit.corrected_value == null ? "" : audit.corrected_value),
                audit.reason || ""
            ].join("|");
            if (seen[key]) return false;
            seen[key] = true;
            return true;
        });
}

function selectedDftItemById(itemId) {
    if (!state.selectedPaper || !itemId) return null;
    const items = dftResultsWithSafety(state.selectedPaper);
    for (var i = 0; i < items.length; i += 1) {
        if (dftResultId(items[i]) === String(itemId)) return items[i];
    }
    return null;
}

function dftResultId(item) {
    if (!item) return "";
    return String(
        item.id ||
        item.record_id ||
        (item.export_safety && item.export_safety.record_id) ||
        ""
    ).trim();
}

function renderDftDecisionActions(item, exportable) {
    const resultId = dftResultId(item);
    if (!resultId) return "";
    const safety = item && item.export_safety;
    const reviewStatuses = String((safety && safety.review_status) || "")
        .toLowerCase()
        .split(",")
        .map(function(part) { return part.trim(); })
        .filter(Boolean);
    if (reviewStatuses.includes("rejected")) {
        return "";
    }
    if (exportable) {
        return '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">' +
            '<button class="btn ghost small" type="button" onclick="revokeDftResult(\'' + escAttr(resultId) + '\')">取消入库</button>' +
        '</div>';
    }
    return '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">' +
        '<button class="btn primary small" type="button" onclick="acceptDftResult(\'' + escAttr(resultId) + '\')">接受入库</button>' +
        '<button class="btn ghost small" type="button" onclick="rejectDftResult(\'' + escAttr(resultId) + '\')">拒绝</button>' +
    '</div>';
}

function dftItemStatusMeta(item) {
    const safety = item && item.export_safety;
    if (!safety) {
        return {
            label: "状态待加载",
            className: "meta",
            title: "这条 DFT 记录的入库安全状态还没有加载完成。"
        };
    }
    const exportable = safety.is_exportable === true || safety.eligible === true;
    const reviewStatuses = String(safety.review_status || "")
        .toLowerCase()
        .split(",")
        .map(function(part) { return part.trim(); })
        .filter(Boolean);
    if (reviewStatuses.includes("rejected")) {
        return {
            label: "已拒绝",
            className: "muted",
            title: "这条 DFT 候选已经被拒绝，不再属于待处理项。"
        };
    }
    const blockedReasons = Array.isArray(safety.blocked_reasons) ? safety.blocked_reasons : [];
    const reasons = dftBlockedReasonText(blockedReasons, item);
    return {
        label: exportable ? "可导出" : "需处理",
        className: exportable ? "parsed" : "meta",
        title: exportable
            ? "这条 DFT 候选已通过当前导出安全门。"
            : ("阻断原因：" + (reasons || "待按 AI 协议和 PDF 证据检查"))
    };
}

function renderDftItemStatusChip(item) {
    const meta = dftItemStatusMeta(item);
    if (!meta) return "";
    return '<span class="status-chip ' + meta.className + '" title="' + escAttr(meta.title) + '">' + esc(meta.label) + '</span>';
}

function dftAiOpinionMeta(item) {
    const audits = item && Array.isArray(item.object_review_audits) ? item.object_review_audits : [];
    if (!audits.length) {
        const candidateStatus = String(item && item.candidate_status || "").trim().toLowerCase();
        const importPolicy = String(item && item.evidence_payload && item.evidence_payload.import_policy || "").trim().toLowerCase();
        if (candidateStatus === "new_candidate" || importPolicy === "new_candidate_unverified_dft_result") {
            return {
                label: "待对象审核",
                className: "meta",
                title: "这条 DFT 数据由 AI 新发现并写入候选队列，必须完成对象级证据审核后才能进入正式数据库。"
            };
        }
        return {
            label: "无 AI 意见",
            className: "ok",
            title: "这条 DFT 没有对象级 AI 修正意见；若同时显示可导出，表示已通过当前安全门。"
        };
    }
    const sources = {};
    let hasReject = false;
    let hasProposed = false;
    let hasPass = false;
    let hasNeedsHuman = false;
    audits.forEach(function(audit) {
        const source = audit.source_label || audit.source || "unknown";
        sources[source] = true;
        const decision = dftOpinionDecision(audit);
        if (isNegativeDftDecision(decision)) hasReject = true;
        if (decision === "PROPOSED") hasProposed = true;
        if (decision === "PASS") hasPass = true;
        if (decision === "NEEDS_HUMAN") hasNeedsHuman = true;
    });
    const sourceCount = Object.keys(sources).length;
    const safety = item && item.export_safety;
    const exportable = item && (
        item.is_exportable === true ||
        (safety && (safety.is_exportable === true || safety.eligible === true))
    );
    const hasUnresolvedConflicts = Number(item && item.conflict_count || 0) > 0 ||
        Boolean(item && Array.isArray(item.field_conflicts) && item.field_conflicts.length);
    if (exportable && hasProposed && !hasUnresolvedConflicts) {
        return {
            label: "已采纳 AI 修正",
            className: "ok",
            title: "这条 DFT 已采纳 AI 修正意见，并已通过导出安全门。"
        };
    }
    if (hasReject && (hasPass || hasProposed)) {
        return {
            label: "AI 冲突",
            className: "failed",
            title: "至少一个 AI 建议拒绝，同时存在另一个 AI 的保留/通过意见，必须人工裁决。"
        };
    }
    if (hasReject) {
        return {
            label: sourceCount >= 2 ? "AI 一致拒绝" : "AI 建议拒绝",
            className: "failed",
            title: "AI 审核意见认为这条 DFT 候选应拒绝或删除。"
        };
    }
    if (hasNeedsHuman) {
        return {
            label: "AI 无法确认",
            className: "meta",
            title: "AI 无法从当前证据确认这条 DFT 候选，需要第三 AI 补证据或人工裁决。"
        };
    }
    if (hasProposed && sourceCount >= 2) {
        return {
            label: "AI 修正待采纳",
            className: "meta",
            title: "已有 AI 修正意见和另一 AI 的字段确认，但不是完整的双 AI 同字段一致；需要采纳修正并跑安全门。"
        };
    }
    if (hasProposed) {
        return {
            label: "AI 已提修正",
            className: "meta",
            title: "AI 已提出材料、单位、证据定位等修正，尚未采纳为最终字段。"
        };
    }
    if (hasPass) {
        return {
            label: sourceCount >= 2 ? "AI 字段通过" : "AI 确认字段",
            className: "ok",
            title: "AI 只确认了部分字段，不等于整条 DFT 候选已经满足入库条件。"
        };
    }
    return {
        label: "AI 意见待判定",
        className: "meta",
        title: "这条 DFT 候选有 AI 审核记录，但系统无法归类为通过、修正或拒绝。"
    };
}

function renderDftAiOpinionChip(item) {
    const meta = dftAiOpinionMeta(item);
    if (!meta) return "";
    return '<span class="status-chip ' + meta.className + '" title="' + escAttr(meta.title) + '">' + esc(meta.label) + '</span>';
}

function dftOpinionDecision(audit) {
    const decision = String(audit && audit.decision || "").trim().toUpperCase();
    if (["CONFIRMED", "ACCEPT", "ACCEPTED", "APPROVED", "VERIFIED", "OK"].includes(decision)) return "PASS";
    if (["CONFIRMED_WITH_CORRECTIONS", "CORRECTED", "REVISE", "REVISION"].includes(decision)) return "PROPOSED";
    return decision;
}

function dftOpinionSource(audit) {
    return String(audit && (audit.source_label || audit.source || "unknown") || "unknown");
}

function dftOpinionHasAnchor(audit) {
    const loc = audit && audit.evidence_location;
    if (!loc) return false;
    if (typeof loc === "string") return !!loc.trim();
    if (typeof loc !== "object") return false;
    return ["page", "section", "section_title", "figure", "figure_id", "table", "table_id", "quoted_text", "evidence_text", "bbox"]
        .some(function(key) {
            return loc[key] != null && String(loc[key]).trim();
        });
}

function dftWholeRowProposal(row) {
    const audits = row && Array.isArray(row.object_review_audits) ? row.object_review_audits.slice() : [];
    return audits
        .filter(function(audit) {
            return ["PROPOSED", "REVISE", "NEW_CANDIDATE"].includes(dftOpinionDecision(audit)) &&
                String(audit.field_name || "").trim() === "dft_results" &&
                audit.corrected_value &&
                typeof audit.corrected_value === "object" &&
                dftOpinionHasAnchor(audit);
        })
        .sort(sortDftAuditsNewestFirst)[0] || null;
}

function normalizeDftDecisionValue(value, unit) {
    const numeric = Number(value);
    const rawUnit = String(unit || "").trim();
    const unitKey = rawUnit.toLowerCase().replace(/\s+/g, "");
    if (!Number.isFinite(numeric)) return { value: null, unit: rawUnit };
    if (["e", "|e|", "electron", "electrons"].includes(unitKey)) {
        return { value: numeric, unit: "e" };
    }
    if (unitKey === "mev") return { value: numeric / 1000, unit: "eV" };
    if (unitKey === "ev") return { value: numeric, unit: "eV" };
    if (unitKey.includes("gpu")) {
        const asciiKey = Array.from(unitKey).filter((ch) => ch.charCodeAt(0) < 128).join("");
        const scaled = ["10^3", "x10^3", "103"].some((marker) => asciiKey.includes(marker)) ||
            (asciiKey.startsWith("10") && asciiKey !== "gpu");
        return { value: scaled ? numeric * 1000 : numeric, unit: "GPU" };
    }
    return { value: numeric, unit: rawUnit };
}

function dftAuditNormalizedTarget(row, audit) {
    const corrected = audit && audit.corrected_value;
    if (corrected && typeof corrected === "object") {
        return normalizeDftDecisionValue(corrected.value, corrected.unit || row.unit);
    }
    return normalizeDftDecisionValue(corrected == null ? row.value : corrected, row.unit);
}

function dftSameNormalizedValue(left, right) {
    if (!left || !right || left.value == null || right.value == null) return false;
    if (String(left.unit || "").toLowerCase() !== String(right.unit || "").toLowerCase()) return false;
    const tolerance = Math.max(1e-9, Math.abs(left.value) * 1e-6);
    return Math.abs(left.value - right.value) <= tolerance;
}

function dftAuditMaterialIdentity(audit) {
    const corrected = audit && audit.corrected_value;
    const value = corrected && typeof corrected === "object"
        ? (corrected.material_identity || corrected.material || corrected.catalyst || corrected.structure_name)
        : "";
    return String(value || audit && (audit.normalized_material || audit.normalized_material_or_catalyst) || "")
        .trim()
        .toLowerCase();
}

function dftIndependentOpinionsAgree(row, opinions) {
    const normalized = (opinions || []).map(function(audit) { return dftAuditNormalizedTarget(row, audit); });
    if (normalized.length < 2 || !normalized.every(function(item) {
        return dftSameNormalizedValue(normalized[0], item);
    })) return false;
    const materials = (opinions || []).map(dftAuditMaterialIdentity).filter(Boolean);
    if (materials.length < 2) return true;
    return materials.every(function(material) {
        return material === materials[0] || material.includes(materials[0]) || materials[0].includes(material);
    });
}

function dftSupportingValuePass(row, proposal) {
    if (!proposal) return null;
    const proposedTarget = dftAuditNormalizedTarget(row, proposal);
    const proposalSource = dftOpinionSource(proposal);
    const audits = row && Array.isArray(row.object_review_audits) ? row.object_review_audits : [];
    return audits.find(function(audit) {
        if (!["PASS", "PROPOSED", "REVISE", "NEW_CANDIDATE"].includes(dftOpinionDecision(audit))) return false;
        if (!["value", "dft_results"].includes(String(audit.field_name || "").trim())) return false;
        if (dftOpinionSource(audit) === proposalSource) return false;
        if (!dftOpinionHasAnchor(audit)) return false;
        return dftSameNormalizedValue(proposedTarget, dftAuditNormalizedTarget(row, audit));
    }) || null;
}

function dftExtractDuplicateTargetId(audit) {
    const text = [
        audit && audit.duplicate_of,
        audit && audit.reason,
        audit && audit.corrected_value && audit.corrected_value.duplicate_of
    ].filter(Boolean).join(" ");
    const match = text.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    return match ? match[0] : "";
}

function dftAnyItemById(resultId) {
    if (!state.selectedPaper || !resultId) return null;
    const items = dftResultsWithSafety(state.selectedPaper);
    return items.find(function(item) { return dftResultId(item) === String(resultId); }) || null;
}

function dftIsAutoRejectDuplicate(row) {
    const audits = row && Array.isArray(row.object_review_audits) ? row.object_review_audits : [];
    const rejectAudit = audits.find(function(audit) {
        return isNegativeDftDecision(audit && audit.decision) && /duplicate/i.test(String(audit.reason || audit.duplicate_of || ""));
    });
    if (!rejectAudit) return false;
    const duplicateId = dftExtractDuplicateTargetId(rejectAudit);
    const target = dftAnyItemById(duplicateId);
    if (!target) return false;
    const left = normalizeDftDecisionValue(row.value, row.unit);
    const right = normalizeDftDecisionValue(target.value, target.unit);
    return dftSameNormalizedValue(left, right);
}

function classifyDftAutomationRows(rows) {
    const result = { consensus: [], conflicts: [], newReview: [] };
    (rows || []).forEach(function(row) {
        if (!row || row.is_exportable === true) return;
        const audits = (Array.isArray(row.object_review_audits) ? row.object_review_audits : [])
            .filter(dftOpinionHasAnchor)
            .sort(sortDftAuditsNewestFirst);
        const bySource = {};
        audits.forEach(function(audit) {
            const source = dftOpinionSource(audit);
            if (!bySource[source]) bySource[source] = audit;
        });
        const independent = Object.keys(bySource).map(function(source) { return bySource[source]; });
        const repairReasons = new Set(["missing_material_identity", "missing_evidence", "missing_evidence_text", "unsafe_locator"]);
        const blockedReasons = Array.isArray(row.blocked_reasons) ? row.blocked_reasons : [];
        const hasReject = independent.some(function(audit) { return isNegativeDftDecision(audit && audit.decision); });
        const hasPositive = independent.some(function(audit) {
            const decision = dftOpinionDecision(audit);
            return ["PASS", "PROPOSED", "REVISE", "NEW_CANDIDATE"].includes(decision);
        });
        if (hasReject && hasPositive) {
            result.conflicts.push(row);
            return;
        }
        if (independent.length < 2 || blockedReasons.some(function(reason) { return repairReasons.has(reason); })) {
            result.newReview.push(row);
            return;
        }
        if (dftIsAutoRejectDuplicate(row)) {
            result.consensus.push(row);
            return;
        }
        if (hasReject && !hasPositive) {
            result.consensus.push(row);
            return;
        }
        const proposal = dftWholeRowProposal(row);
        const support = dftSupportingValuePass(row, proposal);
        if (proposal && support && dftIndependentOpinionsAgree(row, independent)) {
            result.consensus.push(row);
            return;
        }
        if (proposal) {
            result.conflicts.push(row);
            return;
        }
        if (dftIndependentOpinionsAgree(row, independent)) {
            result.consensus.push(row);
            return;
        }
        result.conflicts.push(row);
    });
    return result;
}

async function refreshDftAutomationSummaryBadges(container, paperId, renderSeq) {
    const targetPaperId = paperId || state.selectedPaperId;
    if (!container || !targetPaperId) return;
    try {
        const rows = await fetchSelectedDftReviewRows(200, targetPaperId);
        if (
            state.selectedPaperId !== targetPaperId ||
            (renderSeq && state.dftReadinessRenderSeq !== renderSeq) ||
            !container.isConnected
        ) {
            return;
        }
        const classified = classifyDftAutomationRows(rows);
        const setText = function(role, value) {
            const el = container.querySelector('[data-role="' + role + '"]');
            if (el) el.textContent = value;
        };
        setText("dft-new-review-count", "第二 AI / 补证据 " + classified.newReview.length);
        setText("dft-conflict-count", "第三 AI 裁决 " + classified.conflicts.length);
        setText(
            "dft-next-action",
            "生成下一轮 AI 审核任务（" + (classified.newReview.length + classified.conflicts.length) + "）"
        );
    } catch (_) {
        const pending = container.querySelector('[data-role="dft-new-review-count"]');
        if (pending) pending.textContent = "新数据审核 ?";
    }
}

async function settleAiDftReviews() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在结算当前论文已有的 DFT AI 审核...", "info");
        const summary = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/settle-ai-dft-reviews",
            { method: "POST" }
        );
        showToast(
            "已结算 " + Number(summary && summary.auto_applied_count || 0) +
            " 条；可导出 " + Number(summary && summary.exportable_count || 0) +
            "；需第三AI裁决 " + Number(summary && summary.need_third_ai_count || 0) +
            "；需补字段 " + Number(summary && summary.need_repair_count || 0),
            "success"
        );
        await refreshSelectedPaperDetail({ reason: "settle_ai_dft_reviews", mode: "full" });
    } catch (error) {
        showToast("结算现有 AI 审核失败：" + error.message, "error");
    }
}

function dftResultsWithSafety(detail) {
    const items = detail.dft_results_items || [];
    const readiness = detail.codex_context && detail.codex_context.dft_export_readiness;
    const safetyById = {};
    ((readiness && readiness.items) || []).forEach(function(item) {
        safetyById[String(item.record_id || "")] = item;
    });
    return items.map(function(item) {
        const recordId = dftResultId(item);
        const safety = safetyById[recordId];
        if (!safety) {
            return Object.assign({}, item, { record_id: recordId });
        }
        const reviewStatuses = String(safety.review_status || "")
            .toLowerCase()
            .split(",")
            .map(function(part) { return part.trim(); })
            .filter(Boolean);
        let effectiveCandidateStatus = item.candidate_status;
        if (reviewStatuses.includes("rejected")) {
            effectiveCandidateStatus = "Rejected";
        } else if (safety.is_exportable === true || safety.eligible === true) {
            effectiveCandidateStatus = "ML_Ready";
        } else if (reviewStatuses.includes("verified")) {
            effectiveCandidateStatus = "human_reviewed_needs_evidence";
        }
        return Object.assign({}, item, {
            record_id: recordId,
            export_safety: safety,
            candidate_status: effectiveCandidateStatus
        });
    });
}

function renderDftExportReadiness(detail) {
    const readiness = detail && detail.codex_context && detail.codex_context.dft_export_readiness;
    const fallbackTotal = Array.isArray(detail && detail.dft_results_items) ? detail.dft_results_items.length : 0;
    const hasReadiness = !!readiness;
    const readinessData = readiness || {};
    const rejectedCount = Number(readinessData.rejected_count || 0);
    const blockedCount = Number(readinessData.blocked_count || 0);
    const pendingCount = Math.max(0, blockedCount);
    const completionControls = hasReadiness && pendingCount === 0
        ? renderManualReviewCompletionControls(detail, "dft")
        : '<span class="status-chip subtle">未完成</span>';
    const reasons = Object.keys(readinessData.blocked_reasons || {}).map(function(reason) {
        return (DFT_BLOCK_REASON_LABELS[reason] || reason) + " " + readinessData.blocked_reasons[reason] + " 条";
    }).join("、");
    return '<div class="section-card figure-audit-note" data-role="dft-status-panel" data-paper-id="' + escAttr(detail && (detail.paper_id || detail.id) || "") + '">' +
        '<h3>DFT 数据状态</h3>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 10px;">' +
            completionControls +
            (hasReadiness
                ? '<span class="status-chip parsed">可导出 ' + Number(readiness.eligible_count || 0) + '</span>' +
                  '<span class="status-chip meta">待完成 ' + pendingCount + '</span>' +
                  (rejectedCount ? '<span class="status-chip muted">\u5df2\u62d2\u7edd ' + rejectedCount + '</span>' : '') +
                  '<span class="status-chip">候选总数 ' + Number(readiness.total_candidates || 0) + '</span>'
                : '<span class="status-chip meta">安全状态加载中</span>' +
                  '<span class="status-chip">候选总数 ' + fallbackTotal + '</span>') +
        '</div>' +
        '<div class="subtle">处理方式：点击“生成下一轮 AI 审核任务”，交给一位尚未审核这些记录的 AI；AI 回写后再点一次。系统会先自动写回一致项，只把缺第二意见、缺证据或真正冲突的记录放进下一轮。审核全部收口后才允许标记完成。</div>' +
        (reasons ? '<div class="subtle" style="margin-top:6px;">当前阻断：' + esc(reasons) + '</div>' : '') +
    '</div>';
}

async function resetDftAiReviewsForPaper() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const ok = window.confirm(
        "确认清除当前文献的 DFT AI 审核记录并重新核验吗？\n\n" +
        "这会删除 DFT AI 审核/冲突意见，把 DFT 候选退回待审核；不会删除候选 DFT 数据本身。"
    );
    if (!ok) return;
    try {
        showToast("正在清除当前文献的 DFT AI 审核状态...", "info");
        const summary = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/dft-ai-reviews/reset",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_reset_dft_ai_reviews: true,
                    reviewer: "literature_library_dft",
                    keep_dft_candidates: true
                })
            }
        );
        showToast(
            "已清除 DFT AI 审核：" +
            "对象意见 " + Number(summary && summary.deleted_object_review_candidates || 0) +
            " 条，字段审核 " + Number(summary && summary.deleted_field_reviews || 0) +
            " 条；候选退回 " + Number(summary && summary.reset_dft_results || 0) + " 条。",
            "success"
        );
        await refreshSelectedPaperDetail({ reason: "reset_dft_ai_reviews", mode: "full" });
    } catch (error) {
        showToast("清除 DFT AI 审核失败：" + error.message, "error");
    }
}

function writingCardReviewMeta(item) {
    if (item && item.can_use_for_writing) {
        return { label: "可直接参考", className: "high", tip: "这张写作卡已满足当前写作使用条件。" };
    }
    const reasons = Array.isArray(item && item.blocked_reasons) ? item.blocked_reasons : [];
    if (reasons.length) {
        return { label: "需先核对", className: "medium", tip: "当前仍有待处理项：" + reasons.join("、") };
    }
    return { label: "草稿候选", className: "unknown", tip: "这是自动生成的写作草稿，使用前建议先核对证据。" };
}

function writingCardLogicBlock(label, value) {
    const textValue = compactText(value);
    if (!textValue) return "";
    return '<div class="knowledge-detail-block"><div class="knowledge-detail-title">' + esc(label) + '</div><div class="knowledge-detail-text">' + esc(textValue) + '</div></div>';
}

function writingCardAuditSummaryHtml(item) {
    const auditCount = Number(item && (item.object_review_audit_count || (item.object_review_audits && item.object_review_audits.length)) || 0);
    const conflictCount = Number(item && (item.conflict_count || (item.field_conflicts && item.field_conflicts.length)) || 0);
    const latest = (item && (item.latest_object_review_audit || ((item.object_review_audits || [])[0]))) || null;
    const evidenceStatus = item && (item.evidence_status || item.evidence_chain_status) || "missing";
    const safetyStatus = item && (item.safety_status || item.review_gate_status) || "blocked";
    const safeVerified = Boolean(item && (item.safe_verified || item.can_use_for_writing));
    const latestHtml = latest
        ? '<div class="figure-review-latest"><strong>Latest audit:</strong> ' +
            esc(latest.source_label || latest.source || "unknown") +
            ' | decision=' + esc(latest.decision || "-") +
            ' | confidence=' + esc(latest.confidence == null ? "-" : latest.confidence) +
            ' | verification=' + esc(latest.verification_status || "unverified") +
            '</div>'
        : '<div class="subtle">Latest audit: none</div>';
    const conflictHtml = conflictCount
        ? '<div class="subtle">Conflict fields: ' + esc((item.field_conflicts || []).map(function(row) { return row.field_name || "-"; }).join(", ")) + '</div>'
        : "";
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<span class="status-chip">Object audits ' + auditCount + '</span>' +
            '<span class="status-chip ' + (conflictCount ? 'danger' : '') + '">Conflicts ' + conflictCount + '</span>' +
            '<span class="status-chip">Evidence status: ' + esc(prettifyToken(evidenceStatus)) + '</span>' +
            '<span class="status-chip ' + (safeVerified ? 'ok' : 'danger') + '">Safety: ' + esc(prettifyToken(safetyStatus)) + '</span>' +
        '</div>' +
        latestHtml +
        conflictHtml +
    '</div>';
}

function mechanismClaimAuditSummaryHtml(item) {
    const auditCount = Number(item && (item.object_review_audit_count || (item.object_review_audits && item.object_review_audits.length)) || 0);
    const conflictCount = Number(item && (item.conflict_count || (item.field_conflicts && item.field_conflicts.length)) || 0);
    const latest = (item && (item.latest_object_review_audit || ((item.object_review_audits || [])[0]))) || null;
    const evidenceStatus = item && item.evidence_status ? item.evidence_status : (compactText(item && item.evidence_text) ? "present" : "missing");
    const locatorStatus = item && item.locator_status ? item.locator_status : (compactText(item && item.evidence_text) ? "text_only" : "missing_locator");
    const confidenceStatus = item && item.confidence_status ? item.confidence_status : (item && item.confidence != null ? "candidate" : "missing");
    const latestHtml = latest
        ? '<div class="figure-review-latest"><strong>Latest audit:</strong> ' +
            esc(latest.source_label || latest.source || "unknown") +
            ' | decision=' + esc(latest.decision || "-") +
            ' | confidence=' + esc(latest.confidence == null ? "-" : latest.confidence) +
            ' | verification=' + esc(latest.verification_status || "unverified") +
            '</div>'
        : '<div class="subtle">Latest audit: none</div>';
    const conflictHtml = conflictCount
        ? '<div class="subtle">Conflict fields: ' + esc((item.field_conflicts || []).map(function(row) { return row.field_name || "-"; }).join(", ")) + '</div>'
        : "";
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<span class="status-chip">Object audits ' + auditCount + '</span>' +
            '<span class="status-chip ' + (conflictCount ? 'danger' : '') + '">Conflicts ' + conflictCount + '</span>' +
            '<span class="status-chip">Evidence status: ' + esc(prettifyToken(evidenceStatus)) + '</span>' +
            '<span class="status-chip">Locator: ' + esc(prettifyToken(locatorStatus)) + '</span>' +
            '<span class="status-chip">Confidence: ' + esc(prettifyToken(confidenceStatus)) + '</span>' +
        '</div>' +
        latestHtml +
        conflictHtml +
    '</div>';
}

function dftConflictSummaryHtml(item) {
    const conflicts = Array.isArray(item && item.field_conflicts) ? item.field_conflicts : [];
    const conflictCount = Number(item && (item.conflict_count || conflicts.length) || 0);
    if (!conflictCount) return "";
    const fields = [];
    [item && item.affected_field_names, item && item.conflict_field_names].concat(
        conflicts.map(function(conflict) {
            return conflict && (conflict.affected_field_names || conflict.conflict_field_names);
        })
    ).forEach(function(values) {
        (Array.isArray(values) ? values : []).forEach(function(field) {
            const normalized = compactText(field);
            if (normalized && !fields.includes(normalized)) fields.push(normalized);
        });
    });
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div><span class="status-chip danger">Conflicts ' + conflictCount + '</span></div>' +
        (fields.length ? '<div class="subtle">Conflict fields: ' + esc(fields.join(", ")) + '</div>' : '') +
    '</div>';
}

function isPendingNavigationItem(itemType, item) {
    const target = state.pendingNavigationTarget;
    return !!(
        target && item && item.id &&
        target.itemType === itemType &&
        String(target.targetId) === String(item.id)
    );
}

function renderWritingCardsCompact(items) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>写作卡片</h3><div class="muted">暂无内容。</div></div>';
    }
    const intro = '<div class="section-card figure-audit-note"><h3>写作卡片说明</h3><div class="subtle">这里优先显示适合写作时直接阅读的短摘要。详细逻辑、证据链和阻塞原因默认折叠，避免一上来铺成整页长文本。</div></div>';
    return intro + items.map(function(item, index) {
        const review = writingCardReviewMeta(item);
        const action = codexItemActionHtml("writing_card", item);
        const evidenceStatus = item && item.evidence_chain_status ? prettifyToken(item.evidence_chain_status) : "未提供";
        const summaryBlocks = [
            { label: "研究空白", value: item && item.research_gap },
            { label: "拟解决方案", value: item && item.proposed_solution },
            { label: "核心假设", value: item && item.core_hypothesis }
        ].filter(function(block) {
            return compactText(block.value);
        }).map(function(block) {
            return '<div class="writing-card-summary-block"><div class="writing-card-summary-title">' + esc(block.label) + '</div><div class="writing-card-summary-text">' + esc(clipText(block.value, 160)) + '</div></div>';
        }).join("");
        const details = [
            writingCardLogicBlock("摘要写法", item && item.abstract_logic),
            writingCardLogicBlock("引言写法", item && item.introduction_logic),
            writingCardLogicBlock("讨论写法", item && item.discussion_logic)
        ].filter(Boolean).join("");
        const auditSummary = writingCardAuditSummaryHtml(item || {});
        const blocked = Array.isArray(item && item.blocked_reasons) && item.blocked_reasons.length
            ? '<div class="knowledge-detail-block"><div class="knowledge-detail-title">当前限制</div><div class="knowledge-detail-text">' + esc(item.blocked_reasons.join("、")) + '</div></div>'
            : "";
        const navigationAttrs = ' data-codex-item-type="writing_card" data-target-id="' + escAttr(String(item && item.id || "")) + '"' +
            (isPendingNavigationItem("writing_card", item) ? " open" : "");
        return '<details class="section-card writing-card-compact"' + navigationAttrs + '>' +
            '<summary style="display:flex; justify-content:space-between; align-items:flex-start; flex:1; width:100%;">' +
                '<div style="flex:1;">' +
                    '<div class="knowledge-card-head">' +
                        '<div><h3 style="margin:0;">写作卡片 ' + (items.length > 1 ? (index + 1) : "") + '</h3><div class="knowledge-card-use">适合用来组织引言、摘要和讨论的写作骨架</div></div>' +
                        '<div class="knowledge-card-actions">' + action + '</div>' +
                    '</div>' +
                    '<div class="knowledge-tag-row">' +
                        '<span class="status-chip meta">' + esc(paperTypeLabel(item && item.paper_type)) + '</span>' +
                        '<span class="status-chip confidence-' + esc(review.className) + '" title="' + esc(review.tip) + '">' + esc(review.label) + '</span>' +
                        '<span class="status-chip" title="当前证据链状态">' + esc(evidenceStatus) + '</span>' +
                    '</div>' +
                '</div>' +
            '</summary>' +
            auditSummary +
            '<div class="writing-card-summary-grid">' + (summaryBlocks || '<div class="muted">这张写作卡还没有生成可直接阅读的短摘要。</div>') + '</div>' +
            '<details class="knowledge-details">' +
                '<summary>展开写作逻辑与限制</summary>' +
                details +
                blocked +
            '</details>' +
        '</details>';
    }).join("");
}

function renderReadableCards(title, items) {
    if (!items || !items.length) {
        if (title === "电化学性能") {
            return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">当前没有结构化电化学性能数据。该模块来自实验/电化学信号的 Stage 2 抽取，或由 IDE AI 通过 import_analysis 回写；纯计算论文通常为空。</div></div>';
        }
        if (title === "机理声明") {
            return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">当前没有结构化机理声明。该模块来自 Stage 2 机理规则抽取，或由 IDE AI 通过 import_analysis 回写；写作卡只引用这些证据，不承载原始结构化数据。</div></div>';
        }
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    if (title === "写作卡片") {
        return renderWritingCardsCompact(items);
    }
    const keySets = {
        "DFT 设置": ["software", "functional", "dispersion_correction", "pseudopotential", "cutoff_energy_ev", "cutoff_energy", "k_points", "convergence_settings", "vacuum_thickness_a", "vacuum_thickness"],
        "催化剂样本": ["name", "catalyst_type", "metal_centers", "coordination", "support", "synthesis_method", "evidence_text", "confidence"],
        "DFT 结果": ["catalyst", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "evidence_text", "confidence"],
        "候选 DFT 数据": ["candidate_status", "catalyst", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "source_figure", "evidence_text", "confidence"],
        "DFT 候选结果": ["candidate_status", "catalyst", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "source_figure", "evidence_text", "confidence"],
        "电化学性能": ["sulfur_loading", "sulfur_content", "electrolyte_sulfur_ratio", "capacity", "cycle_number", "rate", "decay_per_cycle", "evidence_text", "confidence"],
        "机理声明": ["claim_type", "claim_text", "key_species", "mechanism_direction", "evidence_text", "confidence"],
        "写作卡片": ["paper_type", "research_gap", "proposed_solution", "core_hypothesis", "evidence_text"],
        "表格": ["caption", "page", "markdown_content"],
        "出站关联": ["relationship_type", "target_title", "target_doi", "reason"],
        "入站关联": ["relationship_type", "source_title", "source_doi", "reason"]
    };
    let keys = keySets[title] ? keySets[title].slice() : Object.keys(items[0] || {}).filter(function(key) {
        return !["id", "paper_id", "raw_json", "created_at", "updated_at"].includes(key);
    }).slice(0, 10);
    const longFields = ["evidence_text", "markdown_content", "reason", "claim_text", "research_gap", "proposed_solution", "core_hypothesis", "caption"];
    keys.sort(function(a, b) {
        const aLong = longFields.includes(a) ? 1 : 0;
        const bLong = longFields.includes(b) ? 1 : 0;
        return aLong - bLong;
    });
    return items.map(function(item, index) {
        const heading = title + (items.length > 1 ? " " + (index + 1) : "");
        const itemType = CODEX_ITEM_TYPE_BY_CARD_TITLE[title];
        const action = codexItemActionHtml(itemType, item);
        const dftStatusChip = itemType === "dft_result" ? renderDftItemStatusChip(item) : "";
        const dftAiChip = itemType === "dft_result" ? renderDftAiOpinionChip(item) : "";
        const mechanismAuditSummary = itemType === "mechanism_claim" ? mechanismClaimAuditSummaryHtml(item || {}) : "";
        const dftEvidenceSource = itemType === "dft_result" ? renderDftEvidenceSource(item) : "";
        const dftConflictSummary = itemType === "dft_result" ? dftConflictSummaryHtml(item) : "";
        const safety = (title === "DFT 结果" || title === "候选 DFT 数据" || title === "DFT 候选结果") ? renderDftItemSafety(item) : "";
        const tableReviewChip = title === "\u8868\u683c" ? tableReviewChipHtml(item) : "";
        const itemTypeAttr = itemType ? ' data-codex-item-type="' + escAttr(itemType) + '"' : "";
        const targetIdAttr = item && item.id ? ' data-target-id="' + escAttr(String(item.id)) + '"' : "";
        const openAttr = isPendingNavigationItem(itemType, item) ? " open" : "";
        return '<details class="section-card readable-card"' + itemTypeAttr + targetIdAttr + openAttr + '>' +
            '<summary><div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;flex:1;width:100%;"><h3 style="margin:0;">' + esc(heading) + '</h3><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">' + dftStatusChip + dftAiChip + tableReviewChip + action + '</div></div></summary>' +
            '<div style="margin-top:10px;">' +
            renderReadableFields(item || {}, keys) +
            dftEvidenceSource +
            dftConflictSummary +
            mechanismAuditSummary +
            safety +
            '</div>' +
        '</details>';
    }).join("");
}

function renderComprehensiveAnalysis(data) {
    if (!data || !Object.keys(data).length) {
        return '<div class="section-card"><h3>综合解析</h3><div class="muted">暂无综合解析。</div></div>';
    }
    const summary = data.layman_summary || {};
    const logic = data.writing_logic || {};
    return '<details class="section-card readable-card"><summary><h3>综合解析</h3></summary>' +
        renderReadableFields({
            one_sentence_takeaway: summary.one_sentence_takeaway,
            real_world_impact: summary.real_world_impact,
            research_gap: logic.research_gap_framing,
            core_hypothesis: logic.core_hypothesis,
            conclusion_mapping: logic.conclusion_mapping
        }, ["one_sentence_takeaway", "real_world_impact", "research_gap", "core_hypothesis", "conclusion_mapping"]) +
    '</details>';
}

function isLikelyNoisyFigure(item) {
    item = item || {};
    var caption = String(item.caption || "").trim();
    var text = [
        caption,
        item.figure_role || "",
        item.content_summary || "",
        Array.isArray(item.key_elements) ? item.key_elements.join(" ") : ""
    ].join(" ").toLowerCase();
    var sparseCaption = /^(figure|fig\.?|scheme)\s*\d+\.?$/i.test(caption);
    var hasScientificSummary = !!(item.content_summary || (Array.isArray(item.key_elements) && item.key_elements.length));
    return /crossmark|science china press|publisher|logo|header|footer/.test(text) ||
        (sparseCaption && !hasScientificSummary);
}

// ── G3B Evidence Locator Rendering ──

function isPlaceholderFigureKeyElement(value) {
    const normalized = String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
    return ["verified_figure", "figure_verified", "reviewed_figure", "ai_verified", "verified"].includes(normalized);
}

function formatFigureKeyElementsForDisplay(keyElements) {
    if (!keyElements.length) return "-";
    const text = keyElements.map(figureTermLabel).join(", ");
    if (keyElements.every(isPlaceholderFigureKeyElement)) {
        return text + "（占位词，需要具体元素）";
    }
    if (keyElements.some(isPlaceholderFigureKeyElement)) {
        return text + "（含占位词）";
    }
    return text;
}

function figureTermLabel(value) {
    const raw = String(value == null ? "" : value).trim();
    if (!raw) return "-";
    const key = raw.toLowerCase().replace(/[\s-]+/g, "_");
    const labels = {
        structural_model: "结构模型",
        structure: "结构",
        model: "模型",
        mechanism_diagram: "机理示意图",
        band_structure: "能带结构",
        dos: "态密度",
        pdos: "分波态密度",
        charge_density: "电荷密度",
        reaction_pathway: "反应路径",
        reaction_barrier: "反应能垒",
        energy_profile: "能量曲线",
        fluorination: "氟化过程",
        adsorption_energy: "吸附能",
        xps: "XPS 谱图",
        spectra: "谱图",
        curve: "曲线",
        curves: "曲线",
        axis: "坐标轴",
        panel: "分图",
        panels: "分图",
        hexagonal_lattice: "六角晶格",
        stacking: "堆垛",
        aa_stacking: "AA 堆垛",
        ab_stacking: "AB 堆垛",
        abc_stacking: "ABC 堆垛",
        alpha_gdy_structure: "alpha-GDY 结构",
        hsgdy_structure: "HsGDY 结构",
        gdy_membrane: "GDY 膜",
        h2_molecules: "H2 分子",
        pair_potential: "对势曲线",
        interaction_energy_profile: "相互作用能曲线",
        quantum_transmission: "量子透射",
        time_series_crossings: "穿越数时间序列",
        spatial_distribution: "空间分布",
        arrhenius_plot: "Arrhenius 图",
        permeance_vs_temperature: "渗透率-温度曲线",
        position_dependent_barrier: "位置相关能垒",
        time_evolution: "时间演化"
    };
    if (labels[key]) return labels[key] + "（" + raw + "）";
    return raw;
}

function figureRagBlockedMap(detail) {
    const blockedItems = detail && detail.rag_quality && detail.rag_quality.figures && Array.isArray(detail.rag_quality.figures.blocked_items)
        ? detail.rag_quality.figures.blocked_items
        : [];
    const map = {};
    blockedItems.forEach(function(item) {
        if (item && item.source_id) map[item.source_id] = item;
    });
    return map;
}

function renderFigureParseDetailHtml(item) {
    item = item || {};
    const keyElements = Array.isArray(item.key_elements) ? item.key_elements : [];
    const imageReview = item.image_review || {};
    const approvedCorrectionFields = Array.isArray(item.approved_correction_fields) ? item.approved_correction_fields : [];
    const rows = [
        ["图号", item.figure_label || "-"],
        ["PDF 页码", item.page || "-"],
        ["图类型", figureTermLabel(item.figure_role || "unclassified")],
        ["类型置信度", item.role_confidence == null ? "-" : item.role_confidence],
        ["裁图状态", item.crop_status || "-"],
        ["裁图来源", item.crop_source || "-"],
        ["图片路径", item.image_path || "-"],
        ["内容摘要", item.content_summary || "-"],
        ["关键元素", formatFigureKeyElementsForDisplay(keyElements)],
        ["图片检查标记", Array.isArray(item.flags) && item.flags.length ? item.flags.join(", ") : "none"],
        ["整页快照", imageReview.full_page_image_path ? "有" : "缺失"],
        ["已批准修正数", item.approved_correction_count || 0],
        ["已批准修正字段", approvedCorrectionFields.length ? approvedCorrectionFields.join(", ") : "-"],
        ["对象审核数", item.object_review_audit_count || 0],
        ["冲突数", item.conflict_count || 0],
    ];
    return '<div class="figure-parse-detail">' +
        '<div class="figure-parse-title">图表解析字段</div>' +
        rows.filter(function(row) { return !(item.content_summary && row[1] === item.content_summary); }).map(function(row) {
            return '<div class="figure-parse-row"><strong>' + esc(row[0]) + '</strong><span>' + esc(row[1]) + '</span></div>';
        }).join("") +
    '</div>';
}

function openFigureLightbox(options) {
    options = options || {};
    const overlay = $("figureLightboxOverlay");
    const image = $("figureLightboxImage");
    const title = $("figureLightboxTitle");
    const meta = $("figureLightboxMeta");
    const src = options.src || "";
    if (!overlay || !image || !src) return;
    image.src = src;
    image.alt = options.alt || "图片大图预览";
    if (title) title.textContent = options.title || "图片预览";
    if (meta) {
        meta.textContent = [options.caption || "", options.page ? ("PDF 第 " + options.page + " 页") : ""]
            .filter(Boolean)
            .join(" | ");
    }
    overlay.style.display = "flex";
}

function closeFigureLightbox() {
    const overlay = $("figureLightboxOverlay");
    const image = $("figureLightboxImage");
    if (overlay) overlay.style.display = "none";
    if (image) {
        image.removeAttribute("src");
        image.alt = "图片大图预览";
    }
}

if (!window.__litaiFigureLightboxBound) {
    window.__litaiFigureLightboxBound = true;
    document.addEventListener("click", function(event) {
        const overlay = $("figureLightboxOverlay");
        if (overlay && event.target === overlay) closeFigureLightbox();
    });
    document.addEventListener("keydown", function(event) {
        if (event.key === "Escape") closeFigureLightbox();
    });
}

function escAttr(value) {
    return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

function normalizeLocatorStatus(locator) {
    const s = String((locator && locator.locator_status) || locator || "unknown").trim().toLowerCase();
    if (s === "exact" || s === "page_only") return "exact_page";
    if (s === "needs_reparse") return "missing_page";
    if (s === "missing") return "missing_locator";
    if (s === "approximate_candidate" || s === "ambiguous_match") return "approximate";
    return s;
}

function locatorCanJump(locator) {
    return normalizeLocatorStatus(locator) === "exact_page" && locator && locator.can_jump_to_pdf_page !== false && Number(locator.page || 0) > 0;
}

function locatorDegradedText(locator) {
    const s = normalizeLocatorStatus(locator);
    if (s === "approximate") return "可能相关页码，需要人工确认";
    if (s === "unresolved") return "证据定位待解析/待确认";
    if (s === "missing_locator") return "暂无可用 PDF 定位";
    return "仅有证据文本，暂无 PDF 页码定位";
}

function locatorStatusBadge(locatorStatus) {
    const s = normalizeLocatorStatus(locatorStatus);
    const label = uiLabel("locator_status", s);
    if (s === "exact_page") {
        return '<span class="status-chip" style="background:var(--color-success-bg);color:var(--color-success);border:1px solid var(--color-success)40;padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else if (s === "approximate") {
        return '<span class="status-chip" style="background:var(--color-primary-bg);color:var(--color-primary);border:1px solid var(--color-primary)40;padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else if (s === "text_only" || s === "missing_page") {
        return '<span class="status-chip" style="background:var(--color-warning-bg);color:var(--color-warning);border:1px solid var(--color-warning)40;padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else if (s === "missing_locator" || s === "unresolved") {
        return '<span class="status-chip" style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else {
        return '<span class="status-chip" style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    }
}

function locatorActionHtml(locator) {
    const s = normalizeLocatorStatus(locator);
    const paperId = locator.paper_id || "";
    const page = locator.page;
    const bbox = locator.bbox;
    const evidenceText = locator.evidence_text || "";

    if (locatorCanJump(locator)) {
        var uid = "loc-act-detail-" + (locator.id || Math.random().toString(36).slice(2));
        var bboxDataAttr = bbox ? ('data-bbox="' + escAttr(JSON.stringify(bbox)) + '"') : "";
        return '<span id="' + uid + '" data-paper-id="' + esc(paperId) + '" data-page="' + (page || 0) + '" data-has-bbox="' + (bbox ? "true" : "false") + '" data-locator-status="exact_page" data-evidence-text="' + escAttr(ellipsis(evidenceText, 80)) + '" ' + bboxDataAttr + ' style="display:none;"></span>' +
            '<button class="btn primary small" onclick="triggerDetailLocatorAction(\'' + uid + '\')">跳转到第 ' + (page || "?") + ' 页</button>';
    } else {
        return '<span style="font-size:12px;color:var(--color-text-secondary);">' + locatorDegradedText(locator) + '</span>';
    }
}

function buildPdfJumpButtonHtml(options) {
    options = options || {};
    var paperId = options.paperId || "";
    var page = Number(options.page || 0);
    if (!paperId || !page) return "";
    var uid = "pdf-jump-" + Math.random().toString(36).slice(2);
    var evidenceText = options.evidenceText || "";
    var stopPrefix = options.stopPropagation ? "event.stopPropagation(); " : "";
    var label = options.label || ("跳转到第 " + page + " 页");
    return '<span id="' + uid + '" data-paper-id="' + escAttr(paperId) + '" data-page="' + page + '" data-has-bbox="false" data-locator-status="exact_page" data-evidence-text="' + escAttr(evidenceText) + '" style="display:none;"></span>' +
        '<button class="btn ghost small" type="button" onclick="' + stopPrefix + 'triggerDetailLocatorAction(\'' + uid + '\')">' + esc(label) + '</button>';
}

function renderEvidenceLocators(locators) {
    const panel = $("evidenceLocatorsPanel");
    if (!panel) return;

    if (!locators || locators._error) {
        if (locators && locators._error) {
            panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted" style="color:var(--color-warning);">证据定位暂不可用</div><div class="muted" style="margin-top:8px;">请稍后重试；如果当前文献没有 PDF，也无法执行页码跳转。</div></div>';
        } else {
            panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted">暂无可定位证据</div><div class="muted" style="margin-top:8px;">可能原因：未上传 PDF、尚未生成页码定位，或当前只有证据文本没有精确页码。</div></div>';
        }
        return;
    }

    if (!Array.isArray(locators) || locators.length === 0) {
        panel.innerHTML = '<div class="muted">暂无可定位证据</div><div class="muted" style="margin-top:8px;">可能原因：未上传 PDF、尚未生成页码定位，或当前只有证据文本没有精确页码。</div>';
        return;
    }

    var html = '';
    locators.forEach(function(loc, idx) {
        html += '<div class="section-card" style="margin-bottom:8px;padding:10px;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">' +
                '<span style="font-size:12px;font-weight:700;color:var(--color-primary);">' + esc(ellipsis(loc.evidence_text || "无证据文本", 80)) + '</span>' +
                locatorStatusBadge(loc.locator_status) +
            '</div>' +
            '<div style="display:grid;grid-template-columns:80px 1fr;gap:2px 8px;font-size:12px;margin-bottom:6px;">' +
                '<span class="muted">页码</span><span>' + esc(loc.page != null ? loc.page : "-") + '</span>' +
                '<span class="muted">来源类型</span><span>' + esc(uiLabel("source", loc.source_type || "-")) + '</span>' +
                '<span class="muted">章节</span><span>' + esc(loc.section || "-") + '</span>' +
                '<span class="muted">目标类型</span><span>' + esc(loc.target_type || "-") + '</span>' +
                '<span class="muted">字段</span><span>' + esc(loc.field_name || "-") + '</span>' +
                '<span class="muted">定位置信度</span><span>' + esc(loc.locator_confidence != null ? Number(loc.locator_confidence).toFixed(2) : "-") + '</span>' +
                (loc.warning_reason ? '<span class="muted" style="color:var(--color-warning);">警告</span><span style="color:var(--color-warning);">' + esc(loc.warning_reason === 'bbox unavailable' ? '具体坐标不可用' : loc.warning_reason) + '</span>' : '') +
            '</div>' +
            '<div>' + locatorActionHtml(loc) + '</div>' +
        '</div>';
    });
    panel.innerHTML = html;
}

async function loadEvidenceLocators(paperId) {
    var result = await fetchPaperEvidenceLocators(paperId);
    if (state.selectedPaperId !== paperId) return;
    state.selectedPaperEvidenceLocators = result;
    renderEvidenceLocators(result);
}

function triggerDetailLocatorAction(uid) {
    var el = document.getElementById(uid);
    if (!el) return;
    var paperId = el.getAttribute("data-paper-id") || "";
    var page = parseInt(el.getAttribute("data-page") || "0", 10);
    var hasBbox = el.getAttribute("data-has-bbox") === "true";
    var locatorStatus = el.getAttribute("data-locator-status") || "";
    var evidenceText = el.getAttribute("data-evidence-text") || "";
    var bbox = null;
    try { bbox = JSON.parse(el.getAttribute("data-bbox") || "null"); } catch (_) {}
    openPdfViewer(paperId, page, hasBbox, bbox, locatorStatus, evidenceText);
}

async function openPdfViewer(paperId, page, hasBbox, bboxOrJson, locatorStatus, evidenceText) {
    var overlay = $("pdfViewerOverlay");
    if (!overlay) return;

    locatorStatus = normalizeLocatorStatus(locatorStatus);
    if (locatorStatus !== "exact_page") {
        return;
    }

    var bbox = null;
    if (typeof bboxOrJson === "string" && bboxOrJson) {
        try { bbox = JSON.parse(bboxOrJson); } catch (_) { bbox = null; }
    } else if (typeof bboxOrJson === "object" && bboxOrJson !== null) {
        bbox = bboxOrJson;
    }

    overlay.style.display = "flex";

    var iframe = $("pdfViewerIframe");
    var highlightOverlay = $("pdfHighlightOverlay");
    var viewerTitle = $("pdfViewerTitle");
    var viewerStatus = $("pdfViewerStatus");
    var viewerPageIndicator = $("pdfViewerPageIndicator");
    var viewerEvidencePanel = $("pdfViewerEvidencePanel");
    var viewerPdfUnavailable = $("pdfViewerUnavailable");
    var viewerPdfContent = $("pdfViewerContent");

    if (viewerTitle) viewerTitle.textContent = "PDF 预览 - 文献 " + paperId.slice(0, 8);
    if (viewerStatus) viewerStatus.textContent = "加载中...";
    if (viewerPageIndicator) viewerPageIndicator.textContent = "目标页码：" + Math.max(1, page || 1);

    // Hide unavailable message, show content area
    if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "none";
    if (viewerPdfContent) viewerPdfContent.style.display = "block";

    // Build evidence panel content
    var evidenceHtml = "";
    evidenceHtml = '<div style="font-size:12px;margin-bottom:4px;font-weight:700;color:var(--color-primary);">PDF 页码定位</div>' +
        '<div style="font-size:11px;color:var(--color-text-secondary);">这里用于查看原文页和核对证据。浏览器 PDF 工具栏里的临时高亮/绘制不会写回系统；需要保存结论时，请通过审核中心或 import_analysis 回写。</div>' +
        (evidenceText ? '<div style="font-size:11px;margin-top:6px;padding:6px 8px;background:var(--color-surface-alt);border-radius:var(--radius);border:1px solid var(--color-border);">"' + esc(evidenceText) + '"</div>' : '');
    if (viewerEvidencePanel) viewerEvidencePanel.innerHTML = evidenceHtml;

    // Probe PDF availability with HEAD request
    var pdfUrl = "/api/papers/" + encodeURIComponent(paperId) + "/pdf";
    try {
        var probeResp = await fetch(pdfUrl, { method: "HEAD" });
        if (!probeResp.ok && probeResp.status !== 405) {
            // PDF not available
            if (viewerPdfContent) viewerPdfContent.style.display = "none";
            if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "block";
            if (viewerStatus) viewerStatus.textContent = "PDF 尚未上传或不可预览";
            return;
        }
    } catch (_) {
        // Network error
        if (viewerPdfContent) viewerPdfContent.style.display = "none";
        if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "block";
        if (viewerStatus) viewerStatus.textContent = "PDF 请求失败";
        return;
    }

    if (viewerStatus) viewerStatus.textContent = "";

    // Load PDF in iframe with page fragment
    if (iframe) {
        iframe.src = pdfUrl + "#page=" + Math.max(1, page || 1) + "&toolbar=0&navpanes=0";
        iframe.style.display = "block";
    }

    // Clear highlight overlay (bbox positioning over iframe is unreliable;
    // evidence info is shown in the side panel instead)
    if (highlightOverlay) highlightOverlay.innerHTML = "";
}

function closePdfViewer() {
    var overlay = $("pdfViewerOverlay");
    if (overlay) overlay.style.display = "none";
    var iframe = $("pdfViewerIframe");
    if (iframe) iframe.src = "";
}

function contentReviewStatus(detail, key) {
    return (detail && detail[key]) || "missing";
}

function manualReviewProgress(detail) {
    const source = detail && detail.comprehensive_analysis && detail.comprehensive_analysis.manual_review_progress;
    const progress = source && typeof source === "object" ? source : {};
    function normalize(module) {
        const value = progress[module];
        if (value && typeof value === "object") {
            return {
                completed: !!value.completed,
                updated_at: value.updated_at || null,
                updated_by: value.updated_by || ""
            };
        }
        return {
            completed: !!value,
            updated_at: null,
            updated_by: ""
        };
    }
    return {
        content: normalize("content"),
        figures: normalize("figures"),
        dft: normalize("dft")
    };
}

function isManualReviewCompleted(detail, module) {
    const progress = manualReviewProgress(detail);
    return !!(progress[module] && progress[module].completed);
}

function renderManualReviewCompletionCard(detail, module, title, message) {
    const status = isManualReviewCompleted(detail, module);
    return '<div class="section-card figure-audit-note">' +
        '<h3>' + esc(title) + '</h3>' +
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 10px;">' +
            '<span class="status-chip ' + (status ? 'ok' : 'subtle') + '">' + esc(status ? '已完成' : '未完成') + '</span>' +
            '<button class="btn ' + (status ? 'ghost' : 'primary') + ' small" type="button" onclick="setManualReviewProgress(\'' + escAttr(module) + '\', ' + (status ? 'false' : 'true') + ')">' +
                esc(status ? '取消已完成' : '标记已完成') +
            '</button>' +
        '</div>' +
        '<div class="subtle">' + esc(message) + '</div>' +
    '</div>';
}

function renderManualReviewCompletionControls(detail, module) {
    const status = isManualReviewCompleted(detail, module);
    return '<span class="status-chip ' + (status ? 'ok' : 'subtle') + '">' + esc(status ? '已完成' : '未完成') + '</span>' +
        '<button class="btn ' + (status ? 'ghost' : 'primary') + ' small" type="button" onclick="setManualReviewProgress(\'' + escAttr(module) + '\', ' + (status ? 'false' : 'true') + ')">' +
            esc(status ? '取消已完成' : '标记已完成') +
        '</button>';
}

async function setManualReviewProgress(module, completed) {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const labels = {
        content: "内容解析",
        figures: "图表",
        dft: "DFT"
    };
    try {
        const result = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/manual-review-progress",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    module: module,
                    completed: !!completed,
                    reviewer: "literature_library"
                })
            }
        );
        if (state.selectedPaper) {
            const analysis = Object.assign({}, state.selectedPaper.comprehensive_analysis || {});
            analysis.manual_review_progress = result.manual_review_progress || {};
            state.selectedPaper.comprehensive_analysis = analysis;
            cachePaperDetail(state.selectedPaper);
            rerenderSelectedDetail(state.selectedPaperId);
        }
        showToast((labels[module] || "当前模块") + (completed ? "已标记完成。" : "已取消完成。"), "success");
    } catch (error) {
        showToast("更新完成状态失败：" + error.message, "error");
    }
}

function isAiVerifiedStatus(status) {
    return status === "ai_verified" || status === "reviewed";
}

function renderPendingReviewCard(title, message) {
    return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">' + esc(message) + '</div></div>';
}

function reviewStatusLabel(status, labels) {
    return labels[status] || labels.raw_only || status || "-";
}

function reviewStatusChipClass(status, options) {
    options = options || {};
    if (status === "risk" || status === "conflict") return "failed";
    if (status === "missing") return "none";
    if (status === "ai_verified" || status === "reviewed") return "full";
    if (status === "raw_only" || status === "candidate") return "parsed";
    return options.fallback || "meta";
}

function renderDetailTrustStrip(detail) {
    const abstractStatus = contentReviewStatus(detail, "abstract_review_status");
    const sectionsStatus = contentReviewStatus(detail, "sections_review_status");
    const figuresStatus = contentReviewStatus(detail, "figures_review_status");
    const dftStatus = contentReviewStatus(detail, "dft_review_status");
    const chip = function(label, value, className) {
        return '<span class="status-chip ' + escAttr(className || "meta") + '">' + esc(label) + '：' + esc(value) + '</span>';
    };
    return '<div class="section-card" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">' +
        '<strong>Review status</strong>' +
        chip("Figures", reviewStatusLabel(figuresStatus, { ai_verified: "AI verified", risk: "Risk", raw_only: "Parsed, not verified", missing: "Missing" }), reviewStatusChipClass(figuresStatus)) +
        chip("DFT", reviewStatusLabel(dftStatus, { reviewed: "Reviewed", conflict: "Conflict", candidate: "Candidate parsed", missing: "Missing" }), reviewStatusChipClass(dftStatus)) +
        chip("Abstract", reviewStatusLabel(abstractStatus, { ai_verified: "AI verified", raw_only: "Parsed, not verified", missing: "Missing" }), reviewStatusChipClass(abstractStatus)) +
        chip("Sections", reviewStatusLabel(sectionsStatus, { ai_verified: "AI verified", raw_only: "Parsed, not verified", missing: "Missing" }), reviewStatusChipClass(sectionsStatus)) +
    '</div>';
    return '<div class="section-card" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">' +
        '<strong>AI 核验状态</strong>' +
        chip("图表", reviewStatusLabel(figuresStatus, { ai_verified: "AI已核验", risk: "有风险", raw_only: "已解析", missing: "缺失" }), figuresStatus === "risk" ? "failed" : "meta") +
        chip("DFT", reviewStatusLabel(dftStatus, { reviewed: "已审核", conflict: "有冲突", candidate: "候选", missing: "缺失" }), dftStatus === "conflict" ? "failed" : "meta") +
        chip("摘要", reviewStatusLabel(abstractStatus, { ai_verified: "AI已核验", raw_only: "待AI核验", missing: "缺失" }), abstractStatus === "ai_verified" ? "parsed" : "meta") +
        chip("章节", reviewStatusLabel(sectionsStatus, { ai_verified: "AI已核验", raw_only: "待AI核验", missing: "缺失" }), sectionsStatus === "ai_verified" ? "parsed" : "meta") +
    '</div>';
}

function ragReasonLabel(reason) {
    const labels = {
        missing_image: "缺图",
        missing_page: "缺页码",
        missing_caption: "缺 caption",
        unclassified_or_unreviewed: "未分类/未核验",
        missing_material_identity: "缺材料身份",
        missing_review: "缺审核",
        unsafe_review: "审核不安全",
        missing_evidence: "缺证据",
        missing_evidence_text: "缺证据文本",
        unsafe_locator: "定位不安全",
        missing_property_type: "缺性质类型",
        missing_value: "缺数值",
        missing_unit: "缺单位",
        missing_figure_role: "缺图类型",
        missing_content_summary: "缺图摘要",
        missing_key_elements: "缺关键元素",
        caption_echo_summary: "图表摘要没有新增有效信息，只是重复图注",
        placeholder_key_elements: "key_elements 占位",
        contains_placeholder_key_elements: "key_elements 含占位",
        missing_evidence_chain: "缺证据链",
        unsafe_locator: "定位不安全",
        unreviewed: "未核验"
    };
    if (!reason) return "-";
    if (reason === "unlocated_full_page_recrop") return "整页兜底图，未精确定位";
    if (reason.indexOf("crop_status:") === 0) return "裁图状态 " + reason.split(":")[1];
    if (reason.indexOf("figure_role:") === 0) return "图片角色 " + reason.split(":")[1];
    return labels[reason] || reason;
}

function renderRagQualityPanel(detail) {
    const quality = detail && detail.rag_quality;
    if (!quality || typeof quality !== "object") return "";
    const groups = [
        ["图表 RAG", quality.figures || {}],
        ["DFT RAG", quality.dft_results || {}],
        ["写作卡 RAG", quality.writing_cards || {}]
    ];
    const cards = groups.map(function(pair) {
        const label = pair[0];
        const item = pair[1] || {};
        const total = Number(item.total || 0);
        const eligible = Number(item.eligible || 0);
        const blocked = Number(item.blocked || Math.max(0, total - eligible));
        const reasons = item.blocked_reasons || {};
        const warnings = item.quality_warnings || {};
        const blockedItems = Array.isArray(item.blocked_items) ? item.blocked_items : [];
        const reasonText = Object.keys(reasons).slice(0, 4).map(function(key) {
            return ragReasonLabel(key) + " " + reasons[key];
        }).join("；");
        const warningText = Object.keys(warnings).slice(0, 4).map(function(key) {
            return ragReasonLabel(key) + " " + warnings[key];
        }).join("；");
        const blockedList = blockedItems.length
            ? '<details class="rag-blocked-list" style="margin-top:8px;"><summary>查看不合格图表</summary>' +
                blockedItems.slice(0, 12).map(function(blocked) {
                    const reasons = Array.isArray(blocked.reasons) ? blocked.reasons : [];
                    const reasonText = reasons.map(ragReasonLabel).join("；") || "-";
                    const name = blocked.figure_label || (blocked.page ? "Page " + blocked.page : blocked.source_id);
                    return '<div class="subtle" style="margin-top:6px;">' +
                        '<strong>' + esc(name || "-") + '</strong>' +
                        (blocked.page ? ' · 第 ' + esc(blocked.page) + ' 页' : '') +
                        '：' + esc(reasonText) +
                    '</div>';
                }).join("") +
                (blockedItems.length > 12 ? '<div class="subtle" style="margin-top:6px;">还有 ' + (blockedItems.length - 12) + ' 项，请到图表页查看。</div>' : '') +
            '</details>'
            : "";
        return '<div class="stat-card rag-quality-card" style="flex-direction: column; align-items: flex-start; min-width: 0; gap: 6px;">' +
            '<div style="display: flex; justify-content: space-between; width: 100%; align-items: center;">' +
                '<h3 style="margin: 0;">' + esc(label) + '</h3>' +
                '<div class="value">' + eligible + ' / ' + total + '</div>' +
            '</div>' +
            '<div class="subtle" style="margin-top: 2px;">可用 ' + eligible + '，阻断 ' + blocked + '</div>' +
            (reasonText ? '<div class="subtle" style="margin-top:6px;">' + esc(reasonText) + '</div>' : '') +
            (warningText ? '<div class="subtle" style="margin-top:6px;">Warnings: ' + esc(warningText) + '</div>' : '') +
            blockedList +
        '</div>';
    }).join("");
    return '<div class="section-card rag-quality-panel">' +
        '<h3>RAG 可用状态</h3>' +
        '<div class="subtle">只统计正式 RAG 会使用的图表、DFT 和写作卡；raw 章节不计入。</div>' +
        '<div class="cards" style="margin-top:12px;">' + cards + '</div>' +
    '</div>';
}

function renderDetail(detail, audit) {
    const counts = detail.counts || {};
    const summaryCards =
        '<div class="cards">' +
            '<div class="stat-card"><h3>章节</h3><div class="value">' + (counts.sections || 0) + "</div></div>" +
            '<div class="stat-card"><h3>表格</h3><div class="value">' + (counts.tables || 0) + "</div></div>" +
            '<div class="stat-card"><h3>图片</h3><div class="value">' + (counts.figures || 0) + "</div></div>" +
            '<div class="stat-card"><h3>DFT 候选</h3><div class="value">' + (counts.dft_results || 0) + "</div></div>" +
            '<div class="stat-card"><h3>机理</h3><div class="value">' + (counts.mechanism_claims || 0) + "</div></div>" +
            '<div class="stat-card"><h3>写作卡</h3><div class="value">' + (counts.writing_cards || 0) + "</div></div>" +
        "</div>";

    const baseInfo =
        '<details class="section-card"><summary><h3>基础信息</h3></summary>' +
            '<div class="inline-grid">' +
                '<div class="key-value"><div class="k">文献库</div><div class="v">' + esc(detail.library_name || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">文献类型</div><div class="v">' + esc(paperTypeLabel(detail.paper_type)) + (detail.type_confidence ? ' (置信度 ' + detail.type_confidence + ')' : '') + '</div></div>' +
                '<div class="key-value"><div class="k">分类来源</div><div class="v">' + esc(detail.classification_source || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">创建时间</div><div class="v">' + esc(formatDate(detail.created_at)) + '</div></div>' +
                '<div class="key-value"><div class="k">PDF 路径</div><div class="v">' + esc(detail.pdf_path || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">Markdown 路径</div><div class="v">' + esc(detail.markdown_path || "-") + '</div></div>' +
            "</div>" +
        "</details>";

    const abstractReviewStatus = contentReviewStatus(detail, "abstract_review_status");
    const sectionsReviewStatus = contentReviewStatus(detail, "sections_review_status");
    const writingCardsReviewStatus = contentReviewStatus(detail, "writing_cards_review_status");
    const translationReviewStatus = contentReviewStatus(detail, "translation_review_status");
    const abstractCard = isAiVerifiedStatus(abstractReviewStatus)
        ? '<details class="section-card"><summary><h3>摘要</h3></summary><div class="prewrap">' + esc(detail.abstract || "暂无摘要。") + "</div></details>"
        : renderPendingReviewCard("摘要", "摘要待 AI 核验，不在详情页展示。");

    const localizedSummaryCard = renderLocalizedSummary(detail);
    const comprehensiveCard = "";

    const activeTab = state.currentTab || "summary";
    let sectionCards = "";
    if (activeTab === "sections") {
        if (isAiVerifiedStatus(sectionsReviewStatus)) {
            const displaySections = (detail.sections || []).filter(isDisplayBodySection).sort(compareDisplaySections).slice(0, 8);
            if (displaySections.length) {
            sectionCards = renderListBlock("章节内容", displaySections, function(item) {
                const text = cleanPdfExtractedText(item.text || "");
                return '<div class="prewrap">' + esc(ellipsis(text, 2200) || "暂无文本。") + "</div>";
            }, function(item) {
                const title = cleanPdfExtractedText(item.section_title || item.section_type || "未命名章节");
                return esc(title);
            });
            } else {
                sectionCards = renderPendingReviewCard("\u7ae0\u8282", "\u5f53\u524d\u53ea\u5269\u6574\u9875\u5207\u5206\u6216\u5df2\u5e9f\u5f03\u7ae0\u8282\uff0c\u6682\u65f6\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u7ed3\u6784\u5316\u7ae0\u8282\u3002");
            }
        } else {
            sectionCards = renderPendingReviewCard("章节", "章节待 AI 核验。");
        }
    }

    let figureCards = "";
    if (activeTab === "figures" && detail.figures && detail.figures.length) {
        function extractFigureNumber(caption) {
            if (!caption) return null;
            var m = String(caption).match(/(?:Figure|Fig\.?|Scheme)\s*\.?\s*(\d+)(?:\s*[\(\[]?\s*([a-z])\s*[\)\]]?)?/i);
            if (!m) return null;
            var num = parseInt(m[1], 10);
            return Number.isFinite(num) ? num : null;
        }

        function extractFigureSortParts(text) {
            if (!text) return { number: null, subRank: null };
            var m = String(text).match(/(?:Figure|Fig\.?|Scheme)\s*\.?\s*(\d+)(?:\s*[\(\[]?\s*([a-z])\s*[\)\]]?)?/i);
            if (!m) return { number: null, subRank: null };
            var num = parseInt(m[1], 10);
            if (!Number.isFinite(num)) return { number: null, subRank: null };
            var sub = m[2] ? m[2].toLowerCase().charCodeAt(0) - 96 : null;
            return {
                number: num,
                subRank: Number.isFinite(sub) ? sub : null
            };
        }

        function figureSortNumber(item) {
            return extractFigureNumber(item.figure_label) || extractFigureNumber(item.caption);
        }

        function figureSortParts(item) {
            var labelParts = extractFigureSortParts(item.figure_label);
            if (labelParts.number !== null) return labelParts;
            return extractFigureSortParts(item.caption);
        }

        function compareFigureItems(a, b) {
            var aPage = Number(a.page || 999999);
            var bPage = Number(b.page || 999999);
            if (aPage !== bPage) return aPage - bPage;
            var aParts = figureSortParts(a);
            var bParts = figureSortParts(b);
            if (aParts.number !== null && bParts.number !== null && aParts.number !== bParts.number) return aParts.number - bParts.number;
            if (aParts.number !== null && bParts.number === null) return -1;
            if (aParts.number === null && bParts.number !== null) return 1;
            if (aParts.subRank !== null && bParts.subRank !== null && aParts.subRank !== bParts.subRank) return aParts.subRank - bParts.subRank;
            if (aParts.subRank !== null && bParts.subRank === null) return -1;
            if (aParts.subRank === null && bParts.subRank !== null) return 1;
            return String(a.id || "").localeCompare(String(b.id || ""));
        }

        function figureRoleLabel(role) {
            var raw = String(role || "").trim();
            var key = raw.toLowerCase();
            if (!key || key === "unknown") return "未分类/待核对";
            var labels = {
                plot: "数据图",
                data_plot: "数据图",
                structure: "结构图",
                schematic: "示意图",
                method: "方法图",
                microscopy: "显微图",
                spectra: "谱图",
                table_snapshot: "表格截图"
            };
            return labels[key] || raw;
        }

        const sortedFigures = detail.figures.slice().sort(compareFigureItems);
        const figureBlockedMap = figureRagBlockedMap(detail);
        const roles = new Set();
        sortedFigures.forEach(f => {
            if (f.figure_role) roles.add(f.figure_role);
        });
        
        let filterHtml = '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">' +
            '<button class="btn primary small" type="button" onclick="recropPaperFigures(\'' + escAttr(detail.id) + '\')">重新定位/重裁图</button>' +
            '<span class="status-chip meta" title="裁剪图只作为候选图，必须对照 PDF 原页、图注和上下文。">图片定位可靠性需审核</span>' +
        '</div>';
        if (roles.size > 0) {
            filterHtml += '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">';
            filterHtml += '<button class="btn small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display=\'block\')">全部</button>';
            filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.ragBlocked === \'true\' ? \'block\' : \'none\')">只看 RAG 不合格</button>';
            filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.ragBlocked === \'false\' ? \'block\' : \'none\')">只看 RAG 可用</button>';
            roles.forEach(role => {
                filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.role === \'' + esc(role) + '\' ? \'block\' : \'none\')">' + esc(figureRoleLabel(role)) + '</button>';
            });
            filterHtml += '</div>';
        }

        const noisyCount = sortedFigures.filter(isLikelyNoisyFigure).length;
        const cardsHtml = sortedFigures.map(function(item, index) {
            const ragBlocked = figureBlockedMap[String(item.id || "")] || null;
            const ragReasons = ragBlocked && Array.isArray(ragBlocked.reasons) ? ragBlocked.reasons : [];
            const ragStatusHtml = ragBlocked
                ? '<span class="status-chip danger" title="' + escAttr(ragReasons.map(ragReasonLabel).join("；")) + '">RAG 不合格：' + esc(ragReasons.map(ragReasonLabel).join("；") || "未通过") + '</span>'
                : '<span class="status-chip ok">RAG 可用</span>';
            let imgHtml = "";
            const noisyFigure = isLikelyNoisyFigure(item);
            if (item.image_path && !noisyFigure) {
                const imageSrc = item.asset_url || ("/api/papers/assets/" + item.image_path);
                const lightboxPayload = escAttr(JSON.stringify({
                    src: imageSrc,
                    title: item.figure_label || ("图片 " + (index + 1)),
                    caption: item.caption || "",
                    page: item.page || "",
                    alt: item.figure_label || ("图片 " + (index + 1))
                }));
                const openPageAction = item.page
                    ? buildPdfJumpButtonHtml({
                        paperId: detail.id,
                        page: item.page,
                        evidenceText: clipText(item.caption || "", 160),
                        label: "打开图片对应页面",
                        stopPropagation: true
                    })
                    : "";
                imgHtml = '<figure class="figure-image-block">' +
                    '<button type="button" class="figure-lightbox-trigger" onclick="event.stopPropagation(); openFigureLightbox(' + lightboxPayload + ')" style="display:block;width:100%;padding:0;border:0;background:transparent;cursor:zoom-in;">' +
                        '<img src="' + escAttr(imageSrc) + '" loading="lazy" decoding="async" alt="Parsed paper figure" style="display:block;width:100%;max-height:420px;object-fit:contain;border:1px solid var(--color-border);border-radius:var(--radius-sm);background:var(--color-surface);" />' +
                    '</button>' +
                    '<figcaption><span class="figure-image-actions"><button type="button" class="btn ghost small" onclick="event.stopPropagation(); openFigureLightbox(' + lightboxPayload + ')">点击查看大图</button>' + openPageAction + '</span><span class="subtle figure-image-label">' + esc(item.figure_label || ("figure " + (index + 1))) + '</span></figcaption>' +
                '</figure>';
            } else if (!item.image_path) {
                var missingPageLink = item.page
                    ? '<a class="btn ghost small" style="text-decoration:none;" target="_blank" href="/api/papers/' + escAttr(detail.id) + '/pdf#page=' + Number(item.page || 1) + '&toolbar=0">打开 PDF 原页</a>'
                    : "";
                imgHtml = '<div class="figure-warning figure-missing-image">' +
                    '<strong>当前没有可展示的裁图。</strong>' +
                    '<div>这张图的结构化记录已保留，但 image_path 为空，页面只能显示文字信息和证据入口。后续补图后会自动恢复为图片卡片。</div>' +
                    (missingPageLink ? '<div style="margin-top:8px;">' + missingPageLink + '</div>' : '') +
                '</div>';
            } else if (noisyFigure) {
                var pageLink = item.page
                    ? '<a class="btn ghost small" style="text-decoration:none;" target="_blank" href="/api/papers/' + escAttr(detail.id) + '/pdf#page=' + Number(item.page || 1) + '&toolbar=0">打开 PDF 原页</a>'
                    : "";
                imgHtml = '<div class="figure-warning">' +
                    '<strong>自动截图疑似不是有效论文图。</strong>' +
                    '<div>这类图片通常是页眉、出版社标志或 CrossMark，不会作为可靠图表使用。请以 PDF 原页、图注和正文证据为准。</div>' +
                    (pageLink ? '<div style="margin-top:8px;">' + pageLink + '</div>' : '') +
                '</div>';
            }

            let metaHtml = "";
            if (item.figure_role) {
                metaHtml += '<span class="status-chip" style="background: var(--color-primary-bg); color: var(--color-text-secondary); margin-right: 8px;">' + esc(figureRoleLabel(item.figure_role)) + (item.role_confidence ? ' (' + (item.role_confidence*100).toFixed(0) + '%)' : '') + '</span>';
            }
            if (item.key_elements && item.key_elements.length) {
                item.key_elements.forEach(el => {
                    metaHtml += '<span class="status-chip meta" style="margin-right: 4px;">' + esc(figureTermLabel(el)) + '</span>';
                });
            }
            if (metaHtml) {
                metaHtml = '<div style="margin-top: 8px;">' + metaHtml + '</div>';
            }

            let summaryHtml = "";
            if (item.content_summary) {
                summaryHtml = '<div class="subtle" style="margin-top: 8px; font-weight: 500;">' + esc(item.content_summary) + '</div>';
            }

            var figNum = figureSortNumber(item);
            var figLabel = figNum !== null ? '图片 ' + figNum : '图片 ' + (index + 1);
            var codexAction = codexItemActionHtml("figure", item);
            var directDeleteAction = item.direct_delete_eligible
                ? '<button type="button" class="btn danger small" onclick="event.stopPropagation(); directDeleteFigure(\'' + escAttr(detail.id) + '\', \'' + escAttr(item.id) + '\', this)">直接删除</button>'
                : '';
            var legacyDeleteNote = Number(item.pending_delete_proposal_count || 0) > 0
                ? '<div class="subtle">Legacy delete proposals still pending (' + Number(item.pending_delete_proposal_count || 0) + ')</div>'
                : '';
            var reviewSummary = figureReviewSummaryHtml(item);

            return '<details class="section-card figure-card" data-role="' + esc(item.figure_role || 'unknown') + '" data-rag-blocked="' + (ragBlocked ? "true" : "false") + '" ontoggle="if(this.open){loadFigureCardImage(this.querySelector(\'.figure-image-placeholder\'));}">' +
                   '<summary><div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;flex:1;width:100%;"><h3 style="margin:0;">' + figLabel + '</h3><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' + ragStatusHtml + directDeleteAction + codexAction + '</div></div></summary>' +
                   '<div style="margin-top:10px;">' +
                   imgHtml +
                   '<div class="prewrap" style="margin-top:12px;">' + esc(item.caption || "无 caption") + "</div>" +
                   summaryHtml + metaHtml + legacyDeleteNote + renderFigureParseDetailHtml(item) + reviewSummary + '</div></details>';
        }).join("");
        
        const figureNotice = noisyCount
            ? '<div class="section-card figure-audit-note"><h3>图表抽取提示</h3><div class="subtle">检测到 ' + noisyCount + ' 张自动截图疑似无效。IDE AI 回写核对时应把这些当作抽取噪声处理，不再把出版社标志、CrossMark 或页眉图片当作科学图表。</div></div>'
            : "";
        figureCards = figureNotice + filterHtml + cardsHtml;
    } else if (activeTab === "figures") {
        figureCards = '<div class="section-card"><h3>图片</h3><div class="muted">暂无内容。</div></div>';
    }

    const pdfEvidenceEntry =
        '<details class="section-card pdf-evidence-entry" open><summary><h3>PDF 证据定位</h3></summary>' +
            '<div class="subtle" style="margin-bottom:12px;">' +
                '当前只支持有精确页码的证据跳转到 PDF，请使用右上角的“' + (paperHasPdf(detail) ? '查看 PDF / 证据定位' : 'PDF 未上传') + '”入口。<br>' +
                '如果下方仅显示文字，说明暂无精确的页码定位。' +
            '</div>' +
            '<div id="evidenceLocatorsPanel"><div class="muted">正在加载证据定位...</div></div>' +
        '</details>';

    let referenceCards = "";
    if (activeTab === "sections") {
        referenceCards = renderListBlock("参考文献", detail.references ? detail.references.slice(0, 20) : [], function(item) {
            return (
                '<div class="prewrap">' + esc(item.title || "未命名参考文献") + "</div>" +
                '<div class="subtle" style="margin-top:8px;">作者：' + esc(item.authors || "-") + " | DOI：" + esc(item.doi || "-") + "</div>" +
                (item.citation_context ? '<div class="mono" style="margin-top:8px;">' + esc(item.citation_context) + "</div>" : "")
            );
        });
    }

    const summaryEl = $("summaryContent");
    const sectionsEl = $("sectionsContent");
    const figuresEl = $("figuresContent");
    const dftEl = $("dftContent");
    const writingEl = $("writingContent");
    const translationEl = $("translationContent");
    const aggregateEl = $("aggregateResult");
    
    let missingPdfBanner = "";
    if (detail.oa_status === "metadata_only") {
        missingPdfBanner = 
            '<div class="section-card" style="border: 1px dashed var(--color-warning); background: var(--color-warning-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-warning); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 尚无 PDF' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '当前文献仅包含元数据（标题、期刊、年份、DOI 等），尚未上传或关联实际 PDF 文献文件。' +
                '</p>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<button class="btn primary small" onclick="document.getElementById(\'attachPdfInput\').click()">上传 PDF 并自动合并</button>' +
                    '<input id="attachPdfInput" type="file" accept=".pdf" style="display: none;" onchange="attachPDFToPaperDetail(this, \'' + detail.id + '\')">' +
                '</div>' +
            '</div>';
    }

    let auditBanner = "";
    const reviewTabEl = $("tab-review");
    const existingWarning = $("reviewTabAuditWarning");
    if (existingWarning) existingWarning.remove();
    const existingAiTrail = $("aiAuditTrailPanel");
    if (existingAiTrail) existingAiTrail.remove();
    const existingPaperNotes = $("paperNotesPanel");
    if (existingPaperNotes) existingPaperNotes.remove();
    const existingTaskLog = $("taskLogPanel");
    if (existingTaskLog) existingTaskLog.remove();
    
    if (audit && (audit.stale > 0 || audit.ambiguous > 0 || audit.unresolved > 0)) {
        const totalAlerts = (audit.stale || 0) + (audit.ambiguous || 0) + (audit.unresolved || 0);
        auditBanner = 
            '<div class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div class="subtle">请在审核中心或 DFT 数据库处理需要人工确认的数据；IDE AI 的总体意见会显示在“IDE AI 回写笔记”。</div>' +
            '</div>';
        
        const reviewTabWarningHtml = 
            '<div id="reviewTabAuditWarning" class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div class="subtle">请在审核中心或 DFT 数据库处理需要人工确认的数据；IDE AI 的总体意见会显示在“IDE AI 回写笔记”。</div>' +
            '</div>';
        if (reviewTabEl) {
            reviewTabEl.insertAdjacentHTML("afterbegin", reviewTabWarningHtml);
        }
    }
    if (reviewTabEl && activeTab === "review") {
        reviewTabEl.insertAdjacentHTML("afterbegin", renderTaskLogPanel(detail, audit));
    }
    
    if (summaryEl) {
        summaryEl.innerHTML =
            missingPdfBanner +
            auditBanner +
            summaryCards +
            renderDetailTrustStrip(detail) +
            renderRagQualityPanel(detail) +
            baseInfo +
            pdfEvidenceEntry +
            localizedSummaryCard +
            abstractCard +
            comprehensiveCard;
        if (state.selectedPaperEvidenceLocators !== undefined) {
            renderEvidenceLocators(state.selectedPaperEvidenceLocators);
        }
    }
    if (sectionsEl && activeTab === "sections") {
        sectionsEl.innerHTML =
            renderManualReviewCompletionCard(detail, "content", "内容解析进度", "当摘要、章节和详情页展示内容都核对完毕后，再手动标记完成。若之后重新补解析，可随时取消。") +
            sectionCards +
            referenceCards +
            '<div style="margin-bottom:16px;">' +
            '<button class="btn primary small" onclick="promptAddRelationship(\'' + detail.id + '\')">添加关联文献</button>' +
            '</div>' +
            renderJSONCards("出向关系", detail.outgoing_relationships || []) +
            renderJSONCards("入向关系", detail.incoming_relationships || []);
    }
    if (figuresEl && activeTab === "figures") {
        figuresEl.innerHTML =
            renderManualReviewCompletionCard(detail, "figures", "图表进度", "当图表对象、裁图和关键信息都核对完毕后，再手动标记完成。若后续重新补图或重裁，可取消。") +
            figureCards +
            renderJSONCards("表格", detail.tables || []);
    }
    if (dftEl && activeTab === "dft") {
        dftEl.innerHTML =
            renderDftExportReadiness(detail) +
            renderJSONCards("DFT 设置", detail.dft_settings_items || []) +
            renderJSONCards("催化剂样本", detail.catalyst_samples_items || []) +
            renderJSONCards("候选 DFT 数据", dftResultsWithSafety(detail)) +
            renderJSONCards("电化学性能", detail.electrochemical_performance_items || []) +
            renderJSONCards("机理声明", detail.mechanism_claims_items || []);
        decorateDftReadinessPanel(detail);
    }
    if (writingEl && activeTab === "writing") {
        const writingItems = detail.writing_cards_items || [];
        if (writingItems.length) {
            const reviewNotice = isAiVerifiedStatus(writingCardsReviewStatus)
                ? ""
                : '<div class="section-card figure-audit-note"><h3>\u5199\u4f5c\u5361\u72b6\u6001</h3><div class="subtle">\u8fd9\u6279\u5199\u4f5c\u5361\u8fd8\u6ca1\u8fdb\u5165 safe_verified\uff0c\u4f46\u73b0\u5728\u53ef\u4ee5\u76f4\u63a5\u67e5\u770b\u7814\u7a76\u7a7a\u767d\u3001\u62df\u89e3\u51b3\u65b9\u6848\u3001\u6838\u5fc3\u5047\u8bbe\u3001\u8bc1\u636e\u94fe\u548c\u5f53\u524d\u963b\u585e\u9879\u3002</div></div>';
            writingEl.innerHTML = reviewNotice + renderJSONCards("写作卡片", writingItems);
        } else {
            writingEl.innerHTML = renderPendingReviewCard("写作卡片", "\u5f53\u524d\u8fd8\u6ca1\u6709\u5199\u4f5c\u5361\u5185\u5bb9\u3002");
        }
    }
    if (false && writingEl && activeTab === "writing") {
        writingEl.innerHTML = isAiVerifiedStatus(writingCardsReviewStatus)
            ? renderJSONCards("写作卡片", detail.writing_cards_items || [])
            : renderPendingReviewCard("写作卡片", "写作卡待 AI 核验，不在详情页展示。");
    }
    if (translationEl && activeTab === "translation") {
        translationEl.innerHTML = (isAiVerifiedStatus(translationReviewStatus) || translationReviewStatus === "final_trusted")
            ? renderFullTranslation(detail)
            : renderPendingReviewCard("中文译文", "中文译文待 AI 核验或正式保存，不在详情页展示。");
    }
    if (aggregateEl) aggregateEl.innerHTML = "";
}

function renderDetailSkeleton() {
    const detailContainer = $("summaryContent");
    if (!detailContainer) return;
    ["sectionsContent", "figuresContent", "dftContent", "writingContent", "writerResult", "externalRuns", "aggregateResult"].forEach(function(id) {
        const el = $(id);
        if (el) el.innerHTML = "";
    });
    detailContainer.innerHTML = 
        '<div class="skeleton-detail">' +
            '<div class="skeleton-row">' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
            '</div>' +
            '<div class="skeleton skeleton-info" style="margin-top:14px; margin-bottom:14px;"></div>' +
            '<div class="section-card">' +
                '<div class="skeleton skeleton-text long"></div>' +
                '<div class="skeleton skeleton-text medium"></div>' +
                '<div class="skeleton skeleton-text long"></div>' +
                '<div class="skeleton skeleton-text short"></div>' +
            '</div>' +
        '</div>';
}

function clearDeferredDetailPanels() {
    ["sectionsContent", "figuresContent", "dftContent", "writingContent", "translationContent", "writerResult", "externalRuns", "aggregateResult"].forEach(function(id) {
        const el = $(id);
        if (el) el.innerHTML = "";
    });
}

function buildPreviewDetail(paper) {
    const source = paper || {};
    return {
        id: source.id,
        serial_number: source.serial_number,
        library_name: source.library_name || (state.currentLibrary && state.currentLibrary.name) || "",
        doi: source.doi,
        title: source.title,
        title_zh: source.title_zh,
        year: source.year,
        journal: source.journal,
        authors: source.authors || [],
        abstract: source.abstract,
        abstract_zh: source.abstract_zh,
        pdf_path: source.pdf_path,
        oa_status: source.oa_status,
        license: source.license,
        tei_path: source.tei_path,
        docling_json_path: source.docling_json_path,
        markdown_path: source.markdown_path,
        paper_type: source.paper_type,
        type_confidence: source.type_confidence,
        classification_source: source.classification_source,
        workflow_status: source.workflow_status,
        pdf_quality_status: source.pdf_quality_status,
        pdf_quality_score: source.pdf_quality_score,
        pdf_quality_report: source.pdf_quality_report,
        workspace_path: source.workspace_path,
        comprehensive_analysis: source.comprehensive_analysis || {},
        created_at: source.created_at,
        counts: source.counts || {},
        relationship_summary: source.relationship_summary || {},
        sections: [],
        tables: [],
        figures: [],
        dft_settings_items: [],
        catalyst_samples_items: [],
        dft_results_items: [],
        electrochemical_performance_items: [],
        mechanism_claims_items: [],
        writing_cards_items: [],
        outgoing_relationships: [],
        incoming_relationships: [],
        references: [],
        figure_data_points_items: [],
        full_translation_zh: source.full_translation_zh || null,
        is_preview_detail: true,
    };
}

function cachePaperDetail(detail) {
    if (!detail || !detail.id) return;
    state.paperDetailCache = state.paperDetailCache || {};
    const existing = state.paperDetailCache[detail.id];
    if (existing && existing._detailMode === "full" && detail._detailMode !== "full") {
        return;
    }
    state.paperDetailCache[detail.id] = detail;
    const keys = Object.keys(state.paperDetailCache);
    if (keys.length > 3) {
        delete state.paperDetailCache[keys[0]];
    }
}

function detailModeForTab(tab) {
    return tab === "summary" ? "light" : "full";
}

function renderImmediatePaperDetail(paperId) {
    const cached = state.paperDetailCache && state.paperDetailCache[paperId];
    const preview = state.papers.find(function(paper) { return paper.id === paperId; }) || state.selectedPaper;
    const immediate = cached || (preview && preview.id === paperId ? buildPreviewDetail(preview) : null);
    if (!immediate) {
        renderDetailSkeleton();
        return false;
    }
    state.selectedPaper = immediate;
    renderPaperList();
    renderWorkspaceHeader(immediate);
    renderDetail(immediate, state.selectedPaperAudit || null);
    showWorkspace();
    return true;
}

function scheduleDetailEnrichment(paperId, loadToken) {
    const run = function() {
        if (state.detailLoadToken === loadToken && state.selectedPaperId === paperId) {
            loadPaperDetailEnrichment(paperId, loadToken);
        }
    };
    if (window.requestIdleCallback) {
        window.requestIdleCallback(run, { timeout: 1200 });
    } else {
        window.setTimeout(run, 60);
    }
}

function applyPendingPdfJump(paperId) {
    const pending = state.pendingPdfJump;
    if (!pending || pending.opened || String(pending.paperId) !== String(paperId)) return;
    pending.opened = true;
    window.setTimeout(function() {
        if (state.selectedPaperId !== paperId) return;
        openPdfViewer(
            paperId,
            pending.page,
            false,
            null,
            pending.locatorStatus || "exact_page",
            pending.evidenceText || ""
        );
    }, 0);
}

async function loadPaperDetail(paperId, options) {
    if (!paperId) {
        showEmptyWorkspace();
        return;
    }
    const opts = options || {};
    const detailMode = opts.mode || "light";
    const loadToken = Date.now() + ":" + paperId;
    state.detailLoadToken = loadToken;
    state.selectedPaperId = paperId;
    state.selectedPaperAudit = null;
    state.selectedPaperEvidenceLocators = undefined;
    try {
        clearDeferredDetailPanels();
        renderImmediatePaperDetail(paperId);
        syncQueryParams();
        let detail = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(paperId) + "?mode=" + encodeURIComponent(detailMode)
        );
        if (state.detailLoadToken !== loadToken) return;
        const cachedFullDetail = state.paperDetailCache && state.paperDetailCache[paperId];
        if (cachedFullDetail && cachedFullDetail._detailMode === "full" && detailMode !== "full") {
            detail = cachedFullDetail;
        } else {
            detail._detailMode = detailMode;
        }
        state.selectedPaper = detail;
        cachePaperDetail(detail);
        renderPaperList();
        renderWorkspaceHeader(detail);
        renderDetail(detail, null);
        showWorkspace();
        applyPendingPdfJump(paperId);
        syncQueryParams();
        if (!opts.mode && detailMode !== "full" && detailModeForTab(state.currentTab) === "full") {
            window.setTimeout(function() {
                if (state.selectedPaperId === paperId) {
                    ensureFullPaperDetailForTab(state.currentTab);
                }
            }, 0);
        }
        scheduleDetailEnrichment(paperId, loadToken);
        loadEvidenceLocators(paperId);
        if (state.currentTab === "review") loadExternalRuns();
        if (state.currentTab === "aggregate") loadAggregate();
        if (state.currentTab === "writer") ensureWriterStatus();
    } catch (error) {
        if (state.detailLoadToken === loadToken) {
            showToast("详情加载失败：" + error.message, "error");
        }
    }
}

function ensureFullPaperDetailForTab(tab) {
    if (!state.selectedPaperId || detailModeForTab(tab) !== "full") return;
    if (state.selectedPaper && state.selectedPaper._detailMode === "full") return;
    if (state.fullDetailLoadingFor === state.selectedPaperId) return;
    const paperId = state.selectedPaperId;
    state.fullDetailLoadingFor = paperId;
    loadPaperDetail(paperId, { mode: "full" }).finally(function() {
        if (state.fullDetailLoadingFor === paperId) {
            state.fullDetailLoadingFor = null;
        }
    });
}

function rerenderSelectedDetail(paperId) {
    if (state.selectedPaperId !== paperId || !state.selectedPaper) return;
    renderDetail(state.selectedPaper, state.selectedPaperAudit || null);
    if (state.currentTab === "dft") {
        decorateDftReadinessPanel(state.selectedPaper);
    }
}

async function refreshSelectedPaperDetail(options) {
    if (!state.selectedPaperId) return null;
    const opts = options || {};
    const paperId = state.selectedPaperId;
    const mode = opts.mode || (state.selectedPaper && state.selectedPaper._detailMode) || detailModeForTab(state.currentTab);
    const refreshToken = Date.now() + ":refresh:" + paperId + ":" + (opts.reason || "detail");
    state.detailRefreshToken = refreshToken;
    const detail = await fetchJSON(
        API_BASE + "/" + encodeURIComponent(paperId) + "?mode=" + encodeURIComponent(mode)
    );
    if (state.detailRefreshToken !== refreshToken || state.selectedPaperId !== paperId) {
        return null;
    }
    detail._detailMode = mode;
    state.selectedPaper = detail;
    cachePaperDetail(detail);
    renderWorkspaceHeader(detail);
    renderDetail(detail, state.selectedPaperAudit || null);
    if (state.currentTab === "dft") {
        decorateDftReadinessPanel(detail);
    }
    showWorkspace();
    return detail;
}

async function refreshSelectedPaperDetailFromHeader() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献", "error");
        return;
    }
    const button = document.getElementById("refreshPaperDetailBtn");
    if (button && button.disabled) return;
    const previousLabel = button ? button.textContent : "刷新详情";
    if (button) {
        button.disabled = true;
        button.textContent = "刷新中…";
    }
    try {
        const detail = await refreshSelectedPaperDetail({ reason: "header_manual_refresh" });
        if (detail) {
            showToast("当前文献详情已刷新", "success");
        }
    } catch (error) {
        showToast("详情刷新失败：" + error.message, "error");
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = previousLabel;
        }
    }
}

function loadPaperDetailEnrichment(paperId, loadToken) {
    fetchJSON("/api/extraction/results/" + encodeURIComponent(paperId) + "/reviews/audit")
        .then(function(audit) {
            if (state.detailLoadToken === loadToken && state.selectedPaperId === paperId) {
                state.selectedPaperAudit = audit;
                rerenderSelectedDetail(paperId);
            }
        })
        .catch(function(e) {
            console.warn("Audit API is not available or failed:", e);
        });

    if ((state.currentTab === "dft" || state.currentTab === "review") && !(state.selectedPaper && state.selectedPaper.codex_context)) {
        fetchJSON(
            API_BASE + "/" + encodeURIComponent(paperId) +
            "/codex-context?max_sections=1&max_chars_per_section=300&max_figures=0&max_tables=0&max_candidates=500"
        )
            .then(function(codexBundle) {
                if (state.detailLoadToken === loadToken && state.selectedPaperId === paperId && codexBundle && codexBundle.context) {
                    state.selectedPaper.codex_context = codexBundle.context;
                    rerenderSelectedDetail(paperId);
                }
            })
            .catch(function(error) {
                console.warn("Codex context summary is not available:", error);
            });
    }

    if (state.currentTab === "writing") {
        loadPaperKnowledgeContext(paperId);
    }
}

function loadPaperKnowledgeContext(paperId) {
    if (!paperId || state.selectedPaperId !== paperId) return;
    if (state.selectedPaper && state.selectedPaper.knowledge_context) return;
    if (state.knowledgeContextLoadingFor === paperId) return;
    state.knowledgeContextLoadingFor = paperId;
    fetchJSON(
        API_BASE + "/" + encodeURIComponent(paperId) +
        "/knowledge-context?max_candidates=24&max_chars_per_candidate=600"
    )
            .then(function(audit) {
                if (state.selectedPaperId === paperId && state.selectedPaper) {
                    state.selectedPaper.knowledge_context = audit;
                    rerenderSelectedDetail(paperId);
                }
            })
            .catch(function(e) {
                console.warn("Knowledge context is not available:", e);
            })
            .finally(function() {
                if (state.knowledgeContextLoadingFor === paperId) {
                    state.knowledgeContextLoadingFor = null;
                }
            });
}

function openPaperDetailPage() {
    if (!state.selectedPaperId) return;
    window.open("/pages/paper_detail/index.html?paper_id=" + encodeURIComponent(state.selectedPaperId), "_blank");
}

function openSelectedReviewCenter() {
    if (!state.selectedPaperId) return;
    const params = new URLSearchParams();
    params.set("paper_id", state.selectedPaperId);
    const libraryName = (typeof getCurrentLibraryName === "function" ? getCurrentLibraryName() : "") ||
        (state.selectedPaper && state.selectedPaper.library_name) ||
        "";
    if (libraryName) params.set("library_name", libraryName);
    window.open("/pages/review_center/index.html?" + params.toString(), "_blank");
}

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

async function canonicalIdePromptForSelectedPaper(kind) {
    const guide = await fetchJSON("/api/system/agent-guide");
    const contract = guide && guide.prompt_contract ? guide.prompt_contract : {};
    const templates = contract.templates && typeof contract.templates === "object" ? contract.templates : {};
    const template = templates[kind] || templates.overall || guide.suggested_client_prompt || "";
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
        .split(contract.source_label_token || "{{SOURCE_LABEL}}").join(sourceLabel);
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
        "任务：审核下面这些已列出的 DFT 候选；不要把清单内候选重新当成新数据提交。",
        "要求：先核对 PDF 证据，再逐条给出完整意见。审核来源名称不固定，但必须与每条记录 existing_review_sources 中的来源不同。",
        "强制规则：清单内每条候选都必须使用该行给出的 target_id；禁止对清单内候选输出 target_id='new' 或 decision='new_candidate'。",
        "不要输出长解释；只输出一个可直接用于 import_analysis 的 JSON，顶层只保留 object_review_audits。",
        "如果当前 IDE 没有暴露 MCP 工具，不要直接停下；请通过仓库内 `app.mcp.context.mcp_auth_context` 建立明确身份，再受控调用 `app.mcp.server` 已公开的 MCP 工具。禁止直接调用 service/session/model 或数据库。",
        "",
        "决策规则：",
        "- 证据、材料身份、数值、单位、定位都能确认且无需改字段时，用 PASS。",
        "- 候选明显错误、重复、无证据支持时，用 REJECT。",
        "- 候选基本正确但字段需要修正/补全时，用 PROPOSED，并填写完整 corrected_value。",
        "- 无法从 PDF 确认时，用 NEEDS_HUMAN，不要硬判。",
        "- 清单内候选只允许 PASS / REJECT / PROPOSED / NEEDS_HUMAN，不能用 new_candidate。",
        "- 只有发现清单外确实漏提的额外 DFT 行时，才可在处理完清单后追加 decision='new_candidate'、target_id='new'、field_name='dft_results'。",
        "- 追加漏提行后，不要只停在 candidate-only JSON；实际调用 import_analysis 时应使用 auto_apply_review_rules=true，让 new_candidate 自动进入未验证 DFT 候选队列。",
        "- 缺 material identity、缺证据原文、缺准确页码定位时，不要 PASS。",
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
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在写回一致意见并生成新数据审核提示词...", "info");
        await settleDftConsensusBeforePrompt();
        const pendingRows = await fetchSelectedDftReviewRows(200);
        const rows = classifyDftAutomationRows(pendingRows).newReview;
        if (!rows.length) {
            showToast("当前没有新发现或缺少独立第二意见的 DFT 数据。", "info");
            return;
        }
        const canonicalPrompt = await canonicalIdePromptForSelectedPaper("dft");
        await navigator.clipboard.writeText(
            [canonicalPrompt, buildCompactBlockedDftBatchPrompt(rows)].filter(Boolean).join("\n\n")
        );
        showToast("新数据审核提示词已复制，请交给未审核过这些记录的独立 AI。", "success");
    } catch (error) {
        showToast("新数据审核提示词生成失败：" + error.message, "error");
    }
}

function dftQueueUrlForSelectedPaper(limit, paperId) {
    const targetPaperId = paperId || state.selectedPaperId;
    return "/api/papers/export/dft-review-queue?paper_id=" +
        encodeURIComponent(targetPaperId) +
        "&status=needs_review&limit=" + encodeURIComponent(limit || 200);
}

async function fetchSelectedDftReviewRows(limit, paperId) {
    const targetPaperId = paperId || state.selectedPaperId;
    if (!targetPaperId) return [];
    const queue = await fetchJSON(dftQueueUrlForSelectedPaper(limit || 200, targetPaperId));
    return Array.isArray(queue && queue.rows) ? queue.rows.filter(function(row) {
        return row && row.is_exportable !== true;
    }) : [];
}

async function applyImportedDftOpinion(resultId, opinion) {
    return fetchJSON(
        API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
        "/dft-results/" + encodeURIComponent(resultId) + "/apply-imported-opinion",
        {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                reviewer: "literature_library_dft_auto",
                opinion: opinion
            })
        }
    );
}

async function rejectDftResultById(resultId, note) {
    return fetchJSON(
        API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
        "/dft-results/" + encodeURIComponent(resultId) + "/reject",
        {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                confirm_reject_candidate: true,
                reviewer: "literature_library_dft_auto",
                reviewer_note: note || "Auto-rejected as a low-risk duplicate from the Literature Library DFT panel."
            })
        }
    );
}

async function autoProcessLowRiskDftRows() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在分析低风险 DFT 候选...", "info");
        const rows = await fetchSelectedDftReviewRows(200);
        const classified = classifyDftAutomationRows(rows);
        const acceptCount = classified.autoAccept.length;
        const rejectCount = classified.autoReject.length;
        if (!acceptCount && !rejectCount) {
            showToast("当前没有可自动处理的低风险 DFT 候选。", "info");
            return;
        }
        const ok = window.confirm(
            "将自动采纳 " + acceptCount + " 条、自动拒绝重复项 " + rejectCount +
            " 条；其余仍保留为第三 AI 仲裁或人工处理。继续吗？"
        );
        if (!ok) return;
        let applied = 0;
        let rejected = 0;
        for (var i = 0; i < classified.autoAccept.length; i += 1) {
            const row = classified.autoAccept[i];
            const resultId = row.record_id || row.id;
            const opinions = importedDftAcceptanceOpinions(row);
            for (var j = 0; j < opinions.length; j += 1) {
                await applyImportedDftOpinion(resultId, opinions[j]);
            }
            applied += 1;
        }
        for (var k = 0; k < classified.autoReject.length; k += 1) {
            const row = classified.autoReject[k];
            await rejectDftResultById(row.record_id || row.id, "Auto-rejected duplicate after AI duplicate opinion and normalized value check.");
            rejected += 1;
        }
        showToast("低风险处理完成：采纳 " + applied + " 条，拒绝 " + rejected + " 条。", "success");
        await refreshSelectedPaperDetail({ reason: "auto_process_dft", mode: "full" });
    } catch (error) {
        showToast("低风险自动处理失败：" + error.message, "error");
    }
}

function buildThirdAiDftAdjudicationPrompt(rows) {
    const paper = state.selectedPaper || {};
    const paperId = paper.paper_id || paper.id || state.selectedPaperId || "-";
    const sourcePdf = (
        paper.codex_context &&
        paper.codex_context.source_assets &&
        paper.codex_context.source_assets.pdf_path
    ) || "<source_pdf>";
    const compactRows = (rows || []).map(function(row, index) {
        return {
            index: index + 1,
            target_id: row.record_id || row.id,
            current: {
                property_type: row.property_type,
                adsorbate: row.adsorbate,
                value: row.value,
                unit: row.unit,
                reaction_step: row.reaction_step,
                blocked_reasons: row.blocked_reasons || []
            },
            prior_ai_opinions: (row.object_review_audits || []).map(function(audit) {
                return {
                    candidate_id: audit.candidate_id,
                    source_identity: audit.source_label || audit.source || "unknown",
                    source: audit.source_label || audit.source || "unknown",
                    decision: audit.decision,
                    field_name: audit.field_name,
                    corrected_value: audit.corrected_value,
                    reason: audit.reason,
                    evidence_location: audit.evidence_location
                };
            })
        };
    });
    return [
        "任务：你是第三 AI 裁决员，只处理下面最终数据真正不一致的 DFT 候选。",
        "必须读取原始 PDF 或 PDF 证据包；不要只复述前两个 AI 的意见。",
        "如果当前 IDE 没有暴露 MCP 工具，请通过仓库内 `app.mcp.context.mcp_auth_context` 建立明确身份，再受控调用 `app.mcp.server` 已公开的 MCP 工具读取证据；禁止直接操作 service/session/model 或数据库。",
        "只需输出有争议字段的最终值；未争议字段由系统从当前记录和 selected_source_ids 自动补齐。",
        "必须填写 adjudication_role='third_ai'。选择已有意见时填写 selected_source_ids；可填 candidate_id、source_identity 或 source。",
        "可以给出新的 evidence_location；若沿用被选意见的证据，系统会从 selected_source_ids 自动继承。",
        "如果缺证据页码或原文，请主动在 PDF 中定位；确实找不到时输出 NEEDS_HUMAN，记录继续留在冲突裁决队列。",
        "单位标准：能量统一为 eV，meV 除以 1000；渗透率统一为 GPU，10^3 GPU 乘以 1000；原始表达写入 raw_value/raw_unit 或 evidence quoted_text。",
        "重复项必须明确 duplicate_of，并说明保留哪条、拒绝哪条。",
        "只输出 JSON：顶层 object_review_audits，不要长解释。",
        "",
        "输出字段：decision=PASS|PROPOSED|REJECT|NEEDS_HUMAN；target_type=dft_results；field_name=dft_results；adjudication_role=third_ai；selected_source_ids=[]；corrected_value 只写裁决后需要覆盖的字段；evidence_location 写新证据时包含 page、quoted_text、source_document_type、source_pdf。",
        "",
        "paper_id=" + paperId,
        "title=" + (paper.title_zh || paper.title || "-"),
        "doi=" + (paper.doi || "-"),
        "source_pdf=" + sourcePdf,
        "",
        JSON.stringify({ disputed_dft_candidates: compactRows }, null, 2)
    ].join("\n");
}

async function copyThirdAiDftAdjudicationPrompt() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在写回一致意见并生成 DFT 冲突裁决提示词...", "info");
        await settleDftConsensusBeforePrompt();
        const rows = await fetchSelectedDftReviewRows(200);
        const classified = classifyDftAutomationRows(rows);
        if (!classified.conflicts.length) {
            showToast("当前没有最终数据真正不一致的 DFT 意见。", "info");
            return;
        }
        const canonicalPrompt = await canonicalIdePromptForSelectedPaper("dft");
        await navigator.clipboard.writeText(
            [canonicalPrompt, buildThirdAiDftAdjudicationPrompt(classified.conflicts)].filter(Boolean).join("\n\n")
        );
        showToast("DFT 冲突裁决提示词已复制。", "success");
    } catch (error) {
        showToast("DFT 冲突裁决提示词生成失败：" + error.message, "error");
    }
}

async function copyNextDftAiReviewPrompt() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在自动写回一致项并生成下一轮 AI 审核任务...", "info");
        await settleDftConsensusBeforePrompt();
        const rows = await fetchSelectedDftReviewRows(200);
        const classified = classifyDftAutomationRows(rows);
        if (!classified.newReview.length && !classified.conflicts.length) {
            await refreshSelectedPaperDetail({ reason: "dft_workflow_complete", mode: "full" });
            showToast("当前 DFT 审核已经收口，没有下一轮任务。", "success");
            return;
        }
        const canonicalPrompt = await canonicalIdePromptForSelectedPaper("dft");
        const promptParts = [canonicalPrompt];
        if (classified.newReview.length) {
            promptParts.push(buildCompactBlockedDftBatchPrompt(classified.newReview));
        }
        if (classified.conflicts.length) {
            promptParts.push(buildThirdAiDftAdjudicationPrompt(classified.conflicts));
        }
        await navigator.clipboard.writeText(promptParts.filter(Boolean).join("\n\n"));
        await refreshSelectedPaperDetail({ reason: "dft_next_ai_prompt", mode: "full" });
        showToast(
            "下一轮任务已复制：第二 AI / 补证据 " + classified.newReview.length +
            " 条，第三 AI 裁决 " + classified.conflicts.length + " 条。",
            "success"
        );
    } catch (error) {
        showToast("下一轮 DFT 审核任务生成失败：" + error.message, "error");
    }
}

function decorateDftReadinessPanel(detail) {
    const panel = $("dftContent");
    if (!panel) return;
    const card = panel.querySelector('[data-role="dft-status-panel"]');
    if (!card || card.querySelector('[data-role="dft-readiness-actions"]')) return;
    const readiness = detail && detail.codex_context && detail.codex_context.dft_export_readiness;
    const paperId = String(detail && (detail.paper_id || detail.id) || state.selectedPaperId || "");
    const blockedCount = Number(readiness && readiness.blocked_count || 0);
    const renderSeq = (state.dftReadinessRenderSeq || 0) + 1;
    state.dftReadinessRenderSeq = renderSeq;
    const actions = document.createElement("div");
    actions.setAttribute("data-role", "dft-readiness-actions");
    actions.style.display = "flex";
    actions.style.gap = "8px";
    actions.style.flexWrap = "wrap";
    actions.style.margin = "0 0 10px";
    actions.innerHTML =
        (blockedCount
            ? '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;width:100%;">' +
                '<span class="status-chip meta" data-role="dft-new-review-count">第二 AI / 补证据 ...</span>' +
                '<span class="status-chip failed" data-role="dft-conflict-count">第三 AI 裁决 ...</span>' +
              '</div>' +
              '<button class="btn primary small" data-role="dft-next-action" type="button" onclick="copyNextDftAiReviewPrompt()">生成下一轮 AI 审核任务</button>'
            : "") +
        '<button class="btn ghost small" type="button" onclick="settleAiDftReviews()">重新检查写回</button>' +
        '<button class="btn ghost small" type="button" onclick="resetDftAiReviewsForPaper()">清除 AI 审核重来</button>' +
        '<button class="btn ghost small" type="button" onclick="openSelectedReviewCenter()">打开审核中心</button>';
    const firstSubtle = card.querySelector(".subtle");
    if (firstSubtle && firstSubtle.parentNode === card) {
        card.insertBefore(actions, firstSubtle);
    } else {
        card.appendChild(actions);
    }
    if (blockedCount) {
        refreshDftAutomationSummaryBadges(actions, paperId, renderSeq);
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

function renderAiAuditTrail(items) {
    const aiItems = (items || []).filter(function(item) {
        const payload = item.review_payload || {};
        return payload.latest_ai_audit || (payload.ai_audits && payload.ai_audits.length);
    }).slice(0, 12);
    if (!aiItems.length) {
        return "";
    }
    return (
        '<div id="aiAuditTrailPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;">' +
            '<h3>AI 审核建议记录</h3>' +
            '<div class="subtle">这里显示 AI / GLM / 第二 AI 写入的审核意见。AI 结论不会直接进入可信数据库；冲突项会标记为 review_conflict 并要求人工确认。</div>' +
            '<div style="display:grid;gap:10px;margin-top:12px;">' +
                aiItems.map(renderAiAuditTrailItem).join("") +
            '</div>' +
        '</div>'
    );
}

function taskLogActorLabel(value) {
    const normalized = compactText(value).toLowerCase();
    if (!normalized) return "system";
    if (normalized === "ide_ai") return "IDE AI";
    return compactText(value);
}

function taskLogTargetLabel(targetType, targetId, fieldName) {
    const bits = [compactText(targetType), compactText(fieldName)];
    const base = bits.filter(Boolean).join(" / ");
    if (!compactText(targetId)) return base || "paper";
    return (base || "target") + " / " + String(targetId).slice(0, 8);
}

function collectTaskLogEntries(detail, audit) {
    const entries = [];
    const seen = new Set();
    const pushEntry = function(entry) {
        if (!entry) return;
        const key = [
            entry.time || "",
            entry.actor || "",
            entry.action || "",
            entry.target || "",
            entry.detail || ""
        ].join("|");
        if (seen.has(key)) return;
        seen.add(key);
        entries.push(entry);
    };

    (state.externalRuns || []).forEach(function(run) {
        const candidates = Array.isArray(run && run.candidates) ? run.candidates : [];
        const counts = {};
        candidates.forEach(function(item) {
            const type = compactText(item && item.candidate_type) || "candidate";
            counts[type] = (counts[type] || 0) + 1;
        });
        const summary = Object.keys(counts).sort().map(function(type) {
            return type + " x" + counts[type];
        }).join(", ");
        pushEntry({
            time: run.created_at || null,
            actor: taskLogActorLabel(run.source_label || run.source || "ide_ai"),
            action: "import_analysis 导入",
            target: "paper",
            detail: summary || "no candidates",
            tone: "meta"
        });
    });

    (detail.paper_notes || []).forEach(function(note) {
        pushEntry({
            time: note.created_at || null,
            actor: taskLogActorLabel(note.source || "ide_ai"),
            action: "写入 AI 笔记",
            target: taskLogTargetLabel("paper_note", "", note.field_name || "overall"),
            detail: clipText(note.content || "", 180),
            tone: "meta"
        });
    });

    [
        ["figure", detail.figures || []],
        ["dft_result", detail.dft_results_items || []],
        ["writing_card", detail.writing_cards_items || []],
        ["mechanism_claim", detail.mechanism_claims_items || []],
        ["table", detail.tables || []]
    ].forEach(function(group) {
        const targetType = group[0];
        (group[1] || []).forEach(function(item) {
            (item.object_review_audits || []).slice(0, 3).forEach(function(auditItem) {
                const decision = compactText(auditItem.decision || auditItem.verification_status || "reviewed");
                pushEntry({
                    time: auditItem.created_at || auditItem.reviewed_at || null,
                    actor: taskLogActorLabel(auditItem.source_label || auditItem.source || auditItem.reviewer || "ide_ai"),
                    action: "对象审核",
                    target: taskLogTargetLabel(targetType, item.id, auditItem.field_name || item.figure_label || item.claim_type || ""),
                    detail: decision,
                    tone: /reject|conflict|need|repair|block/i.test(decision) ? "danger" : "ok"
                });
            });
        });
    });

    ((audit && audit.items) || []).forEach(function(item) {
        const payload = item.review_payload || {};
        const latest = payload.latest_ai_audit || ((payload.ai_audits || []).slice(-1)[0]) || {};
        if (!latest || (!latest.reviewer && !latest.model_name && !latest.decision)) return;
        const decision = compactText(latest.decision || item.reviewer_status || "reviewed");
        pushEntry({
            time: latest.created_at || item.reviewed_at || item.updated_at || item.created_at || null,
            actor: taskLogActorLabel(latest.reviewer || item.reviewer || "ide_ai"),
            action: "字段审核",
            target: taskLogTargetLabel(item.target_type, item.target_id, item.field_name),
            detail: decision,
            tone: /reject|conflict/i.test(decision) ? "danger" : "ok"
        });
    });

    return entries.sort(function(a, b) {
        const left = a.time ? Date.parse(a.time) : 0;
        const right = b.time ? Date.parse(b.time) : 0;
        return right - left;
    }).slice(0, 40);
}

function renderTaskLogPanel(detail, audit) {
    const entries = collectTaskLogEntries(detail || {}, audit || null);
    if (!entries.length) {
        return '<div id="taskLogPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;"><h3>任务日志</h3><div class="muted">当前还没有可展示的任务日志。</div></div>';
    }
    return (
        '<div id="taskLogPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;">' +
            '<h3>任务日志</h3>' +
            '<div class="subtle">这里只按时间记录 AI 何时导入、审核、写回了什么。</div>' +
            '<div style="display:grid;gap:10px;margin-top:12px;">' +
                entries.map(function(entry) {
                    return '<div class="candidate-card">' +
                        '<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap;">' +
                            '<strong>' + esc(entry.actor || "system") + '</strong>' +
                            '<span class="status-chip ' + escAttr(entry.tone || "meta") + '">' + esc(entry.time ? formatDate(entry.time) : "time unknown") + '</span>' +
                        '</div>' +
                        '<div style="margin-top:6px;">' + esc(entry.action || "action") + ' | ' + esc(entry.target || "paper") + '</div>' +
                        (entry.detail ? '<div class="subtle" style="margin-top:6px;">' + esc(entry.detail) + '</div>' : '') +
                    '</div>';
                }).join("") +
            '</div>' +
        '</div>'
    );
}

function renderPaperNotesPanel(notes) {
    const rows = (notes || []).slice(0, 12);
    if (!rows.length) return "";
    return (
        '<div id="paperNotesPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;">' +
            '<h3>IDE AI 回写笔记</h3>' +
            '<div class="subtle">这里显示 import_analysis 写入的 review_notes。它表示 AI 已经给出审核意见，但不等于 DFT 数据已自动确认入库。</div>' +
            '<div style="display:grid;gap:10px;margin-top:12px;">' +
                rows.map(renderPaperNoteItem).join("") +
            '</div>' +
        '</div>'
    );
}

function renderPaperNoteItem(note) {
    const meta = [
        note.source || "ide_ai",
        note.field_name || "overall",
        note.page ? ("p." + note.page) : "",
        note.created_at ? formatDate(note.created_at) : ""
    ].filter(Boolean).join(" / ");
    return (
        '<div class="candidate-card">' +
            '<div class="subtle">' + esc(meta) + '</div>' +
            '<div style="margin-top:8px;">' + esc(note.content || "") + '</div>' +
            (note.quoted_text ? '<div class="mono" style="margin-top:8px;">' + esc(note.quoted_text) + '</div>' : '') +
        '</div>'
    );
}

function loadFigureCardImage(container) {
    if (!container || container.getAttribute("data-loaded") === "1") return;
    const src = container.getAttribute("data-figure-src");
    if (!src) return;
    container.setAttribute("data-loaded", "1");
    container.innerHTML =
        '<img src="' + escAttr(src) + '" loading="lazy" decoding="async" ' +
        'style="max-width:100%;max-height:400px;border:1px solid var(--color-border);border-radius:var(--radius-sm);object-fit:contain;" ' +
        'alt="提取的文献图片" />';
}

function renderAiAuditTrailItem(item) {
    const payload = item.review_payload || {};
    const audits = payload.ai_audits || [];
    const latest = payload.latest_ai_audit || audits[audits.length - 1] || {};
    const protocol = latest.protocol || {};
    const conflict = payload.review_conflict || item.reviewer_status === "review_conflict";
    const hash = protocol.sha256 ? String(protocol.sha256).slice(0, 12) : "-";
    return (
        '<div class="candidate-card" style="' + (conflict ? 'border-color:var(--color-danger);' : '') + '">' +
            '<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;">' +
                '<strong>' + esc(item.target_type || "-") + " / " + esc(item.field_name || "-") + '</strong>' +
                '<span class="badge ' + (conflict ? 'danger' : 'ok') + '">' + esc(item.reviewer_status || "-") + '</span>' +
            '</div>' +
            '<div class="subtle" style="margin-top:6px;">AI：' + esc(latest.reviewer || item.reviewer || "-") +
                ' ｜ 模型：' + esc(latest.model_name || "-") +
                ' ｜ 角色：' + esc(latest.agent_role || "-") +
                ' ｜ 决策：' + esc(latest.decision || "-") +
            '</div>' +
            '<div class="subtle">协议：' + esc(protocol.version || protocol.key || "-") + ' ｜ hash：' + esc(hash) + '</div>' +
            (item.reviewer_note ? '<div style="margin-top:8px;">' + esc(item.reviewer_note) + '</div>' : '') +
            (conflict ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">AI 结论冲突：该条必须人工确认后才能进入可信数据。</div>' : '') +
        '</div>'
    );
}

async function verifyDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法核验。", "error");
        return;
    }
    const ok = window.confirm("请确认你已经按 AI 解析协议，对照 PDF 原文、证据文本、页码/章节/表格/图号和重复项检查过这条 DFT 候选。确认后，该记录才会尝试进入可导出安全门。");
    if (!ok) return;
    try {
        showToast("正在写入 DFT 核验记录...", "info");
        const data = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(itemId) + "/verify",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_reviewed_against_pdf: true,
                    reviewer: "user_codex_review",
                    reviewer_note: "Verified from the Literature Library DFT panel after checking Codex item context and evidence."
                })
            }
        );
        const safety = data && data.export_safety;
        showToast(
            safety && safety.is_exportable
                ? "DFT 候选已审核可导出。"
                : "DFT 核验已记录，但仍有安全门阻断项。",
            safety && safety.is_exportable ? "success" : "info"
        );
        await refreshSelectedPaperDetail({ reason: "verify_dft_result", mode: "full" });
    } catch (error) {
        showToast("DFT 核验失败：" + error.message, "error");
    }
}

async function acceptDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法接受入库。", "error");
        return;
    }
    const item = selectedDftItemById(itemId);
    const opinionMeta = dftAiOpinionMeta(item);
    if (opinionMeta && opinionMeta.label === "AI 冲突") {
        showToast("这条 DFT 存在 AI 冲突，不能直接接受入库；请展开查看后人工拒绝或重新处理。", "error");
        return;
    }
    const opinions = importedDftAcceptanceOpinions(item);
    const ok = window.confirm(opinions.length
        ? "确认应用这条 DFT 的 AI 修正意见，并接受入库吗？"
        : "确认接受这条 DFT 数据并入库吗？");
    if (!ok) return;
    try {
        showToast(opinions.length ? "正在应用 AI 修正并接受入库..." : "正在写入 DFT 接受结果...", "info");
        let data;
        if (opinions.length) {
            for (var i = 0; i < opinions.length; i += 1) {
                data = await fetchJSON(
                    API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
                    "/dft-results/" + encodeURIComponent(itemId) + "/apply-imported-opinion",
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            reviewer: "literature_library_dft",
                            opinion: opinions[i]
                        })
                    }
                );
            }
        } else {
            data = await fetchJSON(
                API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
                "/dft-results/" + encodeURIComponent(itemId) + "/verify",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        confirm_reviewed_against_pdf: true,
                        reviewer: "literature_library_dft",
                        reviewer_note: "Accepted from the Literature Library DFT panel."
                    })
                }
            );
        }
        const safety = (data && data.review_result && data.review_result.export_safety) || (data && data.export_safety) || {};
        showToast(
            safety && safety.is_exportable
                ? "这条 DFT 已应用 AI 修正并入库。"
                : "已应用接受结果，但这条 DFT 还有阻断项。",
            safety && safety.is_exportable ? "success" : "info"
        );
        await refreshSelectedPaperDetail({ reason: "accept_dft_result", mode: "full" });
    } catch (error) {
        showToast("接受入库失败：" + error.message, "error");
    }
}

async function rejectDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法拒绝。", "error");
        return;
    }
    const ok = window.confirm("确认拒绝这条 DFT 数据吗？");
    if (!ok) return;
    try {
        showToast("正在写入 DFT 拒绝结果...", "info");
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(itemId) + "/reject",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_reject_candidate: true,
                    reviewer: "literature_library_dft",
                    reviewer_note: "Rejected from the Literature Library DFT panel."
                })
            }
        );
        showToast("这条 DFT 已拒绝。", "success");
        await refreshSelectedPaperDetail({ reason: "reject_dft_result", mode: "full" });
    } catch (error) {
        showToast("拒绝失败：" + error.message, "error");
    }
}

async function revokeDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法取消入库。", "error");
        return;
    }
    const item = selectedDftItemById(itemId);
    const resultId = dftResultId(item) || String(itemId);
    const ok = window.confirm("确认把这条 DFT 从已入库退回待处理吗？");
    if (!ok) return;
    try {
        showToast("正在取消这条 DFT 的入库状态...", "info");
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(resultId) + "/revoke-review",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    reviewer: "literature_library_dft",
                    reviewer_note: "Revoked from the Literature Library DFT panel.",
                    field_names: []
                })
            }
        );
        showToast("这条 DFT 已退回待处理。", "success");
        await refreshSelectedPaperDetail({ reason: "revoke_dft_result", mode: "full" });
    } catch (error) {
        showToast("取消入库失败：" + error.message, "error");
    }
}

async function directDeleteFigure(paperId, figureId, button) {
    const reason = window.prompt("请输入直接删除这张污染/重复图片的理由：");
    if (!reason || !reason.trim()) return;
    const figures = state.selectedPaper && Array.isArray(state.selectedPaper.figures) ? state.selectedPaper.figures : [];
    const figure = figures.find(function(item) { return String(item.id) === String(figureId); }) || {};
    try {
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(paperId) + "/figures/" + encodeURIComponent(figureId) + "/delete",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_direct_delete: true,
                    reviewer: "literature_library_user",
                    reason: reason.trim(),
                    evidence_payload: {
                        page: figure.page || null,
                        figure_label: figure.figure_label || null,
                        caption: figure.caption || null,
                    },
                    delete_image_file: true,
                }),
            }
        );
        if (state.selectedPaper && Array.isArray(state.selectedPaper.figures)) {
            state.selectedPaper.figures = state.selectedPaper.figures.filter(function(item) {
                return String(item.id) !== String(figureId);
            });
        }
        const card = button && button.closest ? button.closest("details.figure-card") : null;
        if (card) card.remove();
        showToast("污染/重复图片已删除。", "success");
    } catch (error) {
        showToast("图片删除失败：" + error.message, "error");
    }
}

async function recropPaperFigures(paperId) {
    if (!paperId) {
        showToast("当前文献无法重新裁图。", "error");
        return;
    }
    const ok = window.confirm("将重新根据 PDF 页码、图注和候选裁剪框定位图片。裁剪图仍然只是候选证据，需要人工核对原 PDF 页。是否继续？");
    if (!ok) return;
    try {
        showToast("正在重新定位/重裁图片...", "info");
        const data = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(paperId) + "/figures/recrop",
            { method: "POST" }
        );
        showToast("图片重裁完成：" + (data.extracted_count || 0) + " / " + (data.figure_count || 0), "success");
        if (state.selectedPaperId === paperId) {
            await refreshSelectedPaperDetail({ reason: "recrop_figures", mode: "full" });
        } else {
            await loadPaperDetail(paperId);
        }
    } catch (error) {
        showToast("图片重裁失败：" + error.message, "error");
    }
}

async function promptAddRelationship(paperId) {
    const targetId = window.prompt("请输入目标文献的 ID (例如您刚上传的补充材料):");
    if (!targetId) return;
    const relType = window.prompt("请输入关联类型 (如: supplementary, citation):", "supplementary");
    if (!relType) return;
    
    try {
        showToast("正在创建关联...", "info");
        await fetchJSON(API_BASE + "/" + encodeURIComponent(paperId) + "/relationships", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                target_paper_id: targetId.trim(),
                relationship_type: relType.trim(),
                note: "Manual frontend binding"
            })
        });
        showToast("关联创建成功", "success");
        if (state.selectedPaperId === paperId) {
            await refreshSelectedPaperDetail({ reason: "relationship_created", mode: "full" });
        } else {
            await loadPaperDetail(paperId);
        }
    } catch (e) {
        showToast("创建失败: " + e.message, "error");
    }
}
