"use strict";

const state = {
  templates: null,
  rows: [],
  total: 0,
  page: 1,
  pageSize: 50,
  rowsLoading: false,
  rowsGeneration: 0,
  paperId: new URLSearchParams(location.search).get("paper_id") || "",
};

function $(id) { return document.getElementById(id); }

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function hasConflict(value) {
  return ["value_conflict", "unit_conflict"].includes(value.conflict?.status) ||
    ["value_conflict", "unit_conflict"].includes(value.conflict?.legacy_unresolved);
}

function status(message, kind) {
  const area = $("statusArea");
  if (!message) { area.innerHTML = ""; return; }
  const cls = kind === "error" ? "rebuild-error" : kind === "progress" ? "rebuild-progress" : "rebuild-empty";
  area.innerHTML = `<div class="${cls}" role="status">${escapeHtml(message)}</div>`;
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options));
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { const payload = await response.json(); detail = typeof payload.detail === "string" ? payload.detail : Array.isArray(payload.detail) ? payload.detail.map(item => item.msg || "提交格式不正确").join("；") : detail; } catch (_) {}
    const error = new Error(String(detail)); error.httpStatus = response.status; throw error;
  }
  return response.json();
}

/* 未指定反应（「全部反应」）时只显示公共字段，具体反应由单独的「反应」列区分；
   选中某个反应后才追加该反应的专属字段，避免把所有反应字段合并成一张过宽的表。 */
function currentFields() {
  const reaction = $("reactionFilter").value;
  const common = state.templates.common_fields || [];
  if (!reaction) return common.slice();
  const reactionFields = ((state.templates.reactions || {})[reaction] || {}).fields || [];
  return common.concat(reactionFields);
}

function showReactionColumn() {
  return !$("reactionFilter").value;
}

function reactionLabel(row) {
  const item = (state.templates.reactions || {})[row.reaction] || null;
  return (item && item.label) || row.reaction || "—";
}

function updateReactionHint() {
  const hint = $("reactionHint");
  if (!hint) return;
  if (!$("reactionFilter").value) {
    hint.textContent = "全部反应：只显示公共字段，具体反应见「反应」列；选中某个反应后可看它的专属字段。列外已保存的值可展开「补充数据」查看及回看来源。";
  } else {
    hint.textContent = "已选定反应：显示该反应专属字段 + 公共字段。列外已保存的值可展开「补充数据」查看及回看来源。";
  }
}

async function loadTemplates() {
  state.templates = await api("/api/rebuild/templates");
  $("reactionFilter").innerHTML = `<option value="">全部反应</option>` + Object.entries(state.templates.reactions).map(([value, item]) => `<option value="${escapeHtml(value)}">${escapeHtml(item.label)}</option>`).join("");
  $("activeSiteFilter").innerHTML = `<option value="">全部</option>` + state.templates.active_site_types.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
}

/* 带 paper_id 打开时，先问后端这篇论文实际有哪些反应，再决定默认筛选：
   只有一个已知反应就切到它，否则保持“全部反应”，不沿用页面默认值把它自己的行藏掉。 */
async function resolvePaperReaction() {
  const params = new URLSearchParams({ paper_id: state.paperId, limit: "500", offset: "0" });
  const data = await api(`/api/rebuild/rows?${params}`);
  const reactions = (data.items || []).map((row) => row.reaction).filter(Boolean);
  return [...new Set(reactions)];
}

function setPagerLoading() {
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  if ($("prevPage")) $("prevPage").disabled = state.rowsLoading || state.page <= 1;
  if ($("nextPage")) $("nextPage").disabled = state.rowsLoading || state.page >= pageCount;
}

async function loadRows(requestedPage) {
  const generation = ++state.rowsGeneration;
  const page = Number.isInteger(requestedPage) ? Math.max(1, requestedPage) : state.page;
  state.rowsLoading = true;
  setPagerLoading();
  status("正在读取数据行…", "progress");
  const params = new URLSearchParams({
    reaction: $("reactionFilter").value,
    material: $("materialFilter").value.trim(),
    active_site_type: $("activeSiteFilter").value,
    data_type: $("dataTypeFilter").value,
    limit: String(state.pageSize),
    offset: String((page - 1) * state.pageSize),
  });
  if (state.paperId) params.set("paper_id", state.paperId);
  try {
    const data = await api(`/api/rebuild/rows?${params}`);
    if (generation !== state.rowsGeneration) return false;
    const pageCount = Math.max(1, Math.ceil(data.total / state.pageSize));
    if (page > pageCount) return await loadRows(pageCount);
    state.page = page;
    state.rows = data.items;
    state.total = data.total;
    renderRows();
    status("");
    return true;
  } catch (error) {
    if (generation !== state.rowsGeneration) return false;
    status(`读取失败：${error.message}。请点击刷新重试。`, "error");
    return false;
  } finally {
    if (generation === state.rowsGeneration) {
      state.rowsLoading = false;
      setPagerLoading();
    }
  }
}

function valueFor(row, field) {
  return row.values.find((value) => value.field_name === field.name) || null;
}

function savedValueText(value) {
  if (!value || value.value_type === "missing") return "空";
  const raw = value.raw_value === "" || value.raw_value == null ? value.numeric_value ?? "" : value.raw_value;
  return raw === "" ? "空" : String(raw);
}

function valueNotes(row, value) {
  return (value?.is_estimated ? "（估读）" : "") +
    (row.identity_status === "pending" ? "（身份待定）" : "") +
    (value && hasConflict(value) ? "（存在冲突 ⚠️）" : "");
}

function renderValue(row, field) {
  const value = valueFor(row, field);
  const title = !value ? "未录入" : value.value_type === "missing" ? value.missing_reason || "缺失" : "查看来源";
  return `<button class="rebuild-value-button" data-row="${escapeHtml(row.id)}" data-field="${escapeHtml(field.name)}" type="button" title="${escapeHtml(title)}">${escapeHtml(savedValueText(value) + valueNotes(row, value))}</button>`;
}

/* 只检查当前页已返回的值；列外字段逐项展示自己的单位，不合并成反应宽表。 */
function renderSupplementalData(row, fields) {
  const visibleNames = new Set(fields.map(field => field.name));
  const values = row.values.filter(value => !visibleNames.has(value.field_name));
  if (!values.length) return "—";
  return `<details class="rebuild-supplemental-data" style="min-width:180px;max-width:280px;white-space:normal;overflow-wrap:anywhere"><summary style="cursor:pointer">${values.length} 项补充数据</summary>${values.map(value => `
    <div class="rebuild-supplemental-item" style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border,#e2e8f0)">
      <code>${escapeHtml(value.field_name)}</code>
      <div>${escapeHtml(savedValueText(value))} <span>${escapeHtml(value.unit ?? "—")}</span>${escapeHtml(valueNotes(row, value))}</div>
      ${value.value_type === "missing" ? `<div>${escapeHtml(value.missing_reason || "缺失")}</div>` : ""}
      <button class="rebuild-value-button" data-row="${escapeHtml(row.id)}" data-field="${escapeHtml(value.field_name)}" type="button">来源（${Array.isArray(value.sources) ? value.sources.length : 0}）</button>
    </div>`).join("")}</details>`;
}

function renderRows() {
  const fields = currentFields();
  const showReaction = showReactionColumn();
  const fixedColumns = showReaction ? 7 : 6;
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("rowSummary").textContent = `${state.total} 行 · 第 ${state.page}/${pageCount} 页${showReaction ? " · 全部反应（仅公共字段）" : ""}`;
  const wrap = $("tableHead").closest('.rebuild-table-wrap');
  let grid=document.getElementById('sharedCurrentGrid');
  if(!grid) {grid=document.createElement('div');grid.id='sharedCurrentGrid';wrap.parentNode.insertBefore(grid,wrap);wrap.hidden=true;}
  EditableDataGrid.mount(grid,{rows:state.rows,templates:state.templates,reload:()=>loadRows(),onIdentity:row=>openIdentityEditor(row.id)});
  $("pager").innerHTML = `
    <button class="rebuild-btn ghost" id="prevPage" ${state.rowsLoading || state.page <= 1 ? "disabled" : ""} type="button">上一页</button>
    <button class="rebuild-btn ghost" id="nextPage" ${state.rowsLoading || state.page >= pageCount ? "disabled" : ""} type="button">下一页</button>
  `;
  $("prevPage").addEventListener("click", () => { if (!state.rowsLoading && state.page > 1) loadRows(state.page - 1); });
  $("nextPage").addEventListener("click", () => { if (!state.rowsLoading && state.page < pageCount) loadRows(state.page + 1); });
  updateReactionHint();
}

function validSourcePage(value) {
  if (typeof value !== "number" && !(typeof value === "string" && /^\d+$/.test(value))) return null;
  const page = Number(value);
  return Number.isSafeInteger(page) && page > 0 ? page : null;
}

/* 明确文件 ID 优先；文件标注无效时不把来源悄悄改成正文。 */
function sourcePdfLink(row, source) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const hasFileId = source.file_id != null && source.file_id !== "";
  if (hasFileId && !(typeof source.file_id === "string" && uuid.test(source.file_id))) {
    return '<span class="rebuild-error">来源文件不可用：文件标注无效</span>';
  }
  if (source.page_basis === "ooxml" && source.ooxml_locator && hasFileId) {
    return `<span>OOXML 定位（DOCX 原件，无物理页）：<code>${escapeHtml(source.ooxml_locator)}</code></span> <a href="/api/rebuild/files/${encodeURIComponent(source.file_id)}/download" download>下载 DOCX 原件</a>`;
  }
  const page = validSourcePage(source.page_number);
  const path = hasFileId ? `/api/rebuild/files/${source.file_id}/preview` :
    `/api/rebuild/papers/${encodeURIComponent(row.paper_id)}/source-pdf/preview`;
  const locator = page === null ? "" : `#page=${page}`;
  const label = (hasFileId ? "查看来源 PDF" : "查看正文 PDF") +
    (page === null ? "（页码未标注）" : ` 第 ${page} 页`) +
    (hasFileId ? "" : "（来源文件未标注）");
  return `<a class="rebuild-source-pdf-link" href="${escapeHtml(path + locator)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
}

function showSource(rowId, fieldName) {
  const row = state.rows.find((item) => item.id === rowId);
  const value = row && row.values.find((item) => item.field_name === fieldName);
  if (!row || !value) return;
  $("sourceTitle").textContent = `${row.material} · ${fieldName}`;
  const sources = Array.isArray(value.sources) ? value.sources : [];
  const sourceItems = sources.map((source) => `
    <div class="rebuild-panel" style="box-shadow:none">
      ${value.preferred_source_id && source.id === value.preferred_source_id ? '<span class="rebuild-tag">当前首选来源</span>' : ""}
      <div>来源 ID：${escapeHtml(source.id)}</div>
      <div><strong>${escapeHtml(source.label || source.source_kind)}</strong> · ${source.page_basis === "ooxml" ? "OOXML 定位（DOCX 原件，无物理页）" : validSourcePage(source.page_number) === null ? "页码未标注" : `第 ${validSourcePage(source.page_number)} 页`}</div>
      ${source.quote ? `<p>${escapeHtml(source.quote)}</p>` : ""}
      ${source.estimate_basis ? `<p><strong>估读依据：</strong>${escapeHtml(source.estimate_basis)}</p>` : ""}
      ${source.asset_id ? `
        <p>
          <a href="/api/rebuild/assets/${escapeHtml(encodeURIComponent(source.asset_id))}" target="_blank" rel="noopener">查看裁图对象</a>
          ·
          <a href="../figure_assets/index.html?paper_id=${escapeHtml(encodeURIComponent(row.paper_id))}" target="_blank" rel="noopener">在图表资料中查看</a>
        </p>
        <img src="/api/rebuild/assets/${escapeHtml(encodeURIComponent(source.asset_id))}" alt="${escapeHtml(source.label || "图表证据")}" loading="lazy" style="max-width:100%;max-height:220px;object-fit:contain;border:1px solid var(--border,#e2e8f0);border-radius:8px;background:#fff">
      ` : ""}
      <p>${sourcePdfLink(row, source)}</p>
      ${source.table_row != null ? `<p>表格位置：行 ${escapeHtml(source.table_row)}，列 ${escapeHtml(source.table_column ?? "?")}</p>` : ""}
    </div>
  `).join("");
  $("sourceBody").innerHTML = `
    <div class="rebuild-metrics">
      <div class="rebuild-metric"><span>原值</span><strong>${escapeHtml(savedValueText(value))}</strong></div>
      <div class="rebuild-metric"><span>单位</span><strong>${escapeHtml(value.unit ?? "—")}</strong></div>
      <div class="rebuild-metric"><span>类型</span><strong>${escapeHtml(value.is_estimated ? "估读" : value.value_type)}</strong></div>
      <div class="rebuild-metric"><span>来源数</span><strong>${sources.length}</strong></div>
    </div>
    ${value.conflict?.status === "corrected_previous_retained" ? `<div class="rebuild-panel" style="margin-top:12px">已修正；其他来源与历史候选保留用于回查。${value.conflict.correction_reason ? `<details><summary>修正说明</summary><p>${escapeHtml(value.conflict.correction_reason)}</p></details>` : ""}</div>` : ""}
    ${hasConflict(value) ? `<div class="rebuild-error" style="margin-top:12px">存在冲突：${escapeHtml(value.conflict.status || value.conflict.legacy_unresolved)}；历史值已保留。</div>` : ""}
    ${(value.conflict?.candidates || []).map(candidate => `<div class="rebuild-panel">候选：${escapeHtml(candidate.raw_value ?? "空")} ${escapeHtml(candidate.unit || "")}（${escapeHtml(candidate.value_type)}）<br>来源 ID：${escapeHtml((candidate.source_ids || []).join(", "))}</div>`).join("")}
    <h3 style="margin-top:18px">来源</h3>
    ${sourceItems || '<div class="rebuild-empty">该格为空或无来源</div>'}
  `;
  fillCorrectionForm(row, value);
  $("sourceModal").classList.add("open");
}

function fillCorrectionForm(row, value) {
  const form = $("correctionForm");
  form.dataset.rowId = row.id;
  form.dataset.fieldName = value.field_name;
  $("correctionRawValue").value = value.raw_value ?? "";
  $("correctionNumericValue").value = value.numeric_value ?? "";
  $("correctionUnit").value = value.unit ?? "";
  $("correctionValueType").value = value.value_type || "explicit";
  $("correctionPrecision").value = value.precision_digits ?? "";
  $("correctionMissingReason").value = value.missing_reason ?? "";
  const source = value.sources?.find(item => item.id === value.preferred_source_id) || value.sources?.[0] || null;
  const fileSelect = $("correctionSourceFile");
  fileSelect.innerHTML = `<option value="">来源文件未标注</option>` + (source?.file_id ? `<option value="${escapeHtml(source.file_id)}">当前来源文件</option>` : "");
  fileSelect.value = source?.file_id || "";
  api(`/api/rebuild/papers/${encodeURIComponent(row.paper_id)}/files`).then(result => {
    if (form.dataset.rowId !== row.id || form.dataset.fieldName !== value.field_name) return;
    fileSelect.innerHTML = `<option value="">来源文件未标注</option>` + result.items.map(file => `<option value="${escapeHtml(file.id)}">${file.role === "si" ? "SI" : "正文"}${file.lifecycle_status === "superseded" ? "（历史；需独立复核）" : file.preferred ? "（当前首选）" : ""} · ${escapeHtml(file.original_filename)}</option>`).join("");
    fileSelect.value = source?.file_id || "";
  }).catch(() => { $("correctionStatus").textContent = "来源文件列表读取失败，请刷新后重试。"; });
  $("correctionSourceKind").value = source?.source_kind || "";
  $("correctionPageNumber").value = source?.page_number ?? "";
  $("correctionSourceLabel").value = source?.label || "";
  $("correctionQuote").value = source?.quote || source?.estimate_basis || "";
  $("correctionReason").value = "";
  $("correctionStatus").textContent = "";
}

async function submitCorrection(event) {
  event.preventDefault();
  const form = $("correctionForm");
  const rowId = form.dataset.rowId;
  const fieldName = form.dataset.fieldName;
  const row = state.rows.find((item) => item.id === rowId);
  const value = row && row.values.find((item) => item.field_name === fieldName);
  if (!row || !value) return;
  const valueType = $("correctionValueType").value;
  const sourceKind = $("correctionSourceKind").value.trim() || "text";
  const pageNumber = $("correctionPageNumber").value;
  const quote = $("correctionQuote").value.trim() || null;
  const originalSource = value.sources?.find(item => item.id === value.preferred_source_id) || value.sources?.[0];
  const payload = {
    raw_value: valueType === "missing" ? null : ($("correctionRawValue").value.trim() || null),
    numeric_value: $("correctionNumericValue").value === "" ? null : Number($("correctionNumericValue").value),
    unit: $("correctionUnit").value.trim() || null,
    value_type: valueType,
    precision_digits: $("correctionPrecision").value === "" ? null : Number($("correctionPrecision").value),
    missing_reason: valueType === "missing" ? ($("correctionMissingReason").value.trim() || "missing") : null,
    correction_reason: $("correctionReason").value.trim() || null,
    source: {
      ...(originalSource?.page_basis === "ooxml" ? Object.fromEntries(["page_basis", "source_sha256", "reading_pdf_sha256", "ooxml_locator"].map(key => [key, originalSource[key]])) : {}),
      file_id: $("correctionSourceFile").value || null,
      source_kind: sourceKind,
      page_number: pageNumber ? Number(pageNumber) : null,
      label: $("correctionSourceLabel").value.trim() || null,
      quote,
    },
  };
  $("correctionSubmit").disabled = true;
  $("correctionStatus").textContent = "正在保存修正…";
  try {
    await api(`/api/rebuild/values/${value.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("correctionStatus").textContent = "已保存；先前值保留在冲突历史中。";
    await loadRows();
    showSource(rowId, fieldName);
  } catch (error) {
    $("correctionStatus").textContent = `保存失败：${error.message}`;
  } finally {
    $("correctionSubmit").disabled = false;
  }
}

let unknownImportBody = null;

async function preflightRows() {
  let payload;
  try { payload = JSON.parse($("importJson").value); }
  catch (_) { status("JSON 格式不正确", "error"); return; }
  $("preflightBtn").disabled = true;
  $("importBtn").disabled = true;
  status("正在预检查（不保存）…", "progress");
  try {
    const result = await api("/api/rebuild/rows/import/preflight", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const details = (result.errors || []).map(e => `第 ${e.row_index + 1} 行${e.field ? ` / ${e.field}` : ""}：${e.error || e.status || e.outcome}`);
    status(`预检查：${result.summary?.message || (result.complete ? "全部可处理" : "存在需处理项")}。模拟可保存 ${result.summary?.rows?.would_persist ?? 0} 行；拒绝 ${result.rows_rejected ?? 0} 行。${details.join("；")}。未保存任何数据；实际导入时重新校验，期间状态可能变化。`, result.complete ? "" : "error");
  } catch (error) {
    status(`预检查失败（未保存）：${error.message}`, "error");
  } finally {
    $("preflightBtn").disabled = false;
    $("importBtn").disabled = false;
  }
}

// A 2xx status alone is not a write receipt. Check the original request
// identity and the complete legacy result envelope before lifting protection.
function validImportReceipt(result, payload) {
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const count = value => Number.isInteger(value) && value >= 0;
  if (!object(result) || !Array.isArray(payload.rows) || result.paper_id !== payload.paper_id ||
      result.rows_submitted !== payload.rows.length || typeof result.complete !== "boolean" ||
      !Array.isArray(result.results) || result.results.length !== payload.rows.length ||
      !Array.isArray(result.errors) || !object(result.audit) || result.audit.request_id !== payload.request_id ||
      typeof result.audit.replayed !== "boolean" || !Array.isArray(result.audit.event_ids) ||
      !result.audit.event_ids.every(id => typeof id === "string") ||
      (payload.task_id != null && result.audit.task_id !== payload.task_id)) return false;
  const outcomes = { written: "rows_imported", duplicate: "rows_duplicate", partial: "rows_partial", rejected: "rows_rejected" };
  if (!Object.values(outcomes).every(key => count(result[key])) ||
      Object.values(outcomes).reduce((total, key) => total + result[key], 0) !== result.rows_submitted) return false;
  if (!result.results.every((row, index) => object(row) && row.row_index === index &&
      Object.hasOwn(outcomes, row.outcome) && typeof row.complete === "boolean" &&
      row.complete === ["written", "duplicate"].includes(row.outcome) &&
      (row.outcome === "rejected" || (typeof row.row_key === "string" && typeof row.row_id === "string" &&
        Array.isArray(row.values) && row.values.every(object) && Array.isArray(row.properties) && row.properties.every(object))))) return false;
  if (result.complete !== result.results.every(row => row.complete) ||
      !Object.entries(outcomes).every(([outcome, key]) => result[key] === result.results.filter(row => row.outcome === outcome).length) ||
      !result.errors.every(error => object(error) && count(error.row_index) && error.row_index < payload.rows.length)) return false;
  if (result.summary != null && (!object(result.summary) || typeof result.summary.message !== "string" ||
      (result.summary.values != null && (!object(result.summary.values) ||
       !["persisted", "duplicate", "pending", "conflict", "not_persisted"].every(key => count(result.summary.values[key])))))) return false;
  return true;
}

async function importRows() {
  let payload;
  try { payload = JSON.parse($("importJson").value); }
  catch (_) { status("JSON 格式不正确", "error"); return; }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    status("导入请求必须是包含 paper_id 和 rows 的 JSON 对象", "error");
    return;
  }
  // Keep the exact request in the textarea so a lost response can be retried
  // with the same id/body, including after copying it to another session.
  if (unknownImportBody && JSON.stringify(payload) !== unknownImportBody) {
    status("上次导入结果未知。请恢复并以相同 request_id 和原请求重试查回；核实回执前不要修改请求或新建 ID。", "error");
    return;
  }
  if (!payload.request_id) payload.request_id = crypto.randomUUID();
  const requestBody = JSON.stringify(payload);
  const previousUnknownBody = unknownImportBody;
  let receiptVerified = false;
  $("importJson").value = JSON.stringify(payload, null, 2);
  $("importBtn").disabled = true;
  $("preflightBtn").disabled = true;
  status("正在批量导入…", "progress");
  try {
    const result = await api("/api/rebuild/rows/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: requestBody,
    });
    if (!validImportReceipt(result, payload)) throw new Error("导入响应缺少完整且匹配原请求的回执");
    receiptVerified = true;
    unknownImportBody = null;
    const refreshed = await loadRows();
    const errors = result.errors || [];
    const rows = result.results || [];
    const pending = rows.filter(row => row.identity_status === "pending");
    const details = errors.filter(e => !(e.part === "row" && pending.some(row => row.row_index === e.row_index)))
      .map(e => `第 ${e.row_index + 1} 行${e.field ? ` / ${e.field}` : ""}${e.row_key ? `（行键 ${e.row_key}）` : ""}：${e.error || e.status || e.outcome}`);
    pending.forEach(row => details.push(`第 ${row.row_index + 1} 行身份待定；行键 ${row.row_key}；待补身份字段：${(row.pending_identity_fields || []).join(", ")}。请用该行键补交正确身份字段及来源`));
    const incomplete = result.complete === false || errors.length > 0 || pending.length > 0 ||
      (result.rows_partial || 0) > 0 || (result.rows_rejected || 0) > 0 || rows.some(row => row.complete === false);
    const cells = result.summary?.values;
    const cellSummary = cells ? `已执行行内字段：已存 ${cells.persisted}（重复 ${cells.duplicate}、身份待定 ${cells.pending}、冲突 ${cells.conflict}），未保存 ${cells.not_persisted}。` : "";
    status(`${result.summary?.message || (incomplete ? "本次尚未完整处理" : "全部成功")}。提交 ${result.rows_submitted} 行：写入 ${result.rows_imported}，重复 ${result.rows_duplicate ?? 0}，部分保存 ${result.rows_partial ?? 0}，拒绝 ${result.rows_rejected ?? 0}。${cellSummary}${incomplete ? "本次提交尚未完整处理。" : ""}${refreshed ? "" : "刷新数据行失败，请稍后手动刷新；本次回执仍有效。"}${details.length ? `需处理：${details.join("；")}。请先读回 canonical 行及完整身份/条件；明确修正失败项后用新 request_id 补交，冲突和身份待定需人工核对。` : ""}`, incomplete ? "error" : "");
  } catch (error) {
    if (receiptVerified) {
      status(`已核实原导入回执，但显示或刷新失败：${error.message}。请保留原请求和回执并手动刷新。`, "error");
    } else {
      const rejected = [400, 401, 403, 404, 409, 422].includes(error.httpStatus);
      // A rejected lookup says nothing about the earlier unknown write. Only
      // a verified original receipt can resolve that existing uncertainty.
      unknownImportBody = previousUnknownBody || (rejected ? null : requestBody);
      status(unknownImportBody ? `导入结果未确认：${error.message}。保留文本框中的原请求及 request_id，原样重试查回；查回被拒绝也不能证明原写入未发生，核实原回执前不要改体或新建 ID。` : `导入请求被拒绝：${error.message}。请核对原回执和当前行；修正已知失败项时使用新 request_id。`, "error");
    }
  } finally {
    $("importBtn").disabled = false;
    $("preflightBtn").disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("identityClose").addEventListener("click", () => { if (!identitySaving) $("identityModal").classList.remove("open"); });
  $("identityForm").addEventListener("submit", submitIdentityCorrection);
  TopNav.init({ currentPage: "data-table", mountId: "topnav-mount" });
  $("applyBtn").addEventListener("click", () => loadRows(1));
  $("reactionFilter").addEventListener("change", updateReactionHint);
  $("refreshBtn").addEventListener("click", loadRows);
  $("importBtn").addEventListener("click", importRows);
  $("preflightBtn").addEventListener("click", preflightRows);
  $("closeModal").addEventListener("click", () => $("sourceModal").classList.remove("open"));
  $("correctionForm").addEventListener("submit", submitCorrection);
  $("sourceModal").addEventListener("click", (event) => { if (event.target === $("sourceModal")) $("sourceModal").classList.remove("open"); });
  try {
    await loadTemplates();
    const params = new URLSearchParams(location.search);
    if (["dft", "experimental"].includes(params.get("data_type"))) $("dataTypeFilter").value = params.get("data_type");
    if (state.paperId) {
      let reactions = [];
      try { reactions = await resolvePaperReaction(); }
      catch (_) { reactions = []; }
      const known = Object.keys(state.templates.reactions || {});
      $("reactionFilter").value = reactions.length === 1 && known.includes(reactions[0]) ? reactions[0] : "";
    }
    await loadRows();
  } catch (error) {
    status(`初始化失败：${error.message}`, "error");
  }
});


let identityRow = null;
let identitySaving = false;
function identityIntentKey(row) { return `litai.identity-correction.${row.paper_id}.${row.id}`; }
function identityFeedback(error) {
  const message = error.message || "请求失败";
  if (message.includes("revision")) return "这行已被更新。请关闭窗口、刷新数据后重新核对再保存。";
  if (message.includes("collision")) return "已有相同身份和条件的数据，或旧行条件尚不明确。请核对已有行；不会合并或覆盖。";
  if (message.includes("identity_pending")) return "这行身份仍待定，请通过批量导入补交待定字段与来源。";
  if (message.includes("dimension")) return "条件与已保存的吸附物种或其他身份字段矛盾。请核对原文及已有字段，不会改动数值或来源。";
  if (message.includes("Not authenticated") || error.httpStatus === 401) return "登录已过期，请重新登录后重试本次保存。";
  if (error.httpStatus === 403) return "本次保存未获授权，请刷新页面并重新登录后重试。";
  return message;
}
function setIdentityLocked(locked) {
  for (const element of $("identityForm").elements) if (element.id !== "identitySubmit") element.disabled = locked;
}
async function openIdentityEditor(rowId) {
  if (identitySaving) return;
  const row = state.rows.find(item => item.id === rowId);
  if (!row) return;
  identityRow = row;
  $("identityTitle").textContent = `${row.material} · 更正身份与条件`;
  $("identityBefore").textContent = `当前材料：${row.material}；当前活性中心：${state.templates.active_site_types.find(item => item.value === row.active_site_type)?.label || row.active_site_type}；当前条件：${EditableDataGrid.readable(row.condition)}`;
  $("identitySiteType").innerHTML = state.templates.active_site_types.map(item => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  $("identitySiteType").value = row.active_site_type;
  $("identityMaterial").value=row.material || ""; $("identitySupport").value=row.support || ""; $("identityActiveSite").value=row.active_site || ""; $("identityConfiguration").value=row.configuration || "";
  $("identityCondition").value = JSON.stringify(row.condition, null, 2);
  $("identitySourceKind").value = "text";
  for (const id of ["identityPage", "identityLabel", "identityQuote", "identityReason"]) $(id).value = "";
  $("identityFile").innerHTML = '<option value="">请选择本篇关联 PDF</option>';
  $("identitySubmit").disabled = true;
  $("identityStatus").textContent = "正在读取关联文件…";
  setIdentityLocked(false);
  $("identityModal").classList.add("open");
  try {
    const files = await api(`/api/rebuild/papers/${row.paper_id}/files`);
    if (identityRow?.id !== row.id) return;
    $("identityFile").innerHTML += files.items.map(file => `<option value="${escapeHtml(file.id)}">${escapeHtml(file.role === "si" ? "SI" : "正文")}${file.lifecycle_status === "superseded" ? "（历史；需独立复核）" : file.preferred ? "（当前首选）" : ""} · ${escapeHtml(file.original_filename)}</option>`).join("");
    const saved = sessionStorage.getItem(identityIntentKey(row));
    if (saved) {
      const intent = JSON.parse(saved);
      $("identitySiteType").value = intent.changes.active_site_type;
      $("identityCondition").value = JSON.stringify(intent.changes.condition, null, 2);
      $("identityFile").value = intent.evidence[0].file_id;
      $("identitySourceKind").value = intent.evidence[0].source_kind;
      $("identityPage").value = intent.evidence[0].page_number;
      $("identityLabel").value = intent.evidence[0].label || "";
      $("identityQuote").value = intent.evidence[0].quote || "";
      $("identityReason").value = intent.correction_reason;
      setIdentityLocked(true);
    }
    $("identityStatus").textContent = row.identity_status === "pending" ? "身份待定：请用批量导入补交待定字段及来源。" : saved ? "上次保存结果尚未确认。重试将使用原请求，请勿重复创建数据行。" : "";
    $("identitySubmit").textContent = saved ? "重试原次保存" : "保存身份与条件";
    $("identitySubmit").disabled = row.identity_status === "pending" || !row.identity_revision;
  } catch (error) { $("identityStatus").textContent = `无法准备更正：${identityFeedback(error)}。请刷新后重试。`; }
}
function finiteCondition(value) {
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(finiteCondition);
  if (value && typeof value === "object") return Object.values(value).every(finiteCondition);
  return true;
}
async function submitIdentityCorrection(event) {
  event.preventDefault();
  const row = identityRow;
  if (!row || row.identity_status === "pending") return;
  const key = identityIntentKey(row);
  let payload;
  try {
    const saved = sessionStorage.getItem(key);
    if (saved) payload = JSON.parse(saved);
    else {
      let condition;
      try { condition = JSON.parse($("identityCondition").value); } catch { throw Error("条件 JSON 格式不正确，请填写完整对象，例如 {\"pH\":7}。"); }
      if (!condition || typeof condition !== "object" || Array.isArray(condition) || !finiteCondition(condition)) throw Error("条件须为 JSON 对象，数值须有限；未知条件请留空，不要填写 0。");
      if (Object.keys(condition).some(field => field.startsWith("_rebuild_")) || new TextEncoder().encode(JSON.stringify(condition)).length > 65536) throw Error("条件包含保留字段或内容过大，请只填写科学条件。");
      const reason = $("identityReason").value.trim();
      const page = Number($("identityPage").value);
      if (!reason || !$("identityFile").value || !Number.isSafeInteger(page) || page < 1) throw Error("请填写更正理由，选择本篇依据 PDF 并填写有效页码。");
      payload = {paper_id:row.paper_id,row_key:row.row_key,request_id:crypto.randomUUID(),expected_revision:row.identity_revision,
        changes:{material:$("identityMaterial").value.trim(),support:$("identitySupport").value.trim() || null,active_site:$("identityActiveSite").value.trim() || null,configuration:$("identityConfiguration").value.trim() || null,active_site_type:$("identitySiteType").value,condition},correction_reason:reason,
        evidence:[{file_id:$("identityFile").value,source_kind:$("identitySourceKind").value,page_number:page,label:$("identityLabel").value.trim() || null,quote:$("identityQuote").value.trim() || null}]};
      // Persist the exact request before sending; a lost reply must reuse this ID.
      sessionStorage.setItem(key, JSON.stringify(payload));
    }
  } catch (error) { $("identityStatus").textContent = `尚未保存：${identityFeedback(error)}`; return; }
  identitySaving = true;
  $("identityClose").disabled = true;
  $("identitySubmit").disabled = true;
  setIdentityLocked(true);
  $("identityStatus").textContent = "正在保存身份与条件…";
  try {
    const result = await api(`/api/rebuild/rows/${row.id}/identity`, {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    sessionStorage.removeItem(key);
    identityRow = result.row;
    const refreshed = await loadRows();
    $("identityStatus").textContent = refreshed ? "已保存身份与条件；原身份、理由与证据已保留，数值与来源未改变。" : "已保存身份与条件，但列表刷新失败。请关闭窗口后刷新列表；本次保存已确认，无需重试保存。";
    $("identityBefore").textContent = `当前条件：${JSON.stringify(result.row.condition)}`;
    $("identitySubmit").textContent = "保存身份与条件";
    setIdentityLocked(false);
  } catch (error) {
    if (error.httpStatus && error.httpStatus < 500) {
      sessionStorage.removeItem(key);
      setIdentityLocked(false);
      $("identityStatus").textContent = `未保存：${identityFeedback(error)}`;
    } else {
      $("identityStatus").textContent = "保存结果尚未确认。请重试原次保存；使用同一请求，已保存的结果不会重复写入。";
      $("identitySubmit").textContent = "重试原次保存";
    }
  } finally {
    identitySaving = false;
    $("identityClose").disabled = false;
    $("identitySubmit").disabled = false;
  }
}

window.addEventListener("load", async () => {const rowId=new URLSearchParams(location.search).get("row_id");if(!rowId)return;for(let i=0;i<50;i++){if(state.rows.some(row=>row.id===rowId)){openIdentityEditor(rowId);return;}await new Promise(resolve=>setTimeout(resolve,100));}});
