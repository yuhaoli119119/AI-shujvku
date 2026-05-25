const API_BASE = "/api/papers";
const LIB_API = "/api/libraries";
const WRITER_API = "/api/writer";
const EXTERNAL_API = "/api/external-analysis";
const PAGE_SIZE = 20;

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

function hideProgress() {
    const el = $("progressBox");
    if (!el) return;
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

function getCurrentLibraryName() {
    const el = $("librarySelect");
    return el ? el.value || "" : "";
}

async function loadLibraries() {
    try {
        const libraries = await fetchJSON(LIB_API);
        const el = $("librarySelect");
        if (el) {
            el.innerHTML = libraries.map(function(item) {
                return '<option value="' + esc(item.name) + '"' + (item.is_active ? " selected" : "") + ">" +
                    esc(item.name) + (item.is_active ? "（当前）" : "") +
                "</option>";
            }).join("");
        }
        const active = (libraries || []).find(function(item) { return item.is_active; });
        state.currentLibrary = active || null;
        state.currentLibraryTotal = active ? Number(active.paper_count || 0) : 0;
        const status = $("libStatus");
        if (status) status.textContent = active ? (active.root_path + " | " + active.paper_count + " 篇文献") : "";
    } catch (error) {
        console.error("loadLibraries failed", error);
    }
}

async function loadWriterSettings() {
    try {
        state.writerSettings = await fetchJSON("/api/settings");
    } catch (error) {
        state.writerSettings = null;
        console.warn("loadWriterSettings failed", error);
    }
}

async function activateLibraryByName(name) {
    if (!name) return;
    try {
        await fetchJSON(LIB_API + "/" + encodeURIComponent(name) + "/activate", { method: "POST" });
        showToast("已切换到：" + name, "success");
        state.currentOffset = 0;
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        showToast("切库失败：" + error.message, "error");
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
        state.currentOffset = 0;
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

