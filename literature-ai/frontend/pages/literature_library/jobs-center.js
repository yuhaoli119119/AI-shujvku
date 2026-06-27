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

function getSelectedPaperForSupplementaryUpload() {
    if (state.selectedPaper && state.selectedPaperId) {
        const selectedStableId = stablePaperIdOf(state.selectedPaper);
        const canonicalSelectedId = canonicalPaperId(state.selectedPaperId);
        if (selectedStableId && canonicalSelectedId && selectedStableId !== canonicalSelectedId) {
            state.selectedPaperId = selectedStableId;
        }
        return state.selectedPaper;
    }
    if (!state.selectedPaperId) {
        return null;
    }
    const selected = resolvePaperFromState(state.selectedPaperId);
    if (selected) {
        state.selectedPaper = selected;
        state.selectedPaperId = stablePaperIdOf(selected) || canonicalPaperId(state.selectedPaperId);
    }
    return selected;
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
    const displayItems = groupWorkflowDownloadJobs(jobs);
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
        (displayItems.length ? displayItems.map(function(item) {
            return item.group ? renderWorkflowJobGroupCard(item) : renderWorkflowJobCard(item.job);
        }).join("") : '<div class="section-card"><h3>暂无任务</h3><div class="muted">当前筛选下没有任务。</div></div>')
    );
}

function groupWorkflowDownloadJobs(jobs) {
    const groups = new Map();
    const items = [];
    jobs.forEach(function(job) {
        if (!isPerPaperWorkflowDownloadJob(job)) {
            items.push({ group: false, job: job });
            return;
        }
        const created = new Date(job.created_at || job.updated_at || 0);
        const when = new Date(job.updated_at || job.created_at || 0);
        const bucket = Number.isNaN(created.getTime()) ? "unknown" : Math.floor(created.getTime() / (10 * 60 * 1000));
        const key = [job.library_name || "", job.type || "", bucket].join("|");
        if (!groups.has(key)) {
            const group = { group: true, key: key, jobs: [], sortTime: when.getTime() || 0 };
            groups.set(key, group);
            items.push(group);
        }
        groups.get(key).jobs.push(job);
    });
    return items.map(function(item) {
        if (!item.group) return item;
        item.jobs.sort(function(a, b) {
            return new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0);
        });
        item.sortTime = Math.max.apply(null, item.jobs.map(function(job) {
            return new Date(job.updated_at || job.created_at || 0).getTime() || 0;
        }));
        return item;
    }).sort(function(a, b) {
        const aTime = a.group ? a.sortTime : new Date((a.job || {}).updated_at || (a.job || {}).created_at || 0).getTime();
        const bTime = b.group ? b.sortTime : new Date((b.job || {}).updated_at || (b.job || {}).created_at || 0).getTime();
        return (bTime || 0) - (aTime || 0);
    });
}

function isPerPaperWorkflowDownloadJob(job) {
    if (!job || job.type !== "discovery_download_ingest") return false;
    const summary = job.summary || {};
    const progress = job.progress || {};
    const result = job.result || {};
    const total = Number(firstPresent(summary.total, progress.total, result.total, 1));
    return total <= 1;
}

function renderWorkflowJobGroupCard(group) {
    const jobs = group.jobs || [];
    const first = jobs[0] || {};
    const completed = jobs.filter(function(job) { return job.status === "completed"; }).length;
    const failed = jobs.filter(function(job) { return job.status === "failed"; }).length;
    const cancelled = jobs.filter(function(job) { return job.status === "cancelled"; }).length;
    const running = jobs.filter(function(job) { return job.status === "queued" || job.status === "running"; }).length;
    const status = running ? "running" : failed ? "failed" : cancelled ? "cancelled" : "completed";
    const success = jobs.reduce(function(sum, job) {
        const summary = job.summary || {};
        const progress = job.progress || {};
        const result = job.result || {};
        const value = Number(firstPresent(summary.success_count, progress.ingested, result.status ? 1 : 0, 0));
        return sum + (Number.isFinite(value) ? value : 0);
    }, 0);
    const metadataOnly = jobs.filter(function(job) { return (job.result || {}).status === "metadata_only"; }).length;
    const groupId = "workflow-job-group-" + String(group.key || "group").replace(/[^A-Za-z0-9_-]+/g, "-");
    const statusLabels = {
        running: "运行中",
        completed: "已完成",
        failed: "有失败",
        cancelled: "已取消",
    };
    const statusMessage = running
        ? "同一时间段触发的单篇下载入库任务已合并展示。"
        : failed
            ? "本组里存在失败任务，可展开逐条重试。"
            : cancelled
                ? "本组任务已取消，可展开查看各条记录。"
                : "同一时间段触发的单篇下载入库任务已合并展示。";
    return '<div class="section-card" id="' + esc(groupId) + '">' +
        '<h3>批量下载入库 · ' + esc(jobs.length) + ' 篇 · ' + esc(statusLabels[status] || status) + '</h3>' +
        '<div class="subtle">文献库 ' + esc(first.library_name || "-") + ' | 更新时间 ' + esc(formatJobTime(first.updated_at || first.created_at)) + '</div>' +
        '<div class="muted" style="margin-top:8px;">' + esc(statusMessage) + '</div>' +
        '<div style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;">' +
            renderJobMetric("成功", success) +
            renderJobMetric("元数据", metadataOnly) +
            renderJobMetric("失败", failed) +
            renderJobMetric("已取消", cancelled) +
            renderJobMetric("运行中", running) +
        '</div>' +
        '<div class="modal-actions" style="justify-content:flex-start;"><button class="btn ghost small" onclick="toggleWorkflowJobGroup(' + JSON.stringify(groupId).replace(/"/g, "&quot;") + ')">展开详情</button></div>' +
        '<div class="workflow-job-group-details" hidden>' + jobs.map(renderWorkflowJobCard).join("") + '</div>' +
    '</div>';
}

function firstPresent() {
    for (let index = 0; index < arguments.length; index += 1) {
        const value = arguments[index];
        if (value !== undefined && value !== null && value !== "") return value;
    }
    return "-";
}

function toggleWorkflowJobGroup(groupId) {
    const card = document.getElementById(groupId);
    const detail = card ? card.querySelector(".workflow-job-group-details") : null;
    const button = card ? card.querySelector(".modal-actions button") : null;
    if (!detail) return;
    detail.hidden = !detail.hidden;
    if (button) button.textContent = detail.hidden ? "展开详情" : "收起详情";
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
