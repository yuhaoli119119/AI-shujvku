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

function acquisitionResultEl() {
    return $("acquisitionResult");
}

function setAcquisitionResult(html) {
    const el = acquisitionResultEl();
    if (el) el.innerHTML = html;
}

async function searchOnline() {
    const onlineQuery = $("onlineSearchQuery");
    const searchInput = $("searchInput");
    const query = ((onlineQuery && onlineQuery.value) || (searchInput ? searchInput.value : "") || "").trim();
    if (!query) {
        showToast("请先输入检索关键词。", "error");
        return;
    }
    openAddLiteraturePanel("online");
    if (onlineQuery) onlineQuery.value = query;
    setAcquisitionResult('<div class="workspace-empty small-empty">正在从 OpenAlex / arXiv 检索，最多拉取 100 篇...</div>');
    try {
        const maxResults = $("onlineSearchMaxResults");
        const limit = clampSearchLimit(maxResults ? maxResults.value : 100);
        const data = await fetchJSON(API_BASE + "/discovery/search?q=" + encodeURIComponent(query) + "&limit=" + limit);
        const stats = mergeDiscoveryResults(data.items || []);
        renderDiscoveryResults({ items: state.discoveryCache }, stats, "在线检索结果");
    } catch (error) {
        setAcquisitionResult('<div class="workspace-empty small-empty">在线检索失败：' + esc(error.message) + "</div>");
    }
}

function renderDiscoveryResults(data, stats, title, prefixHtml) {
    const items = data && data.items ? data.items : [];
    if (!items.length) {
        setAcquisitionResult('<div class="workspace-empty small-empty">没有找到在线结果。</div>');
        return;
    }
    setAcquisitionResult(
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
        }).join(""));
}

function getAIQueryRewriteModel() {
    if (!state.writerSettings) return null;
    const model = (state.writerSettings.writer_model || "").trim();
    return model || null;
}

function getAIQueryRewriteHint() {
    if (!state.writerSettings) return "";
    const backend = (state.writerSettings.writer_backend || "").trim() || "rule";
    const apiBase = (state.writerSettings.writer_api_base || "").trim();
    const apiKey = (state.writerSettings.writer_api_key || "").trim();
    if (!apiBase || !apiKey || backend === "rule") {
        return "当前未配置可用的 Writer LLM，已退回普通关键词检索。请在设置页填写 Writer LLM 配置。";
    }
    return "";
}

async function runAISearch() {
    const aiQuery = $("aiSearchQuery");
    const searchInput = $("searchInput");
    const query = ((aiQuery ? aiQuery.value : "") || (searchInput ? searchInput.value : "")).trim();
    if (!query) {
        showToast("请输入 AI 搜索查询。", "error");
        return;
    }
    openAddLiteraturePanel("ai");
    if (aiQuery) aiQuery.value = query;
    setAcquisitionResult('<div class="workspace-empty small-empty">AI 正在扩展查询并筛选文献...</div>');
    try {
        const maxResults = $("aiSearchMaxResults");
        const data = await fetchJSON(API_BASE + "/ai_search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query: query,
                model: getAIQueryRewriteModel(),
                max_results: clampSearchLimit(maxResults ? maxResults.value : 100),
                providers: [],
                skip_guard: false
            })
        });
        const papers = data && data.papers ? data.papers : [];
        if (!papers.length) {
            setAcquisitionResult('<div class="workspace-empty small-empty">AI 没有返回结果。</div>');
            return;
        }
        const stats = mergeDiscoveryResults(papers);
        const prefix = '<div class="writer-block"><h3>AI 自动搜索结果</h3><div class="subtle">模型状态：' + esc(data.llm_status || "unknown") + " | 注释状态：" + esc(data.result_annotation_status || "-") + '</div><div class="mono" style="margin-top:12px;">' + esc(data.prompt_used || "") + "</div></div>";
        renderDiscoveryResults({ items: state.discoveryCache }, stats, "AI 自动搜索结果", prefix);
    } catch (error) {
        setAcquisitionResult('<div class="workspace-empty small-empty">AI 搜索失败：' + esc(error.message) + "</div>");
    }
}

async function runAIWorkflow() {
    const aiQuery = $("aiSearchQuery");
    const searchInput = $("searchInput");
    const query = ((aiQuery ? aiQuery.value : "") || (searchInput ? searchInput.value : "")).trim();
    if (!query) {
        showToast("请输入 AI 搜索查询。", "error");
        return;
    }
    openAddLiteraturePanel("ai");
    showProgress("AI 工作流已转入后台，不会卡住页面...");
    try {
        const maxResults = $("aiSearchMaxResults");
        const maxDownloads = $("aiWorkflowMaxDownloads");
        const job = await fetchJSON(API_BASE + "/ai_workflow/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query: query,
                library_name: getCurrentLibraryName(),
                model: getAIQueryRewriteModel(),
                max_results: clampSearchLimit(maxResults ? maxResults.value : 100),
                max_downloads: clampSearchLimit(maxDownloads ? maxDownloads.value : 100),
                providers: [],
                skip_existing: true
            })
        });
        state.aiWorkflowJobId = job.job_id;
        renderAIWorkflowJob(job);
        pollAIWorkflowJob(job.job_id);
        showToast("AI 工作流已进入后台任务。", "success");
    } catch (error) {
        setAcquisitionResult('<div class="workspace-empty small-empty">AI 工作流失败：' + esc(error.message) + "</div>");
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
        const el = acquisitionResultEl();
        if (el) {
            el.insertAdjacentHTML("afterbegin", '<div class="section-card"><h3>任务轮询失败</h3><div class="subtle">' + esc(error.message) + "</div></div>");
        }
    }
}

function renderAIWorkflowJob(job) {
    const result = job.result || {};
    setAcquisitionResult(
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
        }));
}

function renderWorkflowList(title, items, formatter) {
    if (!items.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无。</div></div>';
    }
    return items.map(function(item) {
        return '<div class="section-card"><h3>' + esc(title) + " - " + esc(item.title || item.identifier || "未命名") + "</h3>" + formatter(item) + "</div>";
    }).join("");
}

async function openExtractionJobCenter() {
    openAddLiteraturePanel("ai");
    setAcquisitionResult('<div class="workspace-empty small-empty">Loading extraction jobs...</div>');
    try {
        const jobs = await fetchJSON("/api/extraction/jobs?limit=30");
        renderExtractionJobs(jobs || []);
    } catch (error) {
        setAcquisitionResult('<div class="workspace-empty small-empty">Extraction Job Center failed: ' + esc(error.message) + "</div>");
    }
}

function renderExtractionJobs(jobs) {
    if (!jobs.length) {
        setAcquisitionResult('<div class="writer-block"><h3>Extraction Job Center</h3><div class="subtle">No extraction jobs yet. Use Re-extract on a paper or the validation workbench to queue one.</div></div>');
        return;
    }
    setAcquisitionResult(
        '<div class="writer-block"><h3>Extraction Job Center</h3><div class="subtle">Persistent extraction jobs survive refresh through workflow_jobs.</div></div>' +
        jobs.map(function(job) {
            const canRetry = job.status === "failed" || job.status === "cancelled";
            return (
                '<div class="section-card">' +
                    '<h3>' + esc(job.type || "extraction") + " · " + esc(job.status || "-") + "</h3>" +
                    '<div class="subtle">Job ' + esc(job.job_id || "-") + " | Library " + esc(job.library_name || "-") + "</div>" +
                    '<div class="mono" style="margin-top:10px;">' + esc(JSON.stringify(job.progress || {}, null, 2)) + "</div>" +
                    (job.error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
                    (canRetry ? '<div class="modal-actions" style="justify-content:flex-start;"><button class="btn ghost small" onclick="retryExtractionJob(' + JSON.stringify(job.job_id).replace(/"/g, "&quot;") + ')">Retry</button></div>' : "") +
                "</div>"
            );
        }).join("")
    );
}

async function retryExtractionJob(jobId) {
    if (!jobId) return;
    try {
        const job = await fetchJSON("/api/extraction/jobs/" + encodeURIComponent(jobId) + "/retry", { method: "POST" });
        showToast("Extraction retry queued: " + job.job_id, "success");
        openExtractionJobCenter();
    } catch (error) {
        showToast("Extraction retry failed: " + error.message, "error");
    }
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
    const doiInput = $("doiInput");
    const identifier = doiInput ? doiInput.value.trim() : "";
    if (!identifier) {
        showToast("请输入 DOI 或 URL。", "error");
        return;
    }
    downloadIdentifier(identifier).then(function() {
        if (doiInput) doiInput.value = "";
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
        const summary = $("summaryContent");
        if (summary) {
            summary.insertAdjacentHTML("afterbegin",
                '<div class="section-card"><h3>最近一次重解析结果</h3><div class="mono">' + esc(JSON.stringify(data, null, 2)) + "</div></div>"
            );
        }
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("重解析失败：" + error.message, "error");
    }
    hideProgress();
}
