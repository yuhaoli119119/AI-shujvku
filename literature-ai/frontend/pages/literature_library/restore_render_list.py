path = r'D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\frontend\pages\literature_library\render-list.js'

original_content = r'''function libraryQualityLabel(paper) {
    const status = String((paper && paper.pdf_quality_status) || "").trim();
    const mapping = {
        A_text_readable: "A 可读",
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
    return paper && paper.year ? String(paper.year) : "待补";
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
    const yearValues = papers.map(function(paper) {
        return Number(paper && paper.year);
    }).filter(function(year) {
        return Number.isFinite(year) && year > 0;
    });
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

function renderPaperList() {
    const container = $("paperList");
    const meta = $("paperListMeta");
    const summary = $("paperListSummary");
    const papers = Array.isArray(state.papers) ? state.papers.slice() : [];
    const sortedPapers = papers; // 优先信任后端返回顺序，不自己做有冲突的重排
    if (meta) meta.textContent = (state.currentLibraryTotal || sortedPapers.length) + " 篇";
    if (summary) summary.textContent = "按年份排序，优先显示题目、来源、PDF 质量、页数和文件大小。";
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
    
    let currentYear = null;
    let html = "";
    sortedPapers.forEach(function(paper) {
        const yearLabel = paperYearLabel(paper);
        if (yearLabel !== currentYear) {
            currentYear = yearLabel;
            html += '<div class="year-group-header">' + esc(currentYear === "待补" ? "年份待补" : currentYear) + '</div>';
        }
        
        const active = paper.id === state.selectedPaperId ? " active" : "";
        const fileSize = formatFileSize(paperFileSizeBytes(paper));
        const pageCount = paperPageCount(paper);
        const dftCount = Number(paper && paper.counts && paper.counts.dft_results || 0);
        const figureCount = Number(paper && paper.counts && paper.counts.figures || 0);
        const writingCount = Number(paper && paper.counts && paper.counts.writing_cards || 0);
        const serial = paper.serial_number ? formatSerialNumber(paper.serial_number) : "";
        const title = paper.title_zh || paper.title || "未命名文献";
        const journal = paper.journal || "期刊待补";
        const doiPreview = paperDoiPreview(paper);
        const pagesText = pageCount ? pageCount + " 页" : "页数待补";
        const sizeText = fileSize || "大小待补";

        html += '<div class="paper-list-item' + active + '" onclick="selectPaperById(\\'' + paper.id + '\\')">' +
            '<div class="paper-item-top">' +
                '<div style="display:flex;align-items:flex-start;">' +
                    (serial ? '<span class="paper-item-serial">' + esc(serial) + '</span>' : '') +
                    '<span class="paper-item-title" title="' + esc(title) + '">' + esc(title) + '</span>' +
                '</div>' +
            '</div>' +
            '<div class="paper-item-middle">' +
                '<span class="paper-item-journal">' + esc(journal) + '</span>' +
                '<span class="paper-item-doi">' + esc(doiPreview) + '</span>' +
            '</div>' +
            '<div class="paper-item-bottom">' +
                '<div style="display:flex;gap:6px;align-items:center;">' +
                    '<span class="status-chip ' + libraryQualityChipClass(paper) + '">' + esc(libraryQualityLabel(paper)) + '</span>' +
                    '<span class="paper-support-item">' + esc(pagesText) + '</span>' +
                    '<span class="paper-support-item">' + esc(sizeText) + '</span>' +
                '</div>' +
                '<div class="paper-item-stats">' +
                    '<span class="paper-side-stat" title="DFT结果数">D:' + esc(dftCount) + '</span>' +
                    '<span class="paper-side-stat" title="图表数">F:' + esc(figureCount) + '</span>' +
                    '<span class="paper-side-stat" title="写作卡数">W:' + esc(writingCount) + '</span>' +
                '</div>' +
            '</div>' +
        '</div>';
    });
    container.innerHTML = html;
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
    for (let i = 0; i < 6; i++) {
        skeletonHtml +=
            '<div class="paper-list-item skeleton-item">' +
                '<div class="paper-item-top">' +
                    '<div class="skeleton skeleton-title" style="width: 80%; height: 16px;"></div>' +
                '</div>' +
                '<div class="paper-item-middle">' +
                    '<div class="skeleton" style="width: 40%; height: 12px;"></div>' +
                '</div>' +
                '<div class="paper-item-bottom">' +
                    '<div class="skeleton" style="width: 30%; height: 16px; border-radius: 12px;"></div>' +
                '</div>' +
            '</div>';
    }
    container.innerHTML = skeletonHtml;
}
'''
with open(path, 'w', encoding='utf-8') as f:
    f.write(original_content)
print('Restored render-list.js successfully')
