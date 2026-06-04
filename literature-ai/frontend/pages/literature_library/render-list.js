function libraryQualityLabel(paper) {
    const status = String((paper && paper.pdf_quality_status) || "").trim();
    const mapping = {
        A_text_readable: "A 可直接读",
        B_text_partial: "B 部分可读",
        C_scan_clear: "C 扫描清晰",
        D_scan_unclear: "D 扫描不清",
        Broken: "文件异常"
    };
    return mapping[status] || "质量待检";
}

function libraryQualityChipClass(paper) {
    const status = String((paper && paper.pdf_quality_status) || "").trim();
    if (status === "A_text_readable") return "parsed";
    if (status === "B_text_partial" || status === "C_scan_clear") return "meta";
    if (status === "D_scan_unclear" || status === "Broken") return "failed";
    return "none";
}

function paperYearSortValue(paper) {
    const year = Number(paper && paper.year);
    return Number.isFinite(year) && year > 0 ? year : 999999;
}

function paperSerialSortValue(paper) {
    const serial = Number(paper && paper.serial_number);
    return Number.isFinite(serial) && serial > 0 ? serial : 999999;
}

function paperFileSizeBytes(paper) {
    const metrics = paper && paper.pdf_quality_report && paper.pdf_quality_report.metrics;
    const bytes = Number(metrics && metrics.file_size_bytes);
    return Number.isFinite(bytes) && bytes > 0 ? bytes : 0;
}

function paperPageCount(paper) {
    const metrics = paper && paper.pdf_quality_report && paper.pdf_quality_report.metrics;
    const pages = Number(metrics && metrics.page_count);
    return Number.isFinite(pages) && pages > 0 ? pages : 0;
}

function formatFileSize(bytes) {
    if (!bytes || bytes <= 0) return "大小待补";
    if (bytes >= 1024 * 1024) {
        const mb = bytes / (1024 * 1024);
        return mb >= 10 ? Math.round(mb) + " MB" : mb.toFixed(1) + " MB";
    }
    if (bytes >= 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
    return bytes + " B";
}

function paperDoiPreview(paper) {
    return paper && paper.doi ? paper.doi : "无 DOI";
}

function paperYearLabel(paper) {
    return paper && paper.year ? String(paper.year) : "年份待补";
}

function renderLibraryMetrics() {
    const metricsEl = $("libraryMetrics");
    if (!metricsEl) return;
    const papers = Array.isArray(state.papers) ? state.papers.slice() : [];
    const total = Number(state.currentLibraryTotal || papers.length || 0);
    const readable = papers.filter(function(paper) {
        return ["A_text_readable", "B_text_partial"].includes(String((paper && paper.pdf_quality_status) || ""));
    }).length;
    const withDft = papers.filter(function(paper) {
        return Number(paper && paper.counts && paper.counts.dft_results || 0) > 0;
    }).length;
    const yearValues = papers
        .map(function(paper) { return Number(paper && paper.year); })
        .filter(function(year) { return Number.isFinite(year) && year > 0; });
    const yearText = yearValues.length
        ? (Math.min.apply(null, yearValues) + " - " + Math.max.apply(null, yearValues))
        : "待补";
    metricsEl.innerHTML = [
        ["当前页文献", papers.length],
        ["库内总数", total],
        ["可直接读", readable],
        ["含 DFT", withDft],
        ["年份范围", yearText]
    ].map(function(item) {
        return '<div class="library-metric"><span>' + esc(item[0]) + '</span><strong>' + esc(item[1]) + '</strong></div>';
    }).join("");
}

function renderPaperGroupTitle(label, count) {
    return (
        '<div class="paper-year-group">' +
            '<div class="paper-year-heading">' +
                '<span class="paper-year-heading-label">' + esc(label) + '</span>' +
                '<span class="paper-year-heading-count">' + esc(count) + ' 篇</span>' +
            '</div>' +
        '</div>'
    );
}

function renderPaperCard(paper) {
    const active = paper.id === state.selectedPaperId ? " active" : "";
    const fileSize = formatFileSize(paperFileSizeBytes(paper));
    const pageCount = paperPageCount(paper);
    const dftCount = Number(paper && paper.counts && paper.counts.dft_results || 0);
    const figureCount = Number(paper && paper.counts && paper.counts.figures || 0);
    const writingCount = Number(paper && paper.counts && paper.counts.writing_cards || 0);
    return (
        '<div class="paper-card' + active + '" onclick="selectPaperById(\'' + paper.id + '\')">' +
            '<div class="paper-row-head">' +
                (paper.serial_number ? '<span class="serial-chip">' + esc(formatSerialNumber(paper.serial_number)) + '</span>' : "") +
                '<span class="paper-status-inline">' + paperStatusChip(paper) + '</span>' +
            '</div>' +
            '<div class="paper-title">' + esc(paper.title_zh || paper.title || "未命名文献") + '</div>' +
            (paper.title_zh && paper.title ? '<div class="paper-original-title">' + esc(paper.title) + '</div>' : '') +
            '<div class="paper-meta-line">' + esc(paper.journal || "期刊待补") + ' · ' + esc(paperTypeLabel(paper.paper_type)) + '</div>' +
            '<div class="paper-submeta-line">' + esc(paperDoiPreview(paper)) + '</div>' +
            '<div class="paper-support-line">' +
                '<span class="status-chip ' + libraryQualityChipClass(paper) + '">' + esc(libraryQualityLabel(paper)) + '</span>' +
                '<span class="paper-support-item">' + esc(pageCount ? (pageCount + " 页") : "页数待补") + '</span>' +
                '<span class="paper-support-item">' + esc(fileSize) + '</span>' +
            '</div>' +
            '<div class="badge-row compact-badge-row">' +
                '<span class="count-badge ' + (dftCount > 0 ? "has" : "zero") + '">DFT ' + esc(dftCount) + '</span>' +
                '<span class="count-badge ' + (figureCount > 0 ? "has" : "zero") + '">图 ' + esc(figureCount) + '</span>' +
                '<span class="count-badge ' + (writingCount > 0 ? "has" : "zero") + '">写作 ' + esc(writingCount) + '</span>' +
            '</div>' +
        '</div>'
    );
}

function renderPaperList() {
    const container = $("paperList");
    const meta = $("paperListMeta");
    const summary = $("paperListSummary");
    const papers = Array.isArray(state.papers) ? state.papers.slice() : [];
    const sortedPapers = papers.sort(function(a, b) {
        const yearDiff = paperYearSortValue(a) - paperYearSortValue(b);
        if (yearDiff !== 0) return yearDiff;
        const serialDiff = paperSerialSortValue(a) - paperSerialSortValue(b);
        if (serialDiff !== 0) return serialDiff;
        const titleA = String((a && (a.title_zh || a.title)) || "");
        const titleB = String((b && (b.title_zh || b.title)) || "");
        return titleA.localeCompare(titleB, "zh-CN");
    });

    if (meta) meta.textContent = (state.currentLibraryTotal || sortedPapers.length) + " 篇";
    if (summary) summary.textContent = "按年份分组，组内按编号顺序显示。";
    renderLibraryMetrics();

    if (!container) return;
    if (!sortedPapers.length) {
        container.innerHTML = '<div class="list-empty">当前条件下没有文献</div>';
        if (state.currentLibraryTotal === 0) {
            renderLibraryEmptyState();
        } else {
            renderNoSelectionState();
        }
        return;
    }

    const groups = [];
    let currentLabel = null;
    let currentItems = [];
    sortedPapers.forEach(function(paper) {
        const label = paperYearLabel(paper);
        if (label !== currentLabel) {
            if (currentItems.length) {
                groups.push({ label: currentLabel, items: currentItems.slice() });
            }
            currentLabel = label;
            currentItems = [];
        }
        currentItems.push(paper);
    });
    if (currentItems.length) {
        groups.push({ label: currentLabel, items: currentItems.slice() });
    }

    container.innerHTML = groups.map(function(group) {
        return (
            '<section class="paper-group-block">' +
                renderPaperGroupTitle(group.label, group.items.length) +
                '<div class="paper-group-list">' +
                    group.items.map(renderPaperCard).join("") +
                '</div>' +
            '</section>'
        );
    }).join("");
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
    for (let i = 0; i < 4; i++) {
        skeletonHtml +=
            '<section class="paper-group-block">' +
                '<div class="paper-year-heading">' +
                    '<div class="skeleton skeleton-year-title"></div>' +
                '</div>' +
                '<div class="paper-group-list">' +
                    '<div class="skeleton-card">' +
                        '<div class="skeleton skeleton-meta"></div>' +
                        '<div class="skeleton skeleton-title"></div>' +
                        '<div class="skeleton skeleton-meta"></div>' +
                        '<div class="skeleton skeleton-badge"></div>' +
                    '</div>' +
                '</div>' +
            '</section>';
    }
    container.innerHTML = skeletonHtml;
}
