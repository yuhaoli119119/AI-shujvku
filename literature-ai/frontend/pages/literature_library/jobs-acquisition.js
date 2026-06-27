function disconnectSSE() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
}

function initSSE() {
    if (location.protocol === "file:") return;
    disconnectSSE();
    const streamLibraryName = getCurrentLibraryName();
    if (!streamLibraryName) {
        state.paperStreamLibraryName = "";
        return;
    }
    state.paperStreamLibraryName = streamLibraryName;
    state.eventSource = new EventSource(API_BASE + "/stream?" + getFilters().toString());
    state.eventSource.addEventListener("papers_update", function(event) {
        try {
            if (state.paperStreamLibraryName !== getCurrentLibraryName()) {
                return;
            }
            const papers = JSON.parse(event.data) || [];
            const mismatched = papers.filter(function(paper) {
                return paper && paper.library_name && paper.library_name !== state.paperStreamLibraryName;
            });
            if (mismatched.length) {
                console.error("Rejected cross-library SSE papers_update", mismatched);
                showToast("实时刷新返回了非当前库记录，已拒绝渲染", "error");
                return;
            }
            state.papers = papers;
            renderPaperList();
            if (typeof updatePager === "function") {
                updatePager();
            }
        } catch (error) {
            console.error("SSE parse error", error);
        }
    });
}

function clampSearchLimit(value) {
    const n = Number(value || 50);
    if (!Number.isFinite(n)) return 50;
    return Math.max(1, Math.min(50, Math.round(n)));
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

function renderJobProgressNotice(job) {
    const progress = job && job.progress ? job.progress : {};
    const phase = progress.phase || (job && job.status) || "-";
    const current = progress.current || progress.current_item || progress.message || "";
    const total = progress.total || progress.total_items || "";
    const bits = ["阶段：" + phase];
    if (current) bits.push("当前：" + current);
    if (total) bits.push("总数：" + total);
    return '<div class="subtle" style="margin-top:10px;">' + esc(bits.join(" | ")) + '</div>';
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
    setAcquisitionResult('<div class="workspace-empty small-empty">正在从 OpenAlex / arXiv 检索，最多拉取 50 篇...</div>');
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
    return null;
}

function getAIQueryRewriteHint() {
    return "网页端 AI 检索已停用；请使用在线检索，或让 IDE AI 优先通过 MCP 搜索并入库。若当前会话未暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。";
}

async function runAISearch() {
    showToast("网页端 AI 检索已停用，请使用在线检索或 IDE AI；优先走 MCP，若当前会话未暴露工具可改用仓库内 `literature-ai/backend` 的 `app.mcp.*`。", "info");
}

async function runAIWorkflow() {
    showToast("网页端 AI 工作流已停用，请使用在线检索或 IDE AI；优先走 MCP，若当前会话未暴露工具可改用仓库内 `literature-ai/backend` 的 `app.mcp.*`。", "info");
}

async function pollAIWorkflowJob(jobId) {
    if (!jobId) return;
    try {
        const job = await fetchJSON(API_BASE + "/ai_workflow/jobs/" + encodeURIComponent(jobId));
        renderAIWorkflowJob(job);
        if (job.status === "queued" || job.status === "running") {
            setTimeout(function() { pollAIWorkflowJob(jobId); }, 3000);
        } else if (job.status === "completed") {
            showToast("AI 工作流完成，文献列表已刷新。", "success");
            if (typeof resetLibraryPagination === "function") resetLibraryPagination();
            else state.currentOffset = 0;
            refreshLibraryData({ preserveDetail: true, loadingMessage: "正在同步 AI 工作流结果..." });
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

function renderQueuedIngestJob(job) {
    const result = job.result || {};
    const summary = job.summary || {};
    setAcquisitionResult(
        '<div class="writer-block"><h3>后台收录任务</h3>' +
        '<div class="subtle">任务：' + esc(job.job_id || "-") + " | 状态：" + esc(job.status || "-") + " | 文献库：" + esc(job.library_name || getCurrentLibraryName() || "-") + "</div>" +
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;">' +
            renderJobMetric("阶段", summary.phase || job.status || "-") +
            renderJobMetric("成功", summary.success_count) +
            renderJobMetric("失败", summary.failure_count) +
        "</div>" +
        (summary.identifier ? '<div class="subtle">标识符：' + esc(summary.identifier) + "</div>" : "") +
        (summary.source_path ? '<div class="subtle">PDF：' + esc(summary.source_path) + "</div>" : "") +
        (result.title ? '<div class="subtle" style="margin-top:8px;">结果：' + esc(result.title) + " | " + esc(result.status || "-") + "</div>" : "") +
        renderJobFailureExplanation(job) +
        renderJobProgressNotice(job) +
        (job.error ? '<div class="subtle" style="margin-top:10px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
        "</div>"
    );
}

async function pollWorkflowIngestJob(jobId, context) {
    if (!jobId) return;
    try {
        const job = await fetchJSON("/api/jobs/" + encodeURIComponent(jobId));
        renderQueuedIngestJob(job);
        if (job.status === "queued" || job.status === "running") {
            setTimeout(function() { pollWorkflowIngestJob(jobId, context); }, 3000);
        } else if (job.status === "completed") {
            const result = job.result || {};
            if (result.status === "already_exists") {
                showToast("文献已在库中：" + (result.title || ""), "info");
                if (result.paper_id) showAlreadyExistsPrompt(result.paper_id, result.title || "已存在的文献");
            } else if (result.status === "already_linked") {
                showToast("相同的 SI 已经绑定到当前主文献。", "info");
            } else if (result.status === "needs_confirmation") {
                if (context && context.paperId && context.file) {
                    showIdentityConfirmationPrompt(context.paperId, context.file, result);
                } else {
                    showToast("PDF 已解析，但系统需要你重新选择同一文件并确认绑定。", "info");
                }
            } else if (result.status === "identity_mismatch") {
                showIdentityMismatchPrompt(result);
            } else if (result.status === "metadata_only") {
                showToast("已按元数据收录：" + (result.title || ""), "info");
            } else {
                showToast("已完成后台收录：" + (result.title || ""), "success");
            }
            if (typeof resetLibraryPagination === "function") resetLibraryPagination();
            else state.currentOffset = 0;
            refreshLibraryData({
                preserveDetail: true,
                refreshSelectedDetail: true,
                loadingMessage: "正在同步后台解析结果...",
                reason: "ingest_job_completed"
            });
        } else if (job.status === "failed") {
            showToast("后台收录失败：" + (job.error || ""), "error");
        }
    } catch (error) {
        const el = acquisitionResultEl();
        if (el) {
            el.insertAdjacentHTML("afterbegin", '<div class="section-card"><h3>任务轮询失败</h3><div class="subtle">' + esc(error.message) + "</div></div>");
        }
    }
}
