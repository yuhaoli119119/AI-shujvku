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
                '<div class="paper-title">' + (paper.serial_number ? '<span class="serial-chip">' + formatSerialNumber(paper.serial_number) + '</span> ' : "") + esc(paper.title || "未命名文献") + "</div>" +
                '<div class="paper-meta">' + esc(paper.year || "-") + " | " + esc(paper.journal || "-") + " | " + esc(paper.paper_type || "未知类型") + "<br>" + paperStatusChip(paper) + "</div>" +
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

async function fetchPapers() {
    try {
        renderPaperListSkeleton();
        const papers = await fetchJSON(API_BASE + "?" + getFilters().toString());
        state.papers = papers || [];
        if (state.papers.length) {
            state.currentLibraryTotal = Math.max(state.currentLibraryTotal || 0, state.currentOffset + state.papers.length);
        }
        if (!state.selectedPaperId || !state.papers.some(function(item) { return item.id === state.selectedPaperId; })) {
            state.selectedPaperId = null;
            state.selectedPaper = null;
        }
        renderPaperList();
        updatePagination();
        if (state.selectedPaperId) {
            await loadPaperDetail(state.selectedPaperId);
        } else {
            if (state.currentLibraryTotal === 0 && !state.papers.length) {
                renderLibraryEmptyState();
            } else {
                renderNoSelectionState();
            }
        }
    } catch (error) {
        const container = $("paperList");
        if (container) container.innerHTML = '<div class="list-empty">列表加载失败：' + esc(error.message) + "</div>";
        showToast("列表加载失败：" + error.message, "error");
    }
}

function updatePagination() {
    const page = Math.floor(state.currentOffset / PAGE_SIZE) + 1;
    const info = $("pageInfo");
    const prev = $("prevBtn");
    const next = $("nextBtn");
    if (info) info.textContent = "第 " + page + " 页";
    if (prev) prev.disabled = state.currentOffset === 0;
    if (next) next.disabled = state.papers.length < PAGE_SIZE;
}

async function selectPaperById(paperId) {
    await loadPaperDetail(paperId);
}

function refreshCurrentPage() {
    disconnectSSE();
    fetchPapers();
    initSSE();
}

function searchLocal() {
    state.currentOffset = 0;
    refreshCurrentPage();
}

function clearFilters() {
    const y = $("filterYear"); if (y) y.value = "";
    const j = $("filterJournal"); if (j) j.value = "";
    const t = $("filterPaperType"); if (t) t.value = "";
    const d = $("filterDFT"); if (d) d.value = "";
    const w = $("filterWC"); if (w) w.value = "";
    const s = $("searchInput"); if (s) s.value = "";
    state.currentOffset = 0;
    refreshCurrentPage();
}

function prevPage() {
    state.currentOffset = Math.max(0, state.currentOffset - PAGE_SIZE);
    refreshCurrentPage();
}

function nextPage() {
    state.currentOffset += PAGE_SIZE;
    refreshCurrentPage();
}
