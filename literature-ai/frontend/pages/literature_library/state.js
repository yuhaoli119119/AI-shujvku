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
    selectedPaperEvidenceLocators: undefined,
    knowledgeContextLoadingFor: null,
    paperDetailCache: {},
    paperResourceCache: {},
    paperResourceCacheOrder: [],
    paperResourceInflight: {},
    paperResourceFreshness: {},
    fullDetailLoadingFor: null,
    pendingNavigationTarget: null,
    pendingPdfJump: null,
};

window.state = state;

function $(id) { return document.getElementById(id); }

function esc(value) {
    const el = document.createElement("div");
    el.textContent = value == null ? "" : String(value);
    return el.innerHTML;
}

function renderPipeTable(md) {
    if (!md || typeof md !== "string") return "";
    var lines = md.replace(/\r\n?/g, "\n").split("\n");

    function splitRow(line) {
        var value = line.trim();
        if (value.charAt(0) === "|") value = value.slice(1);
        if (value.charAt(value.length - 1) === "|" && value.charAt(value.length - 2) !== "\\") value = value.slice(0, -1);
        var cells = [];
        var cell = "";
        for (var i = 0; i < value.length; i++) {
            if (value.charAt(i) === "\\" && value.charAt(i + 1) === "|") {
                cell += "|";
                i += 1;
            } else if (value.charAt(i) === "|") {
                cells.push(cell.trim());
                cell = "";
            } else {
                cell += value.charAt(i);
            }
        }
        cells.push(cell.trim());
        return cells;
    }

    function isSeparator(line) {
        if (line.indexOf("|") < 0) return false;
        var cells = splitRow(line);
        return cells.length >= 2 && cells.every(function(cell) {
            return /^:?-{3,}:?$/.test(cell.replace(/\s/g, ""));
        });
    }

    var separatorIndex = -1;
    for (var lineIndex = 1; lineIndex < lines.length; lineIndex++) {
        if (lines[lineIndex - 1].indexOf("|") >= 0 && isSeparator(lines[lineIndex])) {
            separatorIndex = lineIndex;
            break;
        }
    }
    if (separatorIndex < 0) return '<pre class="mono">' + esc(md) + '</pre>';

    var headers = splitRow(lines[separatorIndex - 1]);
    var colCount = headers.length;
    var rowLines = [];
    var tableEnd = separatorIndex;
    for (var rowIndex = separatorIndex + 1; rowIndex < lines.length; rowIndex++) {
        var rowLine = lines[rowIndex].trim();
        if (!rowLine || rowLine.indexOf("|") < 0) break;
        rowLines.push(rowLine);
        tableEnd = rowIndex;
    }

    var before = lines.slice(0, separatorIndex - 1).join("\n").trim();
    var after = lines.slice(tableEnd + 1).join("\n").trim();
    var html = before ? '<div class="md-table-note">' + esc(before) + '</div>' : '';
    html += '<div class="md-table-scroll"><table class="md-table"><thead><tr>';
    headers.forEach(function(h) { html += '<th>' + esc(h.trim()) + '</th>'; });
    html += '</tr></thead><tbody>';
    rowLines.forEach(function(line) {
        html += '<tr>';
        var cells = splitRow(line);
        for (var cellIndex = 0; cellIndex < colCount; cellIndex++) {
            html += '<td>' + esc(cells[cellIndex] || '') + '</td>';
        }
        html += '</tr>';
    });
    html += '</tbody></table></div>';
    if (after) html += '<div class="md-table-note">' + esc(after) + '</div>';
    return html;
}

function ellipsis(text, limit) {
    const value = text == null ? "" : String(text);
    return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}
