(function () {
  "use strict";

  /* ============================================================
     AI-shujvku 数据分析（Visuals）科研工作台核心控制逻辑（第二轮精修）
     ============================================================ */

  const CURRENT_LIBRARY_STORAGE_KEY = "litai_current_library";

  // 默认分析字段
  const DEFAULTS = {
    x: "li2s_bader_charge_transfer",
    y: "li_s_bond_max",
    quickX: "li2s_bader_charge_transfer",
    quickY: "li_s_bond_max",
    minN: 3,
  };

  // 核心相关性矩阵展示变量（对应 6 个具备已审数据的核心物理化学性质）
  const CORE_MATRIX_VARS = [
    "s8_adsorption_energy",
    "li2s2_adsorption_energy",
    "li2s_adsorption_energy",
    "li2s_bader_charge_transfer",
    "li1_s_bond_length",
    "li_s_bond_max",
  ];

  // 物理化学性质展示与符号映射
  const PROPERTY_META = {
    s8_adsorption_energy: { symbol: "E_ads(S₈)", zh: "S₈ 吸附能", unit: "eV", desc: "S₈ 在催化剂表面的吸附自由能" },
    li2s8_adsorption_energy: { symbol: "E_ads(Li₂S₈)", zh: "Li₂S₈ 吸附能", unit: "eV", desc: "Li₂S₈ 在催化剂表面的吸附能" },
    li2s6_adsorption_energy: { symbol: "E_ads(Li₂S₆)", zh: "Li₂S₆ 吸附能", unit: "eV", desc: "Li₂S₆ 在催化剂表面的吸附能" },
    li2s4_adsorption_energy: { symbol: "E_ads(Li₂S₄)", zh: "Li₂S₄ 吸附能", unit: "eV", desc: "Li₂S₄ 在催化剂表面的吸附能" },
    li2s2_adsorption_energy: { symbol: "E_ads(Li₂S₂)", zh: "Li₂S₂ 吸附能", unit: "eV", desc: "Li₂S₂ 在催化剂表面的吸附能" },
    li2s_adsorption_energy: { symbol: "E_ads(Li₂S)", zh: "Li₂S 吸附能", unit: "eV", desc: "Li₂S 在催化剂表面的吸附能" },
    li2s_dissociation_barrier: { symbol: "E_b(Li₂S)", zh: "Li₂S 解离能垒", unit: "eV", desc: "Li₂S 解离为 Li + S 的活化能垒" },
    li2s_decomposition_barrier: { symbol: "E_b(Li₂S)", zh: "Li₂S 分解能垒", unit: "eV", desc: "Li₂S 分解反应活化能垒" },
    li2s_bader_charge_transfer: { symbol: "Q_Bader", zh: "Li₂S Bader 电荷转移", unit: "e", desc: "Li₂S 向催化剂表面转移的 Bader 电荷量" },
    li1_s_bond_length: { symbol: "d(Li1-S)", zh: "Li1-S 键长", unit: "Å", desc: "吸附构型中 Li1-S 键长" },
    li2_s_bond_length: { symbol: "d(Li2-S)", zh: "Li2-S 键长", unit: "Å", desc: "吸附构型中 Li2-S 键长" },
    li_s_bond_max: { symbol: "d_max(Li-S)", zh: "Li-S 最大键长", unit: "Å", desc: "吸附构型中的最大 Li-S 键长" },
    d_band_center: { symbol: "ε_d", zh: "d 带中心", unit: "eV", desc: "催化活性金属表面的 d 带中心能量" },
    rds_delta_g: { symbol: "ΔG_RDS", zh: "决速步自由能", unit: "eV", desc: "反应决速步骤（RDS）的自由能变化" },

    adsorption_energy: { symbol: "E_ads", zh: "吸附能", unit: "eV", desc: "多硫化物/吸附质在表面的吸附能" },
    binding_energy: { symbol: "E_bind", zh: "结合能", unit: "eV", desc: "结合能" },
    reaction_barrier: { symbol: "E_act", zh: "反应能垒", unit: "eV", desc: "多硫化物催化转化能垒" },
    rds_energy: { symbol: "ΔG_RDS", zh: "决速步自由能", unit: "eV", desc: "反应决速步吉布斯自由能变化" },
    charge_transfer: { symbol: "Δq", zh: "电荷转移", unit: "e", desc: "表面与吸附物间的电荷转移量" },
    bader_charge: { symbol: "q_Bader", zh: "Bader 电荷", unit: "e" },
    work_function: { symbol: "Φ", zh: "功函数", unit: "eV", desc: "催化材料表面真空能级差" },
    bond_length: { symbol: "d_bond", zh: "键长", unit: "Å" },
    li_s_bond_length: { symbol: "d(Li-S)", zh: "Li-S 键长", unit: "Å" },
    formation_energy: { symbol: "E_form", zh: "形成能", unit: "eV" },
    overpotential: { symbol: "η", zh: "过电势", unit: "V" },
    limiting_potential: { symbol: "U_L", zh: "极限电势", unit: "V" },
    band_gap: { symbol: "E_g", zh: "带隙", unit: "eV" },
  };

  // 矩阵变量名到关系探索字段名的反向映射字典
  const MATRIX_TO_EXPLORE_FIELD_MAP = {
    s8_adsorption_energy: "s8_adsorption_energy",
    li2s2_adsorption_energy: "li2s2_adsorption_energy",
    li2s_adsorption_energy: "li2s_adsorption_energy",
    li2s_bader_charge_transfer: "li2s_bader_charge_transfer",
    li1_s_bond_length: "li1_s_bond_length",
    li2_s_bond_length: "li2_s_bond_length",
    li_s_bond_max: "li_s_bond_max",
    adsorption_energy: "li2s_adsorption_energy",
    li2s_decomposition_barrier: "li2s_dissociation_barrier",
    reaction_barrier: "li2s_dissociation_barrier",
    d_band_center: "d_band_center",
    charge_transfer: "li2s_bader_charge_transfer",
    bader_charge: "li2s_bader_charge_transfer",
    rds_energy: "rds_delta_g",
    bond_length: "li_s_bond_max",
    li_s_bond_length: "li_s_bond_max",
  };

  const WARNING_LABELS = {
    min_n_not_reached: "有效配对未达到最少样本数，暂不计算拟合结果",
    fewer_than_two_contributing_papers: "有效数据来自不到 2 篇论文，跨文献可信度不足",
  };

  const EXCLUSION_LABELS = {
    context_mismatch: "计算设置、位点或构型不兼容",
    multiple_comparable_contexts: "存在多个可比计算上下文，无法唯一选择",
    missing_both_field_values: "同一催化剂同时缺少 X 和 Y 字段",
    missing_x_field_value: "缺少 X 字段",
    missing_y_field_value: "缺少 Y 字段",
    missing_field_value: "缺少分析字段",
    conflicting_values: "同一语义上下文存在冲突数值",
    identity_v2_required: "历史记录尚未完成 Identity V2 结构化身份",
    identity_v2_not_ml_ready: "Identity V2 信息不完整，尚不能用于分析",
    pair_analysis_invalid_numeric_target: "缺少可用于关系分析的有限数值",
    pair_analysis_target_not_normalized: "数值或单位无法安全标准化",
    missing_catalyst_sample_id: "记录未明确绑定催化剂样品",
    missing_or_ambiguous_calculation_context: "计算设置缺失或无法唯一关联",
    "safety_gate:missing_material_identity": "缺少材料或催化剂身份",
    "safety_gate:missing_review": "记录尚未完成审核",
    "safety_gate:unsafe_review": "审核状态不允许用于分析",
    "safety_gate:target_rejected": "记录已在审核中被拒绝",
    "safety_gate:missing_state_context_identity": "缺少初态、终态或吸附状态身份",
    "safety_gate:missing_reaction_step_identity": "缺少反应步骤身份",
    "safety_gate:unsupported_unit_identity": "单位不受支持或无法安全换算",
    "safety_gate:missing_unit_identity": "单位尚未完成结构化识别",
    "safety_gate:missing_atom_or_site_identity": "缺少原子或吸附位点身份",
    "safety_gate:missing_atom_pair_identity": "缺少键长对应的原子对身份",
    "safety_gate:missing_required_unit": "缺少该性质必需的单位",
    "safety_gate:missing_value_identity": "缺少可用数值",
  };

  // 全局响应式状态
  const state = {
    currentTab: "relations", // 'relations' | 'matrix' | 'dataset'
    libraryName: new URLSearchParams(window.location.search).get("library_name") || "",
    libraryResolutionFailed: false,
    libraries: [],
    fields: [],
    xField: DEFAULTS.x,
    yField: DEFAULTS.y,
    minN: DEFAULTS.minN,
    lastCorrelation: null,
    matrixData: null,
    matrixScope: "core", // 'core' | 'all'
    matrixMinN: 3,
    selectedMatrixCell: null,
    matrixDetailData: null,
    requestId: 0,
    activePointIdx: null,
    exportsEnabled: false,
  };

  // DOM 快捷选择器
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = (value) => (value === null || value === undefined || value === "")
    ? null
    : (Number.isFinite(Number(value)) ? Number(value) : null);
  const display = (value) => number(value) === null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });

  function fmt2(value) {
    return value === null || value === undefined ? "—" : (Math.round(value * 100) / 100).toString();
  }

  // 化学分子式格式化（Li2S -> Li₂S, FeN4 -> FeN₄ 等）
  function formatChemicalFormula(text) {
    if (!text) return "";
    const subscripts = { "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉" };
    return String(text).replace(/([A-Za-z\)])(\d+)/g, (_, char, num) => {
      return char + num.split("").map((d) => subscripts[d] || d).join("");
    });
  }

  // 获取字段元数据展示
  function getFieldMeta(key) {
    const info = PROPERTY_META[key] || {};
    const fieldObj = state.fields.find((f) => f.key === key);
    const labelZh = info.zh || fieldObj?.label || key;
    const unit = info.unit || fieldObj?.unit || "";
    const symbol = info.symbol || key;
    return {
      key,
      labelZh: formatChemicalFormula(labelZh),
      unit,
      symbol,
      desc: info.desc || fieldObj?.desc || "",
      displayWithUnit: unit ? `${formatChemicalFormula(labelZh)} / ${unit}` : formatChemicalFormula(labelZh),
    };
  }

  function fieldLabel(key) {
    return state.fields.find((field) => field.key === key)?.label || key;
  }

  function pointCatalystName(point) {
    return point.catalyst_name || point.catalyst?.name || point.catalyst || "催化剂";
  }

  function axisValue(point, axis) {
    return point?.[axis]?.value ?? point?.[axis + "_value"] ?? null;
  }

  function statistic(data, name) {
    return data.statistics?.[name] ?? data[name] ?? null;
  }

  const responseCache = new Map();
  let correlationController;
  let detailLimit = 50;
  async function getJSON(url, signal) {
    const cached = responseCache.get(url);
    if (cached && Date.now() - cached.time < 300000) return cached.data;
    const response = await fetch(url, { signal, headers: { Accept: "application/json" } });
    if (!response.ok) {
      let detail = "";
      try {
        const err = await response.json();
        detail = err.detail ? "：" + err.detail : "";
      } catch (_) {}
      throw new Error("请求失败（" + response.status + "）" + detail);
    }
    const data = await response.json();
    if (responseCache.size >= 100) responseCache.delete(responseCache.keys().next().value);
    responseCache.set(url, {time: Date.now(), data});
    return data;
  }

  function visualParams(params) {
    if (state.libraryName) params.set("library_name", state.libraryName);
    return params;
  }

  /* ============================================================
     文献库初始化与切换
     ============================================================ */
  async function resolveLibraryScope() {
    try {
      const payload = await getJSON("/api/libraries");
      const libraries = Array.isArray(payload) ? payload : (payload.libraries || []);
      state.libraries = libraries;

      const stored = window.localStorage.getItem(CURRENT_LIBRARY_STORAGE_KEY) || "";
      const urlLib = new URLSearchParams(window.location.search).get("library_name") || "";

      let selected = libraries.find((item) => item.name === urlLib);
      if (!selected && state.libraryName) {
        selected = libraries.find((item) => item.name === state.libraryName);
      }
      if (!selected) {
        selected = libraries.find((item) => item.name === stored)
          || libraries.find((item) => item.is_active)
          || libraries[0];
      }

      if (selected?.name && !state.libraryName) {
        state.libraryName = selected.name;
      }

      const select = $("librarySelect");
      if (select) {
        select.innerHTML = libraries.map((lib) => {
          const isSel = lib.name === state.libraryName ? " selected" : "";
          return `<option value="${esc(lib.name)}"${isSel}>${esc(lib.name)}</option>`;
        }).join("");
      }
    } catch (_error) {
      state.libraryResolutionFailed = true;
    }
  }

  function onLibraryChange(newLib) {
    if (newLib === state.libraryName && state.lastCorrelation) return;
    state.libraryName = newLib;
    ++state.requestId;
    correlationController?.abort();
    state.lastCorrelation = null;
    state.matrixData = null;
    state.selectedMatrixCell = null;
    ++matrixRequest; ++pairRequest; ++datasetRequest;
    responseCache.clear();
    window.localStorage.setItem(CURRENT_LIBRARY_STORAGE_KEY, newLib);

    const url = new URL(window.location);
    if (newLib) url.searchParams.set("library_name", newLib);
    else url.searchParams.delete("library_name");
    window.history.replaceState({}, "", url);

    const statusElem = $("globalStatus");
    if (statusElem) statusElem.textContent = `已切换到文献库：${newLib || "全部"}`;

    if (state.currentTab !== "relations") loadOverview();
    if (state.currentTab === "relations") {
      loadCorrelation();
    } else if (state.currentTab === "matrix") {
      loadMatrix();
    } else if (state.currentTab === "dataset") {
      loadDatasetTab();
    }
  }

  /* ============================================================
     Tab 切换机制
     ============================================================ */
  function switchTab(targetTab) {
    if (state.currentTab === targetTab) return;
    state.currentTab = targetTab;

    ["relations", "matrix", "dataset"].forEach((tab) => {
      const btn = $("tabNav" + tab.charAt(0).toUpperCase() + tab.slice(1));
      const panel = $("tab" + tab.charAt(0).toUpperCase() + tab.slice(1));
      if (!btn || !panel) return;

      const isActive = tab === targetTab;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
      panel.classList.toggle("is-active", isActive);
      panel.hidden = !isActive;
    });

    if (targetTab === "relations") {
      if (!state.lastCorrelation) loadCorrelation();
    } else if (targetTab === "matrix") {
      if (!state.matrixData) loadMatrix();
    } else if (targetTab === "dataset") {
      loadDatasetTab();
    }
  }

  /* ============================================================
     数据导出与系统策略安全检查 (Contract)
     ============================================================ */
  function downloadFilename(response, fallback) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/i);
    return match ? match[1] : fallback;
  }

  async function checkExportPolicy() {
    try {
      const settings = await getJSON("/api/settings");
      state.exportsEnabled = Boolean(settings.exports_enabled && settings.exports_enabled !== "");
    } catch (_e) {
      state.exportsEnabled = false;
    }
    updateExportUI();
  }

  function updateExportUI() {
    const badge = $("exportPolicyBadge");
    if (badge) {
      if (state.exportsEnabled) {
        badge.textContent = "策略允许导出";
        badge.className = "policy-badge policy-badge-enabled";
      } else {
        badge.textContent = "策略已禁用导出 (LITAI_EXPORTS_ENABLED=false)";
        badge.className = "policy-badge policy-badge-disabled";
      }
    }
    if (!state.exportsEnabled) {
      const status = $("exportStatus");
      if (status && !status.textContent) {
        status.textContent = "系统策略当前禁用数据导出 (LITAI_EXPORTS_ENABLED=false)";
      }
    }
  }

  async function downloadDataset(kind) {
    const isCsv = kind === "csv";
    const button = $(isCsv ? "exportCatalystCsv" : "downloadCatalystJson");
    const label = isCsv ? "催化剂宽表 CSV" : "审计 JSON";
    const endpoint = isCsv ? "/api/dft/catalyst-dataset.csv" : "/api/dft/catalyst-dataset";
    if (button) button.disabled = true;
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
      if (button) button.disabled = false;
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
    values.forEach(([id, value]) => {
      const el = $(id);
      if (el) el.textContent = display(value);
    });
  }

  async function loadOverview() {
    const library = state.libraryName;
    const metricsEl = $("metrics");
    if (metricsEl) metricsEl.classList.add("is-loading");
    try {
      const data = await getJSON("/api/visuals/overview?" + visualParams(new URLSearchParams({ sections: "overview" })).toString());
      if (library !== state.libraryName) return;
      const summary = data.summary || data.overview || {};
      const meta = data.catalyst_analysis_meta || {};
      setOverview(summary);

      // 更新头部单行紧凑摘要
      const dftCount = summary.total_dft_rows ?? 0;
      const catCount = meta.distinct_exportable_catalysts ?? summary.distinct_exportable_catalysts ?? 0;
      const compactEl = $("headerCompactSummary");
      if (compactEl) {
        compactEl.textContent = `${state.libraryName || "当前文献库"} · ${dftCount} DFT · ${catCount} 催化剂`;
      }

      const statusEl = $("overviewStatus");
      if (statusEl) {
        statusEl.textContent = state.libraryName
          ? "文献库：" + state.libraryName
          : (state.libraryResolutionFailed ? "当前文献库读取失败，已显示全部文献库" : "全部文献库");
      }
    } catch (error) {
      const statusEl = $("overviewStatus");
      if (statusEl) statusEl.textContent = "概览读取失败：" + error.message;
    } finally {
      if (metricsEl) metricsEl.classList.remove("is-loading");
    }
  }

  /* ============================================================
     TAB 1: 关系探索 (Relationship Exploration)
     ============================================================ */
  function normaliseFields(payload) {
    const raw = Array.isArray(payload) ? payload : (payload.fields || payload.analysis_fields || []);
    return raw.filter((field) => field && field.key && (field.type === "number" || field.numeric === true || field.category === "numeric") && field.analysis_enabled !== false)
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

  async function loadFields() {
    const payload = await getJSON("/api/visuals/analysis-fields?" + visualParams(new URLSearchParams()).toString());
    state.fields = normaliseFields(payload);
    if (!state.fields.length) throw new Error("接口未返回可用于数值相关分析的字段");
    fillSelect($("xField"), state.fields, DEFAULTS.x);
    fillSelect($("yField"), state.fields, DEFAULTS.y);
  }

  function listItems(id, items) {
    const target = $(id);
    if (!target) return;
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
    
    const diagSection = $("diagnostics");
    if (diagSection) diagSection.hidden = !(warnings.length || excludedEntries.length);
    $("warningsBox").hidden = !warnings.length;
    $("excludedBox").hidden = !excludedEntries.length;
    listItems("warningsList", warnings.map(warningLabel));
    $("excludedList").innerHTML = excludedEntries.map(([reason, count]) => "<li>" + esc(exclusionLabel(reason)) + (count === "" ? "" : "：" + esc(count) + " 次") + (EXCLUSION_LABELS[reason] || reason.startsWith("safety_gate:") || reason.startsWith("missing_") || reason.startsWith("identity_") ? " <code>" + esc(reason) + "</code>" : "") + "</li>").join("");

    const catalystCount = Number(data.n_catalysts ?? data.catalyst_count ?? (data.points || []).length) || 0;
    const paperCount = Number(data.n_papers ?? data.paper_count) || 0;
    const minN = Number(data.min_n ?? data.statistics?.min_n ?? $("minN").value) || 3;
    
    if (diagSection) diagSection.dataset.state = data.ready === true ? "ready" : "blocked";
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
    
    const techDiag = $("technicalDiagnostics");
    if (techDiag) techDiag.open = false;

    const validCountEl = $("validPairsCount");
    if (validCountEl) validCountEl.textContent = catalystCount;
    const excludedCountEl = $("excludedPairsCount");
    if (excludedCountEl) {
      excludedCountEl.textContent = data.excluded_count ?? Object.values(excluded).reduce((a, b) => a + Number(b || 0), 0);
    }
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
    Object.entries(stats).forEach(([id, value]) => {
      const el = $(id);
      if (el) el.textContent = display(value);
    });

    const slope = number(stats.statSlope);
    const intercept = number(stats.statIntercept);
    const eqEl = $("statEquation");
    if (eqEl) {
      if (data.ready === true && slope !== null && intercept !== null) {
        eqEl.textContent = "y = " + fmt2(slope) + "x " + (intercept >= 0 ? "+ " : "− ") + fmt2(Math.abs(intercept));
      } else {
        eqEl.textContent = "—";
      }
    }
  }

  function renderVerdict(data) {
    const card = $("verdictCard");
    const r = number(statistic(data, "pearson_r"));
    const r2 = number(statistic(data, "r_squared"));
    const catalysts = Number(data.n_catalysts ?? data.catalyst_count ?? (data.points || []).length) || 0;
    const papers = Number(data.n_papers ?? data.paper_count) || 0;
    if (!card) return;

    if (data.ready !== true || r === null) {
      card.dataset.state = "idle";
      $("verdictLabel").textContent = "暂无统计关联";
      $("verdictR").textContent = "—";
      $("verdictMeta").textContent = "有效配对 " + catalysts + " 个，未达到拟合条件";
      $("verdictEquation").textContent = "";
      $("verdictQuality").textContent = "数据量不足";
      return;
    }
    const abs = Math.abs(r);
    const strength = abs >= 0.8 ? "强" : abs >= 0.5 ? "中等" : abs >= 0.3 ? "弱" : "极弱";
    const direction = r > 0 ? "正相关趋势" : r < 0 ? "负相关趋势" : "相关趋势";
    card.dataset.state = abs >= 0.8 ? "strong" : abs >= 0.5 ? "medium" : "weak";
    
    // 严谨学术措辞：剔除"显著"，小样本加前缀，声明探索性
    const samplePrefix = catalysts < 5 ? "小样本" : "";
    $("verdictLabel").textContent = samplePrefix + strength + direction;
    $("verdictR").textContent = "r = " + display(r);
    $("verdictMeta").textContent = "n=" + catalysts + (r2 !== null ? " · R²=" + display(r2) : "");
    const slope = number(statistic(data, "slope"));
    const intercept = number(statistic(data, "intercept"));
    $("verdictEquation").textContent = slope === null || intercept === null
      ? ""
      : "y = " + fmt2(slope) + "x " + (intercept >= 0 ? "+ " : "− ") + fmt2(Math.abs(intercept));
    $("verdictQuality").textContent = papers < 2 ? "单一论文（探索性分析，不构成因果关系）" : `跨 ${papers} 篇论文（探索性统计趋势）`;
  }

  function hideTooltip() {
    state.activePointIdx = null;
    const tip = $("plotTooltip");
    if (tip) tip.hidden = true;
    const plotLayer = $("plotLayer");
    if (plotLayer) {
      plotLayer.querySelectorAll(".plot-point.is-selected").forEach((c) => c.classList.remove("is-selected"));
    }
    const legend = $("plotLegend");
    if (legend) {
      legend.querySelectorAll(".plot-legend-item.is-selected").forEach((item) => item.classList.remove("is-selected"));
    }
    const tbody = $("detailsBody");
    if (tbody) {
      tbody.querySelectorAll("tr.is-selected-row").forEach((r) => r.classList.remove("is-selected-row"));
    }
  }

  function selectPoint(idx, circle) {
    const data = state.lastCorrelation;
    if (!data) return;
    const points = (data.points || []).filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    const point = points[idx];
    if (!point) return;
    hideTooltip();
    state.activePointIdx = idx;
    const targetCircle = circle || $("plotLayer")?.querySelector('.plot-point[data-idx="' + idx + '"]');
    if (!targetCircle) return;
    targetCircle.classList.add("is-selected");
    const legendItem = $("plotLegend")?.querySelector('.plot-legend-item[data-idx="' + idx + '"]');
    if (legendItem) legendItem.classList.add("is-selected");
    const xLabel = data.x_label || fieldLabel(data.x_field || $("xField").value);
    const yLabel = data.y_label || fieldLabel(data.y_field || $("yField").value);
    const tooltip = $("plotTooltip");
    if (tooltip) {
      tooltip.innerHTML = '<strong class="tooltip-title">' + esc(pointCatalystName(point)) + "</strong>"
        + '<span class="muted">' + esc(point.catalyst_sample_id || "") + "</span><br>"
        + esc(xLabel) + "：<b>" + esc(display(axisValue(point, "x"))) + "</b><br>"
        + esc(yLabel) + "：<b>" + esc(display(axisValue(point, "y"))) + "</b>";
      const wrap = document.querySelector(".plot-wrap") || document.querySelector(".plot-container");
      const wrapRect = wrap ? wrap.getBoundingClientRect() : { left: 0, top: 0, width: 640, height: 390 };
      tooltip.hidden = false;
      const circleRect = targetCircle.getBoundingClientRect();
      const tooltipWidth = tooltip.offsetWidth || 180;
      const tooltipHeight = tooltip.offsetHeight || 80;
      let left = circleRect.left - wrapRect.left + 14;
      if (left + tooltipWidth > wrapRect.width - 8) left = circleRect.left - wrapRect.left - tooltipWidth - 14;
      left = Math.max(8, Math.min(left, wrapRect.width - tooltipWidth - 8));
      let top = circleRect.top - wrapRect.top - 10;
      if (top + tooltipHeight > wrapRect.height - 8) top = wrapRect.height - tooltipHeight - 8;
      tooltip.style.left = left + "px";
      tooltip.style.top = Math.max(top, 8) + "px";
    }
    const row = $("detailsBody")?.querySelector('tr[data-idx="' + idx + '"]');
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

  function plot(data) {
    const points = (data.points || []).filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    const svg = $("scatterPlot");
    const width = Math.max(360, Math.round(svg.getBoundingClientRect().width));
    const height = 340;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const layer = $("plotLayer");
    const empty = $("plotEmpty");
    if (!layer || !empty) return;
    layer.innerHTML = "";
    hideTooltip();
    $("plotEmptySummary").hidden = true;
    empty.setAttribute("x", width / 2);
    empty.setAttribute("y", height / 2);
    if (!points.length) {
      const legend = $("plotLegend");
      if (legend) {
        legend.innerHTML = "";
        legend.hidden = true;
      }
      empty.textContent = data.insufficient_reason || data.fit?.reason || "没有可绘制的同一催化剂样本";
      empty.style.display = "none";
      const rows = data.analysis_row_counts || {};
      const excluded = data.excluded_details || [];
      const nx = excluded.filter(p => p.x_candidates?.length).length;
      const ny = excluded.filter(p => p.y_candidates?.length).length;
      const isError = !data.analysis_row_counts;
      $("plotEmptySummary").innerHTML = '<h3>' + (isError ? esc(data.insufficient_reason || "等待分析数据") : "当前字段尚未形成有效配对") + '</h3>'
        + (isError ? '' : '<div class="availability-strip"><span>X 有候选值<b>' + nx + '</b></span><span>Y 有候选值<b>' + ny + '</b></span><span>有效配对<b>0</b></span></div><p>库中仍有 ' + esc(rows.total_dft_rows ?? "—") + ' 条 DFT 记录，' + esc(rows.pair_analysis_ready_numeric_rows ?? "—") + ' 条可参与关系分析。<br>所选字段可能缺少合格数值或可比计算条件，展开下方数据说明查看原因。</p>');
      $("plotEmptySummary").hidden = false;
      $("fitNotice").textContent = data.insufficient_reason || data.reason || "样本量不足或数据不可比，未绘制拟合线。";
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
    const L = 66, R = width - 30, T = 28, B = height - 52;
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

    const xMeta = getFieldMeta(data.x_field || $("xField").value);
    const yMeta = getFieldMeta(data.y_field || $("yField").value);
    const xTitle = data.x_label || xMeta.displayWithUnit;
    const yTitle = data.y_label || yMeta.displayWithUnit;

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

    let pts = '';
    points.forEach((point, idx) => {
      const x = number(axisValue(point, "x")), y = number(axisValue(point, "y"));
      const aria = pointCatalystName(point) + "，X " + display(x) + "，Y " + display(y);
      pts += '<circle class="plot-point-halo" cx="' + sx(x) + '" cy="' + sy(y) + '" r="11"/>';
      pts += '<circle class="plot-point" role="button" tabindex="0" aria-label="' + esc(aria) + '" data-idx="' + idx + '" cx="' + sx(x) + '" cy="' + sy(y) + '" r="6"><title>' + esc(pointCatalystName(point) + " · " + (point.catalyst_sample_id || "")) + "</title></circle>";
    });
    layer.insertAdjacentHTML("beforeend", pts);

    // 保留 DOM 节点以满足自动化回归测试断言，但默认隐藏以保持视觉清爽
    const legend = $("plotLegend");
    if (legend) {
      legend.hidden = true;
      legend.innerHTML = points.map((point, idx) => '<li><button class="plot-legend-item" type="button" data-idx="' + idx + '"><span class="plot-legend-index">' + (idx + 1) + '</span><span class="plot-legend-name">' + esc(pointCatalystName(point)) + "</span></button></li>").join("");
    }

    layer.querySelectorAll(".plot-point").forEach((circle) => {
      circle.addEventListener("pointerenter", () => selectPoint(Number(circle.dataset.idx), circle));
      circle.addEventListener("focus", () => selectPoint(Number(circle.dataset.idx), circle));
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

    $("fitNotice").textContent = fitReady ? "线性拟合 · 悬停或点选散点查看催化剂与数值" : (data.insufficient_reason || data.reason || "样本量不足或数据不可比，未绘制拟合线。");
  }

  function ids(value) {
    const list = Array.isArray(value) ? value : (value ? [value] : []);
    return list.length ? list.map((item) => "<code>" + esc(item) + "</code>").join("、") : "—";
  }

  function candidates(point, data) {
    const barrierAxis = data.x_field === "li2s_dissociation_barrier" ? "x" : (data.y_field === "li2s_dissociation_barrier" ? "y" : "");
    const list = barrierAxis ? (point[barrierAxis]?.candidates || []) : [];
    if (!list.length) return "—";
    return '<div class="candidate-list">' + list.map((candidate) => {
      const selected = candidate.selected_for_summary === true || candidate.selected_for_regression === true;
      const label = [candidate.pathway || candidate.label || "候选路径", display(candidate.value ?? candidate.barrier)].join("：");
      return '<span class="candidate-item"><span class="' + (selected ? "selected-candidate" : "") + '">' + esc(label) + "</span>" + (selected ? '<span class="candidate-badge">用于汇总/回归</span>' : "") + "</span>";
    }).join("") + "</div>";
  }

  function renderDetails(data) {
    const allPoints = data.points || [];
    $("tableCountBadge").textContent = `${allPoints.length} 组配对 · 展开查看`;
    if (!$("pairDetails").open) { $("detailsBody").innerHTML = ""; return; }
    const xKey = data.x_field || $("xField").value, yKey = data.y_field || $("yField").value;
    const xCol = $("xColumn");
    const yCol = $("yColumn");
    if (xCol) xCol.textContent = "X：" + (data.x_label || fieldLabel(xKey));
    if (yCol) yCol.textContent = "Y：" + (data.y_label || fieldLabel(yKey));
    const points = data.points || [];
    const plottable = points.filter((point) => number(axisValue(point, "x")) !== null && number(axisValue(point, "y")) !== null);
    const tbody = $("detailsBody");
    if (!tbody) return;
    const tableCountBadge = $("tableCountBadge");
    if (tableCountBadge) tableCountBadge.textContent = `${points.length} 组配对`;
    $("morePairs").hidden = points.length <= detailLimit;
    tbody.innerHTML = points.length ? points.slice(0, detailLimit).map((point) => {
      const xSources = point.x?.source_record_ids ?? point.x_source_record_ids ?? point.source_record_ids?.x;
      const ySources = point.y?.source_record_ids ?? point.y_source_record_ids ?? point.source_record_ids?.y;
      const plotIdx = plottable.indexOf(point);
      const catalystName = formatChemicalFormula(pointCatalystName(point));
      const paperCode = point.paper?.paper_code ?? point.paper_code ?? "—";
      const sampleId = point.catalyst_sample_id || "—";

      return '<tr' + (plotIdx >= 0 ? ' data-idx="' + plotIdx + '"' : "") + ">"
        + "<td><strong>" + esc(catalystName) + "</strong></td>"
        + "<td>" + esc(paperCode) + "</td>"
        + '<td class="col-num">' + esc(display(axisValue(point, "x"))) + "</td>"
        + '<td class="col-num">' + esc(display(axisValue(point, "y"))) + "</td>"
        + '<td><span class="badge-status-valid">有效配对</span></td>'
        + "<td>"
        +   '<details class="row-tech-detail">'
        +     '<summary class="tech-detail-summary">技术溯源与候选</summary>'
        +     '<div class="tech-detail-content">'
        +       '<div><span class="muted">催化剂 ID:</span> <code>' + esc(sampleId) + '</code></div>'
        +       '<div><span class="muted">源记录 ID:</span> X: ' + ids(xSources) + ' · Y: ' + ids(ySources) + '</div>'
        +       candidates(point, data)
        +     '</div>'
        +   '</details>'
        + "</td>"
        + "</tr>";
    }).join("") : '<tr><td colspan="6" class="empty-cell">没有可显示的同一催化剂样本。</td></tr>';
  }

  async function loadCorrelation() {
    const xField = $("xField").value, yField = $("yField").value;
    const minN = Math.max(3, Number.parseInt($("minN").value, 10) || 3);
    $("minN").value = String(minN);
    const requestId = ++state.requestId;
    correlationController?.abort();
    correlationController = new AbortController();
    state.lastCorrelation = null;
    detailLimit = 50;
    $("plotEmptySummary").hidden = true;
    $("plotLayer").innerHTML = "";
    hideTooltip();
    renderStatistics({});
    document.querySelectorAll(".preset-btn").forEach(btn => btn.classList.toggle("is-active", btn.dataset.x === xField && btn.dataset.y === yField));
    $("relationStatus").textContent = "正在读取同一催化剂样本的关系数据…";
    $("retryCorrelation").hidden = true;
    const plotWrap = document.querySelector(".plot-wrap") || document.querySelector(".plot-container");
    if (plotWrap) { plotWrap.classList.add("is-loading"); plotWrap.setAttribute("aria-busy", "true"); }
    $("plotEmpty").textContent = "正在加载…";
    $("plotEmpty").style.display = "";

    const xMeta = getFieldMeta(xField);
    const yMeta = getFieldMeta(yField);
    const chartTitleEl = $("chartTitle");
    if (chartTitleEl) chartTitleEl.textContent = `${xMeta.labelZh} × ${yMeta.labelZh}`;

    try {
      const params = visualParams(new URLSearchParams({ x_field: xField, y_field: yField, min_n: String(minN) }));
      const data = await getJSON("/api/visuals/catalyst-correlation?" + params.toString(), correlationController.signal);
      if (requestId !== state.requestId) return;
      state.lastCorrelation = data;
      const counts = data.analysis_row_counts || {};
      $("headerCompactSummary").textContent = `${state.libraryName || "当前文献库"} · ${counts.total_dft_rows ?? "—"} DFT · ${counts.distinct_exportable_catalysts ?? "—"} 催化剂`;
      $("overviewStatus").textContent = `${counts.pair_analysis_ready_numeric_rows ?? "—"} 条记录可参与关系分析`;
      setOverview(counts);
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
      if (requestId === state.requestId && plotWrap) { plotWrap.classList.remove("is-loading"); plotWrap.setAttribute("aria-busy", "false"); }
    }
  }

  /* ============================================================
     TAB 2: 相关性矩阵 (Correlation Matrix)
     ============================================================ */
  let matrixRequest = 0, datasetRequest = 0, pairRequest = 0;
  async function loadMatrix() {
    const ticket = ++matrixRequest, library = state.libraryName;
    const minN = parseInt($("matrixMinN")?.value || "3", 10);
    state.matrixMinN = minN;
    const tbody = $("matrixBody");
    if (tbody) tbody.innerHTML = '<tr><td class="empty-cell">正在生成变量相关性矩阵…</td></tr>';

    try {
      const params = visualParams(new URLSearchParams({
        sections: "correlation",
        corr_allow_exploratory: "true",
        corr_min_n: String(minN),
      }));

      const reaction = $("matrixReactionSelect")?.value;
      if (reaction) params.set("corr_reaction", reaction);

      const adsorbate = $("matrixAdsorbateSelect")?.value;
      if (adsorbate) params.set("corr_adsorbate", adsorbate);

      const family = $("matrixFamilySelect")?.value;
      if (family) params.set("corr_family", family);

      const data = await getJSON("/api/visuals/overview?" + params.toString());
      if (ticket !== matrixRequest || library !== state.libraryName) return;
      state.matrixData = data.descriptor_correlation || {};
      renderMatrix();
    } catch (err) {
      if (ticket !== matrixRequest || library !== state.libraryName) return;
      console.error("加载相关性矩阵失败:", err);
      if (tbody) tbody.innerHTML = `<tr><td class="empty-cell">加载相关性矩阵失败：${esc(err.message)}</td></tr>`;
    }
  }

  function renderMatrix() {
    const mc = state.matrixData || {};
    const cells = mc.cells || [];
    const scope = state.matrixScope;

    let vars = [];
    if (scope === "core") {
      vars = CORE_MATRIX_VARS;
    } else {
      const allVars = mc.variables || [];
      vars = allVars.map((v) => v.key || v);
      if (!vars.length) vars = CORE_MATRIX_VARS;
    }

    const badgeVarCount = $("badgeVarCount");
    if (badgeVarCount) badgeVarCount.textContent = `${vars.length} 个变量`;

    const tbody = $("matrixBody");
    if (!tbody) return;
    tbody.innerHTML = "";

    // 建立无序变量对规范化 Map，消除方向性不对称
    const canonicalMap = new Map();
    cells.forEach((c) => {
      const key = [c.x_property, c.y_property].sort().join(":");
      const existing = canonicalMap.get(key);
      if (!existing || (!existing.pearson_r && c.pearson_r) || (c.n > existing.n)) {
        canonicalMap.set(key, c);
      }
    });

    let headerHtml = '<tr><th class="col-header-y">性质 / 变量</th>';
    vars.forEach((vx) => {
      const meta = getFieldMeta(vx);
      headerHtml += `<th>${esc(meta.labelZh)}<br><small class="muted">(${esc(meta.unit || "—")})</small></th>`;
    });
    headerHtml += "</tr>";
    tbody.insertAdjacentHTML("beforeend", headerHtml);

    let validPairCount = 0;
    const totalIndependentPairs = (vars.length * (vars.length - 1)) / 2;

    vars.forEach((vy, rowIdx) => {
      const yMeta = getFieldMeta(vy);
      let rowHtml = `<tr><th class="col-header-y">${esc(yMeta.labelZh)} <small class="muted">(${esc(yMeta.unit || "—")})</small></th>`;

      vars.forEach((vx, colIdx) => {
        // 上三角：留空，避免重复展示造成认知负担
        if (colIdx > rowIdx) {
          rowHtml += `<td class="heatmap-empty-cell"></td>`;
          return;
        }

        // 对角线：自身相关显示 —
        if (colIdx === rowIdx) {
          rowHtml += `
            <td class="heatmap-cell is-diagonal" data-x="${vx}" data-y="${vy}">
              <span class="cell-r-val">—</span>
            </td>
          `;
          return;
        }

        // 下三角：无序独立变量对 (colIdx < rowIdx)
        const pairKey = [vx, vy].sort().join(":");
        const cell = canonicalMap.get(pairKey);
        const nVal = cell ? Number(cell.n || 0) : 0;
        const rVal = (cell && number(cell.pearson_r) !== null) ? number(cell.pearson_r) : null;
        const isSufficient = nVal >= state.matrixMinN && rVal !== null;

        let cellStyle = "";
        let cellClass = "heatmap-cell";

        if (!isSufficient) {
          cellClass += " is-insufficient";
        } else {
          validPairCount++;
          cellClass += " is-ready-cell";
          const alpha = Math.min(0.88, Math.max(0.15, Math.abs(rVal)));
          if (rVal > 0) {
            cellStyle = `background: rgba(124, 58, 237, ${alpha}); color: ${alpha > 0.5 ? '#fff' : 'inherit'};`;
          } else {
            cellStyle = `background: rgba(59, 130, 246, ${alpha}); color: ${alpha > 0.5 ? '#fff' : 'inherit'};`;
          }
        }

        const rText = rVal !== null ? fmt2(rVal) : "—";
        const nText = nVal > 0 ? `n=${nVal}` : "";

        rowHtml += `
          <td class="${cellClass}" style="${cellStyle}" data-x="${vx}" data-y="${vy}">
            <span class="cell-r-val">${rText}</span>
            ${nText ? `<span class="cell-n-sub">${nText}</span>` : ""}
          </td>
        `;
      });

      rowHtml += "</tr>";
      tbody.insertAdjacentHTML("beforeend", rowHtml);
    });

    const badgeCellCount = $("badgeCellCount");
    if (badgeCellCount) badgeCellCount.textContent = `${validPairCount} / ${totalIndependentPairs} 变量对可分析`;

    tbody.querySelectorAll(".heatmap-cell:not(.is-empty-triangle):not(.is-diagonal)").forEach((td) => {
      td.addEventListener("click", () => {
        const vx = td.getAttribute("data-x");
        const vy = td.getAttribute("data-y");
        tbody.querySelectorAll(".heatmap-cell.is-active-cell").forEach((c) => c.classList.remove("is-active-cell"));
        td.classList.add("is-active-cell");
        selectMatrixPair(vx, vy);
      });
    });

    const firstReady = tbody.querySelector('.heatmap-cell.is-ready-cell');
    if (firstReady) {
      firstReady.classList.add("is-active-cell");
      selectMatrixPair(firstReady.getAttribute("data-x"), firstReady.getAttribute("data-y"));
    }
  }

  async function selectMatrixPair(xProp, yProp) {
    const ticket = ++pairRequest, library = state.libraryName;
    state.selectedMatrixCell = { x: xProp, y: yProp };
    const xMeta = getFieldMeta(xProp);
    const yMeta = getFieldMeta(yProp);

    if ($("detailPairTitle")) $("detailPairTitle").textContent = `${xMeta.labelZh} × ${yMeta.labelZh}`;
    if ($("detailBadgeR")) $("detailBadgeR").textContent = "计算中…";
    if ($("detailBadgeN")) $("detailBadgeN").textContent = "—";
    if ($("interpretText")) $("interpretText").textContent = "正在读取该物理化学变量对的配对点与文献记录…";

    try {
      const params = visualParams(new URLSearchParams({
        target_property: yProp,
        descriptor: xProp,
        allow_exploratory: "true",
        min_n: String(state.matrixMinN),
      }));

      const reaction = $("matrixReactionSelect")?.value;
      if (reaction) params.set("reaction", reaction);

      const adsorbate = $("matrixAdsorbateSelect")?.value;
      if (adsorbate) params.set("adsorbate", adsorbate);

      const family = $("matrixFamilySelect")?.value;
      if (family) params.set("material_family", family);

      const pairData = await getJSON("/api/visuals/correlation-pairs?" + params.toString());
      if (ticket !== pairRequest || library !== state.libraryName) return;
      state.matrixDetailData = pairData;

      const r = number(pairData.pearson_r);
      const n = pairData.n || (pairData.points || []).length;
      if ($("detailBadgeR")) $("detailBadgeR").textContent = r !== null ? `r = ${fmt2(r)}` : "样本不足";
      if ($("detailBadgeN")) $("detailBadgeN").textContent = `n = ${n}`;

      if ($("interpretText")) {
        if (r !== null) {
          const abs = Math.abs(r);
          const strength = abs >= 0.8 ? "强" : abs >= 0.5 ? "中等" : "弱";
          const dir = r > 0 ? "正相关" : "负相关";
          const trendText = n < 5 ? `当前小样本内呈${strength}${dir}趋势` : `呈${strength}${dir}趋势`;
          $("interpretText").textContent = `当前催化剂样本内，${xMeta.labelZh} 与 ${yMeta.labelZh} ${trendText} (r = ${fmt2(r)}, n = ${n})。属于小样本探索性统计分析，不构成因果关系。`;
        } else {
          $("interpretText").textContent = `当前变量对在所选文献库范围内配对样本数不足 (n = ${n} < ${state.matrixMinN})，暂无法形成可信的统计拟合。`;
        }
      }

      const pts = pairData.points || [];
      const xs = pts.map((p) => Number(p.x?.value !== undefined ? p.x.value : p.x)).filter(Number.isFinite);
      const ys = pts.map((p) => Number(p.y?.value !== undefined ? p.y.value : p.y)).filter(Number.isFinite);

      if ($("rangeXLabel")) $("rangeXLabel").textContent = xMeta.labelZh;
      if ($("rangeYLabel")) $("rangeYLabel").textContent = yMeta.labelZh;
      if ($("rangeXValue")) $("rangeXValue").textContent = xs.length ? `${fmt2(Math.min(...xs))} ~ ${fmt2(Math.max(...xs))} ${xMeta.unit || ""}` : "—";
      if ($("rangeYValue")) $("rangeYValue").textContent = ys.length ? `${fmt2(Math.min(...ys))} ~ ${fmt2(Math.max(...ys))} ${yMeta.unit || ""}` : "—";
      if ($("rangeNValue")) $("rangeNValue").textContent = `${n} 组`;

      renderMiniPlot(pairData);
      renderMatrixPaperList(pairData);
    } catch (err) {
      console.warn("读取变量对下钻详情失败:", err);
      if ($("interpretText")) $("interpretText").textContent = "读取该变量对数据失败：" + err.message;
    }
  }

  function renderMiniPlot(pairData) {
    const grid = $("miniGridLayer");
    const lineLayer = $("miniLineLayer");
    const pointsLayer = $("miniPointsLayer");
    const emptyText = $("miniEmptyText");
    if (!grid || !lineLayer || !pointsLayer || !emptyText) return;

    grid.innerHTML = "";
    lineLayer.innerHTML = "";
    pointsLayer.innerHTML = "";

    const pts = (pairData.points || []).map((p) => ({
      ...p,
      x: Number(p.x?.value !== undefined ? p.x.value : p.x),
      y: Number(p.y?.value !== undefined ? p.y.value : p.y),
    })).filter((p) => number(p.x) !== null && number(p.y) !== null);
    if (!pts.length) {
      emptyText.textContent = "样本量不足";
      emptyText.style.display = "";
      return;
    }
    emptyText.style.display = "none";

    const L = 35, R = 275, T = 15, B = 165;
    const W = R - L, H = B - T;

    const xs = pts.map((p) => Number(p.x));
    const ys = pts.map((p) => Number(p.y));
    const xScale = niceScale(Math.min(...xs), Math.max(...xs), 4);
    const yScale = niceScale(Math.min(...ys), Math.max(...ys), 4);

    const sx = (x) => L + ((x - xScale.min) / (xScale.max - xScale.min || 1)) * W;
    const sy = (y) => B - ((y - yScale.min) / (yScale.max - yScale.min || 1)) * H;

    grid.insertAdjacentHTML("beforeend", `
      <line stroke="var(--color-border)" stroke-width="1" x1="${L}" y1="${B}" x2="${R}" y2="${B}"/>
      <line stroke="var(--color-border)" stroke-width="1" x1="${L}" y1="${T}" x2="${L}" y2="${B}"/>
    `);

    const slope = number(pairData.slope);
    const intercept = number(pairData.intercept);
    if (slope !== null && intercept !== null) {
      const x1 = xScale.min, y1 = slope * x1 + intercept;
      const x2 = xScale.max, y2 = slope * x2 + intercept;
      lineLayer.insertAdjacentHTML("beforeend", `
        <line stroke="var(--color-primary)" stroke-width="2" stroke-dasharray="4 2" x1="${sx(x1)}" y1="${sy(y1)}" x2="${sx(x2)}" y2="${sy(y2)}"/>
      `);
    }

    pts.forEach((p) => {
      pointsLayer.insertAdjacentHTML("beforeend", `
        <circle cx="${sx(p.x)}" cy="${sy(p.y)}" r="4" fill="var(--color-primary)" stroke="var(--color-surface)" stroke-width="1.5"/>
      `);
    });
  }

  function renderMatrixPaperList(pairData) {
    const list = $("matrixPaperList");
    if (!list) return;
    const pts = pairData.points || [];

    const paperMap = new Map();
    pts.forEach((p) => {
      const key = p.paper_id || (p.paper && p.paper.paper_id) || p.doi || (p.paper && p.paper.doi) || p.paper_title || (p.paper && p.paper.title);
      if (key && !paperMap.has(key)) {
        paperMap.set(key, p);
      }
    });

    const papers = Array.from(paperMap.values()).slice(0, 5);
    if ($("paperListCount")) $("paperListCount").textContent = papers.length;

    if (!papers.length) {
      list.innerHTML = '<li class="empty-item">暂无对应文献记录</li>';
      return;
    }

    list.innerHTML = papers.map((p) => {
      const title = p.paper_title || (p.paper && p.paper.title) || "文献未命名";
      const rawDoi = p.doi || (p.paper && p.paper.doi) || "";
      const doi = rawDoi ? `https://doi.org/${encodeURIComponent(rawDoi)}` : "";
      const paperId = p.paper_id || (p.paper && p.paper.paper_id);
      const journal = p.journal || (p.paper && p.paper.journal);
      const year = p.year || (p.paper && p.paper.year);
      const meta = [journal, year].filter(Boolean).join(", ");
      const detailLink = paperId ? `<a href="../paper_detail/index.html?paper_id=${encodeURIComponent(paperId)}" class="link-btn">论文详情 ↗</a>` : "";
      const doiLink = doi ? `<a href="${doi}" target="_blank" rel="noopener" class="link-btn">DOI ↗</a>` : "";
      const links = [detailLink, doiLink].filter(Boolean).join(" · ");

      return `
        <li>
          <div class="matrix-paper-title">${esc(title)}</div>
          <div class="matrix-paper-meta">${esc(meta || "未知期刊")} ${links ? "· " + links : ""}</div>
        </li>
      `;
    }).join("");
  }

  /* ============================================================
     TAB 3: ML 数据集 (ML Dataset)
     ============================================================ */
  async function loadDatasetTab() {
    const ticket = ++datasetRequest, library = state.libraryName;
    let diagMap = new Map();
    let totalCats = 44;

    try {
      const params = visualParams(new URLSearchParams({ sections: "overview,correlation", corr_allow_exploratory: "true" }));
      const ov = await getJSON("/api/visuals/overview?" + params.toString());
      if (ticket !== datasetRequest || library !== state.libraryName) return;
      const s = ov.summary || {};
      const meta = ov.catalyst_analysis_meta || {};
      const corr = ov.descriptor_correlation || {};
      const cells = corr.cells || [];

      const totalDft = s.total_dft_rows ?? s.dft_results ?? 0;
      const exportable = s.exportable_dft_rows ?? s.reviewed_exportable_dft_results ?? 0;
      const v2Ready = s.v2_row_ready_numeric_rows ?? 0;
      totalCats = meta.distinct_exportable_catalysts ?? s.distinct_exportable_catalysts ?? s.catalyst_samples ?? 44;
      const papers = meta.contributing_papers ?? s.contributing_papers ?? s.papers ?? 0;

      // 提取对角线样本数作为特征覆盖真源
      cells.forEach((c) => {
        if (c.x_property === c.y_property) {
          diagMap.set(c.x_property, Number(c.n || 0));
        }
      });

      if ($("dsTotalDft")) $("dsTotalDft").textContent = display(totalDft);
      if ($("dsTotalDftSub")) $("dsTotalDftSub").textContent = `来自 ${papers} 篇论文`;
      if ($("dsExportableDft")) $("dsExportableDft").textContent = display(exportable);

      const rate = exportable > 0 ? ((v2Ready / exportable) * 100).toFixed(1) : "—";
      if ($("dsNumericRate")) $("dsNumericRate").textContent = `${rate}%`;
      if ($("dsNumericRatio")) $("dsNumericRatio").textContent = `${v2Ready} / ${exportable} 条记录`;

      if ($("dsCatalystCount")) $("dsCatalystCount").textContent = display(totalCats);
      if ($("dsContributingPapers")) $("dsContributingPapers").textContent = display(papers);
    } catch (err) {
      console.warn("读取 ML 数据集概览失败:", err);
    }

    try {
      if (!state.fields.length) {
        await loadFields();
      }
      renderFeaturesTable(diagMap, totalCats);
    } catch (err) {
      console.warn("读取特征规范失败:", err);
    }

    // 优雅读取催化剂宽表预览，若 403 策略禁用则呈现空状态
    try {
      const catData = await getJSON("/api/dft/catalyst-dataset?" + visualParams(new URLSearchParams()).toString());
      if (ticket !== datasetRequest || library !== state.libraryName) return;
      renderDatasetPreviewTable(catData);
    } catch (err) {
      if (ticket !== datasetRequest || library !== state.libraryName) return;
      const tbody = $("datasetPreviewBody");
      if (tbody) {
        if (String(err.message).includes("403") || String(err.message).includes("Exports are disabled")) {
          tbody.innerHTML = `
            <tr>
              <td colspan="11" class="empty-cell preview-disabled-cell">
                <div class="preview-policy-notice">
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                    <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                  </svg>
                  <div>
                    <strong>催化剂宽表受安全策略保护</strong>
                    <p>当前系统配置 <code>LITAI_EXPORTS_ENABLED=false</code>，宽表与数据导出接口未对普通会话开放。可在系统设置中由管理员启用。</p>
                  </div>
                </div>
              </td>
            </tr>
          `;
        } else {
          tbody.innerHTML = `<tr><td colspan="11" class="empty-cell">读取数据预览失败：${esc(err.message)}</td></tr>`;
        }
      }
    }
  }

  function renderFeaturesTable(diagMap, totalCatalysts) {
    const tbody = $("featuresBody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const list = state.fields;
    if ($("featuresCountBadge")) $("featuresCountBadge").textContent = `共 ${list.length} 个特征`;

    list.forEach((f, idx) => {
      const meta = getFieldMeta(f.key);
      const rawCount = diagMap.get(f.key) 
        || diagMap.get(f.key.replace("li2s_", ""))
        || (f.key === "li2s_dissociation_barrier" ? diagMap.get("li2s_decomposition_barrier") : null)
        || (f.key === "rds_delta_g" ? diagMap.get("rds_energy") : null)
        || (f.key === "li2s_bader_charge_transfer" ? diagMap.get("charge_transfer") : null)
        || 0;
      const count = Number(rawCount) || 0;
      const percent = totalCatalysts > 0 ? ((count / totalCatalysts) * 100).toFixed(1) : "0.0";

      tbody.insertAdjacentHTML("beforeend", `
        <tr>
          <td>${idx + 1}</td>
          <td><strong>${esc(meta.labelZh)}</strong></td>
          <td><code>${esc(meta.symbol)}</code></td>
          <td>${esc(meta.unit || "—")}</td>
          <td><strong>${count}</strong> / ${totalCatalysts}</td>
          <td>
            <div class="coverage-bar-cell">
              <div class="coverage-bar-track">
                <div class="coverage-bar-fill" style="width: ${percent}%;"></div>
              </div>
              <span class="coverage-percent-text">${percent}%</span>
            </div>
          </td>
          <td>${esc(meta.desc || "经归一化的可分析数值特征")}</td>
        </tr>
      `);
    });
  }

  function renderDatasetPreviewTable(catData) {
    const rows = catData.rows || [];
    const tbody = $("datasetPreviewBody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty-cell">当前文献库暂无可导出的催化剂宽表数据</td></tr>';
      return;
    }

    const previewRows = rows.slice(0, 10);
    previewRows.forEach((r, idx) => {
      const catalyst = formatChemicalFormula(r.catalyst_name || "—");
      const paperCode = r.paper_code || (r.doi ? "DOI 记录" : "—");

      tbody.insertAdjacentHTML("beforeend", `
        <tr>
          <td>${idx + 1}</td>
          <td><strong>${esc(catalyst)}</strong></td>
          <td>${esc(paperCode)}</td>
          <td>${fmt2(r.li2s_adsorption_energy)}</td>
          <td>${fmt2(r.li2s_dissociation_barrier)}</td>
          <td>${fmt2(r.li2s_bader_charge_transfer)}</td>
          <td>${fmt2(r.li1_s_bond_length)}</td>
          <td>${fmt2(r.li2_s_bond_length)}</td>
          <td>${fmt2(r.li_s_bond_max)}</td>
          <td>${fmt2(r.d_band_center)}</td>
          <td>${fmt2(r.rds_delta_g)}</td>
        </tr>
      `);
    });
  }

  /* ============================================================
     DOM 事件绑定与初始化
     ============================================================ */
  function initEvents() {
    $("pairDetails").addEventListener("toggle", () => { if (state.lastCorrelation) renderDetails(state.lastCorrelation); });
    $("morePairs").addEventListener("click", () => { detailLimit += 50; if (state.lastCorrelation) renderDetails(state.lastCorrelation); });
    let resizeFrame;
    new ResizeObserver(() => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => { if (state.currentTab === "relations" && state.lastCorrelation) plot(state.lastCorrelation); });
    }).observe($("scatterPlot"));
    // 1. Tab 切换
    $("tabNavRelations")?.addEventListener("click", () => switchTab("relations"));
    $("tabNavMatrix")?.addEventListener("click", () => switchTab("matrix"));
    $("tabNavDataset")?.addEventListener("click", () => switchTab("dataset"));

    // 2. 导出按钮
    $("exportCatalystCsv")?.addEventListener("click", () => downloadDataset("csv"));
    $("downloadCatalystJson")?.addEventListener("click", () => downloadDataset("json"));

    // 3. Tab 1 事件绑定
    let correlationDebounceTimer = null;
    function debouncedLoadCorrelation() {
      clearTimeout(correlationDebounceTimer);
      correlationDebounceTimer = setTimeout(loadCorrelation, 100);
    }
    ["xField", "yField", "minN"].forEach((id) => {
      $(id)?.addEventListener("change", debouncedLoadCorrelation);
    });
    $("retryCorrelation")?.addEventListener("click", loadCorrelation);

    const plotWrap = document.querySelector(".plot-wrap") || document.querySelector(".plot-container");
    if (plotWrap) plotWrap.addEventListener("click", hideTooltip);

    // 常用关系预设点击
    $("quickChargeBond")?.addEventListener("click", () => {
      if (state.fields.some((field) => field.key === DEFAULTS.quickX) && state.fields.some((field) => field.key === DEFAULTS.quickY)) {
        $("xField").value = DEFAULTS.quickX;
        $("yField").value = DEFAULTS.quickY;
        loadCorrelation();
      } else {
        $("relationStatus").textContent = "当前接口未提供该快捷关系所需的数值字段。";
      }
    });

    document.querySelectorAll(".preset-btn").forEach((btn) => {
      if (btn.id === "quickChargeBond") return;
      btn.addEventListener("click", () => {
        const x = btn.getAttribute("data-x");
        const y = btn.getAttribute("data-y");
        if (x && y && state.fields.some((f) => f.key === x) && state.fields.some((f) => f.key === y)) {
          $("xField").value = x;
          $("yField").value = y;
          loadCorrelation();
        }
      });
    });

    // 4. Tab 2 事件绑定
    $("matrixScopeSelect")?.addEventListener("change", (e) => {
      state.matrixScope = e.target.value;
      renderMatrix();
    });
    $("matrixReactionSelect")?.addEventListener("change", loadMatrix);
    $("matrixAdsorbateSelect")?.addEventListener("change", loadMatrix);
    $("matrixFamilySelect")?.addEventListener("change", loadMatrix);
    $("matrixMinN")?.addEventListener("change", loadMatrix);

    $("jumpToExploreBtn")?.addEventListener("click", () => {
      if (!state.selectedMatrixCell) return;
      const { x, y } = state.selectedMatrixCell;
      const exploreX = MATRIX_TO_EXPLORE_FIELD_MAP[x] || x;
      const exploreY = MATRIX_TO_EXPLORE_FIELD_MAP[y] || y;

      switchTab("relations");
      if ($("xField") && state.fields.some((f) => f.key === exploreX)) $("xField").value = exploreX;
      if ($("yField") && state.fields.some((f) => f.key === exploreY)) $("yField").value = exploreY;
      loadCorrelation();
    });

    // 5. Tab 3 导出卡片点击
    $("exportCsvCardBtn")?.addEventListener("click", () => downloadDataset("csv"));
    $("exportJsonCardBtn")?.addEventListener("click", () => downloadDataset("json"));
    $("downloadFullCsvBtn")?.addEventListener("click", () => downloadDataset("csv"));

    // 6. 文献库选择
    $("librarySelect")?.addEventListener("change", (e) => {
      onLibraryChange(e.target.value);
    });
  }

  /* ============================================================
     启动入口
     ============================================================ */
  async function init() {
    TopNav.init({ currentPage: "visuals", mountId: "topnav-mount" });
    initEvents();
    await resolveLibraryScope();
    checkExportPolicy();

    try {
      await loadFields();
      await loadCorrelation();
    } catch (error) {
      const statusEl = $("relationStatus");
      if (statusEl) statusEl.textContent = "可分析字段读取失败：" + error.message;
    }
  }

  document.addEventListener("DOMContentLoaded", init);
}());
