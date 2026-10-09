"use strict";

const state = {
  query: "",
  papers: [],
  selected: null,
  job: null,
  pollTimer: null,
  selectedId: "",
  selectionSeq: 0,
};

const initialPaperId = new URLSearchParams(location.search).get("paper_id");

const STEP_LINKS = {
  figure: "../figure_assets/index.html",
  data: "../data_table/index.html",
  analysis: "../summary_analysis/index.html",
};

function $(id) { return document.getElementById(id); }

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function paperLabel(paper) {
  return paper.paper_code || paper.serial_number || "—";
}

function count(paper, field) {
  const value = Number(paper ? paper[field] : 0);
  return Number.isFinite(value) ? value : 0;
}

function renderStatus(message, kind) {
  const area = $("statusArea");
  if (!message) { area.innerHTML = ""; return; }
  const className = kind === "error" ? "rebuild-error" : kind === "progress" ? "rebuild-progress" : "rebuild-empty";
  area.innerHTML = `<div class="${className}" role="status">${esc(message)}</div>`;
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options));
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { const payload = await response.json(); detail = payload.detail || detail; } catch (_) {}
    throw new Error(String(detail));
  }
  return response.json();
}

async function loadPapers() {
  $("resultSummary").textContent = "正在读取…";
  try {
    const params = new URLSearchParams({ q: state.query });
    const data = await api(`/api/rebuild/papers?${params}`);
    state.papers = data.items || [];
    renderResults();
  } catch (error) {
    $("resultSummary").textContent = "读取失败";
    $("searchResults").innerHTML = `<div class="rebuild-error" role="status">读取失败：${esc(error.message)}</div>`;
  }
  if (initialPaperId && !state.selectedId) await selectPaper(initialPaperId);
}

function renderResults() {
  const host = $("searchResults");
  $("resultSummary").textContent = state.papers.length
    ? `${state.papers.length} 篇结果 · 点击选中`
    : "";
  if (!state.papers.length) {
    host.innerHTML = `<div class="rebuild-empty">没有匹配文献</div>`;
    return;
  }
  host.innerHTML = state.papers.map((paper) => `
    <button class="rebuild-result-item" type="button" data-id="${esc(paper.id)}">
      <span class="rebuild-result-code">${esc(paperLabel(paper))}</span>
      <span class="rebuild-result-title">${esc(paper.title || "无标题")}</span>
      <span class="rebuild-result-counts">图表 ${count(paper, "assets")} · 数据 ${count(paper, "rows")} · 文件 ${count(paper, "files")}</span>
    </button>
  `).join("");
  host.querySelectorAll("button[data-id]").forEach((button) => {
    button.addEventListener("click", () => selectPaper(button.dataset.id));
  });
  markActiveResult();
}

function markActiveResult() {
  const activeId = state.selected ? state.selected.id : null;
  document.querySelectorAll("#searchResults button[data-id]").forEach((button) => {
    button.classList.toggle("is-active", Boolean(activeId) && button.dataset.id === activeId);
  });
}

async function selectPaper(id) {
  if (!id) return;
  const seq = ++state.selectionSeq;
  state.selectedId = id;
  stopPolling();
  state.job = null;
  renderJobStatus();
  state.selected = state.papers.find((paper) => paper.id === id) || null;
  renderSelection();
  renderSteps();
  markActiveResult();
  try {
    const detail = await api(`/api/rebuild/papers/${id}`);
    if (seq !== state.selectionSeq) return;
    state.selected = detail;
    const index = state.papers.findIndex((paper) => paper.id === id);
    if (index >= 0) state.papers[index] = detail; else state.papers.unshift(detail);
    renderResults();
    renderSelection();
    renderSteps();
    renderJobArea();
    checkExistingJob(id);
  } catch (error) {
    if (seq === state.selectionSeq) renderStatus(`读取论文失败：${error.message}`, "error");
  }
}

function renderSelection() {
  const host = $("selectedPaper");
  const paper = state.selected;
  if (!paper) {
    host.className = "rebuild-empty";
    host.textContent = "请选择一篇论文";
    renderJobArea();
    return;
  }
  host.className = "rebuild-selected";
  host.innerHTML = `
    <div class="rebuild-selected-code">${esc(paperLabel(paper))}</div>
    <p class="rebuild-selected-title">${esc(paper.title || "无标题")}</p>
    <p class="rebuild-selected-meta">${esc(paper.year || "—")} · ${esc(paper.journal || "—")}</p>
    <p class="rebuild-selected-meta">源 PDF：${paper.has_pdf ? "已有" : "缺失"}</p>
    <div class="rebuild-counts">
      <span class="rebuild-count"><strong>${count(paper, "files")}</strong> 关联文件</span>
      <span class="rebuild-count"><strong>${count(paper, "assets")}</strong> 图表对象</span>
      <span class="rebuild-count"><strong>${count(paper, "rows")}</strong> 数据行</span>
    </div>
    ${paper.has_pdf ? `<div class="rebuild-step-actions"><a class="rebuild-btn ghost" href="/api/rebuild/papers/${esc(paper.id)}/source-pdf/preview" target="_blank" rel="noopener">预览源 PDF</a></div>` : ""}
  `;
}

function setStepStatus(id, text, ok) {
  const badge = $(id);
  badge.textContent = text;
  badge.classList.toggle("is-ok", Boolean(ok));
  badge.classList.toggle("is-pending", !ok);
}

function renderSteps() {
  const paper = state.selected;
  const files = count(paper, "files");
  const assets = count(paper, "assets");
  const rows = count(paper, "rows");
  document.querySelectorAll(".rebuild-step").forEach((step) => {
    step.classList.toggle("is-disabled", !paper);
  });
  if (!paper) {
    setStepStatus("step1Status", "先选择论文", false);
    setStepStatus("step2Status", "先选择论文", false);
    setStepStatus("step3Status", "先选择论文", false);
    setStepStatus("step4Status", "先选择论文", false);
  } else {
    setStepStatus("step1Status", files === 0 ? "未关联文件" : `已关联 ${files} 个文件`, files > 0);
    setStepStatus("step2Status", assets === 0 ? "暂无图表对象" : `已有 ${assets} 个图表对象`, assets > 0);
    setStepStatus("step3Status", rows === 0 ? "暂无数据行" : `已有 ${rows} 行数据`, rows > 0);
    setStepStatus("step4Status", "可开始汇总分析", true);
  }
  applyLinks(paper);
}

function applyLinks(paper) {
  const mapping = { figureLink: "figure", dataLink: "data", analysisLink: "analysis" };
  Object.keys(mapping).forEach((id) => {
    const link = $(id);
    if (paper) {
      link.href = `${STEP_LINKS[mapping[id]]}?paper_id=${encodeURIComponent(paper.id)}`;
      link.setAttribute("aria-disabled", "false");
    } else {
      link.href = STEP_LINKS[mapping[id]];
      link.setAttribute("aria-disabled", "true");
    }
  });
  $("copyPackage").setAttribute("aria-disabled", paper ? "false" : "true");
  $("uploadToggle").disabled = !paper;
  if (!paper && !$("uploadForm").hidden) toggleUpload(false);
}

function toggleUpload(force) {
  const panel = $("uploadForm");
  const toggle = $("uploadToggle");
  const open = force === undefined ? panel.hidden : Boolean(force);
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.textContent = open ? "收起上传表单" : "上传文件";
}

async function copyWorkPackage() {
  if (!state.selected) { renderStatus("请先选择论文", "error"); return; }
  const url = `${location.origin}/api/rebuild/papers/${state.selected.id}/ai-work-package`;
  try {
    await navigator.clipboard.writeText(url);
    renderStatus("AI 工作包地址已复制到剪贴板。");
  } catch (_) {
    renderStatus(`复制失败，请手动复制：${url}`, "error");
  }
}

async function uploadFile(event) {
  event.preventDefault();
  if (!state.selected) { renderStatus("请先选择论文", "error"); return; }
  const file = $("uploadFile").files[0];
  if (!file) { renderStatus("请选择 PDF 文件", "error"); return; }
  const formData = new FormData();
  formData.append("role", $("uploadRole").value);
  formData.append("file", file);
  const button = $("uploadBtn");
  button.disabled = true;
  renderStatus("正在保存文件关联…", "progress");
  try {
    const result = await api(`/api/rebuild/papers/${state.selected.id}/files`, { method: "POST", body: formData });
    renderStatus(result.created ? "文件关联已保存，未启动自动解析。" : "相同文件已存在，关联保持幂等。");
    $("uploadFile").value = "";
    await selectPaper(state.selected.id);
  } catch (error) {
    renderStatus(`保存失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}


// ---------------------------------------------------------------------------
//  One-click AI extract
// ---------------------------------------------------------------------------

const JOB_POLL_INTERVAL = 5000;

function jobStatusLabel(status) {
  const map = {
    not_started: "未开始",
    queued: "排队中",
    dispatching: "正在派发任务…",
    running: "运行中",
    completed: "执行结束",
    failed: "提取失败",
  };
  return map[status] || status;
}

function jobPhaseMessage(phase, status) {
  if (phase === "needs_check") return "任务状态待核实，请查看当前任务；不会重复派发。";
  if (status === "completed") return "执行结束";
  if (status === "failed") return null;
  if (phase === "dispatching" || status === "dispatching") return "AI 正在准备…";
  if (phase === "running" || status === "running") return "AI 正在读取论文…";
  if (phase === "dispatch_failed" || phase === "dispatch_error") return "派发失败";
  return null;
}

function renderJobArea() {
  const btn = $("aiExtractBtn");
  const placeholder = $("aiPlaceholder");
  const paper = state.selected;
  const selector = $("aiExtractModel");
  const pending = state.job && !["not_started", "completed"].includes(state.job.status);
  if (selector) {
    selector.disabled = !paper || Boolean(pending);
    if (pending && ["gpt-6-luna", "cn:deepseek-v4.1-flash"].includes(state.job.model)) selector.value = state.job.model;
  }

  if (!paper) {
    btn.disabled = true;
    btn.textContent = "让 AI 提取这篇文献";
    placeholder.hidden = false;
    placeholder.textContent = "请在左侧选择一篇文献，然后点击下方按钮";
    $("aiExtractStatus").hidden = true;
    $("aiExtractResult").hidden = true;
    return;
  }

  placeholder.hidden = Boolean(selector) || Boolean(pending);
  if (!selector && !pending) placeholder.textContent = "页面已更新，请刷新后选择提取模型与模式。";
  const job = state.job;
  if (!job || job.status === "not_started") {
    btn.disabled = false;
    btn.textContent = "让 AI 提取这篇文献";
  } else if (job.status === "completed") {
    btn.disabled = false;
    btn.textContent = "重新提取";
  } else {
    btn.disabled = false;
    btn.textContent = "查看当前任务";
  }
}

function renderJobStatus() {
  const statusArea = $("aiExtractStatus");
  const resultArea = $("aiExtractResult");
  const job = state.job;

  if (!job || job.status === "not_started") {
    statusArea.hidden = true;
    resultArea.hidden = true;
    return;
  }

  statusArea.hidden = false;
  const label = jobStatusLabel(job.status);
  const phaseMsg = jobPhaseMessage(job.phase, job.status);
  const modeLabel = job.dispatch_mode === "codex_web_ordinary" && job.model === "gpt-6-luna" ? "Luna 普通任务" : job.dispatch_mode === "codex_web_target" && job.model === "cn:deepseek-v4.1-flash" ? "DeepSeek 目标任务" : "模式待核实";

  if (job.status === "failed") {
    statusArea.innerHTML = `<div class="ai-status-failed">提取失败：${esc(job.error || "未知原因")} · ${esc(modeLabel)}；请查看当前任务核实，不会自动重建。</div>`;
    resultArea.hidden = true;
  } else if (job.status === "completed") {
    statusArea.innerHTML = `<div class="ai-status-done">${esc(label)} · ${esc(modeLabel)}</div>`;
    const r = job.result || {};
    const reading = r.reading_coverage;
    const coverageText = reading ? `图表解读已存 ${reading.structured_readings} / ${reading.current_assets}；${reading.status === 'incomplete' ? '内容尚不完整' : '内容待独立审核'}` : '图表解读覆盖尚未核验';
    statusArea.innerHTML = `<div class="ai-status-done">执行结束 · ${esc(modeLabel)} · ${esc(coverageText)}；科学验收待核实。</div>`;
    resultArea.hidden = false;
    resultArea.innerHTML = `
      <div class="ai-result-summary">
        <span class="ai-result-count"><strong>${count(r, "assets")}</strong> 图表对象</span>
        <span class="ai-result-count"><strong>${count(r, "rows")}</strong> 数据行</span>
        <span class="ai-result-count"><strong>${count(r, "values")}</strong> 数据值</span>
        <span class="ai-result-count"><strong>${count(r, "sources")}</strong> 来源</span>
      </div>
      <div class="ai-result-links">
        <a class="rebuild-btn" href="../figure_assets/index.html?paper_id=${encodeURIComponent(state.selected ? state.selected.id : "")}">查看图表资料</a>
        <a class="rebuild-btn ghost" href="../data_table/index.html?paper_id=${encodeURIComponent(state.selected ? state.selected.id : "")}">查看数据表</a>
      </div>
    `;
  } else if (phaseMsg) {
    statusArea.innerHTML = `<div class="ai-status-running">${esc(phaseMsg)} · ${esc(modeLabel)}</div>`;
    resultArea.hidden = true;
  } else {
    statusArea.innerHTML = `<div class="ai-status-running">${esc(label)}</div>`;
    resultArea.hidden = true;
  }
}

async function startAiExtract() {
  const paper = state.selected;
  if (!paper) { renderStatus("请先选择论文", "error"); return; }
  if (state.job && ["running", "queued", "dispatching", "failed"].includes(state.job.status)) {
    await pollJobStatus(paper.id);
    return;
  }

  const selector = $("aiExtractModel");
  if (!selector) {
    renderStatus("页面已更新，请刷新后选择提取模型与模式，再启动提取。", "error");
    return;
  }
  const btn = $("aiExtractBtn");
  btn.disabled = true;
  btn.textContent = "正在派发…";
  const model = selector.value;
  if (!["gpt-6-luna", "cn:deepseek-v4.1-flash"].includes(model)) {
    renderStatus("请选择提取模型与模式", "error");
    renderJobArea();
    return;
  }
  selector.disabled = true;

  try {
    const job = await api(`/api/rebuild/papers/${paper.id}/ai-extract/jobs`, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({model}) });
    if (state.selectedId !== paper.id) return;
    state.job = job;
    renderJobStatus();
    renderJobArea();
    if (job.status === "running" || job.status === "queued" || job.status === "dispatching") {
      startPolling(paper.id);
    }
  } catch (error) {
    if (state.selectedId !== paper.id) return;
    state.job = {status:"dispatching",phase:"needs_check",needs_check:true,model,
      dispatch_mode:model === "gpt-6-luna" ? "codex_web_ordinary" : "codex_web_target"};
    renderStatus(`派发响应未确认：${error.message}。请先查看当前任务。`, "error");
    renderJobStatus();
    renderJobArea();
  }
}

function startPolling(paperId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(() => pollJobStatus(paperId), JOB_POLL_INTERVAL);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function pollJobStatus(paperId) {
  try {
    const job = await api(`/api/rebuild/papers/${paperId}/ai-extract/jobs/latest`);
    if (state.selectedId !== paperId) return;
    state.job = job;
    renderJobStatus();
    renderJobArea();
    if (job.status === "completed" || job.status === "failed") {
      stopPolling();
      await selectPaper(paperId);
    }
  } catch (error) {
    // Network error — keep polling, don't fail the job
  }
}

async function checkExistingJob(paperId) {
  try {
    const job = await api(`/api/rebuild/papers/${paperId}/ai-extract/jobs/latest`);
    if (state.selectedId !== paperId) return;
    state.job = job;
    renderJobStatus();
    renderJobArea();
    if (job.status === "running" || job.status === "queued" || job.status === "dispatching") {
      startPolling(paperId);
    } else {
      stopPolling();
    }
  } catch (error) {
    if (state.selectedId !== paperId) return;
    state.job = {status:"dispatching",phase:"needs_check",needs_check:true};
    renderJobStatus();
    renderJobArea();
  }
}


document.addEventListener("DOMContentLoaded", () => {
  TopNav.init({ currentPage: "ai-extract", mountId: "topnav-mount" });
  $("searchBtn").addEventListener("click", () => { state.query = $("searchInput").value.trim(); loadPapers(); });
  $("searchInput").addEventListener("keydown", (event) => { if (event.key === "Enter") $("searchBtn").click(); });
  $("refreshBtn").addEventListener("click", loadPapers);
  $("uploadToggle").addEventListener("click", () => toggleUpload());
  $("copyPackage").addEventListener("click", copyWorkPackage);
  $("uploadForm").addEventListener("submit", uploadFile);
  $("aiExtractBtn").addEventListener("click", startAiExtract);
  renderSteps();
  renderJobArea();
  loadPapers();
});
