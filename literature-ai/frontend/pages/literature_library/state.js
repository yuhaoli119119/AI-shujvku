const state = {
    currentOffset: 0,
    papers: [],
    selectedPaperId: null,
    selectedPaper: null,
    currentTab: "summary",
    hasExplicitTab: false,
    currentLibrary: null,
    currentLibraryTotal: 0,
    paperListRequestSeq: 0,
    paperStreamLibraryName: "",
    openAddOnLoad: null,
    eventSource: null,
    writerStatus: null,
    externalRuns: [],
    aggregateData: null,
    discoveryCache: [],
    aiWorkflowJobId: null,
    writerSettings: null,
    jobCenterStatus: "",
    jobCenterType: "",
    qualityReasonContext: "",
    detailLoadToken: null,
    selectedPaperAudit: null,
    knowledgeContextLoadingFor: null,
    paperDetailCache: {},
};

function $(id) { return document.getElementById(id); }

function esc(value) {
    const el = document.createElement("div");
    el.textContent = value == null ? "" : String(value);
    return el.innerHTML;
}

function ellipsis(text, limit) {
    const value = text == null ? "" : String(text);
    return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}
