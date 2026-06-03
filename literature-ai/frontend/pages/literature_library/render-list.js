function renderPaperList() {
    const container = $("paperList");
    const meta = $("paperListMeta");
    if (meta) meta.textContent = state.papers.length + " 篇";
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
    container.innerHTML = state.papers.map(function(paper) {
        const active = paper.id === state.selectedPaperId ? " active" : "";
        return (
            '<div class="paper-card' + active + '" onclick="selectPaperById(\'' + paper.id + '\')">' +
                '<div class="paper-title">' + (paper.serial_number ? '<span class="serial-chip">' + formatSerialNumber(paper.serial_number) + '</span> ' : "") + esc(paper.title_zh || paper.title || "未命名文献") + "</div>" +
                (paper.title_zh && paper.title ? '<div class="paper-original-title">' + esc(paper.title) + '</div>' : '') +
                '<div class="paper-meta">' + esc(paper.year || "-") + " | " + esc(paper.journal || "-") + " | " + esc(paperTypeLabel(paper.paper_type)) + "<br>" + paperStatusChip(paper) + "</div>" +
                '<div class="badge-row">' +
                    badge(paper.counts && paper.counts.sections) +
                    badge(paper.counts && paper.counts.figures) +
                    badge(paper.counts && paper.counts.dft_results) +
                    badge(paper.counts && paper.counts.writing_cards) +
                "</div>" +
            "</div>"
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
