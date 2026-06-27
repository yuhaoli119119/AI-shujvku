// Manual review progress, trust state, and RAG quality renderers.
function contentReviewStatus(detail, key) {
    return (detail && detail[key]) || "missing";
}

function isSupplementaryRelationshipType(value) {
    return String(value || "").trim().toLowerCase() === "supplementary";
}

function normalizeManualReviewProgressValue(value) {
    if (value && typeof value === "object") {
        return {
            completed: !!value.completed,
            updated_at: value.updated_at || null,
            updated_by: value.updated_by || "",
            inherited: !!value.inherited,
            inherited_from_code: value.inherited_from_code || "",
            inherited_from_title: value.inherited_from_title || ""
        };
    }
    return {
        completed: !!value,
        updated_at: null,
        updated_by: "",
        inherited: false,
        inherited_from_code: "",
        inherited_from_title: ""
    };
}

function supplementaryMainReviewProgress(detail) {
    if (!detail || !isSupplementaryPaperType(detail.paper_type)) return null;
    const relationships = Array.isArray(detail.incoming_relationships) ? detail.incoming_relationships : [];
    for (let i = 0; i < relationships.length; i++) {
        const item = relationships[i] || {};
        const progress = item.related_manual_review_progress;
        if (isSupplementaryRelationshipType(item.relationship_type) && progress && typeof progress === "object") {
            return {
                progress: progress,
                code: item.related_paper_code || "",
                title: item.related_paper_title || ""
            };
        }
    }
    return null;
}

function manualReviewProgress(detail) {
    const source = detail && detail.comprehensive_analysis && detail.comprehensive_analysis.manual_review_progress;
    const progress = source && typeof source === "object" ? source : {};
    const mainProgress = supplementaryMainReviewProgress(detail);
    function normalize(module) {
        const own = normalizeManualReviewProgressValue(progress[module]);
        if (own.completed || !mainProgress) {
            return own;
        }
        const inherited = normalizeManualReviewProgressValue(mainProgress.progress[module]);
        if (!inherited.completed) {
            return own;
        }
        inherited.inherited = true;
        inherited.inherited_from_code = mainProgress.code;
        inherited.inherited_from_title = mainProgress.title;
        return inherited;
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
    const progress = manualReviewProgress(detail);
    const moduleProgress = progress[module] || {};
    const status = !!moduleProgress.completed;
    const inherited = !!moduleProgress.inherited;
    const sourceText = moduleProgress.inherited_from_code || moduleProgress.inherited_from_title || "主文献";
    const inheritedNote = inherited
        ? '<div class="subtle" style="margin-top:6px;">此 SI 的完成状态随主文献 ' + esc(sourceText) + ' 同步显示；如需取消，请在主文献详情页调整。</div>'
        : "";
    return '<div class="section-card figure-audit-note">' +
        '<h3>' + esc(title) + '</h3>' +
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 10px;">' +
            '<span class="status-chip ' + (status ? 'ok' : 'subtle') + '">' + esc(status ? '已完成' : '未完成') + '</span>' +
            (inherited
                ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("该状态来自已绑定主文献。") + '">随主文献同步</button>'
                : '<button class="btn ' + (status ? 'ghost' : 'primary') + ' small" type="button" onclick="setManualReviewProgress(\'' + escAttr(module) + '\', ' + (status ? 'false' : 'true') + ')">' +
                    esc(status ? '取消已完成' : '标记已完成') +
                  '</button>') +
        '</div>' +
        '<div class="subtle">' + esc(message) + '</div>' +
        inheritedNote +
    '</div>';
}

function renderManualReviewCompletionControls(detail, module) {
    const progress = manualReviewProgress(detail);
    const moduleProgress = progress[module] || {};
    const status = !!moduleProgress.completed;
    const inherited = !!moduleProgress.inherited;
    return '<span class="status-chip ' + (status ? 'ok' : 'subtle') + '">' + esc(status ? '已完成' : '未完成') + '</span>' +
        (inherited
            ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("该状态来自已绑定主文献。") + '">随主文献同步</button>'
            : '<button class="btn ' + (status ? 'ghost' : 'primary') + ' small" type="button" onclick="setManualReviewProgress(\'' + escAttr(module) + '\', ' + (status ? 'false' : 'true') + ')">' +
                esc(status ? '取消已完成' : '标记已完成') +
              '</button>');
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
        if (typeof fetchPapers === "function") {
            fetchPapers({
                preserveList: true,
                preserveDetail: true,
                loadingMessage: "正在同步支撑文献进度..."
            });
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
