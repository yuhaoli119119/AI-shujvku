// Figure inspection, evidence locators, and PDF viewer behavior.
function isLikelyNoisyFigure(item) {
    item = item || {};
    var caption = String(item.caption || "").trim();
    var text = [
        caption,
        item.figure_role || "",
        item.content_summary || "",
        Array.isArray(item.key_elements) ? item.key_elements.join(" ") : ""
    ].join(" ").toLowerCase();
    var sparseCaption = /^(figure|fig\.?|scheme)\s*\d+\.?$/i.test(caption);
    var hasScientificSummary = !!(item.content_summary || (Array.isArray(item.key_elements) && item.key_elements.length));
    return /crossmark|science china press|publisher|logo|header|footer/.test(text) ||
        (sparseCaption && !hasScientificSummary);
}

// ── G3B Evidence Locator Rendering ──

function isPlaceholderFigureKeyElement(value) {
    const normalized = String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
    return ["verified_figure", "figure_verified", "reviewed_figure", "ai_verified", "verified"].includes(normalized);
}

function formatFigureKeyElementsForDisplay(keyElements) {
    if (!keyElements.length) return "-";
    const text = keyElements.map(figureTermLabel).join(", ");
    if (keyElements.every(isPlaceholderFigureKeyElement)) {
        return text + "（占位词，需要具体元素）";
    }
    if (keyElements.some(isPlaceholderFigureKeyElement)) {
        return text + "（含占位词）";
    }
    return text;
}

function figureTermLabel(value) {
    const raw = String(value == null ? "" : value).trim();
    if (!raw) return "-";
    const key = raw.toLowerCase().replace(/[\s-]+/g, "_");
    const labels = {
        structural_model: "结构模型",
        structure: "结构",
        model: "模型",
        mechanism_diagram: "机理示意图",
        band_structure: "能带结构",
        dos: "态密度",
        pdos: "分波态密度",
        charge_density: "电荷密度",
        reaction_pathway: "反应路径",
        reaction_barrier: "反应能垒",
        energy_profile: "能量曲线",
        fluorination: "氟化过程",
        adsorption_energy: "吸附能",
        xps: "XPS 谱图",
        spectra: "谱图",
        curve: "曲线",
        curves: "曲线",
        axis: "坐标轴",
        panel: "分图",
        panels: "分图",
        hexagonal_lattice: "六角晶格",
        stacking: "堆垛",
        aa_stacking: "AA 堆垛",
        ab_stacking: "AB 堆垛",
        abc_stacking: "ABC 堆垛",
        alpha_gdy_structure: "alpha-GDY 结构",
        hsgdy_structure: "HsGDY 结构",
        gdy_membrane: "GDY 膜",
        h2_molecules: "H2 分子",
        pair_potential: "对势曲线",
        interaction_energy_profile: "相互作用能曲线",
        quantum_transmission: "量子透射",
        time_series_crossings: "穿越数时间序列",
        spatial_distribution: "空间分布",
        arrhenius_plot: "Arrhenius 图",
        permeance_vs_temperature: "渗透率-温度曲线",
        position_dependent_barrier: "位置相关能垒",
        time_evolution: "时间演化"
    };
    if (labels[key]) return labels[key] + "（" + raw + "）";
    return raw;
}

function figureRagBlockedMap(detail) {
    const blockedItems = detail && detail.rag_quality && detail.rag_quality.figures && Array.isArray(detail.rag_quality.figures.blocked_items)
        ? detail.rag_quality.figures.blocked_items
        : [];
    const map = {};
    blockedItems.forEach(function(item) {
        if (item && item.source_id) map[item.source_id] = item;
    });
    return map;
}

function renderFigureParseDetailHtml(item) {
    item = item || {};
    const keyElements = Array.isArray(item.key_elements) ? item.key_elements : [];
    const imageReview = item.image_review || {};
    const approvedCorrectionFields = Array.isArray(item.approved_correction_fields) ? item.approved_correction_fields : [];
    const rows = [
        ["图号", item.figure_label || "-"],
        ["PDF 页码", item.page || "-"],
        ["图类型", figureTermLabel(item.figure_role || "unclassified")],
        ["类型置信度", item.role_confidence == null ? "-" : item.role_confidence],
        ["裁图状态", item.crop_status || "-"],
        ["裁图来源", item.crop_source || "-"],
        ["图片路径", item.image_path || "-"],
        ["内容摘要", item.content_summary || "-"],
        ["关键元素", formatFigureKeyElementsForDisplay(keyElements)],
        ["图片检查标记", Array.isArray(item.flags) && item.flags.length ? item.flags.join(", ") : "none"],
        ["整页快照", imageReview.full_page_image_path ? "有" : "缺失"],
        ["已批准修正数", item.approved_correction_count || 0],
        ["已批准修正字段", approvedCorrectionFields.length ? approvedCorrectionFields.join(", ") : "-"],
        ["对象审核数", item.object_review_audit_count || 0],
        ["冲突数", item.conflict_count || 0],
    ];
    return '<div class="figure-parse-detail">' +
        '<div class="figure-parse-title">图表解析字段</div>' +
        rows.filter(function(row) { return !(item.content_summary && row[1] === item.content_summary); }).map(function(row) {
            return '<div class="figure-parse-row"><strong>' + esc(row[0]) + '</strong><span>' + esc(row[1]) + '</span></div>';
        }).join("") +
    '</div>';
}

function openFigureLightbox(options) {
    options = options || {};
    const overlay = $("figureLightboxOverlay");
    const image = $("figureLightboxImage");
    const title = $("figureLightboxTitle");
    const meta = $("figureLightboxMeta");
    const src = options.src || "";
    if (!overlay || !image || !src) return;
    image.src = src;
    image.alt = options.alt || "图片大图预览";
    if (title) title.textContent = options.title || "图片预览";
    if (meta) {
        meta.textContent = [options.caption || "", options.page ? ("PDF 第 " + options.page + " 页") : ""]
            .filter(Boolean)
            .join(" | ");
    }
    overlay.style.display = "flex";
}

function closeFigureLightbox() {
    const overlay = $("figureLightboxOverlay");
    const image = $("figureLightboxImage");
    if (overlay) overlay.style.display = "none";
    if (image) {
        image.removeAttribute("src");
        image.alt = "图片大图预览";
    }
}

if (!window.__litaiFigureLightboxBound) {
    window.__litaiFigureLightboxBound = true;
    document.addEventListener("click", function(event) {
        const overlay = $("figureLightboxOverlay");
        if (overlay && event.target === overlay) closeFigureLightbox();
    });
    document.addEventListener("keydown", function(event) {
        if (event.key === "Escape") closeFigureLightbox();
    });
}

function escAttr(value) {
    return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

function normalizeLocatorStatus(locator) {
    const s = String((locator && locator.locator_status) || locator || "unknown").trim().toLowerCase();
    if (s === "exact" || s === "page_only") return "exact_page";
    if (s === "needs_reparse") return "missing_page";
    if (s === "missing") return "missing_locator";
    if (s === "approximate_candidate" || s === "ambiguous_match") return "approximate";
    return s;
}

function locatorCanJump(locator) {
    return normalizeLocatorStatus(locator) === "exact_page" && locator && locator.can_jump_to_pdf_page !== false && Number(locator.page || 0) > 0;
}

function locatorDegradedText(locator) {
    const s = normalizeLocatorStatus(locator);
    if (s === "approximate") return "可能相关页码，需要人工确认";
    if (s === "unresolved") return "证据定位待解析/待确认";
    if (s === "missing_locator") return "暂无可用 PDF 定位";
    return "仅有证据文本，暂无 PDF 页码定位";
}

function locatorStatusBadge(locatorStatus) {
    const s = normalizeLocatorStatus(locatorStatus);
    const label = uiLabel("locator_status", s);
    if (s === "exact_page") {
        return '<span class="status-chip" style="background:var(--color-success-bg);color:var(--color-success);border:1px solid var(--color-success)40;padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else if (s === "approximate") {
        return '<span class="status-chip" style="background:var(--color-primary-bg);color:var(--color-primary);border:1px solid var(--color-primary)40;padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else if (s === "text_only" || s === "missing_page") {
        return '<span class="status-chip" style="background:var(--color-warning-bg);color:var(--color-warning);border:1px solid var(--color-warning)40;padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else if (s === "missing_locator" || s === "unresolved") {
        return '<span class="status-chip" style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    } else {
        return '<span class="status-chip" style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:2px 8px;font-size:11px;font-weight:700;border-radius:var(--radius-pill);">' + label + '</span>';
    }
}

function locatorActionHtml(locator) {
    const s = normalizeLocatorStatus(locator);
    const paperId = locator.paper_id || "";
    const page = locator.page;
    const bbox = locator.bbox;
    const evidenceText = locator.evidence_text || "";

    if (locatorCanJump(locator)) {
        var uid = "loc-act-detail-" + (locator.id || Math.random().toString(36).slice(2));
        var bboxDataAttr = bbox ? ('data-bbox="' + escAttr(JSON.stringify(bbox)) + '"') : "";
        return '<span id="' + uid + '" data-paper-id="' + esc(paperId) + '" data-page="' + (page || 0) + '" data-has-bbox="' + (bbox ? "true" : "false") + '" data-locator-status="exact_page" data-evidence-text="' + escAttr(ellipsis(evidenceText, 80)) + '" ' + bboxDataAttr + ' style="display:none;"></span>' +
            '<button class="btn primary small" onclick="triggerDetailLocatorAction(\'' + uid + '\')">跳转到第 ' + (page || "?") + ' 页</button>';
    } else {
        return '<span style="font-size:12px;color:var(--color-text-secondary);">' + locatorDegradedText(locator) + '</span>';
    }
}

function buildPdfJumpButtonHtml(options) {
    options = options || {};
    var paperId = options.paperId || "";
    var page = Number(options.page || 0);
    if (!paperId || !page) return "";
    var uid = "pdf-jump-" + Math.random().toString(36).slice(2);
    var evidenceText = options.evidenceText || "";
    var stopPrefix = options.stopPropagation ? "event.stopPropagation(); " : "";
    var label = options.label || ("跳转到第 " + page + " 页");
    return '<span id="' + uid + '" data-paper-id="' + escAttr(paperId) + '" data-page="' + page + '" data-has-bbox="false" data-locator-status="exact_page" data-evidence-text="' + escAttr(evidenceText) + '" style="display:none;"></span>' +
        '<button class="btn ghost small" type="button" onclick="' + stopPrefix + 'triggerDetailLocatorAction(\'' + uid + '\')">' + esc(label) + '</button>';
}

function renderEvidenceLocators(locators) {
    const panel = $("evidenceLocatorsPanel");
    if (!panel) return;

    if (!locators || locators._error) {
        if (locators && locators._error) {
            panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted" style="color:var(--color-warning);">证据定位暂不可用</div><div class="muted" style="margin-top:8px;">请稍后重试；如果当前文献没有 PDF，也无法执行页码跳转。</div></div>';
        } else {
            panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted">暂无可定位证据</div><div class="muted" style="margin-top:8px;">可能原因：未上传 PDF、尚未生成页码定位，或当前只有证据文本没有精确页码。</div></div>';
        }
        return;
    }

    if (!Array.isArray(locators) || locators.length === 0) {
        panel.innerHTML = '<div class="muted">暂无可定位证据</div><div class="muted" style="margin-top:8px;">可能原因：未上传 PDF、尚未生成页码定位，或当前只有证据文本没有精确页码。</div>';
        return;
    }

    var html = '';
    locators.forEach(function(loc, idx) {
        html += '<div class="section-card" style="margin-bottom:8px;padding:10px;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">' +
                '<span style="font-size:12px;font-weight:700;color:var(--color-primary);">' + esc(ellipsis(loc.evidence_text || "无证据文本", 80)) + '</span>' +
                locatorStatusBadge(loc.locator_status) +
            '</div>' +
            '<div style="display:grid;grid-template-columns:80px 1fr;gap:2px 8px;font-size:12px;margin-bottom:6px;">' +
                '<span class="muted">页码</span><span>' + esc(loc.page != null ? loc.page : "-") + '</span>' +
                '<span class="muted">来源类型</span><span>' + esc(uiLabel("source", loc.source_type || "-")) + '</span>' +
                '<span class="muted">章节</span><span>' + esc(loc.section || "-") + '</span>' +
                '<span class="muted">目标类型</span><span>' + esc(loc.target_type || "-") + '</span>' +
                '<span class="muted">字段</span><span>' + esc(loc.field_name || "-") + '</span>' +
                '<span class="muted">定位置信度</span><span>' + esc(loc.locator_confidence != null ? Number(loc.locator_confidence).toFixed(2) : "-") + '</span>' +
                (loc.warning_reason ? '<span class="muted" style="color:var(--color-warning);">警告</span><span style="color:var(--color-warning);">' + esc(loc.warning_reason === 'bbox unavailable' ? '具体坐标不可用' : loc.warning_reason) + '</span>' : '') +
            '</div>' +
            '<div>' + locatorActionHtml(loc) + '</div>' +
        '</div>';
    });
    panel.innerHTML = html;
}

async function loadEvidenceLocators(paperId, options) {
    const opts = options || {};
    let result;
    try {
        const entry = await fetchPaperResource(
            paperId,
            "evidence/locators",
            LOCATORS_VARIANT,
            API_BASE + "/" + encodeURIComponent(paperId) + "/evidence/locators",
            { forceRefresh: opts.forceRefresh === true }
        );
        result = entry.value;
    } catch (error) {
        if (!opts.silent) console.warn("Evidence locators are not available:", error);
        result = { _error: true, message: error && error.message ? error.message : "Evidence locators unavailable" };
    }
    if (state.selectedPaperId !== paperId) return result;
    state.selectedPaperEvidenceLocators = result;
    renderEvidenceLocators(result);
    return result;
}

function triggerDetailLocatorAction(uid) {
    var el = document.getElementById(uid);
    if (!el) return;
    var paperId = el.getAttribute("data-paper-id") || "";
    var page = parseInt(el.getAttribute("data-page") || "0", 10);
    var hasBbox = el.getAttribute("data-has-bbox") === "true";
    var locatorStatus = el.getAttribute("data-locator-status") || "";
    var evidenceText = el.getAttribute("data-evidence-text") || "";
    var bbox = null;
    try { bbox = JSON.parse(el.getAttribute("data-bbox") || "null"); } catch (_) {}
    openPdfViewer(paperId, page, hasBbox, bbox, locatorStatus, evidenceText);
}

async function openPdfViewer(paperId, page, hasBbox, bboxOrJson, locatorStatus, evidenceText) {
    var overlay = $("pdfViewerOverlay");
    if (!overlay) return;

    locatorStatus = normalizeLocatorStatus(locatorStatus);
    if (locatorStatus !== "exact_page") {
        return;
    }

    var bbox = null;
    if (typeof bboxOrJson === "string" && bboxOrJson) {
        try { bbox = JSON.parse(bboxOrJson); } catch (_) { bbox = null; }
    } else if (typeof bboxOrJson === "object" && bboxOrJson !== null) {
        bbox = bboxOrJson;
    }

    document.body.classList.add("pdf-viewer-open");
    overlay.style.display = "flex";

    var iframe = $("pdfViewerIframe");
    var highlightOverlay = $("pdfHighlightOverlay");
    var viewerTitle = $("pdfViewerTitle");
    var viewerStatus = $("pdfViewerStatus");
    var viewerPageIndicator = $("pdfViewerPageIndicator");
    var viewerEvidencePanel = $("pdfViewerEvidencePanel");
    var viewerPdfUnavailable = $("pdfViewerUnavailable");
    var viewerPdfContent = $("pdfViewerContent");

    if (viewerTitle) viewerTitle.textContent = "PDF 预览 - 文献 " + paperId.slice(0, 8);
    if (viewerStatus) viewerStatus.textContent = "加载中...";
    if (viewerPageIndicator) viewerPageIndicator.textContent = "目标页码：" + Math.max(1, page || 1);

    // Hide unavailable message, show content area
    if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "none";
    if (viewerPdfContent) viewerPdfContent.style.display = "block";

    var pdfUrl = "/api/papers/" + encodeURIComponent(paperId) + "/pdf";

    // Build evidence panel content
    var evidenceHtml = "";
    evidenceHtml = '<div style="font-size:12px;margin-bottom:4px;font-weight:700;color:var(--color-primary);">PDF 页码定位</div>' +
        '<div style="font-size:11px;color:var(--color-text-secondary);">这里用于查看原文页和核对证据。浏览器 PDF 工具栏里的临时高亮/绘制不会写回系统；需要保存结论时，请通过审核中心或 import_analysis 回写。</div>' +
        '<div style="margin-top:8px;"><a class="btn ghost small" target="_blank" rel="noopener" href="' + escAttr(pdfUrl) + '#page=' + Math.max(1, page || 1) + '">新窗口打开 PDF</a></div>' +
        (evidenceText ? '<div style="font-size:11px;margin-top:6px;padding:6px 8px;background:var(--color-surface-alt);border-radius:var(--radius);border:1px solid var(--color-border);">"' + esc(evidenceText) + '"</div>' : '');
    if (viewerEvidencePanel) viewerEvidencePanel.innerHTML = evidenceHtml;

    // Load the PDF immediately. A slow HEAD probe must never block preview.
    if (iframe) {
        iframe.onload = function() {
            if (viewerStatus && overlay.style.display !== "none") viewerStatus.textContent = "";
        };
        iframe.onerror = function() {
            if (viewerStatus && overlay.style.display !== "none") viewerStatus.textContent = "PDF 加载失败，可尝试新窗口打开";
        };
        iframe.src = pdfUrl + "#page=" + Math.max(1, page || 1) + "&toolbar=0&navpanes=0";
        iframe.style.display = "block";
    }

    var slowTimer = window.setTimeout(function() {
        if (viewerStatus && overlay.style.display !== "none" && viewerStatus.textContent === "加载中...") {
            viewerStatus.textContent = "PDF 加载较慢，可尝试新窗口打开";
        }
    }, 6000);

    // Probe PDF availability with a short timeout for clearer failure messages.
    try {
        var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
        var timeoutId = controller ? window.setTimeout(function() { controller.abort(); }, 4000) : null;
        var probeResp = await fetch(pdfUrl, {
            method: "HEAD",
            signal: controller ? controller.signal : undefined
        });
        if (timeoutId) window.clearTimeout(timeoutId);
        if (!probeResp.ok && probeResp.status !== 405) {
            if (viewerPdfContent) viewerPdfContent.style.display = "none";
            if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "block";
            if (viewerStatus) viewerStatus.textContent = "PDF 尚未上传或不可预览";
            if (iframe) iframe.src = "";
            return;
        }
        if (viewerStatus && overlay.style.display !== "none" && viewerStatus.textContent === "加载中...") {
            viewerStatus.textContent = "";
        }
    } catch (_) {
        if (viewerStatus && overlay.style.display !== "none" && viewerStatus.textContent === "加载中...") {
            viewerStatus.textContent = "PDF 请求较慢，已继续尝试加载";
        }
    } finally {
        window.clearTimeout(slowTimer);
    }

    // Clear highlight overlay (bbox positioning over iframe is unreliable;
    // evidence info is shown in the side panel instead)
    if (highlightOverlay) highlightOverlay.innerHTML = "";
}

function closePdfViewer() {
    var overlay = $("pdfViewerOverlay");
    if (overlay) overlay.style.display = "none";
    document.body.classList.remove("pdf-viewer-open");
    var iframe = $("pdfViewerIframe");
    if (iframe) {
        iframe.onload = null;
        iframe.onerror = null;
        iframe.src = "";
    }
}
