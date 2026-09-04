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

// ---- 任务轮询调度：单链 / 隐藏与离线暂停 / 恢复即查 / 错误去重 / 最大持续时间 ----
const JOB_POLL_INTERVAL_MS = 3000;
const JOB_POLL_RETRY_INTERVAL_MS = 15000;
const JOB_POLL_MAX_DURATION_MS = 10 * 60 * 1000;
const jobPollChains = {};

function jobPollKey(kind, jobId) {
    return kind + ":" + String(jobId || "");
}

function ensureJobPollChain(kind, jobId, context) {
    const key = jobPollKey(kind, jobId);
    if (!jobPollChains[key]) {
        jobPollChains[key] = {
            kind: kind,
            jobId: jobId,
            context: context || null,
            startedAt: Date.now(),
            running: false,
            scheduled: false,
            timerId: null,
            errorNotified: false
        };
    } else if (context) {
        jobPollChains[key].context = context;
    }
    return jobPollChains[key];
}

function finishJobPollChain(kind, jobId) {
    const key = jobPollKey(kind, jobId);
    const chain = jobPollChains[key];
    if (chain && chain.timerId) clearTimeout(chain.timerId);
    delete jobPollChains[key];
}

function scheduleJobPoll(kind, jobId, context, delay) {
    const key = jobPollKey(kind, jobId);
    const chain = jobPollChains[key];
    if (!chain || chain.scheduled) return;
    if (Date.now() - chain.startedAt > JOB_POLL_MAX_DURATION_MS) {
        finishJobPollChain(kind, jobId);
        showToast("任务状态查询超时，请刷新页面确认任务状态。", "info");
        return;
    }
    chain.scheduled = true;
    chain.timerId = setTimeout(function() {
        const current = jobPollChains[key];
        if (!current) return;
        current.scheduled = false;
        current.timerId = null;
        if (document.hidden || navigator.onLine === false) {
            scheduleJobPoll(kind, jobId, context, JOB_POLL_RETRY_INTERVAL_MS);
            return;
        }
        pollWorkflowIngestJob(jobId, context);
    }, delay);
}

function handleJobPollError(kind, jobId, context, error) {
    const chain = ensureJobPollChain(kind, jobId, context);
    if (!chain.errorNotified) {
        chain.errorNotified = true;
        showToast("任务状态查询失败，将自动低频重试：" + (error && error.message ? error.message : error), "info");
    }
    scheduleJobPoll(kind, jobId, context, JOB_POLL_RETRY_INTERVAL_MS);
}

function resumeJobPolls() {
    Object.keys(jobPollChains).forEach(function(key) {
        const chain = jobPollChains[key];
        if (!chain || chain.running) return;
        // 恢复即查：撤销尚未触发的定时器，立即轮询，不再等待原有延迟
        if (chain.timerId) {
            clearTimeout(chain.timerId);
            chain.timerId = null;
        }
        chain.scheduled = false;
        pollWorkflowIngestJob(chain.jobId, chain.context);
    });
}

document.addEventListener("visibilitychange", function() {
    if (!document.hidden) resumeJobPolls();
});
window.addEventListener("online", resumeJobPolls);

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
    const chain = ensureJobPollChain("ingest", jobId, context);
    if (chain.running) return;
    chain.running = true;
    try {
        const job = await fetchJSON("/api/jobs/" + encodeURIComponent(jobId));
        renderQueuedIngestJob(job);
        chain.errorNotified = false;
        if (job.status === "queued" || job.status === "running") {
            scheduleJobPoll("ingest", jobId, context, JOB_POLL_INTERVAL_MS);
        } else if (job.status === "completed") {
            finishJobPollChain("ingest", jobId);
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
            finishJobPollChain("ingest", jobId);
            showToast("后台收录失败：" + (job.error || ""), "error");
        } else {
            finishJobPollChain("ingest", jobId);
        }
    } catch (error) {
        handleJobPollError("ingest", jobId, context, error);
    } finally {
        chain.running = false;
    }
}
