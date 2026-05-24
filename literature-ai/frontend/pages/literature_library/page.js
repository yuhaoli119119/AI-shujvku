function formatSerialNumber(value) {
    if (value === null || value === undefined || value === "") return "";
    return "#" + String(value).padStart(3, "0");
}

function paperStatusChip(paper) {
    if (paper.oa_status === "metadata_only") {
        return '<span class="status-chip meta">仅元数据</span>';
    }
    if (paper.pdf_path) {
        return '<span class="status-chip full">已入库</span>';
    }
    return '<span class="status-chip none">状态未明</span>';
}

function badge(count) {
    const safe = Number(count || 0);
    return safe > 0
        ? '<span class="count-badge has">' + safe + "</span>"
        : '<span class="count-badge zero">0</span>';
}

function formatDate(value) {
    if (!value) return "-";
    try {
        return new Date(value).toLocaleString("zh-CN");
    } catch (_) {
        return value;
    }
}

function getFilters() {
    const params = new URLSearchParams();
    params.set("limit", PAGE_SIZE);
    params.set("offset", state.currentOffset);
    const libraryName = getCurrentLibraryName();
    const q = $("searchInput").value.trim();
    const year = $("filterYear").value.trim();
    const journal = $("filterJournal").value.trim();
    const paperType = $("filterPaperType").value;
    const dft = $("filterDFT").value;
    const wc = $("filterWC").value;
    if (libraryName) params.set("library_name", libraryName);
    if (q) params.set("q", q);
    if (year) params.set("year", year);
    if (journal) params.set("journal", journal);
    if (paperType) params.set("paper_type", paperType);
    if (dft !== "") params.set("has_dft_results", dft);
    if (wc !== "") params.set("has_writing_cards", wc);
    return params;
}

function applyQueryParams() {
    const params = new URLSearchParams(window.location.search);
    const paperId = params.get("paper_id") || params.get("review_paper_id");
    const tab = params.get("tab");
    if (paperId) state.selectedPaperId = paperId;
    if (params.get("review_paper_id")) state.currentTab = "review";
    const tabMap = {
        detail: "summary",
        writer: "writing",
        aggregate: "dft",
        summary: "summary",
        sections: "sections",
        figures: "figures",
        dft: "dft",
        writing: "writing",
        review: "review"
    };
    if (tab === "ai-search") {
        state.openAddOnLoad = "ai";
    } else if (tab && tabMap[tab]) {
        state.hasExplicitTab = true;
        state.currentTab = tabMap[tab];
    }
}

function syncQueryParams() {
    if (location.protocol === "file:") return;
    const url = new URL(window.location.href);
    if (state.selectedPaperId) url.searchParams.set("paper_id", state.selectedPaperId);
    else url.searchParams.delete("paper_id");
    url.searchParams.set("tab", state.currentTab);
    window.history.replaceState({}, "", url.toString());
}

function switchTab(tab) {
    if (!["summary", "sections", "figures", "dft", "writing", "review"].includes(tab)) {
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
    if (tab === "writing") ensureWriterStatus();
    if (tab === "review" && state.selectedPaperId) loadExternalRuns();
    if (tab === "review" && !state.selectedPaperId) loadAgentGuide();
}

function showEmptyWorkspace() {
    $("workspaceEmpty").style.display = "flex";
    $("workspaceBody").style.display = "none";
}

function showWorkspace() {
    $("workspaceEmpty").style.display = "none";
    $("workspaceBody").style.display = "block";
}

function renderLibraryEmptyState() {
    $("workspaceEmpty").innerHTML =
        '<div class="empty-state-card">' +
            '<h2>当前库还没有文献</h2>' +
            '<p>上传 PDF、输入 DOI / URL，或使用 AI 自动搜文献来建立你的第一个文献库。</p>' +
            '<div class="empty-actions">' +
                '<button class="btn primary" onclick="openAddLiteraturePanel(\'pdf\')">添加文献</button>' +
                '<button class="btn ghost" onclick="openAddLiteraturePanel(\'ai\')">AI 搜文献</button>' +
            '</div>' +
        '</div>';
    showEmptyWorkspace();
}

function renderNoSelectionState() {
    $("workspaceEmpty").innerHTML =
        '<div class="empty-state-card">' +
            '<h2>选择一篇文献查看详情</h2>' +
            '<p>左侧用于浏览、搜索和筛选文献。选中文献后，这里会显示摘要、章节、图表、DFT 数据、写作卡和 AI 审核。</p>' +
        '</div>';
    showEmptyWorkspace();
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

function closeDropdowns() {
    document.querySelectorAll(".dropdown-menu.open").forEach(function(menu) {
        menu.classList.remove("open");
    });
}

function openAddLiteraturePanel(mode) {
    closeDropdowns();
    $("addLiteratureDialog").style.display = "flex";
    switchAcquisitionMode(mode || "pdf");
}

function closeAddLiteraturePanel() {
    $("addLiteratureDialog").style.display = "none";
}

function switchAcquisitionMode(mode) {
    const safeMode = ["pdf", "doi", "online", "ai", "folder"].includes(mode) ? mode : "pdf";
    document.querySelectorAll(".ingest-tab").forEach(function(btn) {
        btn.classList.toggle("active", btn.getAttribute("data-ingest-mode") === safeMode);
    });
    document.querySelectorAll(".acq-panel").forEach(function(panel) {
        panel.style.display = panel.id === "acq-" + safeMode ? "block" : "none";
    });
    const searchValue = $("searchInput") ? $("searchInput").value.trim() : "";
    if (safeMode === "online" && searchValue && !$("onlineSearchQuery").value.trim()) {
        $("onlineSearchQuery").value = searchValue;
    }
    if (safeMode === "ai" && searchValue && !$("aiSearchQuery").value.trim()) {
        $("aiSearchQuery").value = searchValue;
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

async function showFolderImportGuide() {
    setAcquisitionResult('<div class="workspace-empty small-empty">正在读取 MCP 批量导入指南...</div>');
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        setAcquisitionResult(
            '<div class="section-card"><h3>本地文件夹批量导入指南</h3>' +
            '<div class="subtle">批量扫描文件夹由 MCP 工具 <strong>scan_local_pdfs</strong> 和 <strong>ingest_pdf_batch</strong> 执行；网页端请先前往 Ingestion Center 处理常规上传。</div>' +
            '<div class="mono" style="margin-top:12px;">' + esc(JSON.stringify(guide, null, 2)) + "</div></div>"
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

function initProtocolWarning() {
    if (location.protocol === "file:") {
        $("fileModeWarning").style.display = "block";
    }
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

Object.assign(window, {
    openAddLiteraturePanel: openAddLiteraturePanel,
    closeAddLiteraturePanel: closeAddLiteraturePanel,
    switchAcquisitionMode: switchAcquisitionMode,
    toggleAddLiteratureMenu: toggleAddLiteratureMenu,
    togglePaperMoreMenu: togglePaperMoreMenu,
    addToEvidencePack: addToEvidencePack,
    openAggregateView: openAggregateView,
    showFolderImportGuide: showFolderImportGuide,
    switchTab: switchTab
});

window.addEventListener("beforeunload", disconnectSSE);
document.addEventListener("click", closeDropdowns);
$("searchInput").addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });

applyQueryParams();
initProtocolWarning();
initSplitDrag();
initActionMenus();
TopNav.init({ currentPage: 'literature', mountId: 'topnav-mount' });
Promise.all([loadLibraries(), loadWriterSettings()]).then(function() {
    fetchPapers();
    initSSE();
    switchTab(state.currentTab);
    if (state.openAddOnLoad) {
        openAddLiteraturePanel(state.openAddOnLoad);
    }
});
