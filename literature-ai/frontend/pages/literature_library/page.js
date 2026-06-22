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
    if (state.currentLibrary && state.currentLibrary.name) url.searchParams.set("library_name", state.currentLibrary.name);
    else url.searchParams.delete("library_name");
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
            const selectedStillListed = state.papers.some(function(paper) {
                const stablePaperId = paper && (paper.paper_id || paper.id);
                return stablePaperId && String(stablePaperId) === String(state.selectedPaperId);
            });
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
            mode: opts.detailMode
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

function updateRowSelectionUI() {
    document.querySelectorAll(".paper-row").forEach(function(row) {
        if (row.dataset.id === String(state.selectedPaperId)) {
            row.classList.add("active");
        } else {
            row.classList.remove("active");
        }
    });
}

function selectPaperById(paperId) {
    if (!paperId) return;
    if (state.selectedPaperId === paperId) {
        state.selectedPaperId = null;
        state.selectedPaper = null;
        updateRowSelectionUI();
        if (typeof loadPaperDetail === "function") loadPaperDetail(null);
        return;
    }
    state.selectedPaperId = paperId;
    state.selectedPaper = state.papers.find(function(paper) {
        const stablePaperId = paper && (paper.paper_id || paper.id);
        return stablePaperId && String(stablePaperId) === String(paperId);
    }) || state.selectedPaper;
    updateRowSelectionUI();
    if (typeof loadPaperDetail === "function") loadPaperDetail(paperId);
}

function openWorkspaceForPaper(paperId) {
    if (state.selectedPaperId !== paperId) {
        selectPaperById(paperId);
    }
    const layout = document.querySelector(".layout");
    if (layout && layout.classList.contains("hide-workspace")) {
        toggleWorkspace();
    }
}

function toggleDropdown(menuId, event) {
    if (event) event.stopPropagation();
    document.querySelectorAll(".dropdown-menu.open").forEach(function(menu) {
        if (menu.id !== menuId) menu.classList.remove("open");
    });
    const menu = $(menuId);
    if (menu) menu.classList.toggle("open");
}

function toggleAddLiteratureMenu(event) {
    toggleDropdown("addLiteratureMenu", event);
}

function togglePaperMoreMenu(event) {
    toggleDropdown("paperMoreMenu", event);
}

function ensureClassificationToolbarButton() {
    const toolbarRows = document.querySelectorAll(".toolbar .toolbar-row");
    const targetRow = toolbarRows && toolbarRows[2];
    if (!targetRow || targetRow.querySelector("[data-role='classify-unknown-btn']")) return;
    const searchBtn = Array.from(targetRow.querySelectorAll("button")).find(function(btn) {
        return btn.getAttribute("onclick") === "searchLocal()";
    });
    const button = document.createElement("button");
    button.className = "btn ghost";
    button.dataset.role = "classify-unknown-btn";
    button.textContent = "重分类未知类型";
    button.addEventListener("click", classifyUnknownTypes);
    if (searchBtn && searchBtn.nextSibling) {
        targetRow.insertBefore(button, searchBtn.nextSibling);
    } else {
        targetRow.appendChild(button);
    }
}

function closeDropdowns() {
    document.querySelectorAll(".dropdown-menu.open").forEach(function(menu) {
        menu.classList.remove("open");
    });
}

async function classifyUnknownTypes() {
    const libraryName = getCurrentLibraryName();
    if (!libraryName) {
        showToast("请先选择文献库。", "error");
        return;
    }
    try {
        showProgress("正在提交未知类型重分类任务...");
        const job = await fetchJSON(API_BASE + "/classify-batch/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                library_name: libraryName,
                overwrite: false,
                interval: 0,
                batch_size: 50
            })
        });
        const jobId = job && job.job_id ? String(job.job_id).slice(0, 8) : "queued";
        showToast("已发起重分类任务 #" + jobId, "success");
        hideProgress(true);
        setTimeout(function() { refreshCurrentPage(); }, 2000);
    } catch (error) {
        hideProgress(true);
        showToast("重分类提交失败：" + error.message, "error");
    }
}

function openAddLiteraturePanel(mode) {
    closeDropdowns();
    const dialog = $("addLiteratureDialog");
    if (dialog) dialog.style.display = "flex";
    switchAcquisitionMode(mode || "pdf");
    if (typeof loadMetadataOnlyPapers === "function") {
        loadMetadataOnlyPapers();
    }
}

function closeAddLiteraturePanel() {
    const dialog = $("addLiteratureDialog");
    if (dialog) dialog.style.display = "none";
}

function switchAcquisitionMode(mode) {
    const safeMode = ["pdf", "doi", "online", "ai", "folder"].includes(mode) ? mode : "pdf";
    document.querySelectorAll(".ingest-tab").forEach(function(btn) {
        btn.classList.toggle("active", btn.getAttribute("data-ingest-mode") === safeMode);
    });
    document.querySelectorAll(".acq-panel").forEach(function(panel) {
        panel.style.display = panel.id === "acq-" + safeMode ? "block" : "none";
    });
    const searchInput = $("searchInput");
    const searchValue = searchInput ? searchInput.value.trim() : "";
    const onlineQuery = $("onlineSearchQuery");
    if (safeMode === "online" && searchValue && onlineQuery && !onlineQuery.value.trim()) {
        onlineQuery.value = searchValue;
    }
    const aiQuery = $("aiSearchQuery");
    if (safeMode === "ai" && searchValue && aiQuery && !aiQuery.value.trim()) {
        aiQuery.value = searchValue;
    }
}

function addToEvidencePack() {
    closeDropdowns();
    switchTab("writing");
    showToast("已切到写作卡与整理区，可基于当前文献生成证据整理。", "info");
}

function openAggregateView() {
    closeDropdowns();
    switchTab("dft");
    loadAggregate();
}

function openSelectedPdfEvidence() {
    closeDropdowns();
    if (!state.selectedPaper) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    if (!paperHasPdf(state.selectedPaper)) {
        showToast("当前文献尚未上传 PDF，暂时无法预览，也不能执行基于 PDF 页码的证据跳转。", "error");
        return;
    }
    openPdfViewer(state.selectedPaper.id, 1, false, null, "exact_page", "这是从文献标题入口打开的 PDF 预览，不代表已定位到具体证据。请在“PDF 证据定位”卡片中使用可跳转页码的证据项。");
}

function openDeletePaperDialog(event) {
    if (event) event.stopPropagation();
    closeDropdowns();
    if (!state.selectedPaper) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const titleEl = $("deletePaperTitle");
    const doiEl = $("deletePaperDoi");
    const deletePdfEl = $("deletePaperPdfFiles");
    const deleteDerivedEl = $("deletePaperDerivedFiles");
    const info = primaryDoiInfo(state.selectedPaper.doi);
    if (titleEl) titleEl.textContent = state.selectedPaper.title || "未命名文献";
    if (doiEl) doiEl.textContent = info.doi || "无 DOI";
    if (deletePdfEl) deletePdfEl.checked = false;
    if (deleteDerivedEl) deleteDerivedEl.checked = false;
    const dialog = $("deletePaperDialog");
    if (dialog) dialog.style.display = "flex";
}

function closeDeletePaperDialog(event) {
    if (event) event.stopPropagation();
    const dialog = $("deletePaperDialog");
    if (dialog) dialog.style.display = "none";
}

async function confirmDeleteCurrentPaper(event) {
    if (event) event.stopPropagation();
    if (!state.selectedPaperId) return;
    try {
        const params = new URLSearchParams();
        if ($("deletePaperPdfFiles")?.checked) params.set("delete_pdf", "true");
        if ($("deletePaperDerivedFiles")?.checked) params.set("delete_derived", "true");
        const suffix = params.toString() ? "?" + params.toString() : "";
        const result = await fetchJSON(API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + suffix, { method: "DELETE" });
        closeDeletePaperDialog();
        const deletedFileCount = Array.isArray(result.deleted_files) ? result.deleted_files.length : 0;
        const fileMessage = deletedFileCount ? "，同时删除文件 " + deletedFileCount + " 个。" : "，文件未删除。";
        showToast("文献记录已删除" + fileMessage, "success");
        state.selectedPaperId = null;
        state.selectedPaper = null;
        await fetchPapers();
    } catch (error) {
        showToast("删除失败：" + error.message, "error");
    }
}

async function resetCurrentPaperUpload(event) {
    if (event) event.stopPropagation();
    closeDropdowns();
    if (!state.selectedPaper || !state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const paperCode = state.selectedPaper.paper_code || "";
    const title = state.selectedPaper.title || "未命名文献";
    const label = paperCode ? (paperCode + " " + title) : title;
    const confirmed = window.confirm(
        "这会保留当前文献条目和短号，但移除现有 PDF 及解析产物，并把它恢复成可重新上传 PDF 的状态。\n\n确认处理：\n" + label
    );
    if (!confirmed) return;
    try {
        const result = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/reset-upload?delete_pdf=true&delete_derived=true",
            { method: "POST" }
        );
        const deletedFileCount = Array.isArray(result.deleted_files) ? result.deleted_files.length : 0;
        showToast(
            "已保留文献条目并清空当前文件" + (deletedFileCount ? "，删除文件 " + deletedFileCount + " 个。" : "。"),
            "success"
        );
        await refreshCurrentPage();
    } catch (error) {
        showToast("重置失败：" + error.message, "error");
    }
}

async function showFolderImportGuide() {
    setAcquisitionResult('<div class="workspace-empty small-empty">正在读取 IDE AI 批量导入指南...</div>');
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        const entry = guide.recommended_entrypoint || {};
        const tools = guide.mcp && Array.isArray(guide.mcp.common_tools) ? guide.mcp.common_tools : [];
        setAcquisitionResult(
            '<div class="section-card"><h3>本地文件夹批量导入指南</h3>' +
            '<div class="subtle">批量扫描文件夹优先由 MCP 工具 <strong>scan_local_pdfs</strong> 和 <strong>ingest_pdf_batch</strong> 执行；如果当前会话未暴露 MCP 工具，可改用仓库内 <strong>literature-ai/backend</strong> 的 <strong>app.mcp.*</strong> 后备路径。网页端请先前往 Ingestion Center 处理常规上传。</div>' +
            '<div class="readable-grid" style="margin-top:12px;">' +
                '<div class="readable-field"><div class="k">推荐入口</div><div class="v">' + esc((entry.method || "") + " " + (entry.path || "")) + '</div></div>' +
                '<div class="readable-field"><div class="k">MCP 地址</div><div class="v">' + esc((guide.mcp && guide.mcp.url) || "/mcp") + '</div></div>' +
                '<div class="readable-field"><div class="k">常用工具</div><div class="v">' + esc(tools.join("、") || "scan_local_pdfs、ingest_pdf_batch") + '</div></div>' +
            "</div></div>"
        );
    } catch (error) {
        setAcquisitionResult('<div class="workspace-empty small-empty">指南读取失败：' + esc(error.message) + "</div>");
    }
}

function initSplitDrag() {
    const handle = $("dragHandle");
    if (!handle) return;

    const savedWidth = localStorage.getItem("sidebarWidth");
    if (savedWidth) {
        document.documentElement.style.setProperty("--sidebar-width", savedWidth + "px");
    }

    const MIN_W = 240, MAX_W = 600;
    let dragging = false, startX = 0, startWidth = 380, rafId = 0, newWidth = 0;

    function onStart(clientX) {
        dragging = true;
        startX = clientX;
        startWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-width")) || 380;
        document.body.classList.add("resizing");
        handle.classList.add("active");
    }

    function onMove(clientX) {
        if (!dragging) return;
        cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(function () {
            const delta = clientX - startX;
            newWidth = Math.min(MAX_W, Math.max(MIN_W, startWidth + delta));
            document.documentElement.style.setProperty("--sidebar-width", newWidth + "px");
        });
    }

    function onEnd() {
        if (!dragging) return;
        dragging = false;
        cancelAnimationFrame(rafId);
        document.body.classList.remove("resizing");
        handle.classList.remove("active");
        localStorage.setItem("sidebarWidth", newWidth || startWidth);
    }

    handle.addEventListener("mousedown", function (e) {
        e.preventDefault();
        onStart(e.clientX);
        window.addEventListener("mousemove", onMouseMove);
        window.addEventListener("mouseup", onMouseUp);
    });
    function onMouseMove(e) { onMove(e.clientX); }
    function onMouseUp() { onEnd(); window.removeEventListener("mousemove", onMouseMove); window.removeEventListener("mouseup", onMouseUp); }

    handle.addEventListener("touchstart", function (e) {
        e.preventDefault();
        onStart(e.touches[0].clientX);
    }, { passive: false });
    handle.addEventListener("touchmove", function (e) {
        e.preventDefault();
        onMove(e.touches[0].clientX);
    }, { passive: false });
    handle.addEventListener("touchend", onEnd);
    handle.addEventListener("touchcancel", onEnd);

    window.addEventListener("blur", onEnd);
}

async function findReachableHostedLiteraturePage() {
    const path = "/pages/literature_library/index.html";
    const candidates = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
    ];
    for (const base of candidates) {
        const controller = new AbortController();
        const timer = setTimeout(function() {
            controller.abort();
        }, 1200);
        try {
            await fetch(base + path, {
                method: "GET",
                mode: "no-cors",
                cache: "no-store",
                signal: controller.signal,
            });
            clearTimeout(timer);
            return base + path;
        } catch (_) {
            clearTimeout(timer);
        }
    }
    return null;
}

function initProtocolWarning() {
    if (location.protocol !== "file:") return;
    const warning = $("fileModeWarning");
    if (warning) {
        warning.style.display = "block";
        warning.innerHTML =
            '你当前是以本地文件方式直接打开页面，页面可能缺少样式或无法正常调用接口。<br>' +
            "正在尝试自动跳转到本地服务版本...";
    }
    findReachableHostedLiteraturePage().then(function(targetUrl) {
        if (!targetUrl) {
            if (warning) {
                warning.innerHTML =
                    '你当前是以本地文件方式直接打开页面。请改用 <code>http://127.0.0.1:8000/pages/literature_library/index.html</code> ' +
                    "或已启动的本地服务地址打开。";
            }
            return;
        }
        const suffix = (window.location.search || "") + (window.location.hash || "");
        window.location.replace(targetUrl + suffix);
    });
}

function initActionMenus() {
    document.querySelectorAll("[data-add-mode]").forEach(function(button) {
        button.addEventListener("click", function(event) {
            event.preventDefault();
            event.stopPropagation();
            openAddLiteraturePanel(button.getAttribute("data-add-mode"));
        });
    });
}

async function openMetadataDiagnostics() {
    const dialog = $("metadataDiagnosticsDialog");
    const container = $("metadataDiagnosticsContent");
    if (dialog) dialog.style.display = "flex";
    if (container) {
        container.innerHTML = '<div class="empty-state">正在加载报告...</div>';
        try {
            const data = await fetchJSON("/api/library/papers/metadata-diagnostics");
            renderMetadataDiagnostics(data, container);
        } catch (error) {
            container.innerHTML = `<div class="empty-state warning">加载失败：${esc(error.message)}</div>`;
        }
    }
}

function closeMetadataDiagnostics() {
    const dialog = $("metadataDiagnosticsDialog");
    if (dialog) dialog.style.display = "none";
}

function renderMetadataDiagnostics(data, container) {
    if (!data.items || data.items.length === 0) {
        container.innerHTML = '<div class="empty-state">当前没有任何文献缺少必须的元数据字段。</div>';
        return;
    }

    let html = `
        <div style="margin-bottom:16px;">
            <p><strong>需完善元数据的文献总数: ${data.total_papers_needing_metadata} 篇</strong></p>
            <div class="panel-card" style="border-color:var(--color-warning);">
                <span style="color:var(--color-warning);font-weight:700;">安全护栏说明:</span><br/>
                ${esc(data.safety_guardrails.message)}<br/>
                在线自动补全: ${data.safety_guardrails.auto_completion_enabled ? '允许' : '禁止'}<br/>
                安全等级自动提升: ${data.safety_guardrails.safety_upgrade_on_completion ? '允许' : '禁止'}
            </div>
        </div>
        <table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:14px;background:var(--color-surface);border:1px solid var(--color-border);border-radius:var(--radius);">
            <thead>
                <tr style="border-bottom:1px solid var(--color-border);background:var(--color-surface-alt);">
                    <th style="text-align:left;padding:10px;">文献标题</th>
                    <th style="text-align:left;padding:10px;">缺失字段</th>
                </tr>
            </thead>
            <tbody>
    `;
    const fieldNames = {
        "title": "标题",
        "authors": "作者",
        "journal": "期刊",
        "year": "年份",
        "DOI": "DOI",
        "impact factor": "影响因子"
    };

    data.items.forEach(item => {
        const missingList = item.missing_fields.map(m => {
            const zhName = fieldNames[m] || m;
            return `<span class="tag" style="background:var(--color-warning-bg);color:var(--color-warning);">${esc(zhName)}</span>`;
        }).join(" ");
        html += `
            <tr style="border-bottom:1px solid var(--color-border-subtle);">
                <td style="padding:10px;vertical-align:top;">${esc(item.title)}<div class="muted" style="margin-top:4px;">${esc(item.evidence_status_disclaimer)}</div></td>
                <td style="padding:10px;vertical-align:top;">${missingList}</td>
            </tr>
        `;
    });

    html += `</tbody></table>`;
    container.innerHTML = html;
}

function toggleDashboard() {
    const toolbar = $("mainToolbar");
    if (!toolbar) return;
    toolbar.classList.toggle("collapsed");
    const isCollapsed = toolbar.classList.contains("collapsed");
    const btn = $("dashboardToggleBtn");
    if (btn) btn.textContent = isCollapsed ? "展开面板" : "收起面板";
    localStorage.setItem("lit_lib_hide_dashboard", isCollapsed ? "1" : "0");
}

function toggleSidebar() {
    const layout = document.querySelector(".layout");
    if (!layout) return;
    layout.classList.toggle("hide-sidebar");
    const isHidden = layout.classList.contains("hide-sidebar");
    const btn = $("toggleSidebarBtn");
    if (btn) btn.textContent = isHidden ? "展开列表" : "隐藏列表";
    localStorage.setItem("lit_lib_hide_sidebar", isHidden ? "1" : "0");
}

function toggleWorkspace() {
    const layout = document.querySelector(".layout");
    if (!layout) return;
    layout.classList.toggle("hide-workspace");
    const isHidden = layout.classList.contains("hide-workspace");
    const btn = $("toggleWorkspaceBtn");
    if (btn) btn.textContent = isHidden ? "展开详情" : "隐藏详情";
    localStorage.setItem("lit_lib_hide_workspace", isHidden ? "1" : "0");
}

function initLayoutState() {
    if (localStorage.getItem("lit_lib_hide_dashboard") === "1") {
        const toolbar = $("mainToolbar");
        if (toolbar) toolbar.classList.add("collapsed");
        const btn = $("dashboardToggleBtn");
        if (btn) btn.textContent = "展开面板";
    }
    const layout = document.querySelector(".layout");
    if (!layout) return;
    if (localStorage.getItem("lit_lib_hide_sidebar") === "1") {
        layout.classList.add("hide-sidebar");
        const btn = $("toggleSidebarBtn");
        if (btn) btn.textContent = "展开列表";
    }
    if (localStorage.getItem("lit_lib_hide_workspace") === "1") {
        layout.classList.add("hide-workspace");
        const btn = $("toggleWorkspaceBtn");
        if (btn) btn.textContent = "展开详情";
    }
}

Object.assign(window, {
    openAddLiteraturePanel: openAddLiteraturePanel,
    closeAddLiteraturePanel: closeAddLiteraturePanel,
    switchAcquisitionMode: switchAcquisitionMode,
    toggleAddLiteratureMenu: toggleAddLiteratureMenu,
    togglePaperMoreMenu: togglePaperMoreMenu,
    addToEvidencePack: addToEvidencePack,
    openAggregateView: openAggregateView,
    openSelectedPdfEvidence: openSelectedPdfEvidence,
    openDeletePaperDialog: openDeletePaperDialog,
    resetCurrentPaperUpload: resetCurrentPaperUpload,
    closeDeletePaperDialog: closeDeletePaperDialog,
    confirmDeleteCurrentPaper: confirmDeleteCurrentPaper,
    classifyUnknownTypes: classifyUnknownTypes,
    showFolderImportGuide: showFolderImportGuide,
    switchTab: switchTab,
    openMetadataDiagnostics: openMetadataDiagnostics,
    closeMetadataDiagnostics: closeMetadataDiagnostics,
    toggleDashboard: toggleDashboard,
    toggleSidebar: toggleSidebar,
    toggleWorkspace: toggleWorkspace,
    fetchPapers: fetchPapers,
    searchLocal: searchLocal,
    refreshCurrentPage: refreshCurrentPage,
    refreshLibraryData: refreshLibraryData,
    resetLibraryPagination: resetLibraryPagination,
    goToLibraryPage: goToLibraryPage,
    changeLibraryPage: changeLibraryPage,
    setLibraryPageSize: setLibraryPageSize,
    prevPage: prevPage,
    nextPage: nextPage,
    clearFilters: clearFilters,
    selectPaperById: selectPaperById,
    openWorkspaceForPaper: openWorkspaceForPaper
});

window.addEventListener("beforeunload", disconnectSSE);
document.addEventListener("click", closeDropdowns);
document.addEventListener("click", function(event) {
    if (event.target.closest(".paper-row") || 
        event.target.closest(".workspace") ||
        event.target.closest(".sidebar") ||
        event.target.closest(".toolbar") ||
        event.target.closest(".topnav") ||
        event.target.closest("button") ||
        event.target.closest(".modal-overlay") ||
        event.target.closest("a")) {
        return;
    }
    if (state.selectedPaperId) {
        state.selectedPaperId = null;
        state.selectedPaper = null;
        renderPaperList();
        if (typeof loadPaperDetail === "function") loadPaperDetail(null);
    }
});
const searchInput = $("searchInput");
if (searchInput) {
    searchInput.addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });
}
["filterYear", "filterJournal"].forEach(function(id) {
    const el = $(id);
    if (el) el.addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });
});
["filterPaperType", "filterDFT", "filterWC", "filterPdf", "filterSort"].forEach(function(id) {
    const el = $(id);
    if (el) el.addEventListener("change", function() { scheduleFilterSearch(120); });
});

initLayoutState();
applyQueryParams();
restoreLibraryFilterState();
initProtocolWarning();
initSplitDrag();
initActionMenus();
ensureClassificationToolbarButton();
TopNav.init({ currentPage: 'literature', mountId: 'topnav-mount' });
loadLibraries().finally(function() {
    fetchPapers();
    initSSE();
});
switchTab(state.currentTab);
if (state.openAddOnLoad) {
    openAddLiteraturePanel(state.openAddOnLoad);
}
loadWriterSettings();
