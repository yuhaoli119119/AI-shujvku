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
