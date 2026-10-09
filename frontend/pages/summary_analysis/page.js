"use strict";

const state = { templates: null, result: null, fieldsReady: false, fieldLoadSeq: 0, scopeKey: null };
function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fieldScope() { return {paper_id: new URLSearchParams(location.search).get("paper_id") || "", reaction: $("reactionFilter").value || "", material: $("materialFilter").value.trim(), active_site_type: $("activeSiteFilter").value, data_type: $("dataTypeFilter").value}; }

function $(id) { return document.getElementById(id); }

function status(message, kind) {
  const area = $("statusArea");
  if (!message) { area.innerHTML = ""; return; }
  const cls = kind === "error" ? "rebuild-error" : kind === "progress" ? "rebuild-progress" : "rebuild-empty";
  area.innerHTML = `<div class="${cls}" role="status">${message}</div>`;
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

async function loadTemplates() {
  const paperId = new URLSearchParams(location.search).get("paper_id");
  $("paperScopeHint").textContent = paperId ? "正在读取文献范围…" : "分析范围：全部文献";
  if (paperId) {
    try {
      const paper = await api(`/api/rebuild/papers/${encodeURIComponent(paperId)}`);
      $("paperScopeHint").textContent = `当前文献：${paper.paper_code || "未编号"} · ${paper.title || "未命名文献"}`;
    } catch (_) { $("paperScopeHint").textContent = "文献范围信息加载失败；分析仍限制当前文献。"; }
  }
  state.templates = await api("/api/rebuild/templates");
  $("reactionFilter").innerHTML = Object.entries(state.templates.reactions).map(([value, item]) => `<option value="${value}">${item.label}</option>`).join("");
  $("activeSiteFilter").innerHTML = `<option value="">全部</option>` + state.templates.active_site_types.map((item) => `<option value="${item.value}">${item.label}</option>`).join("");
  await updateFields();
}

async function updateFields() {
  const seq = ++state.fieldLoadSeq;
  const scope = fieldScope(), scopeKey = JSON.stringify(scope);
  const previousX = $("xField").value, previousY = $("yField").value;
  state.fieldsReady = false; state.scopeKey = null; state.result = null;
  $("analyzeBtn").disabled = true; $("refreshBtn").disabled = true;
  $("xField").innerHTML = ""; $("yField").innerHTML = "";
  $("resultArea").innerHTML = '<div class="rebuild-empty">正在读取当前范围已保存的字段…</div>';
  $("csvLink").style.display = "none";
  status("正在读取当前范围全部数据行…", "progress");
  try {
    const saved = new Map(), seen = new Set(); let offset = 0, expectedTotal = null;
    while (true) {
      const params = new URLSearchParams({limit:"500", offset:String(offset)});
      if (scope.paper_id) params.set("paper_id", scope.paper_id);
      for (const key of ["reaction","material","active_site_type","data_type"]) if (scope[key]) params.set(key, scope[key]);
      const data = await api(`/api/rebuild/rows?${params}`);
      if (seq !== state.fieldLoadSeq) return;
      if (!Array.isArray(data.items) || !Number.isInteger(data.total) || data.total < 0) throw Error("数据行响应无效");
      if (expectedTotal === null) expectedTotal = data.total;
      if (data.total !== expectedTotal) throw Error("读取期间数据已变动，请刷新重读");
      for (const row of data.items) {
        if (!row.id || seen.has(row.id)) throw Error("分页数据重复或缺少身份，请刷新重读");
        if ((scope.paper_id && row.paper_id !== scope.paper_id) || (scope.reaction && row.reaction !== scope.reaction)) throw Error("数据行范围不匹配");
        seen.add(row.id);
        for (const value of row.values || []) {
          if (typeof value.numeric_value !== "number" || !Number.isFinite(value.numeric_value) || !value.field_name) continue;
          if (!saved.has(value.field_name)) saved.set(value.field_name, new Set());
          saved.get(value.field_name).add(value.unit == null || value.unit === "" ? "未记录单位" : String(value.unit));
        }
      }
      offset += data.items.length;
      if (offset === expectedTotal) break;
      if (!data.items.length || offset > expectedTotal) throw Error("分页读取不完整，请刷新重读");
    }
    const reaction = scope.reaction;
    const templateFields = state.templates.common_fields.concat(reaction ? state.templates.reactions[reaction].fields : []);
    const uniqueTemplates = [...new Map(templateFields.map(f => [f.name,f])).values()];
    const templateOptions = uniqueTemplates.map(f => `<option value="${esc(f.name)}">${esc(f.label)}${f.unit ? ` (${esc(f.unit)})` : ""} · 模板</option>`).join("");
    const savedOptions = [...saved.entries()].sort(([a],[b]) => a.localeCompare(b)).map(([name,units]) => `<option value="${esc(name)}">${esc(name)} · 已保存 [${esc([...units].sort().join(" / "))}${units.size > 1 ? "；多单位" : ""}]</option>`).join("");
    const options = `<optgroup label="模板字段">${templateOptions}</optgroup>` + (savedOptions ? `<optgroup label="当前范围已保存数值字段">${savedOptions}</optgroup>` : "");
    for (const [id,previous] of [["xField",previousX],["yField",previousY]]) {
      $(id).innerHTML = options;
      if ([...$(id).options].some(o => o.value === previous)) $(id).value = previous;
      else if (id === "yField" && $(id).options.length > 1) $(id).selectedIndex = 1;
    }
    state.fieldsReady = true; state.scopeKey = scopeKey;
    $("analyzeBtn").disabled = false; $("refreshBtn").disabled = false;
    $("resultArea").innerHTML = '<div class="rebuild-empty">选择字段并生成分析。</div>';
    status(saved.size ? `当前范围 ${expectedTotal} 行，${saved.size} 个已保存数值字段。` : "当前范围无已保存数值字段；模板字段仍可选择。请检查范围或数据。");
  } catch (error) {
    if (seq !== state.fieldLoadSeq) return;
    status(`当前范围字段读取失败：${error.message}。请刷新字段，未使用旧范围。`, "error");
    $("resultArea").innerHTML = '<div class="rebuild-empty">字段未读取完整，分析已暂停。</div>';
    $("refreshBtn").disabled = false;
  }
}

function axisUnit(result, axis) {
  const units = [...new Set(result.unit_groups.map(g => g[axis + "_unit"] || "未记录单位"))];
  const plotted = result[axis + "_unit"] || "未记录单位";
  return units.length > 1 ? plotted + "；原数据多单位：" + units.join(" / ") : plotted;
}

function chartSvg(result) {
  if (!result.points.length) return "";
  const width = 760;
  const height = 430;
  const margin = { top: 24, right: 24, bottom: 54, left: 78 };
  const xs = result.points.map((point) => point.x);
  const ys = result.points.map((point) => point.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xPad = (xMax - xMin || Math.max(1, Math.abs(xMax) * 0.1)) * 0.08;
  const yPad = (yMax - yMin || Math.max(1, Math.abs(yMax) * 0.1)) * 0.08;
  const x0 = xMin - xPad, x1 = xMax + xPad, y0 = yMin - yPad, y1 = yMax + yPad;
  const sx = (value) => margin.left + ((value - x0) / (x1 - x0)) * (width - margin.left - margin.right);
  const sy = (value) => height - margin.bottom - ((value - y0) / (y1 - y0)) * (height - margin.top - margin.bottom);
  const points = result.points.map((point) => `<circle cx="${sx(point.x)}" cy="${sy(point.y)}" r="6"><title>${point.material}: (${point.x}, ${point.y})</title></circle>`).join("");
  let line = "";
  if (result.regression) {
    const startX = x0, endX = x1;
    const startY = result.regression.intercept + result.regression.slope * startX;
    const endY = result.regression.intercept + result.regression.slope * endX;
    line = `<line x1="${sx(startX)}" y1="${sy(startY)}" x2="${sx(endX)}" y2="${sy(endY)}" stroke="#2563eb" stroke-width="2" />`;
  }
  return `
    <svg class="rebuild-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="散点图">
      <rect x="${margin.left}" y="${margin.top}" width="${width-margin.left-margin.right}" height="${height-margin.top-margin.bottom}" fill="#fff" />
      ${line}${points}
      <line x1="${margin.left}" y1="${height-margin.bottom}" x2="${width-margin.right}" y2="${height-margin.bottom}" stroke="#94a3b8" />
      <line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height-margin.bottom}" stroke="#94a3b8" />
      <text x="${width/2}" y="${height-16}" text-anchor="middle">${esc(result.x_field)} (${esc(axisUnit(result,"x"))})</text>
      <text x="20" y="${height/2}" transform="rotate(-90 20 ${height/2})" text-anchor="middle">${esc(result.y_field)} (${esc(axisUnit(result,"y"))})</text>
    </svg>`;
}

function renderResult(result) {
  state.result = result;
  const regression = result.regression;
  const warnings = result.warnings.map((warning) => `<li>${warning}</li>`).join("");
  const units = result.unit_groups.map((group) => `<span class="rebuild-tag">${group.x_unit || "无单位"} / ${group.y_unit || "无单位"}：${group.count}</span>`).join("");
  const comparisons = result.comparison_fields.map((field) => `<span class="rebuild-tag">比较变量：${field}</span>`).join("");
  const controlled = result.controlled_fields.map((field) => `<span class="rebuild-tag">控制变量：${field}</span>`).join("");
  const groups = result.comparison_groups.map((group) => `
    <details>
      <summary>可比组：${group.count} 条</summary>
      <p>控制字段：${group.controlled_fields.join("、") || "无"}</p>
      <p>条件键：${group.condition_keys.join("、") || "无"}</p>
      <p>控制值：${JSON.stringify(group.controlled_values)}</p>
      <p>条件值：${JSON.stringify(group.condition_values)}</p>
    </details>
  `).join("");
  $("resultArea").innerHTML = `
    <div class="rebuild-metrics">
      <div class="rebuild-metric"><span>有效样本</span><strong>${result.sample_count}</strong></div>
      <div class="rebuild-metric"><span>不可比排除</span><strong>${result.excluded_count}</strong></div>
      <div class="rebuild-metric"><span>斜率</span><strong>${regression ? regression.slope.toFixed(4) : "—"}</strong></div>
      <div class="rebuild-metric"><span>截距</span><strong>${regression ? regression.intercept.toFixed(4) : "—"}</strong></div>
      <div class="rebuild-metric"><span>R²</span><strong>${regression ? regression.r_squared.toFixed(4) : "—"}</strong></div>
    </div>
    <div style="margin:16px 0">${comparisons}${controlled}${units}</div>
    ${chartSvg(result)}
    <h3>缺失与排除说明</h3>
    <ul>${warnings || "<li>无警告。</li>"}</ul>
    <h3>可比组</h3>
    ${groups || '<div class="rebuild-empty">无可比组。</div>'}
  `;
  $("csvLink").href = `/api/rebuild/analysis/${result.id}/csv`;
  $("csvLink").style.display = "inline-flex";
}

async function runAnalysis() {
  if (!state.fieldsReady || state.scopeKey !== JSON.stringify(fieldScope())) { status("当前范围字段尚未读取完整，请刷新字段。", "error"); return; }
  const seq = state.fieldLoadSeq;
  const payload = {
    paper_id: new URLSearchParams(location.search).get("paper_id") || null,
    x_field: $("xField").value,
    y_field: $("yField").value,
    reaction: $("reactionFilter").value || null,
    material: $("materialFilter").value.trim() || null,
    active_site_type: $("activeSiteFilter").value || null,
    data_type: $("dataTypeFilter").value || null,
    required_condition_keys: ($("requiredConditionKeys").value || "").split(",").map((key) => key.trim()).filter(Boolean),
    comparison_condition_keys: ($("comparisonConditionKeys").value || "").split(",").map((key) => key.trim()).filter(Boolean),
    comparison_fields: ($("comparisonFields").value || "").split(",").map((key) => key.trim()).filter(Boolean),
    include_estimated: $("includeEstimated").checked,
  };
  $("analyzeBtn").disabled = true;
  status("正在计算配对样本…", "progress");
  try {
    const result = await api("/api/rebuild/analysis", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (seq !== state.fieldLoadSeq) return;
    renderResult(result);
    status("");
  } catch (error) {
    if (seq === state.fieldLoadSeq) status(`分析失败：${error.message}`, "error");
  } finally {
    $("analyzeBtn").disabled = !state.fieldsReady;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  TopNav.init({ currentPage: "summary-analysis", mountId: "topnav-mount" });
  for (const id of ["reactionFilter","materialFilter","activeSiteFilter","dataTypeFilter"]) $(id).addEventListener("change", updateFields);
  $("analyzeBtn").addEventListener("click", runAnalysis);
  $("refreshBtn").addEventListener("click", async () => { await updateFields(); if (state.fieldsReady) await runAnalysis(); });
  try {
    await loadTemplates();

  } catch (error) {
    status(`初始化失败：${error.message}`, "error");
  }
});
