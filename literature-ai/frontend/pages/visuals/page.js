(function () {
  "use strict";

  const DEFAULTS = {
    x: "li2s_adsorption_energy",
    y: "li2s_dissociation_barrier",
    quickX: "li2s_bader_charge_transfer",
    quickY: "li_s_bond_max",
  };
  const CURRENT_LIBRARY_STORAGE_KEY = "litai_current_library";
  const state = {
    fields: [],
    requestId: 0,
    libraryName: new URLSearchParams(window.location.search).get("library_name") || "",
    libraryResolutionFailed: false,
    lastCorrelation: null,
  };
  const WARNING_LABELS = {
    min_n_not_reached: "有效配对未达到最少样本数，暂不计算拟合结果",
    fewer_than_two_contributing_papers: "有效数据来自不到 2 篇论文，跨文献可信度不足",
  };
  const EXCLUSION_LABELS = {
    context_mismatch: "X、Y 都有候选值，但计算设置、位点或构型不兼容",
    multiple_comparable_contexts: "存在多个可比计算上下文，无法唯一选择",
    missing_both_field_values: "同一催化剂同时缺少 X 和 Y 字段",
    missing_x_field_value: "同一催化剂缺少 X 字段",
    missing_y_field_value: "同一催化剂缺少 Y 字段",
    missing_field_value: "同一催化剂缺少所选字段",
    conflicting_values: "同一语义上下文存在冲突数值",
    identity_v2_required: "历史记录尚未完成 Identity V2 结构化身份",
    identity_v2_not_ml_ready: "Identity V2 信息不完整，尚不能用于分析",
    pair_analysis_invalid_numeric_target: "缺少可用于关系分析的有限数值",
    pair_analysis_target_not_normalized: "数值或单位无法安全标准化",
    missing_catalyst_sample_id: "记录未明确绑定催化剂样品",
    missing_or_ambiguous_calculation_context: "计算设置缺失或无法唯一关联",
    "safety_gate:missing_atom_or_site_identity": "缺少原子或吸附位点身份",
    "safety_gate:missing_atom_pair_identity": "缺少键长对应的原子对身份",
    "safety_gate:missing_material_identity": "缺少材料或催化剂身份",
    "safety_gate:missing_reaction_step_identity": "缺少反应步骤身份",
    "safety_gate:missing_required_unit": "缺少该性质必需的单位",
    "safety_gate:missing_review": "记录尚未完成审核",
    "safety_gate:missing_state_context_identity": "缺少初态、终态或吸附状态身份",
    "safety_gate:missing_unit_identity": "单位尚未完成结构化识别",
    "safety_gate:missing_value_identity": "缺少可用数值",
    "safety_gate:target_rejected": "记录已在审核中被拒绝",
    "safety_gate:unsafe_review": "审核状态不允许用于分析",
    "safety_gate:unsupported_unit_identity": "单位不受支持或无法安全换算",
  };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const number = (value) => value === null || value === undefined || value === ""
    ? null
    : (Number.isFinite(Number(value)) ? Number(value) : null);
  const display = (value) => number(value) === null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("请求失败（" + response.status + "）");
    return response.json();
  }

  function visualParams(params) {
    if (state.libraryName) params.set("library_name", state.libraryName);
    return params;
  }

  async function resolveLibraryScope() {
    if (state.libraryName) return;
    try {
      const payload = await getJSON("/api/libraries");
      const libraries = Array.isArray(payload) ? payload : (payload.libraries || []);
      const stored = window.localStorage.getItem(CURRENT_LIBRARY_STORAGE_KEY) || "";
      const selected = libraries.find((item) => item.name === stored)
        || libraries.find((item) => item.is_active)
        || libraries[0];
      if (selected?.name) state.libraryName = selected.name;
    } catch (_error) {
      state.libraryResolutionFailed = true;
    }
  }

  function downloadFilename(response, fallback) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/i);
    return match ? match[1] : fallback;
  }

  async function downloadDataset(kind) {
    const isCsv = kind === "csv";
    const button = $(isCsv ? "exportCatalystCsv" : "downloadCatalystJson");
    const label = isCsv ? "催化剂宽表 CSV" : "审计 JSON";
    const endpoint = isCsv ? "/api/dft/catalyst-dataset.csv" : "/api/dft/catalyst-dataset";
    button.disabled = true;
    $("exportStatus").textContent = "正在准备" + label + "…";
    try {
      const params = visualParams(new URLSearchParams());
      const response = await fetch(endpoint + "?" + params.toString(), {
        headers: { Accept: isCsv ? "text/csv" : "application/json" },
      });
      if (!response.ok) {
        let detail = "";
        try {
          const payload = await response.json();
          detail = payload.detail ? "：" + payload.detail : "";
        } catch (_error) {
          detail = "";
        }
        throw new Error("请求失败（" + response.status + "）" + detail);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = downloadFilename(
        response,
        isCsv ? "dft_catalyst_dataset_v1.csv" : "dft_catalyst_dataset_v1.json",
      );
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
      $("exportStatus").textContent = label + "已开始下载。";
    } catch (error) {
      $("exportStatus").textContent = label + "下载失败：" + error.message;
    } finally {
      button.disabled = false;
    }
  }

  function setOverview(summary) {
    const values = [
      ["metricDftTotal", summary.total_dft_rows],
      ["metricExportEligible", summary.exportable_dft_rows],
      ["metricV2Numeric", summary.v2_row_ready_numeric_rows],
      ["metricCatalysts", summary.distinct_exportable_catalysts],
      ["metricPapers", summary.contributing_papers],
    ];
    values.forEach(([id, value]) => { $(id).textContent = display(value); });
  }

  async function loadOverview() {
    $("metrics").classList.add("is-loading");
    try {
      const data = await getJSON("/api/visuals/overview?" + visualParams(new URLSearchParams()).toString());
      setOverview(data.summary || data.overview || {});
      $("overviewStatus").textContent = state.libraryName
        ? "当前文献库：" + state.libraryName
        : (state.libraryResolutionFailed ? "当前文献库读取失败，已显示全部文献库" : "全部文献库");
    } catch (error) {
      $("overviewStatus").textContent = "概览读取失败：" + error.message;
    } finally {
      $("metrics").classList.remove("is-loading");
    }
  }

  function normaliseFields(payload) {
    const raw = Array.isArray(payload) ? payload : (payload.fields || payload.analysis_fields || []);
    return raw.filter((field) => field && field.key && (field.type === "number" || field.numeric === true) && field.analysis_enabled !== false)
      .map((field) => ({ key: field.key, label: field.label || field.display_name || field.key }));
  }

  function fillSelect(select, fields, preferred) {
    select.innerHTML = "";
    fields.forEach((field) => {
      const option = document.createElement("option");
      option.value = field.key;
      option.textContent = field.label;
      select.appendChild(option);
    });
    if (fields.some((field) => field.key === preferred)) select.value = preferred;
  }

  function fieldLabel(key) {
    return state.fields.find((field) => field.key === key)?.label || key;
  }

  async function loadFields() {
    const payload = await getJSON("/api/visuals/analysis-fields?" + visualParams(new URLSearchParams()).toString());
    state.fields = normaliseFields(payload);
    if (!state.fields.length) throw new Error("接口未返回可用于数值相关分析的字段");
    fillSelect($("xField"), state.fields, DEFAULTS.x);
    fillSelect($("yField"), state.fields, DEFAULTS.y);
  }

  function listItems(id, items) {
    const target = $(id);
    target.innerHTML = (items || []).map((item) => "<li>" + esc(typeof item === "string" ? item : (item.reason || item.message || JSON.stringify(item))) + "</li>").join("");
  }

  function warningLabel(reason) {
    return WARNING_LABELS[reason] || reason;
  }

  function exclusionLabel(reason) {
    if (reason === "missing_x_field_value") return "同一催化剂缺少 X 字段（" + fieldLabel($("xField").value) + "）";
    if (reason === "missing_y_field_value") return "同一催化剂缺少 Y 字段（" + fieldLabel($("yField").value) + "）";
    return EXCLUSION_LABELS[reason] || reason;
  }

  function renderDiagnostics(data) {
    const warnings = data.warnings || [];
    const excluded = data.excluded_reasons || data.excluded || {};
    const excludedEntries = Array.isArray(excluded)
      ? excluded.map((item) => [typeof item === "string" ? item : (item.reason || item.message || JSON.stringify(item)), ""])
      : Object.entries(excluded);
    $("diagnostics").hidden = !(warnings.length || excludedEntries.length);
    $("warningsBox").hidden = !warnings.length;
    $("excludedBox").hidden = !excludedEntries.length;
    listItems("warningsList", warnings.map(warningLabel));
    $("excludedList").innerHTML = excludedEntries.map(([reason, count]) => "<li>" + esc(exclusionLabel(reason)) + (count === "" ? "" : "：" + esc(count) + " 次") + (EXCLUSION_LABELS[reason] || reason.startsWith("safety_gate:") || reason.startsWith("missing_") || reason.startsWith("identity_") ? " <code>" + esc(reason) + "</code>" : "") + "</li>").join("");

    const catalystCount = Number(data.n_catalysts ?? data.catalyst_count ?? (data.points || []).length) || 0;
    const paperCount = Number(data.n_papers ?? data.paper_count) || 0;
    const minN = Number(data.min_n ?? data.statistics?.min_n ?? $("minN").value) || 3;
    $("diagnostics").dataset.state = data.ready === true ? "ready" : "blocked";
    $("diagnosticFlag").textContent = data.ready === true ? "数据范围说明" : "需要处理";
    $("diagnosticTitle").textContent = data.ready === true ? "当前数据可以进行拟合" : "当前数据不足，暂不进行拟合";
    $("diagnosticMessage").textContent = data.ready === true
      ? "已形成 " + catalystCount + " 个同一催化剂有效配对，来自 " + paperCount + " 篇论文。"
      : "当前只有 " + catalystCount + " 个同一催化剂有效配对，至少需要 " + minN + " 个才能计算拟合结果。";
    const highlights = [];
    const legacyPointCount = Number(data.legacy_identity_point_count) || 0;
    if (legacyPointCount > 0) {
      highlights.push("本次 " + catalystCount + " 个配对中，有 " + legacyPointCount + " 个使用了通过审核的历史身份记录；这些记录仍须有明确的催化剂 ID 和计算设置。");
    }
    if (paperCount < 2) highlights.push("有效数据来自不到 2 篇论文，当前结果只能用于数据核验。");
    $("diagnosticHighlights").innerHTML = highlights.map((item) => "<li>" + esc(item) + "</li>").join("");
    $("technicalDiagnostics").open = false;
  }

  function statistic(data, name) {
    return data.statistics?.[name] ?? data[name] ?? null;
  }

  function renderStatistics(data) {
    const stats = {
      statPearson: statistic(data, "pearson_r"),
      statSpearman: statistic(data, "spearman_rho"),
      statR2: statistic(data, "r_squared"),
      statSlope: statistic(data, "slope"),
      statIntercept: statistic(data, "intercept"),
      statCatalysts: data.n_catalysts ?? data.catalyst_count,
      statPapers: data.n_papers ?? data.paper_count,
    };
    Object.entries(stats).forEach(([id, value]) => { $(id).textContent = display(value); });
  }

  function renderVerdict(data) {
    const card = $("verdictCard");
    const r = number(statistic(data, "pearson_r"));
    const r2 = number(statistic(data, "r_squared"));
    const catalysts = Number(data.n_catalysts ?? data.catalyst_count ?? (data.points || []).length) || 0;
    const papers = Number(data.n_papers ?? data.paper_count) || 0;
    if (data.ready !== true || r === null) {
      card.dataset.state = "idle";
      $("verdictLabel").textContent = "暂无结论";
      $("verdictR").textContent = "—";
      $("verdictMeta").textContent = "有效配对 " + catalysts + " 个，未达到拟合条件";
      $("verdictEquation").textContent = "";
      $("verdictQuality").textContent = "";
      return;
    }
    const abs = Math.abs(r);
    const strength = abs >= 0.8 ? "强" : abs >= 0.5 ? "中等" : abs >= 0.3 ? "弱" : "极弱";
    const direction = r > 0 ? "正相关" : r < 0 ? "负相关" : "相关";
    card.dataset.state = abs >= 0.8 ? "strong" : abs >= 0.5 ? "medium" : "weak";
    $("verdictLabel").textContent = strength + direction;
    $("verdictR").textContent = "r = " + display(r);
    $("verdictMeta").textContent = "n=" + catalysts + (r2 !== null ? " · R²=" + display(r2) : "");
    const slope = number(statistic(data, "slope"));
    const intercept = number(statistic(data, "intercept"));
    $("verdictEquation").textContent = slope === null || intercept === null
      ? ""
      : "y = " + fmt2(slope) + "x " + (intercept >= 0 ? "+ " : "− ") + fmt2(Math.abs(intercept));
    $("verdictQuality").textContent = papers < 2 ? "数据来自单一论文，仅供核验" : "跨 " + papers + " 篇论文";
  }

  function axisValue(point, axis) {
    return point?.[axis]?.value ?? point?.[axis + "_value"] ?? null;
  }

  function pointCatalystName(point) {
    return point.catalyst_name || point.catalyst?.name || point.catalyst || "催化剂";
  }

  let activePointIdx = null;

  function hideTooltip() {
    activePointIdx = null;
    $("plotTooltip").hidden = true;
    $("plotLayer").querySelectorAll(".plot-point.is-selected").forEach((c) => c.classList.remove("is-selected"));
    $("plotLegend").querySelectorAll(".plot-legend-item.is-selected").forEach((item) => item.classList.remove("is-selected"));
    $("detailsBody").querySelectorAll("tr.is-selected-row").forEach((r) => r.classList.remove("is-selected-row"));
  }

  function selectPoint(idx, circle) {
    const data = state.lastCorrelation;
    if (!data) return;
    const points = (data.points || []).filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    const point = points[idx];
    if (!point) return;
    hideTooltip();
    activePointIdx = idx;
    const targetCircle = circle || $("plotLayer").querySelector('.plot-point[data-idx="' + idx + '"]');
    if (!targetCircle) return;
    targetCircle.classList.add("is-selected");
    const legendItem = $("plotLegend").querySelector('.plot-legend-item[data-idx="' + idx + '"]');
    if (legendItem) legendItem.classList.add("is-selected");
    const xLabel = data.x_label || fieldLabel(data.x_field || $("xField").value);
    const yLabel = data.y_label || fieldLabel(data.y_field || $("yField").value);
    const tooltip = $("plotTooltip");
    tooltip.innerHTML = '<strong class="tooltip-title">' + esc(pointCatalystName(point)) + "</strong>"
      + '<span class="muted">' + esc(point.catalyst_sample_id || "") + "</span><br>"
      + esc(xLabel) + "：<b>" + esc(display(axisValue(point, "x"))) + "</b><br>"
      + esc(yLabel) + "：<b>" + esc(display(axisValue(point, "y"))) + "</b>";
    const wrapRect = document.querySelector(".plot-wrap").getBoundingClientRect();
    tooltip.hidden = false;
    const circleRect = targetCircle.getBoundingClientRect();
    const tooltipWidth = tooltip.offsetWidth;
    const tooltipHeight = tooltip.offsetHeight;
    let left = circleRect.left - wrapRect.left + 14;
    if (left + tooltipWidth > wrapRect.width - 8) left = circleRect.left - wrapRect.left - tooltipWidth - 14;
    left = Math.max(8, Math.min(left, wrapRect.width - tooltipWidth - 8));
    let top = circleRect.top - wrapRect.top - 10;
    if (top + tooltipHeight > wrapRect.height - 8) top = wrapRect.height - tooltipHeight - 8;
    tooltip.style.left = left + "px";
    tooltip.style.top = Math.max(top, 8) + "px";
    const row = $("detailsBody").querySelector('tr[data-idx="' + idx + '"]');
    if (row) {
      row.classList.add("is-selected-row");
    }
  }

  function niceStep(range, targetCount) {
    if (!range || !isFinite(range)) return 1;
    const raw = range / targetCount;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    let step;
    if (norm < 1.5) step = 1;
    else if (norm < 3) step = 2;
    else if (norm < 7) step = 5;
    else step = 10;
    return step * mag;
  }

  function niceScale(min, max, targetCount) {
    if (min === max) { min -= 0.5; max += 0.5; }
    const rawSpan = max - min;
    const paddedMin = min - rawSpan * 0.06;
    const paddedMax = max + rawSpan * 0.06;
    const step = niceStep(paddedMax - paddedMin, targetCount);
    const start = Math.floor(paddedMin / step) * step;
    const end = Math.ceil(paddedMax / step) * step;
    const ticks = [];
    const precision = Math.max(0, -Math.floor(Math.log10(step)) + 2);
    for (let v = start; v <= end + step * 0.001; v += step) ticks.push(Number(v.toFixed(precision)));
    return { min: start, max: end, ticks };
  }

  function formatTick(value) {
    const n = Number(value);
    if (!isFinite(n)) return "—";
    if (Math.abs(n) >= 1000 || (Math.abs(n) < 0.01 && n !== 0)) return n.toExponential(1);
    return (Math.round(n * 100) / 100).toString();
  }

  function fmt2(value) {
    return value === null || value === undefined ? "—" : (Math.round(value * 100) / 100).toString();
  }

  function plot(data) {
    const points = (data.points || []).filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    const layer = $("plotLayer");
    const empty = $("plotEmpty");
    layer.innerHTML = "";
    hideTooltip();
    if (!points.length) {
      $("plotLegend").innerHTML = "";
      $("plotLegend").hidden = true;
      empty.textContent = data.insufficient_reason || data.fit?.reason || "没有可绘制的同一催化剂样本";
      empty.style.display = "";
      return;
    }
    empty.style.display = "none";
    const xValues = points.map((point) => number(axisValue(point, "x")));
    const yValues = points.map((point) => number(axisValue(point, "y")));
    const xScale = niceScale(Math.min(...xValues), Math.max(...xValues), 5);
    const yScale = niceScale(Math.min(...yValues), Math.max(...yValues), 5);
    const xTicks = xScale.ticks, yTicks = yScale.ticks;
    const xLo = xScale.min, xHi = xScale.max;
    const yLo = yScale.min, yHi = yScale.max;
    const xSpan = xHi - xLo || 1, ySpan = yHi - yLo || 1;
    const L = 58, R = 606, T = 42, B = 334;
    const W = R - L, H = B - T;
    const sx = (x) => L + ((x - xLo) / xSpan) * W;
    const sy = (y) => B - ((y - yLo) / ySpan) * H;

    layer.insertAdjacentHTML("beforeend",
      '<clipPath id="plotClip"><rect x="' + L + '" y="' + T + '" width="' + W + '" height="' + H + '"/></clipPath>');

    let grid = '';
    yTicks.forEach((t) => {
      const y = sy(t);
      grid += '<line class="plot-grid" x1="' + L + '" y1="' + y + '" x2="' + R + '" y2="' + y + '"/>';
      grid += '<text class="plot-tick" x="' + (L - 6) + '" y="' + (y + 3) + '" text-anchor="end">' + esc(formatTick(t)) + "</text>";
    });
    xTicks.forEach((t) => {
      const x = sx(t);
      grid += '<line class="plot-grid" x1="' + x + '" y1="' + T + '" x2="' + x + '" y2="' + B + '"/>';
      grid += '<text class="plot-tick" x="' + x + '" y="' + (B + 16) + '" text-anchor="middle">' + esc(formatTick(t)) + "</text>";
    });
    layer.insertAdjacentHTML("beforeend", grid);

    layer.insertAdjacentHTML("beforeend",
      '<line class="plot-axis" x1="' + L + '" y1="' + T + '" x2="' + L + '" y2="' + B + '"/>'
      + '<line class="plot-axis" x1="' + L + '" y1="' + B + '" x2="' + R + '" y2="' + B + '"/>');

    const xTitle = data.x_label || fieldLabel(data.x_field || $("xField").value);
    const yTitle = data.y_label || fieldLabel(data.y_field || $("yField").value);
    layer.insertAdjacentHTML("beforeend",
      '<text class="plot-axis-title" x="' + (L + W / 2) + '" y="' + (B + 34) + '" text-anchor="middle">' + esc(xTitle) + "</text>"
      + '<text class="plot-axis-title" x="14" y="' + (T + H / 2) + '" text-anchor="middle" transform="rotate(-90 14 ' + (T + H / 2) + ')">' + esc(yTitle) + "</text>");

    const slope = number(data.statistics?.slope);
    const intercept = number(data.statistics?.intercept);
    const fitReady = data.ready === true && slope !== null && intercept !== null;
    if (fitReady) {
      layer.insertAdjacentHTML("beforeend",
        '<g clip-path="url(#plotClip)"><line class="fit-line" x1="' + sx(xLo) + '" y1="' + sy(slope * xLo + intercept) + '" x2="' + sx(xHi) + '" y2="' + sy(slope * xHi + intercept) + '"/></g>');
    }

    const showLegend = points.length <= 12;
    let pts = '';
    points.forEach((point, idx) => {
      const x = number(axisValue(point, "x")), y = number(axisValue(point, "y"));
      const aria = pointCatalystName(point) + "，X " + display(x) + "，Y " + display(y);
      pts += '<circle class="plot-point" role="button" tabindex="0" aria-label="' + esc(aria) + '" data-idx="' + idx + '" cx="' + sx(x) + '" cy="' + sy(y) + '" r="7"><title>' + esc(pointCatalystName(point) + " · " + (point.catalyst_sample_id || "")) + "</title></circle>";
      if (showLegend) pts += '<text class="plot-point-index" x="' + sx(x) + '" y="' + sy(y) + '">' + (idx + 1) + "</text>";
    });
    layer.insertAdjacentHTML("beforeend", pts);

    const legend = $("plotLegend");
    legend.hidden = !showLegend;
    legend.innerHTML = showLegend ? points.map((point, idx) => '<li><button class="plot-legend-item" type="button" data-idx="' + idx + '"><span class="plot-legend-index">' + (idx + 1) + '</span><span class="plot-legend-name">' + esc(pointCatalystName(point)) + "</span></button></li>").join("") : "";

    layer.querySelectorAll(".plot-point").forEach((circle) => {
      circle.addEventListener("click", (event) => {
        event.stopPropagation();
        selectPoint(Number(circle.getAttribute("data-idx")), circle);
      });
      circle.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          event.stopPropagation();
          selectPoint(Number(circle.getAttribute("data-idx")), circle);
        }
      });
    });
    legend.querySelectorAll(".plot-legend-item").forEach((item) => item.addEventListener("click", (event) => {
      event.stopPropagation();
      selectPoint(Number(item.getAttribute("data-idx")));
    }));
    $("fitNotice").textContent = fitReady ? "拟合线按接口返回的斜率和截距绘制。" : (data.insufficient_reason || data.reason || "样本量不足或数据不可比，未绘制拟合线。");
  }

  function ids(value) {
    const list = Array.isArray(value) ? value : (value ? [value] : []);
    return list.length ? list.map((item) => "<code>" + esc(item) + "</code>").join("、") : "—";
  }

  function candidates(point, data) {
    const barrierAxis = data.x_field === DEFAULTS.y ? "x" : (data.y_field === DEFAULTS.y ? "y" : "");
    const list = barrierAxis ? (point[barrierAxis]?.candidates || []) : [];
    if (!list.length) return "—";
    return '<div class="candidate-list">' + list.map((candidate) => {
      const selected = candidate.selected_for_summary === true || candidate.selected_for_regression === true;
      const label = [candidate.pathway || candidate.label || "候选路径", display(candidate.value ?? candidate.barrier)].join("：");
      return '<span class="candidate-item"><span class="' + (selected ? "selected-candidate" : "") + '">' + esc(label) + "</span>" + (selected ? '<span class="candidate-badge">用于汇总/回归</span>' : "") + "</span>";
    }).join("") + "</div>";
  }

  function renderDetails(data) {
    const xKey = data.x_field || $("xField").value, yKey = data.y_field || $("yField").value;
    $("xColumn").textContent = "X：" + (data.x_label || fieldLabel(xKey));
    $("yColumn").textContent = "Y：" + (data.y_label || fieldLabel(yKey));
    const points = data.points || [];
    const plottable = points.filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    $("detailsBody").innerHTML = points.length ? points.map((point) => {
      const xSources = point.x?.source_record_ids ?? point.x_source_record_ids ?? point.source_record_ids?.x;
      const ySources = point.y?.source_record_ids ?? point.y_source_record_ids ?? point.source_record_ids?.y;
      const plotIdx = plottable.indexOf(point);
      return '<tr' + (plotIdx >= 0 ? ' data-idx="' + plotIdx + '"' : "") + "><td>" + esc(pointCatalystName(point)) + "</td><td>" + esc(point.paper?.paper_code ?? point.paper_code ?? "—") + '</td><td class="col-id"><code>' + esc(point.catalyst_sample_id || "—") + "</code></td><td>" + esc(display(axisValue(point, "x"))) + "</td><td>" + esc(display(axisValue(point, "y"))) + '</td><td class="col-id">X: ' + ids(xSources) + "<br>Y: " + ids(ySources) + "</td><td>" + candidates(point, data) + "</td></tr>";
    }).join("") : '<tr><td colspan="7">没有可显示的同一催化剂样本。</td></tr>';
  }

  async function loadCorrelation() {
    const xField = $("xField").value, yField = $("yField").value;
    const minN = Math.max(3, Number.parseInt($("minN").value, 10) || 3);
    $("minN").value = String(minN);
    const requestId = ++state.requestId;
    $("relationStatus").textContent = "正在读取同一催化剂样本的关系数据…";
    $("retryCorrelation").hidden = true;
    document.querySelector(".plot-wrap").classList.add("is-loading");
    $("plotEmpty").textContent = "正在加载…";
    $("plotEmpty").style.display = "";
    try {
      const params = visualParams(new URLSearchParams({ x_field: xField, y_field: yField, min_n: String(minN) }));
      const data = await getJSON("/api/visuals/catalyst-correlation?" + params.toString());
      if (requestId !== state.requestId) return;
      state.lastCorrelation = data;
      renderStatistics(data);
      renderVerdict(data);
      renderDiagnostics(data);
      plot(data);
      renderDetails(data);
      $("relationStatus").textContent = "已加载 " + (data.n_catalysts ?? data.catalyst_count ?? (data.points || []).length) + " 个同一催化剂样本。";
    } catch (error) {
      if (requestId !== state.requestId) return;
      state.lastCorrelation = null;
      $("relationStatus").textContent = "关系数据读取失败：" + error.message;
      $("retryCorrelation").hidden = false;
      renderStatistics({});
      renderVerdict({ ready: false, points: [] });
      renderDiagnostics({});
      plot({ insufficient_reason: "关系数据读取失败，未绘制拟合线。" });
      renderDetails({ points: [] });
    } finally {
      if (requestId === state.requestId) document.querySelector(".plot-wrap").classList.remove("is-loading");
    }
  }

  async function init() {
    TopNav.init({ currentPage: "visuals", mountId: "topnav-mount" });
    ["xField", "yField", "minN"].forEach((id) => $(id).addEventListener("change", loadCorrelation));
    $("exportCatalystCsv").addEventListener("click", () => downloadDataset("csv"));
    $("downloadCatalystJson").addEventListener("click", () => downloadDataset("json"));
    $("retryCorrelation").addEventListener("click", loadCorrelation);
    document.querySelector(".plot-wrap").addEventListener("click", hideTooltip);
    $("quickChargeBond").addEventListener("click", () => {
      if (state.fields.some((field) => field.key === DEFAULTS.quickX) && state.fields.some((field) => field.key === DEFAULTS.quickY)) {
        $("xField").value = DEFAULTS.quickX;
        $("yField").value = DEFAULTS.quickY;
        loadCorrelation();
      } else {
        $("relationStatus").textContent = "当前接口未提供该快捷关系所需的数值字段。";
      }
    });
    await resolveLibraryScope();
    loadOverview();
    try {
      await loadFields();
      await loadCorrelation();
    } catch (error) {
      $("relationStatus").textContent = "可分析字段读取失败：" + error.message;
    }
  }
  document.addEventListener("DOMContentLoaded", init);
}());
