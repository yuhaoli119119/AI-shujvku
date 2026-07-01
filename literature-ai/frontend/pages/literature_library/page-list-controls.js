function formatSerialNumber(value) {
    if (value === null || value === undefined || value === "") return "";
    return "#" + String(value).padStart(3, "0");
}

const LIBRARY_FILTER_SESSION_KEY = "litai:literature-library:filters:v1";

function paperStatusChip(paper) {
    if (!paper) return '<span class="status-chip none">状态未知</span>';
    // 1. duplicate_candidate
    if (paper.oa_status === "duplicate_candidate" ||
        (paper.relationship_summary && (paper.relationship_summary["duplicate"] > 0 || paper.relationship_summary["duplicate_candidate"] > 0))) {
        return '<span class="status-chip duplicate">潜在重复</span>';
    }
    // 2. metadata_only / needs_upload / no pdf
    if (paper.oa_status === "metadata_only" || paper.oa_status === "needs_upload" || !paperHasPdf(paper)) {
        return '<span class="status-chip meta" style="background:var(--color-surface-alt); color:var(--color-text-secondary); border:1px solid var(--color-border);">无 PDF</span>';
    }
    // 3. extraction_failed
    if (paper.oa_status === "failed" || paper.oa_status === "extraction_failed" || paper.oa_status === "error") {
        return '<span class="status-chip failed">解析失败</span>';
    }
    // 4. parsed
    if (paperHasPdf(paper) && (paper.tei_path || paper.markdown_path || (paper.counts && paper.counts.sections > 0))) {
        return '<span class="status-chip parsed">已解析</span>';
    }
    // 5. pdf_available
    if (paperHasPdf(paper)) {
        return '<span class="status-chip pdf-available">PDF已上传</span>';
    }
    return '<span class="status-chip none">状态未知</span>';
}

function badge(count, title) {
    const safe = Number(count || 0);
    const titleAttr = title ? ' title="' + String(title).replace(/"/g, '&quot;') + '"' : '';
    return safe > 0
        ? '<span class="count-badge has"' + titleAttr + '>' + safe + "</span>"
        : '<span class="count-badge zero"' + titleAttr + '>0</span>';
}

function formatDate(value) {
    if (!value) return "-";
    try {
        let normalized = value;
        if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(value)) {
            normalized = value + "Z";
        }
        return new Date(normalized).toLocaleString("zh-CN");
    } catch (_) {
        return value;
    }
}

function ensureLibraryPaginationState() {
    if (!state.pagination || typeof state.pagination !== "object") {
        state.pagination = { page: 1, pageSize: PAGE_SIZE };
    }
    if (!Number.isFinite(Number(state.pagination.page)) || Number(state.pagination.page) < 1) {
        state.pagination.page = 1;
    }
    if (!Number.isFinite(Number(state.pagination.pageSize)) || Number(state.pagination.pageSize) < 1) {
        state.pagination.pageSize = PAGE_SIZE;
    }
}

function getLibraryPageSize() {
    ensureLibraryPaginationState();
    return Math.max(1, Number(state.pagination.pageSize || PAGE_SIZE));
}

function getLibraryCurrentPage() {
    ensureLibraryPaginationState();
    return Math.max(1, Math.floor(Number(state.pagination.page || 1)));
}

function syncLibraryOffset() {
    state.currentOffset = Math.max(0, (getLibraryCurrentPage() - 1) * getLibraryPageSize());
    return state.currentOffset;
}

function resetLibraryPagination() {
    ensureLibraryPaginationState();
    state.pagination.page = 1;
    state.currentOffset = 0;
}

function getLibraryTotalPages() {
    const total = Math.max(0, Number(state.currentLibraryTotal || 0));
    return Math.max(1, Math.ceil(total / getLibraryPageSize()));
}

function getFilters() {
    syncLibraryOffset();
    const params = new URLSearchParams();
    params.set("limit", getLibraryPageSize());
    params.set("offset", state.currentOffset);
    const libraryName = getCurrentLibraryName();
    const searchInput = $("searchInput");
    const filterYear = $("filterYear");
    const filterJournal = $("filterJournal");
    const filterPaperType = $("filterPaperType");
    const filterDFT = $("filterDFT");
    const filterWC = $("filterWC");
    const filterPdf = $("filterPdf");
    const filterSort = $("filterSort");
    const q = searchInput ? searchInput.value.trim() : "";
    const year = filterYear ? filterYear.value.trim() : "";
    const journal = filterJournal ? filterJournal.value.trim() : "";
    const paperType = filterPaperType ? filterPaperType.value : "";
    const dft = filterDFT ? filterDFT.value : "";
    const wc = filterWC ? filterWC.value : "";
    const pdf = filterPdf ? filterPdf.value : "";
    const sort = filterSort ? filterSort.value : "";
    if (libraryName) params.set("library_name", libraryName);
    if (q) params.set("q", q);
    if (year) params.set("year", year);
    if (journal) params.set("journal", journal);
    if (paperType) params.set("paper_type", paperType);
    if (dft !== "") params.set("has_dft_results", dft);
    if (wc !== "") params.set("has_writing_cards", wc);
    if (pdf !== "") params.set("has_pdf", pdf);
    if (sort === "paper_code_asc") {
        params.set("sort_by", "paper_code_numeric");
        params.set("sort_order", "asc");
    }
    return params;
}

function collectLibraryFilterState() {
    return {
        searchInput: $("searchInput") ? $("searchInput").value : "",
        filterYear: $("filterYear") ? $("filterYear").value : "",
        filterJournal: $("filterJournal") ? $("filterJournal").value : "",
        filterPaperType: $("filterPaperType") ? $("filterPaperType").value : "",
        filterDFT: $("filterDFT") ? $("filterDFT").value : "",
        filterWC: $("filterWC") ? $("filterWC").value : "",
        filterPdf: $("filterPdf") ? $("filterPdf").value : "",
        filterSort: $("filterSort") ? $("filterSort").value : "",
        pagination: {
            page: Number(state.pagination && state.pagination.page || 1),
            pageSize: Number(state.pagination && state.pagination.pageSize || PAGE_SIZE),
        },
    };
}

function saveLibraryFilterState() {
    try {
        window.sessionStorage.setItem(LIBRARY_FILTER_SESSION_KEY, JSON.stringify(collectLibraryFilterState()));
    } catch (_) {
        // sessionStorage can be unavailable in strict browser modes.
    }
}

function restoreLibraryFilterState() {
    try {
        const raw = window.sessionStorage.getItem(LIBRARY_FILTER_SESSION_KEY);
        if (!raw) return;
        const saved = JSON.parse(raw);
        if (!saved || typeof saved !== "object") return;
        [
            "searchInput",
            "filterYear",
            "filterJournal",
            "filterPaperType",
            "filterDFT",
            "filterWC",
            "filterPdf",
            "filterSort",
        ].forEach(function(id) {
            const el = $(id);
            if (el && Object.prototype.hasOwnProperty.call(saved, id)) {
                el.value = saved[id] == null ? "" : String(saved[id]);
            }
        });
        ensureLibraryPaginationState();
        const savedPage = Number(saved.pagination && saved.pagination.page);
        const savedPageSize = Number(saved.pagination && saved.pagination.pageSize);
        if (Number.isFinite(savedPage) && savedPage >= 1) {
            state.pagination.page = savedPage;
        }
        if (Number.isFinite(savedPageSize) && savedPageSize > 0) {
            state.pagination.pageSize = savedPageSize;
        }
        syncLibraryOffset();
    } catch (_) {
        // Ignore corrupted session state and continue with defaults.
    }
}

function clearStoredLibraryFilterState() {
    try {
        window.sessionStorage.removeItem(LIBRARY_FILTER_SESSION_KEY);
    } catch (_) {
        // sessionStorage can be unavailable in strict browser modes.
    }
}

function applyQueryParams() {
    const params = new URLSearchParams(window.location.search);
    const paperId = params.get("paper_id") || params.get("review_paper_id");
    const tab = params.get("tab");
    const rawTargetType = params.get("target_type") || "";
    const targetId = params.get("target_id") || "";
    const fieldName = params.get("field_name") || "";
    const pdfPage = Number(params.get("pdf_page") || 0);
    const pdfLocatorStatus = params.get("pdf_locator_status") || "";
    const pdfEvidenceText = params.get("pdf_evidence_text") || "";
    state.qualityReasonContext = params.get("quality_reason") || "";
    if (paperId) state.selectedPaperId = paperId;
    if (params.get("review_paper_id")) state.currentTab = "review";
    if (paperId && rawTargetType && targetId) {
        const typeMap = {
            section: { itemType: "section", tab: "sections" },
            sections: { itemType: "section", tab: "sections" },
            dft_setting: { itemType: "dft_setting", tab: "dft" },
            dft_settings: { itemType: "dft_setting", tab: "dft" },
            catalyst_sample: { itemType: "catalyst_sample", tab: "dft" },
            catalyst_samples: { itemType: "catalyst_sample", tab: "dft" },
            dft_results: { itemType: "dft_result", tab: "dft" },
            electrochemical_performance: { itemType: "electrochemical_performance", tab: "dft" },
            writing_card: { itemType: "writing_card", tab: "writing" },
            writing_cards: { itemType: "writing_card", tab: "writing" },
            mechanism_claim: { itemType: "mechanism_claim", tab: "dft" },
            mechanism_claims: { itemType: "mechanism_claim", tab: "dft" },
            table: { itemType: "table", tab: "figures" },
            tables: { itemType: "table", tab: "figures" },
            figure: { itemType: "figure", tab: "figures" },
            figures: { itemType: "figure", tab: "figures" },
        };
        const normalized = typeMap[rawTargetType];
        if (normalized) {
            state.pendingNavigationTarget = {
                paperId: paperId,
                rawTargetType: rawTargetType,
                itemType: normalized.itemType,
                tab: normalized.tab,
                targetId: targetId,
                fieldName: fieldName,
            };
        }
    }
    if (paperId && Number.isFinite(pdfPage) && pdfPage > 0) {
        state.pendingPdfJump = {
            paperId: paperId,
            page: pdfPage,
            locatorStatus: pdfLocatorStatus || "exact_page",
            evidenceText: pdfEvidenceText,
            opened: false,
        };
    }
    const tabMap = {
        detail: "summary",
        writer: "writing",
        aggregate: "dft",
        summary: "summary",
        sections: "sections",
        figures: "figures",
        dft: "dft",
        writing: "writing",
        translation: "translation",
        review: "review"
    };
    if (tab === "ai-search") {
        state.openAddOnLoad = "ai";
    } else if (tab && tabMap[tab]) {
        state.hasExplicitTab = true;
        state.currentTab = tabMap[tab];
    } else if (state.pendingNavigationTarget && state.pendingNavigationTarget.tab) {
        state.currentTab = state.pendingNavigationTarget.tab;
    }
}

function syncQueryParams() {
    if (location.protocol === "file:") return;
    const url = new URL(window.location.href);
    if (state.selectedPaperId) url.searchParams.set("paper_id", state.selectedPaperId);
    else url.searchParams.delete("paper_id");
    const requestedLibrary = url.searchParams.get("library_name") || "";
    if (state.currentLibrary && state.currentLibrary.name) url.searchParams.set("library_name", state.currentLibrary.name);
    else if (!requestedLibrary) url.searchParams.delete("library_name");
    url.searchParams.set("tab", state.currentTab);
    window.history.replaceState({}, "", url.toString());
}

function switchTab(tab) {
    if (!["summary", "sections", "figures", "dft", "writing", "translation", "review"].includes(tab)) {
        tab = "summary";
    }
    state.currentTab = tab;
    document.querySelectorAll(".tab-btn").forEach(function(btn) {
        btn.classList.toggle("active", btn.getAttribute("data-tab") === tab);
    });
    document.querySelectorAll("[data-nav-tab]").forEach(function(link) {
        link.classList.toggle("active", link.getAttribute("data-nav-tab") === tab);
    });
    document.querySelectorAll(".tab-panel").forEach(function(panel) {
        panel.classList.toggle("active", panel.id === "tab-" + tab);
    });
    if (tab !== "summary") {
        const locatorPanel = $("evidenceLocatorsPanel");
        if (locatorPanel) locatorPanel.innerHTML = "";
    }
    syncQueryParams();
    if (state.selectedPaper && typeof rerenderSelectedDetail === "function") {
        rerenderSelectedDetail(state.selectedPaperId);
        if (tab === "dft" && typeof decorateDftReadinessPanel === "function") {
            decorateDftReadinessPanel(state.selectedPaper);
        }
    }
    if (state.selectedPaperId && typeof ensureFullPaperDetailForTab === "function") {
        ensureFullPaperDetailForTab(tab);
    }
    if (tab === "writing") {
        ensureWriterStatus();
        if (state.selectedPaperId && typeof loadPaperKnowledgeContext === "function") {
            loadPaperKnowledgeContext(state.selectedPaperId);
        }
    }
    if (tab === "review" && state.selectedPaperId) loadExternalRuns();
    if ((tab === "review" || tab === "dft") && state.selectedPaperId && typeof loadPaperDetailEnrichment === "function") {
        loadPaperDetailEnrichment(state.selectedPaperId, state.detailLoadToken);
    }
    if (tab === "review" && !state.selectedPaperId) loadAgentGuide();
}

function showEmptyWorkspace() {
    const emptyEl = $("workspaceEmpty");
    const bodyEl = $("workspaceBody");
    if (emptyEl) emptyEl.style.display = "flex";
    if (bodyEl) bodyEl.style.display = "none";
}

function showWorkspace() {
    const emptyEl = $("workspaceEmpty");
    const bodyEl = $("workspaceBody");
    if (emptyEl) emptyEl.style.display = "none";
    if (bodyEl) bodyEl.style.display = "block";
}

function renderLibraryEmptyState() {
    const emptyEl = $("workspaceEmpty");
    if (emptyEl) {
        emptyEl.innerHTML =
            '<div class="empty-state-card">' +
                '<h2>当前库还没有文献</h2>' +
                '<p>上传 PDF、输入 DOI / URL，或使用在线检索来建立你的第一个文献库。</p>' +
                '<div class="empty-actions">' +
                    '<button class="btn primary" onclick="openAddLiteraturePanel(\'pdf\')">添加文献</button>' +
                    '<button class="btn ghost" onclick="openAddLiteraturePanel(\'online\')">在线检索</button>' +
                '</div>' +
            '</div>';
    }
    showEmptyWorkspace();
}

function renderNoSelectionState() {
    const emptyEl = $("workspaceEmpty");
    if (emptyEl) {
        emptyEl.innerHTML =
            '<div class="empty-state-card">' +
                '<h2>选择一篇文献查看详情</h2>' +
                    '<p>左侧用于浏览、搜索和筛选文献。选中文献后，这里会显示摘要、文字审核、图表、候选 DFT 数据、写作卡和 IDE AI 回写结果。</p>' +
            '</div>';
    }
    showEmptyWorkspace();
}

function normalizePaperListResponse(data) {
    if (Array.isArray(data)) return { papers: data, total: null };
    if (!data || typeof data !== "object") return { papers: [], total: null };
    const papers = Array.isArray(data.papers) ? data.papers : (Array.isArray(data.items) ? data.items : []);
    const total = Number(data.total ?? data.total_count ?? data.count ?? NaN);
    return { papers: papers, total: Number.isFinite(total) ? total : null };
}

function updatePager() {
    const metaEl = $("paginationMeta");
    const barEl = $("paginationBar");
    if (!metaEl || !barEl) return;
    const currentPage = getLibraryCurrentPage();
    const pageSize = getLibraryPageSize();
    const totalPages = getLibraryTotalPages();
    const shown = state.papers.length;
    const total = Number(state.currentLibraryTotal || 0);
    const rangeStart = shown ? state.currentOffset + 1 : 0;
    const rangeEnd = shown ? (state.currentOffset + shown) : 0;
    const metaParts = [];
    metaParts.push(total ? ("当前页 " + rangeStart + "-" + rangeEnd + " / " + total + " 篇") : ("当前页 " + shown + " 篇"));
    metaParts.push("第 " + currentPage + " / " + totalPages + " 页");
    metaParts.push("总计 " + total + " 篇");
    metaEl.textContent = metaParts.join(" | ");

    const startPage = Math.max(1, currentPage - 2);
    const endPage = Math.min(totalPages, currentPage + 2);
    const pages = [];
    for (let page = startPage; page <= endPage; page += 1) {
        pages.push(
            '<button class="page-indicator' + (page === currentPage ? ' is-active' : '') + '" type="button" onclick="goToLibraryPage(' + page + ')"' +
            (page === currentPage ? ' aria-current="page"' : '') +
            '>' + page + '</button>'
        );
    }
    barEl.innerHTML =
        '<span class="page-size-box">每页' +
            '<select id="paperPageSizeSelect" onchange="setLibraryPageSize()">' +
                '<option value="25"' + (pageSize === 25 ? ' selected' : '') + '>25 篇</option>' +
                '<option value="50"' + (pageSize === 50 ? ' selected' : '') + '>50 篇</option>' +
                '<option value="100"' + (pageSize === 100 ? ' selected' : '') + '>100 篇</option>' +
            '</select>' +
        '</span>' +
        '<button class="btn ghost small" type="button" onclick="goToLibraryPage(1)"' + (currentPage <= 1 ? ' disabled' : '') + '>首页</button>' +
        '<button class="btn ghost small" type="button" onclick="changeLibraryPage(-1)"' + (currentPage <= 1 ? ' disabled' : '') + '>上一页</button>' +
        '<span class="pagination-pages">' + pages.join("") + '</span>' +
        '<button class="btn ghost small" type="button" onclick="changeLibraryPage(1)"' + (currentPage >= totalPages ? ' disabled' : '') + '>下一页</button>' +
        '<button class="btn ghost small" type="button" onclick="goToLibraryPage(' + totalPages + ')"' + (currentPage >= totalPages ? ' disabled' : '') + '>末页</button>';
}

function setPaperListLoading(isLoading, message) {
    const container = $("paperList");
    const metaEl = $("paginationMeta");
    if (container) {
        container.style.opacity = isLoading ? "0.68" : "";
        container.style.pointerEvents = isLoading ? "none" : "";
        container.setAttribute("aria-busy", isLoading ? "true" : "false");
    }
    if (metaEl && isLoading && message) {
        metaEl.textContent = message;
    }
}

function goToLibraryPage(page) {
    ensureLibraryPaginationState();
    const totalPages = getLibraryTotalPages();
    const safePage = Math.max(1, Math.min(totalPages, Number(page || 1)));
    state.pagination.page = safePage;
    syncLibraryOffset();
    saveLibraryFilterState();
    fetchPapers({ preserveList: true, preserveDetail: true, loadingMessage: "正在切换分页..." });
}

function changeLibraryPage(delta) {
    goToLibraryPage(getLibraryCurrentPage() + Number(delta || 0));
}

function setLibraryPageSize() {
    const select = $("paperPageSizeSelect");
    const nextSize = Number(select && select.value ? select.value : getLibraryPageSize());
    ensureLibraryPaginationState();
    state.pagination.pageSize = Number.isFinite(nextSize) && nextSize > 0 ? nextSize : PAGE_SIZE;
    state.pagination.page = 1;
    syncLibraryOffset();
    saveLibraryFilterState();
    fetchPapers({ preserveList: true, preserveDetail: true, loadingMessage: "正在刷新列表..." });
}

async function fetchPapers(options) {
    const opts = options || {};
    const preserveList = opts.preserveList === true;
    const preserveDetail = opts.preserveDetail === true;
    const loadingMessage = opts.loadingMessage || "正在刷新列表...";
    const requestSeq = (state.paperListRequestSeq || 0) + 1;
    state.paperListRequestSeq = requestSeq;
    const requestLibrary = getCurrentLibraryName();
    try {
        ensureLibraryPaginationState();
        syncLibraryOffset();
        if (!preserveList || !state.papers.length) {
            renderPaperListSkeleton();
        } else {
            setPaperListLoading(true, loadingMessage);
        }
        const params = getFilters();
        const requestedOffset = state.currentOffset;
        const data = await fetchJSON(API_BASE + "/?" + params.toString());
        if (requestSeq !== state.paperListRequestSeq || requestLibrary !== getCurrentLibraryName()) {
            return;
        }
        const normalized = normalizePaperListResponse(data);
        if (requestLibrary) {
            const mismatched = normalized.papers.filter(function(paper) {
                return paper && paper.library_name && paper.library_name !== requestLibrary;
            });
            if (mismatched.length) {
                throw new Error("文献列表返回了非当前库记录，已拒绝渲染：" + mismatched.length + " 条");
            }
        }
        state.papers = normalized.papers;
        if (normalized.total !== null) {
            state.currentLibraryTotal = normalized.total;
        } else if (state.currentOffset === 0 && state.papers.length > state.currentLibraryTotal) {
            state.currentLibraryTotal = state.papers.length;
        }
        if (state.currentLibraryTotal > 0 && requestedOffset >= state.currentLibraryTotal) {
            state.pagination.page = getLibraryTotalPages();
            syncLibraryOffset();
            if (state.currentOffset !== requestedOffset) {
                return fetchPapers(opts);
            }
        } else if (state.currentLibraryTotal === 0) {
            resetLibraryPagination();
        }
        renderPaperList();
        updatePager();

        if (state.selectedPaperId) {
            const resolvedSelectedPaper = resolvePaperFromState(state.selectedPaperId);
            if (resolvedSelectedPaper) {
                const resolvedStableId = stablePaperIdOf(resolvedSelectedPaper);
                if (resolvedStableId && resolvedStableId !== String(state.selectedPaperId)) {
                    state.selectedPaperId = resolvedStableId;
                }
                state.selectedPaper = resolvedSelectedPaper;
            }
            const selectedStillListed = !!resolvePaperFromState(state.selectedPaperId);
            const selectedStableId = state.selectedPaper && (state.selectedPaper.paper_id || state.selectedPaper.id);
            if (selectedStableId && String(selectedStableId) === String(state.selectedPaperId)) {
                if (!preserveDetail) {
                    renderWorkspaceHeader(state.selectedPaper);
                    renderDetail(state.selectedPaper, state.selectedPaperAudit || null);
                    showWorkspace();
                }
            } else {
                try {
                    await loadPaperDetail(state.selectedPaperId);
                } catch (detailError) {
                    console.warn("loadPaperDetail for selected paper failed", detailError);
                    if (selectedStillListed) {
                        throw detailError;
                    }
                    state.selectedPaperId = null;
                    state.selectedPaper = null;
                    renderNoSelectionState();
                }
            }
        } else if (!state.papers.length && state.currentLibraryTotal === 0) {
            renderLibraryEmptyState();
        } else {
            renderNoSelectionState();
        }
        setPaperListLoading(false);
    } catch (error) {
        if (requestSeq !== state.paperListRequestSeq || requestLibrary !== getCurrentLibraryName()) {
            return;
        }
        setPaperListLoading(false);
        state.papers = [];
        const container = $("paperList");
        if (container) container.innerHTML = '<div class="list-empty error">文献列表加载失败：' + esc(error.message) + "</div>";
        updatePager();
        showToast("文献列表加载失败：" + error.message, "error");
    }
}

function searchLocal() {
    if (state.paperFilterTimer) {
        clearTimeout(state.paperFilterTimer);
        state.paperFilterTimer = 0;
    }
    resetLibraryPagination();
    saveLibraryFilterState();
    fetchPapers({ preserveList: true, preserveDetail: true, loadingMessage: "正在筛选文献..." });
}

function scheduleFilterSearch(delayMs) {
    if (state.paperFilterTimer) {
        clearTimeout(state.paperFilterTimer);
    }
    state.paperFilterTimer = window.setTimeout(function() {
        state.paperFilterTimer = 0;
        searchLocal();
    }, Math.max(0, Number(delayMs || 0)));
}

async function refreshCurrentPage() {
    await loadLibraries();
    await fetchPapers();
    if (typeof initSSE === "function") {
        initSSE();
    }
}

async function refreshLibraryData(options) {
    const opts = options || {};
    if (opts.reloadLibraries === true) {
        await loadLibraries();
    }
    await fetchPapers({
        preserveList: true,
        preserveDetail: opts.preserveDetail !== false,
        loadingMessage: opts.loadingMessage || "正在同步数据库更新..."
    });
    if (opts.reinitSSE === true && typeof initSSE === "function") {
        initSSE();
    }
    if (opts.refreshSelectedDetail === true && state.selectedPaperId && typeof refreshSelectedPaperDetail === "function") {
        await refreshSelectedPaperDetail({
            reason: opts.reason || "database_sync",
            mode: opts.detailMode,
            forceRefresh: true,
            invalidateCache: true
        });
    }
}

function prevPage() {
    changeLibraryPage(-1);
}

function nextPage() {
    changeLibraryPage(1);
}

function clearFilters() {
    ["searchInput", "filterYear", "filterJournal", "filterPaperType", "filterDFT", "filterWC", "filterPdf", "filterSort"].forEach(function(id) {
        const el = $(id);
        if (el) el.value = "";
    });
    resetLibraryPagination();
    clearStoredLibraryFilterState();
    searchLocal();
}
