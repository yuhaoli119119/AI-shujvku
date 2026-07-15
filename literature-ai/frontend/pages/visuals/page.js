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
    try {
      const data = await getJSON("/api/visuals/overview?" + visualParams(new URLSearchParams()).toString());
      setOverview(data.summary || data.overview || {});
      $("overviewStatus").textContent = state.libraryName
        ? "当前文献库：" + state.libraryName
        : (state.libraryResolutionFailed ? "当前文献库读取失败，已显示全部文献库" : "全部文献库");
    } catch (error) {
      $("overviewStatus").textContent = "概览读取失败：" + error.message;
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
    $("diagnosticTitle").textContent = data.ready === true ? "当前数据可以进行拟合" : "当前数据不足，暂不进行拟合";
    $("diagnosticMessage").textContent = data.ready === true
      ? "已形成 " + catalystCount + " 个同一催化剂有效配对，来自 " + paperCount + " 篇论文。"
      : "当前只有 " + catalystCount + " 个同一催化剂有效配对，至少需要 " + minN + " 个才能计算拟合结果。";
    const highlights = [];
    if (paperCount < 2) highlights.push("有效数据来自不到 2 篇论文，当前结果只能用于数据核验。");
    const topReasons = excludedEntries
      .filter(([, count]) => Number.isFinite(Number(count)) && Number(count) > 0)
      .sort((left, right) => Number(right[1]) - Number(left[1]))
      .slice(0, 3);
    if (topReasons.length) {
      highlights.push("主要阻断项（原因次数可能重复）：" + topReasons.map(([reason, count]) => exclusionLabel(reason) + " " + count + " 次").join("；") + "。");
    }
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

  function axisValue(point, axis) {
    return point?.[axis]?.value ?? point?.[axis + "_value"] ?? null;
  }

  function pointCatalystName(point) {
    return point.catalyst_name || point.catalyst?.name || point.catalyst || "催化剂";
  }

  function plot(data) {
    const points = (data.points || []).filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    const layer = $("plotLayer");
    const empty = $("plotEmpty");
    layer.innerHTML = "";
    if (!points.length) {
      empty.textContent = data.insufficient_reason || data.fit?.reason || "没有可绘制的同一催化剂样本";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    const xValues = points.map((point) => number(axisValue(point, "x")));
    const yValues = points.map((point) => number(axisValue(point, "y")));
    const xMin = Math.min(...xValues), xMax = Math.max(...xValues), yMin = Math.min(...yValues), yMax = Math.max(...yValues);
    const xSpan = xMax - xMin || 1, ySpan = yMax - yMin || 1;
    const sx = (x) => 58 + ((x - xMin) / xSpan) * 548;
    const sy = (y) => 334 - ((y - yMin) / ySpan) * 278;
    layer.insertAdjacentHTML("beforeend", '<line class="plot-axis" x1="58" y1="334" x2="606" y2="334"/><line class="plot-axis" x1="58" y1="42" x2="58" y2="334"/>');
    for (let tick = 1; tick < 4; tick += 1) {
      const x = 58 + tick * (548 / 4), y = 334 - tick * (278 / 4);
      layer.insertAdjacentHTML("beforeend", '<line class="plot-grid" x1="' + x + '" y1="42" x2="' + x + '" y2="334"/><line class="plot-grid" x1="58" y1="' + y + '" x2="606" y2="' + y + '"/>');
    }
    const slope = number(data.statistics?.slope);
    const intercept = number(data.statistics?.intercept);
    const fitReady = data.ready === true && slope !== null && intercept !== null;
    if (fitReady) {
      layer.insertAdjacentHTML("beforeend", '<line class="fit-line" x1="' + sx(xMin) + '" y1="' + sy(slope * xMin + intercept) + '" x2="' + sx(xMax) + '" y2="' + sy(slope * xMax + intercept) + '"/>');
    }
    points.forEach((point) => {
      const x = number(axisValue(point, "x")), y = number(axisValue(point, "y"));
      layer.insertAdjacentHTML("beforeend", '<circle class="plot-point" cx="' + sx(x) + '" cy="' + sy(y) + '" r="6"><title>' + esc(pointCatalystName(point) + " · " + (point.catalyst_sample_id || "")) + '</title></circle>');
    });
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
      return '<span class="' + (selected ? "selected-candidate" : "") + '">' + esc(label) + (selected ? "（用于汇总/回归的最大可比能垒）" : "") + "</span>";
    }).join("") + "</div>";
  }

  function renderDetails(data) {
    const xKey = data.x_field || $("xField").value, yKey = data.y_field || $("yField").value;
    $("xColumn").textContent = "X：" + (data.x_label || fieldLabel(xKey));
    $("yColumn").textContent = "Y：" + (data.y_label || fieldLabel(yKey));
    const points = data.points || [];
    $("detailsBody").innerHTML = points.length ? points.map((point) => {
      const xSources = point.x?.source_record_ids ?? point.x_source_record_ids ?? point.source_record_ids?.x;
      const ySources = point.y?.source_record_ids ?? point.y_source_record_ids ?? point.source_record_ids?.y;
      return "<tr><td>" + esc(pointCatalystName(point)) + "</td><td>" + esc(point.paper?.paper_code ?? point.paper_code ?? "—") + "</td><td><code>" + esc(point.catalyst_sample_id || "—") + "</code></td><td>" + esc(display(axisValue(point, "x"))) + "</td><td>" + esc(display(axisValue(point, "y"))) + "</td><td>X: " + ids(xSources) + "<br>Y: " + ids(ySources) + "</td><td>" + candidates(point, data) + "</td></tr>";
    }).join("") : '<tr><td colspan="7">没有可显示的同一催化剂样本。</td></tr>';
  }

  async function loadCorrelation() {
    const xField = $("xField").value, yField = $("yField").value;
    const minN = Math.max(3, Number.parseInt($("minN").value, 10) || 3);
    $("minN").value = String(minN);
    const requestId = ++state.requestId;
    $("relationStatus").textContent = "正在读取同一催化剂样本的关系数据…";
    try {
      const params = visualParams(new URLSearchParams({ x_field: xField, y_field: yField, min_n: String(minN) }));
      const data = await getJSON("/api/visuals/catalyst-correlation?" + params.toString());
      if (requestId !== state.requestId) return;
      renderStatistics(data);
      renderDiagnostics(data);
      plot(data);
      renderDetails(data);
      $("relationStatus").textContent = "已加载 " + (data.n_catalysts ?? data.catalyst_count ?? (data.points || []).length) + " 个同一催化剂样本。";
    } catch (error) {
      if (requestId !== state.requestId) return;
      $("relationStatus").textContent = "关系数据读取失败：" + error.message;
      plot({ insufficient_reason: "关系数据读取失败，未绘制拟合线。" });
      renderDetails({ points: [] });
    }
  }

  async function init() {
    TopNav.init({ currentPage: "visuals", mountId: "topnav-mount" });
    await resolveLibraryScope();
    loadOverview();
    try {
      await loadFields();
      await loadCorrelation();
    } catch (error) {
      $("relationStatus").textContent = "可分析字段读取失败：" + error.message;
    }
    ["xField", "yField", "minN"].forEach((id) => $(id).addEventListener("change", loadCorrelation));
    $("exportCatalystCsv").addEventListener("click", () => downloadDataset("csv"));
    $("downloadCatalystJson").addEventListener("click", () => downloadDataset("json"));
    $("quickChargeBond").addEventListener("click", () => {
      if (state.fields.some((field) => field.key === DEFAULTS.quickX) && state.fields.some((field) => field.key === DEFAULTS.quickY)) {
        $("xField").value = DEFAULTS.quickX;
        $("yField").value = DEFAULTS.quickY;
        loadCorrelation();
      } else {
        $("relationStatus").textContent = "当前接口未提供该快捷关系所需的数值字段。";
      }
    });
  }
  document.addEventListener("DOMContentLoaded", init);
}());
