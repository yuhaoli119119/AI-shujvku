function renderWorkspaceHeader(paper) {
    const counts = paper.counts || {};
    const titleEl = $("paperTitle");
    const metaEl = $("paperMeta");
    const badgesEl = $("paperHeaderBadges");
    const topicEl = $("writerTopic");
    const pdfBtn = $("pdfEvidenceHeaderBtn");
    if (titleEl) titleEl.textContent = paper.title_zh || paper.title || "未命名文献";
    if (metaEl) {
        metaEl.innerHTML = [
            esc(paper.year || "-"),
            esc(paper.journal || "-"),
            esc(paperTypeLabel(paper.paper_type)),
            renderDoiMeta(paper.doi)
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
            (paper.serial_number ? '<span class="serial-chip">' + formatSerialNumber(paper.serial_number) + "</span>" : "") +
            paperStatusChip(paper) +
            badge(counts.sections) +
            badge(counts.figures) +
            badge(counts.dft_results) +
            badge(counts.mechanism_claims) +
            badge(counts.writing_cards);
    }
    if (topicEl) topicEl.value = paper.title_zh || paper.title || "";
}

function renderListBlock(title, items, formatter) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    return items.map(function(item, index) {
        return '<div class="section-card"><h3>' + esc(title) + " " + (items.length > 1 ? (index + 1) : "") + '</h3>' + formatter(item) + "</div>";
    }).join("");
}

function renderJSONCards(title, items) {
    return renderReadableCards(title, items);
}

function compactText(value) {
    return String(value || "").replace(/s+/g, " ").trim();
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
        external_analysis_candidate: { label: "外部 AI 导入", tip: "来自网页 AI 或外部模型导入，必须再核对。" },
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
        external_ai_import_unverified: "外部导入待核对",
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
    return '<button class="btn ghost small" type="button" onclick="openPdfViewer(\'' +
        escAttr(item.paper_id) + '\', ' + page + ', false, null, \'exact_page\', \'' + escAttr(evidencePreview) +
        '\')">\u67e5\u770b\u539f\u9875</button>';
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
    return '<section class="section-card knowledge-group-section">' +
        '<div class="knowledge-group-head">' +
            '<h3>' + esc(groupName) + '</h3>' +
            '<span class="status-chip">' + items.length + ' 条</span>' +
        '</div>' +
        '<div class="knowledge-group-list">' + items.map(renderKnowledgeCandidateCard).join("") + '</div>' +
    '</section>';
}

function renderKnowledgeContext(detail) {
    const knowledge = detail.knowledge_context || {};
    const candidates = knowledge.candidates || [];
    if (!candidates.length) {
        return '<div class="section-card"><h3>知识候选</h3><div class="muted">暂无知识候选。可先导入网页 AI 解析，或重新解析文献。</div></div>';
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
    return '<div class="section-card localized-summary-card">' +
        '<h3>' + esc("\u4e2d\u6587\u9898\u76ee\u4e0e\u6458\u8981") + '</h3>' +
        (titleZh ? '<div class="localized-title">' + esc(titleZh) + '</div>' : '') +
        (detail.title ? '<div class="subtle original-title">' + esc("\u82f1\u6587\u9898\u76ee\uff1a") + esc(detail.title) + '</div>' : '') +
        (abstractZh ? '<h4>' + esc("\u4e2d\u6587\u6458\u8981") + '</h4><div class="prewrap">' + esc(abstractZh) + '</div>' : '') +
        (detail.abstract ? '<details class="original-abstract"><summary>' + esc("\u67e5\u770b\u82f1\u6587\u6458\u8981") + '</summary><div class="prewrap">' + esc(detail.abstract) + '</div></details>' : '') +
        '</div>';
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
        const value = readableValue(item ? item[key] : null);
        if (value && value !== "-") {
            fields.push('<div class="readable-field"><div class="k">' + esc(readableFieldLabel(key)) + '</div><div class="v">' + esc(value) + '</div></div>');
        }
    });
    return fields.length ? '<div class="readable-grid">' + fields.join("") + '</div>' : '<div class="muted">暂无可读字段。</div>';
}

const CODEX_ITEM_TYPE_BY_CARD_TITLE = {
    "DFT 设置": "dft_setting",
    "催化剂样本": "catalyst_sample",
    "DFT 结果": "dft_result",
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

function codexItemActionHtml(itemType, item) {
    if (!itemType || !item || !item.id) return "";
    return '<button class="btn ghost small" type="button" title="只复制此项、证据定位和邻近正文" onclick="copyCodexItem(\'' +
        escAttr(itemType) + '\', \'' + escAttr(item.id) + '\')">复制此项给 Codex</button>';
}

function dftBlockedReasonText(reasons) {
    return (Array.isArray(reasons) ? reasons : []).map(function(reason) {
        return DFT_BLOCK_REASON_LABELS[reason] || reason;
    }).join("、");
}

function renderDftItemSafety(item) {
    const safety = item && item.export_safety;
    if (!safety) return "";
    const exportable = safety.is_exportable === true || safety.eligible === true;
    const blockedReasons = Array.isArray(safety.blocked_reasons) ? safety.blocked_reasons : [];
    const reasons = dftBlockedReasonText(safety.blocked_reasons);
    const canVerify = !exportable && item && item.id && blockedReasons.includes("missing_review");
    return '<div class="figure-warning" style="margin-top:12px;">' +
        '<strong>' + (exportable ? "已通过 DFT 导出安全门" : "当前不可进入机器学习数据库") + '</strong>' +
        '<div>' + (exportable
            ? "该条记录已满足人工核验、证据原文和准确 PDF 定位要求。"
            : "阻断原因：" + (reasons || "待检查")) + '</div>' +
        (canVerify
            ? '<div style="margin-top:10px;"><button class="btn primary small" type="button" onclick="verifyDftResult(\'' +
                escAttr(item.id) + '\')">标记已核验</button></div>'
            : '') +
    '</div>';
}

function dftResultsWithSafety(detail) {
    const items = detail.dft_results_items || [];
    const readiness = detail.codex_context && detail.codex_context.dft_export_readiness;
    const safetyById = {};
    ((readiness && readiness.items) || []).forEach(function(item) {
        safetyById[String(item.record_id || "")] = item;
    });
    return items.map(function(item) {
        const safety = safetyById[String(item.id || "")];
        return safety ? Object.assign({}, item, { export_safety: safety }) : item;
    });
}

function renderDftExportReadiness(detail) {
    const readiness = detail.codex_context && detail.codex_context.dft_export_readiness;
    if (!readiness) return "";
    const reasons = Object.keys(readiness.blocked_reasons || {}).map(function(reason) {
        return (DFT_BLOCK_REASON_LABELS[reason] || reason) + " " + readiness.blocked_reasons[reason] + " 条";
    }).join("、");
    return '<div class="section-card figure-audit-note">' +
        '<h3>DFT 数据库导出安全状态</h3>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 10px;">' +
            '<span class="status-chip parsed">可导出 ' + Number(readiness.eligible_count || 0) + '</span>' +
            '<span class="status-chip meta">需处理 ' + Number(readiness.blocked_count || 0) + '</span>' +
            '<span class="status-chip">候选总数 ' + Number(readiness.total_candidates || 0) + '</span>' +
        '</div>' +
        '<div class="subtle">只有经过人工核验、具有证据原文且能准确定位到 PDF 页面的 DFT 记录才会进入导出和机器学习数据集。</div>' +
        (reasons ? '<div class="subtle" style="margin-top:6px;">当前阻断：' + esc(reasons) + '</div>' : '') +
    '</div>';
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
        const blocked = Array.isArray(item && item.blocked_reasons) && item.blocked_reasons.length
            ? '<div class="knowledge-detail-block"><div class="knowledge-detail-title">当前限制</div><div class="knowledge-detail-text">' + esc(item.blocked_reasons.join("、")) + '</div></div>'
            : "";
        return '<div class="section-card writing-card-compact">' +
            '<div class="knowledge-card-head">' +
                '<div><h3 style="margin:0;">写作卡片 ' + (items.length > 1 ? (index + 1) : "") + '</h3><div class="knowledge-card-use">适合用来组织引言、摘要和讨论的写作骨架</div></div>' +
                '<div class="knowledge-card-actions">' + action + '</div>' +
            '</div>' +
            '<div class="knowledge-tag-row">' +
                '<span class="status-chip meta">' + esc(paperTypeLabel(item && item.paper_type)) + '</span>' +
                '<span class="status-chip confidence-' + esc(review.className) + '" title="' + esc(review.tip) + '">' + esc(review.label) + '</span>' +
                '<span class="status-chip" title="当前证据链状态">' + esc(evidenceStatus) + '</span>' +
            '</div>' +
            '<div class="writing-card-summary-grid">' + (summaryBlocks || '<div class="muted">这张写作卡还没有生成可直接阅读的短摘要。</div>') + '</div>' +
            '<details class="knowledge-details">' +
                '<summary>展开写作逻辑与限制</summary>' +
                details +
                blocked +
            '</details>' +
        '</div>';
    }).join("");
}

function renderReadableCards(title, items) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    if (title === "写作卡片") {
        return renderWritingCardsCompact(items);
    }
    const keySets = {
        "DFT ??": ["software", "functional", "dispersion_correction", "pseudopotential", "cutoff_energy_ev", "cutoff_energy", "k_points", "convergence_settings", "vacuum_thickness_a", "vacuum_thickness"],
        "?????": ["name", "catalyst_type", "metal_centers", "coordination", "support", "synthesis_method", "evidence_text", "confidence"],
        "DFT ??": ["catalyst", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "evidence_text", "confidence"],
        "?????": ["sulfur_loading", "sulfur_content", "electrolyte_sulfur_ratio", "capacity", "cycle_number", "rate", "decay_per_cycle", "evidence_text", "confidence"],
        "????": ["claim_type", "claim_text", "key_species", "mechanism_direction", "evidence_text", "confidence"],
        "????": ["paper_type", "research_gap", "proposed_solution", "core_hypothesis", "evidence_text"],
        "??": ["caption", "page", "markdown_content"],
        "????": ["relationship_type", "target_title", "target_doi", "reason"],
        "????": ["relationship_type", "source_title", "source_doi", "reason"]
    };
    const keys = keySets[title] || Object.keys(items[0] || {}).filter(function(key) {
        return !["id", "paper_id", "raw_json", "created_at", "updated_at"].includes(key);
    }).slice(0, 10);
    return items.map(function(item, index) {
        const heading = title + (items.length > 1 ? " " + (index + 1) : "");
        const itemType = CODEX_ITEM_TYPE_BY_CARD_TITLE[title];
        const action = codexItemActionHtml(itemType, item);
        const safety = title === "DFT 结果" ? renderDftItemSafety(item) : "";
        return '<div class="section-card readable-card">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;"><h3 style="margin:0;">' + esc(heading) + '</h3>' + action + '</div>' +
            renderReadableFields(item || {}, keys) +
            safety +
        '</div>';
    }).join("");
}

function renderComprehensiveAnalysis(data) {
    if (!data || !Object.keys(data).length) {
        return '<div class="section-card"><h3>综合解析</h3><div class="muted">暂无综合解析。</div></div>';
    }
    const summary = data.layman_summary || {};
    const logic = data.writing_logic || {};
    return '<div class="section-card readable-card"><h3>综合解析</h3>' +
        renderReadableFields({
            one_sentence_takeaway: summary.one_sentence_takeaway,
            real_world_impact: summary.real_world_impact,
            research_gap: logic.research_gap_framing,
            core_hypothesis: logic.core_hypothesis,
            conclusion_mapping: logic.conclusion_mapping
        }, ["one_sentence_takeaway", "real_world_impact", "research_gap", "core_hypothesis", "conclusion_mapping"]) +
    '</div>';
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
        panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted">暂无可定位证据</div><div class="muted" style="margin-top:8px;">可能原因：未上传 PDF、尚未生成页码定位，或当前只有证据文本没有精确页码。</div></div>';
        return;
    }

    var html = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted" style="margin:6px 0 10px;">只有带精确页码的证据才能跳转到 PDF；如果这里只显示说明文字，表示当前还没有可直接跳转的定位。</div>';
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
                (loc.warning_reason ? '<span class="muted" style="color:var(--color-warning);">警告</span><span style="color:var(--color-warning);">' + esc(loc.warning_reason) + '</span>' : '') +
            '</div>' +
            '<div>' + locatorActionHtml(loc) + '</div>' +
        '</div>';
    });
    html += '</div>';
    panel.innerHTML = html;
}

async function loadEvidenceLocators(paperId) {
    var result = await fetchPaperEvidenceLocators(paperId);
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
        '<div style="font-size:11px;color:var(--color-text-secondary);">这里用于查看原文页和核对证据。浏览器 PDF 工具栏里的临时高亮/绘制不会写回系统；需要保存结论时，请在解析候选或人工确认工作台里保存。</div>' +
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

function renderDetail(detail, audit) {
    const counts = detail.counts || {};
    const summaryCards =
        '<div class="cards">' +
            '<div class="stat-card"><h3>章节</h3><div class="value">' + (counts.sections || 0) + "</div></div>" +
            '<div class="stat-card"><h3>表格</h3><div class="value">' + (counts.tables || 0) + "</div></div>" +
            '<div class="stat-card"><h3>图片</h3><div class="value">' + (counts.figures || 0) + "</div></div>" +
            '<div class="stat-card"><h3>DFT 结果</h3><div class="value">' + (counts.dft_results || 0) + "</div></div>" +
            '<div class="stat-card"><h3>机理</h3><div class="value">' + (counts.mechanism_claims || 0) + "</div></div>" +
            '<div class="stat-card"><h3>写作卡</h3><div class="value">' + (counts.writing_cards || 0) + "</div></div>" +
        "</div>";

    const baseInfo =
        '<div class="section-card"><h3>基础信息</h3>' +
            '<div class="inline-grid">' +
                '<div class="key-value"><div class="k">文献库</div><div class="v">' + esc(detail.library_name || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">文献类型</div><div class="v">' + esc(paperTypeLabel(detail.paper_type)) + (detail.type_confidence ? ' (置信度 ' + detail.type_confidence + ')' : '') + '</div></div>' +
                '<div class="key-value"><div class="k">分类来源</div><div class="v">' + esc(detail.classification_source || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">创建时间</div><div class="v">' + esc(formatDate(detail.created_at)) + '</div></div>' +
                '<div class="key-value"><div class="k">PDF 路径</div><div class="v">' + esc(detail.pdf_path || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">Markdown 路径</div><div class="v">' + esc(detail.markdown_path || "-") + '</div></div>' +
            "</div>" +
        "</div>";

    const abstractCard =
        '<div class="section-card"><h3>摘要</h3><div class="prewrap">' + esc(detail.abstract || "暂无摘要。") + "</div></div>";

    const localizedSummaryCard = renderLocalizedSummary(detail);
    const comprehensiveCard = renderComprehensiveAnalysis(detail.comprehensive_analysis || {});

    const sectionCards = renderListBlock("正文节选", detail.sections ? detail.sections.slice(0, 8) : [], function(item) {
        return (
            '<div class="subtle">标题：' + esc(item.section_title || item.section_type || "未命名章节") + "</div>" +
            '<div class="prewrap" style="margin-top:8px;">' + esc(ellipsis(item.text || "", 2200) || "暂无文本。") + "</div>"
        );
    });

    let figureCards = "";
    if (detail.figures && detail.figures.length) {
        const roles = new Set();
        detail.figures.forEach(f => {
            if (f.figure_role) roles.add(f.figure_role);
        });
        
        let filterHtml = "";
        if (roles.size > 0) {
            filterHtml += '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">';
            filterHtml += '<button class="btn small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display=\'block\')">全部</button>';
            roles.forEach(role => {
                filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.role === \'' + esc(role) + '\' ? \'block\' : \'none\')">' + esc(role) + '</button>';
            });
            filterHtml += '</div>';
        }
        
        function extractFigureNumber(caption) {
            if (!caption) return null;
            var m = caption.match(/(?:Figure|Fig\.?|Scheme)\s*(\d+)/i);
            return m ? parseInt(m[1], 10) : null;
        }

        const noisyCount = detail.figures.filter(isLikelyNoisyFigure).length;
        const cardsHtml = detail.figures.slice(0, 15).map(function(item, index) {
            let imgHtml = "";
            const noisyFigure = isLikelyNoisyFigure(item);
            if (item.image_path && !noisyFigure) {
                imgHtml = '<div style="margin-top: 12px; text-align: center;"><img src="/api/papers/assets/' + esc(item.image_path) + '" style="max-width: 100%; max-height: 400px; border: 1px solid var(--color-border); border-radius: var(--radius-sm); object-fit: contain;" alt="提取的文献图片" /></div>';
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
                metaHtml += '<span class="status-chip" style="background: var(--color-primary-bg); color: var(--color-text-secondary); margin-right: 8px;">' + esc(item.figure_role) + (item.role_confidence ? ' (' + (item.role_confidence*100).toFixed(0) + '%)' : '') + '</span>';
            }
            if (item.key_elements && item.key_elements.length) {
                item.key_elements.forEach(el => {
                    metaHtml += '<span class="status-chip meta" style="margin-right: 4px;">' + esc(el) + '</span>';
                });
            }
            if (metaHtml) {
                metaHtml = '<div style="margin-top: 8px;">' + metaHtml + '</div>';
            }

            let summaryHtml = "";
            if (item.content_summary) {
                summaryHtml = '<div class="subtle" style="margin-top: 8px; font-weight: 500;">' + esc(item.content_summary) + '</div>';
            }

            var figNum = extractFigureNumber(item.caption);
            var figLabel = figNum !== null ? '图片 ' + figNum : '图片 ' + (index + 1);
            var codexAction = codexItemActionHtml("figure", item);

            return '<div class="section-card figure-card" data-role="' + esc(item.figure_role || 'unknown') + '">' +
                   '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;"><h3 style="margin:0;">' + figLabel + '</h3>' + codexAction + '</div>' +
                   '<div class="prewrap">' + esc(item.caption || "无 caption") + "</div>" +
                   summaryHtml + metaHtml + imgHtml + '</div>';
        }).join("");
        
        const figureNotice = noisyCount
            ? '<div class="section-card figure-audit-note"><h3>图表抽取提示</h3><div class="subtle">检测到 ' + noisyCount + ' 张自动截图疑似无效。AI 审阅会把这些当作抽取噪声处理，不再把出版社标志、CrossMark 或页眉图片当作科学图表。</div></div>'
            : "";
        figureCards = figureNotice + filterHtml + cardsHtml;
    } else {
        figureCards = '<div class="section-card"><h3>图片</h3><div class="muted">暂无内容。</div></div>';
    }

    const pdfEvidenceEntry =
        '<div class="section-card pdf-evidence-entry"><h3>PDF 证据定位</h3>' +
            '<p>当前版本只在有精确页码时跳转到 PDF 页，并显示证据信息。</p>' +
            '<p class="subtle">请使用标题右侧的“' + (paperHasPdf(detail) ? '查看 PDF / 证据定位' : 'PDF 未上传') + '”入口。</p>' +
        '</div>';

    const referenceCards = renderListBlock("参考文献", detail.references ? detail.references.slice(0, 20) : [], function(item) {
        return (
            '<div class="prewrap">' + esc(item.title || "未命名参考文献") + "</div>" +
            '<div class="subtle" style="margin-top:8px;">作者：' + esc(item.authors || "-") + " | DOI：" + esc(item.doi || "-") + "</div>" +
            (item.citation_context ? '<div class="mono" style="margin-top:8px;">' + esc(item.citation_context) + "</div>" : "")
        );
    });

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
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<a class="btn primary small" style="text-decoration: none; display: inline-flex; align-items: center;" href="/pages/external_analysis_workbench/index.html?paper_id=' + encodeURIComponent(detail.id) + '">立即核对 (去工作台)</a>' +
                '</div>' +
            '</div>';
        
        const reviewTabWarningHtml = 
            '<div id="reviewTabAuditWarning" class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<a class="btn primary small" style="text-decoration: none; display: inline-flex; align-items: center;" href="/pages/external_analysis_workbench/index.html?paper_id=' + encodeURIComponent(detail.id) + '">立即核对 (去工作台)</a>' +
                '</div>' +
            '</div>';
        if (reviewTabEl) {
            reviewTabEl.insertAdjacentHTML("afterbegin", reviewTabWarningHtml);
        }
    }
    
    if (summaryEl) {
        summaryEl.innerHTML =
            missingPdfBanner +
            auditBanner +
            summaryCards +
            baseInfo +
            pdfEvidenceEntry +
            localizedSummaryCard +
            abstractCard +
            comprehensiveCard;
    }
    if (sectionsEl) {
        sectionsEl.innerHTML =
            sectionCards +
            referenceCards +
            renderJSONCards("出向关系", detail.outgoing_relationships || []) +
            renderJSONCards("入向关系", detail.incoming_relationships || []);
    }
    if (figuresEl) {
        figuresEl.innerHTML =
            figureCards +
            renderJSONCards("表格", detail.tables || []);
    }
    if (dftEl) {
        dftEl.innerHTML =
            renderDftExportReadiness(detail) +
            renderJSONCards("DFT 设置", detail.dft_settings_items || []) +
            renderJSONCards("催化剂样本", detail.catalyst_samples_items || []) +
            renderJSONCards("DFT 结果", dftResultsWithSafety(detail)) +
            renderJSONCards("电化学性能", detail.electrochemical_performance_items || []) +
            renderJSONCards("机理声明", detail.mechanism_claims_items || []);
    }
    if (writingEl) {
        writingEl.innerHTML =
            renderKnowledgeContext(detail) +
            renderJSONCards("写作卡片", detail.writing_cards_items || []);
    }
    if (translationEl) {
        translationEl.innerHTML = renderFullTranslation(detail);
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

async function loadPaperDetail(paperId) {
    if (!paperId) {
        showEmptyWorkspace();
        return;
    }
    const loadToken = Date.now() + ":" + paperId;
    state.detailLoadToken = loadToken;
    state.selectedPaperId = paperId;
    state.selectedPaperAudit = null;
    try {
        renderDetailSkeleton();
        const preview = state.papers.find(function(paper) { return paper.id === paperId; }) || state.selectedPaper;
        if (preview && preview.id === paperId) {
            renderWorkspaceHeader(preview);
            showWorkspace();
        }
        const detail = await fetchJSON(API_BASE + "/" + encodeURIComponent(paperId));
        if (state.detailLoadToken !== loadToken) return;
        state.selectedPaper = detail;
        renderPaperList();
        renderWorkspaceHeader(detail);
        renderDetail(detail, null);
        showWorkspace();
        syncQueryParams();
        loadPaperDetailEnrichment(paperId, loadToken);
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

function rerenderSelectedDetail(paperId) {
    if (state.selectedPaperId !== paperId || !state.selectedPaper) return;
    renderDetail(state.selectedPaper, state.selectedPaperAudit || null);
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

    fetchJSON(
        API_BASE + "/" + encodeURIComponent(paperId) +
        "/codex-context?max_sections=1&max_chars_per_section=300&max_figures=0&max_tables=0&max_candidates=30"
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
    window.open("/pages/literature_library/index.html?paper_id=" + encodeURIComponent(state.selectedPaperId) + "&tab=summary", "_blank");
}

function copyPaperIdentity() {
    if (!state.selectedPaper) return;
    const value = [state.selectedPaper.title || "", state.selectedPaper.doi || ""].filter(Boolean).join("\n");
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
        showToast("当前项目无法复制给 Codex。", "error");
        return;
    }
    try {
        showToast("正在生成 Codex 单项文献包...", "info");
        const data = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/codex-item/" + encodeURIComponent(itemType) + "/" + encodeURIComponent(itemId)
        );
        const value = data && data.markdown ? data.markdown : JSON.stringify(data, null, 2);
        await navigator.clipboard.writeText(value);
        showToast("此项及其证据已复制给 Codex。", "success");
    } catch (error) {
        showToast("Codex 单项文献包生成失败：" + error.message, "error");
    }
}

async function verifyDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法核验。", "error");
        return;
    }
    const ok = window.confirm("请确认你已经对照 PDF 原文、证据文本和定位检查过这条 DFT 数据。确认后，该记录会进入 DFT 导出安全门复核。");
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
                ? "DFT 记录已通过导出安全门。"
                : "DFT 核验已记录，但仍有安全门阻断项。",
            safety && safety.is_exportable ? "success" : "info"
        );
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("DFT 核验失败：" + error.message, "error");
    }
}
