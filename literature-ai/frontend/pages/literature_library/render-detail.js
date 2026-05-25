function renderWorkspaceHeader(paper) {
    const counts = paper.counts || {};
    const titleEl = $("paperTitle");
    const metaEl = $("paperMeta");
    const badgesEl = $("paperHeaderBadges");
    const topicEl = $("writerTopic");
    if (titleEl) titleEl.textContent = paper.title || "未命名文献";
    if (metaEl) {
        metaEl.textContent = [
            paper.year || "-",
            paper.journal || "-",
            paper.paper_type ? paper.paper_type : "未知类型",
            paper.doi ? "DOI: " + paper.doi : "无 DOI"
        ].join(" | ");
    }
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

function renderDetail(detail) {
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
                imgHtml = '<div style="margin-top: 12px; text-align: center;"><img src="/api/papers/assets/' + esc(item.image_path) + '" style="max-width: 100%; max-height: 400px; border: 1px solid var(--color-border); border-radius: var(--radius-sm); object-fit: contain;" alt="Extracted Figure" /></div>';
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
    
    if (summaryEl) {
        summaryEl.innerHTML =
            summaryCards +
            baseInfo +
            abstractCard +
            comprehensiveCard;
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
        renderPaperList();
        renderWorkspaceHeader(detail);
        renderDetail(detail);
        showWorkspace();
        syncQueryParams();
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
