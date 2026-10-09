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
        ["extraction", "结构化解析"],
        ["classify_batch", "批量分类"],
        ["agent_activity", "AI任务记录"]
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
    openAddLiteraturePanel("pdf");
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
        '<div class="subtle">统一查看 PDF 解析、结构化解析、批量分类和 AI 批次任务；重试会复用正在运行的同类任务，避免重复解析。</div>' +
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
    const title = summary.title || summary.task_display_name || summary.source_label || job.type || "job";
    return '<div class="section-card">' +
        '<h3>' + esc(title) + " · " + esc(job.status || "-") + "</h3>" +
        '<div class="subtle">任务 ' + esc(job.job_id || "-") + " | 文献库 " + esc(job.library_name || "-") + (summary.retried_from_job_id ? " | 重试来源 " + esc(summary.retried_from_job_id) : "") + "</div>" +
        renderJobSummaryByType(job) +
        renderJobFailureExplanation(job) +
        renderJobProgressNotice(job) +
        (job.error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">' + esc(job.error) + "</div>" : "") +
        (canRetry ? '<div class="modal-actions" style="justify-content:flex-start;"><button class="btn ghost small" onclick="retryWorkflowJob(' + JSON.stringify(job.job_id).replace(/"/g, "&quot;") + ')">重试</button></div>' + retryHint : "") +
    "</div>";
}

function renderJobSummaryByType(job) {
    if (job.type === "extraction") return renderExtractionJobSummary(job);
    return renderGenericJobSummary(job);
}

function renderGenericJobSummary(job) {
    const summary = job.summary || {};
    const problemItems = Array.isArray(summary.problem_items) ? summary.problem_items.slice(0, 5) : [];
    const problemHtml = problemItems.length
        ? '<details style="margin-top:10px;"><summary>问题项 ' + esc(summary.problem_count || problemItems.length) + '</summary>' +
            '<div style="display:grid;gap:8px;margin-top:8px;">' +
                problemItems.map(function(item) {
                    const bits = [
                        item.status || item.code || "problem",
                        item.candidate_type || item.target_type || "",
                        item.field_name || item.target_path || "",
                        item.reason || item.error || item.message || ""
                    ].filter(Boolean).join(" | ");
                    return '<div class="subtle">' + esc(bits) + '</div>';
                }).join("") +
            '</div>' +
        '</details>'
        : "";
    const paperText = summary.paper_code || summary.paper_title
        ? '<div class="subtle" style="margin-top:8px;">文献：' + esc(summary.paper_code || "-") + (summary.paper_title ? " | " + esc(summary.paper_title) : "") + "</div>"
        : "";
    const runLink = summary.external_analysis_run_id
        ? ' | <a href="/api/external-analysis/runs/' + encodeURIComponent(summary.external_analysis_run_id) + '" target="_blank">run 详情</a>'
        : "";
    return '<div style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;">' +
            renderJobMetric("成功", summary.success_count) +
            renderJobMetric("失败", summary.failure_count) +
            renderJobMetric("总数", summary.total) +
            renderJobMetric("待处理", summary.pending_count) +
            renderJobMetric("阻断", summary.blocking_count) +
            renderJobMetric("问题", summary.problem_count) +
            renderJobMetric("阶段", summary.phase || job.status) +
        "</div>" +
        '<div class="subtle">来源：' + esc(summary.source_display || summary.source_label || summary.source || job.type || "-") +
            " | 创建：" + esc(formatJobTime(summary.created_at || job.created_at)) +
            " | 更新：" + esc(formatJobTime(summary.updated_at || job.updated_at)) +
            " | 文献库：" + esc(summary.library_name || job.library_name || "-") +
        "</div>" +
        paperText +
        (summary.module_label ? '<div class="subtle" style="margin-top:8px;">模块：' + esc(summary.module_label) + ' | 当前阶段：' + esc(summary.lifecycle || summary.phase || "-") + ' | 最后动作：' + esc(summary.last_action || "-") + runLink + '</div>' : '') +
        (summary.summary_text ? '<div class="subtle" style="margin-top:8px;">批次摘要：' + esc(summary.summary_text) + "</div>" : "") +
        (summary.message && summary.message !== summary.summary_text ? '<div class="subtle" style="margin-top:8px;">状态说明：' + esc(summary.message) + "</div>" : "") +
        problemHtml;
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
