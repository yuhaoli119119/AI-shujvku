(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MLCharts = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const FONT = "'Aptos', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif";
  let gradientSequence = 0;
  const COLORS = Object.freeze({
    ink: "#12203f",
    muted: "#68758d",
    grid: "#dfe5f1",
    blue: "#2864f0",
    purple: "#8b55ed",
    red: "#f04d68",
    pale: "#f5f7fc",
    white: "#ffffff",
  });

  function el(name, attrs = {}, textContent) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs).forEach(([key, value]) => {
      if (value !== undefined && value !== null) node.setAttribute(key, String(value));
    });
    if (textContent !== undefined) node.textContent = String(textContent);
    return node;
  }

  function clear(svg, width = 920, height = 520) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.setAttribute("tabindex", "0");
    svg.setAttribute("focusable", "true");
    svg.style.aspectRatio = `${width} / ${height}`;
  }

  function fmt(value, digits = 3) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    if (Math.abs(number) >= 100000) return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(number);
    if (Math.abs(number) >= 100) return number.toFixed(Math.min(1, digits));
    return number.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
  }

  function niceMax(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    const scaled = value / power;
    const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return nice * power;
  }

  function colorMix(value) {
    const t = Math.max(0, Math.min(1, Number(value) || 0));
    const hue = 225 + 115 * t;
    const saturation = 76 + 8 * Math.sin(Math.PI * t);
    const lightness = 57 - 3 * Math.sin(Math.PI * t);
    return `hsl(${hue.toFixed(1)} ${saturation.toFixed(1)}% ${lightness.toFixed(1)}%)`;
  }

  function addBase(svg, width, height, title) {
    const titleNode = el("title", {}, title || "交互式数据图");
    svg.appendChild(titleNode);
    svg.appendChild(el("desc", {}, "图表只保留一个键盘焦点；具体数据可从同页点级溯源表和 CSV 访问。"));
    const defs = el("defs");
    const gradientId = `ml-chart-gradient-${++gradientSequence}`;
    svg._gradientUrl = `url(#${gradientId})`;
    const gradient = el("linearGradient", { id: gradientId, x1: "0", x2: "1", y1: "0", y2: "0" });
    gradient.appendChild(el("stop", { offset: "0%", "stop-color": COLORS.blue }));
    gradient.appendChild(el("stop", { offset: "53%", "stop-color": COLORS.purple }));
    gradient.appendChild(el("stop", { offset: "100%", "stop-color": COLORS.red }));
    defs.appendChild(gradient);
    svg.appendChild(defs);
    svg.appendChild(el("rect", { x: 0, y: 0, width, height, rx: 18, fill: COLORS.white }));
  }

  function tooltip() {
    let node = document.getElementById("chartTooltip");
    if (!node) {
      node = document.createElement("div");
      node.id = "chartTooltip";
      node.className = "chart-tooltip";
      node.setAttribute("role", "status");
      document.body.appendChild(node);
    }
    return node;
  }

  function showTooltip(event, title, rows) {
    const node = tooltip();
    const safe = String(title).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    node.innerHTML = `<strong>${safe}</strong>${(rows || []).map(([key, value]) => `<span><b>${String(key)}</b>${String(value)}</span>`).join("")}`;
    node.hidden = false;
    const margin = 16;
    const rect = node.getBoundingClientRect();
    let x = event.clientX + 14;
    let y = event.clientY + 14;
    if (x + rect.width + margin > window.innerWidth) x = event.clientX - rect.width - 14;
    if (y + rect.height + margin > window.innerHeight) y = event.clientY - rect.height - 14;
    node.style.left = `${Math.max(margin, x)}px`;
    node.style.top = `${Math.max(margin, y)}px`;
  }

  function hideTooltip() {
    const node = tooltip();
    node.hidden = true;
  }

  function bindTooltip(node, title, rows) {
    node.style.cursor = "pointer";
    node.addEventListener("pointerenter", (event) => showTooltip(event, title, rows));
    node.addEventListener("pointermove", (event) => showTooltip(event, title, rows));
    node.addEventListener("pointerleave", hideTooltip);
    node.addEventListener("focus", (event) => {
      const box = event.target.getBoundingClientRect();
      showTooltip({ clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 }, title, rows);
    });
    node.addEventListener("blur", hideTooltip);
  }

  function renderHorizontalBars(svg, items, options = {}) {
    const width = 920;
    const rowHeight = options.rowHeight || 48;
    const margin = { top: 58, right: 72, bottom: 34, left: options.left || 190 };
    const height = margin.top + margin.bottom + Math.max(1, items.length) * rowHeight;
    clear(svg, width, height);
    addBase(svg, width, height, options.title);
    const max = niceMax(Math.max(1, ...items.map((item) => Number(item.value) || 0)));
    const plotW = width - margin.left - margin.right;
    svg.appendChild(el("text", { x: margin.left, y: 28, fill: COLORS.ink, "font-size": 15, "font-weight": 700, "font-family": FONT }, options.title || ""));
    svg.appendChild(el("line", { x1: margin.left, x2: width - margin.right, y1: margin.top - 8, y2: margin.top - 8, stroke: COLORS.grid }));
    items.forEach((item, index) => {
      const y = margin.top + index * rowHeight;
      const barW = (Number(item.value) / max) * plotW;
      const row = el("g", { role: "listitem" });
      row.appendChild(el("rect", { x: 0, y: y - 14, width, height: rowHeight - 2, fill: index % 2 ? "#fbfcff" : COLORS.white }));
      row.appendChild(el("text", { x: margin.left - 16, y: y + 6, "text-anchor": "end", fill: COLORS.ink, "font-size": 13, "font-family": FONT }, item.label));
      row.appendChild(el("rect", { x: margin.left, y: y - 8, width: Math.max(2, barW), height: 20, rx: 10, fill: item.color || svg._gradientUrl }));
      row.appendChild(el("text", { x: margin.left + Math.max(barW + 12, 16), y: y + 7, fill: COLORS.muted, "font-size": 12.5, "font-family": FONT }, item.display || fmt(item.value, 0)));
      bindTooltip(row, item.label, item.tooltip || [["数值", item.display || fmt(item.value, 0)]]);
      svg.appendChild(row);
    });
    svg.dataset.chartId = options.id || "horizontal-bars";
    svg._chartRows = items.map((item) => ({ label: item.label, value: item.value, ...(item.export || {}) }));
    return { width, height, rows: svg._chartRows };
  }

  function renderGroupedBars(svg, categories, series, options = {}) {
    const width = 920;
    const height = 500;
    const margin = { top: 70, right: 32, bottom: 128, left: 66 };
    clear(svg, width, height);
    addBase(svg, width, height, options.title);
    const max = niceMax(Math.max(1, ...series.flatMap((entry) => entry.values.map(Number))));
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const groupW = plotW / Math.max(1, categories.length);
    const barW = Math.min(26, (groupW - 18) / Math.max(1, series.length));
    svg.appendChild(el("text", { x: margin.left, y: 28, fill: COLORS.ink, "font-size": 15, "font-weight": 700, "font-family": FONT }, options.title || ""));
    [0, 0.25, 0.5, 0.75, 1].forEach((tick) => {
      const x = margin.left + tick * plotW;
      svg.appendChild(el("line", { x1: x, x2: x, y1: margin.top, y2: margin.top + plotH, stroke: COLORS.grid }));
      svg.appendChild(el("text", { x, y: margin.top - 12, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11, "font-family": FONT }, fmt(max * tick, 0)));
    });
    categories.forEach((category, index) => {
      const groupX = margin.left + index * groupW;
      series.forEach((entry, seriesIndex) => {
        const value = Number(entry.values[index]) || 0;
        const barH = (value / max) * plotH;
        const x = groupX + groupW / 2 - (barW * series.length) / 2 + seriesIndex * barW;
        const rect = el("rect", { x, y: margin.top + plotH - barH, width: barW - 2, height: Math.max(1, barH), rx: 5, fill: entry.color });
        const point = el("g");
        point.appendChild(rect);
        point.appendChild(el("title", {}, `${category} · ${entry.name}: ${fmt(value, 0)}`));
        bindTooltip(point, category, [[entry.name, fmt(value, 0)]]);
        svg.appendChild(point);
      });
      const label = el("text", { x: groupX + groupW / 2, y: margin.top + plotH + 22, "text-anchor": "end", fill: COLORS.ink, "font-size": 11.5, "font-family": FONT, transform: `rotate(-28 ${groupX + groupW / 2} ${margin.top + plotH + 22})` }, category);
      svg.appendChild(label);
    });
    series.forEach((entry, index) => {
      const x = margin.left + index * 148;
      svg.appendChild(el("circle", { cx: x, cy: height - 26, r: 5, fill: entry.color }));
      svg.appendChild(el("text", { x: x + 10, y: height - 22, fill: COLORS.muted, "font-size": 11.5, "font-family": FONT }, entry.name));
    });
    svg.dataset.chartId = options.id || "grouped-bars";
    svg._chartRows = categories.map((category, index) => {
      const row = { 类别: category };
      series.forEach((entry) => { row[entry.name] = entry.values[index]; });
      return row;
    });
    return { width, height, rows: svg._chartRows };
  }

  function histogram(values, count = 14) {
    if (!values.length) return [];
    const min = Math.min(...values);
    const max = Math.max(...values);
    if (min === max) return [{ x0: min - 0.5, x1: max + 0.5, mid: min, values: values.slice(), count: values.length }];
    const step = (max - min) / count;
    const bins = Array.from({ length: count }, (_, index) => ({ x0: min + index * step, x1: min + (index + 1) * step, mid: min + (index + 0.5) * step, values: [] }));
    values.forEach((value) => {
      const index = Math.min(count - 1, Math.max(0, Math.floor((value - min) / step)));
      bins[index].values.push(value);
    });
    bins.forEach((bin) => { bin.count = bin.values.length; });
    return bins;
  }

  function renderHistogram(svg, values, options = {}) {
    const width = 920;
    const height = 440;
    const margin = { top: 66, right: 30, bottom: 62, left: 62 };
    clear(svg, width, height);
    addBase(svg, width, height, options.title);
    const bins = histogram(values, options.bins || 14);
    const max = niceMax(Math.max(1, ...bins.map((bin) => bin.count)));
    const minX = bins.length ? bins[0].x0 : 0;
    const maxX = bins.length ? bins[bins.length - 1].x1 : 1;
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const xScale = (value) => margin.left + ((value - minX) / Math.max(1e-9, maxX - minX)) * plotW;
    svg.appendChild(el("text", { x: margin.left, y: 28, fill: COLORS.ink, "font-size": 15, "font-weight": 700, "font-family": FONT }, options.title || "分布"));
    for (let tick = 0; tick <= 4; tick += 1) {
      const y = margin.top + plotH - (tick / 4) * plotH;
      svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, stroke: COLORS.grid }));
      svg.appendChild(el("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", fill: COLORS.muted, "font-size": 11, "font-family": FONT }, fmt((max * tick) / 4, 0)));
    }
    bins.forEach((bin) => {
      const x = xScale(bin.x0);
      const w = Math.max(1, xScale(bin.x1) - x - 2);
      const h = (bin.count / max) * plotH;
      const group = el("g");
      group.appendChild(el("rect", { x, y: margin.top + plotH - h, width: w, height: Math.max(1, h), rx: 5, fill: options.color || svg._gradientUrl, opacity: 0.92 }));
      bindTooltip(group, options.tooltipTitle || "分布区间", [["区间", `${fmt(bin.x0)} 至 ${fmt(bin.x1)} ${options.unit || ""}`], ["数量", fmt(bin.count, 0)]]);
      svg.appendChild(group);
    });
    for (let tick = 0; tick <= 5; tick += 1) {
      const value = minX + ((maxX - minX) * tick) / 5;
      const x = xScale(value);
      svg.appendChild(el("text", { x, y: margin.top + plotH + 24, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11, "font-family": FONT }, fmt(value)));
    }
    svg.appendChild(el("text", { x: margin.left + plotW / 2, y: height - 16, "text-anchor": "middle", fill: COLORS.muted, "font-size": 12, "font-family": FONT }, options.xLabel || "数值"));
    svg.dataset.chartId = options.id || "histogram";
    svg._chartRows = options.exportRows || bins.map((bin) => ({ 区间起点: bin.x0, 区间终点: bin.x1, 数量: bin.count, 单位: options.unit || "" }));
    return { width, height, rows: svg._chartRows };
  }

  function rawType(points) {
    const values = points.map((point) => point[0]).filter((value) => value !== "缺失" && value !== "");
    const numeric = values.filter((value) => Number.isFinite(Number(value)));
    return numeric.length >= Math.max(2, values.length * 0.7) ? "numeric" : "categorical";
  }

  function featureColor(point, points, type) {
    const raw = point[0];
    if (type === "numeric" && Number.isFinite(Number(raw))) {
      const values = points.map((item) => Number(item[0])).filter(Number.isFinite);
      const min = Math.min(...values);
      const max = Math.max(...values);
      return colorMix(max === min ? 0.5 : (Number(raw) - min) / (max - min));
    }
    const categories = Array.from(new Set(points.map((item) => String(item[0])))).sort();
    const index = Math.max(0, categories.indexOf(String(raw)));
    return colorMix(categories.length <= 1 ? 0.5 : index / (categories.length - 1));
  }

  function renderBeeswarm(svg, features, options = {}) {
    const width = 1000;
    const top = 72;
    const rowH = 38;
    const bottom = 58;
    const height = top + bottom + features.length * rowH;
    const margin = { left: 248, right: 42 };
    clear(svg, width, height);
    addBase(svg, width, height, options.title);
    const shapValues = features.flatMap((feature) => feature.points.map((point) => Number(point[1]))).filter(Number.isFinite);
    const limit = niceMax(Math.max(0.01, ...shapValues.map(Math.abs)));
    const plotW = width - margin.left - margin.right;
    const xScale = (value) => margin.left + ((value + limit) / (limit * 2)) * plotW;
    const zeroX = xScale(0);
    svg.appendChild(el("text", { x: margin.left, y: 30, fill: COLORS.ink, "font-size": 15, "font-weight": 700, "font-family": FONT }, options.title || "SHAP 蜂群"));
    svg.appendChild(el("line", { x1: zeroX, x2: zeroX, y1: 56, y2: height - bottom + 4, stroke: "#9aa6ba", "stroke-width": 1.2, "stroke-dasharray": "4 5" }));
    [-limit, -limit / 2, 0, limit / 2, limit].forEach((tick) => {
      const x = xScale(tick);
      svg.appendChild(el("line", { x1: x, x2: x, y1: 56, y2: height - bottom + 4, stroke: COLORS.grid, opacity: tick === 0 ? 0 : 0.55 }));
      svg.appendChild(el("text", { x, y: height - 24, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11, "font-family": FONT }, fmt(tick)));
    });
    svg.appendChild(el("text", { x: margin.left + plotW / 2, y: height - 6, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11.5, "font-family": FONT }, "SHAP 值（对模型输出的 eV 加性贡献）"));
    features.forEach((feature, rowIndex) => {
      const type = rawType(feature.points);
      const rowY = top + rowIndex * rowH + 8;
      if (rowIndex % 2 === 0) svg.appendChild(el("rect", { x: 18, y: rowY - 16, width: width - 36, height: rowH, rx: 8, fill: "#f8faff" }));
      const label = el("g");
      label.appendChild(el("text", { x: margin.left - 18, y: rowY + 4, "text-anchor": "end", fill: COLORS.ink, "font-size": 12.5, "font-weight": 600, "font-family": FONT }, `${feature.localRank || feature.rank}. ${feature.label}`));
      label.appendChild(el("text", { x: margin.left - 18, y: rowY + 19, "text-anchor": "end", fill: COLORS.muted, "font-size": 10, "font-family": FONT }, `全局第 ${feature.globalRank || feature.rank} · ${feature.stability === "U" ? "跨折不稳定" : "跨折较稳定"}`));
      label.addEventListener("click", () => options.onFeatureSelect?.(feature));
      label.style.cursor = "pointer";
      svg.appendChild(label);
      const buckets = new Map();
      feature.points.forEach((point) => {
        const value = Number(point[1]);
        if (!Number.isFinite(value)) return;
        const bucket = Math.round(xScale(value) / 3.1);
        const stack = buckets.get(bucket) || 0;
        buckets.set(bucket, stack + 1);
        const side = stack === 0 ? 0 : (stack % 2 ? 1 : -1) * Math.ceil(stack / 2) * 3.2;
        const circle = el("circle", {
          cx: xScale(value),
          cy: rowY + side,
          r: 2.85,
          fill: featureColor(point, feature.points, type),
          stroke: "#ffffff",
          "stroke-width": 0.45,
          opacity: 0.82,
        });
        bindTooltip(circle, feature.label, [
          ["原始特征值", String(point[0])],
          ["SHAP", fmt(point[1], 4)],
          ["目标", options.targetLabel?.(point[2]) || point[2]],
          ["外折", point[3]],
          ["记录", point[4] ? `${point[4].slice(0, 12)}…` : "—"],
          ["文献", point[6] || point[5] || "—"],
          ["来源组", point[7] || "—"],
          ["库域", point[8] || "—"],
        ]);
        circle.addEventListener("click", () => options.onFeatureSelect?.(feature));
        svg.appendChild(circle);
      });
    });
    svg.dataset.chartId = options.id || "shap-beeswarm";
    svg._chartRows = features.flatMap((feature) => feature.points.map((point) => ({
      特征: feature.id,
      特征名称: feature.label,
      原始值: point[0],
      SHAP: point[1],
      target_id: point[2],
      外折: point[3],
      record_id: point[4] || "",
      paper_id: point[5] || "",
      paper_code: point[6] || "",
      source_group_id: point[7] || "",
      data_domain: point[8] || "",
      material_descriptor_group_id: point[9] || "",
    })));
    return { width, height, rows: svg._chartRows };
  }

  function renderScatter(svg, points, options = {}) {
    const width = 920;
    const height = 560;
    const margin = { top: 66, right: 38, bottom: 66, left: 70 };
    clear(svg, width, height);
    addBase(svg, width, height, options.title);
    const xs = points.map((point) => Number(point[1])).filter(Number.isFinite);
    const ys = points.map((point) => Number(point[2])).filter(Number.isFinite);
    const min = Math.min(...xs, ...ys);
    const max = Math.max(...xs, ...ys);
    const pad = Math.max(0.2, (max - min) * 0.08);
    const x0 = min - pad;
    const x1 = max + pad;
    const y0 = min - pad;
    const y1 = max + pad;
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const xScale = (value) => margin.left + ((value - x0) / (x1 - x0)) * plotW;
    const yScale = (value) => margin.top + plotH - ((value - y0) / (y1 - y0)) * plotH;
    svg.appendChild(el("text", { x: margin.left, y: 28, fill: COLORS.ink, "font-size": 15, "font-weight": 700, "font-family": FONT }, options.title || "观测值与 OOF 预测"));
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = y0 + ((y1 - y0) * tick) / 4;
      const y = yScale(value);
      svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, stroke: COLORS.grid }));
      svg.appendChild(el("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", fill: COLORS.muted, "font-size": 11, "font-family": FONT }, fmt(value)));
      const x = xScale(value);
      svg.appendChild(el("text", { x, y: margin.top + plotH + 24, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11, "font-family": FONT }, fmt(value)));
    }
    svg.appendChild(el("line", { x1: xScale(x0), y1: yScale(x0), x2: xScale(x1), y2: yScale(x1), stroke: "#929db2", "stroke-width": 1.3, "stroke-dasharray": "6 5" }));
    points.forEach((point) => {
      if (!Number.isFinite(Number(point[1])) || !Number.isFinite(Number(point[2]))) return;
      const circle = el("circle", {
        cx: xScale(Number(point[1])),
        cy: yScale(Number(point[2])),
        r: point[6] ? 4.7 : 3.5,
        fill: point[6] ? COLORS.red : svg._gradientUrl,
        opacity: point[6] ? 0.9 : 0.64,
        stroke: point[6] ? "#ffffff" : "none",
        "stroke-width": 0.8,
      });
      bindTooltip(circle, point[6] ? "数值域外 · OOF 记录" : "数值域内 · OOF 记录", [
        ["观测值", fmt(point[1], 4)],
        ["预测值", fmt(point[2], 4)],
        ["95% 区间", `${fmt(point[3])}–${fmt(point[4])}`],
        ["残差", fmt(point[5], 4)],
        ["来源组", point[7] || "—"],
        ["文献", point[8] || "—"],
        ["库域", point[9] || "—"],
        ["外折", point[10] ?? "—"],
      ]);
      svg.appendChild(circle);
    });
    svg.appendChild(el("text", { x: margin.left + plotW / 2, y: height - 18, "text-anchor": "middle", fill: COLORS.muted, "font-size": 12, "font-family": FONT }, "观测值 / eV"));
    svg.appendChild(el("text", { x: 18, y: margin.top + plotH / 2, transform: `rotate(-90 18 ${margin.top + plotH / 2})`, "text-anchor": "middle", fill: COLORS.muted, "font-size": 12, "font-family": FONT }, "OOF 预测值 / eV"));
    svg.dataset.chartId = options.id || "oof-scatter";
    svg._chartRows = points.map((point) => ({ record_id: point[0], 观测值: point[1], OOF预测值: point[2], 区间低: point[3], 区间高: point[4], 残差: point[5], 数值域外: point[6], source_group_id: point[7] || "", paper_codes: point[8] || "", data_domain: point[9] || "", outer_fold: point[10] ?? "" }));
    return { width, height, rows: svg._chartRows };
  }

  function renderDependency(svg, points, options = {}) {
    const width = 920;
    const height = 520;
    const margin = { top: 66, right: 34, bottom: 70, left: 72 };
    clear(svg, width, height);
    addBase(svg, width, height, options.title);
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const numeric = points.filter((point) => Number.isFinite(Number(point[0])));
    const isNumeric = numeric.length >= Math.max(2, points.length * 0.7);
    const rawValues = points.map((point) => point[0]);
    const categories = Array.from(new Set(rawValues.map(String))).sort((a, b) => a.localeCompare(b, "zh-CN"));
    const numericValues = numeric.map((point) => Number(point[0]));
    const minRaw = isNumeric ? Math.min(...numericValues) : 0;
    const maxRaw = isNumeric ? Math.max(...numericValues) : Math.max(1, categories.length - 1);
    const shapValues = points.map((point) => Number(point[1])).filter(Number.isFinite);
    const shapLimit = niceMax(Math.max(0.01, ...shapValues.map(Math.abs)));
    const xScale = (value) => margin.left + ((value - minRaw) / Math.max(1e-9, maxRaw - minRaw)) * plotW;
    const yScale = (value) => margin.top + plotH - ((value + shapLimit) / (shapLimit * 2)) * plotH;
    const targetPalette = [COLORS.blue, COLORS.purple, COLORS.red, "#22a88f", "#d58a18", "#5f6ed8"];
    const targets = Array.from(new Set(points.map((point) => point[2])));
    const targetColor = new Map(targets.map((target, index) => [target, targetPalette[index % targetPalette.length]]));
    svg.appendChild(el("text", { x: margin.left, y: 28, fill: COLORS.ink, "font-size": 15, "font-weight": 700, "font-family": FONT }, options.title || "SHAP 依赖图"));
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: yScale(0), y2: yScale(0), stroke: "#8e9bb0", "stroke-dasharray": "5 5" }));
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = -shapLimit + (shapLimit * 2 * tick) / 4;
      const y = yScale(value);
      svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, stroke: COLORS.grid, opacity: 0.72 }));
      svg.appendChild(el("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", fill: COLORS.muted, "font-size": 10.5, "font-family": FONT }, fmt(value)));
    }
    if (isNumeric) {
      for (let tick = 0; tick <= 5; tick += 1) {
        const value = minRaw + ((maxRaw - minRaw) * tick) / 5;
        svg.appendChild(el("text", { x: xScale(value), y: margin.top + plotH + 24, "text-anchor": "middle", fill: COLORS.muted, "font-size": 10.5, "font-family": FONT }, fmt(value)));
      }
    } else {
      const maxTicks = 7;
      const stride = Math.max(1, Math.ceil(categories.length / maxTicks));
      categories.forEach((category, index) => {
        if (index % stride !== 0 && index !== categories.length - 1) return;
        const x = margin.left + ((index / Math.max(1, categories.length - 1)) * plotW);
        svg.appendChild(el("text", { x, y: margin.top + plotH + 24, "text-anchor": "middle", fill: COLORS.muted, "font-size": 9.5, "font-family": FONT }, category.length > 12 ? `${category.slice(0, 11)}…` : category));
      });
    }
    const jitterStacks = new Map();
    points.forEach((point) => {
      const shapValue = Number(point[1]);
      if (!Number.isFinite(shapValue)) return;
      let x;
      if (isNumeric && Number.isFinite(Number(point[0]))) {
        x = xScale(Number(point[0]));
      } else {
        const categoryIndex = Math.max(0, categories.indexOf(String(point[0])));
        const baseX = margin.left + ((categoryIndex / Math.max(1, categories.length - 1)) * plotW);
        const key = `${categoryIndex}:${Math.round(yScale(shapValue) / 4)}`;
        const stack = jitterStacks.get(key) || 0;
        jitterStacks.set(key, stack + 1);
        x = baseX + (stack === 0 ? 0 : (stack % 2 ? 1 : -1) * Math.ceil(stack / 2) * 3.2);
      }
      const circle = el("circle", {
        cx: x,
        cy: yScale(shapValue),
        r: 3.2,
        fill: targetColor.get(point[2]) || svg._gradientUrl,
        opacity: 0.7,
        stroke: "#fff",
        "stroke-width": 0.45,
      });
      bindTooltip(circle, options.featureLabel || "特征依赖", [
        ["原始值", String(point[0])],
        ["SHAP", fmt(shapValue, 4)],
        ["目标", options.targetLabel?.(point[2]) || point[2]],
        ["外折", point[3]],
        ["记录", point[4] ? `${point[4].slice(0, 12)}…` : "—"],
        ["文献", point[6] || point[5] || "—"],
        ["来源组", point[7] || "—"],
        ["库域", point[8] || "—"],
      ]);
      svg.appendChild(circle);
    });
    svg.appendChild(el("text", { x: margin.left + plotW / 2, y: height - 18, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11.5, "font-family": FONT }, options.xLabel || "原始特征值"));
    svg.appendChild(el("text", { x: 16, y: margin.top + plotH / 2, transform: `rotate(-90 16 ${margin.top + plotH / 2})`, "text-anchor": "middle", fill: COLORS.muted, "font-size": 11.5, "font-family": FONT }, "SHAP 值"));
    targets.slice(0, 6).forEach((target, index) => {
      const x = margin.left + index * 125;
      svg.appendChild(el("circle", { cx: x, cy: height - 48, r: 4.5, fill: targetColor.get(target) }));
      svg.appendChild(el("text", { x: x + 8, y: height - 44, fill: COLORS.muted, "font-size": 9.5, "font-family": FONT }, options.targetLabel?.(target) || target));
    });
    svg.dataset.chartId = options.id || "shap-dependency";
    svg._chartRows = points.map((point) => ({
      特征: options.featureId || "",
      原始值: point[0],
      SHAP: point[1],
      target_id: point[2],
      外折: point[3],
      record_id: point[4] || "",
      paper_id: point[5] || "",
      paper_code: point[6] || "",
      source_group_id: point[7] || "",
      data_domain: point[8] || "",
      material_descriptor_group_id: point[9] || "",
    }));
    return { width, height, rows: svg._chartRows };
  }

  function csvEscape(value) {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function rowsToCsv(rows) {
    if (!rows || !rows.length) return "\ufeffno_data\n";
    const headers = Array.from(rows.reduce((set, row) => { Object.keys(row).forEach((key) => set.add(key)); return set; }, new Set()));
    return `\ufeff${headers.map(csvEscape).join(",")}\n${rows.map((row) => headers.map((header) => csvEscape(row[header])).join(",")).join("\n")}\n`;
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function downloadCsv(svg, filename) {
    downloadBlob(new Blob([rowsToCsv(svg._chartRows || [])], { type: "text/csv;charset=utf-8" }), filename.endsWith(".csv") ? filename : `${filename}.csv`);
  }

  function downloadRowsCsv(rows, filename) {
    downloadBlob(new Blob([rowsToCsv(rows || [])], { type: "text/csv;charset=utf-8" }), filename.endsWith(".csv") ? filename : `${filename}.csv`);
  }

  function cloneForExport(svg) {
    const clone = svg.cloneNode(true);
    const box = svg.viewBox.baseVal;
    clone.setAttribute("xmlns", NS);
    clone.setAttribute("width", box.width);
    clone.setAttribute("height", box.height);
    clone.querySelectorAll("text").forEach((node) => node.setAttribute("font-family", FONT));
    const background = el("rect", { x: 0, y: 0, width: box.width, height: box.height, fill: "#ffffff" });
    clone.insertBefore(background, clone.firstChild);
    return { clone, width: box.width, height: box.height };
  }

  function downloadSvg(svg, filename) {
    const { clone } = cloneForExport(svg);
    const content = new XMLSerializer().serializeToString(clone);
    downloadBlob(new Blob([content], { type: "image/svg+xml;charset=utf-8" }), filename.endsWith(".svg") ? filename : `${filename}.svg`);
  }

  async function downloadPng(svg, filename, scale = 3) {
    const { clone, width, height } = cloneForExport(svg);
    const serialized = new XMLSerializer().serializeToString(clone);
    const url = URL.createObjectURL(new Blob([serialized], { type: "image/svg+xml;charset=utf-8" }));
    try {
      const image = new Image();
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = () => reject(new Error("SVG 渲染失败"));
        image.src = url;
      });
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      const context = canvas.getContext("2d");
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("PNG 编码失败");
      downloadBlob(blob, filename.endsWith(".png") ? filename : `${filename}.png`);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  return Object.freeze({
    COLORS,
    clear,
    fmt,
    colorMix,
    renderHorizontalBars,
    renderGroupedBars,
    renderHistogram,
    renderBeeswarm,
    renderScatter,
    renderDependency,
    rowsToCsv,
    downloadCsv,
    downloadRowsCsv,
    downloadSvg,
    downloadPng,
    hideTooltip,
  });
});
