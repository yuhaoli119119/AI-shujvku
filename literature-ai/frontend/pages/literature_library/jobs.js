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
        const prefix = '<div class="writer-block"><h3>AI 自动搜索结果</h3><div class="subtle">已根据你的检索词扩展候选文献；结果会自动去重，下载失败也会先保留元数据，之后可以补 PDF。</div></div>';
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
            setTimeout(function() { pollAIWorkflowJob(jobId); }, 3000);
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

async function pollWorkflowIngestJob(jobId) {
    if (!jobId) return;
    try {
        const job = await fetchJSON("/api/jobs/" + encodeURIComponent(jobId));
        renderQueuedIngestJob(job);
        if (job.status === "queued" || job.status === "running") {
            setTimeout(function() { pollWorkflowIngestJob(jobId); }, 3000);
        } else if (job.status === "completed") {
            const result = job.result || {};
            if (result.status === "already_exists") {
                showToast("文献已在库中：" + (result.title || ""), "info");
                if (result.paper_id) showAlreadyExistsPrompt(result.paper_id, result.title || "已存在的文献");
            } else if (result.status === "metadata_only") {
                showToast("已按元数据收录：" + (result.title || ""), "info");
            } else {
                showToast("已完成后台收录：" + (result.title || ""), "success");
            }
            state.currentOffset = 0;
            refreshCurrentPage();
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

function renderAIWorkflowJob(job) {
    const result = job.result || {};
    setAcquisitionResult(
        '<div class="writer-block"><h3>AI 后台检索 / 收录任务</h3>' +
        '<div class="subtle">任务：' + esc(job.job_id || "-") + " | 状态：" + esc(job.status || "-") + " | 库：" + esc(job.library_name || getCurrentLibraryName() || "-") + "</div>" +
        renderAIWorkflowJobSummary(job) +
        renderJobFailureExplanation(job) +
        renderJobProgressNotice(job) +
        (job.error ? '<div class="subtle" style="margin-top:10px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
        "</div>" +
        (result.prompt_used ? '<div class="section-card"><h3>实际检索式</h3><div class="mono">' + esc(result.prompt_used) + "</div></div>" : "") +
        renderWorkflowList("已收录 / 已存在", result.ingested || [], function(item) {
            let statusBadge = '';
            if (item.status === 'completed') {
                statusBadge = '<span class="status-chip parsed" style="margin-left: 8px;">已收录</span>';
            } else if (item.status === 'metadata_only') {
                statusBadge = '<span class="status-chip meta" style="margin-left: 8px;">元数据</span>';
            } else if (item.status === 'already_exists') {
                statusBadge = '<span class="status-chip duplicate" style="margin-left: 8px;">已存在</span>';
            } else if (item.status === 'merged') {
                statusBadge = '<span class="status-chip parsed" style="margin-left: 8px;">已合并</span>';
            } else {
                statusBadge = '<span class="status-chip none" style="margin-left: 8px;">' + esc(item.status) + '</span>';
            }
            return '<div class="subtle" style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">' +
                       '状态：' + statusBadge + 
                       ' | DOI：' + esc(item.doi || "-") + 
                       ' | 标识符：' + esc(item.identifier || "-") + 
                       (item.paper_id ? ' | <a href="#" style="color:var(--color-primary);text-decoration:underline;" onclick="loadPaperDetail(\'' + item.paper_id + '\'); closeAddLiteraturePanel(); return false;">查看文献</a>' : '') +
                   '</div>';
        }) +
        renderWorkflowList("失败项", result.failed || [], function(item) {
            return '<div class="subtle" style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">' +
                       '代码：<span class="status-chip failed">' + esc(item.code || "未识别") + '</span>' +
                       ' | 原因：' + esc(item.reason || "-") +
                   '</div>';
        }));
}

function renderAIWorkflowJobSummary(job) {
    const summary = job.summary || {};
    return (
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;">' +
            renderJobMetric("检索", summary.searched_total) +
            renderJobMetric("尝试下载", summary.attempted_downloads) +
            renderJobMetric("成功", summary.completed_count == null ? summary.success_count : summary.completed_count) +
            renderJobMetric("已存在", summary.already_exists_count) +
            renderJobMetric("元数据", summary.metadata_only_count) +
            renderJobMetric("失败", summary.failure_count) +
        "</div>" +
        '<div class="subtle">query：' + esc(summary.query || "-") +
            " | 来源：" + esc(summary.source_label || summary.source || job.type || "-") +
            " | 创建：" + esc(formatJobTime(summary.created_at || job.created_at)) +
            " | 更新：" + esc(formatJobTime(summary.updated_at || job.updated_at)) +
            " | 文献库：" + esc(summary.library_name || job.library_name || "-") +
        "</div>" +
        (summary.message ? '<div class="subtle" style="margin-top:8px;">状态说明：' + esc(summary.message) + "</div>" : "")
    );
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
    return openJobCenter();
}

function formatJobTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString();
}

function renderJobMetric(label, value) {
    const text = value == null || value === "" ? "-" : value;
    return '<div style="min-width:96px;"><div class="muted" style="font-size:12px;">' + esc(label) + '</div><div style="font-weight:800;font-size:18px;">' + esc(text) + '</div></div>';
}

function renderExtractionJobSummary(job) {
    const summary = job.summary || {};
    const schemas = Array.isArray(summary.schemas) ? summary.schemas.join(", ") : (summary.schemas || "-");
    const counts = summary.extracted_counts || {};
    const countRows = Object.keys(counts).length
        ? '<div class="subtle" style="margin-top:10px;">解析产物：' + Object.keys(counts).map(function(key) {
            return esc(key) + " " + esc(counts[key]);
        }).join(" | ") + "</div>"
        : "";
    const paperLink = summary.paper_id
        ? ' | <a href="#" style="color:var(--color-primary);text-decoration:underline;" onclick="loadPaperDetail(\'' + escAttr(summary.paper_id) + '\'); closeAddLiteraturePanel(); return false;">打开论文</a>'
        : "";
    return (
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;">' +
            renderJobMetric("成功", summary.success_count) +
            renderJobMetric("失败", summary.failure_count) +
            renderJobMetric("阶段", summary.phase || job.status) +
        "</div>" +
        '<div class="subtle">来源：' + esc(summary.source_label || summary.source || job.type || "-") +
            " | 创建：" + esc(formatJobTime(summary.created_at || job.created_at)) +
            " | 更新：" + esc(formatJobTime(summary.updated_at || job.updated_at)) +
            " | 文献库：" + esc(summary.library_name || job.library_name || "-") +
            paperLink +
        "</div>" +
        '<div class="subtle" style="margin-top:8px;">paper_id：' + esc(summary.paper_id || "-") + " | schemas：" + esc(schemas) + "</div>" +
        (summary.message ? '<div class="subtle" style="margin-top:8px;">状态说明：' + esc(summary.message) + "</div>" : "") +
        countRows
    );
}

function renderJobFailureExplanation(job) {
    const explanation = job.failure_explanation;
    if (!explanation) return "";
    const reasons = explanation.reasons || [];
    return (
        '<div style="margin-top:12px;border-left:3px solid var(--color-danger);padding-left:12px;">' +
            '<div class="subtle" style="color:var(--color-danger);font-weight:700;">' + esc(explanation.summary || "任务失败") + "</div>" +
            reasons.map(function(reason) {
                const examples = (reason.examples || []).slice(0, 3);
                return '<div class="subtle" style="margin-top:8px;">' +
                    '<span class="status-chip failed">' + esc(reason.code || "failed") + "</span> " +
                    esc(reason.label || "-") + " x " + esc(reason.count || 1) +
                    '<div class="muted" style="margin-top:4px;">建议：' + esc(reason.suggestion || "-") + "</div>" +
                    (examples.length ? '<div class="muted" style="margin-top:4px;">示例：' + esc(examples.join("; ")) + "</div>" : "") +
                "</div>";
            }).join("") +
        "</div>"
    );
}

function jobCenterFiltersHtml() {
    const status = state.jobCenterStatus || "";
    const type = state.jobCenterType || "";
    const statusItems = [
        ["", "全部"],
        ["active", "运行中"],
        ["failed", "失败"],
        ["completed", "完成"],
        ["cancelled", "取消"]
    ];
    const typeItems = [
        ["", "全部类型"],
        ["ai_workflow", "检索入库"],
        ["extraction", "结构化解析"],
        ["classify_batch", "批量分类"]
    ];
    return '<div class="modal-actions" style="justify-content:flex-start;margin-top:12px;gap:8px;flex-wrap:wrap;">' +
        statusItems.map(function(item) {
            const active = status === item[0] ? " primary" : " ghost";
            return '<button class="btn small' + active + '" onclick="setJobCenterStatus(' + JSON.stringify(item[0]).replace(/"/g, "&quot;") + ')">' + esc(item[1]) + '</button>';
        }).join("") +
        '<select style="width:auto;min-width:130px;height:32px;padding:4px 8px;" onchange="setJobCenterType(this.value)">' +
            typeItems.map(function(item) {
                return '<option value="' + esc(item[0]) + '"' + (type === item[0] ? " selected" : "") + '>' + esc(item[1]) + '</option>';
            }).join("") +
        "</select>" +
    "</div>";
}

function setJobCenterStatus(status) {
    state.jobCenterStatus = status || "";
    openJobCenter();
}

function setJobCenterType(type) {
    state.jobCenterType = type || "";
    openJobCenter();
}

async function openJobCenter() {
    openAddLiteraturePanel("ai");
    setAcquisitionResult('<div class="workspace-empty small-empty">正在加载任务中心...</div>');
    try {
        const params = new URLSearchParams();
        params.set("limit", "80");
        if (state.jobCenterStatus) params.set("status", state.jobCenterStatus);
        if (state.jobCenterType) params.set("type", state.jobCenterType);
        const libraryName = getCurrentLibraryName();
        if (libraryName) params.set("library_name", libraryName);
        const jobs = await fetchJSON("/api/jobs?" + params.toString());
        renderJobCenter(jobs || []);
    } catch (error) {
        setAcquisitionResult('<div class="workspace-empty small-empty">任务中心加载失败：' + esc(error.message) + "</div>");
    }
}

function renderJobCenter(jobs) {
    const counts = jobs.reduce(function(acc, job) {
        const status = job.status || "unknown";
        acc[status] = (acc[status] || 0) + 1;
        return acc;
    }, {});
    setAcquisitionResult(
        '<div class="writer-block"><h3>任务中心</h3>' +
        '<div class="subtle">统一查看 AI 检索入库、结构化解析和批量分类任务；重试会复用正在运行的同类任务，避免重复入库或重复解析。</div>' +
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;">' +
            renderJobMetric("总数", jobs.length) +
            renderJobMetric("运行中", (counts.queued || 0) + (counts.running || 0)) +
            renderJobMetric("失败", counts.failed || 0) +
            renderJobMetric("完成", counts.completed || 0) +
        "</div>" +
        jobCenterFiltersHtml() +
        "</div>" +
        (jobs.length ? jobs.map(renderWorkflowJobCard).join("") : '<div class="section-card"><h3>暂无任务</h3><div class="muted">当前筛选下没有任务。</div></div>')
    );
}

function renderWorkflowJobCard(job) {
    const summary = job.summary || {};
    const canRetry = job.status === "failed" || job.status === "cancelled";
    const retryHint = canRetry ? '<div class="muted" style="margin-top:6px;">重试会复用 identity 去重与同一 paper_id 替换逻辑，不会重复写入同一篇论文。</div>' : "";
    return '<div class="section-card">' +
        '<h3>' + esc(summary.source_label || job.type || "job") + " · " + esc(job.status || "-") + "</h3>" +
        '<div class="subtle">任务 ' + esc(job.job_id || "-") + " | 文献库 " + esc(job.library_name || "-") + (summary.retried_from_job_id ? " | 重试来源 " + esc(summary.retried_from_job_id) : "") + "</div>" +
        renderJobSummaryByType(job) +
        renderJobFailureExplanation(job) +
        renderJobProgressNotice(job) +
        (job.error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
        (canRetry ? '<div class="modal-actions" style="justify-content:flex-start;"><button class="btn ghost small" onclick="retryWorkflowJob(' + JSON.stringify(job.job_id).replace(/"/g, "&quot;") + ')">重试</button></div>' + retryHint : "") +
    "</div>";
}

function renderJobSummaryByType(job) {
    if (job.type === "ai_workflow") return renderAIWorkflowJobSummary(job);
    if (job.type === "extraction") return renderExtractionJobSummary(job);
    return renderGenericJobSummary(job);
}

function renderGenericJobSummary(job) {
    const summary = job.summary || {};
    return '<div style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;">' +
            renderJobMetric("成功", summary.success_count) +
            renderJobMetric("失败", summary.failure_count) +
            renderJobMetric("总数", summary.total) +
            renderJobMetric("阶段", summary.phase || job.status) +
        "</div>" +
        '<div class="subtle">来源：' + esc(summary.source_label || summary.source || job.type || "-") +
            " | 创建：" + esc(formatJobTime(summary.created_at || job.created_at)) +
            " | 更新：" + esc(formatJobTime(summary.updated_at || job.updated_at)) +
            " | 文献库：" + esc(summary.library_name || job.library_name || "-") +
        "</div>" +
        (summary.message ? '<div class="subtle" style="margin-top:8px;">状态说明：' + esc(summary.message) + "</div>" : "");
}

async function retryWorkflowJob(jobId) {
    if (!jobId) return;
    try {
        const job = await fetchJSON("/api/jobs/" + encodeURIComponent(jobId) + "/retry", { method: "POST" });
        const prefix = job.deduplicated ? "已有同类任务在运行，已复用：" : "重试已入队：";
        showToast(prefix + job.job_id, "success");
        openJobCenter();
    } catch (error) {
        showToast("重试失败：" + error.message, "error");
    }
}

function renderExtractionJobs(jobs) {
    if (!jobs.length) {
        setAcquisitionResult('<div class="writer-block"><h3>解析任务中心</h3><div class="subtle">暂无解析任务。可以在文献详情中点击重新解析，或在校验工作台中创建任务。</div></div>');
        return;
    }
    setAcquisitionResult(
        '<div class="writer-block"><h3>解析任务中心</h3><div class="subtle">解析任务会保存在 workflow_jobs 中，刷新页面后仍可查看。</div></div>' +
        jobs.map(function(job) {
            const canRetry = job.status === "failed" || job.status === "cancelled";
            return (
                '<div class="section-card">' +
                    '<h3>' + esc(job.type || "extraction") + " · " + esc(job.status || "-") + "</h3>" +
                    '<div class="subtle">任务 ' + esc(job.job_id || "-") + " | 文献库 " + esc(job.library_name || "-") + "</div>" +
                    renderExtractionJobSummary(job) +
                    renderJobFailureExplanation(job) +
                    renderJobProgressNotice(job) +
                    (job.error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
                    (canRetry ? '<div class="modal-actions" style="justify-content:flex-start;"><button class="btn ghost small" onclick="retryExtractionJob(' + JSON.stringify(job.job_id).replace(/"/g, "&quot;") + ')">重试</button></div>' : "") +
                "</div>"
            );
        }).join("")
    );
}

async function retryExtractionJob(jobId) {
    return retryWorkflowJob(jobId);
}

async function downloadIdentifier(identifier) {
    if (!identifier) return;
    showProgress("正在创建后台收录任务...");
    try {
        const job = await fetchJSON(API_BASE + "/discovery/download/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ identifier: identifier, providers: [], library_name: getCurrentLibraryName() })
        });
        showToast("收录任务已进入后台队列。", "success");
        renderQueuedIngestJob(job);
        pollWorkflowIngestJob(job.job_id);
    } catch (error) {
        const detail = error.detail;
        if (detail && detail.status === "already_exists") {
            showToast("收录失败：该文献已存在", "error");
            showAlreadyExistsPrompt(detail.paper_id, detail.title || "已存在文献");
        } else {
            showToast("收录失败：" + error.message, "error");
        }
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
        if (data.status === "merged") {
            showToast("已成功与现有元数据文献合并：" + (data.title || file.name), "success");
            state.selectedPaperId = data.paper_id;
            refreshCurrentPage();
        } else {
            showToast("已上传并收录：" + (data.title || file.name), "success");
            state.currentOffset = 0;
            refreshCurrentPage();
        }
    } catch (error) {
        const detail = error.detail;
        if (detail && detail.status === "already_exists") {
            showToast("上传失败：该文献已存在", "error");
            showAlreadyExistsPrompt(detail.paper_id, detail.title || "已存在文献");
        } else {
            showToast("上传失败：" + error.message, "error");
        }
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
                '<div class="section-card"><h3>最近一次重解析结果</h3><div class="subtle">任务已创建或完成。状态：' + esc(data.status || data.job_status || "已提交") + (data.job_id ? " | 任务：" + esc(data.job_id) : "") + "</div></div>"
            );
        }
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("重解析失败：" + error.message, "error");
    }
    hideProgress();
}

function showAlreadyExistsPrompt(paperId, title) {
    const existing = document.querySelector(".already-exists-toast");
    if (existing) existing.remove();
    
    const container = document.createElement("div");
    container.className = "toast error already-exists-toast";
    container.style.display = "flex";
    container.style.flexDirection = "column";
    container.style.gap = "8px";
    container.style.padding = "16px";
    container.style.maxWidth = "360px";
    container.style.background = "var(--color-surface)";
    container.style.border = "1px solid var(--color-danger)";
    container.style.color = "var(--color-text)";
    container.style.boxShadow = "var(--shadow-elevated)";
    container.style.position = "fixed";
    container.style.right = "18px";
    container.style.top = "18px";
    container.style.zIndex = "3100";
    
    container.innerHTML = 
        '<div style="font-weight:700;color:var(--color-danger);font-size:14px;margin-bottom:2px;">⚠️ 文献已存在</div>' +
        '<div style="font-size:13px;color:var(--color-text-secondary);word-break:break-all;">' + esc(title) + '</div>' +
        '<div style="display:flex;gap:8px;margin-top:6px;justify-content:flex-end;">' +
            '<button class="btn primary small" id="jumpToPaperBtn" style="height:28px;padding:0 10px;font-size:12px;">跳转查看</button>' +
            '<button class="btn ghost small" id="closeExistsToastBtn" style="height:28px;padding:0 10px;font-size:12px;">关闭</button>' +
        '</div>';
        
    document.body.appendChild(container);
    
    container.querySelector("#jumpToPaperBtn").onclick = function(e) {
        e.preventDefault();
        e.stopPropagation();
        loadPaperDetail(paperId);
        container.remove();
    };
    container.querySelector("#closeExistsToastBtn").onclick = function(e) {
        e.preventDefault();
        e.stopPropagation();
        container.remove();
    };
    
    setTimeout(function() {
        if (container.parentNode) {
            container.style.opacity = "0";
            setTimeout(function() { container.remove(); }, 280);
        }
    }, 8000);
}

function showIdentityConfirmationPrompt(paperId, file, detail) {
    const existing = document.getElementById("identityConfirmModal");
    if (existing) existing.remove();
    
    const target = detail.target || {};
    const incoming = detail.incoming || {};
    const matchScore = detail.match_score;
    const matchScoreText = matchScore != null ? (Number(matchScore) * 100).toFixed(0) + "%" : "未知";
    const doiText = (doi) => (doi && doi.trim()) ? doi : "未识别";
    const yearText = (year) => (year != null && year !== "") ? year : "未识别";
    
    const container = document.createElement("div");
    container.id = "identityConfirmModal";
    container.className = "modal-overlay";
    container.style.cssText = "display: flex; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 3200; justify-content: center; align-items: center; padding: 20px;";
    
    container.innerHTML = 
        '<div class="modal" style="max-width: 600px; width: 100%; background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-lg); padding: 24px; box-shadow: var(--shadow-elevated);">' +
            '<div class="modal-title-row" style="margin-bottom: 16px;">' +
                '<h3 style="margin: 0; color: var(--color-warning);">⚠️ 需要确认文献身份</h3>' +
            '</div>' +
            '<div style="margin-bottom: 18px; font-size: 14px; line-height: 1.5;">' +
                '<p style="margin-top: 0; color: var(--color-text-secondary);">系统认为这份 PDF 与当前 metadata-only 条目匹配置信度较低（匹配度：<strong style="color: var(--color-primary);">' + matchScoreText + '</strong>）。</p>' +
                '<p style="margin-bottom: 12px;"><strong>匹配原因：</strong> ' + esc(detail.match_reason || "未知") + '</p>' +
                
                '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; background: var(--color-surface-alt); padding: 12px; border-radius: var(--radius); border: 1px solid var(--color-border);">' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">当前条目信息 (Target)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(target.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> ' + esc(doiText(target.doi)) + '</div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(target.year)) + '</div>' +
                    '</div>' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">上传 PDF 信息 (Incoming)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(incoming.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> ' + esc(doiText(incoming.doi)) + '</div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(incoming.year)) + '</div>' +
                    '</div>' +
                '</div>' +
                
                '<div style="border-left: 4px solid var(--color-warning); background: var(--color-warning-bg); padding: 10px; border-radius: var(--radius); color: var(--color-warning); font-weight: bold; font-size: 13px;">' +
                    '风险提示：系统认为这份 PDF 与当前 metadata-only 条目匹配置信度较低。确认后会绑定到当前文献条目，并保留当前 paper_id。' +
                '</div>' +
            '</div>' +
            '<div class="modal-actions" style="display: flex; gap: 12px; justify-content: flex-end;">' +
                '<button class="btn ghost" id="confirmCancelBtn">取消</button>' +
                '<button class="btn primary" id="confirmAttachBtn">确认绑定</button>' +
            '</div>' +
        '</div>';
        
    document.body.appendChild(container);
    
    container.querySelector("#confirmCancelBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
    };
    container.querySelector("#confirmAttachBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
        attachPDFToPaperFile(paperId, file, true);
    };
}

function showIdentityMismatchPrompt(detail) {
    const existing = document.getElementById("identityMismatchModal");
    if (existing) existing.remove();
    
    const target = detail.target || {};
    const incoming = detail.incoming || {};
    const doiText = (doi) => (doi && doi.trim()) ? doi : "未识别";
    const yearText = (year) => (year != null && year !== "") ? year : "未识别";
    
    const container = document.createElement("div");
    container.id = "identityMismatchModal";
    container.className = "modal-overlay";
    container.style.cssText = "display: flex; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 3200; justify-content: center; align-items: center; padding: 20px;";
    
    container.innerHTML = 
        '<div class="modal" style="max-width: 600px; width: 100%; background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-lg); padding: 24px; box-shadow: var(--shadow-elevated);">' +
            '<div class="modal-title-row" style="margin-bottom: 16px;">' +
                '<h3 style="margin: 0; color: var(--color-danger);">❌ 文献身份冲突</h3>' +
            '</div>' +
            '<div style="margin-bottom: 18px; font-size: 14px; line-height: 1.5;">' +
                '<p style="margin-top: 0; color: var(--color-danger); font-weight: bold;">目标条目和上传 PDF 的 DOI 冲突，系统已阻止绑定。请检查是否上传错 PDF，或将 PDF 作为新文献导入。</p>' +
                '<p style="margin-bottom: 12px;"><strong>匹配原因：</strong> ' + esc(detail.match_reason || "未知") + '</p>' +
                
                '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; background: var(--color-surface-alt); padding: 12px; border-radius: var(--radius); border: 1px solid var(--color-border);">' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">当前条目信息 (Target)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(target.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> <strong style="color: var(--color-danger);">' + esc(doiText(target.doi)) + '</strong></div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(target.year)) + '</div>' +
                    '</div>' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">上传 PDF 信息 (Incoming)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(incoming.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> <strong style="color: var(--color-danger);">' + esc(doiText(incoming.doi)) + '</strong></div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(incoming.year)) + '</div>' +
                    '</div>' +
                '</div>' +
            '</div>' +
            '<div class="modal-actions" style="display: flex; gap: 12px; justify-content: flex-end;">' +
                '<button class="btn ghost" id="mismatchCancelBtn">取消</button>' +
                '<button class="btn primary" id="mismatchUploadNewBtn">作为新文献上传</button>' +
            '</div>' +
        '</div>';
        
    document.body.appendChild(container);
    
    container.querySelector("#mismatchCancelBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
    };
    container.querySelector("#mismatchUploadNewBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
        closeAddLiteraturePanel();
        const pdfUpload = document.getElementById("pdfUpload");
        if (pdfUpload) pdfUpload.click();
    };
}

async function attachPDFToPaperFile(paperId, file, confirmIdentityMismatch) {
    if (!paperId || !file) return;
    const formData = new FormData();
    formData.append("file", file);
    formData.append("confirm_identity_mismatch", confirmIdentityMismatch ? "true" : "false");
    
    showProgress("正在上传并关联 PDF：" + file.name);
    let keepProgress = false;
    try {
        const data = await fetchJSON(API_BASE + "/" + paperId + "/attach-pdf", {
            method: "POST",
            body: formData
        });
        if (confirmIdentityMismatch) {
            showToast("PDF 已经经人工确认绑定到当前文献条目。", "success");
        } else {
            showToast("PDF 关联成功：" + (data.title || file.name), "success");
        }
        state.selectedPaperId = paperId;
        closeAddLiteraturePanel();
        refreshCurrentPage();
    } catch (error) {
        const detail = error.detail;
        if (detail && typeof detail === "object") {
            if (detail.status === "needs_confirmation") {
                keepProgress = true;
                hideProgress();
                showIdentityConfirmationPrompt(paperId, file, detail);
                return;
            } else if (detail.status === "identity_mismatch") {
                keepProgress = true;
                hideProgress();
                showIdentityMismatchPrompt(detail);
                return;
            } else if (detail.status === "already_exists") {
                keepProgress = true;
                hideProgress();
                showToast("系统发现该文献已有 PDF，未覆盖已有文件。", "error");
                showAlreadyExistsPrompt(detail.target_paper_id || detail.paper_id, detail.target?.title || detail.incoming?.title || detail.title || "已存在文献");
                return;
            }
        }
        showToast("关联失败：" + error.message, "error");
    } finally {
        if (!keepProgress) {
            hideProgress();
        }
    }
}

function triggerAttachPDF() {
    const selectEl = $("attachPaperSelect");
    if (!selectEl || !selectEl.value) {
        showToast("请先选择一个元数据文献条目。", "error");
        return;
    }
    const fileInput = $("attachPdfInputModal");
    if (fileInput) fileInput.click();
}

async function uploadAttachPDFModal(input) {
    if (!input.files || !input.files.length) return;
    const selectEl = $("attachPaperSelect");
    if (!selectEl || !selectEl.value) {
        showToast("未选中目标文献条目。", "error");
        return;
    }
    const paperId = selectEl.value;
    const file = input.files[0];
    try {
        await attachPDFToPaperFile(paperId, file);
    } finally {
        input.value = "";
    }
}

async function attachPDFToPaperDetail(input, paperId) {
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    try {
        await attachPDFToPaperFile(paperId, file);
    } finally {
        input.value = "";
    }
}

async function loadMetadataOnlyPapers() {
    const selectEl = $("attachPaperSelect");
    if (!selectEl) return;
    selectEl.innerHTML = '<option value="">正在加载元数据条目...</option>';
    try {
        const params = new URLSearchParams();
        params.set("limit", 100);
        const libraryName = getCurrentLibraryName();
        if (libraryName) params.set("library_name", libraryName);
        
        const papers = await fetchJSON(API_BASE + "?" + params.toString());
        const metaOnly = (papers || []).filter(function(p) { return p.oa_status === "metadata_only"; });
        
        if (metaOnly.length === 0) {
            selectEl.innerHTML = '<option value="">无待上传 PDF 的元数据条目</option>';
        } else {
            selectEl.innerHTML = '<option value="">-- 请选择文献 --</option>' + 
                metaOnly.map(function(p) {
                    return '<option value="' + p.id + '">' + esc(p.title || "未命名文献") + '</option>';
                }).join("");
        }
    } catch (error) {
        selectEl.innerHTML = '<option value="">加载失败：' + esc(error.message) + '</option>';
    }
}

Object.assign(window, {
    openExtractionJobCenter: openJobCenter,
    openJobCenter: openJobCenter,
    setJobCenterStatus: setJobCenterStatus,
    setJobCenterType: setJobCenterType,
    retryWorkflowJob: retryWorkflowJob,
    retryExtractionJob: retryWorkflowJob,
    triggerAttachPDF: triggerAttachPDF,
    uploadAttachPDFModal: uploadAttachPDFModal,
    attachPDFToPaperDetail: attachPDFToPaperDetail,
    loadMetadataOnlyPapers: loadMetadataOnlyPapers,
    showAlreadyExistsPrompt: showAlreadyExistsPrompt
});
