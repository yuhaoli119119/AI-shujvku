(function () {
  "use strict";

  const DEFAULTS = {
    x: "li2s_adsorption_energy",
    y: "li2s_dissociation_barrier",
    quickX: "li2s_bader_charge_transfer",
    quickY: "li_s_max_bond_length",
  };
  const state = { fields: [], requestId: 0 };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const number = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
  const display = (value) => number(value) === null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("请求失败（" + response.status + "）");
    return response.json();
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
      const data = await getJSON("/api/visuals/overview");
      setOverview(data.summary || data.overview || {});
      $("overviewStatus").textContent = "概览已更新";
    } catch (error) {
      $("overviewStatus").textContent = "概览读取失败：" + error.message;
    }
  }

  function normaliseFields(payload) {
    const raw = Array.isArray(payload) ? payload : (payload.fields || payload.analysis_fields || []);
    return raw.filter((field) => field && field.key && field.numeric !== false && field.analysis_enabled !== false)
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
    const payload = await getJSON("/api/visuals/analysis-fields");
    state.fields = normaliseFields(payload);
    if (!state.fields.length) throw new Error("接口未返回可用于数值相关分析的字段");
    fillSelect($("xField"), state.fields, DEFAULTS.x);
    fillSelect($("yField"), state.fields, DEFAULTS.y);
  }

  function listItems(id, items) {
    const target = $(id);
    target.innerHTML = (items || []).map((item) => "<li>" + esc(typeof item === "string" ? item : (item.reason || item.message || JSON.stringify(item))) + "</li>").join("");
  }

  function renderDiagnostics(data) {
    const warnings = data.warnings || [];
    const excluded = data.excluded_reasons || data.excluded || [];
    $("diagnostics").hidden = !(warnings.length || excluded.length);
    $("warningsBox").hidden = !warnings.length;
    $("excludedBox").hidden = !excluded.length;
    listItems("warningsList", warnings);
    listItems("excludedList", excluded);
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
      statCatalysts: data.catalyst_count,
      statPapers: data.paper_count,
    };
    Object.entries(stats).forEach(([id, value]) => { $(id).textContent = display(value); });
  }

  function plot(data) {
    const points = (data.points || []).filter((point) => number(point.x_value ?? point.x) !== null && number(point.y_value ?? point.y) !== null);
    const layer = $("plotLayer");
    const empty = $("plotEmpty");
    layer.innerHTML = "";
    if (!points.length) {
      empty.textContent = data.insufficient_reason || data.fit?.reason || "没有可绘制的同一催化剂样本";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    const xValues = points.map((point) => number(point.x_value ?? point.x));
    const yValues = points.map((point) => number(point.y_value ?? point.y));
    const xMin = Math.min(...xValues), xMax = Math.max(...xValues), yMin = Math.min(...yValues), yMax = Math.max(...yValues);
    const xSpan = xMax - xMin || 1, ySpan = yMax - yMin || 1;
    const sx = (x) => 58 + ((x - xMin) / xSpan) * 548;
    const sy = (y) => 334 - ((y - yMin) / ySpan) * 278;
    layer.insertAdjacentHTML("beforeend", '<line class="plot-axis" x1="58" y1="334" x2="606" y2="334"/><line class="plot-axis" x1="58" y1="42" x2="58" y2="334"/>');
    for (let tick = 1; tick < 4; tick += 1) {
      const x = 58 + tick * (548 / 4), y = 334 - tick * (278 / 4);
      layer.insertAdjacentHTML("beforeend", '<line class="plot-grid" x1="' + x + '" y1="42" x2="' + x + '" y2="334"/><line class="plot-grid" x1="58" y1="' + y + '" x2="606" y2="' + y + '"/>');
    }
    const fit = data.fit || {};
    const line = fit.line || data.fit_line;
    if (fit.ready === true && Array.isArray(line) && line.length >= 2) {
      const a = line[0], b = line[1];
      if (number(a.x) !== null && number(a.y) !== null && number(b.x) !== null && number(b.y) !== null) {
        layer.insertAdjacentHTML("beforeend", '<line class="fit-line" x1="' + sx(number(a.x)) + '" y1="' + sy(number(a.y)) + '" x2="' + sx(number(b.x)) + '" y2="' + sy(number(b.y)) + '"/>');
      }
    }
    points.forEach((point) => {
      const x = number(point.x_value ?? point.x), y = number(point.y_value ?? point.y);
      layer.insertAdjacentHTML("beforeend", '<circle class="plot-point" cx="' + sx(x) + '" cy="' + sy(y) + '" r="6"><title>' + esc((point.catalyst_name || point.catalyst || "催化剂") + " · " + (point.catalyst_sample_id || "")) + '</title></circle>');
    });
    const fitReady = fit.ready === true && Array.isArray(line) && line.length >= 2;
    $("fitNotice").textContent = fitReady ? "拟合线由接口返回的回归结果绘制。" : (data.insufficient_reason || fit.reason || "样本量不足或数据不可比，未绘制拟合线。");
  }

  function ids(value) {
    const list = Array.isArray(value) ? value : (value ? [value] : []);
    return list.length ? list.map((item) => "<code>" + esc(item) + "</code>").join("、") : "—";
  }

  function candidates(point) {
    const list = point.barrier_candidates || point.li2s_barrier_candidates || [];
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
      const sourceIds = point.source_record_ids || { x: point.x_source_record_ids, y: point.y_source_record_ids };
      return "<tr><td>" + esc(point.catalyst_name || point.catalyst || "—") + "</td><td>" + esc(point.paper_code || "—") + "</td><td><code>" + esc(point.catalyst_sample_id || "—") + "</code></td><td>" + esc(display(point.x_value ?? point.x)) + "</td><td>" + esc(display(point.y_value ?? point.y)) + "</td><td>X: " + ids(sourceIds.x || sourceIds.x_ids) + "<br>Y: " + ids(sourceIds.y || sourceIds.y_ids) + "</td><td>" + candidates(point) + "</td></tr>";
    }).join("") : '<tr><td colspan="7">没有可显示的同一催化剂样本。</td></tr>';
  }

  async function loadCorrelation() {
    const xField = $("xField").value, yField = $("yField").value;
    const minN = Math.max(2, Number.parseInt($("minN").value, 10) || 3);
    $("minN").value = String(minN);
    const requestId = ++state.requestId;
    $("relationStatus").textContent = "正在读取同一催化剂样本的关系数据…";
    try {
      const params = new URLSearchParams({ x_field: xField, y_field: yField, min_n: String(minN) });
      const data = await getJSON("/api/visuals/catalyst-correlation?" + params.toString());
      if (requestId !== state.requestId) return;
      renderStatistics(data);
      renderDiagnostics(data);
      plot(data);
      renderDetails(data);
      $("relationStatus").textContent = "已加载 " + (data.catalyst_count ?? (data.points || []).length) + " 个同一催化剂样本。";
    } catch (error) {
      if (requestId !== state.requestId) return;
      $("relationStatus").textContent = "关系数据读取失败：" + error.message;
      plot({ insufficient_reason: "关系数据读取失败，未绘制拟合线。" });
      renderDetails({ points: [] });
    }
  }

  async function init() {
    TopNav.init({ currentPage: "visuals", mountId: "topnav-mount" });
    loadOverview();
    try {
      await loadFields();
      await loadCorrelation();
    } catch (error) {
      $("relationStatus").textContent = "可分析字段读取失败：" + error.message;
    }
    ["xField", "yField", "minN"].forEach((id) => $(id).addEventListener("change", loadCorrelation));
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
