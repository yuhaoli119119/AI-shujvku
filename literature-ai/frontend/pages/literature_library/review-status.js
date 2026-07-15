// Manual review progress and direct review-state renderers.
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
    const source = detail && (
        detail.manual_review_progress ||
        (detail.comprehensive_analysis && detail.comprehensive_analysis.manual_review_progress)
    );
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
    if (module === "figures" && chartReviewCompleted(detail)) return true;
    if (module === "figures" && figureReviewCompletionBlocked(detail)) return false;
    return !!(progress[module] && progress[module].completed);
}

function chartReviewExcludedFigures(detail) {
    const chartStatus = detail && detail.chart_review_status || {};
    return (chartStatus.excluded_duplicate_figures || []).filter(function(item) {
        return item && String(item.excluded_figure_id || "").trim();
    });
}

function chartReviewExcludedFigure(detail, item) {
    const figureId = String(item && item.id || "").trim();
    if (!figureId) return null;
    return chartReviewExcludedFigures(detail).find(function(exclusion) {
        return String(exclusion.excluded_figure_id || "").trim() === figureId;
    }) || null;
}

function chartReviewDuplicateReasonLabel(exclusion) {
    const reason = String(exclusion && exclusion.reason || "").trim();
    const labels = {
        same_page_same_image_reference: "同页同一图片引用",
        same_page_same_normalized_caption: "同页相同图注",
        same_page_highly_similar_caption: "同页高度相似图注",
        duplicate_scope_object: "重复的图表候选"
    };
    return labels[reason] || reason || "与正规图重复";
}

function chartReviewCoverage(detail) {
    const excludedFigureIds = new Set(chartReviewExcludedFigures(detail).map(function(item) {
        return String(item.excluded_figure_id || "").trim();
    }));
    const figures = (detail && detail.figures || []).filter(function(item) {
        return !excludedFigureIds.has(String(item && item.id || "").trim());
    });
    const mainFigureIds = new Set(figures.map(function(item) {
        return String(item && item.id || "").trim();
    }).filter(Boolean));
    const reviewedMainFigureIds = new Set(figures.filter(function(item) {
        return String(item && item.review_status || "").toLowerCase() === "reviewed" ||
            Number(item && item.object_review_audit_count || 0) > 0;
    }).map(function(item) { return String(item.id); }));
    const reviewedTableCount = (detail && detail.tables || []).filter(function(item) {
        const status = String(item && item.table_review_status || "").toLowerCase();
        return ["verified", "reviewed", "reviewed_empty_content"].includes(status) ||
            Number(item && item.object_review_audit_count || 0) > 0;
    }).length;
    return {
        mainFigureIds: mainFigureIds,
        reviewedMainFigureIds: reviewedMainFigureIds,
        mainFigureTotal: mainFigureIds.size,
        reviewedMainFigureCount: reviewedMainFigureIds.size,
        pendingMainFigureCount: Math.max(0, mainFigureIds.size - reviewedMainFigureIds.size),
        reviewedTableCount: reviewedTableCount
    };
}

function figureChartReviewStatusHtml(detail, item, coverage) {
    coverage = coverage || chartReviewCoverage(detail);
    const figureId = String(item && item.id || "").trim();
    if (chartReviewExcludedFigure(detail, item)) {
        return '<span class="status-chip subtle">重复候选，已从审核范围排除</span>';
    }
    if (!figureId || !coverage.mainFigureIds.has(figureId)) return "";
    return coverage.reviewedMainFigureIds.has(figureId)
        ? '<span class="status-chip ok">图表审核已完成</span>'
        : '<span class="status-chip warn">待补充图表审核</span>' +
          '<button class="btn primary small" type="button" onclick="event.stopPropagation(); copyFigureChartReviewPrompt(\'' + escAttr(figureId) + '\')">复制图表审核提示</button>';
}

function chartReviewCompleted(detail) {
    const coverage = chartReviewCoverage(detail);
    if (coverage.mainFigureTotal > 0) return coverage.pendingMainFigureCount === 0;
    return !!(manualReviewProgress(detail).figures || {}).completed;
}

function chartReviewScopeSummary(detail) {
    const coverage = chartReviewCoverage(detail);
    const lines = [];
    if (coverage.mainFigureTotal > 0) {
        lines.push("主文图片审核：已审核 " + coverage.reviewedMainFigureCount + "/" + coverage.mainFigureTotal + " 图" +
            (coverage.pendingMainFigureCount ? "；待补充 " + coverage.pendingMainFigureCount + " 图" : "；已完成"));
    }
    if (coverage.reviewedTableCount > 0) {
        lines.push("已审核表格：" + coverage.reviewedTableCount + " 表（可能含关联 SI）");
    }
    return lines.length ? '<div class="subtle" style="margin-top:8px;white-space:pre-line;">' + esc(lines.join("\n")) + '</div>' : "";
}

function figureReviewCompletionBlocked(detail) {
    const coverage = chartReviewCoverage(detail);
    if (coverage.mainFigureTotal > 0 && coverage.pendingMainFigureCount > 0) return true;
    if (detail && detail.figures_review_status === "risk") return true;
    return false;
}

function figureReviewBlockingNote(detail) {
    if (!figureReviewCompletionBlocked(detail)) return "";
    const coverage = chartReviewCoverage(detail);
    if (coverage.mainFigureTotal > 0 && coverage.pendingMainFigureCount > 0) {
        return '<div class="subtle" style="margin-top:6px;color:#b45309;">主文图片已审核 ' +
            esc(String(coverage.reviewedMainFigureCount)) + "/" + esc(String(coverage.mainFigureTotal)) +
            '，还有 ' + esc(String(coverage.pendingMainFigureCount)) + ' 张待审核。</div>';
    }
    return '<div class="subtle" style="margin-top:6px;color:#b45309;">当前图表存在风险项，需要完成审核后才能标记完成。</div>';
}

function renderManualReviewCompletionCard(detail, module, title, message) {
    const progress = manualReviewProgress(detail);
    const moduleProgress = progress[module] || {};
    const blocked = module === "figures" && figureReviewCompletionBlocked(detail);
    const chartCompleted = module === "figures" && chartReviewCompleted(detail);
    const status = (!!moduleProgress.completed || chartCompleted) && !blocked;
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
                : chartCompleted && !moduleProgress.completed
                    ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("当前图片和表格审核已闭环。") + '">已闭环</button>'
                : blocked
                    ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("仍有图表待审核或存在风险项。") + '">需先复核</button>'
                : '<button class="btn ' + (status ? 'ghost' : 'primary') + ' small" type="button" onclick="setManualReviewProgress(\'' + escAttr(module) + '\', ' + (status ? 'false' : 'true') + ')">' +
                    esc(status ? '取消人工浏览标记' : '人工浏览标记为已完成') +
                  '</button>') +
        '</div>' +
        '<div class="subtle">' + esc(message) + '</div>' +
        '<div class="status-chip warn" style="display:inline-flex;margin-top:8px;">人工浏览标记不等于审核通过</div>' +
        chartReviewScopeSummary(detail) +
        (module === "figures" ? figureReviewBlockingNote(detail) : "") +
        inheritedNote +
    '</div>';
}

function renderManualReviewCompletionControls(detail, module) {
    const progress = manualReviewProgress(detail);
    const moduleProgress = progress[module] || {};
    const blocked = module === "figures" && figureReviewCompletionBlocked(detail);
    const chartCompleted = module === "figures" && chartReviewCompleted(detail);
    const status = (!!moduleProgress.completed || chartCompleted) && !blocked;
    const inherited = !!moduleProgress.inherited;
    return '<span class="status-chip ' + (status ? 'ok' : 'subtle') + '">' + esc(status ? '已完成' : '未完成') + '</span>' +
        (inherited
            ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("该状态来自已绑定主文献。") + '">随主文献同步</button>'
            : chartCompleted && !moduleProgress.completed
                ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("当前图片和表格审核已闭环。") + '">已闭环</button>'
            : blocked
                ? '<button class="btn ghost small" type="button" disabled title="' + escAttr("仍有图表待审核或存在风险项。") + '">需先复核</button>'
            : '<button class="btn ' + (status ? 'ghost' : 'primary') + ' small" type="button" onclick="setManualReviewProgress(\'' + escAttr(module) + '\', ' + (status ? 'false' : 'true') + ')">' +
                esc(status ? '取消人工浏览标记' : '人工浏览标记为已完成') +
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
                    reviewer: "literature_library",
                    chart_run_id: module === "figures" && state.selectedPaper && state.selectedPaper.chart_review_status
                        ? (state.selectedPaper.chart_review_status.chart_run_id || null)
                        : null
                })
            }
        );
        if (state.selectedPaper) {
            state.selectedPaper.manual_review_progress = result.manual_review_progress || {};
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

function renderDetailReviewStatusPanel(detail) {
    const abstractStatus = contentReviewStatus(detail, "abstract_review_status");
    const sectionsStatus = contentReviewStatus(detail, "sections_review_status");
    const figuresStatus = contentReviewStatus(detail, "figures_review_status");
    const dftStatus = contentReviewStatus(detail, "dft_review_status");
    const progress = manualReviewProgress(detail);
    const contentReviewed = !!progress.content.completed || [abstractStatus, sectionsStatus].some(isAiVerifiedStatus);
    const figuresReviewed = !!progress.figures.completed || isAiVerifiedStatus(figuresStatus);
    const dftReviewed = dftStatus === "reviewed";
    const card = function(label, reviewed, pendingLabel, missing) {
        const stateLabel = missing ? "暂无数据" : (reviewed ? "已审核" : pendingLabel);
        const className = missing ? "none" : (reviewed ? "full" : "meta");
        return '<div class="stat-card" style="flex-direction:column;align-items:flex-start;gap:6px;min-width:0;">' +
            '<h3 style="margin:0;">' + esc(label) + '</h3>' +
            '<span class="status-chip ' + className + '">' + esc(stateLabel) + '</span>' +
        '</div>';
    };
    const counts = detail && detail.counts || {};
    return '<div class="section-card review-status-panel">' +
        '<h3>审核状态</h3>' +
        '<div class="cards" style="margin-top:12px;">' +
            card("内容", contentReviewed, "待审核", !detail.abstract && !Number(counts.sections || 0)) +
            card("图表", figuresReviewed, figuresStatus === "risk" ? "有风险" : "待审核", !Number(counts.figures || 0)) +
            card("DFT 数据", dftReviewed, dftStatus === "conflict" ? "有冲突" : "待审核", !Number(counts.dft_results || 0)) +
        '</div>' +
    '</div>';
}
