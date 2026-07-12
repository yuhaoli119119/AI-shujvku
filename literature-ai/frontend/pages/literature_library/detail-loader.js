// Paper detail caching, staged loading, and enrichment.
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
        manual_review_progress: source.manual_review_progress || {},
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

const DETAIL_LIGHT_VARIANT = "mode=light";
const DETAIL_FULL_VARIANT = "mode=full";
const DETAIL_CONTENT_VARIANT = "mode=content";
const DETAIL_DFT_VARIANT = "mode=dft";
const AUDIT_VARIANT = "reviews/audit";
const LOCATORS_VARIANT = "evidence/locators";
const KNOWLEDGE_CONTEXT_VARIANT = "max_candidates=24&max_chars_per_candidate=600";
const PAPER_RESOURCE_CACHE_LIMIT = 60;
const DFT_RESULTS_PAGE_SIZE = 100;
const DFT_RESULTS_FALLBACK_PAGE_SIZE = 50;

function formatPaperResourceFreshness(freshness) {
    if (!freshness || !freshness.updatedAt) return "";
    const date = new Date(freshness.updatedAt);
    if (Number.isNaN(date.getTime())) return "";
    const hh = String(date.getHours()).padStart(2, "0");
    const mm = String(date.getMinutes()).padStart(2, "0");
    const ss = String(date.getSeconds()).padStart(2, "0");
    return (freshness.fromCache ? "使用缓存" : "状态更新时间") + "：" + hh + ":" + mm + ":" + ss;
}

function paperResourceCacheKey(paperId, resourceType, variant) {
    return [String(paperId || ""), String(resourceType || ""), String(variant || "")].join("::");
}

function getPaperResourceCacheEntry(paperId, resourceType, variant) {
    const key = paperResourceCacheKey(paperId, resourceType, variant);
    return state.paperResourceCache && state.paperResourceCache[key] ? state.paperResourceCache[key] : null;
}

function getPaperResourceCachedValue(paperId, resourceType, variant) {
    const entry = getPaperResourceCacheEntry(paperId, resourceType, variant);
    return entry ? entry.value : null;
}

function setPaperResourceCacheEntry(paperId, resourceType, variant, value) {
    const key = paperResourceCacheKey(paperId, resourceType, variant);
    const updatedAt = Date.now();
    state.paperResourceCache = state.paperResourceCache || {};
    state.paperResourceCacheOrder = state.paperResourceCacheOrder || [];
    state.paperResourceCache[key] = {
        paperId: String(paperId || ""),
        resourceType: String(resourceType || ""),
        variant: String(variant || ""),
        updatedAt: updatedAt,
        value: value,
    };
    state.paperResourceCacheOrder = state.paperResourceCacheOrder.filter(function(item) { return item !== key; });
    state.paperResourceCacheOrder.push(key);
    while (state.paperResourceCacheOrder.length > PAPER_RESOURCE_CACHE_LIMIT) {
        const oldestKey = state.paperResourceCacheOrder.shift();
        if (!oldestKey) break;
        delete state.paperResourceCache[oldestKey];
    }
    return state.paperResourceCache[key];
}

function clearPaperResourceCaches(paperId) {
    const id = String(paperId || "");
    if (!id) return;
    state.paperResourceCache = state.paperResourceCache || {};
    state.paperResourceCacheOrder = state.paperResourceCacheOrder || [];
    Object.keys(state.paperResourceCache).forEach(function(key) {
        if (key.indexOf(id + "::") === 0) delete state.paperResourceCache[key];
    });
    state.paperResourceCacheOrder = state.paperResourceCacheOrder.filter(function(key) {
        return key.indexOf(id + "::") !== 0;
    });
    if (state.paperDetailCache) delete state.paperDetailCache[id];
    if (state.paperResourceFreshness) delete state.paperResourceFreshness[id];
}

function setPaperResourceFreshness(paperId, updatedAt, fromCache) {
    const id = String(paperId || "");
    if (!id || !updatedAt) return;
    state.paperResourceFreshness = state.paperResourceFreshness || {};
    state.paperResourceFreshness[id] = {
        updatedAt: updatedAt,
        fromCache: fromCache === true,
    };
}

function fetchPaperResource(paperId, resourceType, variant, url, options) {
    const opts = options || {};
    const key = paperResourceCacheKey(paperId, resourceType, variant);
    const cached = opts.forceRefresh ? null : getPaperResourceCacheEntry(paperId, resourceType, variant);
    if (cached) {
        return Promise.resolve({
            value: cached.value,
            updatedAt: cached.updatedAt,
            fromCache: true,
        });
    }
    state.paperResourceInflight = state.paperResourceInflight || {};
    if (state.paperResourceInflight[key]) {
        return state.paperResourceInflight[key];
    }
    const request = fetchJSON(url)
        .then(function(value) {
            const entry = setPaperResourceCacheEntry(paperId, resourceType, variant, value);
            return {
                value: entry.value,
                updatedAt: entry.updatedAt,
                fromCache: false,
            };
        })
        .finally(function() {
            if (state.paperResourceInflight) delete state.paperResourceInflight[key];
        });
    state.paperResourceInflight[key] = request;
    return request;
}

function mergeCachedPaperResources(detail, paperId) {
    if (!detail) return detail;
    const id = String(paperId || detail.id || "");
    const knowledgeContext = getPaperResourceCachedValue(id, "knowledge-context", KNOWLEDGE_CONTEXT_VARIANT);
    if (knowledgeContext && !detail.knowledge_context) {
        detail.knowledge_context = knowledgeContext;
    }
    return detail;
}

function cachedDetailForMode(paperId, detailMode) {
    const variants = {
        light: DETAIL_LIGHT_VARIANT,
        content: DETAIL_CONTENT_VARIANT,
        dft: DETAIL_DFT_VARIANT,
        full: DETAIL_FULL_VARIANT,
    };
    const exactVariant = variants[detailMode] || DETAIL_LIGHT_VARIANT;
    const exact = getPaperResourceCacheEntry(paperId, "detail", exactVariant);
    if (exact) return exact;
    const full = getPaperResourceCacheEntry(paperId, "detail", DETAIL_FULL_VARIANT);
    if (full) return full;
    if (detailMode === "light") {
        return getPaperResourceCacheEntry(paperId, "detail", DETAIL_CONTENT_VARIANT);
    }
    return null;
}

function syncSelectedPaperSupplementalFromCache(paperId) {
    const auditEntry = getPaperResourceCacheEntry(paperId, "reviews/audit", AUDIT_VARIANT);
    const locatorEntry = getPaperResourceCacheEntry(paperId, "evidence/locators", LOCATORS_VARIANT);
    state.selectedPaperAudit = auditEntry ? auditEntry.value : null;
    state.selectedPaperEvidenceLocators = locatorEntry ? locatorEntry.value : undefined;
    if (state.selectedPaper) {
        mergeCachedPaperResources(state.selectedPaper, paperId);
    }
}

function applySelectedPaperDetail(detail, options) {
    if (!detail) return;
    const opts = options || {};
    const paperId = String(detail.paper_id || detail.id || "");
    mergeCachedPaperResources(detail, paperId);
    state.selectedPaper = detail;
    if (opts.cache !== false) {
        cachePaperDetail(detail);
    }
    if (opts.updatedAt) {
        setPaperResourceFreshness(paperId, opts.updatedAt, opts.fromCache === true);
    }
    renderPaperList();
    renderWorkspaceHeader(detail);
    renderDetail(detail, state.selectedPaperAudit || null);
    showWorkspace();
}

function cachePaperDetail(detail) {
    if (!detail || !detail.id) return;
    state.paperDetailCache = state.paperDetailCache || {};
    state.paperDetailCache[detail.id] = detail;
    const variants = {
        light: DETAIL_LIGHT_VARIANT,
        content: DETAIL_CONTENT_VARIANT,
        dft: DETAIL_DFT_VARIANT,
        full: DETAIL_FULL_VARIANT,
    };
    setPaperResourceCacheEntry(
        detail.id,
        "detail",
        variants[detail._detailMode] || DETAIL_LIGHT_VARIANT,
        detail
    );
    const keys = Object.keys(state.paperDetailCache);
    if (keys.length > 3) {
        delete state.paperDetailCache[keys[0]];
    }
}

function detailModeForTab(tab) {
    if (tab === "summary") return "light";
    if (tab === "dft") return "dft";
    return "content";
}

function renderImmediatePaperDetail(paperId) {
    const cached = state.paperDetailCache && state.paperDetailCache[paperId];
    const preview = state.papers.find(function(paper) { return paper.id === paperId; }) || state.selectedPaper;
    const immediate = cached || (preview && preview.id === paperId ? buildPreviewDetail(preview) : null);
    if (!immediate) {
        renderDetailSkeleton();
        return false;
    }
    syncSelectedPaperSupplementalFromCache(paperId);
    const variant = {
        full: DETAIL_FULL_VARIANT,
        content: DETAIL_CONTENT_VARIANT,
        dft: DETAIL_DFT_VARIANT,
    }[immediate._detailMode] || DETAIL_LIGHT_VARIANT;
    const entry = immediate.id ? getPaperResourceCacheEntry(immediate.id, "detail", variant) : null;
    if (entry) {
        applySelectedPaperDetail(immediate, {
            updatedAt: entry.updatedAt,
            fromCache: true,
            cache: false,
        });
    } else {
        state.selectedPaper = immediate;
        renderPaperList();
        renderWorkspaceHeader(immediate);
        renderDetail(immediate, state.selectedPaperAudit || null);
        showWorkspace();
    }
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

async function fetchDftResultsPage(paperId, offset) {
    state.dftResultsPageSize = state.dftResultsPageSize || DFT_RESULTS_PAGE_SIZE;
    const urlForLimit = function(limit) {
        return API_BASE + "/" + encodeURIComponent(paperId) +
            "/dft-results?offset=" + offset + "&limit=" + limit;
    };
    try {
        return await fetchJSON(urlForLimit(state.dftResultsPageSize));
    } catch (error) {
        if (error && error.status === 422 && state.dftResultsPageSize > DFT_RESULTS_FALLBACK_PAGE_SIZE) {
            state.dftResultsPageSize = DFT_RESULTS_FALLBACK_PAGE_SIZE;
            return fetchJSON(urlForLimit(state.dftResultsPageSize));
        }
        throw error;
    }
}

async function loadPaperDetail(paperId, options) {
    if (!paperId) {
        showEmptyWorkspace();
        return;
    }
    const opts = options || {};
    const detailMode = opts.mode || "light";
    const resolvedPaperId = canonicalPaperId(paperId);
    const loadToken = Date.now() + ":" + resolvedPaperId;
    state.detailLoadToken = loadToken;
    state.selectedPaperId = resolvedPaperId;
    syncSelectedPaperSupplementalFromCache(resolvedPaperId);
    try {
        clearDeferredDetailPanels();
        const cachedDetailEntry = opts.forceRefresh ? null : cachedDetailForMode(resolvedPaperId, detailMode);
        if (cachedDetailEntry && cachedDetailEntry.value) {
            const cachedDetail = cachedDetailEntry.value;
            cachedDetail._detailMode = cachedDetail._detailMode || (cachedDetailEntry.variant === DETAIL_FULL_VARIANT ? "full" : detailMode);
            applySelectedPaperDetail(cachedDetail, {
                updatedAt: cachedDetailEntry.updatedAt,
                fromCache: true,
                cache: false,
            });
        } else {
            renderImmediatePaperDetail(paperId);
        }
        syncQueryParams();
        if (!cachedDetailEntry || opts.forceRefresh) {
            let detailResult = await fetchPaperResource(
                resolvedPaperId,
                "detail",
                {
                    full: DETAIL_FULL_VARIANT,
                    content: DETAIL_CONTENT_VARIANT,
                    dft: DETAIL_DFT_VARIANT,
                }[detailMode] || DETAIL_LIGHT_VARIANT,
                API_BASE + "/" + encodeURIComponent(resolvedPaperId) + "?mode=" + encodeURIComponent(detailMode),
                { forceRefresh: opts.forceRefresh === true }
            );
            if (state.detailLoadToken !== loadToken) return;
            let detail = detailResult.value;
            const detailStableId = stablePaperIdOf(detail);
            if (detailStableId) {
                state.selectedPaperId = detailStableId;
            }
            detail._detailMode = detailMode;
            applySelectedPaperDetail(detail, {
                updatedAt: detailResult.updatedAt,
                fromCache: detailResult.fromCache,
            });
        }
        applyPendingPdfJump(state.selectedPaperId);
        syncQueryParams();
        if (!opts.mode && detailMode !== "full" && detailModeForTab(state.currentTab) === "full") {
            window.setTimeout(function() {
                if (state.selectedPaperId === resolvedPaperId) {
                    ensurePaperDetailForTab(state.currentTab);
                }
            }, 0);
        }
        scheduleDetailEnrichment(state.selectedPaperId, loadToken);
        if (state.currentTab === "summary" || state.currentTab === "sections") {
            loadEvidenceLocators(state.selectedPaperId, { forceRefresh: opts.forceRefresh === true });
        }
        if (state.currentTab === "review") loadExternalRuns();
        if (state.currentTab === "aggregate") loadAggregate();
        if (state.currentTab === "writer") ensureWriterStatus();
    } catch (error) {
        if (state.detailLoadToken === loadToken) {
            showToast("详情加载失败：" + error.message, "error");
        }
    }
}

async function ensureCompleteSelectedDftResults() {
    const detail = state.selectedPaper;
    const paperId = String(state.selectedPaperId || "");
    if (!detail || !paperId || hasCompleteDftResults(detail)) return detail;
    state.dftResultsInflight = state.dftResultsInflight || {};
    state.dftResultsLoadErrors = state.dftResultsLoadErrors || {};
    if (state.dftResultsInflight[paperId]) {
        return state.dftResultsInflight[paperId];
    }
    delete state.dftResultsLoadErrors[paperId];
    const task = (async function() {
        let existing = Array.isArray(detail.dft_results_items) ? detail.dft_results_items.slice() : [];
        let page = detail.dft_results_page || {};
        let total = Number(page.total || (detail.counts && detail.counts.dft_results) || existing.length);
        let requestCount = 0;
        if (typeof updateDftPaginationControls === "function") {
            updateDftPaginationControls(detail);
        }
        while (page.has_more === true || existing.length < total) {
            requestCount += 1;
            if (requestCount > 100) {
                throw new Error("DFT 分页超过安全批次数。");
            }
            const nextPage = await fetchDftResultsPage(paperId, existing.length);
            total = Number(nextPage.total || total);
            const seen = new Set(existing.map(function(item) { return String(item && item.id || ""); }));
            const appended = (nextPage.items || []).filter(function(item) {
                return !seen.has(String(item && item.id || ""));
            });
            if (!appended.length && (nextPage.has_more === true || existing.length < total)) {
                throw new Error("DFT 分页未返回新的记录。");
            }
            existing = existing.concat(appended);
            page = {
                ...nextPage,
                offset: 0,
                returned: existing.length,
                total: total,
                has_more: nextPage.has_more === true || existing.length < total,
            };
            detail.dft_results_items = existing;
            detail.dft_results_page = page;
            if (state.selectedPaperId === paperId && typeof updateDftPaginationControls === "function") {
                updateDftPaginationControls(detail);
            }
        }
        detail.dft_results_page = {
            ...page,
            offset: 0,
            returned: existing.length,
            total: total,
            has_more: false,
        };
        cachePaperDetail(detail);
        return detail;
    })();
    state.dftResultsInflight[paperId] = task;
    if (typeof updateDftPaginationControls === "function" && updateDftPaginationControls(detail) === 0 && state.currentTab === "dft") {
        rerenderSelectedDetail(paperId);
    }
    try {
        return await task;
    } catch (error) {
        state.dftResultsLoadErrors[paperId] = error.message || String(error);
        showToast("完整 DFT 数据加载失败：" + (error.message || error), "error");
        return detail;
    } finally {
        delete state.dftResultsInflight[paperId];
        if (state.selectedPaperId === paperId) {
            rerenderSelectedDetail(paperId);
        }
    }
}

function ensurePaperDetailForTab(tab) {
    if (!state.selectedPaperId) return;
    const requiredMode = detailModeForTab(tab);
    const currentMode = state.selectedPaper && state.selectedPaper._detailMode;
    if (currentMode === requiredMode || currentMode === "full") {
        if (tab === "dft") ensureCompleteSelectedDftResults();
        return;
    }
    const loadingKey = state.selectedPaperId + ":" + requiredMode;
    if (state.detailViewLoadingFor === loadingKey) return;
    const paperId = state.selectedPaperId;
    state.detailViewLoadingFor = loadingKey;
    loadPaperDetail(paperId, { mode: requiredMode })
        .then(function() {
            if (tab === "dft" && state.selectedPaperId === paperId) {
                return ensureCompleteSelectedDftResults();
            }
            return null;
        })
        .finally(function() {
            if (state.detailViewLoadingFor === loadingKey) {
                state.detailViewLoadingFor = null;
            }
        });
}

function rerenderSelectedDetail(paperId) {
    if (state.selectedPaperId !== paperId || !state.selectedPaper) return;
    renderDetail(state.selectedPaper, state.selectedPaperAudit || null);
}

async function refreshSelectedPaperDetail(options) {
    if (!state.selectedPaperId) return null;
    const opts = options || {};
    const paperId = state.selectedPaperId;
    const shouldForce = opts.forceRefresh === true || opts.invalidateCache === true || Boolean(opts.mode);
    if (shouldForce) {
        clearPaperResourceCaches(paperId);
    }
    const mode = opts.mode || (state.selectedPaper && state.selectedPaper._detailMode) || detailModeForTab(state.currentTab);
    const refreshToken = Date.now() + ":refresh:" + paperId + ":" + (opts.reason || "detail");
    state.detailRefreshToken = refreshToken;
    const detailResult = await fetchPaperResource(
        paperId,
        "detail",
        {
            full: DETAIL_FULL_VARIANT,
            content: DETAIL_CONTENT_VARIANT,
            dft: DETAIL_DFT_VARIANT,
        }[mode] || DETAIL_LIGHT_VARIANT,
        API_BASE + "/" + encodeURIComponent(paperId) + "?mode=" + encodeURIComponent(mode),
        { forceRefresh: shouldForce }
    );
    const detail = detailResult.value;
    if (state.detailRefreshToken !== refreshToken || state.selectedPaperId !== paperId) {
        return null;
    }
    detail._detailMode = mode;
    applySelectedPaperDetail(detail, {
        updatedAt: detailResult.updatedAt,
        fromCache: detailResult.fromCache,
    });
    if (state.currentTab === "dft" && (mode === "dft" || mode === "full")) {
        await ensureCompleteSelectedDftResults();
    }
    await Promise.all([
        loadEvidenceLocators(paperId, { forceRefresh: shouldForce, silent: true }),
        loadPaperDetailEnrichment(paperId, state.detailLoadToken, { forceRefresh: shouldForce, silent: true }),
    ]);
    rerenderSelectedDetail(paperId);
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
        clearPaperResourceCaches(state.selectedPaperId);
        const detail = await refreshSelectedPaperDetail({
            reason: "header_manual_refresh",
            forceRefresh: true,
            invalidateCache: true,
        });
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

function loadPaperDetailEnrichment(paperId, loadToken, options) {
    const opts = options || {};
    const tasks = [];
    if (state.currentTab === "summary" || state.currentTab === "review") {
        tasks.push(
            fetchPaperResource(
                paperId,
                "reviews/audit",
                AUDIT_VARIANT,
                "/api/extraction/results/" + encodeURIComponent(paperId) + "/reviews/audit",
                { forceRefresh: opts.forceRefresh === true }
            )
                .then(function(result) {
                    if (state.detailLoadToken === loadToken && state.selectedPaperId === paperId) {
                        state.selectedPaperAudit = result.value;
                    }
                })
                .catch(function(e) {
                    if (!opts.silent) console.warn("Audit API is not available or failed:", e);
                })
        );
    }

    if (state.currentTab === "writing") {
        tasks.push(loadPaperKnowledgeContext(paperId, opts));
    }

    if (!tasks.length) return Promise.resolve([]);
    return Promise.all(tasks).then(function() {
        if (state.detailLoadToken === loadToken && state.selectedPaperId === paperId) {
            rerenderSelectedDetail(paperId);
        }
    });
}

function loadPaperKnowledgeContext(paperId, options) {
    if (!paperId || state.selectedPaperId !== paperId) return;
    const opts = options || {};
    if (state.selectedPaper && state.selectedPaper.knowledge_context && opts.forceRefresh !== true) return Promise.resolve(state.selectedPaper.knowledge_context);
    state.knowledgeContextLoadingFor = paperId;
    return fetchPaperResource(
        paperId,
        "knowledge-context",
        KNOWLEDGE_CONTEXT_VARIANT,
        API_BASE + "/" + encodeURIComponent(paperId) + "/knowledge-context?" + KNOWLEDGE_CONTEXT_VARIANT,
        { forceRefresh: opts.forceRefresh === true }
    )
        .then(function(result) {
            if (state.selectedPaperId === paperId && state.selectedPaper) {
                state.selectedPaper.knowledge_context = result.value;
                rerenderSelectedDetail(paperId);
            }
            return result.value;
        })
        .catch(function(e) {
            if (!opts.silent) console.warn("Knowledge context is not available:", e);
            throw e;
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
    const chartStatus = state.selectedPaper && state.selectedPaper.chart_review_status;
    const recommendedChartRunId = chartStatus && (
        chartStatus.selected_chart_run_id ||
        (chartStatus.primary_completed_run && chartStatus.primary_completed_run.chart_run_id)
    );
    if (recommendedChartRunId) {
        params.set("chart_run_id", recommendedChartRunId);
        params.set("run_id", recommendedChartRunId);
        params.set("mode", "evidence");
    }
    window.open("/pages/review_center/index.html?" + params.toString(), "_blank");
}
