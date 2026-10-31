(function () {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const number = (value, digits = 0) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(Number(value) || 0);
  const safeText = (node, value) => { if (node) node.textContent = String(value ?? ""); };
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const VIEWS = new Set(["overview", "targets", "materials", "explanations", "diagnostics"]);
  const DOMAIN = "LITHIUM_SULFUR_MODEL";

  const state = {
    payload: null,
    runtimeManifest: null,
    adapter: null,
    view: "overview",
    materialScope: "audit_pool_720",
    featureSet: "F1",
    shapTarget: "ALL",
    dependencyFeature: null,
    targetSelection: "readiness:rds_delta_g_numeric",
    rdsSelection: "readiness:rds_delta_g_numeric",
    rdsLayer: "wide68",
    oofSelection: "oof:decomposition_barrier_li2s",
  };

  const TARGET_LABELS = {
    adsorption_li2s: "Li₂S 吸附",
    adsorption_li2s2: "Li₂S₂ 吸附",
    adsorption_li2s4: "Li₂S₄ 吸附",
    adsorption_li2s6: "Li₂S₆ 吸附",
    adsorption_li2s8: "Li₂S₈ 吸附",
    adsorption_s8: "S₈ 吸附",
    decomposition_barrier_li2s: "Li₂S 分解能垒",
  };

  function targetLabel(targetId) {
    return TARGET_LABELS[targetId] || targetId;
  }

  function setGlobalStatus(message, type = "") {
    const node = $("#globalStatus");
    node.textContent = message;
    node.classList.toggle("is-ready", type === "ready");
    node.classList.toggle("is-error", type === "error");
  }

  function showExportStatus(message, isError = false) {
    const node = $("#exportStatus");
    node.textContent = message;
    node.classList.add("is-visible");
    node.classList.toggle("is-error", isError);
    clearTimeout(showExportStatus.timer);
    showExportStatus.timer = setTimeout(() => node.classList.remove("is-visible"), 2600);
  }

  function setChartEmpty(svg, message) {
    MLCharts.clear(svg, 920, 380);
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", "460");
    text.setAttribute("y", "190");
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("fill", "#68758d");
    text.setAttribute("font-size", "14");
    text.textContent = message;
    group.appendChild(text);
    svg.appendChild(group);
    svg._chartRows = [];
  }

  function revealActiveTab() {
    requestAnimationFrame(() => {
      const rail = $(".view-tabs");
      const tab = $(".view-tab.is-active");
      if (!rail || !tab) return;
      const left = tab.offsetLeft;
      const right = left + tab.offsetWidth;
      const visibleLeft = rail.scrollLeft;
      const visibleRight = visibleLeft + rail.clientWidth;
      if (left < visibleLeft || right > visibleRight) {
        rail.scrollTo({ left: Math.max(0, left - (rail.clientWidth - tab.offsetWidth) / 2), behavior: "smooth" });
      }
    });
  }

  function showView(view, updateHash = true) {
    if (!VIEWS.has(view)) view = "overview";
    state.view = view;
    $$("[data-view-panel]").forEach((panel) => {
      const active = panel.dataset.viewPanel === view;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    $$("[data-view]").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    if (updateHash && location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    revealActiveTab();
    MLCharts.hideTooltip();
  }

  function wireNavigation() {
    $$("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
    $$("[data-jump]").forEach((button) => button.addEventListener("click", () => {
      showView(button.dataset.jump);
      $(".view-tabs").scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    window.addEventListener("hashchange", () => showView(location.hash.slice(1), false));
    document.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const active = $(".view-tab.is-active");
      if (!active || !active.contains(document.activeElement)) return;
      const tabs = $$(".view-tab");
      const current = tabs.indexOf(active);
      const next = event.key === "ArrowRight" ? (current + 1) % tabs.length : (current - 1 + tabs.length) % tabs.length;
      tabs[next].focus();
      showView(tabs[next].dataset.view);
    });
  }

  function updateManifestChrome() {
    const manifest = state.adapter.payloadManifest;
    const runtime = state.runtimeManifest;
    const accounting = new Map(manifest.sampleAccounting.map((item) => [item.label, item]));
    safeText($("#metricUnits"), number(accounting.get("统计单元").value));
    safeText($("#metricRows"), number(accounting.get("精确数据行").value));
    safeText($("#metricTrainRows"), number(accounting.get("六吸附开发样本").value));
    safeText($("#metricShapViews"), number(accounting.get("SHAP 解释次数").value));
    safeText($("#buildMeta"), `${manifest.repositoryHead.slice(0, 8)} · bundle ${runtime.bundle.sha256.slice(0, 10)}`);
    safeText($("#footerHash"), `bundle ${runtime.bundle.sha256} · ${number(runtime.bundle.bytes)} bytes · 客户端完整性校验，不构成权限授权`);
  }

  function renderOverview() {
    const library = state.adapter.overview();
    const items = library.roles.map((role) => ({
      label: role.label,
      value: role.uniqueValues,
      display: `${number(role.uniqueValues)} · ${number(role.papers)} 篇`,
      tooltip: [["统计单元", number(role.uniqueValues)], ["精确行", number(role.rows)], ["精确材料", number(role.exactMaterials)], ["文献", number(role.papers)]],
      export: { role: role.id, unit: "value_id", rows: role.rows, papers: role.papers },
    }));
    MLCharts.renderHorizontalBars($("#ledgerChart"), items, {
      id: "data-utilization-ledger-lithium-sulfur",
      title: `${library.label} · 互斥主用途`,
      rowHeight: 43,
      left: 174,
    });
    safeText($("#ledgerTitle"), `${library.label} · ${number(library.uniqueValues)} 个统计单元`);
    safeText($("#ledgerNote"), "每个 value_id 只进入一个主用途桶；运行时包不含其它文献库或跨库 ALL 行。");
    safeText($("#domainValueCount"), number(library.uniqueValues));
    safeText($("#domainValueLabel"), "锂硫单库 · 个 value_id");
    safeText($("#domainRowCount"), number(library.rows));
    safeText($("#domainMaterialCount"), number(library.exactMaterials));
    safeText($("#domainPaperCount"), `${number(library.papers)} 篇`);
    safeText($("#domainDataKinds"), `${number(library.dftValues)} / ${number(library.experimentalValues)}`);
  }

  function option(value, text) {
    const item = document.createElement("option");
    item.value = value;
    item.textContent = text;
    return item;
  }

  function rdsTargetItems() {
    return [
      ...state.adapter.currentTargets().filter((target) => target.sourceKey === "rds_delta_g_numeric").map((target) => ({ selection: `readiness:${target.sourceKey}`, item: target, current: true })),
      ...state.adapter.historicalRdsDefinitions().map((target) => ({ selection: `rds:${target.id}`, item: target, current: false })),
    ];
  }

  function populateTargetSelects() {
    const targetSelect = $("#targetSelect");
    targetSelect.replaceChildren();
    const currentGroup = document.createElement("optgroup");
    currentGroup.label = "当前 readiness · 无目标模型";
    state.adapter.currentTargets().forEach((target) => {
      const count = target.displayMode === "summary" ? `${target.lithiumRows}/${target.upstreamRows}` : `${target.n}${target.upstreamRows !== target.n ? ` / 上游 ${target.upstreamRows}` : ""}`;
      currentGroup.appendChild(option(`readiness:${target.sourceKey}`, `${target.label} · ${count} · ${target.status}`));
    });

    const oofGroup = document.createElement("optgroup");
    oofGroup.label = "历史 dummy OOF · 未重新训练";
    state.adapter.oofTargets().forEach((target) => {
      oofGroup.appendChild(option(target.id, `${target.label} · ${target.n} 条 · dummy 中位数`));
    });

    const barrierGroup = document.createElement("optgroup");
    barrierGroup.label = "历史字段覆盖 · 非当前路径定义";
    state.adapter.historicalBarrierDefinitions().forEach((target) => {
      barrierGroup.appendChild(option(`barrier:${target.id}`, `${target.label} · ${target.n} 条`));
    });

    const rdsHistoryGroup = document.createElement("optgroup");
    rdsHistoryGroup.label = "历史 RDS 族与字段 · 非当前目标";
    state.adapter.historicalRdsDefinitions().forEach((target) => {
      rdsHistoryGroup.appendChild(option(`rds:${target.id}`, `${target.label} · ${target.n}${target.n === 89 ? " / 主证据 72" : ""}`));
    });
    targetSelect.append(currentGroup, oofGroup, barrierGroup, rdsHistoryGroup);
    targetSelect.value = state.targetSelection;

    const oofSelect = $("#oofSelect");
    oofSelect.replaceChildren();
    state.adapter.oofTargets().forEach((target) => oofSelect.appendChild(option(target.id, `${target.label} · ${target.n} 条`)));
    oofSelect.value = state.oofSelection;

    const rdsSelect = $("#rdsSelect");
    rdsSelect.replaceChildren();
    const currentRdsGroup = document.createElement("optgroup");
    currentRdsGroup.label = "当前 RDS ΔG";
    const currentRds = state.adapter.currentTarget("readiness:rds_delta_g_numeric");
    currentRdsGroup.appendChild(option("readiness:rds_delta_g_numeric", `${currentRds.label} · 68 宽 / 64 部分 / 0 严格`));
    const historicalRdsGroup = document.createElement("optgroup");
    historicalRdsGroup.label = "历史 v2 RDS 覆盖";
    state.adapter.historicalRdsDefinitions().forEach((target) => {
      historicalRdsGroup.appendChild(option(`rds:${target.id}`, `${target.label} · ${target.n} 条`));
    });
    rdsSelect.append(currentRdsGroup, historicalRdsGroup);
    rdsSelect.value = state.rdsSelection;

    targetSelect.addEventListener("change", () => setTargetSelection(targetSelect.value));
    rdsSelect.addEventListener("change", () => setTargetSelection(rdsSelect.value));
    oofSelect.addEventListener("change", () => {
      state.oofSelection = oofSelect.value;
      renderDiagnostics();
    });
  }

  function setTargetSelection(value) {
    state.targetSelection = value;
    $("#targetSelect").value = value;
    if (value === "readiness:rds_delta_g_numeric" || value.startsWith("rds:")) {
      state.rdsSelection = value;
      $("#rdsSelect").value = value;
    }
    renderTarget();
  }

  function renderReadinessGrid() {
    const cards = state.adapter.currentTargets().map((target) => {
      let count;
      if (target.sourceKey === "rds_delta_g_numeric") count = "68 · 64 · 0";
      else if (target.displayMode === "summary") count = `${number(target.lithiumRows)} / ${number(target.upstreamRows)}`;
      else if (target.upstreamRows !== target.n) count = `${number(target.n)} / ${number(target.upstreamRows)}`;
      else count = number(target.n);
      const scopeLabel = target.excludedOtherDomainRows > 0 ? `上游总盘 ${number(target.upstreamRows)}，排除其它域 ${number(target.excludedOtherDomainRows)} · 无模型` : "无模型 · 无目标 SHAP";
      return `<button type="button" class="readiness-card" data-ready-target="${escapeHtml(target.sourceKey)}">
        <span>${escapeHtml(target.status)}</span><strong>${count}</strong>
        <b>${escapeHtml(target.label)}</b><small>${scopeLabel}</small>
      </button>`;
    });
    cards.push(`<div class="readiness-card readiness-card-static"><span>AUDITED</span><strong>0</strong><b>目标专属模型 / SHAP</b><small>barrier、RDS 与 readiness 目标均未建模</small></div>`);
    $("#readinessGrid").innerHTML = cards.join("");
    $$("[data-ready-target]").forEach((button) => button.addEventListener("click", () => setTargetSelection(`readiness:${button.dataset.readyTarget}`)));
  }

  function pointRowsTarget(points) {
    return points.map((point) => ({
      value_id: point.valueId || point.value_id || "",
      row_id: point.rowId || point.row_id || "",
      record_id: point.recordId || point.record_id || "",
      paper_id: point.paperId || point.paper_id || "",
      paper_code: point.paperCode || point.paper || point.paper_code || "",
      source_group_id: point.sourceGroupId || point.source_group_id || "",
      data_domain: point.dataDomain || point.domain || "",
      outer_fold: point.fold ?? point.outer_fold ?? "",
      target_family: point.targetFamily || "",
      value_eV: point.value ?? "",
      unit: point.unit || "eV",
      step_class: point.stepClass || "",
      readiness_status: point.readinessStatus || point.status || "",
      migration_action: point.migrationAction || "",
    }));
  }

  function renderPointTable(containerId, rows, columns) {
    const container = $(`#${containerId}`);
    if (!container) return;
    if (!rows.length) {
      container.innerHTML = '<p class="empty-caption">当前筛选没有数据成员。</p>';
      return;
    }
    const shown = rows.slice(0, 50);
    container._pointRows = rows.slice();
    container.innerHTML = `<div class="point-table-head"><p class="table-caption">显示 ${shown.length} / ${rows.length} 行</p><button type="button" class="button" data-point-export="${containerId}">下载全部点级 CSV</button></div><div class="table-scroll"><table><thead><tr>${columns.map(([key, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead><tbody>${shown.map((row) => `<tr>${columns.map(([key]) => `<td>${escapeHtml(row[key] ?? "—")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  const TARGET_POINT_COLUMNS = [
    ["value_id", "value_id"], ["record_id", "record_id"], ["paper_code", "文献"], ["source_group_id", "来源组"],
    ["data_domain", "库域"], ["outer_fold", "折"], ["target_family", "目标族"], ["value_eV", "数值 / eV"], ["readiness_status", "状态"],
  ];

  function evidenceFiltered(points, evidenceOnly) {
    if (!evidenceOnly) return points;
    return points.filter((point) => {
      if (typeof point.oldPrimary === "boolean") return point.oldPrimary;
      return ["PRIMARY_EXPLICIT", "Y_PRIMARY_EXPLICIT_TARGET"].includes(point.status);
    });
  }

  function renderTargetChart(points, options) {
    const values = points.map((point) => Number(point.value)).filter(Number.isFinite);
    if (!values.length) {
      setChartEmpty($("#targetChart"), options.emptyMessage || "当前筛选没有可作图数值");
      return 0;
    }
    MLCharts.renderHistogram($("#targetChart"), values, {
      id: options.id,
      title: options.title,
      unit: "eV",
      xLabel: `${options.label} / eV`,
      tooltipTitle: options.tooltipTitle || "真实记录分箱",
      exportRows: pointRowsTarget(points),
    });
    return values.length;
  }

  function renderCurrentTarget(target) {
    const layerControl = $("#targetLayerControl");
    layerControl.hidden = target.sourceKey !== "rds_delta_g_numeric";
    $("#evidenceControlWrap").hidden = true;
    if (!layerControl.hidden) {
      $$("[data-rds-layer]", layerControl).forEach((button) => button.classList.toggle("is-active", button.dataset.rdsLayer === state.rdsLayer));
    }
    let points = target.points || [];
    let layerClaim = "";
    if (target.sourceKey === "rds_delta_g_numeric") {
      const layer = target.layers.find((item) => item.id === state.rdsLayer);
      const ids = new Set(layer.valueIds);
      points = points.filter((point) => ids.has(point.valueId));
      layerClaim = layer.claim;
    }
    const numericPoints = points.filter((point) => Number.isFinite(Number(point.value)) && point.unit === "eV");
    if (target.displayMode === "summary") {
      setChartEmpty($("#targetChart"), "该目标是分类状态盘点，不作为数值分布绘制。");
    } else {
      renderTargetChart(numericPoints, {
        id: `current-${target.sourceKey}-${state.rdsLayer}`,
        title: `${target.label} · 当前 readiness 成员`,
        label: target.label,
        tooltipTitle: "当前目标点级分箱",
      });
    }
    safeText($("#targetStatusMark"), target.status === "EXPLORATORY" ? "探索性 · 无模型" : "样本不足 · 无模型");
    $("#targetStatusMark").className = "status-mark is-descriptive";
    safeText($("#targetN"), target.displayMode === "summary" ? `${number(target.lithiumRows)} / ${number(target.upstreamRows)}` : `${number(points.length)}${target.upstreamRows !== points.length ? ` / 上游 ${target.upstreamRows}` : ""}`);
    safeText($("#targetPapers"), `${number(new Set(points.map((point) => point.paperCode).filter(Boolean)).size || target.paperCount || 0)} 篇`);
    document.querySelector("#targetPrimary").previousElementSibling.textContent = target.sourceKey === "rds_delta_g_numeric" ? "条件层" : "范围拆分";
    safeText($("#targetPrimary"), target.sourceKey === "rds_delta_g_numeric" ? "68 宽 / 64 部分 / 0 严格" : `${number(points.length)} 单库 / ${number(target.upstreamRows)} 上游`);
    safeText($("#targetUnit"), target.unit || "—");
    safeText($("#targetChartTitle"), `${target.label} · ${numericPoints.length} 条可绘制`);
    const scopeDetail = target.excludedOtherDomainRows > 0 ? `上游 readiness 总盘 ${target.upstreamRows} 条，其中其它域 ${target.excludedOtherDomainRows} 条不进入当前单库运行时包。` : "";
    safeText($("#targetChartNote"), `${layerClaim ? `${layerClaim}。` : ""}${scopeDetail}${target.statusReason}`);
    safeText($("#targetDefinition"), target.definition);
    $("#targetScopeBanner").innerHTML = `<b>${escapeHtml(target.status)}</b><span>${escapeHtml(target.label)}</span><small>无目标专属模型 / SHAP · 运行时仅含锂硫单库成员</small>`;
    renderPointTable("targetPointTable", pointRowsTarget(points), TARGET_POINT_COLUMNS);
  }

  function renderHistoricalTarget(target, isBarrier) {
    $("#targetLayerControl").hidden = true;
    $("#evidenceControlWrap").hidden = target.displayMode === "coverage";
    const evidenceOnly = $("#evidenceOnly").checked;
    const points = evidenceFiltered(target.points || [], evidenceOnly);
    const rows = pointRowsTarget(points);
    if (target.displayMode === "coverage" && target.definitions) {
      MLCharts.renderHorizontalBars($("#targetChart"), target.definitions.map((definition) => ({
        label: definition.label,
        value: definition.n,
        display: `${definition.n} 条 · 主证据 ${definition.primaryCount}`,
        tooltip: [["历史定义", definition.id], ["行数", definition.n], ["主证据", definition.primaryCount]],
      })), { id: `history-${target.id}-coverage`, title: `${target.label} · 字段覆盖`, rowHeight: 42, left: 225 });
    } else {
      renderTargetChart(points, { id: `history-${target.id}`, title: `${target.label} · 历史描述性分布`, label: target.label });
    }
    safeText($("#targetStatusMark"), "历史定义覆盖");
    $("#targetStatusMark").className = "status-mark is-descriptive";
    safeText($("#targetN"), number(target.n));
    safeText($("#targetPapers"), `${number(target.paperCount)} 篇`);
    document.querySelector("#targetPrimary").previousElementSibling.textContent = "旧主证据";
    safeText($("#targetPrimary"), `${number(target.primaryCount)} / ${number(target.n)}`);
    safeText($("#targetUnit"), target.unit || "eV");
    safeText($("#targetChartTitle"), `${target.label} · ${target.displayMode === "coverage" ? "族定义覆盖" : `${points.length} 条历史记录`}`);
    safeText($("#targetChartNote"), "该层只保留旧 v2 覆盖与追溯，不是当前目标；历史族不混合绘制数值分布。");
    safeText($("#targetDefinition"), target.definition);
    $("#targetScopeBanner").innerHTML = `<b>历史 v2 · 非当前目标</b><span>${escapeHtml(target.label)}</span><small>${isBarrier ? "历史字段覆盖" : "旧 RDS 族账"} · 无模型 / SHAP</small>`;
    renderPointTable("targetPointTable", rows, TARGET_POINT_COLUMNS);
  }

  function renderOofTarget(target) {
    $("#targetLayerControl").hidden = true;
    $("#evidenceControlWrap").hidden = true;
    const points = target.points;
    const yValues = points.map((point) => Number(point[1])).filter(Number.isFinite);
    MLCharts.renderHistogram($("#targetChart"), yValues, {
      id: `historical-oof-${target.sourceKey}`,
      title: `${target.label} · 历史 OOF 观测分布`,
      unit: "eV",
      xLabel: `${target.label} / eV`,
      exportRows: oofRows(target),
    });
    safeText($("#targetStatusMark"), "历史 dummy 诊断");
    $("#targetStatusMark").className = "status-mark is-model";
    safeText($("#targetN"), number(target.n));
    safeText($("#targetPapers"), `${number(target.sourceGroupCount)} 个来源组`);
    document.querySelector("#targetPrimary").previousElementSibling.textContent = "历史总账";
    safeText($("#targetPrimary"), target.sourceKey === "decomposition_barrier_li2s" ? "58 合并 / 51 单库 / 7 排除" : "—");
    safeText($("#targetUnit"), target.unit);
    safeText($("#targetChartTitle"), `${target.label} · ${target.n} 条历史 dummy OOF`);
    safeText($("#targetChartNote"), target.notice);
    safeText($("#targetDefinition"), "历史来源组 CV 的 dummy 中位数基线；无目标专属模型，也未重新训练。");
    $("#targetScopeBanner").innerHTML = `<b>历史 dummy 诊断</b><span>${escapeHtml(target.label)}</span><small>非目标专用模型 · 单库仅显示可归属本库成员</small>`;
    renderPointTable("targetPointTable", oofRows(target), OOF_COLUMNS);
  }

  function renderTarget() {
    const selection = state.targetSelection;
    if (selection.startsWith("oof:")) return renderOofTarget(state.adapter.oofTarget(selection));
    if (selection.startsWith("barrier:")) return renderHistoricalTarget(state.adapter.historicalBarrierDefinition(selection.slice(8)), true);
    if (selection.startsWith("rds:")) return renderHistoricalTarget(state.adapter.historicalRdsDefinition(selection.slice(4)), false);
    return renderCurrentTarget(state.adapter.currentTarget(selection));
  }

  function renderReadinessVisuals() {
    const ready = Object.fromEntries(state.adapter.currentTargets().map((target) => [target.sourceKey, target]));
    const categories = ["Step I", "Step II", "完整路径", "动力学活化能"];
    MLCharts.renderGroupedBars($("#barrierCompareChart"), categories, [
      { name: "单库成员", color: "#2864f0", values: [ready.li2s_step1_local_barrier.n, 0, 0, ready.kinetic_activation_energy.n] },
      { name: "上游总盘点", color: "#f04d68", values: [43, 0, 0, 271] },
    ], { id: "current-li2s-readiness-coverage", title: "当前 Li₂S 路径 readiness" });
    const ranking = rdsTargetItems();
    $("#rdsRanking").innerHTML = ranking.map(({ selection, item, current }, index) => {
      const count = current ? "68 宽 / 64 部分 / 0 严格" : `${item.n} / 主证据 ${item.primaryCount}`;
      return `<button type="button" class="ranking-item" data-rds-pick="${selection}"><span>${index + 1}</span><span><b>${escapeHtml(item.label)}</b><small>${current ? "当前目标 · 无模型" : "历史 v2 · 非当前目标"} · ${count}</small></span><em>${current ? "Current" : "Legacy"}</em></button>`;
    }).join("");
    $$("[data-rds-pick]").forEach((button) => button.addEventListener("click", () => setTargetSelection(button.dataset.rdsPick)));
  }

  function renderMaterials() {
    const scope = state.adapter.materialScope(state.materialScope);
    safeText($("#materialScopeLabel"), scope.label);
    safeText($("#materialScopeCounts"), `${number(scope.targetRows)} 个目标行 · ${number(scope.exactMaterialStrings)} 个精确材料字符串 · ${number(scope.paperCount)} 篇`);
    MLCharts.renderHorizontalBars($("#elementChart"), scope.elements.map((element) => ({
      label: element.symbol,
      value: element.targetRows,
      display: `${number(element.targetRows)} 行 · ${number(element.exactMaterials)} 材料`,
      tooltip: [["目标行", number(element.targetRows)], ["精确材料字符串", number(element.exactMaterials)]],
      export: { exact_materials: element.exactMaterials },
    })), { id: `material-elements-${scope.id}`, title: "元素覆盖 · 目标行", rowHeight: 34, left: 75 });
    if (scope.pairs.length) {
      MLCharts.renderHorizontalBars($("#pairChart"), scope.pairs.map((pair) => ({
        label: pair.pair.replace("–", " / "),
        value: pair.targetRows,
        display: `${number(pair.targetRows)} 行`,
        tooltip: [["明确双元素组合", pair.pair], ["目标行", number(pair.targetRows)]],
      })), { id: `material-pairs-${scope.id}`, title: "双金属组合 · 目标行", rowHeight: 36, left: 150 });
    } else setChartEmpty($("#pairChart"), "当前范围没有明确双元素身份");
    $("#materialBreakdown").innerHTML = scope.scopeBreakdown.map((item) => `<div class="breakdown-card"><strong>${number(item.targetRows)}</strong><span>${escapeHtml(item.label)}</span></div>`).join("");
  }

  function populateShapControls() {
    const select = $("#shapTargetFilter");
    select.replaceChildren(option("ALL", "六吸附目标合并视图"));
    Object.keys(state.adapter.shap("F1").targetCounts).forEach((target) => select.appendChild(option(target, targetLabel(target))));
    select.value = state.shapTarget;
    select.addEventListener("change", () => {
      state.shapTarget = select.value;
      state.dependencyFeature = null;
      renderShap();
    });
    $$("[data-feature-set]").forEach((button) => button.addEventListener("click", () => {
      state.featureSet = button.dataset.featureSet;
      state.dependencyFeature = null;
      $$("[data-feature-set]").forEach((item) => item.classList.toggle("is-active", item === button));
      renderShap();
    }));
    $("#dependencyFeature").addEventListener("change", () => {
      state.dependencyFeature = $("#dependencyFeature").value;
      renderDependency();
    });
  }

  function filteredShapFeatures(view) {
    return view.features.map((feature) => {
      const points = state.shapTarget === "ALL" ? feature.points : feature.points.filter((point) => point[2] === state.shapTarget);
      const importance = points.length ? points.reduce((sum, point) => sum + Math.abs(Number(point[1])), 0) / points.length : 0;
      return { ...feature, globalRank: feature.rank, points, filteredImportance: importance };
    }).filter((feature) => feature.points.length).sort((a, b) => b.filteredImportance - a.filteredImportance).map((feature, index) => ({ ...feature, localRank: index + 1 }));
  }

  function shapPointRows(features) {
    return features.flatMap((feature) => feature.points.map((point) => ({
      feature_id: feature.id,
      feature_name: feature.label,
      raw_value: point[0],
      shap_value: point[1],
      target_id: point[2],
      outer_fold: point[3],
      record_id: point[4],
      paper_id: point[5],
      paper_code: point[6],
      source_group_id: point[7],
      data_domain: point[8],
      material_descriptor_group_id: point[9],
    })));
  }

  const SHAP_POINT_COLUMNS = [["feature_name", "特征"], ["raw_value", "原始值"], ["shap_value", "SHAP"], ["target_id", "目标"], ["record_id", "record_id"], ["paper_code", "文献"], ["source_group_id", "来源组"], ["data_domain", "库域"], ["outer_fold", "折"]];

  function renderShap() {
    const view = state.adapter.shap(state.featureSet);
    const meta = state.adapter.shapMeta();
    const features = filteredShapFeatures(view);
    const allPointCount = features.reduce((sum, feature) => sum + feature.points.length, 0);
    safeText($("#shapScopeTitle"), `E2 ${state.featureSet} · RandomForest · material_descriptor_cv`);
    safeText($("#shapScopeText"), `${meta.claimBoundary} 运行：${view.runId}；${meta.countRule}`);
    safeText($("#shapCountBadge"), `${state.featureSet} · ${allPointCount} 个特征点`);
    safeText($("#beeswarmTitle"), `${state.featureSet} · ${state.shapTarget === "ALL" ? "六吸附联合" : targetLabel(state.shapTarget)} SHAP 蜂群`);
    if (!features.length) {
      setChartEmpty($("#beeswarmChart"), "当前目标筛选下没有 SHAP 记录");
    } else {
      MLCharts.renderBeeswarm($("#beeswarmChart"), features, {
        id: `shap-beeswarm-${state.featureSet.toLowerCase()}-${state.shapTarget}`,
        title: `${state.featureSet} · 新材料留出 SHAP 蜂群`,
        targetLabel,
        onFeatureSelect: (feature) => {
          state.dependencyFeature = feature.id;
          $("#dependencyFeature").value = feature.id;
          renderDependency();
          highlightFeature(feature.id);
        },
      });
    }
    $("#shapFeatureList").innerHTML = features.map((feature, index) => `<button type="button" class="feature-row ${feature.id === state.dependencyFeature ? "is-active" : ""}" data-feature-id="${escapeHtml(feature.id)}" aria-label="当前筛选第 ${index + 1}，全局第 ${feature.globalRank}，${escapeHtml(feature.label)}，平均绝对 SHAP ${feature.filteredImportance.toFixed(3)}"><span class="feature-rank">${index + 1}</span><span><b>${escapeHtml(feature.label)}</b><small>全局第 ${feature.globalRank} · ${escapeHtml(feature.groupLabel)} · ${feature.stability === "U" ? "跨折不稳定" : "跨折较稳定"}</small></span><em>${feature.filteredImportance.toFixed(3)}</em></button>`).join("");
    $$("[data-feature-id]", $("#shapFeatureList")).forEach((button) => button.addEventListener("click", () => {
      state.dependencyFeature = button.dataset.featureId;
      $("#dependencyFeature").value = state.dependencyFeature;
      renderDependency();
      highlightFeature(state.dependencyFeature);
    }));
    const dependencySelect = $("#dependencyFeature");
    dependencySelect.replaceChildren(...features.map((feature) => option(feature.id, feature.label)));
    if (!features.some((feature) => feature.id === state.dependencyFeature)) state.dependencyFeature = features[0]?.id || null;
    dependencySelect.value = state.dependencyFeature;
    renderDependency();
    renderPointTable("shapPointTable", shapPointRows(features), SHAP_POINT_COLUMNS);
  }

  function highlightFeature(featureId) {
    $$("[data-feature-id]").forEach((button) => button.classList.toggle("is-active", button.dataset.featureId === featureId));
  }

  function renderDependency() {
    const features = filteredShapFeatures(state.adapter.shap(state.featureSet));
    const feature = features.find((item) => item.id === state.dependencyFeature) || features[0];
    if (!feature) return setChartEmpty($("#dependencyChart"), "没有可用的依赖数据");
    safeText($("#dependencyTitle"), `${feature.label} · SHAP 依赖`);
    MLCharts.renderDependency($("#dependencyChart"), feature.points, {
      id: `shap-dependency-${state.featureSet.toLowerCase()}-${state.shapTarget}-${feature.id}`,
      title: `${state.featureSet} · ${feature.label}`,
      featureId: feature.id,
      featureLabel: feature.label,
      targetLabel,
      xLabel: `${feature.label} 原始值`,
    });
  }

  function oofRows(target) {
    return target.points.map((point) => ({
      record_id: point[0], 观测值_eV: point[1], OOF预测值_eV: point[2], 区间低_eV: point[3], 区间高_eV: point[4],
      残差_eV: point[5], 数值域外: point[6], source_group_id: point[7], paper_codes: point[8], data_domain: point[9], outer_fold: point[10], selected_model: "dummy_median",
    }));
  }

  const OOF_COLUMNS = [["record_id", "record_id"], ["paper_codes", "文献"], ["source_group_id", "来源组"], ["data_domain", "库域"], ["outer_fold", "折"], ["观测值_eV", "观测 / eV"], ["OOF预测值_eV", "OOF 预测 / eV"], ["残差_eV", "残差 / eV"], ["selected_model", "模型"]];

  function renderDiagnostics() {
    const target = state.adapter.oofTarget(state.oofSelection);
    const residuals = target.points.map((point) => Number(point[5])).filter(Number.isFinite);
    safeText($("#oofN"), number(target.n));
    safeText($("#oofMae"), `${target.meanAbsResidual.toFixed(3)} eV`);
    safeText($("#oofOutside"), `${number(target.domainOutlierCount)} 条`);
    safeText($("#oofModel"), "历史 dummy 中位数");
    safeText($("#oofScatterTitle"), `${target.label} · 观测 / 历史 dummy OOF`);
    safeText($("#residualTitle"), `${target.label} · 残差分布`);
    MLCharts.renderScatter($("#oofScatter"), target.points, { id: `oof-scatter-${target.sourceKey}-dummy`, title: `${target.label} · 历史 dummy OOF` });
    MLCharts.renderHistogram($("#residualChart"), residuals, {
      id: `oof-residual-${target.sourceKey}-dummy`, title: `${target.label} · 残差 = 观测 − 预测`, unit: "eV", xLabel: "历史 OOF 残差 / eV", tooltipTitle: "残差区间", exportRows: oofRows(target),
    });
    const historyCounts = target.sourceKey === "decomposition_barrier_li2s" ? `历史合并账 ${target.historicalSourceRows} · 单库打包 ${target.n} · 其它域排除 ${target.excludedOtherDomainRows}。` : "";
    $("#oofBanner").innerHTML = `<b>历史 dummy 诊断</b><span>${escapeHtml(target.label)} · ${target.n} 条单库 OOF</span><small>${escapeHtml(historyCounts + target.notice)}</small>`;
    renderPointTable("oofPointTable", oofRows(target), OOF_COLUMNS);
    $("#protocolGrid").innerHTML = state.adapter.modelSummary().map((row) => `<div class="protocol-card"><span>${row.featureSet} · ${row.protocolLabel}</span><b>${row.mae.toFixed(3)} eV</b><small>${row.modelLabel} · 相比 dummy ${(row.relativeImprovement * 100).toFixed(1)}% · 来源重叠 ${row.sourceOverlapMax}</small></div>`).join("");
  }

  function slug(value) {
    return String(value || "na").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "na";
  }

  function exportContext(svgId) {
    if (svgId === "ledgerChart") return "library-lithium-sulfur";
    if (svgId === "elementChart" || svgId === "pairChart") return `material-${slug(state.materialScope)}`;
    if (svgId === "targetChart") {
      const layer = state.targetSelection === "readiness:rds_delta_g_numeric" ? `-${slug(state.rdsLayer)}` : "";
      const evidence = !$("#evidenceControlWrap").hidden ? `-${$("#evidenceOnly").checked ? "primary" : "all"}` : "";
      return `target-${slug(state.targetSelection)}${layer}${evidence}`;
    }
    if (svgId === "beeswarmChart") return `shap-${slug(state.featureSet)}-target-${slug(state.shapTarget)}-rf-material-descriptor-cv`;
    if (svgId === "dependencyChart") return `shap-dependency-${slug(state.featureSet)}-target-${slug(state.shapTarget)}-feature-${slug(state.dependencyFeature)}`;
    if (svgId === "oofScatter" || svgId === "residualChart") return `oof-${slug(state.oofSelection)}-dummy-target-specific-cv`;
    return slug(svgId);
  }

  function wireExports() {
    document.addEventListener("click", async (event) => {
      const pointButton = event.target.closest("[data-point-export]");
      if (pointButton) {
        const container = $(`#${pointButton.dataset.pointExport}`);
        const scope = slug(pointButton.dataset.pointExport.replace("PointTable", ""));
        const selection = pointButton.dataset.pointExport === "shapPointTable" ? `${state.featureSet}-${state.shapTarget}` : pointButton.dataset.pointExport === "oofPointTable" ? state.oofSelection : state.targetSelection;
        MLCharts.downloadRowsCsv(container?._pointRows || [], `ml-explorer-points-${scope}-${slug(selection)}-20261031.csv`);
        showExportStatus("全部点级溯源 CSV 已导出");
        return;
      }
      const button = event.target.closest("[data-export]");
      if (!button) return;
      const group = button.closest("[data-svg]");
      const svg = group && document.getElementById(group.dataset.svg);
      if (!svg) return;
      const type = button.dataset.export;
      const base = `ml-explorer-${group.dataset.svg}-${exportContext(group.dataset.svg)}-20261031`;
      button.disabled = true;
      try {
        if (type === "png") {
          showExportStatus("正在生成 3× 高清 PNG…");
          await MLCharts.downloadPng(svg, `${base}.png`, 3);
          showExportStatus("高清 PNG 已导出");
        } else {
          MLCharts.downloadCsv(svg, `${base}.csv`);
          showExportStatus("作图数据 CSV 已导出");
        }
      } catch (error) {
        showExportStatus(`导出失败：${error.message}`, true);
      } finally {
        button.disabled = false;
      }
    });
  }

  function wireControls() {
    $$("[data-material-scope]").forEach((button) => button.addEventListener("click", () => {
      state.materialScope = button.dataset.materialScope;
      $$("[data-material-scope]").forEach((item) => item.classList.toggle("is-active", item === button));
      renderMaterials();
    }));
    $$("[data-rds-layer]").forEach((button) => button.addEventListener("click", () => {
      state.rdsLayer = button.dataset.rdsLayer;
      $$("[data-rds-layer]").forEach((item) => item.classList.toggle("is-active", item === button));
      renderTarget();
    }));
    $("#evidenceOnly").addEventListener("change", renderTarget);
    $("#retryLoad").addEventListener("click", boot);
  }

  async function boot() {
    const pageChrome = $("#pageChrome");
    const appContent = $("#appContent");
    const appError = $("#appError");
    const pageFooter = $("#pageFooter");
    pageChrome.hidden = true;
    appContent.hidden = true;
    pageFooter.hidden = true;
    appError.hidden = true;
    $("#mainContent").setAttribute("aria-busy", "true");
    setGlobalStatus("正在校验运行时 manifest、bundle hash 与目标白名单…");
    try {
      const loaded = await MLExplorerAdapter.load();
      state.payload = loaded.payload;
      state.runtimeManifest = loaded.runtimeManifest;
      state.adapter = await MLExplorerAdapter.create(state.payload, state.runtimeManifest);
      updateManifestChrome();
      populateTargetSelects();
      populateShapControls();
      renderReadinessGrid();
      renderOverview();
      renderTarget();
      renderReadinessVisuals();
      renderMaterials();
      renderShap();
      renderDiagnostics();
      pageChrome.hidden = false;
      appContent.hidden = false;
      pageFooter.hidden = false;
      const initialView = VIEWS.has(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
      showView(initialView, false);
      setGlobalStatus("完整性校验通过 · 锂硫单库运行时 · 822 是 F1/F2 两个模型视图，不是独立样本", "ready");
    } catch (error) {
      pageChrome.hidden = true;
      appContent.hidden = true;
      pageFooter.hidden = true;
      setGlobalStatus("数据包校验失败 · 未渲染任何图表", "error");
      safeText($("#appErrorMessage"), error.message || String(error));
      appError.hidden = false;
      appError.focus({ preventScroll: true });
      appError.scrollIntoView({ block: "start" });
    } finally {
      $("#mainContent").setAttribute("aria-busy", "false");
    }
  }

  wireNavigation();
  wireControls();
  wireExports();
  boot();
})();
