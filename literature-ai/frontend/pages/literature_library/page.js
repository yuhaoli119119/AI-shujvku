function formatSerialNumber(value) {
    if (value === null || value === undefined || value === "") return "";
    return "#" + String(value).padStart(3, "0");
}

function paperStatusChip(paper) {
    if (!paper) return '<span class="status-chip none">状态未知</span>';
    // 1. duplicate_candidate
    if (paper.oa_status === "duplicate_candidate" || 
        (paper.relationship_summary && (paper.relationship_summary["duplicate"] > 0 || paper.relationship_summary["duplicate_candidate"] > 0))) {
        return '<span class="status-chip duplicate">潜在重复</span>';
    }
    // 2. metadata_only / needs_upload
    if (paper.oa_status === "metadata_only" || paper.oa_status === "needs_upload") {
        return '<span class="status-chip meta">仅元数据</span>';
    }
    // 3. extraction_failed
    if (paper.oa_status === "failed" || paper.oa_status === "extraction_failed" || paper.oa_status === "error") {
        return '<span class="status-chip failed">解析失败</span>';
    }
    // 4. parsed
    if (paper.pdf_path && (paper.tei_path || paper.markdown_path || (paper.counts && paper.counts.sections > 0))) {
        return '<span class="status-chip parsed">已解析</span>';
    }
    // 5. pdf_available
    if (paper.pdf_path) {
        return '<span class="status-chip pdf-available">PDF已上传</span>';
    }
    return '<span class="status-chip none">状态未知</span>';
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
        let normalized = value;
        if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(value)) {
            normalized = value + "Z";
        }
        return new Date(normalized).toLocaleString("zh-CN");
    } catch (_) {
        return value;
    }
}

function getFilters() {
    const params = new URLSearchParams();
    params.set("limit", PAGE_SIZE);
    params.set("offset", state.currentOffset);
    const libraryName = getCurrentLibraryName();
    const searchInput = $("searchInput");
    const filterYear = $("filterYear");
    const filterJournal = $("filterJournal");
    const filterPaperType = $("filterPaperType");
    const filterDFT = $("filterDFT");
    const filterWC = $("filterWC");
    const q = searchInput ? searchInput.value.trim() : "";
    const year = filterYear ? filterYear.value.trim() : "";
    const journal = filterJournal ? filterJournal.value.trim() : "";
    const paperType = filterPaperType ? filterPaperType.value : "";
    const dft = filterDFT ? filterDFT.value : "";
    const wc = filterWC ? filterWC.value : "";
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
    state.qualityReasonContext = params.get("quality_reason") || "";
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
                '<p>上传 PDF、输入 DOI / URL，或使用 AI 自动搜文献来建立你的第一个文献库。</p>' +
                '<div class="empty-actions">' +
                    '<button class="btn primary" onclick="openAddLiteraturePanel(\'pdf\')">添加文献</button>' +
                    '<button class="btn ghost" onclick="openAddLiteraturePanel(\'ai\')">AI 搜文献</button>' +
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
                    '<p>左侧用于浏览、搜索和筛选文献。选中文献后，这里会显示摘要、章节、图表、DFT 数据、写作卡和 AI 建议候选。</p>' +
            '</div>';
    }
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

function ensureClassificationToolbarButton() {
    const toolbarRows = document.querySelectorAll(".toolbar .toolbar-row");
    const targetRow = toolbarRows && toolbarRows[1];
    if (!targetRow || targetRow.querySelector("[data-role='classify-unknown-btn']")) return;
    const refreshBtn = Array.from(targetRow.querySelectorAll("button")).find(function(btn) {
        return btn.getAttribute("onclick") === "refreshCurrentPage()";
    });
    const button = document.createElement("button");
    button.className = "btn ghost";
    button.dataset.role = "classify-unknown-btn";
    button.textContent = "重分类未知类型";
    button.addEventListener("click", classifyUnknownTypes);
    if (refreshBtn && refreshBtn.nextSibling) {
        targetRow.insertBefore(button, refreshBtn.nextSibling);
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

async function showFolderImportGuide() {
    setAcquisitionResult('<div class="workspace-empty small-empty">正在读取 MCP 批量导入指南...</div>');
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        const entry = guide.recommended_entrypoint || {};
        const tools = guide.mcp && Array.isArray(guide.mcp.common_tools) ? guide.mcp.common_tools : [];
        setAcquisitionResult(
            '<div class="section-card"><h3>本地文件夹批量导入指南</h3>' +
            '<div class="subtle">批量扫描文件夹由 MCP 工具 <strong>scan_local_pdfs</strong> 和 <strong>ingest_pdf_batch</strong> 执行；网页端请先前往 Ingestion Center 处理常规上传。</div>' +
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

function initProtocolWarning() {
    if (location.protocol === "file:") {
        const warning = $("fileModeWarning");
        if (warning) warning.style.display = "block";
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
    
    data.items.forEach(item => {
        const missingList = item.missing_fields.map(m => `<span class="tag" style="background:var(--color-warning-bg);color:var(--color-warning);">${esc(m)}</span>`).join(" ");
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
    closeDeletePaperDialog: closeDeletePaperDialog,
    confirmDeleteCurrentPaper: confirmDeleteCurrentPaper,
    classifyUnknownTypes: classifyUnknownTypes,
    showFolderImportGuide: showFolderImportGuide,
    switchTab: switchTab,
    openMetadataDiagnostics: openMetadataDiagnostics,
    closeMetadataDiagnostics: closeMetadataDiagnostics
});

window.addEventListener("beforeunload", disconnectSSE);
document.addEventListener("click", closeDropdowns);
const searchInput = $("searchInput");
if (searchInput) {
    searchInput.addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });
}

applyQueryParams();
initProtocolWarning();
initSplitDrag();
initActionMenus();
ensureClassificationToolbarButton();
TopNav.init({ currentPage: 'literature', mountId: 'topnav-mount' });
fetchPapers();
initSSE();
switchTab(state.currentTab);
if (state.openAddOnLoad) {
    openAddLiteraturePanel(state.openAddOnLoad);
}
loadLibraries();
loadWriterSettings();
