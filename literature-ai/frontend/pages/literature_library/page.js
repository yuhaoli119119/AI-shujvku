const API_BASE = "/api/papers";
const LIB_API = "/api/libraries";
const WRITER_API = "/api/writer";
const EXTERNAL_API = "/api/external-analysis";
const PAGE_SIZE = 20;

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

function showToast(message, type) {
    const existing = document.querySelector(".toast");
    if (existing) existing.remove();
    const el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function() {
        el.style.opacity = "0";
        setTimeout(function() { el.remove(); }, 280);
    }, 3000);
}

function showProgress(message, color) {
    let el = $("progressBox");
    if (!el) {
        el = document.createElement("div");
        el.id = "progressBox";
        el.className = "progress-box";
        document.body.appendChild(el);
    }
    el.textContent = message;
    el.style.background = color || "";
}

function hideProgress() {
    const el = $("progressBox");
    if (!el) return;
    setTimeout(function() {
        const box = $("progressBox");
        if (box) box.remove();
    }, 1800);
}

function getCurrentLibraryName() {
    return $("librarySelect").value || "";
}

function formatSerialNumber(value) {
    if (value === null || value === undefined || value === "") return "";
    return "#" + String(value).padStart(3, "0");
}

function paperStatusChip(paper) {
    if (paper.oa_status === "metadata_only") {
        return '<span class="status-chip meta">仅元数据</span>';
    }
    if (paper.pdf_path) {
        return '<span class="status-chip full">已入库</span>';
    }
    return '<span class="status-chip none">状态未明</span>';
}

function badge(count) {
    const safe = Number(count || 0);
    return safe > 0
        ? '<span class="count-badge has">' + safe + "</span>"
        : '<span class="count-badge zero">0</span>';
}

function formatDate(value) {
    if (!value) return "-";
    try {
        return new Date(value).toLocaleString("zh-CN");
    } catch (_) {
        return value;
    }
}

function getFilters() {
    const params = new URLSearchParams();
    params.set("limit", PAGE_SIZE);
    params.set("offset", state.currentOffset);
    const libraryName = getCurrentLibraryName();
    const q = $("searchInput").value.trim();
    const year = $("filterYear").value.trim();
    const journal = $("filterJournal").value.trim();
    const paperType = $("filterPaperType").value;
    const dft = $("filterDFT").value;
    const wc = $("filterWC").value;
    if (libraryName) params.set("library_name", libraryName);
    if (q) params.set("q", q);
    if (year) params.set("year", year);
    if (journal) params.set("journal", journal);
    if (paperType) params.set("paper_type", paperType);
    if (dft !== "") params.set("has_dft_results", dft);
    if (wc !== "") params.set("has_writing_cards", wc);
    return params;
}

async function fetchJSON(url, options) {
    const resp = await fetch(url, options);
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) {}
    if (!resp.ok) {
        const detail = data && data.detail ? data.detail : ("HTTP " + resp.status);
        throw new Error(detail);
    }
    return data;
}

function applyQueryParams() {
    const params = new URLSearchParams(window.location.search);
    const paperId = params.get("paper_id") || params.get("review_paper_id");
    const tab = params.get("tab");
    if (paperId) state.selectedPaperId = paperId;
    if (params.get("review_paper_id")) state.currentTab = "review";
    if (tab && ["detail", "writer", "review", "ai-search", "aggregate"].includes(tab)) {
        state.hasExplicitTab = true;
        state.currentTab = tab;
    }
}

function syncQueryParams() {
    if (location.protocol === "file:") return;
    const url = new URL(window.location.href);
    if (state.selectedPaperId) url.searchParams.set("paper_id", state.selectedPaperId);
    else url.searchParams.delete("paper_id");
    url.searchParams.set("tab", state.currentTab);
    window.history.replaceState({}, "", url.toString());
}

function switchTab(tab) {
    state.currentTab = tab;
    document.querySelectorAll(".tab-btn").forEach(function(btn) {
        btn.classList.toggle("active", btn.getAttribute("data-tab") === tab);
    });
    document.querySelectorAll("[data-nav-tab]").forEach(function(link) {
        link.classList.toggle("active", link.getAttribute("data-nav-tab") === tab);
    });
    document.querySelectorAll(".tab-panel").forEach(function(panel) {
        panel.classList.toggle("active", panel.id === "tab-" + tab);
    });
    if (!state.selectedPaperId) {
        renderTabLanding(tab);
    }
    syncQueryParams();
    if (tab === "writer") ensureWriterStatus();
    if (tab === "review" && state.selectedPaperId) loadExternalRuns();
    if (tab === "review" && !state.selectedPaperId) loadAgentGuide();
    if (tab === "aggregate") loadAggregate();
}

function showEmptyWorkspace() {
    $("workspaceEmpty").style.display = "flex";
    $("workspaceBody").style.display = "none";
}

function showWorkspace() {
    $("workspaceEmpty").style.display = "none";
    $("workspaceBody").style.display = "block";
}

function renderTabLanding(tab) {
    const landing = {
        detail: {
            title: "论文详情",
            meta: "先从左侧选择一篇文献，再查看结构化提取结果。",
        },
        writer: {
            title: "内部 AI 整理",
            meta: "这里用于让站内 AI 对单篇文献做归纳、补充和写作整理。",
        },
        review: {
            title: "外部 / IDE AI",
            meta: "这里用于导入外部 AI 审核结果，或查看 IDE / MCP 接入指南。",
        },
        "ai-search": {
            title: "AI 检索入库",
            meta: "这里可以直接做在线检索、AI 扩展检索，以及后台批量收录。",
        },
        aggregate: {
            title: "聚合视图",
            meta: "这里查看跨文献的 DFT、催化剂和别名聚合结果。",
        },
    };
    const current = landing[tab] || landing.detail;
    $("paperTitle").textContent = current.title;
    $("paperMeta").textContent = current.meta;
    $("paperHeaderBadges").innerHTML = "";
    if (tab === "detail") {
        $("detailContent").innerHTML = '<div class="workspace-empty">先在左侧选择一篇文献，再查看论文详情。</div>';
    }
    if (tab === "writer") {
        $("writerResult").innerHTML = '<div class="workspace-empty">先选择一篇文献，然后让内部 AI 对该文献继续整理和补充。</div>';
    }
    if (tab === "review") {
        $("externalRuns").innerHTML = '<div class="workspace-empty">先选择一篇文献以导入审核结果；也可以先点上方按钮查看 IDE / MCP 接入指南。</div>';
    }
    if (tab === "ai-search" && !$("aiSearchResult").innerHTML.trim()) {
        $("aiSearchResult").innerHTML = '<div class="workspace-empty">输入关键词后即可开始在线检索或 AI 检索入库。</div>';
    }
    if (tab === "aggregate" && !$("aggregateResult").innerHTML.trim()) {
        $("aggregateResult").innerHTML = '<div class="workspace-empty">点击“刷新聚合”即可查看跨文献聚合结果。</div>';
    }
    showWorkspace();
}

function renderPaperList() {
    const container = $("paperList");
    $("paperListMeta").textContent = state.papers.length + " 篇";
    $("globalSummary").textContent = "当前页 " + state.papers.length + " 篇文献，右侧支持内部 AI、外部 AI 审核与 AI 自动搜索。";
    if (!state.papers.length) {
        container.innerHTML = '<div class="workspace-empty">当前条件下没有文献。</div>';
        if (state.currentTab === "detail") showEmptyWorkspace();
        else renderTabLanding(state.currentTab);
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

function renderWorkspaceHeader(paper) {
    $("paperTitle").textContent = paper.title || "未命名文献";
    $("paperMeta").textContent = [
        paper.year || "-",
        paper.journal || "-",
        paper.paper_type ? paper.paper_type : "未知类型",
        paper.doi ? "DOI: " + paper.doi : "无 DOI"
    ].join(" | ");
    $("paperHeaderBadges").innerHTML =
        (paper.serial_number ? '<span class="serial-chip">' + formatSerialNumber(paper.serial_number) + "</span>" : "") +
        paperStatusChip(paper) +
        badge(paper.counts && paper.counts.sections) +
        badge(paper.counts && paper.counts.figures) +
        badge(paper.counts && paper.counts.dft_results) +
        badge(paper.counts && paper.counts.mechanism_claims) +
        badge(paper.counts && paper.counts.writing_cards);
    $("writerTopic").value = paper.title || "";
}

function renderListBlock(title, items, formatter) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    return items.map(function(item, index) {
        return '<div class="section-card"><h3>' + esc(title) + " " + (items.length > 1 ? (index + 1) : "") + '</h3>' + formatter(item) + "</div>";
    }).join("");
}

function renderJSONCards(title, items) {
    return renderListBlock(title, items, function(item) {
        return '<div class="mono">' + esc(JSON.stringify(item, null, 2)) + "</div>";
    });
}

function renderDetail(detail) {
    const summaryCards =
        '<div class="cards">' +
            '<div class="stat-card"><h3>章节</h3><div class="value">' + (detail.counts.sections || 0) + "</div></div>" +
            '<div class="stat-card"><h3>表格</h3><div class="value">' + (detail.counts.tables || 0) + "</div></div>" +
            '<div class="stat-card"><h3>图片</h3><div class="value">' + (detail.counts.figures || 0) + "</div></div>" +
            '<div class="stat-card"><h3>DFT 结果</h3><div class="value">' + (detail.counts.dft_results || 0) + "</div></div>" +
            '<div class="stat-card"><h3>机理</h3><div class="value">' + (detail.counts.mechanism_claims || 0) + "</div></div>" +
            '<div class="stat-card"><h3>写作卡</h3><div class="value">' + (detail.counts.writing_cards || 0) + "</div></div>" +
        "</div>";

    const baseInfo =
        '<div class="section-card"><h3>基础信息</h3>' +
            '<div class="inline-grid">' +
                '<div class="key-value"><div class="k">文献库</div><div class="v">' + esc(detail.library_name || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">文献类型</div><div class="v">' + esc(detail.paper_type || "未知") + (detail.type_confidence ? ' (置信度 ' + detail.type_confidence + ')' : '') + '</div></div>' +
                '<div class="key-value"><div class="k">分类来源</div><div class="v">' + esc(detail.classification_source || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">创建时间</div><div class="v">' + esc(formatDate(detail.created_at)) + '</div></div>' +
                '<div class="key-value"><div class="k">PDF 路径</div><div class="v">' + esc(detail.pdf_path || "-") + '</div></div>' +
                '<div class="key-value"><div class="k">Markdown 路径</div><div class="v">' + esc(detail.markdown_path || "-") + '</div></div>' +
            "</div>" +
        "</div>";

    const abstractCard =
        '<div class="section-card"><h3>摘要</h3><div class="prewrap">' + esc(detail.abstract || "暂无摘要。") + "</div></div>";

    const comprehensiveCard =
        '<div class="section-card"><h3>综合解析</h3><div class="mono">' +
            esc(JSON.stringify(detail.comprehensive_analysis || {}, null, 2)) +
        "</div></div>";

    const sectionCards = renderListBlock("正文节选", detail.sections ? detail.sections.slice(0, 8) : [], function(item) {
        return (
            '<div class="subtle">标题：' + esc(item.section_title || item.section_type || "未命名章节") + "</div>" +
            '<div class="prewrap" style="margin-top:8px;">' + esc(ellipsis(item.text || "", 2200) || "暂无文本。") + "</div>"
        );
    });

    let figureCards = "";
    if (detail.figures && detail.figures.length) {
        const roles = new Set();
        detail.figures.forEach(f => {
            if (f.figure_role) roles.add(f.figure_role);
        });
        
        let filterHtml = "";
        if (roles.size > 0) {
            filterHtml += '<div style="margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap;">';
            filterHtml += '<button class="btn small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display=\'block\')">全部</button>';
            roles.forEach(role => {
                filterHtml += '<button class="btn ghost small" onclick="document.querySelectorAll(\'.figure-card\').forEach(el => el.style.display = el.dataset.role === \'' + esc(role) + '\' ? \'block\' : \'none\')">' + esc(role) + '</button>';
            });
            filterHtml += '</div>';
        }
        
        function extractFigureNumber(caption) {
            if (!caption) return null;
            var m = caption.match(/(?:Figure|Fig\.?|Scheme)\s*(\d+)/i);
            return m ? parseInt(m[1], 10) : null;
        }

        const cardsHtml = detail.figures.slice(0, 15).map(function(item, index) {
            let imgHtml = "";
            if (item.image_path) {
                imgHtml = '<div style="margin-top: 12px; text-align: center;"><img src="/api/papers/assets/' + esc(item.image_path) + '" style="max-width: 100%; max-height: 400px; border: 1px solid var(--color-border); border-radius: var(--radius-sm); object-fit: contain;" alt="Extracted Figure" /></div>';
            }

            let metaHtml = "";
            if (item.figure_role) {
                metaHtml += '<span class="status-chip" style="background: var(--color-primary-bg); color: var(--color-text-secondary); margin-right: 8px;">' + esc(item.figure_role) + (item.role_confidence ? ' (' + (item.role_confidence*100).toFixed(0) + '%)' : '') + '</span>';
            }
            if (item.key_elements && item.key_elements.length) {
                item.key_elements.forEach(el => {
                    metaHtml += '<span class="status-chip meta" style="margin-right: 4px;">' + esc(el) + '</span>';
                });
            }
            if (metaHtml) {
                metaHtml = '<div style="margin-top: 8px;">' + metaHtml + '</div>';
            }

            let summaryHtml = "";
            if (item.content_summary) {
                summaryHtml = '<div class="subtle" style="margin-top: 8px; font-weight: 500;">' + esc(item.content_summary) + '</div>';
            }

            var figNum = extractFigureNumber(item.caption);
            var figLabel = figNum !== null ? '图片 ' + figNum : '图片 ' + (index + 1);

            return '<div class="section-card figure-card" data-role="' + esc(item.figure_role || 'unknown') + '"><h3>' + figLabel + '</h3>' +
                   '<div class="prewrap">' + esc(item.caption || "无 caption") + "</div>" +
                   summaryHtml + metaHtml + imgHtml + '</div>';
        }).join("");
        
        figureCards = filterHtml + cardsHtml;
    } else {
        figureCards = '<div class="section-card"><h3>图片</h3><div class="muted">暂无内容。</div></div>';
    }

    const referenceCards = renderListBlock("参考文献", detail.references ? detail.references.slice(0, 20) : [], function(item) {
        return (
            '<div class="prewrap">' + esc(item.title || "未命名参考文献") + "</div>" +
            '<div class="subtle" style="margin-top:8px;">作者：' + esc(item.authors || "-") + " | DOI：" + esc(item.doi || "-") + "</div>" +
            (item.citation_context ? '<div class="mono" style="margin-top:8px;">' + esc(item.citation_context) + "</div>" : "")
        );
    });

    $("detailContent").innerHTML =
        summaryCards +
        baseInfo +
        abstractCard +
        comprehensiveCard +
        renderJSONCards("DFT 设置", detail.dft_settings_items || []) +
        renderJSONCards("催化剂样本", detail.catalyst_samples_items || []) +
        renderJSONCards("DFT 结果", detail.dft_results_items || []) +
        renderJSONCards("电化学性能", detail.electrochemical_performance_items || []) +
        renderJSONCards("机理声明", detail.mechanism_claims_items || []) +
        renderJSONCards("写作卡片", detail.writing_cards_items || []) +
        sectionCards +
        figureCards +
        referenceCards +
        renderJSONCards("出向关系", detail.outgoing_relationships || []) +
        renderJSONCards("入向关系", detail.incoming_relationships || []);
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

function renderDetailSkeleton() {
    const detailContainer = $("detailContent");
    if (!detailContainer) return;
    detailContainer.innerHTML = 
        '<div class="skeleton-detail">' +
            '<div class="skeleton-row">' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
                '<div class="skeleton skeleton-stat"></div>' +
            '</div>' +
            '<div class="skeleton skeleton-info" style="margin-top:14px; margin-bottom:14px;"></div>' +
            '<div class="section-card">' +
                '<div class="skeleton skeleton-text long"></div>' +
                '<div class="skeleton skeleton-text medium"></div>' +
                '<div class="skeleton skeleton-text long"></div>' +
                '<div class="skeleton skeleton-text short"></div>' +
            '</div>' +
        '</div>';
}

async function fetchPapers() {
    try {
        renderPaperListSkeleton();
        const papers = await fetchJSON(API_BASE + "?" + getFilters().toString());
        state.papers = papers || [];
        if (!state.selectedPaperId || !state.papers.some(function(item) { return item.id === state.selectedPaperId; })) {
            state.selectedPaperId = state.papers[0] ? state.papers[0].id : null;
        }
        if (!state.selectedPaperId && !state.hasExplicitTab && state.currentTab === "detail") {
            state.currentTab = "ai-search";
        }
        renderPaperList();
        updatePagination();
        if (state.selectedPaperId) {
            await loadPaperDetail(state.selectedPaperId);
        } else {
            switchTab(state.currentTab);
        }
    } catch (error) {
        $("paperList").innerHTML = '<div class="workspace-empty">列表加载失败：' + esc(error.message) + "</div>";
        showToast("列表加载失败：" + error.message, "error");
    }
}

function updatePagination() {
    const page = Math.floor(state.currentOffset / PAGE_SIZE) + 1;
    $("pageInfo").textContent = "第 " + page + " 页";
    $("prevBtn").disabled = state.currentOffset === 0;
    $("nextBtn").disabled = state.papers.length < PAGE_SIZE;
}

async function loadPaperDetail(paperId) {
    if (!paperId) {
        showEmptyWorkspace();
        return;
    }
    try {
        renderDetailSkeleton();
        const detail = await fetchJSON(API_BASE + "/" + paperId);
        state.selectedPaperId = paperId;
        state.selectedPaper = detail;
        renderPaperList();
        renderWorkspaceHeader(detail);
        renderDetail(detail);
        showWorkspace();
        syncQueryParams();
        if (state.currentTab === "review") loadExternalRuns();
        if (state.currentTab === "aggregate") loadAggregate();
        if (state.currentTab === "writer") ensureWriterStatus();
    } catch (error) {
        showToast("详情加载失败：" + error.message, "error");
    }
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
    $("filterYear").value = "";
    $("filterJournal").value = "";
    $("filterPaperType").value = "";
    $("filterDFT").value = "";
    $("filterWC").value = "";
    $("searchInput").value = "";
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

function disconnectSSE() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
}

function initSSE() {
    if (location.protocol === "file:") return;
    disconnectSSE();
    state.eventSource = new EventSource(API_BASE + "/stream?" + getFilters().toString());
    state.eventSource.addEventListener("papers_update", function(event) {
        try {
            state.papers = JSON.parse(event.data) || [];
            renderPaperList();
            updatePagination();
        } catch (error) {
            console.error("SSE parse error", error);
        }
    });
}

function clampSearchLimit(value) {
    const n = Number(value || 100);
    if (!Number.isFinite(n)) return 100;
    return Math.max(1, Math.min(100, Math.round(n)));
}

function discoveryKey(item) {
    const doi = (item.doi || "").trim().toLowerCase();
    if (doi) return "doi:" + doi;
    const identifier = (item.identifier || item.url || "").trim().toLowerCase();
    if (identifier) return "id:" + identifier;
    return "title:" + String(item.title || "").trim().toLowerCase();
}

function mergeDiscoveryResults(items) {
    const existingKeys = new Set(state.discoveryCache.map(discoveryKey));
    let added = 0;
    let duplicate = 0;
    (items || []).forEach(function(item) {
        const key = discoveryKey(item);
        if (!key || key === "title:") return;
        if (existingKeys.has(key)) {
            duplicate += 1;
            return;
        }
        existingKeys.add(key);
        state.discoveryCache.push(item);
        added += 1;
    });
    return { added: added, duplicate: duplicate, total: state.discoveryCache.length };
}

async function searchOnline() {
    const query = $("searchInput").value.trim();
    if (!query) {
        showToast("请先输入检索关键词。", "error");
        return;
    }
    switchTab("ai-search");
    $("aiSearchQuery").value = query;
    $("aiSearchResult").innerHTML = '<div class="workspace-empty">正在从 OpenAlex / arXiv 检索，最多拉取 100 篇...</div>';
    try {
        const limit = clampSearchLimit($("aiSearchMaxResults").value);
        const data = await fetchJSON(API_BASE + "/discovery/search?q=" + encodeURIComponent(query) + "&limit=" + limit);
        const stats = mergeDiscoveryResults(data.items || []);
        renderDiscoveryResults({ items: state.discoveryCache }, stats, "在线检索结果");
    } catch (error) {
        $("aiSearchResult").innerHTML = '<div class="workspace-empty">在线检索失败：' + esc(error.message) + "</div>";
    }
}

function renderDiscoveryResults(data, stats, title, prefixHtml) {
    const items = data && data.items ? data.items : [];
    if (!items.length) {
        $("aiSearchResult").innerHTML = '<div class="workspace-empty">没有找到在线结果。</div>';
        return;
    }
    $("aiSearchResult").innerHTML =
        (prefixHtml || "") +
        '<div class="writer-block"><h3>' + esc(title || "检索结果") + '（累计去重后 ' + items.length + ' 篇）</h3><div class="subtle">本页会合并后续检索结果：新增 ' + esc(stats && stats.added != null ? stats.added : "-") + ' 篇，过滤重复 ' + esc(stats && stats.duplicate != null ? stats.duplicate : "-") + ' 篇。点击“下载并收录”时，下载失败也会按元数据入库，之后可人工补 PDF。</div></div>' +
        items.map(function(item) {
            const identifier = item.identifier || item.doi || item.url || "";
            return (
                '<div class="ai-result-card">' +
                    '<h4>' + esc(item.title || "未命名文献") + "</h4>" +
                    '<div class="subtle">' + esc(item.year || "-") + " | " + esc(item.journal || "-") + " | " + esc((item.authors || []).slice(0, 4).join(", ") || "-") + "</div>" +
                    (item.abstract ? '<div class="prewrap" style="margin-top:10px;">' + esc(ellipsis(item.abstract, 520)) + "</div>" : "") +
                    '<div class="modal-actions" style="justify-content:flex-start;">' +
                        '<button class="btn green small" onclick="downloadIdentifier(' + JSON.stringify(identifier).replace(/"/g, "&quot;") + ')">下载并收录</button>' +
                    "</div>" +
                "</div>"
            );
        }).join("");
}

async function runAISearch() {
    const query = ($("aiSearchQuery").value || $("searchInput").value).trim();
    if (!query) {
        showToast("请输入 AI 搜索查询。", "error");
        return;
    }
    switchTab("ai-search");
    $("aiSearchQuery").value = query;
    $("aiSearchResult").innerHTML = '<div class="workspace-empty">AI 正在扩展查询并筛选文献...</div>';
    try {
        const data = await fetchJSON(API_BASE + "/ai_search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query: query,
                model: "deepseek-chat",
                max_results: clampSearchLimit($("aiSearchMaxResults").value),
                providers: [],
                skip_guard: false
            })
        });
        const papers = data && data.papers ? data.papers : [];
        if (!papers.length) {
            $("aiSearchResult").innerHTML = '<div class="workspace-empty">AI 没有返回结果。</div>';
            return;
        }
        const stats = mergeDiscoveryResults(papers);
        const prefix = '<div class="writer-block"><h3>AI 自动搜索结果</h3><div class="subtle">模型状态：' + esc(data.llm_status || "unknown") + " | 注释状态：" + esc(data.result_annotation_status || "-") + '</div><div class="mono" style="margin-top:12px;">' + esc(data.prompt_used || "") + "</div></div>";
        renderDiscoveryResults({ items: state.discoveryCache }, stats, "AI 自动搜索结果", prefix);
    } catch (error) {
        $("aiSearchResult").innerHTML = '<div class="workspace-empty">AI 搜索失败：' + esc(error.message) + "</div>";
    }
}

async function runAIWorkflow() {
    const query = ($("aiSearchQuery").value || $("searchInput").value).trim();
    if (!query) {
        showToast("请输入 AI 搜索查询。", "error");
        return;
    }
    switchTab("ai-search");
    showProgress("AI 工作流已转入后台，不会卡住页面...");
    try {
        const job = await fetchJSON(API_BASE + "/ai_workflow/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query: query,
                library_name: getCurrentLibraryName(),
                model: "deepseek-chat",
                max_results: clampSearchLimit($("aiSearchMaxResults").value),
                max_downloads: clampSearchLimit($("aiWorkflowMaxDownloads").value),
                providers: [],
                skip_existing: true
            })
        });
        state.aiWorkflowJobId = job.job_id;
        renderAIWorkflowJob(job);
        pollAIWorkflowJob(job.job_id);
        showToast("AI 工作流已进入后台任务。", "success");
    } catch (error) {
        $("aiSearchResult").innerHTML = '<div class="workspace-empty">AI 工作流失败：' + esc(error.message) + "</div>";
        showToast("AI 工作流失败：" + error.message, "error");
    }
    hideProgress();
}

async function pollAIWorkflowJob(jobId) {
    if (!jobId) return;
    try {
        const job = await fetchJSON(API_BASE + "/ai_workflow/jobs/" + encodeURIComponent(jobId));
        renderAIWorkflowJob(job);
        if (job.status === "queued" || job.status === "running") {
            setTimeout(function() { pollAIWorkflowJob(jobId); }, 1800);
        } else if (job.status === "completed") {
            showToast("AI 工作流完成，文献列表已刷新。", "success");
            state.currentOffset = 0;
            refreshCurrentPage();
        } else if (job.status === "failed") {
            showToast("AI 工作流失败：" + (job.error || ""), "error");
        }
    } catch (error) {
        $("aiSearchResult").insertAdjacentHTML("afterbegin", '<div class="section-card"><h3>任务轮询失败</h3><div class="subtle">' + esc(error.message) + "</div></div>");
    }
}

function renderAIWorkflowJob(job) {
    const result = job.result || {};
    $("aiSearchResult").innerHTML =
        '<div class="writer-block"><h3>AI 后台检索 / 收录任务</h3>' +
        '<div class="subtle">任务：' + esc(job.job_id || "-") + " | 状态：" + esc(job.status || "-") + " | 库：" + esc(job.library_name || getCurrentLibraryName() || "-") + "</div>" +
        '<div class="mono" style="margin-top:12px;">' + esc(JSON.stringify(job.progress || {}, null, 2)) + "</div>" +
        (job.error ? '<div class="subtle" style="margin-top:10px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
        "</div>" +
        (result.prompt_used ? '<div class="section-card"><h3>实际检索式</h3><div class="mono">' + esc(result.prompt_used) + "</div></div>" : "") +
        renderWorkflowList("已收录 / 已存在", result.ingested || [], function(item) {
            return '<div class="subtle">状态：' + esc(item.status) + " | DOI：" + esc(item.doi || "-") + " | 标识符：" + esc(item.identifier || "-") + "</div>";
        }) +
        renderWorkflowList("失败项", result.failed || [], function(item) {
            return '<div class="subtle">代码：' + esc(item.code || "-") + " | 原因：" + esc(item.reason || "-") + "</div>";
        });
}

function renderWorkflowList(title, items, formatter) {
    if (!items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无。</div></div>';
    }
    return items.map(function(item) {
        return '<div class="section-card"><h3>' + esc(title) + " - " + esc(item.title || item.identifier || "未命名") + "</h3>" + formatter(item) + "</div>";
    }).join("");
}

async function downloadIdentifier(identifier) {
    if (!identifier) return;
    showProgress("正在下载并收录...");
    try {
        const data = await fetchJSON(API_BASE + "/discovery/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ identifier: identifier, providers: [], library_name: getCurrentLibraryName() })
        });
        if (data.status === "metadata_only") {
            showToast("已按元数据收录：" + (data.title || ""), "info");
        } else if (data.status === "already_exists") {
            showToast("文献已在库中：" + (data.title || ""), "info");
        } else {
            showToast("已成功收录：" + (data.title || ""), "success");
        }
        state.currentOffset = 0;
        refreshCurrentPage();
    } catch (error) {
        showToast("收录失败：" + error.message, "error");
    }
    hideProgress();
}

function downloadByDOI() {
    const identifier = $("doiInput").value.trim();
    if (!identifier) {
        showToast("请输入 DOI 或 URL。", "error");
        return;
    }
    downloadIdentifier(identifier).then(function() {
        $("doiInput").value = "";
    });
}

async function uploadPDF(input) {
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append("file", file);
    formData.append("library_name", getCurrentLibraryName());
    showProgress("正在上传并解析：" + file.name);
    try {
        const data = await fetchJSON(API_BASE + "/ingest/upload", {
            method: "POST",
            body: formData
        });
        showToast("已上传并收录：" + (data.title || file.name), "success");
        state.currentOffset = 0;
        refreshCurrentPage();
    } catch (error) {
        showToast("上传失败：" + error.message, "error");
    } finally {
        input.value = "";
        hideProgress();
    }
}

async function rerunExtraction() {
    if (!state.selectedPaperId) return;
    showProgress("正在重新解析当前文献...");
    try {
        const data = await fetchJSON(API_BASE + "/" + state.selectedPaperId + "/extract", { method: "POST" });
        showToast("重新解析完成。", "success");
        $("detailContent").insertAdjacentHTML("afterbegin",
            '<div class="section-card"><h3>最近一次重解析结果</h3><div class="mono">' + esc(JSON.stringify(data, null, 2)) + "</div></div>"
        );
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("重解析失败：" + error.message, "error");
    }
    hideProgress();
}

async function ensureWriterStatus() {
    if (state.writerStatus) {
        renderWriterStatus();
        return;
    }
    try {
        state.writerStatus = await fetchJSON(WRITER_API + "/status");
        renderWriterStatus();
    } catch (error) {
        $("writerStatusBox").textContent = "写作器状态读取失败：" + error.message;
    }
}

function renderWriterStatus() {
    if (!state.writerStatus) return;
    $("writerStatusBox").innerHTML =
        "后端：<strong>" + esc(state.writerStatus.backend_used || "-") + "</strong> | " +
        "状态：<strong>" + esc(state.writerStatus.llm_status || "-") + "</strong> | " +
        (state.writerStatus.llm_error ? "错误：" + esc(state.writerStatus.llm_error) : "LLM 已就绪");
}

async function generateWriterDraft() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const topic = $("writerTopic").value.trim();
    if (!topic) {
        showToast("请输入写作主题。", "error");
        return;
    }
    showProgress("内部 AI 正在整理归纳...");
    try {
        const data = await fetchJSON(WRITER_API + "/draft", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                topic: topic,
                paper_ids: [state.selectedPaperId],
                user_notes: $("writerNotes").value.trim() || null,
                sections: ["outline", "introduction", "dft_results", "discussion", "figure_storyline"],
                limit_per_type: Number($("writerLimit").value || 5)
            })
        });
        $("writerResult").innerHTML =
            '<div class="writer-block"><h3>写作器返回状态</h3><div class="mono">' + esc(JSON.stringify({
                backend_used: data.backend_used,
                llm_status: data.llm_status,
                llm_error: data.llm_error,
                guard_actions: data.guard_actions,
                citation_guard: data.citation_guard
            }, null, 2)) + "</div></div>" +
            '<div class="section-card"><h3>提纲</h3><div class="prewrap">' + esc((data.outline || []).join("\n")) + "</div></div>" +
            '<div class="section-card"><h3>引言</h3><div class="prewrap">' + esc(data.introduction || "") + "</div></div>" +
            '<div class="section-card"><h3>DFT 结果整理</h3><div class="prewrap">' + esc(data.dft_results || "") + "</div></div>" +
            '<div class="section-card"><h3>讨论</h3><div class="prewrap">' + esc(data.discussion || "") + "</div></div>" +
            '<div class="section-card"><h3>图文叙事</h3><div class="prewrap">' + esc((data.figure_storyline || []).join("\n")) + "</div></div>" +
            '<div class="section-card"><h3>Prompt 预览</h3><div class="mono">' + esc(data.prompt_preview || "") + "</div></div>";
        showToast("内部 AI 整理完成。", "success");
    } catch (error) {
        $("writerResult").innerHTML = '<div class="workspace-empty">写作失败：' + esc(error.message) + "</div>";
        showToast("内部 AI 整理失败：" + error.message, "error");
    }
    hideProgress();
}

async function runInternalAIParse() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    switchTab("review");
    showProgress("内部 AI 正在审查并写回候选项...");
    try {
        const data = await fetchJSON(EXTERNAL_API + "/papers/" + state.selectedPaperId + "/internal-parse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_label: "内部AI解析",
                auto_apply: true
            })
        });
        $("externalRawText").value = "";
        showToast("内部 AI 解析完成。", "success");
        $("externalRuns").insertAdjacentHTML("afterbegin",
            '<div class="section-card"><h3>最近一次内部 AI 解析</h3><div class="mono">' + esc(JSON.stringify(data, null, 2)) + "</div></div>"
        );
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        $("externalRuns").innerHTML = '<div class="workspace-empty">内部 AI 解析失败：' + esc(error.message) + "</div>";
        showToast("内部 AI 解析失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadAgentGuide() {
    if (state.currentTab !== "review") {
        switchTab("review");
    }
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        $("externalRuns").innerHTML =
            '<div class="section-card"><h3>IDE / MCP AI 连接指南</h3>' +
            '<div class="subtle">外部 IDE AI 可以按这里的入口读取文献、追加 notes、提出 corrections、触发 parse；网页内部 AI 则使用本页“内部 AI 解析”直接写回候选项。</div>' +
            '<div class="mono" style="margin-top:12px;">' + esc(JSON.stringify(guide, null, 2)) + "</div></div>" +
            $("externalRuns").innerHTML;
    } catch (error) {
        showToast("读取 IDE / MCP 指南失败：" + error.message, "error");
    }
}

async function importExternalAnalysis() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const raw = $("externalRawText").value.trim();
    if (!raw) {
        showToast("请粘贴外部 AI 返回结果。", "error");
        return;
    }
    showProgress("正在导入外部 AI 审核结果...");
    let rawPayload = raw;
    try {
        rawPayload = JSON.parse(raw);
    } catch (_) {}
    try {
        await fetchJSON(EXTERNAL_API + "/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                paper_id: state.selectedPaperId,
                source: $("externalSource").value.trim() || "manual",
                source_label: $("externalSourceLabel").value.trim() || "外部AI复核",
                raw_text: typeof rawPayload === "string" ? rawPayload : null,
                raw_payload: rawPayload
            })
        });
        showToast("外部 AI 审核结果已导入。", "success");
        $("externalRawText").value = "";
        await loadExternalRuns();
    } catch (error) {
        showToast("导入失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadExternalRuns() {
    if (!state.selectedPaperId) return;
    $("externalRuns").innerHTML = '<div class="workspace-empty">正在加载审核记录...</div>';
    try {
        const runs = await fetchJSON(EXTERNAL_API + "/runs?paper_id=" + encodeURIComponent(state.selectedPaperId));
        state.externalRuns = runs || [];
        if (!state.externalRuns.length) {
            $("externalRuns").innerHTML = '<div class="workspace-empty">当前文献还没有审核记录。</div>';
            return;
        }
        $("externalRuns").innerHTML = state.externalRuns.map(function(run) {
            const pending = (run.candidates || []).filter(function(item) {
                return item.status === "pending" || item.status === "requires_resolution";
            });
            return (
                '<div class="run-card">' +
                    '<h4>' + esc(run.source_label || run.source || "未命名审核源") + "</h4>" +
                    '<div class="subtle">创建时间：' + esc(formatDate(run.created_at)) + " | 映射状态：" + esc(run.mapping_status || "-") + "</div>" +
                    (run.mapping_error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">错误：' + esc(run.mapping_error) + "</div>" : "") +
                    (run.raw_text ? '<div class="mono" style="margin-top:10px;">' + esc(ellipsis(run.raw_text, 1200)) + "</div>" : "") +
                    '<div class="candidate-toolbar" style="margin-top:12px;">' +
                        '<button class="btn blue small" onclick="materializeRun(\'' + run.id + '\')">全部写回数据库</button>' +
                        '<button class="btn ghost small" onclick="toggleRunCandidates(\'' + run.id + '\')">展开候选项（' + (run.candidates || []).length + "）</button>" +
                    '</div>' +
                    '<div id="run-candidates-' + run.id + '" style="display:none;">' +
                        renderCandidates(run.candidates || []) +
                    '</div>' +
                    (pending.length ? '<div class="subtle" style="margin-top:10px;">待处理候选项：' + pending.length + " 个</div>" : '<div class="subtle" style="margin-top:10px;">当前 run 没有待处理候选项。</div>') +
                "</div>"
            );
        }).join("");
    } catch (error) {
        $("externalRuns").innerHTML = '<div class="workspace-empty">审核记录加载失败：' + esc(error.message) + "</div>";
    }
}

function renderCandidates(candidates) {
    if (!candidates.length) {
        return '<div class="candidate-card"><div class="muted">没有候选项。</div></div>';
    }
    return candidates.map(function(item) {
        return (
            '<div class="candidate-card">' +
                '<h4>' + esc(item.candidate_type || "candidate") + " | 状态：" + esc(item.status || "-") + "</h4>" +
                '<div class="subtle">置信度：' + esc(item.confidence == null ? "-" : item.confidence) + " | 目标类型：" + esc(item.materialized_target_type || "-") + "</div>" +
                '<div class="mono" style="margin-top:10px;">' + esc(JSON.stringify(item.normalized_payload || {}, null, 2)) + "</div>" +
            "</div>"
        );
    }).join("");
}

function toggleRunCandidates(runId) {
    const el = $("run-candidates-" + runId);
    if (!el) return;
    el.style.display = el.style.display === "none" ? "block" : "none";
}

async function materializeRun(runId) {
    showProgress("正在把候选项写回数据库...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/materialize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidate_ids: [], created_by: "web_user" })
        });
        showToast("候选项已写回数据库。", "success");
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("写回失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadAggregate() {
    $("aggregateResult").innerHTML = '<div class="workspace-empty">正在加载聚合视图...</div>';
    try {
        state.aggregateData = await fetchJSON(API_BASE + "/aggregate");
        $("aggregateResult").innerHTML =
            '<div class="section-card"><h3>吸附物聚合</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.adsorbate_groups || {}, null, 2)) + "</div></div>" +
            '<div class="section-card"><h3>催化剂聚合</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.catalyst_groups || {}, null, 2)) + "</div></div>" +
            '<div class="section-card"><h3>可能别名</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.possible_name_aliases || [], null, 2)) + "</div></div>";
    } catch (error) {
        $("aggregateResult").innerHTML = '<div class="workspace-empty">聚合视图加载失败：' + esc(error.message) + "</div>";
    }
}

function openPaperDetailPage() {
    if (!state.selectedPaperId) return;
    window.open("/pages/paper_detail/index.html?id=" + encodeURIComponent(state.selectedPaperId), "_blank");
}

function copyPaperIdentity() {
    if (!state.selectedPaper) return;
    const value = [state.selectedPaper.title || "", state.selectedPaper.doi || ""].filter(Boolean).join("\n");
    navigator.clipboard.writeText(value).then(function() {
        showToast("已复制标题和 DOI。", "success");
    }).catch(function() {
        showToast("复制失败，请手动复制。", "error");
    });
}

async function loadLibraries() {
    try {
        const libraries = await fetchJSON(LIB_API);
        $("librarySelect").innerHTML = libraries.map(function(item) {
            return '<option value="' + esc(item.name) + '"' + (item.is_active ? " selected" : "") + ">" +
                esc(item.name) + (item.is_active ? "（当前）" : "") +
            "</option>";
        }).join("");
        const active = (libraries || []).find(function(item) { return item.is_active; });
        $("libStatus").textContent = active ? (active.root_path + " | " + active.paper_count + " 篇") : "";
    } catch (error) {
        console.error("loadLibraries failed", error);
    }
}

async function activateLibraryByName(name) {
    if (!name) return;
    try {
        await fetchJSON(LIB_API + "/" + encodeURIComponent(name) + "/activate", { method: "POST" });
        showToast("已切换到：" + name, "success");
        state.currentOffset = 0;
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        showToast("切库失败：" + error.message, "error");
    }
}

async function removeCurrentLibrary() {
    try {
        const libraries = await fetchJSON(LIB_API);
        const active = (libraries || []).find(function(item) { return item.is_active; });
        if (!active) {
            showToast("当前没有激活的库。", "error");
            return;
        }
        window._removeLibName = active.name;
        $("removeLibMsg").textContent = '确定要移除“' + active.name + '”吗？';
        $("removeLibDialog").style.display = "flex";
    } catch (error) {
        showToast("读取库信息失败：" + error.message, "error");
    }
}

function closeRemoveLibraryDialog() { $("removeLibDialog").style.display = "none"; }

async function confirmRemoveLibrary() {
    if (!window._removeLibName) return;
    try {
        await fetchJSON(LIB_API + "/" + encodeURIComponent(window._removeLibName), { method: "DELETE" });
        closeRemoveLibraryDialog();
        showToast("库已移除。", "success");
        await loadLibraries();
        state.currentOffset = 0;
        refreshCurrentPage();
    } catch (error) {
        showToast("移除失败：" + error.message, "error");
    }
}

async function loadDirBrowser(kind) {
    $(kind + "DirBrowser").textContent = "加载中...";
    try {
        const roots = await fetchJSON(LIB_API + "/browse-roots");
        if (!roots.length) {
            $(kind + "DirBrowser").textContent = "没有可浏览的目录";
            return;
        }
        renderDirBrowser(kind, roots[0].path);
    } catch (error) {
        $(kind + "DirBrowser").textContent = "加载失败：" + error.message;
    }
}

async function renderDirBrowser(kind, path) {
    try {
        const data = await fetchJSON(LIB_API + "/browse?path=" + encodeURIComponent(path));
        const parts = String(data.current_path || "").replace(/\\/g, "/").split("/").filter(Boolean);
        let html = '<div class="breadcrumbs">';
        if (data.parent_path) {
            html += '<span class="bc-link" data-path="' + esc(data.parent_path) + '">..</span> / ';
        }
        let current = "";
        parts.forEach(function(part, idx) {
            current += "/" + part;
            html += '<span class="bc-link" data-path="' + esc(current) + '">' + esc(part) + "</span>";
            if (idx < parts.length - 1) html += " / ";
        });
        html += "</div>";
        (data.subdirs || []).forEach(function(item) {
            html += '<div class="dir-item" data-path="' + esc(item.path) + '">📁 ' + esc(item.name) + "</div>";
        });
        $(kind + "DirBrowser").innerHTML = html;
        $(kind + "DirBrowser").querySelectorAll(".bc-link").forEach(function(el) {
            el.onclick = function() { renderDirBrowser(kind, this.getAttribute("data-path")); };
        });
        $(kind + "DirBrowser").querySelectorAll(".dir-item").forEach(function(el) {
            el.onclick = function() { selectDir(kind, this.getAttribute("data-path")); };
        });
    } catch (error) {
        $(kind + "DirBrowser").textContent = "浏览失败：" + error.message;
    }
}

function selectDir(kind, path) {
    const el = $(kind + "SelectedPath");
    el.style.display = "block";
    el.textContent = path;
    window["_selectedPath_" + kind] = path;
    renderDirBrowser(kind, path);
}

function openCreateLibraryDialog() {
    $("createLibName").value = "";
    $("createLibSelectedPath").style.display = "none";
    window._selectedPath_createLib = null;
    $("createLibDialog").style.display = "flex";
    loadDirBrowser("createLib");
}

function closeCreateLibraryDialog() { $("createLibDialog").style.display = "none"; }

async function setCreateDefaultLocation() {
    let path = "";
    try {
        const roots = await fetchJSON(`${LIB_API}/browse-roots`);
        path = roots?.[0]?.path || "";
    } catch (error) {
        console.warn("Failed to load browse roots", error);
    }
    if (!path) return;
    $("createLibSelectedPath").style.display = "block";
    $("createLibSelectedPath").textContent = path;
    window._selectedPath_createLib = path;
}

async function submitCreateLibrary() {
    const name = $("createLibName").value.trim();
    if (!name) {
        showToast("请输入库名称。", "error");
        return;
    }
    try {
        await fetchJSON(LIB_API, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name, root_path: window._selectedPath_createLib || "" })
        });
        closeCreateLibraryDialog();
        showToast("库创建成功。", "success");
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        showToast("创建失败：" + error.message, "error");
    }
}

function openImportLibraryDialog() {
    $("importLibSelectedPath").style.display = "none";
    window._selectedPath_importLib = null;
    $("importLibDialog").style.display = "flex";
    loadDirBrowser("importLib");
}

function closeImportLibraryDialog() { $("importLibDialog").style.display = "none"; }

async function submitImportLibrary() {
    if (!window._selectedPath_importLib) {
        showToast("请选择已有库目录。", "error");
        return;
    }
    try {
        await fetchJSON(LIB_API + "/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ root_path: window._selectedPath_importLib })
        });
        closeImportLibraryDialog();
        showToast("库导入成功。", "success");
        await loadLibraries();
        refreshCurrentPage();
    } catch (error) {
        showToast("导入失败：" + error.message, "error");
    }
}

function initSplitDrag() {
    const handle = $("dragHandle");
    if (!handle) return;

    const savedWidth = localStorage.getItem("sidebarWidth");
    if (savedWidth) {
        document.documentElement.style.setProperty("--sidebar-width", savedWidth + "px");
    }

    const MIN_W = 240, MAX_W = 600;
    let dragging = false, startX = 0, startWidth = 380, rafId = 0, newWidth = 0;

    function onStart(clientX) {
        dragging = true;
        startX = clientX;
        startWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-width")) || 380;
        document.body.classList.add("resizing");
        handle.classList.add("active");
    }

    function onMove(clientX) {
        if (!dragging) return;
        cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(function () {
            const delta = clientX - startX;
            newWidth = Math.min(MAX_W, Math.max(MIN_W, startWidth + delta));
            document.documentElement.style.setProperty("--sidebar-width", newWidth + "px");
        });
    }

    function onEnd() {
        if (!dragging) return;
        dragging = false;
        cancelAnimationFrame(rafId);
        document.body.classList.remove("resizing");
        handle.classList.remove("active");
        localStorage.setItem("sidebarWidth", newWidth || startWidth);
    }

    handle.addEventListener("mousedown", function (e) {
        e.preventDefault();
        onStart(e.clientX);
        window.addEventListener("mousemove", onMouseMove);
        window.addEventListener("mouseup", onMouseUp);
    });
    function onMouseMove(e) { onMove(e.clientX); }
    function onMouseUp() { onEnd(); window.removeEventListener("mousemove", onMouseMove); window.removeEventListener("mouseup", onMouseUp); }

    handle.addEventListener("touchstart", function (e) {
        e.preventDefault();
        onStart(e.touches[0].clientX);
    }, { passive: false });
    handle.addEventListener("touchmove", function (e) {
        e.preventDefault();
        onMove(e.touches[0].clientX);
    }, { passive: false });
    handle.addEventListener("touchend", onEnd);
    handle.addEventListener("touchcancel", onEnd);

    window.addEventListener("blur", onEnd);
}

function initProtocolWarning() {
    if (location.protocol === "file:") {
        $("fileModeWarning").style.display = "block";
    }
}

window.addEventListener("beforeunload", disconnectSSE);
$("searchInput").addEventListener("keydown", function(event) { if (event.key === "Enter") searchLocal(); });

applyQueryParams();
initProtocolWarning();
initSplitDrag();
TopNav.init({ currentPage: 'literature', mountId: 'topnav-mount' });
loadLibraries().then(function() {
    fetchPapers();
    initSSE();
    switchTab(state.currentTab);
});
