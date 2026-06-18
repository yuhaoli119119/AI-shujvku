const state = {
    currentOffset: 0,
    pagination: {
        page: 1,
        pageSize: 25,
    },
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
    fullDetailLoadingFor: null,
};

function $(id) { return document.getElementById(id); }

function esc(value) {
    const el = document.createElement("div");
    el.textContent = value == null ? "" : String(value);
    return el.innerHTML;
}

function renderPipeTable(md) {
    if (!md || typeof md !== "string") return "";
    var lines = md.trim().split("\n");
    if (lines.length < 3) return '<pre class="mono">' + esc(md) + '</pre>';
    var allPipe = true;
    for (var i = 0; i < Math.min(lines.length, 3); i++) {
        var t = lines[i].trim();
        if (!(t.indexOf("|") === 0 && t.lastIndexOf("|") === t.length - 1)) { allPipe = false; break; }
    }
    if (!allPipe) return '<pre class="mono">' + esc(md) + '</pre>';
    var colCount = lines[0].split("|").slice(1, -1).length;
    if (colCount > 6) return '<pre class="mono">' + esc(md) + '</pre>';
    var html = '<table class="md-table"><thead><tr>';
    var headers = lines[0].split("|").slice(1, -1);
    headers.forEach(function(h) { html += '<th>' + esc(h.trim()) + '</th>'; });
    html += '</tr></thead><tbody>';
    for (var j = 1; j < lines.length; j++) {
        var line = lines[j].trim();
        if (/^\|[\s\-:]+\|/.test(line)) continue;
        html += '<tr>';
        var cells = line.split("|").slice(1, -1);
        cells.forEach(function(c) { html += '<td>' + esc(c.trim()) + '</td>'; });
        html += '</tr>';
    }
    html += '</tbody></table>';
    return html;
}

function ellipsis(text, limit) {
    const value = text == null ? "" : String(text);
    return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}
