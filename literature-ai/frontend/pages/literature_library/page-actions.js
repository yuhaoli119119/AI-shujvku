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
    const canonicalId = canonicalPaperId(paperId);
    if (state.selectedPaperId === canonicalId) {
        state.selectedPaper = resolvePaperFromState(canonicalId) || state.selectedPaper;
        updateRowSelectionUI();
        return;
    }
    state.selectedPaperId = canonicalId;
    state.selectedPaper = resolvePaperFromState(canonicalId) || state.selectedPaper;
    updateRowSelectionUI();
    if (typeof loadPaperDetail === "function") loadPaperDetail(canonicalId);
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
