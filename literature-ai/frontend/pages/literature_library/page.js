Object.assign(window, {
    openAddLiteraturePanel: openAddLiteraturePanel,
    closeAddLiteraturePanel: closeAddLiteraturePanel,
    switchAcquisitionMode: switchAcquisitionMode,
    toggleAddLiteratureMenu: toggleAddLiteratureMenu,
    togglePaperMoreMenu: togglePaperMoreMenu,
    addToEvidencePack: addToEvidencePack,
    openAggregateView: openAggregateView,
    openSelectedPdfEvidence: openSelectedPdfEvidence,
    openDeletePaperDialog: openDeletePaperDialog,
    resetCurrentPaperUpload: resetCurrentPaperUpload,
    closeDeletePaperDialog: closeDeletePaperDialog,
    confirmDeleteCurrentPaper: confirmDeleteCurrentPaper,
    classifyUnknownTypes: classifyUnknownTypes,
    showFolderImportGuide: showFolderImportGuide,
    switchTab: switchTab,
    openMetadataDiagnostics: openMetadataDiagnostics,
    closeMetadataDiagnostics: closeMetadataDiagnostics,
    toggleDashboard: toggleDashboard,
    toggleSidebar: toggleSidebar,
    toggleWorkspace: toggleWorkspace,
    fetchPapers: fetchPapers,
    searchLocal: searchLocal,
    refreshCurrentPage: refreshCurrentPage,
    refreshLibraryData: refreshLibraryData,
    resetLibraryPagination: resetLibraryPagination,
    goToLibraryPage: goToLibraryPage,
    changeLibraryPage: changeLibraryPage,
    setLibraryPageSize: setLibraryPageSize,
    prevPage: prevPage,
    nextPage: nextPage,
    clearFilters: clearFilters,
    selectPaperById: selectPaperById,
    openWorkspaceForPaper: openWorkspaceForPaper
});

window.addEventListener("beforeunload", disconnectSSE);
document.addEventListener("click", closeDropdowns);
document.addEventListener("click", function(event) {
    if (event.target.closest(".paper-row") ||
        event.target.closest(".workspace") ||
        event.target.closest(".sidebar") ||
        event.target.closest(".toolbar") ||
        event.target.closest(".topnav") ||
        event.target.closest("button") ||
        event.target.closest("input") ||
        event.target.closest(".modal-overlay") ||
        event.target.closest("a")) {
        return;
    }
    if (state.selectedPaperId) {
        state.selectedPaperId = null;
        state.selectedPaper = null;
        renderPaperList();
        if (typeof loadPaperDetail === "function") loadPaperDetail(null);
    }
});
const searchInput = $("searchInput");
if (searchInput) {
    searchInput.addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });
}
["filterYear", "filterJournal"].forEach(function(id) {
    const el = $(id);
    if (el) el.addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });
});
["filterPaperType", "filterDFT", "filterWC", "filterPdf", "filterSort"].forEach(function(id) {
    const el = $(id);
    if (el) el.addEventListener("change", function() { scheduleFilterSearch(120); });
});

initLayoutState();
applyQueryParams();
restoreLibraryFilterState();
initProtocolWarning();
initSplitDrag();
initActionMenus();
ensureClassificationToolbarButton();
TopNav.init({ currentPage: 'literature', mountId: 'topnav-mount' });
loadLibraries().finally(async function() {
    await fetchPapers();
    initSSE();
});
switchTab(state.currentTab);
if (state.openAddOnLoad) {
    openAddLiteraturePanel(state.openAddOnLoad);
}
loadWriterSettings();
