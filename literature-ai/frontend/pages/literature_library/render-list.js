function renderPaperList() {
    const container = $("paperList");
    const meta = $("paperListMeta");
    if (meta) {
        const total = Number(state.currentLibraryTotal || 0);
        const shown = state.papers.length;
        const rangeStart = shown ? state.currentOffset + 1 : 0;
        const rangeEnd = state.currentOffset + shown;
        meta.textContent = total
            ? "全库 " + total + " 篇 · 本页 " + rangeStart + "-" + rangeEnd
            : "本页 " + shown + " 篇";
    }
    if (!container) return;
    if (!state.papers.length) {
        container.innerHTML = '<div class="list-empty">当前条件下没有文献</div>';
        if (state.currentLibraryTotal === 0) {
            renderLibraryEmptyState();
        } else {
            renderNoSelectionState();
        }
        return;
    }
    const tbodyHtml = state.papers.map(function(paper, idx) {
        const active = paper.id === state.selectedPaperId ? " active" : "";
        const titleLine = esc(paper.title_zh || paper.title || "未命名文献");
        const originalTitle = paper.title_zh && paper.title ? '<div class="paper-original-title" style="margin-top:2px;" title="' + esc(paper.title) + '">' + esc(paper.title) + '</div>' : '';
        const metaLine = esc(paper.journal || "未知期刊") + ' | ' + esc(paperTypeLabel(paper.paper_type)) + (paper.doi ? ' | <span class="mono" title="DOI">DOI: ' + esc(paper.doi) + '</span>' : '');
        
        return (
            '<tr class="paper-row' + active + '" onclick="selectPaperById(\'' + paper.id + '\')">' +
                '<td style="text-align:center; color:var(--color-text-secondary);">' + (paper.serial_number ? formatSerialNumber(paper.serial_number) : (idx + 1)) + '</td>' +
                '<td style="text-align:center; color:var(--color-text-secondary);">' + esc(paper.year || "-") + '</td>' +
                '<td>' +
                    '<div class="paper-title" title="' + esc(paper.title || "未命名文献") + '">' + titleLine + '</div>' +
                    originalTitle +
                    '<div class="paper-meta" style="margin-top:4px;">' + metaLine + '</div>' +
                '</td>' +
                '<td>' +
                    '<div style="display:flex; flex-direction:column; gap:4px; align-items:flex-start;">' +
                        paperStatusChip(paper) + 
                        (paper.pdf_quality_status ? '<span class="status-chip ' + (paper.pdf_quality_status === 'Good' ? 'full' : 'meta') + '">' + esc(paper.pdf_quality_status) + '</span>' : '') +
                        (paper.dft_completeness_status ? '<span class="status-chip ' + (paper.dft_completeness_status === 'DB_Ready' ? 'full' : 'meta') + '">' + esc(paper.dft_completeness_status) + '</span>' : '') +
                    '</div>' +
                '</td>' +
                '<td>' +
                    '<div class="paper-meta" style="display:grid; grid-template-columns: 1fr 1fr; gap:4px; font-size:12px;">' +
                        '<span title="线索/章节数量">章节: <strong>' + (paper.counts && paper.counts.sections ? paper.counts.sections : 0) + '</strong></span>' +
                        '<span title="解析图表数量">图表: <strong>' + (paper.counts && paper.counts.figures ? paper.counts.figures : 0) + '</strong></span>' +
                        '<span title="DFT提取数据">DFT: <strong>' + (paper.counts && paper.counts.dft_results ? paper.counts.dft_results : 0) + '</strong></span>' +
                        '<span title="关联写作卡片">写作卡: <strong>' + (paper.counts && paper.counts.writing_cards ? paper.counts.writing_cards : 0) + '</strong></span>' +
                    '</div>' +
                '</td>' +
            '</tr>'
        );
    }).join("");

    container.innerHTML = '<table class="paper-table">' +
        '<thead><tr>' +
            '<th style="width:50px; text-align:center;">#</th>' +
            '<th style="width:60px; text-align:center;">年份</th>' +
            '<th style="width:35%;">文献标题</th>' +
            '<th style="width:160px;">流程与质量</th>' +
            '<th>DFT 审计</th>' +
        '</tr></thead>' +
        '<tbody>' + tbodyHtml + '</tbody>' +
    '</table>';
}

function normalizePaperTypeLabel(value) {
    const raw = String(value || "").trim();
    if (!raw || raw.toLowerCase() === "unknown") return "未知类型";
    return raw;
}

function renderPaperListSkeleton() {
    const container = $("paperList");
    if (!container) return;
    let skeletonHtml = "";
    for (let i = 0; i < 5; i++) {
        skeletonHtml +=
            '<div class="skeleton-card">' +
                '<div class="skeleton skeleton-title"></div>' +
                '<div class="skeleton skeleton-meta"></div>' +
                '<div class="skeleton skeleton-badge"></div>' +
            '</div>';
    }
    container.innerHTML = skeletonHtml;
}
