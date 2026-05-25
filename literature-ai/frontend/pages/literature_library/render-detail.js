function renderWorkspaceHeader(paper) {
    const counts = paper.counts || {};
    const titleEl = $("paperTitle");
    const metaEl = $("paperMeta");
    const badgesEl = $("paperHeaderBadges");
    const topicEl = $("writerTopic");
    if (titleEl) titleEl.textContent = paper.title || "未命名文献";
    if (metaEl) {
        metaEl.innerHTML = [
            esc(paper.year || "-"),
            esc(paper.journal || "-"),
            esc(paper.paper_type ? paper.paper_type : "未知类型"),
            renderDoiMeta(paper.doi)
        ].join(" | ");
    }
    const pdfBtn = $("pdfEvidenceHeaderBtn");
    if (pdfBtn) pdfBtn.textContent = paperHasPdf(paper) ? "查看 PDF / 证据定位" : "PDF 未上传";
    if (badgesEl) {
        badgesEl.innerHTML =
            (paper.serial_number ? '<span class="serial-chip">' + formatSerialNumber(paper.serial_number) + "</span>" : "") +
            paperStatusChip(paper) +
            badge(counts.sections) +
            badge(counts.figures) +
            badge(counts.dft_results) +
            badge(counts.mechanism_claims) +
            badge(counts.writing_cards);
    }
    if (topicEl) topicEl.value = paper.title || "";
}

function renderListBlock(title, items, formatter) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    return items.map(function(item, index) {
        return '<div class="section-card"><h3>' + esc(title) + " " + (items.length > 1 ? (index + 1) : "") + '</h3>' + formatter(item) + "</div>";
    }).join("");
}

function renderJSONCards(title, items) {
    return renderListBlock(title, items, function(item) {
        return '<div class="mono">' + esc(JSON.stringify(item, null, 2)) + "</div>";
    });
}

// ── G3B Evidence Locator Rendering ──

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

function renderEvidenceLocators(locators) {
    const panel = $("evidenceLocatorsPanel");
    if (!panel) return;

    if (!locators || locators._error) {
        if (locators && locators._error) {
            panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted" style="color:var(--color-warning);">证据定位暂不可用</div></div>';
        } else {
            panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted">暂无可定位证据</div></div>';
        }
        return;
    }

    if (!Array.isArray(locators) || locators.length === 0) {
        panel.innerHTML = '<div class="section-card"><h3>PDF 证据定位</h3><div class="muted">暂无可定位证据</div></div>';
        return;
    }

    var html = '<div class="section-card"><h3>PDF 证据定位</h3>';
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
                (loc.warning_reason ? '<span class="muted" style="color:var(--color-warning);">警告</span><span style="color:var(--color-warning);">' + esc(loc.warning_reason) + '</span>' : '') +
            '</div>' +
            '<div>' + locatorActionHtml(loc) + '</div>' +
        '</div>';
    });
    html += '</div>';
    panel.innerHTML = html;
}

async function loadEvidenceLocators(paperId) {
    var result = await fetchPaperEvidenceLocators(paperId);
    renderEvidenceLocators(result);
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

    // Build evidence panel content
    var evidenceHtml = "";
    evidenceHtml = '<div style="font-size:12px;margin-bottom:4px;font-weight:700;color:var(--color-primary);">PDF 页码定位</div>' +
        '<div style="font-size:11px;color:var(--color-text-secondary);">已跳转到证据页；当前版本不提供 PDF 页面内框选。</div>' +
        (evidenceText ? '<div style="font-size:11px;margin-top:6px;padding:6px 8px;background:var(--color-surface-alt);border-radius:var(--radius);border:1px solid var(--color-border);">"' + esc(evidenceText) + '"</div>' : '');
    if (viewerEvidencePanel) viewerEvidencePanel.innerHTML = evidenceHtml;

    // Probe PDF availability with HEAD request
    var pdfUrl = "/api/papers/" + encodeURIComponent(paperId) + "/pdf";
    try {
        var probeResp = await fetch(pdfUrl, { method: "HEAD" });
        if (!probeResp.ok) {
            // PDF not available
            if (viewerPdfContent) viewerPdfContent.style.display = "none";
            if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "block";
            if (viewerStatus) viewerStatus.textContent = "PDF 尚未上传或不可预览";
            return;
        }
    } catch (_) {
        // Network error
        if (viewerPdfContent) viewerPdfContent.style.display = "none";
        if (viewerPdfUnavailable) viewerPdfUnavailable.style.display = "block";
        if (viewerStatus) viewerStatus.textContent = "PDF 请求失败";
        return;
    }

    if (viewerStatus) viewerStatus.textContent = "";

    // Load PDF in iframe with page fragment
    if (iframe) {
        iframe.src = pdfUrl + "#page=" + Math.max(1, page || 1);
        iframe.style.display = "block";
    }

    // Clear highlight overlay (bbox positioning over iframe is unreliable;
    // evidence info is shown in the side panel instead)
    if (highlightOverlay) highlightOverlay.innerHTML = "";
}

function closePdfViewer() {
    var overlay = $("pdfViewerOverlay");
    if (overlay) overlay.style.display = "none";
    var iframe = $("pdfViewerIframe");
    if (iframe) iframe.src = "";
}

function renderDetail(detail, audit) {
    const counts = detail.counts || {};
    const summaryCards =
        '<div class="cards">' +
            '<div class="stat-card"><h3>章节</h3><div class="value">' + (counts.sections || 0) + "</div></div>" +
            '<div class="stat-card"><h3>表格</h3><div class="value">' + (counts.tables || 0) + "</div></div>" +
            '<div class="stat-card"><h3>图片</h3><div class="value">' + (counts.figures || 0) + "</div></div>" +
            '<div class="stat-card"><h3>DFT 结果</h3><div class="value">' + (counts.dft_results || 0) + "</div></div>" +
            '<div class="stat-card"><h3>机理</h3><div class="value">' + (counts.mechanism_claims || 0) + "</div></div>" +
            '<div class="stat-card"><h3>写作卡</h3><div class="value">' + (counts.writing_cards || 0) + "</div></div>" +
        "</div>";

    const baseInfo =
        '<div class="section-card"><h3>基础信息</h3>' +
            '<div class="inline-grid">' +
                '<div class="key-value"><div class="k">文献库</div><div class="v">' + esc(detail.library_name || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">文献类型</div><div class="v">' + esc(detail.paper_type || "未知") + (detail.type_confidence ? ' (置信度 ' + detail.type_confidence + ')' : '') + '</div></div>' +
                '<div class="key-value"><div class="k">分类来源</div><div class="v">' + esc(detail.classification_source || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">创建时间</div><div class="v">' + esc(formatDate(detail.created_at)) + '</div></div>' +
                '<div class="key-value"><div class="k">PDF 路径</div><div class="v">' + esc(detail.pdf_path || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">Markdown 路径</div><div class="v">' + esc(detail.markdown_path || "-") + '</div></div>' +
            "</div>" +
        "</div>";

    const abstractCard =
        '<div class="section-card"><h3>摘要</h3><div class="prewrap">' + esc(detail.abstract || "暂无摘要。") + "</div></div>";

    const comprehensiveCard =
        '<div class="section-card"><h3>综合解析</h3><div class="mono">' +
            esc(JSON.stringify(detail.comprehensive_analysis || {}, null, 2)) +
        "</div></div>";

    const sectionCards = renderListBlock("正文节选", detail.sections ? detail.sections.slice(0, 8) : [], function(item) {
        return (
            '<div class="subtle">标题：' + esc(item.section_title || item.section_type || "未命名章节") + "</div>" +
            '<div class="prewrap" style="margin-top:8px;">' + esc(ellipsis(item.text || "", 2200) || "暂无文本。") + "</div>"
        );
    });

    let figureCards = "";
    if (detail.figures && detail.figures.length) {
        const roles = new Set();
        detail.figures.forEach(f => {
            if (f.figure_role) roles.add(f.figure_role);
        });
        
        let filterHtml = "";
        if (roles.size > 0) {
            filterHtml += '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">';
            filterHtml += '<button class="btn small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display=\'block\')">全部</button>';
            roles.forEach(role => {
                filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.role === \'' + esc(role) + '\' ? \'block\' : \'none\')">' + esc(role) + '</button>';
            });
            filterHtml += '</div>';
        }
        
        function extractFigureNumber(caption) {
            if (!caption) return null;
            var m = caption.match(/(?:Figure|Fig\.?|Scheme)\s*(\d+)/i);
            return m ? parseInt(m[1], 10) : null;
        }

        const cardsHtml = detail.figures.slice(0, 15).map(function(item, index) {
            let imgHtml = "";
            if (item.image_path) {
                imgHtml = '<div style="margin-top: 12px; text-align: center;"><img src="/api/papers/assets/' + esc(item.image_path) + '" style="max-width: 100%; max-height: 400px; border: 1px solid var(--color-border); border-radius: var(--radius-sm); object-fit: contain;" alt="提取的文献图片" /></div>';
            }

            let metaHtml = "";
            if (item.figure_role) {
                metaHtml += '<span class="status-chip" style="background: var(--color-primary-bg); color: var(--color-text-secondary); margin-right: 8px;">' + esc(item.figure_role) + (item.role_confidence ? ' (' + (item.role_confidence*100).toFixed(0) + '%)' : '') + '</span>';
            }
            if (item.key_elements && item.key_elements.length) {
                item.key_elements.forEach(el => {
                    metaHtml += '<span class="status-chip meta" style="margin-right: 4px;">' + esc(el) + '</span>';
                });
            }
            if (metaHtml) {
                metaHtml = '<div style="margin-top: 8px;">' + metaHtml + '</div>';
            }

            let summaryHtml = "";
            if (item.content_summary) {
                summaryHtml = '<div class="subtle" style="margin-top: 8px; font-weight: 500;">' + esc(item.content_summary) + '</div>';
            }

            var figNum = extractFigureNumber(item.caption);
            var figLabel = figNum !== null ? '图片 ' + figNum : '图片 ' + (index + 1);

            return '<div class="section-card figure-card" data-role="' + esc(item.figure_role || 'unknown') + '"><h3>' + figLabel + '</h3>' +
                   '<div class="prewrap">' + esc(item.caption || "无 caption") + "</div>" +
                   summaryHtml + metaHtml + imgHtml + '</div>';
        }).join("");
        
        figureCards = filterHtml + cardsHtml;
    } else {
        figureCards = '<div class="section-card"><h3>图片</h3><div class="muted">暂无内容。</div></div>';
    }

    const pdfEvidenceEntry =
        '<div class="section-card pdf-evidence-entry"><h3>PDF 证据定位</h3>' +
            '<p>当前版本只在有精确页码时跳转到 PDF 页，并显示证据信息。</p>' +
            '<p class="subtle">请使用标题右侧的“' + (paperHasPdf(detail) ? '查看 PDF / 证据定位' : 'PDF 未上传') + '”入口。</p>' +
        '</div>';

    const referenceCards = renderListBlock("参考文献", detail.references ? detail.references.slice(0, 20) : [], function(item) {
        return (
            '<div class="prewrap">' + esc(item.title || "未命名参考文献") + "</div>" +
            '<div class="subtle" style="margin-top:8px;">作者：' + esc(item.authors || "-") + " | DOI：" + esc(item.doi || "-") + "</div>" +
            (item.citation_context ? '<div class="mono" style="margin-top:8px;">' + esc(item.citation_context) + "</div>" : "")
        );
    });

    const summaryEl = $("summaryContent");
    const sectionsEl = $("sectionsContent");
    const figuresEl = $("figuresContent");
    const dftEl = $("dftContent");
    const writingEl = $("writingContent");
    const aggregateEl = $("aggregateResult");
    
    let missingPdfBanner = "";
    if (detail.oa_status === "metadata_only") {
        missingPdfBanner = 
            '<div class="section-card" style="border: 1px dashed var(--color-warning); background: var(--color-warning-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-warning); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 尚无 PDF' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '当前文献仅包含元数据（标题、期刊、年份、DOI 等），尚未上传或关联实际 PDF 文献文件。' +
                '</p>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<button class="btn primary small" onclick="document.getElementById(\'attachPdfInput\').click()">上传 PDF 并自动合并</button>' +
                    '<input id="attachPdfInput" type="file" accept=".pdf" style="display: none;" onchange="attachPDFToPaperDetail(this, \'' + detail.id + '\')">' +
                '</div>' +
            '</div>';
    }

    let auditBanner = "";
    const reviewTabEl = $("tab-review");
    const existingWarning = $("reviewTabAuditWarning");
    if (existingWarning) existingWarning.remove();
    
    if (audit && (audit.stale > 0 || audit.ambiguous > 0 || audit.unresolved > 0)) {
        const totalAlerts = (audit.stale || 0) + (audit.ambiguous || 0) + (audit.unresolved || 0);
        auditBanner = 
            '<div class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<a class="btn primary small" style="text-decoration: none; display: inline-flex; align-items: center;" href="/pages/external_analysis_workbench/index.html?paper_id=' + encodeURIComponent(detail.id) + '">立即核对 (去工作台)</a>' +
                '</div>' +
            '</div>';
        
        const reviewTabWarningHtml = 
            '<div id="reviewTabAuditWarning" class="section-card" style="border: 1px dashed var(--color-danger); background: var(--color-danger-bg); padding: 18px; border-radius: var(--radius-lg); margin-bottom: 16px;">' +
                '<h3 style="color: var(--color-danger); display: flex; align-items: center; gap: 8px; font-size: 15px; margin-bottom: 6px; font-weight: 800;">' +
                    '⚠️ 人工校验需要重新确认' +
                '</h3>' +
                '<p style="color: var(--color-text); font-size: 13px; margin-bottom: 12px; line-height: 1.6;">' +
                    '该文献有 ' + totalAlerts + ' 条人工校验记录需要重新确认（已失效 ' + (audit.stale || 0) + '，有歧义 ' + (audit.ambiguous || 0) + '，未解析 ' + (audit.unresolved || 0) + '）。' +
                '</p>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<a class="btn primary small" style="text-decoration: none; display: inline-flex; align-items: center;" href="/pages/external_analysis_workbench/index.html?paper_id=' + encodeURIComponent(detail.id) + '">立即核对 (去工作台)</a>' +
                '</div>' +
            '</div>';
        if (reviewTabEl) {
            reviewTabEl.insertAdjacentHTML("afterbegin", reviewTabWarningHtml);
        }
    }
    
    if (summaryEl) {
        summaryEl.innerHTML =
            missingPdfBanner +
            pdfEvidenceEntry +
            auditBanner +
            summaryCards +
            baseInfo +
            abstractCard +
            comprehensiveCard +
            '<div id="evidenceLocatorsPanel"></div>';
    }
    if (sectionsEl) {
        sectionsEl.innerHTML =
            sectionCards +
            referenceCards +
            renderJSONCards("出向关系", detail.outgoing_relationships || []) +
            renderJSONCards("入向关系", detail.incoming_relationships || []);
    }
    if (figuresEl) {
        figuresEl.innerHTML =
            figureCards +
            renderJSONCards("表格", detail.tables || []);
    }
    if (dftEl) {
        dftEl.innerHTML =
            renderJSONCards("DFT 设置", detail.dft_settings_items || []) +
            renderJSONCards("催化剂样本", detail.catalyst_samples_items || []) +
            renderJSONCards("DFT 结果", detail.dft_results_items || []) +
            renderJSONCards("电化学性能", detail.electrochemical_performance_items || []) +
            renderJSONCards("机理声明", detail.mechanism_claims_items || []);
    }
    if (writingEl) {
        writingEl.innerHTML =
            renderJSONCards("写作卡片", detail.writing_cards_items || []);
    }
    if (aggregateEl) aggregateEl.innerHTML = "";
}

function renderDetailSkeleton() {
    const detailContainer = $("summaryContent");
    if (!detailContainer) return;
    ["sectionsContent", "figuresContent", "dftContent", "writingContent", "writerResult", "externalRuns", "aggregateResult"].forEach(function(id) {
        const el = $(id);
        if (el) el.innerHTML = "";
    });
    detailContainer.innerHTML = 
        '<div class="skeleton-detail">' +
            '<div class="skeleton-row">' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
            '</div>' +
            '<div class="skeleton skeleton-info" style="margin-top:14px; margin-bottom:14px;"></div>' +
            '<div class="section-card">' +
                '<div class="skeleton skeleton-text long"></div>' +
                '<div class="skeleton skeleton-text medium"></div>' +
                '<div class="skeleton skeleton-text long"></div>' +
                '<div class="skeleton skeleton-text short"></div>' +
            '</div>' +
        '</div>';
}

async function loadPaperDetail(paperId) {
    if (!paperId) {
        showEmptyWorkspace();
        return;
    }
    try {
        renderDetailSkeleton();
        const detail = await fetchJSON(API_BASE + "/" + paperId);
        state.selectedPaperId = paperId;
        state.selectedPaper = detail;
        
        let audit = null;
        try {
            audit = await fetchJSON("/api/extraction/results/" + encodeURIComponent(paperId) + "/reviews/audit");
        } catch (e) {
            console.warn("Audit API is not available or failed:", e);
        }
        
        renderPaperList();
        renderWorkspaceHeader(detail);
        renderDetail(detail, audit);
        showWorkspace();
        syncQueryParams();
        loadEvidenceLocators(paperId);
        if (state.currentTab === "review") loadExternalRuns();
        if (state.currentTab === "aggregate") loadAggregate();
        if (state.currentTab === "writer") ensureWriterStatus();
    } catch (error) {
        showToast("详情加载失败：" + error.message, "error");
    }
}

function openPaperDetailPage() {
    if (!state.selectedPaperId) return;
    window.open("/pages/paper_detail/index.html?id=" + encodeURIComponent(state.selectedPaperId), "_blank");
}

function copyPaperIdentity() {
    if (!state.selectedPaper) return;
    const value = [state.selectedPaper.title || "", state.selectedPaper.doi || ""].filter(Boolean).join("\n");
    navigator.clipboard.writeText(value).then(function() {
        showToast("已复制标题和 DOI。", "success");
    }).catch(function() {
        showToast("复制失败，请手动复制。", "error");
    });
}
