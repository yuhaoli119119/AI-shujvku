const PAPERS_API = "/api/papers";
const WRITING_PLAN_API = "/api/content-knowledge/writing-plan";

const state = {
  papers: [],
  selectedIds: [],
  plan: null,
};

const coverageLabels = {
  represented: "已有安全证据",
  no_safe_evidence: "没有通过安全门的证据",
  not_relevant: "与当前问题不相关",
  budget_exhausted: "因预算未入选",
  not_found: "论文不存在",
};

function byId(id) {
  return document.getElementById(id);
}

function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
}

function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.style.display = "block";
  window.setTimeout(() => {
    toast.style.display = "none";
  }, 2800);
}

async function fetchJSON(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  const response = await fetch(url, { ...options, headers });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_) {
    data = null;
  }
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || `HTTP ${response.status}`));
  }
  return data;
}

function clampInteger(id, fallback, min, max) {
  const element = byId(id);
  const value = Number.parseInt(element.value, 10);
  const bounded = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
  element.value = String(bounded);
  return bounded;
}

function buildRequestPayload() {
  const mode = byId("writingMode").value || "narrative";
  const includeDft = byId("includeDft").checked || mode === "dft_quantitative";
  return {
    query: byId("writingTopic").value.trim(),
    paper_ids: [...state.selectedIds],
    mode,
    requested_sections: includeDft ? ["dft_results"] : [],
    evidence_budget: clampInteger("evidenceBudget", 24, 1, 48),
    batch_size: clampInteger("batchSize", 10, 1, 10),
    max_evidence_per_paper: clampInteger("maxPerPaper", 3, 1, 8),
    max_sources_per_claim: clampInteger("maxSources", 5, 3, 5),
    candidate_pool_per_type: 24,
  };
}

function updateDftFormState() {
  const enabled = byId("includeDft").checked || byId("writingMode").value === "dft_quantitative";
  const badge = byId("dftFormState");
  badge.textContent = enabled ? "DFT 已明确启用" : "DFT 未启用";
  badge.className = `status ${enabled ? "warn" : "neutral"}`;
}

async function loadPapers() {
  try {
    const response = await fetchJSON(`${PAPERS_API}?limit=200`);
    state.papers = Array.isArray(response) ? response : (response?.items || []);
    renderPapers();
  } catch (error) {
    byId("paperChecklist").innerHTML = '<div class="empty-state">文献加载失败。</div>';
    showToast(`加载文献失败：${error.message}`);
  }
}

function renderPapers() {
  const keyword = byId("paperSearch").value.trim().toLowerCase();
  const visible = state.papers.filter((paper) => {
    const haystack = `${paper.paper_code || ""} ${paper.title || ""}`.toLowerCase();
    return !keyword || haystack.includes(keyword);
  });
  if (!visible.length) {
    byId("paperChecklist").innerHTML = '<div class="empty-state">没有匹配的论文。</div>';
    return;
  }
  byId("paperChecklist").innerHTML = visible.map((paper) => {
    const id = String(paper.id);
    const selected = state.selectedIds.includes(id);
    return `
      <label class="paper-item ${selected ? "selected" : ""}">
        <input type="checkbox" data-paper-id="${esc(id)}" ${selected ? "checked" : ""}>
        <span>
          <span class="paper-title">${esc(paper.title || "未命名文献")}</span>
          <span class="paper-meta">${esc(paper.paper_code || id)}</span>
        </span>
      </label>
    `;
  }).join("");
}

function togglePaper(id, checked) {
  const existing = state.selectedIds.indexOf(id);
  if (checked && existing < 0) state.selectedIds.push(id);
  if (!checked && existing >= 0) state.selectedIds.splice(existing, 1);
  byId("selectedCount").textContent = `${state.selectedIds.length} 篇`;
  renderPapers();
}

async function buildPlan() {
  const payload = buildRequestPayload();
  if (!payload.query) {
    showToast("请先输入写作主题或研究问题。");
    return;
  }
  if (!payload.paper_ids.length) {
    showToast("请至少选择一篇论文。");
    return;
  }
  const buttons = [byId("buildPlanBtn"), byId("buildPlanInlineBtn")];
  buttons.forEach((button) => {
    button.disabled = true;
    button.textContent = "正在规划…";
  });
  try {
    state.plan = await fetchJSON(WRITING_PLAN_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderPlan();
    showToast("安全证据计划已生成。网页未调用写作模型。");
  } catch (error) {
    showToast(`计划生成失败：${error.message}`);
  } finally {
    buttons.forEach((button) => {
      button.disabled = false;
      button.textContent = "生成安全证据计划";
    });
  }
}

function renderPlan() {
  const plan = state.plan || {};
  const scope = plan.paper_scope || {};
  const coverage = plan.coverage || {};
  const budgets = plan.budgets || {};
  const batches = plan.batches || [];
  const evidence = plan.selected_evidence || [];
  byId("planEmpty").hidden = true;
  byId("planResults").hidden = false;
  byId("copyAllBtn").disabled = false;
  byId("metricRequested").textContent = plan.requested_paper_count ?? scope.requested_paper_count ?? state.selectedIds.length;
  byId("metricValid").textContent = plan.valid_paper_count ?? scope.valid_paper_count ?? 0;
  byId("metricRepresented").textContent = plan.represented_paper_count ?? 0;
  byId("metricBatches").textContent = batches.length;
  byId("metricBudget").textContent = `${budgets.used || 0} / ${budgets.evidence_budget || 24}`;
  byId("metricRemaining").textContent = budgets.remaining ?? 0;
  byId("intentSummary").textContent =
    `模式 ${plan.retrieval_mode || "narrative"}；证据类型 ${(plan.selected_evidence_types || []).join("、") || "无"}`;
  const coverageComplete = Boolean(coverage.coverage_complete);
  const coverageBadge = byId("coverageBadge");
  coverageBadge.textContent = coverageComplete ? "覆盖完成" : "覆盖不完整";
  coverageBadge.className = `status ${coverageComplete ? "good" : "warn"}`;
  byId("dftStatus").innerHTML = plan.dft_included
    ? `<strong>DFT 已启用：</strong>${esc(plan.dft_included_reason || "明确 DFT 意图")}`
    : `<strong>DFT 未启用 / 未检索：</strong>${esc(plan.dft_included_reason || "普通写作安全默认")}`;
  renderWarnings(plan, coverageComplete);
  renderCoverage(coverage.by_paper || []);
  renderBatches(batches, plan.batch_prompt_contexts || []);
  renderEvidence(evidence);
}

function renderWarnings(plan, coverageComplete) {
  const warnings = [...(plan.warnings || [])].map((warning) => (
    typeof warning === "string" ? warning : (warning.message || warning.code || JSON.stringify(warning))
  ));
  if (!coverageComplete) {
    warnings.unshift("覆盖不完整：不得声称系统性、全面或穷尽性覆盖；未支持观点不得自动补写。");
  }
  byId("warnings").innerHTML = warnings.length
    ? warnings.map((warning) => `<div class="warning-item">${esc(warning)}</div>`).join("")
    : '<div class="muted">当前没有额外警告。</div>';
}

function renderCoverage(rows) {
  byId("coverageList").innerHTML = rows.length
    ? rows.map((row) => `
        <div class="coverage-row">
          <span>${esc(row.paper_code || row.paper_id || "未知论文")}</span>
          <strong>${esc(coverageLabels[row.status] || row.status || "状态未知")}</strong>
        </div>
      `).join("")
    : '<div class="empty-state">没有逐篇覆盖记录。</div>';
}

function renderBatches(batches, contexts) {
  const contextById = new Map(contexts.map((context) => [String(context.batch_id), context]));
  byId("batchList").innerHTML = batches.length
    ? batches.map((batch) => {
        const context = contextById.get(String(batch.batch_id));
        return `
          <article class="batch-card">
            <div class="batch-head">
              <div>
                <h3>${esc(batch.batch_id)} · ${Number(batch.paper_ids?.length || 0)} 篇论文</h3>
                <div class="muted">${Number(batch.selected_evidence_ids?.length || 0)} 条证据 · 预算 ${esc(batch.budget?.used ?? batch.budget ?? "—")}</div>
              </div>
              <button class="btn ghost small copy-batch-btn" type="button"
                data-batch-id="${esc(batch.batch_id)}" ${context ? "" : "disabled"}>复制单批上下文</button>
            </div>
            <div class="tag-row">${(batch.paper_codes || []).map((code) => `<span class="tag">${esc(code)}</span>`).join("")}</div>
            <div class="muted">证据 ID：${esc((batch.selected_evidence_ids || []).join("、") || "本批无安全证据")}</div>
          </article>
        `;
      }).join("")
    : '<div class="empty-state">没有可执行批次。</div>';
}

function renderEvidence(evidence) {
  byId("evidenceCount").textContent = `${evidence.length} 条`;
  if (!evidence.length) {
    byId("evidenceList").innerHTML =
      '<div class="empty-state">安全证据为 0。请让本地 AI 标注“无证据支持”，不要补写事实或数字。</div>';
    return;
  }
  byId("evidenceList").innerHTML = evidence.map((item) => `
    <article class="evidence-card">
      <h3>[${esc(item.paper_code || item.source_paper_id)} / ${esc(item.object_id)} / ${esc(item.page_start || item.evidence_locator?.page || item.evidence_locator?.label || "页码未知")}]</h3>
      <div class="tag-row">
        <span class="tag">${esc(item.evidence_type)}</span>
        <span class="tag">${item.can_use_for_citation === true ? "可引用" : "仅用于写作，不可直接引用"}</span>
        <span class="tag">gate: ${esc(item.gate_status || "安全门已通过")}</span>
        ${item.doi ? `<span class="tag">DOI: ${esc(item.doi)}</span>` : ""}
        ${item.property ? `<span class="tag">${esc(item.property)} ${esc(item.value ?? "")} ${esc(item.unit || "")}</span>` : ""}
      </div>
      <div class="muted">evidence_id ${esc(item.evidence_id)} · locator ${esc(formatLocator(item.evidence_locator))} · review ${esc(item.review_status || "—")}</div>
      <p class="excerpt">${esc(item.excerpt || "")}</p>
      ${item.context ? `<div class="muted">数值上下文：${esc(item.context)}</div>` : ""}
    </article>
  `).join("");
}

function formatLocator(locator) {
  if (!locator) return "—";
  if (typeof locator === "string") return locator;
  return locator.label || locator.quote || locator.section_title || locator.page || JSON.stringify(locator);
}

function buildAllExecutionText(plan) {
  const summary = {
    schema_version: "local_ai_writing_handoff.v1",
    plan_fingerprint: plan.plan_fingerprint,
    query: plan.query,
    retrieval_mode: plan.retrieval_mode,
    selected_evidence_types: plan.selected_evidence_types,
    dft_included: plan.dft_included,
    dft_included_reason: plan.dft_included_reason,
    paper_counts: {
      requested: plan.requested_paper_count,
      valid: plan.valid_paper_count,
      represented: plan.represented_paper_count,
    },
    budgets: plan.budgets,
    coverage: plan.coverage,
    warnings: plan.warnings,
    batches: (plan.batches || []).map((batch) => ({
      batch_id: batch.batch_id,
      paper_ids: batch.paper_ids,
      paper_codes: batch.paper_codes,
      selected_evidence_ids: batch.selected_evidence_ids,
      budget: batch.budget,
    })),
    evidence_full_text_included: false,
    writes_db: false,
  };
  return [
    "本地 AI 分批写作执行说明",
    "1. 一次只处理一个 batch_prompt_context，不读取或引入其他批次证据。",
    "2. 每个事实保留 [paper_code/object_id/page或locator] 来源标记。",
    "3. 只有 can_use_for_citation=true 的证据卡可进入正式引用；writing=true/citation=false 只可辅助组织文字。",
    "4. 数字必须与 property、unit、context 和原论文对象绑定，不跨论文拼接。",
    "5. 无证据不得补写；覆盖不完整时不得声称系统性、全面或穷尽性覆盖。",
    "6. 先输出每批事实/论点摘要及 claim-evidence 映射；总综合只能使用已完成批次摘要与映射。",
    "7. 请从网页分别复制每个单批上下文；本执行说明不包含论文全文或证据摘录。",
    "",
    JSON.stringify(summary, null, 2),
  ].join("\n");
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMessage);
  } catch (_) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
    showToast(successMessage);
  }
}

function copyBatch(batchId) {
  const context = (state.plan?.batch_prompt_contexts || []).find(
    (item) => String(item.batch_id) === String(batchId),
  );
  if (!context) {
    showToast("该批次没有可复制的有界上下文。");
    return;
  }
  const instructions = [
    "仅处理下面这个批次；禁止使用其他批次或整篇 PDF。",
    "事实标记格式：[paper_code/object_id/page或locator]。",
    "只有 can_use_for_citation=true 可正式引用；writing=true/citation=false 仅可辅助组织文字。",
    "无证据不补写；数字保持 property/unit/context 绑定。",
    "",
    JSON.stringify(context, null, 2),
  ].join("\n");
  copyText(instructions, `已复制 ${batchId} 的单批上下文。`);
}

function bindEvents() {
  byId("paperChecklist").addEventListener("change", (event) => {
    const checkbox = event.target.closest("input[data-paper-id]");
    if (checkbox) togglePaper(checkbox.dataset.paperId, checkbox.checked);
  });
  byId("paperSearch").addEventListener("input", renderPapers);
  byId("clearSelectionBtn").addEventListener("click", () => {
    state.selectedIds = [];
    byId("selectedCount").textContent = "0 篇";
    renderPapers();
  });
  byId("writingMode").addEventListener("change", updateDftFormState);
  byId("includeDft").addEventListener("change", updateDftFormState);
  byId("buildPlanBtn").addEventListener("click", buildPlan);
  byId("buildPlanInlineBtn").addEventListener("click", buildPlan);
  byId("copyAllBtn").addEventListener("click", () => {
    if (state.plan) copyText(buildAllExecutionText(state.plan), "全部分批执行说明已复制。");
  });
  byId("batchList").addEventListener("click", (event) => {
    const button = event.target.closest(".copy-batch-btn");
    if (button) copyBatch(button.dataset.batchId);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  TopNav.init({ currentPage: "ai-writer", mountId: "topnav-mount" });
  bindEvents();
  updateDftFormState();
  loadPapers();
});

export {
  buildAllExecutionText,
  buildRequestPayload,
};
