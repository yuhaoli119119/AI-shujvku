const state = {
    currentOffset: 0,
    papers: [],
    selectedPaperId: null,
    selectedPaper: null,
    currentTab: "detail",
    hasExplicitTab: false,
    eventSource: null,
    writerStatus: null,
    externalRuns: [],
    aggregateData: null,
    discoveryCache: [],
    aiWorkflowJobId: null,
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
