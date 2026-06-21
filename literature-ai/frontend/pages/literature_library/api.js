const API_BASE = "/api/papers";
const LIB_API = "/api/libraries";
const WRITER_API = "/api/writer";
const EXTERNAL_API = "/api/external-analysis";
const PAGE_SIZE = 25;
const SYSTEM_API = "/api/system";
const CURRENT_LIBRARY_STORAGE_KEY = "litai_current_library";

function rememberCurrentLibraryName(name) {
    try {
        if (name) {
            window.localStorage.setItem(CURRENT_LIBRARY_STORAGE_KEY, name);
        } else {
            window.localStorage.removeItem(CURRENT_LIBRARY_STORAGE_KEY);
        }
    } catch (_) {
        // localStorage can be unavailable in strict browser modes.
    }
}

function getRememberedCurrentLibraryName() {
    try {
        return window.localStorage.getItem(CURRENT_LIBRARY_STORAGE_KEY) || "";
    } catch (_) {
        return "";
    }
}

function showToast(message, type) {
    const existing = document.querySelector(".toast");
    if (existing) existing.remove();
    const el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function() {
        el.style.opacity = "0";
        setTimeout(function() { el.remove(); }, 280);
    }, 3000);
}

function showProgress(message, color) {
    let el = $("progressBox");
    if (!el) {
        el = document.createElement("div");
        el.id = "progressBox";
        el.className = "progress-box";
        document.body.appendChild(el);
    }
    el.textContent = message;
    el.style.background = color || "";
}

function hideProgress(immediate) {
    const el = $("progressBox");
    if (!el) return;
    if (immediate) {
        el.remove();
        return;
    }
    setTimeout(function() {
        const box = $("progressBox");
        if (box) box.remove();
    }, 1800);
}

async function fetchJSON(url, options) {
    options = options || {};
    options.headers = options.headers || {};
    
    // Check if an administrative token exists in sessionStorage and append it
    const token = sessionStorage.getItem("litai-settings-token");
    if (token) {
        options.headers["X-Settings-Token"] = token;
    }

    const resp = await fetch(url, options);
    
    // Error handling with dynamic administrative guidance if 403 occurs
    if (resp.status === 403) {
        showToast("无权访问该接口。请先在[设置]中配置管理员 Token 或确认是否为本地请求。", "error");
        throw new Error("403 Forbidden: Admin Token Required");
    }

    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) {}
    if (!resp.ok) {
        const detail = (data && data.detail) ? data.detail : (data && (data.status || data.message) ? data : ("HTTP " + resp.status));
        const errStr = typeof detail === "object" ? (detail.message || detail.status || "请求失败") : detail;
        const err = new Error(errStr);
        err.detail = detail;
        err.status = resp.status;
        throw err;
    }
    return data;
}

const UI_LABELS = {
    locator_status: {
        exact_page: "精确页码",
        exact: "精确定位",
        page_only: "仅页码",
        text_only: "仅文本",
        missing_page: "缺页码",
        missing_locator: "暂无定位",
        approximate: "需确认",
        unresolved: "待解析",
        needs_reparse: "需重新解析",
        missing: "暂无定位",
        unknown: "未识别"
    },
    source: {
        manual: "手动导入",
        internal_ai: "IDE AI 回写",
        chatgpt_web: "ChatGPT 网页",
        claude_web: "Claude 网页",
        section: "章节",
        sections: "章节",
        text: "文本",
        docling: "Docling",
        grobid: "GROBID",
        unknown: "未识别"
    },
    mapping_status: {
        normalized: "已规范化",
        heuristic: "规则映射",
        normalized_with_llm: "AI 规范化",
        pending: "待处理",
        failed: "失败",
        unknown: "未识别"
    },
    candidate_status: {
        pending: "待确认",
        requires_resolution: "需人工处理",
        materialized: "已生成审核记录",
        ai_reviewed: "AI 已审核",
        ai_applied: "AI 已修改入库",
        skipped: "已跳过",
        unknown: "未识别"
    }
};

function uiLabel(kind, value) {
    const key = String(value || "unknown").trim().toLowerCase();
    return (UI_LABELS[kind] && UI_LABELS[kind][key]) || (value ? String(value) : UI_LABELS[kind]?.unknown || "未识别");
}

const PAPER_TYPE_LABELS = {
    a: "A类",
    a1: "A1类",
    a2: "A2类",
    a3: "A3类",
    a4: "A4类",
    b: "B类",
    b1: "B1类",
    b2: "B2类",
    b3: "B3类",
    c: "C类",
    c1: "C1类",
    c2: "C2类",
    c3: "C3类",
    r: "综述",
    review: "综述",
    research: "研究论文",
    unknown: "未知类型"
};

function paperTypeLabel(value) {
    const raw = String(value || "").trim();
    if (!raw) return PAPER_TYPE_LABELS.unknown;
    const key = raw.toLowerCase();
    if (PAPER_TYPE_LABELS[key]) return PAPER_TYPE_LABELS[key];
    if (/^[abc]\d?$/i.test(raw)) return raw.toUpperCase() + "类";
    if (/^r\d?$/i.test(raw)) return "综述";
    if (key === "null" || key === "none") return PAPER_TYPE_LABELS.unknown;
    return raw;
}

function paperTypeBadgeClass(value) {
    const raw = String(value || "").trim().toUpperCase();
    return /^[ABCR]/.test(raw) ? raw.charAt(0) : "Unknown";
}

function normalizeExternalSourceForApi(value) {
    const raw = String(value || "").trim();
    if (!raw || raw === "手动导入") return "manual";
    if (raw === "IDE AI 回写") return "internal_ai";
    return raw;
}

function extractDoiList(raw) {
    const value = String(raw || "");
    const matches = value.match(/10\.\d{4,9}\/[-._;()/:A-Z0-9]+/ig) || [];
    const seen = new Set();
    return matches.map(function(item) {
        return item.replace(/[.,;:)]$/g, "").toLowerCase();
    }).filter(function(item) {
        if (seen.has(item)) return false;
        seen.add(item);
        return true;
    });
}

function primaryDoiInfo(raw) {
    const dois = extractDoiList(raw);
    if (dois.length) return { doi: dois[0], hasMultiple: dois.length > 1 };
    return { doi: String(raw || "").trim(), hasMultiple: false };
}

function renderDoiMeta(raw) {
    const info = primaryDoiInfo(raw);
    if (!info.doi) return '<span class="doi-main">无 DOI</span>';
    return '<span class="doi-main" title="' + escAttr(info.doi) + '">DOI: ' + esc(info.doi) + '</span>' +
        (info.hasMultiple ? '<span class="doi-warning">检测到多个 DOI，可能需要重新解析元数据</span>' : '');
}

function paperHasPdf(paper) {
    if (!paper) return false;
    if (paper.oa_status === "metadata_only" || paper.oa_status === "needs_upload") return false;
    const artifactStatus = paper.artifact_status && typeof paper.artifact_status === "object"
        ? paper.artifact_status
        : (paper.pdf_artifact_status && typeof paper.pdf_artifact_status === "object" ? paper.pdf_artifact_status : {});
    if (paper.pdf_exists === true || artifactStatus.pdf_exists === true) return true;
    if (paper.pdf_exists === false || artifactStatus.pdf_exists === false) return false;
    if (typeof paper.pdf_url === "string" && paper.pdf_url.trim()) return true;
    return !!paper.pdf_path;
}

function getCurrentLibraryName() {
    const el = $("librarySelect");
    return el ? el.value || "" : "";
}

function safeLibraryFolderName(name) {
    return String(name || "").trim().replace(/[\\/]/g, "_").replace(/\s+/g, "_");
}

function containerPathToHostHint(path) {
    return "";
}

function setCreatePathPreview() {
    const preview = $("createLibResolvedPath");
    if (!preview) return;
    const nameEl = $("createLibName");
    const parent = window._selectedPath_createLib || "";
    const safeName = safeLibraryFolderName(nameEl ? nameEl.value : "");
    if (!parent) {
        preview.style.display = "none";
        preview.textContent = "";
        return;
    }
    const finalPath = safeName ? parent.replace(/[\\/]$/, "") + "/" + safeName : parent;
    const hostHint = containerPathToHostHint(finalPath);
    preview.style.display = "block";
    preview.textContent = "将创建到: " + finalPath + (hostHint ? " | 宿主机对应: " + hostHint : "");
}

async function loadLibraryRuntimeInfo() {
    const el = $("libraryRuntimeInfo");
    if (!el) return;
    try {
        const info = await fetchJSON(SYSTEM_API + "/db-info");
        if (!info || typeof info !== "object") {
            throw new Error("未获取到数据库运行时信息");
        }
        const activeDb = info.active_library_db_path || info.effective_db_path || "";
        const storageRoot = info.effective_storage_root || info.storage_root || "";
        const registryPath = "/data/library_registry.json";
        let activeRoot = activeDb ? activeDb.replace(/\\/g, "/").replace(/\/database\.sqlite$/i, "") : "";
        if (info.dialect === "postgresql" && !activeRoot) {
            activeRoot = storageRoot ? storageRoot.replace(/\\/g, "/").replace(/\/storage$/i, "") : "";
        }
        const dbDisplay = info.dialect === "postgresql"
            ? "PostgreSQL: <code>" + esc(info.database_url_masked || activeDb || "-") + "</code>"
            : "Database (" + esc(info.dialect || "unknown") + "): <code>" + esc(activeDb || info.database_url_masked || "-") + "</code>";
        el.innerHTML =
            dbDisplay +
            " | 注册表: <code>" + esc(registryPath) + "</code>" +
            " | 库目录: <code>" + esc(activeRoot || "-") + "</code>" +
            " | 产物目录: <code>" + esc(storageRoot || "-") + "</code>";
    } catch (error) {
        el.textContent = "当前文献库路径读取失败: " + error.message;
    }
}

function normalizeLibraryListResponse(data) {
    if (Array.isArray(data)) return data;
    if (!data || typeof data !== "object") return [];
    if (Array.isArray(data.libraries)) return data.libraries;
    if (Array.isArray(data.items)) return data.items;
    return [];
}

async function loadLibraries() {
    try {
        const el = $("librarySelect");
        const requestedLibrary = new URLSearchParams(window.location.search).get("library_name") || "";
        const rememberedLibrary = requestedLibrary ? "" : getRememberedCurrentLibraryName();
        const isInitialLoad = !requestedLibrary && !(state.currentLibrary && state.currentLibrary.name) && (!el || !el.value);
        const previousSelection = isInitialLoad ? "" : (el ? (el.value || (state.currentLibrary && state.currentLibrary.name) || "") : "");
        const quickLibraries = normalizeLibraryListResponse(await fetchJSON(API_BASE + "/libraries"));
        const quickActive = (quickLibraries || []).find(function(item) { return item.is_active; });
        const quickLargest = (quickLibraries || []).slice().sort(function(left, right) {
            return Number(right.paper_count || 0) - Number(left.paper_count || 0);
        })[0];
        const selectedName = requestedLibrary && (quickLibraries || []).some(function(item) { return item.name === requestedLibrary; })
            ? requestedLibrary
            : (previousSelection && (quickLibraries || []).some(function(item) { return item.name === previousSelection; })
                ? previousSelection
                : (rememberedLibrary && (quickLibraries || []).some(function(item) { return item.name === rememberedLibrary; })
                    ? rememberedLibrary
                    : ((quickActive && quickActive.name) || (quickLargest && quickLargest.name) || ((quickLibraries || [])[0] ? quickLibraries[0].name : ""))));
        if (el) {
            el.innerHTML = (quickLibraries || []).map(function(item) {
                return '<option value="' + esc(item.name) + '"' + (item.name === selectedName ? " selected" : "") + ">" +
                    esc(item.name) + "（" + esc(item.paper_count || 0) + " 篇）" +
                "</option>";
            }).join("");
        }
        const selected = (quickLibraries || []).find(function(item) { return item.name === selectedName; });
        state.currentLibrary = selected || null;
        rememberCurrentLibraryName(state.currentLibrary ? state.currentLibrary.name : "");
        state.currentLibraryTotal = selected ? Number(selected.paper_count || 0) : 0;
        const status = $("libStatus");
        if (status) status.textContent = selected ? (selected.name + " | " + selected.paper_count + " 篇文献") : "";

        const libraries = normalizeLibraryListResponse(await fetchJSON(LIB_API));
        const fullEl = $("librarySelect");
        const active = (libraries || []).find(function(item) { return item.is_active; });
        const keepName = requestedLibrary && (libraries || []).some(function(item) { return item.name === requestedLibrary; })
            ? requestedLibrary
            : (previousSelection && (libraries || []).some(function(item) { return item.name === previousSelection; })
                ? previousSelection
                : (rememberedLibrary && (libraries || []).some(function(item) { return item.name === rememberedLibrary; })
                    ? rememberedLibrary
                    : ((active && active.name) || (fullEl ? fullEl.value : selectedName) || selectedName)));
        if (fullEl && libraries && libraries.length) {
            fullEl.innerHTML = libraries.map(function(item) {
                const isSelected = item.name === keepName;
                return '<option value="' + esc(item.name) + '"' + (isSelected ? " selected" : "") + ">" +
                    esc(item.name) + (item.is_active ? "（当前）" : "") +
                "</option>";
            }).join("");
        }
        const selectedFull = (libraries || []).find(function(item) { return item.name === keepName; }) || active;
        if (selectedFull) {
            state.currentLibrary = selectedFull;
            rememberCurrentLibraryName(selectedFull.name || "");
            state.currentLibraryTotal = Number(selectedFull.paper_count || state.currentLibraryTotal || 0);
            if (status) status.textContent = (selectedFull.root_path || selectedFull.name) + " | " + state.currentLibraryTotal + " 篇文献";
        }
    } catch (error) {
        console.error("loadLibraries failed", error);
        try {
            const libraries = normalizeLibraryListResponse(await fetchJSON(LIB_API));
            const el = $("librarySelect");
            const requestedLibrary = new URLSearchParams(window.location.search).get("library_name") || "";
            const rememberedLibrary = requestedLibrary ? "" : getRememberedCurrentLibraryName();
            if (el) {
                const active = (libraries || []).find(function(item) { return item.is_active; });
                const keepName = requestedLibrary && (libraries || []).some(function(item) { return item.name === requestedLibrary; })
                    ? requestedLibrary
                    : (rememberedLibrary && (libraries || []).some(function(item) { return item.name === rememberedLibrary; })
                        ? rememberedLibrary
                        : ((active && active.name) || ""));
                el.innerHTML = libraries.map(function(item) {
                return '<option value="' + esc(item.name) + '"' + (item.name === keepName ? " selected" : "") + ">" +
                    esc(item.name) + (item.is_active ? "（当前）" : "") +
                "</option>";
            }).join("");
        }
        const active = (libraries || []).find(function(item) { return item.is_active; });
        const selected = (requestedLibrary && (libraries || []).find(function(item) { return item.name === requestedLibrary; }))
            || (rememberedLibrary && (libraries || []).find(function(item) { return item.name === rememberedLibrary; }));
        state.currentLibrary = selected || active || null;
        rememberCurrentLibraryName(state.currentLibrary ? state.currentLibrary.name : "");
        state.currentLibraryTotal = state.currentLibrary ? Number(state.currentLibrary.paper_count || 0) : 0;
        const status = $("libStatus");
        if (status) status.textContent = state.currentLibrary ? ((state.currentLibrary.root_path || state.currentLibrary.name) + " | " + state.currentLibraryTotal + " 篇文献") : "";
        } catch (fallbackError) {
            console.error("loadLibraries fallback failed", fallbackError);
        }
    }
}

async function loadWriterSettings() {
    state.writerSettings = {
        writer_backend: "disabled",
        writer_model: ""
    };
}

async function activateLibraryByName(name) {
    if (!name) return;
    const selector = $("librarySelect");
    const previousLibraryName = (state.currentLibrary && state.currentLibrary.name) || "";
    if (selector) selector.disabled = true;
    try {
        const payload = await fetchJSON(LIB_API + "/" + encodeURIComponent(name) + "/activate", { method: "POST" });
        const activeLibraryName = (payload && payload.name) ? payload.name : name;
        rememberCurrentLibraryName(activeLibraryName);
        showToast("已切换到：" + activeLibraryName, "success");
        if (typeof resetLibraryPagination === "function") resetLibraryPagination();
        else state.currentOffset = 0;
        await refreshCurrentPage();
    } catch (error) {
        if (selector) {
            selector.value = previousLibraryName;
        }
        rememberCurrentLibraryName(previousLibraryName);
        try {
            await loadLibraries();
        } catch (_) {
            // Keep the original activation error as the user-facing result.
        }
        showToast("切库失败：" + error.message, "error");
    } finally {
        if (selector) selector.disabled = false;
    }
}

async function removeCurrentLibrary() {
    try {
        const libraries = await fetchJSON(LIB_API);
        const active = (libraries || []).find(function(item) { return item.is_active; });
        if (!active) {
            showToast("当前没有激活的库。", "error");
            return;
        }
        window._removeLibName = active.name;
        const msg = $("removeLibMsg");
        if (msg) msg.textContent = '确定要移除“' + active.name + '”吗？';
        const dialog = $("removeLibDialog");
        if (dialog) dialog.style.display = "flex";
    } catch (error) {
        showToast("读取库信息失败：" + error.message, "error");
    }
}

function closeRemoveLibraryDialog() {
    const dialog = $("removeLibDialog");
    if (dialog) dialog.style.display = "none";
}

async function confirmRemoveLibrary() {
    if (!window._removeLibName) return;
    try {
        await fetchJSON(LIB_API + "/" + encodeURIComponent(window._removeLibName), { method: "DELETE" });
        closeRemoveLibraryDialog();
        showToast("库已移除。", "success");
        await loadLibraries();
        if (typeof resetLibraryPagination === "function") resetLibraryPagination();
        else state.currentOffset = 0;
        refreshCurrentPage();
    } catch (error) {
        showToast("移除失败：" + error.message, "error");
    }
}

async function loadDirBrowser(kind) {
    const el = $(kind + "DirBrowser");
    if (el) el.textContent = "加载中...";
    try {
        const roots = await fetchJSON(LIB_API + "/browse-roots");
        if (!roots.length) {
            if (el) el.textContent = "没有可浏览的目录";
            return;
        }
        renderDirBrowser(kind, roots[0].path);
    } catch (error) {
        if (el) el.textContent = "加载失败：" + error.message;
    }
}

async function renderDirBrowser(kind, path) {
    const el = $(kind + "DirBrowser");
    try {
        const data = await fetchJSON(LIB_API + "/browse?path=" + encodeURIComponent(path));
        const parts = String(data.current_path || "").replace(/\\/g, "/").split("/").filter(Boolean);
        let html = '<div class="breadcrumbs">';
        if (data.parent_path) {
            html += '<span class="bc-link" data-path="' + esc(data.parent_path) + '">..</span> / ';
        }
        let current = "";
        parts.forEach(function(part, idx) {
            current += "/" + part;
            html += '<span class="bc-link" data-path="' + esc(current) + '">' + esc(part) + "</span>";
            if (idx < parts.length - 1) html += " / ";
        });
        html += "</div>";
        (data.subdirs || []).forEach(function(item) {
            html += '<div class="dir-item" data-path="' + esc(item.path) + '">📁 ' + esc(item.name) + "</div>";
        });
        if (el) {
            el.innerHTML = html;
            el.querySelectorAll(".bc-link").forEach(function(link) {
                link.onclick = function() { renderDirBrowser(kind, this.getAttribute("data-path")); };
            });
            el.querySelectorAll(".dir-item").forEach(function(item) {
                item.onclick = function() { selectDir(kind, this.getAttribute("data-path")); };
            });
        }
    } catch (error) {
        if (el) el.textContent = "浏览失败：" + error.message;
    }
}

function selectDir(kind, path) {
    const el = $(kind + "SelectedPath");
    if (el) {
        el.style.display = "block";
        el.textContent = path;
    }
    window["_selectedPath_" + kind] = path;
    renderDirBrowser(kind, path);
}

function openCreateLibraryDialog() {
    const name = $("createLibName");
    if (name) name.value = "";
    const path = $("createLibSelectedPath");
    if (path) path.style.display = "none";
    window._selectedPath_createLib = null;
    const dialog = $("createLibDialog");
    if (dialog) dialog.style.display = "flex";
    loadDirBrowser("createLib");
}

function closeCreateLibraryDialog() {
    const dialog = $("createLibDialog");
    if (dialog) dialog.style.display = "none";
}

async function setCreateDefaultLocation() {
    let path = "";
    try {
        const roots = await fetchJSON(`${LIB_API}/browse-roots`);
        path = roots?.[0]?.path || "";
    } catch (error) {
        console.warn("Failed to load browse roots", error);
    }
    if (!path) return;
    const pathEl = $("createLibSelectedPath");
    if (pathEl) {
        pathEl.style.display = "block";
        pathEl.textContent = path;
    }
    window._selectedPath_createLib = path;
}

async function submitCreateLibrary() {
    const nameEl = $("createLibName");
    const name = nameEl ? nameEl.value.trim() : "";
    if (!name) {
        showToast("请输入库名称。", "error");
        return;
    }
    try {
        await fetchJSON(LIB_API, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name, root_path: window._selectedPath_createLib || "" })
        });
        closeCreateLibraryDialog();
        showToast("库创建成功。", "success");
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        showToast("创建失败：" + error.message, "error");
    }
}

function openImportLibraryDialog() {
    const path = $("importLibSelectedPath");
    if (path) path.style.display = "none";
    window._selectedPath_importLib = null;
    const dialog = $("importLibDialog");
    if (dialog) dialog.style.display = "flex";
    loadDirBrowser("importLib");
}

function closeImportLibraryDialog() {
    const dialog = $("importLibDialog");
    if (dialog) dialog.style.display = "none";
}

async function submitImportLibrary() {
    if (!window._selectedPath_importLib) {
        showToast("请选择已有库目录。", "error");
        return;
    }
    try {
        await fetchJSON(LIB_API + "/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ root_path: window._selectedPath_importLib })
        });
        closeImportLibraryDialog();
        showToast("库导入成功。", "success");
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        showToast("导入失败：" + error.message, "error");
    }
}

async function fetchExtractionReviewAudit(paperId) {
    return await fetchJSON("/api/extraction/results/" + encodeURIComponent(paperId) + "/reviews/audit");
}

// ── G3B Evidence Locator API ──

async function fetchPaperEvidenceLocators(paperId) {
    try {
        return await fetchJSON(API_BASE + "/" + encodeURIComponent(paperId) + "/evidence/locators");
    } catch (error) {
        if (error.status === 404 || error.status === 500) {
            return { _error: true, status: error.status, detail: error.detail || error.message };
        }
        return { _error: true, status: 0, detail: error.message };
    }
}

async function fetchExtractionEvidenceLocators(paperId) {
    try {
        return await fetchJSON("/api/extraction/results/" + encodeURIComponent(paperId) + "/evidence-locators");
    } catch (error) {
        if (error.status === 404 || error.status === 500) {
            return { _error: true, status: error.status, detail: error.detail || error.message };
        }
        return { _error: true, status: 0, detail: error.message };
    }
}

async function fetchClaimEvidenceLocator(claimId) {
    try {
        return await fetchJSON("/api/evidence/claims/" + encodeURIComponent(claimId) + "/locator");
    } catch (error) {
        if (error.status === 404 || error.status === 500) {
            return { _error: true, status: error.status, detail: error.detail || error.message };
        }
        return { _error: true, status: 0, detail: error.message };
    }
}

loadLibraries = async function() {
    try {
        const el = $("librarySelect");
        const requestedLibrary = new URLSearchParams(window.location.search).get("library_name") || "";
        const rememberedLibrary = requestedLibrary ? "" : getRememberedCurrentLibraryName();
        const isInitialLoad = !requestedLibrary && !(state.currentLibrary && state.currentLibrary.name) && (!el || !el.value);
        const previousSelection = isInitialLoad ? "" : (el ? (el.value || (state.currentLibrary && state.currentLibrary.name) || "") : "");
        const quickLibraries = normalizeLibraryListResponse(await fetchJSON(API_BASE + "/libraries"));
        const quickActive = (quickLibraries || []).find(function(item) { return item.is_active; });
        const quickLargest = (quickLibraries || []).slice().sort(function(left, right) {
            return Number(right.paper_count || 0) - Number(left.paper_count || 0);
        })[0];
        const selectedName = requestedLibrary && (quickLibraries || []).some(function(item) { return item.name === requestedLibrary; })
            ? requestedLibrary
            : (previousSelection && (quickLibraries || []).some(function(item) { return item.name === previousSelection; })
                ? previousSelection
                : (rememberedLibrary && (quickLibraries || []).some(function(item) { return item.name === rememberedLibrary; })
                    ? rememberedLibrary
                    : ((quickActive && quickActive.name) || (quickLargest && quickLargest.name) || ((quickLibraries || [])[0] ? quickLibraries[0].name : ""))));
        if (el) {
            el.innerHTML = (quickLibraries || []).map(function(item) {
                return '<option value="' + esc(item.name) + '"' + (item.name === selectedName ? " selected" : "") + ">" +
                    esc(item.name) + "（" + esc(item.paper_count || 0) + " 篇）" +
                "</option>";
            }).join("");
        }
        const selected = (quickLibraries || []).find(function(item) { return item.name === selectedName; });
        state.currentLibrary = selected || null;
        rememberCurrentLibraryName(state.currentLibrary ? state.currentLibrary.name : "");
        state.currentLibraryTotal = selected ? Number(selected.paper_count || 0) : 0;
        const status = $("libStatus");
        if (status) status.textContent = selected ? (selected.name + " | " + selected.paper_count + " 篇文献") : "";

        const libraries = normalizeLibraryListResponse(await fetchJSON(LIB_API));
        const fullEl = $("librarySelect");
        const active = (libraries || []).find(function(item) { return item.is_active; });
        const keepName = requestedLibrary && (libraries || []).some(function(item) { return item.name === requestedLibrary; })
            ? requestedLibrary
            : (previousSelection && (libraries || []).some(function(item) { return item.name === previousSelection; })
                ? previousSelection
                : (rememberedLibrary && (libraries || []).some(function(item) { return item.name === rememberedLibrary; })
                    ? rememberedLibrary
                    : ((active && active.name) || (fullEl ? fullEl.value : selectedName) || selectedName)));
        if (fullEl && libraries && libraries.length) {
            fullEl.innerHTML = libraries.map(function(item) {
                const isSelected = item.name === keepName;
                return '<option value="' + esc(item.name) + '"' + (isSelected ? " selected" : "") + ">" +
                    esc(item.name) + (item.is_active ? "（当前）" : "") +
                "</option>";
            }).join("");
        }
        const selectedFull = (libraries || []).find(function(item) { return item.name === keepName; }) || active;
        if (selectedFull) {
            state.currentLibrary = selectedFull;
            rememberCurrentLibraryName(selectedFull.name || "");
            state.currentLibraryTotal = Number(selectedFull.paper_count || state.currentLibraryTotal || 0);
            if (status) status.textContent = (selectedFull.root_path || selectedFull.name) + " | " + state.currentLibraryTotal + " 篇文献";
        }
        loadLibraryRuntimeInfo();
    } catch (error) {
        console.error("loadLibraries failed", error);
        try {
            const libraries = normalizeLibraryListResponse(await fetchJSON(LIB_API));
            const el = $("librarySelect");
            const requestedLibrary = new URLSearchParams(window.location.search).get("library_name") || "";
            const rememberedLibrary = requestedLibrary ? "" : getRememberedCurrentLibraryName();
            if (el) {
                const active = (libraries || []).find(function(item) { return item.is_active; });
                const keepName = requestedLibrary && (libraries || []).some(function(item) { return item.name === requestedLibrary; })
                    ? requestedLibrary
                    : (rememberedLibrary && (libraries || []).some(function(item) { return item.name === rememberedLibrary; })
                        ? rememberedLibrary
                        : ((active && active.name) || ""));
                el.innerHTML = libraries.map(function(item) {
                    return '<option value="' + esc(item.name) + '"' + (item.name === keepName ? " selected" : "") + ">" +
                        esc(item.name) + (item.is_active ? "（当前）" : "") +
                    "</option>";
                }).join("");
            }
            const active = (libraries || []).find(function(item) { return item.is_active; });
            const selected = (requestedLibrary && (libraries || []).find(function(item) { return item.name === requestedLibrary; }))
                || (rememberedLibrary && (libraries || []).find(function(item) { return item.name === rememberedLibrary; }))
                || active;
            state.currentLibrary = selected || null;
            rememberCurrentLibraryName(state.currentLibrary ? state.currentLibrary.name : "");
            state.currentLibraryTotal = state.currentLibrary ? Number(state.currentLibrary.paper_count || 0) : 0;
            const status = $("libStatus");
            if (status) status.textContent = state.currentLibrary ? ((state.currentLibrary.root_path || state.currentLibrary.name) + " | " + state.currentLibraryTotal + " 篇文献") : "";
            loadLibraryRuntimeInfo();
        } catch (fallbackError) {
            console.error("loadLibraries fallback failed", fallbackError);
        }
    }
};

if (typeof refreshCurrentPage !== "function") {
    refreshCurrentPage = async function() {
        if (typeof loadLibraries === "function") {
            await loadLibraries();
        }
        if (typeof fetchPapers === "function") {
            await fetchPapers();
        }
        if (typeof initSSE === "function") {
            initSSE();
        }
    };
}

selectDir = function(kind, path) {
    const el = $(kind + "SelectedPath");
    if (el) {
        el.style.display = "block";
        const hostHint = containerPathToHostHint(path);
        el.textContent = hostHint ? (path + " | 宿主机对应: " + hostHint) : path;
    }
    window["_selectedPath_" + kind] = path;
    if (kind === "createLib") setCreatePathPreview();
    renderDirBrowser(kind, path);
};

openCreateLibraryDialog = function() {
    const name = $("createLibName");
    if (name) name.value = "";
    const path = $("createLibSelectedPath");
    if (path) path.style.display = "none";
    const resolved = $("createLibResolvedPath");
    if (resolved) {
        resolved.style.display = "none";
        resolved.textContent = "";
    }
    window._selectedPath_createLib = null;
    const dialog = $("createLibDialog");
    if (dialog) dialog.style.display = "flex";
    loadDirBrowser("createLib");
};

setCreateDefaultLocation = async function() {
    let path = "";
    try {
        const roots = await fetchJSON(`${LIB_API}/browse-roots`);
        path = roots?.[0]?.path || "";
    } catch (error) {
        console.warn("Failed to load browse roots", error);
    }
    if (!path) return;
    const pathEl = $("createLibSelectedPath");
    if (pathEl) {
        pathEl.style.display = "block";
        const hostHint = containerPathToHostHint(path);
        pathEl.textContent = hostHint ? (path + " | 宿主机对应: " + hostHint) : path;
    }
    window._selectedPath_createLib = path;
    setCreatePathPreview();
};

openImportLibraryDialog = function() {
    const path = $("importLibSelectedPath");
    if (path) path.style.display = "none";
    window._selectedPath_importLib = null;
    const dialog = $("importLibDialog");
    if (dialog) dialog.style.display = "flex";
    loadDirBrowser("importLib");
};

document.addEventListener("DOMContentLoaded", function() {
    const createName = $("createLibName");
    if (createName) {
        createName.addEventListener("input", setCreatePathPreview);
    }
    loadLibraryRuntimeInfo();
});

submitCreateLibrary = async function() {
    const nameEl = $("createLibName");
    const name = nameEl ? nameEl.value.trim() : "";
    if (!name) {
        showToast("请输入库名称。", "error");
        return;
    }
    try {
        await fetchJSON(LIB_API, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name, root_path: window._selectedPath_createLib || "" })
        });
        closeCreateLibraryDialog();
        showToast("库创建成功。", "success");
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        if (String(error.message || "").includes("already exists")) {
            closeCreateLibraryDialog();
            await activateLibraryByName(name);
            showToast("同名文献库已经存在，已直接切换到该库。", "info");
            return;
        }
        showToast("创建失败：" + error.message, "error");
    }
};

submitImportLibrary = async function() {
    if (!window._selectedPath_importLib) {
        showToast("请选择已有库目录。", "error");
        return;
    }
    try {
        await fetchJSON(LIB_API + "/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ root_path: window._selectedPath_importLib })
        });
        closeImportLibraryDialog();
        showToast("库导入成功。", "success");
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        if (String(error.message || "").includes("already exists")) {
            const parts = String(window._selectedPath_importLib || "").replace(/\\/g, "/").split("/");
            const fallbackName = parts.filter(Boolean).pop() || "";
            closeImportLibraryDialog();
            if (fallbackName) {
                await activateLibraryByName(fallbackName);
            } else {
                await loadLibraries();
            }
            showToast("这个文献库已经导入过了，已直接切换到现有库。", "info");
            return;
        }
        showToast("导入失败：" + error.message, "error");
    }
};

