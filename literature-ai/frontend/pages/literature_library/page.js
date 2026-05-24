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
    if (tab && ["detail", "writer", "review", "ai-search", "aggregate"].includes(tab)) {
        state.hasExplicitTab = true;
        state.currentTab = tab;
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
    if (!state.selectedPaperId) {
        renderTabLanding(tab);
    }
    syncQueryParams();
    if (tab === "writer") ensureWriterStatus();
    if (tab === "review" && state.selectedPaperId) loadExternalRuns();
    if (tab === "review" && !state.selectedPaperId) loadAgentGuide();
    if (tab === "aggregate") loadAggregate();
}

function showEmptyWorkspace() {
    $("workspaceEmpty").style.display = "flex";
    $("workspaceBody").style.display = "none";
}

function showWorkspace() {
    $("workspaceEmpty").style.display = "none";
    $("workspaceBody").style.display = "block";
}

function renderTabLanding(tab) {
    const landing = {
        detail: {
            title: "论文详情",
            meta: "先从左侧选择一篇文献，再查看结构化提取结果。",
        },
        writer: {
            title: "内部 AI 整理",
            meta: "这里用于让站内 AI 对单篇文献做归纳、补充和写作整理。",
        },
        review: {
            title: "外部 / IDE AI",
            meta: "这里用于导入外部 AI 审核结果，或查看 IDE / MCP 接入指南。",
        },
        "ai-search": {
            title: "AI 检索入库",
            meta: "这里可以直接做在线检索、AI 扩展检索，以及后台批量收录。",
        },
        aggregate: {
            title: "聚合视图",
            meta: "这里查看跨文献的 DFT、催化剂和别名聚合结果。",
        },
    };
    const current = landing[tab] || landing.detail;
    $("paperTitle").textContent = current.title;
    $("paperMeta").textContent = current.meta;
    $("paperHeaderBadges").innerHTML = "";
    if (tab === "detail") {
        $("detailContent").innerHTML = '<div class="workspace-empty">先在左侧选择一篇文献，再查看论文详情。</div>';
    }
    if (tab === "writer") {
        $("writerResult").innerHTML = '<div class="workspace-empty">先选择一篇文献，然后让内部 AI 对该文献继续整理和补充。</div>';
    }
    if (tab === "review") {
        $("externalRuns").innerHTML = '<div class="workspace-empty">先选择一篇文献以导入审核结果；也可以先点上方按钮查看 IDE / MCP 接入指南。</div>';
    }
    if (tab === "ai-search" && !$("aiSearchResult").innerHTML.trim()) {
        $("aiSearchResult").innerHTML = '<div class="workspace-empty">输入关键词后即可开始在线检索或 AI 检索入库。</div>';
    }
    if (tab === "aggregate" && !$("aggregateResult").innerHTML.trim()) {
        $("aggregateResult").innerHTML = '<div class="workspace-empty">点击“刷新聚合”即可查看跨文献聚合结果。</div>';
    }
    showWorkspace();
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

window.addEventListener("beforeunload", disconnectSSE);
$("searchInput").addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });

applyQueryParams();
initProtocolWarning();
initSplitDrag();
TopNav.init({ currentPage: 'literature', mountId: 'topnav-mount' });
Promise.all([loadLibraries(), loadWriterSettings()]).then(function() {
    fetchPapers();
    initSSE();
    switchTab(state.currentTab);
});
