    const state = {
      rows: [],
      metadata: {},
      libraries: [],
      scopeRuns: [],
      pagination: {
        page: 1,
        pageSize: 25
      }
    };
    const CURRENT_LIBRARY_STORAGE_KEY = "litai_current_library";
    const REVIEW_CENTER_FILTER_SESSION_KEY = "litai:review-center:filters:v1";
    const REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY = "litai:review-center:manual-chart-context:v1";
    const REVIEW_CENTER_FETCH_LIMITS = [5000, 500];
    const selectedPaperIds = new Set();
    const manualReviewContext = {
      paperId: "",
      paperCode: "",
      runId: "",
      mode: "",
      confirmed: false,
      scopeType: "",
      figureCount: null,
      tableCount: null,
      bundleId: "",
      bundleFingerprint: ""
    };
    const webAiReturnState = {
      mode: "dft",
      paperId: "",
      paperCode: "",
      validatedRawText: "",
      validationResponse: null,
      lastValidationIssues: []
    };
    let dftReviewPreview = null;
    let manualScopeMismatchMessage = "";
    const PROMPT_COPY_ACTIONS = {
      figure_table: {
        kind: "figure_table",
        sourceKind: "figure_table",
        label: "整篇主文+DFT相关SI图表审核提示词",
        targetScope: "main",
        scopeNote: [
          "目标：只选择一篇主文献；一次审核该主文全部图表、与 DFT 明确相关或可能相关的 SI 图片，以及 SI 表格。纯实验或与 DFT 无关的 SI 图片不进入本次范围。",
          "包内每个对象必须保留真实 source_paper_id、source_paper_code、source_document_type、page 和对象 ID；不得按 DFT 关键词跳过 SI 图表。",
          "只输出一份覆盖整包所有 figure/table 的 JSON；服务器按对象真实所属 paper_id 受控写回。"
        ].join("\\n")
      },
      main_figure: {
        kind: "figure",
        sourceKind: "main_figure",
        label: "主文图片审核提示词",
        targetScope: "main",
        scopeNote: [
          "目标：只审核当前唯一主文献的图片和主文 PDF 证据。",
          "- 不处理支撑文献/SI 图片、表格或其他模块。",
          "- 只有图片或图注明确给出可结构化的 DFT 结果时，才创建未验证 DFT 候选并保留图片证据。"
        ].join("\n")
      },
      support_figure: {
        kind: "figure",
        sourceKind: "support_figure",
        label: "支撑文献图片审核提示词",
        targetScope: "support",
        scopeNote: [
          "目标：只审核当前唯一支撑文献/SI 的图片和 SI PDF 证据。",
          "- 不处理主文图片、表格或其他模块。",
          "- 只有图片或图注明确给出可结构化的 DFT 结果时，才创建未验证 DFT 候选。",
          "- DFT 候选按 codex context 的 writeback_paper_id 写回主文，并保留 SI paper_id、figure_id、页码和 source_document_type。"
        ].join("\n")
      },
      table: {
        kind: "table",
        sourceKind: "table",
        label: "表格审核提示词",
        targetScope: "main",
        scopeNote: [
          "目标：只审核当前唯一主文献及其已关联 SI 的表格。",
          "- SI 表格修改必须使用该表真实 paper_id。",
          "- 只处理表格对象；其他问题交给对应专项任务。"
        ].join("\n")
      },
      dft: {
        kind: "dft",
        sourceKind: "dft",
        label: "DFT 数据审核与入库提示词",
        targetScope: "main",
        scopeNote: [
          "目标：只审核当前唯一主文献的 DFT 数据及其已关联 SI 证据。",
          "- 核对已有候选，发现漏项时创建 new_candidate。",
          "- 一份证据合格的 AI 意见即可通过受控入口直接确认、修正、拒绝或新增；NEEDS_HUMAN 留待用户判断。",
          "- 不需要第二 AI、主 AI 或按 AI 身份计票。"
        ].join("\n")
      },
      text_review: {
        kind: "text_review",
        sourceKind: "text_review",
        label: "论文内容审核提示词",
        targetScope: "main",
        scopeNote: "目标：只审核当前唯一主文献的正文、机理内容和论文重点；请按 PDF 证据核对后，通过后端支持的 text_review 任务回写。"
      }
    };
    const conflictState = {
      activeRow: null,
      groups: [],
      activeGroupIndex: 0,
      listCollapsed: false,
      activeOpinionKey: null,
      evidenceCache: {},
      requestToken: 0
    };
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

    function readManualReviewContext() {
      try {
        const stored = JSON.parse(window.sessionStorage.getItem(REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY) || "null");
        return stored && typeof stored === "object" ? stored : null;
      } catch (_) {
        return null;
      }
    }

    function persistManualReviewContext() {
      try {
        window.sessionStorage.setItem(REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY, JSON.stringify(manualReviewContext));
      } catch (_) {
        // Keep the URL contract usable when sessionStorage is unavailable.
      }
    }

    function activeEvidenceScope() {
      return manualReviewContext.confirmed && manualReviewContext.mode === "evidence" && Boolean(manualReviewContext.paperId);
    }

    function resetManualReviewContext() {
      Object.assign(manualReviewContext, {
        paperId: "", paperCode: "", runId: "", mode: "", confirmed: false,
        scopeType: "", figureCount: null, tableCount: null, bundleId: "", bundleFingerprint: ""
      });
    }

    function replaceReviewCenterTargetUrl(paperId) {
      const url = new URL(window.location.href);
      if (paperId) url.searchParams.set("paper_id", String(paperId));
      ["run_id", "chart_run_id", "mode", "scope"].forEach(function(key) { url.searchParams.delete(key); });
      window.history.replaceState({}, "", url.pathname + "?" + url.searchParams.toString() + url.hash);
    }

    function clearMismatchedManualReviewContext(target) {
      const selectedPaperId = String(target && target.paper_id || "").trim();
      const scopedPaperId = String(manualReviewContext.paperId || "").trim();
      if (!selectedPaperId || !scopedPaperId || selectedPaperId === scopedPaperId) return false;
      const stalePaper = String(manualReviewContext.paperCode || scopedPaperId).trim();
      const selectedPaper = String(target.paper_code || selectedPaperId).trim();
      manualScopeMismatchMessage = "当前范围属于 " + stalePaper + "，已清除；请从 " + selectedPaper + " 重新选择批次。";
      resetManualReviewContext();
      state.scopeRuns = [];
      webAiReturnState.paperId = "";
      webAiReturnState.paperCode = "";
      webAiReturnState.validatedRawText = "";
      webAiReturnState.validationResponse = null;
      webAiReturnState.lastValidationIssues = [];
      try { window.sessionStorage.removeItem(REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY); } catch (_) {}
      replaceReviewCenterTargetUrl(selectedPaperId);
      clearWebAiReturnTransientState(manualScopeMismatchMessage);
      showToast(manualScopeMismatchMessage, "error");
      return true;
    }

    function requireSelectedMainEvidenceScope(target) {
      if (!target) {
        showToast("请先只选择一篇主文献。", "error");
        return false;
      }
      if (!activeEvidenceScope() || String(manualReviewContext.paperId) !== String(target.paper_id) || manualReviewContext.runId) {
        // Normal flow is paper-scoped: selecting the main paper implicitly
        // selects one aggregate scope. Run/history selection remains in the
        // advanced audit area and cannot narrow the normal export.
        resetManualReviewContext();
        Object.assign(manualReviewContext, {
          paperId: String(target.paper_id),
          paperCode: String(target.paper_code || ""),
          mode: "evidence",
          confirmed: true,
          scopeType: "paper"
        });
        persistManualReviewContext();
        updateManualReviewUrl();
        renderManualReviewScope();
      }
      return true;
    }

    function evidenceScopeLabel() {
      const figures = manualReviewContext.figureCount == null ? "?" : manualReviewContext.figureCount;
      const tables = manualReviewContext.tableCount == null ? "?" : manualReviewContext.tableCount;
      const range = manualReviewContext.runId ? "指定 run" : "整篇论文";
      return "本次将审核/修改：" + (manualReviewContext.paperCode || "-") + "、" + range + "、" + figures + " 图、" + tables + " 表";
    }

    function updateManualReviewUrl() {
      if (!manualReviewContext.paperId) {
        renderManualReviewScope();
        return;
      }
      const url = new URL(window.location.href);
      url.searchParams.set("paper_id", manualReviewContext.paperId);
      if (manualReviewContext.runId) {
        url.searchParams.set("run_id", manualReviewContext.runId);
        url.searchParams.set("chart_run_id", manualReviewContext.runId);
      } else {
        url.searchParams.delete("run_id");
        url.searchParams.delete("chart_run_id");
      }
      if (manualReviewContext.mode) url.searchParams.set("mode", manualReviewContext.mode);
      if (manualReviewContext.scopeType === "paper" && manualReviewContext.confirmed) url.searchParams.set("scope", "paper");
      else url.searchParams.delete("scope");
      window.history.replaceState({}, "", url.pathname + "?" + url.searchParams.toString() + url.hash);
    }

    function renderManualReviewScope() {
      const card = document.getElementById("manualReviewScopeCard");
      const details = document.getElementById("manualReviewScopeDetails");
      if (!card || !details) return;
      const ready = activeEvidenceScope();
      card.classList.toggle("is-unconfirmed", !ready);
      if (!ready) {
        details.textContent = manualScopeMismatchMessage || "请先选择 AI 批次或明确选择整篇论文审核；已禁用导出图表证据包和回传图表 JSON。";
      } else {
        const scope = manualReviewContext.runId ? "当前 AI 批次" : "整篇论文";
        details.textContent = "论文编号 " + (manualReviewContext.paperCode || manualReviewContext.paperId) +
          " | " + scope +
          " | " + (manualReviewContext.figureCount == null ? "图数待读取" : manualReviewContext.figureCount + " 图") +
          "，" + (manualReviewContext.tableCount == null ? "表数待读取" : manualReviewContext.tableCount + " 表");
      }
      ["exportEvidenceWorkflowOption", "returnEvidenceWorkflowOption"].forEach(function(id) {
        const option = document.getElementById(id);
        if (option) option.disabled = !ready;
      });
      const wholePaperButton = document.getElementById("wholePaperScopeButton");
      if (wholePaperButton) wholePaperButton.disabled = !selectedWebAiReturnTarget();
    }

    function restoreManualReviewContext() {
      const params = new URLSearchParams(window.location.search);
      let stored = readManualReviewContext();
      const urlPaperId = String(params.get("paper_id") || "").trim();
      const urlRunId = String(params.get("chart_run_id") || params.get("run_id") || "").trim();
      const urlMode = String(params.get("mode") || "").trim();
      const urlScope = String(params.get("scope") || "").trim();
      let next = null;
      if (urlPaperId && stored && stored.paperId && String(stored.paperId) !== urlPaperId) {
        const stalePaper = String(stored.paperCode || stored.paperId).trim();
        manualScopeMismatchMessage = "当前范围属于 " + stalePaper + "，已清除；请从当前主文重新选择批次。";
        try { window.sessionStorage.removeItem(REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY); } catch (_) {}
        stored = null;
        state.scopeRuns = [];
        webAiReturnState.paperId = "";
        webAiReturnState.paperCode = "";
        webAiReturnState.validatedRawText = "";
        webAiReturnState.validationResponse = null;
      }
      if (urlPaperId && urlRunId) {
        next = {
          paperId: urlPaperId,
          paperCode: stored && stored.paperId === urlPaperId ? stored.paperCode || "" : "",
          runId: urlRunId,
          mode: urlMode || "evidence",
          confirmed: true,
          scopeType: "external_analysis_run",
          figureCount: null,
          tableCount: null,
          bundleId: stored && stored.paperId === urlPaperId && stored.runId === urlRunId ? stored.bundleId || "" : "",
          bundleFingerprint: stored && stored.paperId === urlPaperId && stored.runId === urlRunId ? stored.bundleFingerprint || "" : ""
        };
      } else if (urlPaperId && urlScope === "paper") {
        next = {
          paperId: urlPaperId,
          paperCode: stored && stored.paperId === urlPaperId ? stored.paperCode || "" : "",
          runId: "",
          mode: urlMode || "evidence",
          confirmed: true,
          scopeType: "paper",
          figureCount: null,
          tableCount: null,
          bundleId: "",
          bundleFingerprint: ""
        };
      } else if (!urlPaperId && stored && stored.paperId && stored.runId && stored.mode === "evidence") {
        // Bare/top navigation may restore an explicitly chosen batch, but never a paper scope.
        next = stored;
      } else {
        resetManualReviewContext();
        if (stored && (!stored.runId || stored.mode !== "evidence")) {
          try { window.sessionStorage.removeItem(REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY); } catch (_) {}
        }
      }
      if (next) Object.assign(manualReviewContext, next);
      if (manualReviewContext.paperId) {
        persistManualReviewContext();
        updateManualReviewUrl();
      }
      renderManualReviewScope();
      renderReviewScopeChooser();
    }

    function updateManualReviewContextFromRows() {
      const target = selectedWebAiReturnTarget();
      if (target) clearMismatchedManualReviewContext(target);
      if (target && manualScopeMismatchMessage.includes("当前主文")) {
        manualScopeMismatchMessage = manualScopeMismatchMessage.replace("当前主文", String(target.paper_code || "当前主文献"));
      }
      if (!manualReviewContext.paperId) return;
      const row = (state.rows || []).find(function(item) { return String(item.paper_id || "") === manualReviewContext.paperId; });
      if (row && row.paper_code) manualReviewContext.paperCode = String(row.paper_code);
      persistManualReviewContext();
      renderManualReviewScope();
      renderReviewScopeChooser();
    }

    function scopeTargetCounts(run) {
      if (run && run.counts && (run.counts.figures != null || run.counts.tables != null)) {
        return { figures: Number(run.counts.figures || 0), tables: Number(run.counts.tables || 0) };
      }
      const figureIds = new Set();
      const tableIds = new Set();
      function visit(value, inheritedType) {
        if (Array.isArray(value)) {
          value.forEach(function(item) { visit(item, inheritedType); });
          return;
        }
        if (!value || typeof value !== "object") return;
        const type = String(value.target_type || value.object_type || inheritedType || "").toLowerCase();
        const figureId = value.figure_id || value.figure_uuid || (type.includes("figure") ? value.target_id || value.object_id || value.source_record_id : "");
        const tableId = value.table_id || value.table_uuid || (type.includes("table") ? value.target_id || value.object_id || value.source_record_id : "");
        if (figureId) figureIds.add(String(figureId));
        if (tableId) tableIds.add(String(tableId));
        if (value.target_path || value.path) {
          [value.target_path, value.path].forEach(function(pathValue) {
            const parts = String(pathValue || "").split(":");
            if (parts.length >= 2 && /^figures?$/i.test(parts[0])) figureIds.add(parts[1]);
            if (parts.length >= 2 && /^tables?$/i.test(parts[0])) tableIds.add(parts[1]);
          });
        }
        Object.keys(value).forEach(function(key) { visit(value[key], type); });
      }
      (run && run.candidates || []).forEach(function(candidate) {
        if (String(candidate.candidate_type || "").toLowerCase() === "dft" || String(candidate.candidate_type || "").toLowerCase().includes("dft")) return;
        visit(candidate.normalized_payload, "");
        visit(candidate.evidence_payload, "");
      });
      return { figures: figureIds.size, tables: tableIds.size };
    }

    function scopeRunPaper(run) {
      return (state.rows || []).find(function(row) { return String(row.paper_id || "") === String(run && run.paper_id || ""); }) || null;
    }

    function renderReviewScopeChooser() {
      const list = document.getElementById("reviewScopeRunList");
      const wholePaperButton = document.getElementById("wholePaperScopeButton");
      if (!list) return;
      const target = selectedWebAiReturnTarget();
      if (wholePaperButton) wholePaperButton.disabled = !target;
      const runs = (state.scopeRuns || []).filter(function(run) {
        if (manualReviewContext.paperId && String(run.paper_id || "") !== String(manualReviewContext.paperId)) return false;
        return scopeTargetCounts(run).figures > 0 || scopeTargetCounts(run).tables > 0;
      });
      if (!runs.length) {
        list.innerHTML = '<div class="muted">当前没有可用于图表审核的 AI 批次。请先从任务中心产生图表证据候选。</div>';
        return;
      }
      const activeRuns = runs.filter(function(run) {
        return ["not_started", "needs_local_ai", "stale", "completed", "not_required"].includes(String(run.stage_status || ""));
      });
      const historyRuns = runs.filter(function(run) { return !activeRuns.includes(run); });
      const renderRun = function(run) {
        const counts = scopeTargetCounts(run);
        const row = scopeRunPaper(run);
        const source = String(run.source_label || run.source || "AI");
        const created = run.created_at ? new Date(run.created_at).toLocaleString("zh-CN") : "时间未知";
        const candidateCount = run.candidate_count != null ? run.candidate_count : (Array.isArray(run.candidates) ? run.candidates.length : 0);
        const runId = String(run.chart_run_id || run.id || "");
        const selected = activeEvidenceScope() && manualReviewContext.runId === runId;
        const status = String(run.stage_status || "待读取");
        return '<button class="review-scope-run-option" type="button" data-run-id="' + esc(runId) + '" onclick="selectRunScope(\'' + esc(runId) + '\')"' + (selected ? ' aria-current="true"' : '') + '>' +
          '<div class="review-scope-run-main">' + esc(row && row.paper_code || "主文献") + ' · ' + esc(source) + (selected ? ' · 当前选择' : '') + '</div>' +
          '<div class="review-scope-run-meta">' + esc(created) + ' · ' + esc(status) + ' · 候选 ' + candidateCount + ' · ' + counts.figures + ' 图、' + counts.tables + ' 表</div>' +
          '</button>';
      };
      const recommendedRuns = [];
      const duplicateRuns = [];
      const duplicateGroups = new Map();
      activeRuns.forEach(function(run) {
        const groupKey = String(run.duplicate_group_key || run.chart_run_id || run.id || "");
        const group = duplicateGroups.get(groupKey) || [];
        group.push(run);
        duplicateGroups.set(groupKey, group);
      });
      duplicateGroups.forEach(function(group) {
        const representative = group.find(function(run) { return Boolean(run.is_duplicate_representative); }) || group[0];
        recommendedRuns.push(representative);
        group.forEach(function(run) {
          if (run !== representative) duplicateRuns.push(run);
        });
      });
      const pendingRuns = recommendedRuns.filter(function(run) {
        return !["completed", "not_required"].includes(String(run.stage_status || ""));
      });
      const completedRuns = recommendedRuns.filter(function(run) {
        return ["completed", "not_required"].includes(String(run.stage_status || ""));
      });
      const pendingCard = pendingRuns.length
        ? '<div class="muted" style="margin:10px 0 6px;">待补充图表（' + pendingRuns.length + ' 条）</div>' + pendingRuns.map(renderRun).join("")
        : '<div class="muted" style="margin:10px 0 6px;">当前没有待补充图表。</div>';
      const completedCard = completedRuns.length
        ? '<details class="review-scope-history"><summary>已完成历史（' + completedRuns.length + ' 条）</summary>' + completedRuns.map(renderRun).join("") + '</details>'
        : "";
      const duplicateCount = duplicateRuns.length;
      list.innerHTML = pendingCard + completedCard + (duplicateCount
        ? '<details class="review-scope-history"><summary>历史重复执行（' + duplicateCount + ' 条）</summary>' + duplicateRuns.map(renderRun).join("") + '</details>'
        : "") + (historyRuns.length
        ? '<details class="review-scope-history"><summary>更多历史批次（' + historyRuns.length + '）</summary>' + historyRuns.map(renderRun).join("") + '</details>'
        : "");
    }

    async function loadReviewScopeCandidates() {
      const list = document.getElementById("reviewScopeRunList");
      try {
        const paperId = String(manualReviewContext.paperId || (new URLSearchParams(window.location.search).get("paper_id") || "")).trim();
        const runs = paperId
          ? await fetchJSON("/api/papers/" + encodeURIComponent(paperId) + "/chart-review-scopes")
          : { chart_runs: [] };
        state.scopeRuns = runs && Array.isArray(runs.chart_runs) ? runs.chart_runs : [];
        if (activeEvidenceScope() && manualReviewContext.runId && !state.scopeRuns.some(function(run) {
          return String(run.chart_run_id || run.id || "") === String(manualReviewContext.runId);
        })) {
          const paperCode = manualReviewContext.paperCode || "当前主文献";
          resetManualReviewContext();
          state.scopeRuns = [];
          manualScopeMismatchMessage = "当前范围所属批次已失效，已清除；请从 " + paperCode + " 重新选择批次。";
          try { window.sessionStorage.removeItem(REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY); } catch (_) {}
          renderManualReviewScope();
        }
        renderReviewScopeChooser();
      } catch (error) {
        state.scopeRuns = [];
        if (list) list.innerHTML = '<div class="error">AI 批次读取失败：' + esc(error.message) + '</div>';
      }
    }

    function selectRunScope(runId) {
      const run = (state.scopeRuns || []).find(function(item) { return String(item.chart_run_id || item.id) === String(runId); });
      const row = run ? scopeRunPaper(run) : null;
      if (!run || !row) {
        showToast("当前批次没有对应的主文献行，无法固定审核范围。");
        return;
      }
      const counts = scopeTargetCounts(run);
      resetManualReviewContext();
      Object.assign(manualReviewContext, {
        paperId: String(run.paper_id),
        paperCode: String(row.paper_code || ""),
        runId: String(run.chart_run_id || run.id),
        mode: "evidence",
        confirmed: true,
        scopeType: "external_analysis_run",
        figureCount: counts.figures,
        tableCount: counts.tables
      });
      manualScopeMismatchMessage = "";
      selectedPaperIds.clear();
      selectedPaperIds.add(String(run.paper_id));
      persistManualReviewContext();
      updateManualReviewUrl();
      renderRows();
      renderManualReviewScope();
      renderReviewScopeChooser();
      refreshManualReviewScope();
    }

    async function selectWholePaperScope() {
      const target = selectedWebAiReturnTarget();
      if (!target) {
        showToast("请先选择一篇主文献，再明确选择整篇论文审核。");
        return;
      }
      if (!window.confirm("确认切换为整篇论文审核？这会审核该论文全部图表，不是某个 AI 批次。")) return;
      resetManualReviewContext();
      Object.assign(manualReviewContext, {
        paperId: String(target.paper_id),
        paperCode: String(target.paper_code || ""),
        mode: "evidence",
        confirmed: true,
        scopeType: "paper"
      });
      manualScopeMismatchMessage = "";
      persistManualReviewContext();
      updateManualReviewUrl();
      renderManualReviewScope();
      renderReviewScopeChooser();
      await refreshManualReviewScope();
    }

    async function refreshManualReviewScope() {
      if (!activeEvidenceScope()) return;
      const contextKey = manualReviewContext.paperId + ":" + manualReviewContext.runId;
      const params = new URLSearchParams();
      if (manualReviewContext.runId) params.set("run_id", manualReviewContext.runId);
      try {
        const task = await fetchJSON("/api/papers/" + encodeURIComponent(manualReviewContext.paperId) + "/chart-review-task" + (params.toString() ? "?" + params.toString() : ""));
        if (contextKey !== manualReviewContext.paperId + ":" + manualReviewContext.runId) return;
        manualReviewContext.scopeType = String(task.scope_type || (manualReviewContext.runId ? "external_analysis_run" : "paper"));
        manualReviewContext.figureCount = Number(task.counts && task.counts.figures || 0);
        manualReviewContext.tableCount = Number(task.counts && task.counts.tables || 0);
        if (task.paper_code) manualReviewContext.paperCode = String(task.paper_code);
        persistManualReviewContext();
        renderManualReviewScope();
      } catch (error) {
        console.error("Unable to read fixed chart review scope", error);
        renderManualReviewScope();
      }
    }

    function esc(value) {
      return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char];
      });
    }

    function isSupportingInformationRow(row) {
      const key = String(row && row.paper_type || "").trim().toLowerCase();
      return ["supplementary", "supplementary_information", "supporting_information", "si"].includes(key);
    }

    function isSupplementaryRelationshipType(value) {
      const key = String(value || "").trim().toLowerCase();
      return ["supplementary", "supplementary_information", "supporting_information", "si"].includes(key);
    }

    function supplementaryGroup(row) {
      const group = row && row.supplementary_group;
      return group && typeof group === "object" ? group : null;
    }

    function applyLiveChartStageToSupplementaryGroup(paperId, stage) {
      const targetPaperId = String(paperId || "");
      if (!targetPaperId) return false;
      const targetRow = state.rows.find(function (row) {
        return String(row && row.paper_id || "") === targetPaperId;
      });
      if (!targetRow) return false;
      const targetGroup = supplementaryGroup(targetRow);
      const mainPaperId = String(targetGroup && targetGroup.main_paper_id || targetRow.paper_id || "");
      let updated = false;
      state.rows.forEach(function (row) {
        const group = supplementaryGroup(row);
        const rowMainPaperId = String(group && group.main_paper_id || row && row.paper_id || "");
        if (rowMainPaperId !== mainPaperId) return;
        row._live_chart_stage = String(stage || "unknown");
        updated = true;
      });
      return updated;
    }

    function supplementaryGroupLabel(row) {
      const group = supplementaryGroup(row);
      if (!group) return "";
      const mainCode = group.main_paper_code || (group.main_paper_id ? String(group.main_paper_id).slice(0, 8) : "主文献");
      if (group.role === "supplementary") return "SI 归属 " + mainCode;
      const supportCodes = (group.support_papers || []).map(function (item) {
        return item.paper_code || (item.paper_id ? String(item.paper_id).slice(0, 8) : "");
      }).filter(Boolean);
      return supportCodes.length ? ("含 SI " + supportCodes.join(" + ")) : "";
    }

    function supplementaryGroupTip(row) {
      const group = supplementaryGroup(row);
      if (!group) return "";
      const mainCode = group.main_paper_code || "主文献";
      const supportActive = toCount(
        group.support_dft_lifecycle_open_count != null
          ? group.support_dft_lifecycle_open_count
          : group.support_active_dft_candidate_count
      );
      const mainActive = toCount(group.main_active_dft_candidate_count);
      const groupTotal = toCount(group.dft_candidate_count);
      const groupActive = toCount(group.active_dft_candidate_count);
      const supportLifecycleLabel = group.support_dft_lifecycle_label || "SI 证据待闭环";
      if (group.role === "supplementary") {
        return "这行是 " + mainCode + " 的支撑文献记录；主文献待处理 DFT " + mainActive + " 条，" + supportLifecycleLabel + " " + supportActive + " 条，规范化新行需回写主文；同步组总候选 " + groupTotal + " 条。";
      }
      return "这是主文献行；同步组总候选 " + groupTotal + " 条，组内待处理 " + groupActive + " 条，其中 " + supportLifecycleLabel + " " + supportActive + " 条，规范化新行需回写主文。";
    }

    function getValue(id) {
      const el = document.getElementById(id);
      return el ? el.value.trim() : "";
    }

    function collectReviewCenterFilterState() {
      return {
        libraryFilter: getValue("libraryFilter"),
        statusFilter: getValue("statusFilter"),
        workflowStatusFilter: getValue("workflowStatusFilter"),
        qualityFilter: getValue("qualityFilter"),
        sortFilter: getValue("sortFilter"),
        searchBox: getValue("searchBox"),
        pagination: {
          page: Number(state.pagination && state.pagination.page || 1),
          pageSize: Number(state.pagination && state.pagination.pageSize || 25),
        },
      };
    }

    function saveReviewCenterFilterState() {
      try {
        window.sessionStorage.setItem(REVIEW_CENTER_FILTER_SESSION_KEY, JSON.stringify(collectReviewCenterFilterState()));
      } catch (_) {
        // sessionStorage can be unavailable in strict browser modes.
      }
    }

    function restoreReviewCenterFilterState() {
      try {
        const raw = window.sessionStorage.getItem(REVIEW_CENTER_FILTER_SESSION_KEY);
        if (!raw) return;
        const saved = JSON.parse(raw);
        if (!saved || typeof saved !== "object") return;
        state.restoredReviewCenterFilters = saved;
        ["libraryFilter", "statusFilter", "workflowStatusFilter", "qualityFilter", "sortFilter", "searchBox"].forEach(function(id) {
          const el = document.getElementById(id);
          if (el && Object.prototype.hasOwnProperty.call(saved, id)) {
            el.value = saved[id] == null ? "" : String(saved[id]);
          }
        });
        const savedPage = Number(saved.pagination && saved.pagination.page);
        const savedPageSize = Number(saved.pagination && saved.pagination.pageSize);
        if (Number.isFinite(savedPage) && savedPage >= 1) {
          state.pagination.page = savedPage;
        }
        if (Number.isFinite(savedPageSize) && savedPageSize > 0) {
          state.pagination.pageSize = savedPageSize;
        }
      } catch (_) {
        // Ignore corrupted session state and continue with defaults.
      }
    }

    function clearReviewCenterFilterState() {
      try {
        window.sessionStorage.removeItem(REVIEW_CENTER_FILTER_SESSION_KEY);
      } catch (_) {
        // sessionStorage can be unavailable in strict browser modes.
      }
    }

    function getStoredLibraryName() {
      try {
        return window.localStorage.getItem(CURRENT_LIBRARY_STORAGE_KEY) || "";
      } catch (_) {
        return "";
      }
    }

    function rememberLibraryName(name) {
      try {
        if (name) {
          window.localStorage.setItem(CURRENT_LIBRARY_STORAGE_KEY, name);
        } else {
          window.localStorage.removeItem(CURRENT_LIBRARY_STORAGE_KEY);
        }
      } catch (_) {
        // localStorage can be unavailable in strict browser modes.
      }
    }

    function getQueryLibraryName() {
      try {
        return new URLSearchParams(window.location.search).get("library_name") || "";
      } catch (_) {
        return "";
      }
    }

    function getQueryPaperId() {
      try {
        return new URLSearchParams(window.location.search).get("paper_id") || "";
      } catch (_) {
        return "";
      }
    }

    function relationshipPaperId(rel, direction) {
      if (!rel) return "";
      const key = direction === "incoming" ? "source_paper_id" : "target_paper_id";
      return String(rel[key] || rel.paper_id || "").trim();
    }

    async function loadFocusedSupportPairInfo(focusPaperId, rows) {
      const focusId = String(focusPaperId || "").trim();
      if (!focusId) return null;
      try {
        const row = (rows || []).find(function (item) {
          return String(item && item.paper_id || "") === focusId;
        });
        const group = supplementaryGroup(row);
        const memberIds = group && Array.isArray(group.member_paper_ids)
          ? group.member_paper_ids
          : [focusId];
        return {
          ids: new Set(memberIds.map(String)),
          mainPaperId: String(group && group.main_paper_id || focusId),
          focusPaperId: focusId
        };
      } catch (_) {
        return { ids: new Set([focusId]), mainPaperId: focusId, focusPaperId: focusId };
      }
    }

    function sortFocusedSupportPairRows(rows, pairInfo) {
      const mainPaperId = String(pairInfo && pairInfo.mainPaperId || "");
      return (rows || []).slice().sort(function (left, right) {
        const leftIsMain = String(left && left.paper_id || "") === mainPaperId;
        const rightIsMain = String(right && right.paper_id || "") === mainPaperId;
        if (leftIsMain !== rightIsMain) return leftIsMain ? -1 : 1;
        const leftIsSi = isSupportingInformationRow(left);
        const rightIsSi = isSupportingInformationRow(right);
        if (leftIsSi !== rightIsSi) return leftIsSi ? 1 : -1;
        return String(left && (left.paper_code || left.title || left.paper_id) || "").localeCompare(
          String(right && (right.paper_code || right.title || right.paper_id) || "")
        );
      });
    }

    function buildFocusedSupportPairLabel(rows) {
      const codes = (rows || []).map(function (row) {
        return String(row && (row.paper_code || row.human_ref || "") || "").trim();
      }).filter(Boolean);
      if (codes.length <= 1) return "";
      return "支撑绑定：" + codes.join(" + ");
    }

    function setReviewLibraryFilter() {
      rememberLibraryName(getValue("libraryFilter"));
      state.pagination.page = 1;
      saveReviewCenterFilterState();
      loadReviewCenter();
    }

    function updateStickyLayout() {
      window.requestAnimationFrame(function () {
        const panelHead = document.querySelector(".panel-head");
        document.body.style.setProperty("--review-panel-head-height", ((panelHead && panelHead.offsetHeight) || 0) + "px");
        syncExternalTableHead();
      });
    }

    function syncExternalTableHead() {
      const tableWrap = document.getElementById("tableWrap");
      const tableHead = document.getElementById("reviewTableHead");
      if (!tableWrap || !tableHead) return;
      tableHead.style.transform = "translateX(" + (-tableWrap.scrollLeft) + "px)";
    }

    function toCount(value) {
      const count = Number(value || 0);
      return Number.isFinite(count) ? count : 0;
    }

    function hasOwnValue(value) {
      return value !== null && value !== undefined;
    }

    function showToast(message) {
      const toast = document.getElementById("toast");
      toast.textContent = message;
      toast.style.display = "block";
      window.clearTimeout(showToast.timer);
      showToast.timer = window.setTimeout(function () {
        toast.style.display = "none";
      }, 2800);
    }

    async function copyTextToClipboard(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(text);
          return;
        } catch (error) {
          console.warn("navigator clipboard unavailable; using copy fallback", error);
        }
      }
      const box = document.createElement("textarea");
      box.value = text;
      box.style.position = "fixed";
      box.style.left = "-9999px";
      document.body.appendChild(box);
      box.focus();
      box.select();
      const copied = document.execCommand("copy");
      document.body.removeChild(box);
      if (!copied) throw new Error("clipboard copy was rejected");
    }

    function webAiModeLabel(mode) {
      if (mode === "evidence") return "图表证据整理";
      const reviewMode = String(
        (webAiReturnState.validationResponse && webAiReturnState.validationResponse.review_mode) ||
        (dftReviewPreview && dftReviewPreview.review_mode) || ""
      );
      return reviewMode === "comprehensive_review" ? "DFT 全量核验（已有+查漏）" :
        (reviewMode === "gap_discovery" ? "DFT 数据查漏" : "DFT 终审");
    }

    async function refreshDftReviewPreview() {
      const target = selectedWebAiReturnTarget();
      const box = document.getElementById("dftEvidenceSummary");
      if (!box) return;
      if (!target || !target.paper_id) {
        dftReviewPreview = null;
        box.querySelector(".manual-review-scope-details").textContent = "请选择一篇主文献；DFT 导出会自动聚合全部已完成且未过期的图表审核结果。";
        return;
      }
      try {
        dftReviewPreview = await fetchJSON("/api/papers/" + encodeURIComponent(target.paper_id) + "/dft-review-state");
        const summary = dftReviewPreview.summary || {};
        const modeLabel = dftReviewPreview.review_mode === "comprehensive_review" ? "DFT 全量核验（已有+查漏）" :
          (dftReviewPreview.review_mode === "gap_discovery" ? "DFT 数据查漏" : "DFT 终审");
        const exportOption = document.getElementById("exportDftWorkflowOption");
        const returnOption = document.getElementById("returnDftWorkflowOption");
        if (exportOption) exportOption.textContent = "4 导出 " + modeLabel + "包（仅已完成两级审核图片）";
        if (returnOption) returnOption.textContent = "5 回传 " + modeLabel + " JSON";
        const mainFigures = Number(summary.reviewed_main_figures || 0);
        const mainTables = Number(summary.reviewed_main_tables || 0);
        const supportingFigures = Math.max(0, Number(summary.reviewed_figures || 0) - mainFigures);
        const supportingTables = Math.max(0, Number(summary.reviewed_tables || 0) - mainTables);
        const pendingMainFigures = Number(summary.pending_main_figures || 0);
        const pendingSupportingFigures = Number(summary.pending_supporting_figures || 0);
        const gateStage = String(dftReviewPreview.review_gate && dftReviewPreview.review_gate.stage_status || "unknown");
        const chartStageUpdated = applyLiveChartStageToSupplementaryGroup(target.paper_id, gateStage);
        const supportingEvidence = [];
        if (supportingFigures) supportingEvidence.push("SI " + supportingFigures + " 图");
        if (supportingTables) supportingEvidence.push("SI " + supportingTables + " 表");
        box.querySelector(".manual-review-scope-details").textContent = gateStage === "completed" || gateStage === "not_required"
          ? modeLabel + "：已完成两级审核的证据为主文 " + mainFigures + " 图、" + mainTables + " 表" +
            (supportingEvidence.length ? "；" + supportingEvidence.join("、") : "") + "。"
          : modeLabel + "暂不可导出：图表阶段 " + gateStage + "。待完成两级审核：主文 " +
            pendingMainFigures + " 图、SI " + pendingSupportingFigures +
            " 图；必须先应用网页 AI 图表结果，再由本地 AI 逐图对照 PDF 核验。";
        if (chartStageUpdated) renderRows();
      } catch (error) {
        dftReviewPreview = null;
        box.querySelector(".manual-review-scope-details").textContent = "DFT 证据摘要读取失败：" + error.message;
      }
    }
    function apiErrorMessageFromPayload(data, status) {
      const detail = data && data.detail;
      if (typeof detail === "string") return detail;
      if (Array.isArray(detail)) {
        return detail.map(function (item) {
          if (item && typeof item === "object") {
            if (item.msg) return item.msg;
            if (item.message) return item.message;
            try {
              return JSON.stringify(item);
            } catch (_) {
              return String(item);
            }
          }
          return String(item);
        }).join("; ");
      }
      if (detail && typeof detail === "object") {
        if (detail.code === "chart_review_scope_selection_required") {
          return detail.message || "请选择图表审核批次；系统不会自动选择或退回整篇论文。";
        }
        if (detail.code === "figure_table_review_not_completed") {
          const gate = detail.figure_table_review || {};
          const stage = gate.stage_status || "unknown";
          const ragStatus = gate.rag_quality_status || (gate.rag_quality && gate.rag_quality.figures && gate.rag_quality.figures.status) || "";
          const blocked = gate.rag_quality && gate.rag_quality.figures ? Number(gate.rag_quality.figures.blocked || 0) : 0;
          if (ragStatus === "blocked" || blocked > 0) {
            return "图表证据阶段不能进入 DFT：仍有 " + blocked + " 个图表 RAG 不合格。请先在图表页补齐图类型、图摘要、关键元素或修复裁图，再重新回传图表 JSON。";
          }
          if (stage === "needs_local_ai") {
            return "图表证据阶段尚未完成：网页 AI 结果之后，仍有图片缺少本地 AI 逐图 PDF 核验。请执行第 3 步“本地 AI 全量图片复核”，完成后再导出 DFT 包。";
          }
          return "图表证据阶段尚未完成或已过期（stage_status=" + stage + "）。请依次完成网页 AI 图表审核和本地 AI 全量图片复核，再导出 DFT 包。";
        }
        if (detail.message) return detail.message;
        if (detail.code) return detail.code;
        try {
          return JSON.stringify(detail);
        } catch (_) {
          return String(detail);
        }
      }
      return data && data.message ? String(data.message) : ("HTTP " + status);
    }

    function buildWebAiEvidencePrompt() {
      return [
        "请解压这个图表证据整理包。第一步打开 START_HERE.md，然后打开 WEB_AI_FILL_THIS.json；禁止脱离该文件重新生成 JSON 结构。",
        "把 WEB_AI_FILL_THIS.json 作为唯一输出对象直接填写，先读 OUTPUT_RULES.json，再用 return_schema.json 自检。完成后保存为 <paper_code>_chart_review_result.json，并以 JSON 文件附件回复；不要把长 JSON 粘贴在聊天正文中，也不要输出 Markdown、解释文字或外层 wrapper。",
        "",
        "你的任务是把系统当前抽取的图片和表格还原成可信证据：",
        "- 硬约束：每一条 figure_actions/table_actions 都必须引用一个或多个包内真实 evidence_ids；从 parsed/extracted_figures.json、parsed/extracted_tables.json 或 manifest.json 复制，禁止编造或留空。",
        "- 图片 crop 正确填 KEEP；裁剪不完整或错位填 RECROP，并给出 page、bbox_norm 与真实 evidence_ids；",
        "- 每张保留的科学图片必须补齐具体 figure_role、content_summary、key_elements；缺任一项时不要只填 KEEP；",
        "- 漏图填 CREATE，并给出 source_paper_id、page、bbox_norm、caption、figure_role、content_summary、key_elements 与真实 evidence_ids；没有证据编号时不要创建。",
        "- 表格正确填 KEEP；表格缺行、缺列、跨页断裂或单位/脚注缺失填 UPDATE，并返回完整 complete_markdown 和真实 evidence_ids；",
        "- 漏表填 CREATE；删除填 DELETE；这两类也必须带真实 evidence_ids。无法判断时填 NEEDS_HUMAN，但 NEEDS_HUMAN 会保持待复核，不能完成图表阶段。",
        "- MERGE 只用于两个已有表格对象合并，必须填写 source_table_id 和 target_table_id，且二者不能相同；不要给同一个 table_id 输出多个 action。",
        "- dft_relevance 只能填写 none / possible / explicit_dft / unknown；不要写 true/false/yes/no/dft_relevant。",
        "",
        "只把图/表里明确写出的 DFT 数值作为 dft_evidence_candidates；不要从曲线、柱状图或颜色图估读数值。",
        "不要声称已经写库、verified、已确认或 ML_Ready。系统会校验 JSON 后从原 PDF 重新裁剪/回写。",
        "",
        "最终只回复一份严格符合 return_schema.json 的 JSON 文件附件，不要使用 Markdown 代码块、不要粘贴 JSON 正文、不要添加解释性文字。"
      ].join("\n");
    }

    function buildWebAiDftPrompt() {
      return [
        "请解压这个 DFT 审核包。第一步打开 START_HERE.md，然后打开 WEB_AI_FILL_THIS.json；禁止脱离该文件重新生成 JSON 结构。",
        "把 WEB_AI_FILL_THIS.json 作为唯一输出对象直接填写，先读 OUTPUT_RULES.json，再用 return_schema.json 自检。完成后保存为 <paper_code>_web_ai_result.json，并以 JSON 文件附件回复；不要把长 JSON 粘贴在聊天正文中，也不要输出 Markdown、解释文字或外层 wrapper。",
        "",
        "请先检查 parsed/curated_figure_table_evidence_snapshot.json：",
        "- 如果 stage_status 不是 completed/not_required，或 completed_snapshot_fingerprint 与当前快照不一致，图表证据还没有完成第一阶段闭环；不要产出 DFT 终审 JSON。",
        "- 如果 rag_quality_status=blocked 或 rag_quality.figures.blocked > 0，图表仍不满足 RAG 要求；不要产出 DFT 终审 JSON，应先回到图表证据整理补齐图类型、content_summary、key_elements 和裁图问题。",
        "- 如果已经 completed/not_required，请保留 return_template.json 中的聚合 fingerprint，并先读取 existing_terminal_context 去重。",
        "",
        "本包只需审核一次，并且必须在同一次结果中完成两个任务：",
        "1. 逐条核验已有 DFT 数据；2. 扫描全部 eligible_for_auto_apply=true 的正文、图、表证据并追加漏提数据。",
        "- 必须覆盖 return_template.coverage_acknowledgement.expected_target_ids / manifest.target_dft_result_ids 里的每一个已有 target_id；证据不足也要为该 target_id 返回 NEEDS_HUMAN；",
        "- 正确填 PASS；",
        "- 需要修正填 REVISE；",
        "- 错误或重复填 REJECT；",
        "- 处理完已有 target_id 后，必须继续逐项检查包内全部合格证据；发现真正未收录且有 reviewed evidence 的数据填 new_candidate；",
        "- 只有已有候选核验和全证据查漏都完成后，才把 coverage_acknowledgement.missing_data_search_complete 设为 true，并把 overall_status 设为 completed；",
        "- 硬约束：target_id=\"new\" 当且仅当 decision=\"new_candidate\"；PASS/REVISE/REJECT/NEEDS_HUMAN 必须使用 dft_review_checklist.json 中真实已有的 target_id；",
        "- new_candidate 必须对 existing_terminal_context 提供 dedupe_analysis（compared_target_ids、conclusion=distinct、reason）；",
        "- 无法确认填 NEEDS_HUMAN。",
        "不得猜测或补造数据，每条意见必须引用包内真实 evidence_id。unreviewed_supporting_context 只能返回 NEEDS_HUMAN，不能支持自动写回。正文、SI、表格或图片对同一计算结果的重复证据应与 existing_terminal_context 合并判断，不要重复创建 DFT 结果。",
        "",
        "输出文件前必须重新解析 JSON，并逐条检查 target_id 与 decision 的组合。最终只回复填写完成的 JSON 文件附件，不要粘贴 JSON 正文。"
      ].join("\n");
    }

    async function copyWebAiBundlePrompt(mode) {
      const normalizedMode = mode === "evidence" ? "evidence" : "dft";
      const prompt = normalizedMode === "evidence" ? buildWebAiEvidencePrompt() : buildWebAiDftPrompt();
      try {
        await copyTextToClipboard(prompt);
        showToast("网页 AI " + webAiModeLabel(normalizedMode) + "提示词已复制");
      } catch (error) {
        console.error(error);
        showToast("复制失败，请检查浏览器剪贴板权限");
      }
    }

    async function handleWebAiWorkflowSelect() {
      const select = document.getElementById("webAiWorkflowSelect");
      const action = String(select && select.value || "");
      if (select) select.value = "";
      if (!action) return;
      if (action === "export_evidence") {
        await downloadWebAiBundle("evidence");
      } else if (action === "copy_evidence_prompt") {
        await copyWebAiBundlePrompt("evidence");
      } else if (action === "return_evidence") {
        openWebAiReturnDialog("evidence");
      } else if (action === "copy_evidence_local_ai") {
        await copyLocalAiChartReviewInstructionFromMenu();
      } else if (action === "export_dft") {
        await downloadWebAiBundle("dft");
      } else if (action === "copy_dft_prompt") {
        await copyWebAiBundlePrompt("dft");
      } else if (action === "return_dft") {
        openWebAiReturnDialog("dft");
      }
    }

    function selectedWebAiSingleMainPaper() {
      const target = selectedWebAiReturnTarget();
      if (!target) {
        showToast("请先只选择一篇主文献。");
        return null;
      }
      return target;
    }

    function contentDispositionFilename(headerValue, fallback) {
      const value = String(headerValue || "");
      const utf8Match = value.match(/filename\*=UTF-8''([^;]+)/i);
      if (utf8Match) {
        try {
          return decodeURIComponent(utf8Match[1]);
        } catch (_) {
          return utf8Match[1];
        }
      }
      const plainMatch = value.match(/filename="?([^";]+)"?/i);
      return plainMatch ? plainMatch[1] : fallback;
    }

    async function downloadWebAiBundle(mode) {
      const normalizedMode = mode === "evidence" ? "evidence" : "dft";
      const target = selectedWebAiSingleMainPaper();
      if (!target) return;
      const paperId = String(target.paper_id || "");
      if (normalizedMode === "evidence" && !requireSelectedMainEvidenceScope(target)) return;
      const runId = manualReviewContext.runId;
      const paperCode = String(target.paper_code || paperId.slice(0, 8) || "paper");
      const endpoint = normalizedMode === "evidence"
        ? "/api/papers/" + encodeURIComponent(paperId) + "/evidence-review-bundle?include_pdf_files=true&include_figure_files=true" + (runId ? "&run_id=" + encodeURIComponent(runId) : "")
        : "/api/papers/" + encodeURIComponent(paperId) + "/dft-review-bundle?include_figure_files=true&chart_scope=paper";
      const selector = document.getElementById("webAiWorkflowSelect");
      const previousTitle = selector ? selector.title : "";
      if (selector) {
        selector.disabled = true;
        selector.title = "正在导出" + webAiModeLabel(normalizedMode) + "包...";
      }
      try {
        showToast("正在导出" + webAiModeLabel(normalizedMode) + "包...");
        const response = await fetch(endpoint, { method: "POST" });
        if (!response.ok) {
          let message = "HTTP " + response.status;
          try {
            const data = await response.json();
            message = apiErrorMessageFromPayload(data, response.status);
          } catch (_) {}
          throw new Error(message);
        }
        if (normalizedMode === "evidence") {
          const metadata = {
            scopeType: String(response.headers.get("X-LitAI-Review-Scope") || ""),
            runId: String(response.headers.get("X-LitAI-Review-Run-Id") || ""),
            figureCount: Number(response.headers.get("X-LitAI-Review-Figure-Count")),
            tableCount: Number(response.headers.get("X-LitAI-Review-Table-Count")),
            bundleId: String(response.headers.get("X-LitAI-Bundle-Id") || ""),
            bundleFingerprint: String(response.headers.get("X-LitAI-Bundle-Fingerprint") || "")
          };
          const expectedScope = manualReviewContext.runId ? "external_analysis_run" : "paper";
          if (
            metadata.scopeType !== expectedScope ||
            metadata.runId !== manualReviewContext.runId ||
            !metadata.bundleId || !metadata.bundleFingerprint ||
            !Number.isFinite(metadata.figureCount) || !Number.isFinite(metadata.tableCount)
          ) {
            throw new Error("后端返回的图表审核范围与当前固定范围不一致，已阻止下载；不会保存错误审核包。");
          }
          manualReviewContext.scopeType = metadata.scopeType;
          manualReviewContext.figureCount = metadata.figureCount;
          manualReviewContext.tableCount = metadata.tableCount;
          manualReviewContext.bundleId = metadata.bundleId;
          manualReviewContext.bundleFingerprint = metadata.bundleFingerprint;
          manualReviewContext.paperCode = paperCode;
          persistManualReviewContext();
          renderManualReviewScope();
        }
        const blob = await response.blob();
        const fallback = paperCode + (normalizedMode === "evidence" ? "_figure_table_evidence_review_bundle.zip" : "_dft_review_bundle.zip");
        const filename = contentDispositionFilename(response.headers.get("Content-Disposition"), fallback);
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        if (normalizedMode === "dft") {
          const responseMode = response.headers.get("X-LitAI-DFT-Review-Mode");
          const modeLabel = responseMode === "comprehensive_review" ? "DFT 全量核验（已有+查漏）" :
            (responseMode === "gap_discovery" ? "DFT 数据查漏" : "DFT 终审");
          showToast(modeLabel + "包已开始下载：已审核" +
            Number(response.headers.get("X-LitAI-Reviewed-Figures") || 0) + "图、" +
            Number(response.headers.get("X-LitAI-Reviewed-Tables") || 0) + "表；另有" +
            Number(response.headers.get("X-LitAI-Pending-Main-Figures") || 0) + "图待补充。"
          );
        } else {
          showToast(webAiModeLabel(normalizedMode) + "包已开始下载：" + filename);
        }
      } catch (error) {
        console.error(error);
        showToast("导出失败：" + error.message);
      } finally {
        if (selector) {
          selector.disabled = false;
          selector.title = previousTitle || "网页 AI 图表/DFT 离线审核流程";
        }
      }
    }

    function currentPromptTargets(rows) {
      const targetRows = rows || promptTargetRows();
      if (!targetRows.length) {
        return "";
      }
      return targetRows.map(function (row) {
        const role = isSupportingInformationRow(row) ? "supplementary_information" : "main_paper";
        const group = supplementaryGroup(row);
        const mainPaperId = group && group.main_paper_id ? group.main_paper_id : "";
        const mainPaperPart = role === "supplementary_information" && mainPaperId
          ? " | main_paper_id: " + mainPaperId
          : "";
        return "- human_ref: " + (row.paper_code || "-") +
          " | paper_id: " + row.paper_id +
          " | role: " + role +
          mainPaperPart +
          " | type: " + (row.paper_type || "-") +
          " | title: " + (row.title || "未命名文献");
      }).join("\n");
    }

    function currentPromptTargetRows() {
      const rows = promptTargetRows();
      if (!rows.length) {
        throw new Error("请先勾选一篇文献作为任务目标。");
      }
      return rows;
    }

    function inferReactionProfileFromText(text) {
      const value = String(text || "").toLowerCase();
      if (!value) return "UNKNOWN";
      if (
        /\b(li[-\s]?s|lithium[-\s]?sulfur|srr)\b/.test(value) ||
        value.includes("锂硫") ||
        value.includes("多硫化") ||
        value.includes("硫还原") ||
        value.includes("shuttle effect")
      ) return "SRR_LiS";
      if (/\b(her|hydrogen evolution)\b/.test(value)) return "HER";
      if (/\b(oer|oxygen evolution)\b/.test(value)) return "OER";
      if (/\b(orr|oxygen reduction)\b/.test(value)) return "ORR";
      if (/\b(co2rr|co2 reduction|carbon dioxide reduction)\b/.test(value)) return "CO2RR";
      return "UNKNOWN";
    }

    function inferPromptTargetReaction(kind) {
      if (kind !== "dft") return "UNKNOWN";
      const rows = promptTargetRows();
      return inferReactionProfileFromText(rows.map(function (row) {
        return [
          row && row.library_name,
          row && row.paper_code,
          row && row.title,
          row && row.abstract,
          row && row.dft_completeness_label,
          row && row.dft_completeness_status
        ].filter(Boolean).join(" ");
      }).join(" "));
    }

    function inferProjectLibraryContext(contract) {
      const contexts = contract && contract.project_library_contexts && typeof contract.project_library_contexts === "object"
        ? contract.project_library_contexts
        : {};
      const libraryNames = new Set(promptTargetRows().map(function (row) {
        return String(row && row.library_name || "").trim();
      }).filter(Boolean));
      return Object.keys(contexts).find(function (key) {
        const context = contexts[key] || {};
        return libraryNames.has(String(context.default_library_name || "").trim());
      }) || null;
    }

    function selectedPromptRows() {
      if (!selectedPaperIds.size) return [];
      const byId = new Map((state.rows || []).map(function (row) {
        return [String(row.paper_id), row];
      }));
      return Array.from(selectedPaperIds).map(function (paperId) {
        return byId.get(String(paperId));
      }).filter(Boolean);
    }

    function promptTargetRows() {
      return selectedPromptRows();
    }

    function validatePromptCopyTarget(actionConfig) {
      const rows = currentPromptTargetRows();
      if (rows.length !== 1) {
        throw new Error("一次只能选择一个目标；请取消多选后再复制单篇任务提示词。");
      }
      const row = rows[0];
      const isSupport = isSupportingInformationRow(row);
      if (actionConfig.targetScope === "support" && !isSupport) {
        throw new Error(actionConfig.label + "只能选择支撑文献/SI 行作为任务目标。");
      }
      if (actionConfig.targetScope === "main" && isSupport) {
        throw new Error(actionConfig.label + "只能选择主文献作为任务目标。");
      }
      return row;
    }

    function togglePaperSelection(paperId, checked) {
      if (!paperId) return;
      if (checked) {
        selectedPaperIds.add(String(paperId));
      } else {
        selectedPaperIds.delete(String(paperId));
      }
      renderRows();
    }

    function selectVisibleRows() {
      currentVisibleRows().forEach(function (row) {
        if (row && row.paper_id) selectedPaperIds.add(String(row.paper_id));
      });
      renderRows();
      showToast("已选择当前页文献：" + selectedPaperIds.size + " 篇");
    }

    function toggleVisibleRowsSelection(checked) {
      currentVisibleRows().forEach(function (row) {
        if (!row || !row.paper_id) return;
        if (checked) selectedPaperIds.add(String(row.paper_id));
        else selectedPaperIds.delete(String(row.paper_id));
      });
      renderRows();
    }

    function syncSelectAllVisible(rows) {
      const checkbox = document.getElementById("selectAllVisible");
      if (!checkbox) return;
      const visibleIds = (rows || []).map(function (row) { return String(row.paper_id || ""); }).filter(Boolean);
      const selectedVisible = visibleIds.filter(function (paperId) { return selectedPaperIds.has(paperId); }).length;
      checkbox.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
      checkbox.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
    }

    function clearSelectedRows() {
      selectedPaperIds.clear();
      renderRows();
      showToast("已清空文献选择");
    }

    function handleFilterChange() {
      state.pagination.page = 1;
      saveReviewCenterFilterState();
      renderRows();
    }

    function handleSortChange() {
      state.pagination.page = 1;
      saveReviewCenterFilterState();
      renderRows();
    }

    function handleSearchInput() {
      state.pagination.page = 1;
      saveReviewCenterFilterState();
      renderRows();
    }

    function totalPagesForCount(count) {
      const pageSize = Math.max(1, Number(state.pagination.pageSize || 25));
      return Math.max(1, Math.ceil(Math.max(0, Number(count || 0)) / pageSize));
    }

    function ensureValidPage(totalCount) {
      const totalPages = totalPagesForCount(totalCount);
      if (state.pagination.page > totalPages) state.pagination.page = totalPages;
      if (state.pagination.page < 1) state.pagination.page = 1;
      return totalPages;
    }

    function currentVisibleRows() {
      const rows = sortedRows(filteredRows());
      const pageSize = Math.max(1, Number(state.pagination.pageSize || 25));
      ensureValidPage(rows.length);
      const start = (state.pagination.page - 1) * pageSize;
      return rows.slice(start, start + pageSize);
    }

    function goToPage(page) {
      state.pagination.page = Number(page || 1);
      saveReviewCenterFilterState();
      renderRows();
    }

    function changePage(delta) {
      goToPage(Number(state.pagination.page || 1) + Number(delta || 0));
    }

    function setPageSize() {
      const select = document.getElementById("pageSizeSelect");
      const nextSize = Number(select && select.value ? select.value : state.pagination.pageSize);
      state.pagination.pageSize = Number.isFinite(nextSize) && nextSize > 0 ? nextSize : 25;
      state.pagination.page = 1;
      saveReviewCenterFilterState();
      renderRows();
    }

    function renderPagination(totalRows, visibleRows) {
      const totalPages = ensureValidPage(totalRows);
      const currentPage = state.pagination.page;
      const pageSize = Math.max(1, Number(state.pagination.pageSize || 25));
      const start = totalRows ? ((currentPage - 1) * pageSize + 1) : 0;
      const end = totalRows ? Math.min(totalRows, start + visibleRows.length - 1) : 0;
      const loadedCount = Number(state.rows.length || 0);
      const knownTotal = Number(state.metadata.total || loadedCount || 0);
      const hasTruncation = knownTotal > loadedCount;
      const metaParts = [];
      metaParts.push(totalRows ? ("当前页 " + start + "-" + end + " / " + totalRows + " 篇") : "当前页 0 / 0 篇");
      metaParts.push("第 " + currentPage + " / " + totalPages + " 页");
      if (hasTruncation) {
        metaParts.push("已加载 " + loadedCount + " / 总 " + knownTotal + " 篇");
      } else {
        metaParts.push("总计 " + knownTotal + " 篇");
      }
      document.getElementById("paginationMeta").textContent = metaParts.join(" | ");

      const pages = [];
      const startPage = Math.max(1, currentPage - 2);
      const endPage = Math.min(totalPages, currentPage + 2);
      for (let page = startPage; page <= endPage; page += 1) {
        pages.push(
          '<button class="page-indicator' + (page === currentPage ? ' is-active' : '') + '" type="button" onclick="goToPage(' + page + ')"' +
          (page === currentPage ? ' aria-current="page"' : '') +
          '>' + esc(page) + '</button>'
        );
      }
      document.getElementById("paginationBar").innerHTML =
        '<span class="page-size-box">每页' +
          '<select id="pageSizeSelect" onchange="setPageSize()">' +
            '<option value="25"' + (pageSize === 25 ? ' selected' : '') + '>25 篇</option>' +
            '<option value="50"' + (pageSize === 50 ? ' selected' : '') + '>50 篇</option>' +
            '<option value="100"' + (pageSize === 100 ? ' selected' : '') + '>100 篇</option>' +
          '</select>' +
        '</span>' +
        '<button class="btn btn-ghost btn-sm" type="button" onclick="goToPage(1)"' + (currentPage <= 1 ? ' disabled' : '') + '>首页</button>' +
        '<button class="btn btn-ghost btn-sm" type="button" onclick="changePage(-1)"' + (currentPage <= 1 ? ' disabled' : '') + '>上一页</button>' +
        '<span class="pagination-pages">' + pages.join("") + '</span>' +
        '<button class="btn btn-ghost btn-sm" type="button" onclick="changePage(1)"' + (currentPage >= totalPages ? ' disabled' : '') + '>下一页</button>' +
        '<button class="btn btn-ghost btn-sm" type="button" onclick="goToPage(' + totalPages + ')"' + (currentPage >= totalPages ? ' disabled' : '') + '>末页</button>';
    }

    function legacyIdePromptTemplateV2(kind) {
      const now = new Date();
      const pad = function (value) { return String(value).padStart(2, "0"); };
      const runTag = [
        now.getFullYear(),
        pad(now.getMonth() + 1),
        pad(now.getDate()),
        "_",
        pad(now.getHours()),
        pad(now.getMinutes()),
        pad(now.getSeconds())
      ].join("");
      const sourceLabel = "<agent_name>_" + kind + "_" + runTag;
      const targetList = currentPromptTargets();
      const common = [
        "你现在是本系统的 IDE 审核 AI。请先使用当前 IDE/项目会话已经暴露的 Literature AI MCP 工具，不要先编辑 mcp_config.json，也不要让用户手工配置。不要只输出文字总结：普通非表格结果通过 import_analysis 回写，任何表格对象修改必须直接调用 update_table/create_table/merge_table/delete_table。",
        "",
        "目标文献清单：",
        targetList,
        "",
        "目标说明：human_ref 是给人沟通用的稳定短号；调用 MCP/API 时必须使用同一行的 paper_id(UUID)，不要使用页面序号或旧 #001 编号。",
        "source_label 必须写成：" + sourceLabel + "。请把 <agent_name> 替换成你的实际模型或窗口名，例如 gemini、kimi、codex、claude；source_label 只用于展示和追踪本轮运行，不参与 DFT 入库资格判断。",
        "",
        "必须执行：",
        "0. 先检查当前可用工具列表：应能看到 literature-ai/query_papers、get_paper、get_codex_context、get_codex_item、read_paper_page、import_analysis 等 MCP 工具；如果 IDE 会话里没有这些工具，只能把服务端已配置的 MCP API key 传给 literature-ai/backend 的 app.mcp.context.mcp_auth_context，再受控调用 app.mcp.server 已公开的 MCP 工具。禁止构造 MCPAuthInfo、自填 source_identity，禁止直接调用 service/session/model 或数据库。受控入口不可用时报告 blocked_by_missing_mcp_tool。不要自行写 mcp_config.json。",
        "1. 每篇文献先准备材料：优先使用 MCP 的 prepare/get context 能力；如果工具提示材料已存在，可以继续；如果无法读取 codex-context 或 source_assets/artifact_status 不存在，先切到项目内后端 MCP 兜底再判断，只有两条路径都失败才报告阻塞。",
        "1a. 先判断本文文献类型与系统解析是否一致；若不一致，直接按 PDF 证据修正系统解析类型，再继续后续审核。",
        "2. 读取证据优先用当前 Literature AI MCP 工具：get_codex_context(paper_id)、get_codex_item(paper_id,item_type,item_id)、read_paper_page(paper_id,page_start,page_end)、import_analysis。若 IDE 会话没有暴露这些工具，就用项目内后端 MCP 兜底路径完成同样的调用，不要默认绕过 MCP 或改成只写文字总结。",
        "3. 禁止直接 import 后端 service/session/model，禁止直接写数据库，禁止用脚本绕过 MCP/API 权限。禁止下载 PDF 到本地后用 pdftotext/pdf2txt/自写脚本替代 read_paper_page；如果 read_paper_page 或证据入口不可用，必须停止并报告 blocked_by_evidence_api_unavailable。",
        "4. 每篇目标文献最终必须至少有一次受控回写：元数据、章节、figure 元信息等普通非表格字段使用 import_analysis；表格对象修改使用 update_table/create_table/merge_table/delete_table。优先走当前 IDE 暴露的 MCP 工具，缺失时走项目内后端 MCP 兜底路径。import_analysis 参数固定包含 source='ide_ai'、source_label='" + sourceLabel + "'、reviewer='<agent_name>'。",
        "4-apply. 如果本次要直接应用非 DFT 修正或创建对象，使用 auto_apply_review_rules=true；[AI_REVIEWED] review note 只能作为说明，不能授予 RAG、写作或引用资格。非 DFT AI 写入允许后写覆盖先写，不需要模块写锁。",
        "4-candidate. 如果只是提交 object_review_audits / paper-level audit candidate，通常可以使用 auto_apply_review_rules=false；这只表示候选意见已导入，状态仍是 unverified/candidate，不等于审核完成、通过或已应用。",
        "4-dft. DFT 审核必须写 PASS、REVISE、REJECT、NEEDS_HUMAN 或 new_candidate，并使用 auto_apply_review_rules=true。取得 dft_results 写锁后，一份证据合格的意见即可通过受控入口直接确认、修正、拒绝或新增；NEEDS_HUMAN 保留待用户判断。",
        "4a. 图像操作是强例外：recrop_figure 和 create_figure_from_bbox 必须 direct MCP tool 调用，绝不能把 recrop、bbox、补图、裁图请求伪装成 import_analysis 或 correction_proposals。",
        "4b. 在尝试图像修复前，先确认当前 MCP 会话里实际暴露了 recrop_figure / create_figure_from_bbox。若工具不存在、不可调用或 capability 缺失，必须停止并明确报告 blocked_by_missing_mcp_tool 或 missing_request_parse_capability；不要谎称系统没有该工具，也不要改走 import_analysis 假装完成。",
        "5. 工具/API 返回错误时必须停止并报告错误；不要伪造已回写，不要只给用户文字报告。",
        "6. parser candidate 不等于可靠知识。每个供正式写作的 section、writing_card、mechanism_claim 都必须有绑定真实对象 UUID 的对象级审核以及 page + quoted_text/evidence_text PDF 证据；只有回读安全门通过后才供 RAG、写作或引用调用。",
        "7. 非 DFT 内容发现问题必须尽量直接修正：普通非表格对象已存在时用 replace，缺失的 sections/figures/writing_cards/mechanism_claims/electrochemical_performance/catalyst_samples 用 create。表格 caption/markdown/page/prov 修正调用 update_table，漏表调用 create_table，重复/跨页表调用 merge_table，无效表调用 delete_table；禁止把表格修改写成 correction_proposals。",
        "8. correction_proposals 的 field_name 必须是单个字段或集合名，不要把示例里的 A|B|C 原样复制。",
        "9. 遇到图表缺失、页码不一致、标题/DOI/期刊/年份异常、图片打不开、数据明显错位时，必须主动核对 codex-context.context.source_assets.pdf_path 给出的原 PDF 文件名/路径，并用 read_paper_page 或 PDF 页面证据交叉确认；不要只依据候选列表下结论。",
        "10. 只有系统暂不自动执行或 DFT 争议问题才写 review_notes；缺摘要、缺章节、figure 元信息错误用非表格 correction_proposals，图片文件用专用图像工具，表格对象问题用 update_table/create_table/merge_table/delete_table。"
      ].join("\n");

      const auditHardening = [
        "",
        "审核硬规则补充：",
        "- 不要依赖网页展示数量上限；需要用 codex-context/detail payload 与原 PDF 页证据交叉核对。",
        "- auto_apply_review_rules=false 只代表候选导入，不等于审核完成、PASS 已应用或状态已更新；必须回读对象状态确认。",
        "- read_paper_page、recrop_figure、create_figure_from_bbox 或对应 capability 缺失时必须明确报告 blocked，不要改用本地下载 PDF、pdftotext/pdf2txt 或临时脚本绕过。",
        "- parser candidate、页面候选、未审核章节、规则抽取的论文重点都不是可信知识；对象级审核必须绑定真实 UUID 和精确 PDF 页证据，[AI_REVIEWED] review note 不能解锁 RAG、写作或引用。",
        "- 写回后必须用 get_codex_item、get_paper 或 retrieve_evidence 回读；安全门未通过时报告仍为候选，不能宣称审核完成。",
        "- 非 DFT RAG-ready 输出必须尽量保留 source_type、source_id、paper_code、page、evidence_text、review_status、evidence_locator；材料/催化剂样本必须有材料身份和 PDF 页证据锚点。",
      ].join("\n");
      const commonWithHardening = common + auditHardening;

      if (kind === "overall") {
        return commonWithHardening + "\n\n本次任务模块：overall 初步总体解析\n目标：核对并直接修正非 DFT 内容；DFT 只标记问题，不在 overall 中最终入库。\n\n必须检查：title、year、journal、authors、doi、abstract、paper_type/type_confidence、sections、figures、tables、writing_cards、mechanism_claims、electrochemical_performance、catalyst_samples。开始前记录 source_assets.pdf_path，并用 read_paper_page 核对异常。\n\n回写规则：\n- 普通非表格错误使用 import_analysis correction_proposals；已存在对象用 replace，缺失 sections/figures/writing_cards/mechanism_claims/electrochemical_performance/catalyst_samples 用 create，并提供 page、quoted_text、source_pdf。\n- 任何表格对象修改必须直接调用 update_table/create_table/merge_table/delete_table，禁止提交 table correction_proposals。表格 PASS/REJECT 可写 object_review_audits，但 corrected_value 不会修改表对象。\n- 图像文件创建或重裁直接调用 create_figure_from_bbox/recrop_figure。\n- catalyst_samples、mechanism_claims、electrochemical_performance 必须包含各自的身份或证据字段。\n- 已核对无误的 section、writing_card、mechanism_claim 仍需对真实对象 UUID 的具体字段提交带 page + quoted_text 的受控 replace；review note 只能说明情况，不能解锁写作或引用。\n- DFT 数据只记录问题或生成候选，不要单 AI 终审、不要解锁导出。\n\ncorrection_proposals 示例只适用于非表格对象：{\"correction_proposals\":[{\"field_name\":\"sections\",\"target_path\":\"sections:new:create\",\"operation\":\"create\",\"proposed_value\":{\"section_title\":\"Methods\",\"section_type\":\"methods\",\"text\":\"Methods section text...\",\"page_start\":2,\"page_end\":3},\"reason\":\"parser missed Methods\",\"evidence_payload\":{\"page\":2,\"quoted_text\":\"Methods...\",\"source_pdf\":\"<source_assets.pdf_path 文件名>\"}}]}";
      }

      if (kind === "dft") {
        return commonWithHardening + "\n\n本次任务模块：DFT 数据专项核验与入库\n目标：核验已有 DFT 结果并补齐漏项，必须回写 raw_payload.object_review_audits；不要用 correction_proposals 修改 DFT。\n\n开始前读取 codex-context.context.source_assets.pdf_path，并用 read_paper_page 核对主文和 SI 证据。\n\n规则：\n- 每条意见必须绑定材料/结构、性质或反应步、数值、单位以及 page + quoted_text。\n- 已有行使用 PASS、REVISE、REJECT 或 NEEDS_HUMAN；漏项使用 new_candidate。材料身份或证据不能确认时必须写 NEEDS_HUMAN，不得猜测。\n- new_candidate 的 corrected_value 至少包含 material_identity、property_type、value、unit；能确认时补充 adsorbate、reaction_step 和 method。\n- 正文、SI、表格或图片对同一计算结果的重复证据应合并到同一行，不要创建重复 DFT 结果。\n- 不从曲线估读数值；图像只有明确标注数值时才可作为结果证据。\n- 写入前获取 dft_results 模块写锁，然后调用 import_analysis(auto_apply_review_rules=true)。一份证据合格的 AI 意见即可直接确认、修正、拒绝或新增，不需要第二 AI 或主 AI。\n- 回读 DFT row、candidate_status、审核记录和 export_safety，确认实际写入后才报告 completed。";
      }

      if (kind === "figure") {
        return commonWithHardening + "\n\n本次任务模块：图表专项核验\n目标：逐图逐表核验并直接修正，但图片与表格使用各自的专用工具。\n\n开始前读取 source_assets.pdf_path，用 PDF 原文核对 figure/table 数量、编号、页码、caption 与内容。每幅图检查裁剪范围、panel、坐标轴、图例、标签、content_summary 和 key_elements；不要从图像臆读精确数值。\n\n回写规则：\n- figure 元信息可使用非表格 correction_proposals；图片创建或重裁直接调用 create_figure_from_bbox/recrop_figure。\n- 任何表格对象修改必须直接调用 update_table/create_table/merge_table/delete_table，禁止 table correction_proposals；表格 PASS/REJECT 可写 object_review_audits，但 corrected_value 不会修改表对象。\n- 图表审核后若 paper_type 错误，可用普通 metadata correction 修正。\n- 所有写入必须带 page、figure/table、quoted_text 或 bbox 证据并回读对象。\n\n非表格 correction_proposals 示例：{\"correction_proposals\":[{\"field_name\":\"figures\",\"target_path\":\"figures:<figure_uuid>:content_summary\",\"operation\":\"replace\",\"proposed_value\":\"Three-panel band structure and DOS comparison under tensile strain.\",\"reason\":\"old summary missed the visual comparison\",\"evidence_payload\":{\"page\":4,\"figure\":\"Fig. 2\",\"quoted_text\":\"Fig. 2 ...\",\"source_pdf\":\"<source_assets.pdf_path 文件名>\"}}]}";
      }

      if (kind === "text_review" || kind === "sections_writing") {
        return commonWithHardening + "\n\n本次任务模块：文字审核\n目标：审核 abstract / sections / writing_cards / mechanism_claims。parser 结果和页面候选不是可信知识，未通过对象级安全门前不能用于正式写作或引用。\n\n开始前读取 codex-context.context.source_assets.pdf_path，并用 read_paper_page 逐项核对 PDF。\n\n回写规则：\n- 使用 import_analysis(auto_apply_review_rules=true) 的 correction_proposals；已有对象用 <collection>:<真实对象 UUID>:<field> replace，缺失对象用 <collection>:new:create。\n- 每个要用于正式写作的 section、writing_card、mechanism_claim 都必须留下绑定最终对象 UUID 的对象级审核；evidence_payload 必须包含 page、quoted_text 或 evidence_text、source_pdf，能定位 bbox 时一并提供。\n- 已有对象核对无误且无须改字时，也要对真实字段提交 proposed_value 等于当前值的受控 replace。review note 只能作为说明，不能授予 RAG、写作或引用资格。\n- writing_card 的 evidence_chain 还必须逐项覆盖核心字段，并包含 text、page/source、reviewer_status='verified'、target_resolution_status='active' 和 locator_status='exact_page'。\n- 不要触碰 DFT 终审。\n- 写回后必须用 get_codex_item、get_paper 或 retrieve_evidence 回读真实对象、review_status、evidence_locator 和使用资格；安全门未通过时必须报告仍为候选，不能宣称审核完成。\n\n对象级 replace 示例：{\"correction_proposals\":[{\"field_name\":\"mechanism_claims\",\"target_path\":\"mechanism_claims:<claim_uuid>:claim_text\",\"operation\":\"replace\",\"proposed_value\":\"PDF 核对后的机理声明\",\"reason\":\"object-level PDF review\",\"evidence_payload\":{\"page\":6,\"quoted_text\":\"Quoted mechanism evidence\",\"source_pdf\":\"<source_assets.pdf_path 文件名>\"}}]}";
      }

      return commonWithHardening + "\n\n本次任务模块：表格/章节专项核验\n目标：核验 tables/sections，发现问题必须通过对应的受控工具修正。\n\n开始前读取 source_assets.pdf_path，用 read_paper_page 核对表格缺失、跨页、列错位、页码、单位和章节边界。\n\n回写规则：\n- 表格 caption/markdown/page/extraction_source/prov 修正调用 update_table；漏表调用 create_table；重复、跨页拆分表调用 merge_table；无效表调用 delete_table。每次必须提供结构化 evidence_payload 并回读表对象。\n- 禁止通过 import_analysis correction_proposals 修改、新建、删除或合并表格。\n- 表格无需修改时可提交 PASS/REJECT object_review_audits；若携带 corrected_value，系统只记录意见并要求调用 direct table tool，不会修改表对象。\n- 章节字段错误或缺失仍可使用 import_analysis 的 sections replace/create correction_proposals。\n- 若发现 DFT 候选来自错误表格列，只转交 DFT 专项，不在表格任务里确认 DFT 入库。\n\nPASS audit 示例：{\"object_review_audits\":[{\"paper_id\":\"<paper_id>\",\"target_type\":\"tables\",\"target_id\":\"<table_uuid>\",\"field_name\":\"table_review\",\"decision\":\"PASS\",\"confidence\":0.9,\"reason\":\"Table matches the source PDF\",\"evidence_location\":{\"page\":8,\"table\":\"Table 2\",\"quoted_text\":\"Table 2 ...\",\"source_pdf\":\"<source_assets.pdf_path 文件名>\"}}]}\n\n章节 correction 示例：{\"correction_proposals\":[{\"field_name\":\"sections\",\"target_path\":\"sections:new:create\",\"operation\":\"create\",\"proposed_value\":{\"section_title\":\"Results and discussion\",\"section_type\":\"results\",\"text\":\"section text...\",\"page_start\":4,\"page_end\":7},\"reason\":\"parser missed logical results section\",\"evidence_payload\":{\"page\":4,\"quoted_text\":\"Results and discussion...\",\"source_pdf\":\"<source_assets.pdf_path 文件名>\"}}]}";
    }

    async function copyIdePrompt(actionKey) {
      try {
        const actionConfig = PROMPT_COPY_ACTIONS[actionKey];
        if (!actionConfig) {
          throw new Error("未知提示词入口：" + actionKey);
        }
        validatePromptCopyTarget(actionConfig);
        await copyTextToClipboard(await buildIdePromptForCopy(actionConfig));
        showToast("已复制 " + actionConfig.label);
      } catch (error) {
        showToast("复制失败：" + error.message);
      }
    }

    async function buildIdePromptForCopy(actionConfig) {
      try {
        const kind = actionConfig.kind;
        const guide = await fetchJSON("/api/system/agent-guide");
        const contract = guide && guide.prompt_contract ? guide.prompt_contract : {};
        const templates = contract.templates && typeof contract.templates === "object" ? contract.templates : {};
        const compositeTemplates = contract.composite_templates && typeof contract.composite_templates === "object" ? contract.composite_templates : {};
        const reactionProfileTemplates = contract.reaction_profile_templates && typeof contract.reaction_profile_templates === "object"
          ? contract.reaction_profile_templates
          : {};
        const projectLibraryTemplates = contract.project_library_prompt_templates && typeof contract.project_library_prompt_templates === "object"
          ? contract.project_library_prompt_templates
          : {};
        const targetReaction = inferPromptTargetReaction(kind);
        const projectLibraryContext = inferProjectLibraryContext(contract);
        const profileTemplates = reactionProfileTemplates[targetReaction] && typeof reactionProfileTemplates[targetReaction] === "object"
          ? reactionProfileTemplates[targetReaction]
          : {};
        const template = profileTemplates[kind] || templates[kind] || compositeTemplates[kind];
        if (!template) throw new Error("prompt contract does not include kind=" + kind);
        const projectTemplates = projectLibraryContext && projectLibraryTemplates[projectLibraryContext] && typeof projectLibraryTemplates[projectLibraryContext] === "object"
          ? projectLibraryTemplates[projectLibraryContext]
          : {};
        const projectFragment = projectTemplates[kind] || "";

        const now = new Date();
        const pad = function (value) { return String(value).padStart(2, "0"); };
        const runTag = [
          now.getFullYear(),
          pad(now.getMonth() + 1),
          pad(now.getDate()),
          "_",
          pad(now.getHours()),
          pad(now.getMinutes()),
          pad(now.getSeconds())
        ].join("");
        const sourceLabel = "<agent_name>_" + (actionConfig.sourceKind || kind) + "_" + runTag;
        const targetToken = contract.target_list_token || "{{TARGET_LIST}}";
        const sourceToken = contract.source_label_token || "{{SOURCE_LABEL}}";
        const reactionToken = contract.target_reaction_token || "{{TARGET_REACTION}}";
        const targetList = currentPromptTargets();
        let rendered = String(template)
          .split(targetToken).join(targetList)
          .split(sourceToken).join(sourceLabel)
          .split(reactionToken).join(targetReaction);
        if (projectFragment && !rendered.includes("专题项目库上下文（ProjectLibraryContext）")) {
          rendered += "\n\n" + String(projectFragment)
            .split(targetToken).join(targetList)
            .split(sourceToken).join(sourceLabel)
            .split(reactionToken).join(targetReaction);
        }
        return actionConfig.scopeNote + "\n\n" + rendered;
      } catch (error) {
        console.warn("canonical IDE prompt unavailable", error);
        return [
          "统一 IDE 审核提示词当前不可用。",
          "请先使用当前会话已暴露的 literature-ai MCP 工具；若工具未注入，只能把服务端已配置的 MCP API key 传给 app.mcp.context.mcp_auth_context，再受控调用 app.mcp.server 已公开工具；禁止构造 MCPAuthInfo 或自填 source_identity。",
          "禁止直接调用 service/session/model 或数据库。无法取得统一提示词与受控证据入口时，请报告 blocked_by_prompt_contract_unavailable。"
        ].join("\n");
      }
    }

    function handlePromptCopySelect() {
      const select = document.getElementById("promptCopySelect");
      if (!select || !select.value) return;
      const actionKey = select.value;
      select.value = "";
      copyIdePrompt(actionKey);
    }

    async function fetchJSON(url, options) {
      const response = await fetch(url, options || {});
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (_) {
        data = null;
      }
      if (!response.ok) {
        const message = apiErrorMessageFromPayload(data, response.status);
        const error = new Error(message);
        error.status = response.status;
        error.payload = data;
        throw error;
      }
      return data;
    }



    function workflowClass(status, needsHuman) {
      if (["Human_Confirmed", "ML_Ready", "Citation_Ready", "Human_Complete", "DB_Ready"].includes(status)) return "ok";
      if (["Needs_Human_Confirmation", "Gemini_Flagged", "Evidence_Insufficient", "Rejected", "Suspected_Missing", "Unparsed"].includes(status)) return "bad";
      if (["Initial_Parsed", "AI_Rescanned"].includes(status)) return "ok";
      if (needsHuman || ["Codex_Candidate", "Gemini_Verified", "Gemini_Revised"].includes(status)) return "warn";
      return "warn";
    }

    function qualityClass(status) {
      if (status === "A_text_readable") return "ok";
      if (status === "B_text_partial" || status === "C_scan_clear") return "warn";
      if (!status) return "";
      return "bad";
    }

    function workflowMeta(status) {
      const mapping = {
        Imported: { label: "已导入", tip: "文献已经进入当前库，但还没有完成后续准备。" },
        Quality_Checked: { label: "已检质量", tip: "已经完成 PDF 质量判断。" },
        Parsed_Material_Ready: { label: "材料已就绪", tip: "工作目录、证据和图表材料已经准备好。" },
        Unparsed: { label: "未解析", tip: "尚未完成 DFT 或材料数据解析。" },
        Initial_Parsed: { label: "初步解析", tip: "已经有候选，但还需要完整性审计和证据核对。" },
        Suspected_Missing: { label: "疑似漏提", tip: "检测到 DFT 线索多于已解析候选，建议优先复核全文。" },
        AI_Rescanned: { label: "AI 已重扫", tip: "AI 已按协议重新扫描全文，仍需要继续合并和审核。" },
        Human_Complete: { label: "确认完整", tip: "当前候选覆盖已经确认完整。" },
        DB_Ready: { label: "可入库", tip: "已达到正式数据库入库条件。" },
        Codex_Candidate: { label: "系统候选", tip: "旧状态，仅作为审核线索，不代表最终结论。" },
        Gemini_Verified: { label: "AI 已核验", tip: "AI 已核验证据，但仍不等于最终正式确认。" },
        Gemini_Revised: { label: "AI 已修订", tip: "AI 认为候选内容需要调整后再确认。" },
        Gemini_Flagged: { label: "AI 标红", tip: "AI 发现明显疑点，建议优先复核。" },
        Evidence_Insufficient: { label: "证据不足", tip: "当前材料不足以支持可靠判断。" },
        Needs_Human_Confirmation: { label: "待确认", tip: "系统已把这篇文献留给确认者做最后判断。" },
        Human_Confirmed: { label: "已确认", tip: "这篇文献的当前候选已经确认。" },
        ML_Ready: { label: "可进机器学习", tip: "确认后的结构化数据已达到机器学习使用条件。" },
        Citation_Ready: { label: "可用于引用", tip: "元数据和审核状态已经足够支持写作引用。" },
        Rejected: { label: "已拒绝", tip: "当前候选内容被判定不应进入正式库。" }
      };
      return mapping[status] || { label: status || "未知状态", tip: "这是系统内部状态码，仍建议查看详情。" };
    }

    function qualityMeta(status) {
      const mapping = {
        A_text_readable: { label: "A 可直接读", tip: "PDF 自带文字层，搜索和定位都比较可靠。" },
        B_text_partial: { label: "B 部分可读", tip: "只有部分页面或部分区域文字可直接读取，后续要谨慎核对。" },
        C_scan_clear: { label: "C 扫描清晰", tip: "扫描版较清晰，适合 OCR 辅助处理。" },
        D_scan_unclear: { label: "D 扫描不清", tip: "扫描质量较差，自动解析容易出错。" },
        Broken: { label: "文件异常", tip: "PDF 可能损坏或无法稳定读取。" },
        Good: { label: "PDF 可用", tip: "当前 PDF 可以正常打开并继续核对。" }
      };
      return mapping[status] || { label: status || "未知", tip: "尚未获得明确的 PDF 质量判断。" };
    }

    function qualityReasonLabel(reason) {
      if (reason && String(reason).startsWith("pdf_open_failed:")) {
        return "PDF 打开失败：" + String(reason).split(":").slice(1).join(":");
      }
      const mapping = {
        native_text_is_readable: "自带文字层，可直接检索",
        native_text_is_partial: "只有部分文字层可读",
        scan_or_image_pdf_requires_ocr: "扫描版或图片型 PDF，需要 OCR 辅读",
        too_little_text_or_image_signal: "文字层和图像信号都不足，可靠性低",
        pdf_has_no_pages: "PDF 没有可读取页面",
        pdf_file_missing: "PDF 文件缺失",
        missing_pdf_reference: "文献没有 PDF 路径记录",
        file_broken_or_unreadable: "文件异常或无法读取"
      };
      return mapping[reason] || reason || "未提供具体原因。";
    }

    function getInspectTarget(row) {
      const figureCount = toCount(row.figure_count);
      const tableCount = toCount(row.table_count);
      const evidenceCount = toCount(row.evidence_count);
      const hasActiveDftCandidates = row.has_active_dft_candidates !== undefined
        ? !!row.has_active_dft_candidates
        : !!row.has_dft_candidates;
      if (hasActiveDftCandidates) {
        return { tab: "dft", note: "跳到 DFT 数据页，优先核对候选字段和证据。" };
      }
      if (row.has_dft_candidates) {
        return { tab: "dft", note: "跳到 DFT 数据页，查看已审核结果和剩余证据状态。" };
      }
      if (figureCount > 0 || tableCount > 0) {
        return { tab: "figures", note: "当前没有 DFT 候选，先核对图表和图注。" };
      }
      if (evidenceCount > 0) {
        return { tab: "sections", note: "当前主要是文字证据定位，跳到文字审核继续检查。" };
      }
      return { tab: "summary", note: "当前没有更多候选材料，先看摘要和基础信息。" };
    }

    function renderStats() {
      const qualityCounts = state.metadata.quality_counts || {};
      const rows = state.rows || [];
      const activeConflictCount = rows.reduce(function (sum, row) {
        return sum + Number(row.dft_review_conflict_count || 0);
      }, 0);
      const items = [
        ["文献", rows.length, "docs", ""],
        ["待确认", rows.filter(function (row) { return row.needs_human_confirmation; }).length, "human", ""],
        ["A/B", Number(qualityCounts.A_text_readable || 0) + Number(qualityCounts.B_text_partial || 0), "quality", ""],
        ["DFT", rows.filter(function (row) {
          return row.has_active_dft_candidates !== undefined ? row.has_active_dft_candidates : row.has_dft_candidates;
        }).length, "dft", ""],
        ["DFT 冲突", activeConflictCount, "conflict", "只汇总当前还没处理完的 DFT 冲突；图表和内容允许 AI 直接覆盖，不再进入冲突队列。"]
      ];
      document.getElementById("stats").innerHTML = items.map(function (item) {
        return '<div class="stat"' + (item[3] ? ' title="' + esc(item[3]) + '"' : '') + '><div class="stat-icon stat-icon-' + esc(item[2]) + '" aria-hidden="true"></div><div class="stat-label">' + esc(item[0]) + '</div><div class="stat-value">' + esc(item[1]) + '</div></div>';
      }).join("");
    }

    const STATUS_GROUPS = {
      "group:needs_action": ["Needs_Human_Confirmation", "Evidence_Insufficient", "Gemini_Flagged"],
      "group:needs_attention": ["Suspected_Missing", "Rejected"],
      "group:ai_processing": ["Imported", "Quality_Checked", "Parsed_Material_Ready", "Unparsed", "Initial_Parsed", "AI_Rescanned"],
      "group:approved": ["Human_Complete", "DB_Ready", "Human_Confirmed", "ML_Ready", "Citation_Ready", "Gemini_Verified", "Gemini_Revised"],
      "group:deprecated": ["Codex_Candidate"]
    };

    const STATUS_FILTER_GROUP_OPTIONS = [
      { value: "group:needs_action", label: "需要我处理" },
      { value: "group:needs_attention", label: "需要关注" },
      { value: "group:ai_processing", label: "AI 处理中" },
      { value: "group:approved", label: "已审核通过" },
      { value: "group:deprecated", label: "历史旧状态" }
    ];

    const WORKFLOW_STATUS_ORDER = [
      "Imported",
      "Quality_Checked",
      "Parsed_Material_Ready",
      "Unparsed",
      "Initial_Parsed",
      "Suspected_Missing",
      "AI_Rescanned",
      "Human_Complete",
      "DB_Ready",
      "Codex_Candidate",
      "Gemini_Verified",
      "Gemini_Revised",
      "Gemini_Flagged",
      "Evidence_Insufficient",
      "Needs_Human_Confirmation",
      "Human_Confirmed",
      "Rejected",
      "ML_Ready",
      "Citation_Ready"
    ];

    function rowMatchesGroupFilter(row, status) {
      if (!status) return true;
      const workflow = String(row && row.workflow_status || "").trim();
      if (STATUS_GROUPS[status]) {
        return STATUS_GROUPS[status].includes(workflow);
      }
      if (status === "Needs_Human_Confirmation") {
        return !!(row && row.needs_human_confirmation) || workflow === "Needs_Human_Confirmation";
      }
      return workflow === status;
    }

    function renderStatusFilterOptions() {
      const groupSelect = document.getElementById("statusFilter");
      const workflowSelect = document.getElementById("workflowStatusFilter");
      if (!groupSelect || !workflowSelect) return;
      const restored = state.restoredReviewCenterFilters && typeof state.restoredReviewCenterFilters === "object"
        ? state.restoredReviewCenterFilters
        : null;
      const currentGroup = groupSelect.value || (restored && restored.statusFilter) || "";
      const currentWorkflow = workflowSelect.value || (restored && restored.workflowStatusFilter) || "";
      const rows = Array.isArray(state.rows) ? state.rows : [];
      const workflowCounts = {};
      rows.forEach(function (row) {
        const workflow = String(row && row.workflow_status || "").trim();
        if (!workflow) return;
        workflowCounts[workflow] = Number(workflowCounts[workflow] || 0) + 1;
      });
      const presentStatuses = Object.keys(workflowCounts);
      const orderedStatuses = WORKFLOW_STATUS_ORDER.filter(function (status) {
        return Object.prototype.hasOwnProperty.call(workflowCounts, status);
      }).concat(
        presentStatuses.filter(function (status) {
          return !WORKFLOW_STATUS_ORDER.includes(status);
        }).sort()
      );

      const groupHtml = ['<option value="">全部流程状态</option>'];
      STATUS_FILTER_GROUP_OPTIONS.forEach(function (item) {
        const count = rows.filter(function (row) { return rowMatchesGroupFilter(row, item.value); }).length;
        groupHtml.push('<option value="' + esc(item.value) + '">' + esc(item.label + " (" + count + ")") + '</option>');
      });
      groupSelect.innerHTML = groupHtml.join("");
      if (currentGroup && STATUS_GROUPS[currentGroup]) {
        groupSelect.value = currentGroup;
      } else {
        groupSelect.value = "";
      }

      const workflowHtml = ['<option value="">具体流程状态</option>'];
      orderedStatuses.forEach(function (status) {
        const meta = workflowMeta(status);
        workflowHtml.push('<option value="' + esc(status) + '">' + esc(meta.label + " (" + workflowCounts[status] + ")") + '</option>');
      });
      workflowSelect.innerHTML = workflowHtml.join("");
      if (currentWorkflow && Object.prototype.hasOwnProperty.call(workflowCounts, currentWorkflow)) {
        workflowSelect.value = currentWorkflow;
      } else {
        workflowSelect.value = "";
      }
      if (restored) {
        state.restoredReviewCenterFilters = Object.assign({}, restored, {
          statusFilter: groupSelect.value || "",
          workflowStatusFilter: workflowSelect.value || "",
        });
      }
    }

    function filteredRows() {
      const status = getValue("statusFilter");
      const workflowStatus = getValue("workflowStatusFilter");
      const quality = getValue("qualityFilter");
      const q = getValue("searchBox").toLowerCase();
      return state.rows.filter(function (row) {
        if (!rowMatchesGroupFilter(row, status)) return false;
        if (workflowStatus && String(row && row.workflow_status || "").trim() !== workflowStatus) return false;
        if (quality && row.pdf_quality_status !== quality) return false;
        if (!q) return true;
        const haystack = [
          row.title,
          row.doi,
          row.journal,
          row.year,
          row.workflow_status,
          row.pdf_quality_status,
          row.paper_code,
          row.paper_id
        ].join(" ").toLowerCase();
        return haystack.includes(q);
      });
    }

    async function loadLibraries() {
      try {
        const libraries = await fetchJSON("/api/libraries");
        state.libraries = Array.isArray(libraries) ? libraries : [];
        const select = document.getElementById("libraryFilter");
        if (!select) return;
        const active = state.libraries.find(function (item) { return item && item.is_active; });
        const requested = select.value || getQueryLibraryName() || getStoredLibraryName() || (active && active.name) || "";
        const hasRequested = requested && state.libraries.some(function (item) { return item && item.name === requested; });
        const current = hasRequested ? requested : ((active && active.name) || "");
        select.innerHTML = '<option value="">全部文献库</option>' + state.libraries.map(function (item) {
          const count = Number(item.paper_count || 0);
          return '<option value="' + esc(item.name || "") + '">' + esc((item.name || "未命名文献库") + " (" + count + ")") + '</option>';
        }).join("");
        if (current && state.libraries.some(function (item) { return item && item.name === current; })) {
          select.value = current;
          rememberLibraryName(current);
        }
      } catch (_) {
        state.libraries = [];
      }
    }

    function sortedRows(rows) {
      const items = Array.isArray(rows) ? rows.slice() : [];
      if (getQueryPaperId() && state.metadata && state.metadata.focus_main_paper_id) {
        return sortFocusedSupportPairRows(items, { mainPaperId: state.metadata.focus_main_paper_id });
      }
      const sortBy = getValue("sortFilter") || "recent";
      const serialValue = function (row) {
        const match = String(row && row.paper_code || "").trim().toUpperCase().match(/^[A-Z](\d+)$/);
        if (match) return Number(match[1]);
        const serial = Number(row && row.serial_number);
        if (Number.isFinite(serial) && serial > 0) return serial;
        return Number.MAX_SAFE_INTEGER;
      };
      if (sortBy === "paper_code_asc") {
        return items.sort(function (left, right) {
          const serialDiff = serialValue(left) - serialValue(right);
          if (serialDiff) return serialDiff;
          const codeDiff = String(left.paper_code || "").localeCompare(String(right.paper_code || ""));
          if (codeDiff) return codeDiff;
          return String(left.paper_id || "").localeCompare(String(right.paper_id || ""));
        });
      }
      if (sortBy === "year_desc") {
        return items.sort(function (left, right) {
          const leftYear = Number(left.year || 0);
          const rightYear = Number(right.year || 0);
          if (rightYear !== leftYear) return rightYear - leftYear;
          return String(right.paper_id || "").localeCompare(String(left.paper_id || ""));
        });
      }
      if (sortBy === "conflicts_desc") {
        return items.sort(function (left, right) {
          const keys = [
            "dft_review_conflict_count",
            "locator_issue_count",
            "figure_issue_count"
          ];
          for (const key of keys) {
            const diff = toCount(right[key]) - toCount(left[key]);
            if (diff) return diff;
          }
          return String(right.paper_id || "").localeCompare(String(left.paper_id || ""));
        });
      }
      if (sortBy === "suspected_missing_desc") {
        return items.sort(function (left, right) {
          const missingDiff = toCount(right.suspected_missing_dft_count) - toCount(left.suspected_missing_dft_count);
          if (missingDiff) return missingDiff;
          const rank = function (row) {
            if (row.workflow_status === "Suspected_Missing") return 0;
            if (row.workflow_status === "Unparsed") return 1;
            return 2;
          };
          const rankDiff = rank(left) - rank(right);
          if (rankDiff) return rankDiff;
          return String(right.paper_id || "").localeCompare(String(left.paper_id || ""));
        });
      }
      return items.sort(function (left, right) {
        const createdDiff = String(right.created_at || "").localeCompare(String(left.created_at || ""));
        if (createdDiff) return createdDiff;
        return String(right.paper_id || "").localeCompare(String(left.paper_id || ""));
      });
    }

    function filteredPaperIds(predicate) {
      return filteredRows().filter(function (row) {
        return typeof predicate === "function" ? predicate(row) : true;
      }).map(function (row) {
        return row.paper_id;
      });
    }

    function adjudicationClass(mode) {
      if (mode === "auto") return "ok";
      if (mode === "suggest") return "warn";
      return "bad";
    }

    function adjudicationLabel(mode) {
      if (mode === "auto") return "自动推进";
      if (mode === "suggest") return "建议裁定";
      return "必须处理";
    }

    function adjudicationActionLabel(action) {
      const mapping = {
        verify: "verify",
        reject: "reject",
        propose_correction: "生成修正草案",
        jump_to_review: "跳到对象审核",
        manual_review: "确认处理",
      };
      return mapping[action] || action || "-";
    }

      function buildReviewTargetHref(row, item) {
        const tabByTarget = {
          section: "sections",
          sections: "sections",
          dft_setting: "dft",
          dft_settings: "dft",
          catalyst_sample: "dft",
          catalyst_samples: "dft",
          dft_results: "dft",
          electrochemical_performance: "dft",
          writing_card: "sections",
          writing_cards: "sections",
          mechanism_claim: "sections",
          mechanism_claims: "sections",
          figure: "figures",
        figures: "figures",
        table: "figures",
        tables: "figures",
        };
        const tab = tabByTarget[item.target_type] || "review";
        const bestPdfLocator = bestDeepLinkPdfLocator(item);
        const params = new URLSearchParams();
        const libraryName = (row && row.library_name) || getValue("libraryFilter") || getQueryLibraryName() || getStoredLibraryName() || "";
        params.set("paper_id", row.paper_id);
        params.set("tab", tab);
        if (libraryName && libraryName !== "全部文献库") params.set("library_name", libraryName);
        if (item && item.target_type) params.set("target_type", item.target_type);
        if (item && item.target_id) params.set("target_id", item.target_id);
        if (item && item.field_name) params.set("field_name", item.field_name);
        if (bestPdfLocator) {
          params.set("pdf_page", String(bestPdfLocator.page));
          params.set("pdf_locator_status", bestPdfLocator.locator_status || "exact_page");
          if (bestPdfLocator.evidence_text) params.set("pdf_evidence_text", clipText(bestPdfLocator.evidence_text, 240));
        }
        return item && item.target_type === "dft_results"
          ? "../paper_detail/index.html?" + params.toString()
          : "../literature_library/index.html?" + params.toString();
      }

    function isDftConflictItem(item) {
      return item && item.target_type === "dft_results";
    }

    function canManualAdoptOpinion(item) {
      return false;
    }

    function buildConflictActionButtons(item, row, index) {
      const buttons = [];
      if (isDftConflictItem(item)) {
        buttons.push('<span class="chip warn" title="只关闭 AI 审核意见不会 verify/reject/edit DFT 数据，也不会产生 verified / safe_verified / ML_Ready。">DFT final truth 请到详情页确认处理</span>');
        buttons.push('<a class="btn btn-tinted btn-sm" href="' + esc(buildReviewTargetHref(row, item)) + '">前往 DFT 详情</a>');
        return buttons.join("");
      }
      buttons.push('<a class="btn btn-ghost btn-sm" href="' + esc(buildReviewTargetHref(row, item)) + '">跳到对象审核</a>');
      return buttons.join("");
    }

    function objectAuditSummary(row) {
      const audits = Array.isArray(row.object_review_audits) ? row.object_review_audits : [];
      if (!audits.length) return "no object_review_audit";
      return audits.slice(0, 5).map(function (audit) {
        return [
          audit.candidate_type || "object_review_audit",
          audit.source_label || audit.source || audit.agent_role || audit.model_name || "object_review_audit",
          audit.target_type,
          audit.field_name,
          audit.decision || audit.recommended_action || "review",
          audit.verification_status || "unverified",
          audit.reason
        ].filter(Boolean).join(" / ");
      }).join("\n");
    }

    function locatorIssueLabel(code) {
      const mapping = {
        text_only_locator: "只有文本定位",
        missing_bbox: "缺少框选坐标",
        missing_page: "缺少页码",
        missing_locator: "缺少定位信息",
        approximate_locator: "定位不够精确",
        unresolved_locator: "定位仍未解决"
      };
      return mapping[code] || code;
    }

    function figureIssueLabel(code) {
      const mapping = {
        missing_full_page_snapshot: "缺少整页快照",
        small_crop: "裁图过小",
        missing_bbox: "缺少框选坐标",
        extreme_aspect_ratio: "裁图比例异常",
        caption_only: "只有图注，没有图像",
        missing_image: "缺少图像文件",
        missing_page: "缺少页码"
      };
      return mapping[code] || code;
    }

    function compactPdfStatus(row) {
      const status = row.pdf_artifact_status && typeof row.pdf_artifact_status === "object" ? row.pdf_artifact_status : {};
      return {
        hasPdf: row.pdf_exists === true || status.pdf_exists === true,
        pathKind: hasOwnValue(row.pdf_path_kind) ? row.pdf_path_kind : (status.pdf_path_kind || "unknown"),
        size: hasOwnValue(row.pdf_file_size) ? row.pdf_file_size : status.pdf_file_size,
        blockers: Array.isArray(status.blocking_errors) ? status.blocking_errors : []
      };
    }

    function compactPdfDisplayState(row) {
      const pdf = compactPdfStatus(row);
      const workflowStatus = String(row && row.workflow_status || "").trim();
      const quality = String(row.pdf_quality_status || "").trim().toLowerCase();
      const broken = quality === "broken";
      const blocked = Array.isArray(pdf.blockers) && pdf.blockers.length > 0;
      const hasInitialParse = !!(
        row.has_parsed_content ||
        [
          "Initial_Parsed",
          "AI_Rescanned",
          "Suspected_Missing",
          "Human_Complete",
          "DB_Ready",
          "Human_Confirmed",
          "ML_Ready",
          "Citation_Ready",
          "Needs_Human_Confirmation",
          "Gemini_Flagged",
          "Gemini_Verified",
          "Gemini_Revised",
          "Evidence_Insufficient",
          "Rejected"
        ].includes(workflowStatus)
      );
      const unusable = pdf.hasPdf && (broken || blocked || !hasInitialParse);
      return {
        pdf: pdf,
        workflowStatus: workflowStatus,
        hasInitialParse: hasInitialParse,
        unusable: unusable,
        label: !pdf.hasPdf ? "无 PDF" : (unusable ? "PDF 不可用" : "PDF 可用"),
        chipClass: !pdf.hasPdf ? "subtle" : (unusable ? "bad" : "ok"),
        summaryText: !pdf.hasPdf
          ? "当前文献没有可用 PDF 文件。"
          : (unusable
            ? "PDF 存在，但文件状态异常、被阻塞，或系统尚未完成初步解析。"
            : "PDF 文件存在，且系统已经完成初步解析。")
      };
    }

    function compactPdfChip(row) {
      const display = compactPdfDisplayState(row);
      const pdf = display.pdf;
      const title = [
        display.summaryText,
        "path kind: " + pdf.pathKind,
        hasOwnValue(pdf.size) ? "size: " + pdf.size + " bytes" : null,
        display.workflowStatus ? "workflow: " + display.workflowStatus : null,
        row.pdf_quality_status ? "quality: " + row.pdf_quality_status : null,
        pdf.blockers.length ? "blockers: " + pdf.blockers.join(", ") : null
      ].filter(Boolean).join("\n");
      return '<span class="chip compact ' + display.chipClass + '" title="' + esc(title) + '">' + display.label + '</span>';
    }

    function workflowChipClass(status) {
      if (["Needs_Human_Confirmation", "Gemini_Flagged", "Evidence_Insufficient", "Rejected", "Suspected_Missing", "Unparsed"].includes(status)) return "bad";
      if (["Initial_Parsed", "AI_Rescanned", "Parsed_Material_Ready", "Quality_Checked", "Imported", "Gemini_Revised", "Gemini_Verified", "Codex_Candidate"].includes(status)) return "warn";
      if (["Human_Complete", "DB_Ready", "Human_Confirmed", "ML_Ready", "Citation_Ready"].includes(status)) return "ok";
      return "subtle";
    }

    function compactManualReviewProgress(row) {
      const source = row && row.manual_review_progress && typeof row.manual_review_progress === "object"
        ? row.manual_review_progress
        : {};
      const normalize = function (progress, key) {
        const value = progress[key];
        if (value && typeof value === "object") return !!value.completed;
        return !!value;
      };
      const group = supplementaryGroup(row);
      const mainPaperId = String(group && group.main_paper_id || row && row.paper_id || "");
      const mainRow = state.rows.find(function (candidate) {
        return String(candidate && candidate.paper_id || "") === mainPaperId;
      }) || row;
      const mainProgress = mainRow && mainRow.manual_review_progress && typeof mainRow.manual_review_progress === "object"
        ? mainRow.manual_review_progress
        : {};
      const liveChartStage = String(mainRow && mainRow._live_chart_stage || row && row._live_chart_stage || "");
      return {
        figures: liveChartStage ? ["completed", "not_required"].includes(liveChartStage) : normalize(mainProgress, "figures"),
        dft: normalize(source, "dft"),
        content: normalize(source, "content")
      };
    }

    function compactModuleProgressChip(label, completed, title) {
      return '<span class="chip compact ' + (completed ? "ok" : "subtle") + '" title="' + esc(title) + '">' + esc(label) + '</span>';
    }

    function compactSummarizeCounts(counts, formatter) {
      return Object.keys(counts || {}).map(function (key) {
        return { key: key, count: toCount(counts[key]) };
      }).filter(function (item) {
        return item.count > 0;
      }).sort(function (left, right) {
        if (right.count !== left.count) return right.count - left.count;
        return left.key.localeCompare(right.key);
      }).map(function (item) {
        return (formatter ? formatter(item.key) : item.key) + ": " + item.count;
      });
    }

    function compactIssueTitle(topIssues, issueCounts, formatter, emptyText) {
      const lines = Array.isArray(topIssues) && topIssues.length
        ? topIssues.map(function (item) { return (formatter ? formatter(item.code) : item.code) + ": " + toCount(item.count); })
        : compactSummarizeCounts(issueCounts || {}, formatter);
      return lines.length ? lines.join("\n") : emptyText;
    }

    function compactIssueList(topIssues, issueCounts, formatter, emptyText) {
      const lines = Array.isArray(topIssues) && topIssues.length
        ? topIssues.map(function (item) { return (formatter ? formatter(item.code) : item.code) + ": " + toCount(item.count); })
        : compactSummarizeCounts(issueCounts || {}, formatter);
      if (!lines.length) {
        return '<div class="detail-value muted">' + esc(emptyText) + '</div>';
      }
      return '<ul class="detail-list">' + lines.map(function (line) {
        return '<li>' + esc(line) + '</li>';
      }).join("") + '</ul>';
    }

    function compactExtractionMeta(row) {
      const dftAudit = row.dft_audit && typeof row.dft_audit === "object" ? row.dft_audit : {};
      const group = supplementaryGroup(row);
      const groupRole = group && group.role;
      const groupSupportActive = group
        ? toCount(
            group.support_dft_lifecycle_open_count != null
              ? group.support_dft_lifecycle_open_count
              : group.support_active_dft_candidate_count
          )
        : 0;
      const groupMainActive = group ? toCount(group.main_active_dft_candidate_count) : 0;
      const suspectedMissing = hasOwnValue(row.suspected_missing_dft_count)
        ? toCount(row.suspected_missing_dft_count)
        : toCount(dftAudit.suspected_missing_count);
      const hasActiveDftCandidates = row.has_active_dft_candidates !== undefined
        ? !!row.has_active_dft_candidates
        : !!row.has_dft_candidates;
      if (suspectedMissing > 0) {
        return { label: "疑似漏提", className: "bad", tip: "检测到 DFT 线索多于已解析候选，建议优先复核全文。", suspectedMissing: suspectedMissing };
      }
      if (hasActiveDftCandidates) {
        if (groupRole === "supplementary") {
          return { label: "SI 待审 DFT", className: "warn", tip: supplementaryGroupTip(row) || "这行是支撑文献记录，仍有待审 DFT 候选需要独立收口。", suspectedMissing: suspectedMissing };
        }
        return { label: "待审 DFT", className: "warn", tip: "已有待审 DFT 候选，下一步去证据页核对字段和定位。", suspectedMissing: suspectedMissing };
      }
      if (row.has_dft_candidates === true) {
        if (groupRole === "main" && groupSupportActive > 0 && groupMainActive === 0) {
          return { label: "主文已审", className: "ok", tip: supplementaryGroupTip(row) || "主文献 DFT 已收口；支撑文献还有待处理项，不等同于主文献训练数据未入库。", suspectedMissing: suspectedMissing };
        }
        return { label: "已审 DFT", className: "ok", tip: "当前 DFT 结果已写入或完成审核，列表中没有未收口候选。", suspectedMissing: suspectedMissing };
      }
      if (row.has_dft_candidates === false) {
        return { label: "未见 DFT", className: "", tip: "当前没有可审核的 DFT 候选。", suspectedMissing: suspectedMissing };
      }
      if (row.dft_completeness_status === "Initial_Parsed" || dftAudit.coverage_status === "Initial_Parsed") {
        return { label: "初步解析", className: "warn", tip: "已完成初步解析，但候选状态仍需继续确认。", suspectedMissing: suspectedMissing };
      }
      return { label: "未解析", className: "", tip: "尚未形成稳定的 DFT 提取结果。", suspectedMissing: suspectedMissing };
    }

    function compactRiskChip(label, count, title, options) {
      const config = options || {};
      const chipClass = count > 0 ? (config.activeClass || "warn") : (config.zeroClass || "subtle");
      const attrs = config.dataAction ? ' data-action="' + esc(config.dataAction) + '"' : "";
      return '<span class="chip compact ' + chipClass + '"' + attrs + ' title="' + esc(title) + '">' + esc(label) + " " + esc(count) + '</span>';
    }

    function compactDetailSection(label, content) {
      return '<div class="detail-block"><div class="detail-label">' + esc(label) + '</div>' + content + '</div>';
    }

    function candidateStatusLabel(status) {
      const mapping = {
        system_candidate: "系统候选",
        new_candidate: "新增候选",
        Rejected: "已拒绝",
        human_reviewed_needs_evidence: "已审核但证据仍不足",
        Needs_Human_Confirmation: "待确认",
        ML_Ready: "已审核可用",
        Gemini_Verified: "AI 已核验",
        Gemini_Revised: "AI 已修订",
        Gemini_Flagged: "AI 标红",
      };
      return mapping[status] || workflowMeta(status).label || status || "未知";
    }

    function reviewDecisionLabel(status) {
      const mapping = {
        PASS: "通过",
        REJECT: "拒绝",
        REVIEW: "需复核",
        PROPOSED: "提出修正",
        ACCEPT: "接受",
        REVISE: "需修改",
        FLAG: "高风险",
        WARN: "需警惕",
        new_candidate: "新增候选",
        review: "待复核",
      };
      return mapping[status] || status || "未标记";
    }

    function verificationStatusLabel(status) {
      const mapping = {
        verified: "已核实",
        unverified: "未核实",
        pending: "待确认",
        active: "有效",
        applied: "已应用",
        ai_reviewed: "AI 已查看",
      };
      return mapping[status] || status || "未标记";
    }

    function cropStatusLabel(status) {
      const mapping = {
        candidate_crop: "候选裁图",
        verified_crop: "裁图已核对",
        needs_recrop: "需要重裁",
        caption_only: "只有图注",
        rejected: "已舍弃",
      };
      return mapping[status] || status || "未知";
    }

    function explainDetailNextStep(row, extraction, pdfDisplay, conflictCount, activeDftCount, suspectedMissing) {
      if (!pdfDisplay.pdf.hasPdf) {
        return {
          title: "这篇文献当前没有可用 PDF，先补 PDF 或确认它是否只保留元数据。",
          body: "没有 PDF 时，后续很多定位、证据核对和 DFT 审核都无法可靠完成。"
        };
      }
      if (conflictCount > 0) {
        return {
          title: "这篇文献存在未收口冲突，先看“冲突详情”，不要直接凭候选数下结论。",
          body: "DFT 冲突应从只读核验入口跳到 DFT 详情页确认处理；图表或内容模块也应先对照原文证据，再走对应审核入口。"
        };
      }
      if (suspectedMissing > 0) {
        return {
          title: "系统怀疑有漏提，建议优先通读原文或表格，确认是否还有漏掉的 DFT 行。",
          body: "这类文献常见问题不是“字段错”，而是“候选不全”，所以先查覆盖率比先改字段更重要。"
        };
      }
      if (activeDftCount > 0) {
        return {
          title: "这篇文献还有待审 DFT 候选，下一步应去 DFT 详情页核对字段和定位。",
          body: extraction.tip || "先核对材料、数值、单位和页码定位，再决定是否确认或拒绝候选。"
        };
      }
      if (row.needs_human_confirmation) {
        return {
          title: "系统已经把这篇文献留给确认者判断，确认前请先看证据是否完整。",
          body: "如果证据、定位和候选都能闭环，再做确认；否则先补材料或标记问题。"
        };
      }
      return {
        title: "这篇文献当前没有明显的待收口 DFT 冲突，可以按材料类型继续抽查证据。",
        body: getInspectTarget(row).note || "优先进入详情页查看 DFT、图表或正文证据。"
      };
    }

    function compactInfoList(items, emptyText) {
      const lines = (items || []).filter(Boolean);
      if (!lines.length) {
        return '<div class="detail-value muted">' + esc(emptyText) + '</div>';
      }
      return '<ul class="detail-list">' + lines.map(function (line) {
        return '<li>' + esc(line) + '</li>';
      }).join("") + '</ul>';
    }

    function modalContent(title, subtitle, html) {
      document.getElementById("infoModalTitle").textContent = title || "详情";
      document.getElementById("infoModalSubtitle").textContent = subtitle || "";
      document.getElementById("infoModalBody").innerHTML = html || "";
      const overlay = document.getElementById("infoOverlay");
      overlay.classList.add("open");
      overlay.setAttribute("aria-hidden", "false");
    }

    function closeInfoOverlay() {
      const overlay = document.getElementById("infoOverlay");
      overlay.classList.remove("open");
      overlay.setAttribute("aria-hidden", "true");
      document.getElementById("infoModalBody").innerHTML = "";
      conflictState.activeRow = null;
      conflictState.groups = [];
      conflictState.activeGroupIndex = 0;
      conflictState.activeOpinionKey = null;
      conflictState.evidenceCache = {};
      conflictState.requestToken = 0;
    }

    function buildRowDetailModal(row) {
      const pdfDisplay = compactPdfDisplayState(row);
      const workflow = workflowMeta(row.workflow_status);
      const quality = qualityMeta(row.pdf_quality_status);
      const qualityReasonText = qualityReasonLabel(row.quality_reason);
      const extraction = compactExtractionMeta(row);
      const dftAudit = row.dft_audit && typeof row.dft_audit === "object" ? row.dft_audit : {};
      const detectedSignals = hasOwnValue(dftAudit.detected_signal_count) ? toCount(dftAudit.detected_signal_count) : 0;
      const parsedCount = hasOwnValue(dftAudit.parsed_dft_count) ? toCount(dftAudit.parsed_dft_count) : 0;
      const suspectedMissing = hasOwnValue(row.suspected_missing_dft_count)
        ? toCount(row.suspected_missing_dft_count)
        : toCount(dftAudit.suspected_missing_count);
      const conflictCount = toCount(row.review_conflict_count);
      const totalConflictCount = toCount(row.review_conflict_total_count);
      const dftConflictCount = toCount(row.dft_review_conflict_count);
      const dftConflictTotalCount = toCount(row.dft_review_conflict_total_count);
      const dftObjectReviewCount = toCount(row.dft_object_review_audit_count);
      const activeDftCount = hasOwnValue(row.active_dft_candidate_count)
        ? toCount(row.active_dft_candidate_count)
        : (row.has_active_dft_candidates !== undefined
          ? (row.has_active_dft_candidates ? toCount(row.dft_candidate_count) : 0)
          : (row.has_dft_candidates ? toCount(row.dft_candidate_count) : 0));
      const latestAudit = (row.external_audit_opinions || [])[0] || null;
      const latestAuditText = latestAudit
        ? [
            latestAudit.source_label || latestAudit.source || "外部审核",
            reviewDecisionLabel(latestAudit.verdict || latestAudit.recommended_action || "review"),
            verificationStatusLabel(latestAudit.verification_status || "unverified")
          ].filter(Boolean).join(" | ")
        : "暂无外部审核记录。";
      const dftObjectAuditLines = (row.dft_object_review_audits || []).slice(0, 5).map(function (audit) {
        const fieldLabel = audit.field_name ? ("字段 " + audit.field_name) : "";
        return [
          audit.source_label || audit.source || audit.agent_role || audit.model_name || "DFT 审核",
          fieldLabel,
          reviewDecisionLabel(audit.decision || audit.recommended_action || "review"),
          verificationStatusLabel(audit.verification_status || "unverified")
        ].filter(Boolean).join(" | ");
      });
      const objectAuditLines = (row.object_review_audits || []).slice(0, 5).map(function (audit) {
        const targetLabel = formatTargetType(audit.target_type || "");
        const fieldLabel = audit.field_name ? ("字段 " + audit.field_name) : "";
        return [
          audit.source_label || audit.source || audit.agent_role || audit.model_name || "对象审核",
          [targetLabel, fieldLabel].filter(Boolean).join(" / "),
          reviewDecisionLabel(audit.decision || audit.recommended_action || "review"),
          verificationStatusLabel(audit.verification_status || "unverified")
        ].filter(Boolean).join(" | ");
      });
      const noteLines = (row.latest_paper_notes || []).slice(0, 5).map(function (note) {
        const text = String(note.content || "").replace(/\s+/g, " ").trim();
        return [
          note.source || "AI 笔记",
          note.field_name,
          note.page ? ("p." + note.page) : "",
          text.length > 120 ? (text.slice(0, 120) + "...") : text
        ].filter(Boolean).join(" | ");
      });
      const candidateSummaryLines = compactSummarizeCounts(row.dft_candidate_status_counts || {}, candidateStatusLabel);
      const cropSummaryLines = compactSummarizeCounts(row.figure_crop_status_counts || {}, cropStatusLabel);
      const nextStep = explainDetailNextStep(row, extraction, pdfDisplay, conflictCount, activeDftCount, suspectedMissing);
      const group = supplementaryGroup(row);
      const groupSummary = group
        ? [
            supplementaryGroupLabel(row),
            "同步组候选 " + toCount(group.dft_candidate_count),
            "同步组待处理 " + toCount(group.active_dft_candidate_count),
            "主文待处理 " + toCount(group.main_active_dft_candidate_count),
            (group.support_dft_lifecycle_label || "SI 证据待闭环") + " " + toCount(
              group.support_dft_lifecycle_open_count != null
                ? group.support_dft_lifecycle_open_count
                : group.support_active_dft_candidate_count
            )
          ].filter(Boolean).join(" | ")
        : "";
      const pdfSummary = !pdfDisplay.pdf.hasPdf
        ? "当前没有可用 PDF 文件。"
        : [quality.label, qualityReasonText].filter(Boolean).join(" | ");
      const html = '<div class="detail-stack">' +
        '<section class="detail-hero">' +
          '<div class="detail-hero-chips">' +
            '<span class="chip compact ' + esc(pdfDisplay.chipClass) + '">' + esc(pdfDisplay.label) + '</span>' +
            '<span class="chip compact ' + esc(workflowChipClass(row.workflow_status)) + '">' + esc("流程：" + workflow.label) + '</span>' +
            '<span class="chip compact ' + esc(extraction.className || "subtle") + '">' + esc("DFT：" + extraction.label) + '</span>' +
          '</div>' +
          '<div class="detail-hero-title">' + esc(nextStep.title) + '</div>' +
          '<div class="detail-hero-copy">' + esc(nextStep.body) + '</div>' +
        '</section>' +
        '<section class="detail-section-card">' +
          '<div class="detail-section-head">' +
            '<div class="detail-section-title">当前状态</div>' +
            '<div class="detail-section-tip">先看这三个判断：文件能不能用、流程走到哪一步、DFT 现在是待审、漏提还是已经收口。</div>' +
          '</div>' +
          '<div class="detail-columns">' +
            compactDetailSection("PDF 状态", '<div class="detail-value">' + esc(pdfSummary) + '</div>') +
            compactDetailSection("流程状态", '<div class="detail-value">' + esc(workflow.label) + '</div><div class="detail-value muted">' + esc(workflow.tip) + '</div>') +
            compactDetailSection("DFT 状态", '<div class="detail-value">' + esc(extraction.label) + '</div><div class="detail-value muted">' + esc(extraction.tip) + '</div>') +
            (groupSummary ? compactDetailSection("主/SI 同步", '<div class="detail-value">' + esc(groupSummary) + '</div><div class="detail-value muted">' + esc(supplementaryGroupTip(row)) + '</div>') : "") +
          '</div>' +
        '</section>' +
        '<section class="detail-section-card">' +
          '<div class="detail-section-head">' +
            '<div class="detail-section-title">处理重点</div>' +
            '<div class="detail-section-tip">这些数字告诉你这篇文献当前最值得先看哪里。</div>' +
          '</div>' +
          '<div class="detail-kpi-grid">' +
            '<div class="detail-kpi"><div class="detail-kpi-label">待处理 DFT</div><div class="detail-kpi-value">' + esc(activeDftCount) + '</div><div class="detail-kpi-note">仍在候选队列里、还没收口的 DFT 项。</div></div>' +
            '<div class="detail-kpi"><div class="detail-kpi-label">DFT 冲突</div><div class="detail-kpi-value">' + esc(dftConflictCount) + '</div><div class="detail-kpi-note">当前还没处理完的 DFT 冲突项。</div></div>' +
            '<div class="detail-kpi"><div class="detail-kpi-label">DFT 审核</div><div class="detail-kpi-value">' + esc(dftObjectReviewCount) + '</div><div class="detail-kpi-note">只统计 target_type=dft_results 的对象审核。</div></div>' +
            '<div class="detail-kpi"><div class="detail-kpi-label">疑似漏提</div><div class="detail-kpi-value">' + esc(suspectedMissing) + '</div><div class="detail-kpi-note">系统怀疑原文里还有没被提取出来的 DFT 线索。</div></div>' +
            '<div class="detail-kpi"><div class="detail-kpi-label">证据定位</div><div class="detail-kpi-value">' + esc(toCount(row.evidence_count)) + '</div><div class="detail-kpi-note">目前能用于核对的定位/证据条数。</div></div>' +
          '</div>' +
          '<div class="detail-columns">' +
            compactDetailSection("候选状态", '<div class="detail-value">' + esc(candidateSummaryLines.length ? candidateSummaryLines.join("，") : "当前没有 DFT 候选。") + '</div>') +
            compactDetailSection("材料概览", '<div class="detail-value">线索 ' + esc(detectedSignals) + ' | 已解析 ' + esc(parsedCount) + ' | 图表 ' + esc(toCount(row.figure_count) + toCount(row.table_count)) + '</div><div class="detail-value muted">裁图状态：' + esc(cropSummaryLines.length ? cropSummaryLines.join("，") : "当前没有裁图记录。") + (historyConflictSummaryText(row) && conflictCount === 0 ? " | " + esc(historyConflictSummaryText(row)) : "") + '</div>') +
            compactDetailSection("冲突分布", '<div class="detail-value">DFT ' + esc(dftConflictCount) + '</div><div class="detail-value muted">历史总数：DFT ' + esc(dftConflictTotalCount) + '；非 DFT 不进入冲突统计。</div>') +
          '</div>' +
        '</section>' +
        '<section class="detail-section-card">' +
          '<div class="detail-section-head">' +
            '<div class="detail-section-title">风险提醒</div>' +
            '<div class="detail-section-tip">这里只列会影响你判断可靠性的风险；如果为空，表示当前没有明显的图表或定位告警。</div>' +
          '</div>' +
          '<div class="detail-columns">' +
            compactDetailSection("图表风险", compactIssueList(row.top_figure_issues, (row.figure_reliability || {}).issue_counts || row.figure_issue_counts || {}, figureIssueLabel, "当前没有明显的图表风险。")) +
            compactDetailSection("证据定位风险", compactIssueList(row.top_locator_issues, (row.locator_reliability || {}).issue_counts || row.locator_issue_counts || {}, locatorIssueLabel, "当前没有明显的定位风险。")) +
          '</div>' +
        '</section>' +
        '<section class="detail-section-card">' +
          '<div class="detail-section-head">' +
            '<div class="detail-section-title">审核痕迹</div>' +
            '<div class="detail-section-tip">这里不是最终结论，而是“谁看过、留下了什么意见、目前是否已经核实”的留痕。</div>' +
          '</div>' +
          '<div class="detail-columns">' +
            compactDetailSection("外部审核", '<div class="detail-value">外部审核：' + esc(toCount(row.external_audit_count)) + '</div><div class="detail-value muted">' + esc(latestAuditText) + '</div>') +
            compactDetailSection("DFT 审核", '<div class="detail-value">DFT 审核：' + esc(dftObjectReviewCount) + '</div>' + compactInfoList(dftObjectAuditLines, "暂无 DFT 对象级 AI 审核记录。")) +
            compactDetailSection("对象审核", '<div class="detail-value">对象审核总数：' + esc(toCount(row.object_review_audit_count)) + '</div>' + compactInfoList(objectAuditLines, "暂无对象级 AI 审核记录。")) +
            compactDetailSection("AI 笔记", '<div class="detail-value">AI 笔记：' + esc(toCount(row.paper_note_count)) + '</div>' + compactInfoList(noteLines, "暂无 IDE AI 回写笔记。")) +
          '</div>' +
        '</section>' +
      '</div>';
      return {
        title: row.title || "文献详情",
        subtitle: [row.journal || "", row.year || "", row.doi || ""].filter(Boolean).join(" | "),
        html: html
      };
    }

    function renderConflictChip(activeCount, totalCount, activeLabel, historyLabel, titleActive, titleHistory) {
      const active = toCount(activeCount);
      const total = toCount(totalCount);
      if (active > 0) {
        return '<span class="chip compact bad" data-action="open-conflicts" title="' + esc(titleActive) + '">' + esc(activeLabel + " " + active) + '</span>';
      }
      if (total > 0) {
        return '<span class="chip compact subtle" title="' + esc(titleHistory) + '">' + esc(historyLabel + " " + total) + '</span>';
      }
      return "";
    }

    function historyConflictSummaryText(row) {
      const parts = [];
      const dft = toCount(row.dft_review_conflict_total_count);
      if (dft > 0) parts.push("DFT " + dft);
      return parts.length ? ("历史已处理冲突：" + parts.join(" | ")) : "";
    }

      function formatTargetType(targetType) {
        const mapping = {
          section: "章节",
          sections: "章节",
          dft_setting: "DFT 设置",
          dft_settings: "DFT 设置",
          catalyst_sample: "催化剂样本",
          catalyst_samples: "催化剂样本",
          dft_results: "DFT 字段",
          electrochemical_performance: "电化学性能",
          writing_card: "论文重点",
          writing_cards: "论文重点",
          mechanism_claim: "机理内容",
          mechanism_claims: "机理内容",
          figure: "图片",
          figures: "图片",
          table: "表格",
          tables: "表格"
        };
        return mapping[targetType] || targetType || "冲突对象";
      }

    function formatConflictType(type) {
      const mapping = {
        value_conflict: "数值冲突",
        unit_conflict: "单位冲突",
        decision_conflict: "结论冲突",
        locator_conflict: "定位冲突",
        mapping_conflict: "映射冲突"
      };
      return mapping[type] || type || "冲突";
    }

    function conflictSeverity(type) {
      if (type === "value_conflict" || type === "decision_conflict") return "high";
      if (type === "unit_conflict" || type === "mapping_conflict" || type === "identity_conflict") return "medium";
      if (type === "locator_conflict") return "low";
      return "medium";
    }

    function conflictSeverityClass(type) {
      const severity = conflictSeverity(type);
      if (severity === "high") return "bad";
      if (severity === "medium") return "mid";
      return "low";
    }

    function groupSeverity(item) {
      const types = Array.isArray(item.conflict_types) ? item.conflict_types : [];
      if (types.some(function (type) { return conflictSeverity(type) === "high"; })) return "high";
      if (types.some(function (type) { return conflictSeverity(type) === "medium"; })) return "medium";
      return "low";
    }

    function formatOpinionValue(opinion) {
      const parts = [opinion && opinion.value, opinion && opinion.unit].filter(function (part) {
        return part !== null && part !== undefined && String(part).trim() !== "";
      });
      return parts.length ? parts.join(" ") : "未知";
    }

    function opinionIdentity(opinion) {
      return opinion && opinion.identity && typeof opinion.identity === "object" ? opinion.identity : {};
    }

    function itemTargetSummary(item) {
      return item && item.target_summary && typeof item.target_summary === "object" ? item.target_summary : {};
    }

    function itemAnchorSummary(item) {
      return item && item.anchor_summary && typeof item.anchor_summary === "object" ? item.anchor_summary : {};
    }

    function joinDisplayParts(parts) {
      return parts.filter(function (part) {
        return part !== null && part !== undefined && String(part).trim() !== "";
      }).join(" | ");
    }

    function opinionIdentitySummary(opinion) {
      const identity = opinionIdentity(opinion);
      return joinDisplayParts([
        identity.normalized_energy_type,
        identity.normalized_material,
        identity.structure_name,
        identity.adsorbate,
        identity.reaction_step
      ]) || identity.object_label || "";
    }

    function targetSummaryLabel(item) {
      const summary = itemTargetSummary(item);
      return summary.object_label || joinDisplayParts([
        summary.property_type,
        summary.normalized_material,
        summary.structure_name,
        summary.adsorbate,
        summary.reaction_step,
        summary.source_section,
        summary.figure_label,
        summary.caption
      ]) || (item && item.target_id) || "-";
    }

    function anchorSummaryText(anchor) {
      const summary = anchor && typeof anchor === "object" ? anchor : {};
      return joinDisplayParts([
        hasOwnValue(summary.page) ? ("page " + summary.page) : "",
        summary.section,
        summary.table,
        summary.figure,
        summary.locator_status
      ]) || "missing locator";
    }

    function adjudicationRoleBadge(opinion) {
      const role = String(opinion && opinion.adjudication_role || "").trim().toLowerCase();
      if (!role) return "";
      return '<span class="chip warn">' + esc(role === "third_ai" ? "历史裁决意见" : role) + '</span>';
    }

    function formatLocatorSummary(opinion) {
      return anchorSummaryText(opinion && opinion.anchor_summary);
    }

    function isWeakLocator(opinion) {
      const evidence = opinion && opinion.evidence && typeof opinion.evidence === "object" ? opinion.evidence : {};
      const locator = evidence.locator && typeof evidence.locator === "object" ? evidence.locator : evidence;
      return ["text_only", "missing_bbox", "missing_page"].includes(locator.locator_status);
    }

    function locatorPayloadFromEvidence(evidence) {
      const payload = Array.isArray(evidence) ? (evidence[0] || {}) : (evidence || {});
      if (!payload || typeof payload !== "object") return {};
      if (payload.locator && typeof payload.locator === "object") return payload.locator;
      if (payload.evidence_location && typeof payload.evidence_location === "object") return payload.evidence_location;
      return payload;
    }

    function opinionEvidencePayload(opinion) {
      if (!opinion || !opinion.evidence) return {};
      const evidence = Array.isArray(opinion.evidence) ? (opinion.evidence[0] || {}) : opinion.evidence;
      return evidence && typeof evidence === "object" ? evidence : {};
    }

    function locatorStatusLabel(status) {
      const mapping = {
        exact_page: "精确页码",
        exact_bbox: "精确框选",
        text_only: "仅文本定位",
        missing_bbox: "缺少框选",
        missing_page: "缺少页码",
        missing_locator: "缺少定位",
        approximate_locator: "近似定位",
        unresolved_locator: "未解析定位"
      };
      if (status && mapping[status]) return mapping[status] + " " + status;
      return status || "缺少定位";
    }

    function reliableLocatorStatus(status) {
      return ["exact_page", "exact_bbox"].includes(status);
    }

    function normalizeWhitespace(value) {
      return String(value || "").replace(/\s+/g, " ").trim();
    }

    function clipText(value, maxChars) {
      const text = normalizeWhitespace(value);
      if (!text) return "";
      const max = Number(maxChars || 0);
      if (!max || text.length <= max) return text;
      return text.slice(0, max - 1).trimEnd() + "…";
    }

    function formatEvidenceSourceType(sourceType) {
      const mapping = {
        section: "section",
        table: "table",
        figure: "figure",
        writing_card: "writing card",
        mechanism_claim: "mechanism claim",
        dft_result: "dft_result",
        dft_results: "dft_result",
        extraction_field_review: "review payload",
        object_review_audit: "object review",
        external_audit_opinion: "external audit",
        paper_correction: "correction proposal"
      };
      return mapping[sourceType] || sourceType || "unknown";
    }

    function deriveEvidenceSourceLabel(evidence, item, opinion) {
      const payload = evidence || {};
      return payload.source_label
        || payload.section_title
        || payload.section
        || payload.label
        || payload.table_label
        || payload.figure_label
        || payload.figure_caption
        || payload.caption
        || opinion.source_label
        || formatTargetType(item && item.target_type);
    }

    function extractNearbyContextLines(payload) {
      const lines = [];
      if (payload && typeof payload === "object") {
        if (Array.isArray(payload.related_sections)) {
          payload.related_sections.slice(0, 2).forEach(function (section) {
            const title = section && (section.title || section.section_type || "section");
            const text = clipText(section && section.text, 280);
            if (text) lines.push((title ? title + ": " : "") + text);
          });
        }
        [payload.context_before, payload.context_after, payload.nearby_context, payload.section_text].forEach(function (value) {
          const text = clipText(value, 280);
          if (text && lines.indexOf(text) === -1) lines.push(text);
        });
      }
      return lines.slice(0, 2);
    }

    function extractOpinionEvidenceKey(item, opinion, groupIndex, opinionIndex) {
      return [
        "conflict-evidence",
        groupIndex,
        opinionIndex,
        item && item.target_type || "target",
        item && item.field_name || "field"
      ].join(":");
    }

    function canUseCodexItem(row, item) {
        if (!row || !item) return false;
        if (!UUID_RE.test(String(row.paper_id || ""))) return false;
        if (!UUID_RE.test(String(item.target_id || ""))) return false;
        return [
          "section",
          "sections",
          "dft_setting",
          "dft_settings",
          "catalyst_sample",
          "catalyst_samples",
          "dft_results",
          "electrochemical_performance",
          "writing_card",
          "writing_cards",
          "mechanism_claim",
          "mechanism_claims",
          "figure",
          "figures",
          "table",
          "tables"
        ].includes(item.target_type);
      }

      function bestDeepLinkPdfLocator(item) {
        const opinions = item && Array.isArray(item.opinions) ? item.opinions : [];
        for (let i = 0; i < opinions.length; i += 1) {
          const evidence = opinionEvidencePayload(opinions[i]);
          const locator = locatorPayloadFromEvidence(evidence);
          const page = hasOwnValue(locator.page) ? Number(locator.page) : null;
          const status = String(locator.locator_status || "").trim().toLowerCase();
          if (Number.isFinite(page) && page > 0 && reliableLocatorStatus(status)) {
            return {
              page: page,
              locator_status: status,
              evidence_text: evidence.evidence_text || locator.evidence_text || ""
            };
          }
        }
        return null;
      }

    function canPreviewEvidence(row, item, opinion) {
      if (canUseCodexItem(row, item)) return true;
      const evidence = opinionEvidencePayload(opinion);
      const locator = locatorPayloadFromEvidence(evidence);
      return Boolean(
        normalizeWhitespace(evidence.evidence_text || evidence.excerpt || evidence.text || "").length
        || normalizeWhitespace(opinion && opinion.reason).length
        || normalizeWhitespace(deriveEvidenceSourceLabel(evidence, item, opinion)).length
        || hasOwnValue(locator.page)
        || normalizeWhitespace(locator.locator_status).length
      );
    }

    function buildEvidencePreviewPlaceholder(message) {
      return '<div class="evidence-preview-card is-placeholder">' +
        '<div class="evidence-preview-title">证据预览</div>' +
        '<div class="detail-value muted">' + esc(message || "请选择一条意见查看原文片段") + '</div>' +
      '</div>';
    }

    function buildEvidencePreviewPanel(payload) {
      if (!payload) return buildEvidencePreviewPlaceholder("请选择一条意见查看原文片段");
      if (payload.loading) return buildEvidencePreviewPlaceholder("正在读取原文片段...");
      const nearbyLines = Array.isArray(payload.nearbyContext) ? payload.nearbyContext.filter(Boolean) : [];
      const materialStructure = [payload.material, payload.structureName].filter(Boolean).join(" | ");
      const adsorbateReaction = [payload.adsorbate, payload.reactionStep].filter(Boolean).join(" | ");
      const metaBlocks = [
        { label: "来源对象", value: payload.objectLabel || "-", wide: true },
        { label: "审核来源", value: payload.reviewSource || "-" },
        { label: "字段", value: payload.fieldName || "-" },
        { label: "页码", value: hasOwnValue(payload.page) ? payload.page : "-" },
        { label: "定位", value: payload.locatorStatus || "missing_locator" },
        { label: "来源标签", value: payload.sourceLabel || "-" },
        { label: "对象类型", value: payload.targetType || "-", wide: true },
      ];
      if (payload.energyType) metaBlocks.push({ label: "能量类型", value: payload.energyType });
      if (materialStructure) metaBlocks.push({ label: "材料 / 结构", value: materialStructure, wide: true });
      if (adsorbateReaction) metaBlocks.push({ label: "吸附物 / 步骤", value: adsorbateReaction, wide: true });
      if (payload.anchorText && payload.anchorText !== "-") metaBlocks.push({ label: "证据锚点", value: payload.anchorText, wide: true });
      if (payload.sourceType && payload.sourceType !== "未知") metaBlocks.push({ label: "来源类型", value: payload.sourceType });
      return '<div class="evidence-preview-card">' +
        '<div class="evidence-preview-title">证据预览</div>' +
        '<div class="evidence-preview-meta">' +
          metaBlocks.map(function (item) {
            return '<div class="detail-block' + (item.wide ? ' is-wide' : '') + '"><div class="detail-label">' + esc(item.label) + '</div><div class="detail-value">' + esc(item.value || "-") + '</div></div>';
          }).join("") +
        '</div>' +
        '<div class="evidence-preview-section"><div class="detail-label">原文片段</div><div class="detail-value evidence-excerpt">' + esc(payload.excerpt || "当前没有可展示的原文片段") + '</div></div>' +
        '<div class="evidence-preview-section"><div class="detail-label">附近上下文</div>' +
          (nearbyLines.length
            ? '<ul class="detail-list">' + nearbyLines.map(function (line) { return '<li>' + esc(line) + '</li>'; }).join("") + '</ul>'
            : '<div class="detail-value muted">当前没有更多上下文。</div>') +
        '</div>' +
        (payload.note ? '<div class="detail-value muted">' + esc(payload.note) + '</div>' : '') +
      '</div>';
    }

    function renderEvidencePreview(payload) {
      const panel = document.getElementById("conflictEvidencePanel");
      if (!panel) return;
      panel.innerHTML = buildEvidencePreviewPanel(payload);
      Array.from(document.querySelectorAll("#infoModalBody tr[data-opinion-key]")).forEach(function (rowNode) {
        rowNode.classList.toggle("is-active", rowNode.getAttribute("data-opinion-key") === conflictState.activeOpinionKey);
      });
    }

    function fallbackEvidencePreview(row, item, opinion) {
      const evidence = opinionEvidencePayload(opinion);
      const locator = locatorPayloadFromEvidence(evidence);
      return {
        targetType: formatTargetType(item && item.target_type),
        objectLabel: targetSummaryLabel(item) || opinionIdentitySummary(opinion),
        fieldName: item && item.field_name || "-",
        reviewSource: reviewSourceLabel(opinion),
        page: hasOwnValue(locator.page) ? locator.page : null,
        locatorStatus: locatorStatusLabel(locator.locator_status),
        sourceType: formatEvidenceSourceType(evidence.source_type || item && item.target_type || opinion.source_type),
        sourceLabel: deriveEvidenceSourceLabel(evidence, item, opinion),
        energyType: opinionIdentity(opinion).normalized_energy_type || itemTargetSummary(item).property_type || "",
        material: opinionIdentity(opinion).normalized_material || itemTargetSummary(item).normalized_material || "",
        structureName: opinionIdentity(opinion).structure_name || itemTargetSummary(item).structure_name || "",
        adsorbate: opinionIdentity(opinion).adsorbate || itemTargetSummary(item).adsorbate || "",
        reactionStep: opinionIdentity(opinion).reaction_step || itemTargetSummary(item).reaction_step || "",
        anchorText: anchorSummaryText(opinion.anchor_summary || itemAnchorSummary(item)),
        excerpt: clipText(evidence.evidence_text || evidence.excerpt || evidence.text, 1200),
        nearbyContext: extractNearbyContextLines(evidence),
        note: canUseCodexItem(row, item) ? "" : "当前预览基于冲突聚合中的只读证据字段。"
      };
    }

    function bestLocatorForPreview(locators, fieldName) {
      const items = Array.isArray(locators) ? locators.slice() : [];
      const scoped = items.filter(function (item) {
        return !fieldName || !item.field_name || item.field_name === fieldName;
      });
      const pool = scoped.length ? scoped : items;
      pool.sort(function (left, right) {
        const leftScore = reliableLocatorStatus(left.locator_status) ? 2 : (hasOwnValue(left.page) ? 1 : 0);
        const rightScore = reliableLocatorStatus(right.locator_status) ? 2 : (hasOwnValue(right.page) ? 1 : 0);
        return rightScore - leftScore;
      });
      return pool[0] || null;
    }

    function mergeCodexItemPreview(base, contextPayload, item) {
      const context = contextPayload && contextPayload.context && typeof contextPayload.context === "object"
        ? contextPayload.context
        : {};
      const locatorItems = (context.evidence_locators && context.evidence_locators.items) || [];
      const bestLocator = bestLocatorForPreview(locatorItems, item && item.field_name);
      const contextItem = context.item && typeof context.item === "object" ? context.item : {};
      const excerpt = clipText(
        (bestLocator && bestLocator.evidence_text)
          || base.excerpt
          || contextItem.evidence_text
          || (item && item.field_name && typeof contextItem[item.field_name] === "string" ? contextItem[item.field_name] : ""),
        1200
      );
      const nearbyContext = extractNearbyContextLines(context.nearby_context || {});
      return {
        targetType: base.targetType,
        objectLabel: base.objectLabel,
        fieldName: base.fieldName,
        reviewSource: base.reviewSource,
        page: bestLocator && hasOwnValue(bestLocator.page) ? bestLocator.page : base.page,
        locatorStatus: locatorStatusLabel(bestLocator && bestLocator.locator_status || base.locatorStatus),
        sourceType: formatEvidenceSourceType(bestLocator && bestLocator.target_type || context.item_type || base.sourceType),
        sourceLabel: (bestLocator && (bestLocator.section || bestLocator.field_name)) || base.sourceLabel,
        energyType: base.energyType,
        material: base.material,
        structureName: base.structureName,
        adsorbate: base.adsorbate,
        reactionStep: base.reactionStep,
        anchorText: anchorSummaryText(bestLocator || {}) || base.anchorText,
        excerpt: excerpt,
        nearbyContext: nearbyContext.length ? nearbyContext : base.nearbyContext,
        note: "当前预览优先复用了 codex-item 只读上下文。"
      };
    }

    async function loadOpinionEvidence(row, item, opinion, opinionKey) {
      if (conflictState.evidenceCache[opinionKey]) return conflictState.evidenceCache[opinionKey];
      const fallback = fallbackEvidencePreview(row, item, opinion);
      if (!canUseCodexItem(row, item)) {
        conflictState.evidenceCache[opinionKey] = fallback;
        return fallback;
      }
      try {
        const payload = await fetchJSON("/api/papers/" + encodeURIComponent(row.paper_id) + "/codex-item/" + encodeURIComponent(item.target_type) + "/" + encodeURIComponent(item.target_id));
        const merged = mergeCodexItemPreview(fallback, payload, item);
        conflictState.evidenceCache[opinionKey] = merged;
        return merged;
      } catch (error) {
        const degraded = Object.assign({}, fallback, {
          note: "codex-item 只读上下文暂不可用，已退回冲突聚合中的证据字段。"
        });
        conflictState.evidenceCache[opinionKey] = degraded;
        return degraded;
      }
    }

    function buildConflictSummary(conflictRows) {
      const rows = Array.isArray(conflictRows) ? conflictRows : [];
      const sourceSet = new Set();
      let highPriority = 0;
      let weakLocator = 0;
      rows.forEach(function (item) {
        const opinions = Array.isArray(item.opinions) ? item.opinions : [];
        if ((item.conflict_types || []).some(function (type) {
          return type === "value_conflict" || type === "decision_conflict";
        })) {
          highPriority += 1;
        }
        opinions.forEach(function (opinion) {
          sourceSet.add(opinion.source_label || opinion.source || opinion.agent_role || opinion.model_name || "review");
          if (isWeakLocator(opinion)) weakLocator += 1;
        });
      });
      return {
        conflictObjects: rows.length,
        opinionSources: sourceSet.size,
        highPriority: highPriority,
        weakLocator: weakLocator
      };
    }

    function buildConflictStatusPanel(summary, adjudicationSummary) {
      const manualCount = toCount(adjudicationSummary.manual);
      const suggestCount = toCount(adjudicationSummary.suggest);
      const autoCount = toCount(adjudicationSummary.auto);
      const mainCount = manualCount || suggestCount || summary.conflictObjects;
      const title = mainCount
        ? ("当前还有 " + mainCount + " 个需要关注的冲突，其中 " + toCount(summary.highPriority) + " 个高优先级。")
        : "当前没有需要处理的冲突。";
      return '<section class="conflict-status-panel">' +
        '<div class="conflict-status-head">' +
          '<div class="conflict-status-copy">' +
            '<div class="conflict-status-title">' + esc(title) + '</div>' +
            '<div class="conflict-status-note">先处理“必须处理”和“高优先级”，证据不稳的条目再回原文页核对。</div>' +
          '</div>' +
          '<div class="conflict-status-actions">' +
            '<button class="btn btn-ghost btn-sm" type="button" data-action="toggle-conflict-list">' + esc(conflictState.listCollapsed ? "显示冲突列表" : "隐藏冲突列表") + '</button>' +
          '</div>' +
        '</div>' +
        '<div class="conflict-summary-bar">' +
          '<span class="chip bad">必须处理 ' + esc(manualCount) + '</span>' +
          '<span class="chip warn">建议裁定 ' + esc(suggestCount) + '</span>' +
          '<span class="chip good">自动推进 ' + esc(autoCount) + '</span>' +
          '<span class="chip bad">高优先级 ' + esc(toCount(summary.highPriority)) + '</span>' +
          '<span class="chip low">定位偏弱 ' + esc(toCount(summary.weakLocator)) + '</span>' +
          '<span class="chip subtle">冲突对象 ' + esc(toCount(summary.conflictObjects)) + '</span>' +
          '<span class="chip subtle">意见来源 ' + esc(toCount(summary.opinionSources)) + '</span>' +
        '</div>' +
      '</section>';
    }

    function buildConflictListItem(item, index) {
      const adjudication = item.adjudication || {};
      const severity = groupSeverity(item);
      const severityClass = severity === "high" ? "bad" : (severity === "medium" ? "warn" : "low");
      const firstType = (item.conflict_types || [])[0] || "conflict";
      return '<button class="conflict-list-item' + (index === conflictState.activeGroupIndex ? ' is-active' : '') + '" type="button" data-action="select-conflict" data-group-index="' + esc(index) + '">' +
        '<div class="conflict-list-title">' + esc(targetSummaryLabel(item)) + '</div>' +
        '<div class="conflict-list-meta">' + esc(formatTargetType(item.target_type) + " / " + (item.field_name || "field")) + '</div>' +
        '<div class="conflict-list-chips">' +
          '<span class="chip ' + esc(severityClass) + '">' + esc(formatConflictType(firstType)) + '</span>' +
          '<span class="chip ' + esc(adjudicationClass(adjudication.adjudication_mode)) + '">' + esc(adjudicationLabel(adjudication.adjudication_mode)) + '</span>' +
          '<span class="chip subtle">来源 ' + esc(toCount(item.reviewer_count)) + '</span>' +
        '</div>' +
        '</button>';
    }

    function setConflictListCollapsed(collapsed) {
      conflictState.listCollapsed = Boolean(collapsed);
      const grid = document.querySelector("#infoModalBody .conflict-workbench-grid");
      if (grid) grid.classList.toggle("is-list-collapsed", conflictState.listCollapsed);
      Array.from(document.querySelectorAll('#infoModalBody [data-action="toggle-conflict-list"]')).forEach(function (button) {
        button.textContent = conflictState.listCollapsed ? "显示冲突列表" : "隐藏冲突列表";
      });
    }

    function renderSelectedConflict(index) {
      const nextIndex = Number(index);
      if (!Number.isFinite(nextIndex) || !conflictState.groups[nextIndex]) return;
      conflictState.activeGroupIndex = nextIndex;
      conflictState.activeOpinionKey = null;
      conflictState.requestToken += 1;
      Array.from(document.querySelectorAll("#infoModalBody .conflict-list-item")).forEach(function (node) {
        node.classList.toggle("is-active", Number(node.getAttribute("data-group-index")) === nextIndex);
      });
      const panel = document.getElementById("selectedConflictPanel");
      if (panel) panel.innerHTML = buildConflictGroupCard(conflictState.groups[nextIndex], conflictState.activeRow, nextIndex);
      renderEvidencePreview(null);
      previewFirstEvidenceForConflict(nextIndex);
    }

    function previewFirstEvidenceForConflict(groupIndex) {
      const item = conflictState.groups[groupIndex];
      const row = conflictState.activeRow;
      const opinions = item && Array.isArray(item.opinions) ? item.opinions : [];
      if (!item || !row || !opinions.length) return;
      const opinionIndex = opinions.findIndex(function (opinion) {
        return canPreviewEvidence(row, item, opinion);
      });
      if (opinionIndex < 0) return;
      const opinion = opinions[opinionIndex];
      const opinionKey = extractOpinionEvidenceKey(item, opinion, groupIndex, opinionIndex);
      conflictState.activeOpinionKey = opinionKey;
      const requestToken = conflictState.requestToken + 1;
      conflictState.requestToken = requestToken;
      renderEvidencePreview({ loading: true });
      loadOpinionEvidence(row, item, opinion, opinionKey).then(function (payload) {
        if (conflictState.requestToken !== requestToken) return;
        conflictState.activeOpinionKey = opinionKey;
        renderEvidencePreview(payload);
      }).catch(function (error) {
        if (conflictState.requestToken !== requestToken) return;
        renderEvidencePreview({
          targetType: formatTargetType(item.target_type),
          fieldName: item.field_name || "-",
          reviewSource: reviewSourceLabel(opinion),
          locatorStatus: "missing_locator",
          sourceType: formatEvidenceSourceType(item.target_type),
          sourceLabel: deriveEvidenceSourceLabel(opinionEvidencePayload(opinion), item, opinion),
          excerpt: "",
          nearbyContext: [],
          note: "加载原文片段失败：" + error.message
        });
      });
    }

    function confidenceLabel(value) {
      if (!hasOwnValue(value) || value === "") return "未知";
      const num = Number(value);
      if (!Number.isFinite(num)) return String(value);
      return num.toFixed(2);
    }

    function reviewSourceLabel(opinion) {
      const raw = String(opinion.source_label || opinion.source || opinion.agent_role || opinion.model_name || opinion.source_type || "review");
      if (raw === "codex_live_test") return "现场核验";
      if (/^verify:.*:primary$/i.test(raw)) return "主审核 AI";
      if (/^verify:.*:secondary$/i.test(raw)) return "AI 审核意见";
      if (/^verify:.*:(third_ai|primary)$/i.test(raw)) return "历史裁决意见";
      if (raw === "manual_adjudication") return "确认裁定";
      return raw;
    }

    function roleModelLabel(opinion) {
      const parts = [opinion.agent_role, opinion.model_name].filter(function (part) {
        return part !== null && part !== undefined && String(part).trim() !== "";
      });
      return parts.length ? parts.join(" / ") : "-";
    }

    function opinionSourceMeta(opinion) {
      const raw = String(opinion.source || opinion.source_type || "").trim();
      if (!raw) return "";
      if (/^verify:/.test(raw)) {
        const parts = raw.split(":");
        return parts.length >= 3 ? ("审核批次 / " + parts[parts.length - 1]) : "审核批次";
      }
      return raw;
    }

    function opinionDecisionLabel(opinion) {
      const raw = String(opinion.decision || opinion.status || "review").trim();
      const mapping = {
        PASS: "通过",
        pass: "通过",
        verified: "已核验",
        review: "待复核",
        reject: "拒绝",
        rejected: "已拒绝",
        correction: "需修正",
        corrected: "已修正",
        needs_fix: "需修正",
        needs_review: "待复核"
      };
      return mapping[raw] || raw;
    }

    function opinionReasonLabel(reason) {
      const raw = String(reason || "").trim();
      if (!raw) return "未填写理由";
      const exact = {
        "Manual adjudication selected this AI opinion.": "确认裁定采用了这条 AI 意见。",
        "Imported object-level external review audit candidate": "导入的对象级外部审核候选。",
        "No reason provided.": "未填写理由。"
      };
      if (exact[raw]) return exact[raw];
      return raw
        .replaceAll("missing_locator", "缺少定位")
        .replaceAll("missing locator", "缺少定位")
        .replaceAll("Imported object-level external review audit candidate", "导入的对象级外部审核候选")
        .replaceAll("Manual adjudication selected this AI opinion.", "确认裁定采用了这条 AI 意见。");
    }

    function bestOpenPage(row, opinions) {
      if (!row || !row.pdf_url) return null;
      for (let i = 0; i < opinions.length; i += 1) {
        const evidence = opinions[i] && opinions[i].evidence && typeof opinions[i].evidence === "object" ? opinions[i].evidence : {};
        const locator = evidence.locator && typeof evidence.locator === "object" ? evidence.locator : evidence;
        if (hasOwnValue(locator.page) && !["text_only", "missing_page", "missing_locator"].includes(locator.locator_status)) {
          return Number(locator.page);
        }
      }
      return null;
    }

    function buildPdfButton(row, page, fallbackTitle) {
      const label = page ? ("Open page " + page) : "Open PDF";
      if (row && row.pdf_url && page) {
        return '<a class="btn btn-ghost btn-sm btn-weak" href="' + esc(row.pdf_url + "#page=" + page) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + '</a>';
      }
      return '<button class="btn btn-ghost btn-sm btn-weak" type="button" disabled title="' + esc(fallbackTitle || "当前定位不足，不能可靠跳页") + '">' + esc(label) + '</button>';
    }

    function canOpenPdf(opinion, row) {
      const locator = locatorPayloadFromEvidence(opinionEvidencePayload(opinion));
      const status = locator.locator_status || "missing_locator";
      const page = hasOwnValue(locator.page) ? Number(locator.page) : null;
      if (!row || !row.pdf_url) {
        return { enabled: false, label: "Open PDF", href: "", title: "当前 paper 没有可用 PDF" };
      }
      if (!hasOwnValue(page) || !Number.isFinite(page)) {
        return { enabled: false, label: "Open PDF", href: "", title: "当前定位不足，不能可靠跳页" };
      }
      if (!reliableLocatorStatus(status)) {
        return { enabled: false, label: "Open page " + page, href: "", title: "当前定位不足，不能可靠跳页" };
      }
      return {
        enabled: true,
        label: "Open page " + page,
        href: row.pdf_url + "#page=" + page,
        title: "Open page " + page
      };
    }

    function buildPdfAction(opinion, row) {
      const action = canOpenPdf(opinion, row);
      if (action.enabled) {
        return '<a class="btn btn-ghost btn-sm btn-weak" href="' + esc(action.href) + '" target="_blank" rel="noopener noreferrer">' + esc(action.label) + '</a>';
      }
      return '<button class="btn btn-ghost btn-sm btn-weak" type="button" disabled title="' + esc(action.title) + '">' + esc(action.label) + '</button>';
    }

    function buildEvidenceAction(opinion, item, row, cardIndex, opinionIndex) {
      const key = extractOpinionEvidenceKey(item, opinion, cardIndex, opinionIndex);
      const enabled = canPreviewEvidence(row, item, opinion);
      return '<button class="btn btn-ghost btn-sm btn-weak" type="button" data-action="view-evidence" data-group-index="' + esc(cardIndex) + '" data-opinion-index="' + esc(opinionIndex) + '" data-opinion-key="' + esc(key) + '"' +
        (enabled ? '' : ' disabled title="当前没有可展示的原文片段"') + '>查看原文</button>';
    }

    function buildAdoptOpinionAction(item, opinion, cardIndex, opinionIndex) {
      if (!canManualAdoptOpinion(item)) return "";
      return '<button class="btn btn-ghost btn-sm btn-weak" type="button" data-action="adopt-opinion" data-group-index="' + esc(cardIndex) + '" data-source-id="' + esc(opinion.source_id || "") + '">采用此条</button>';
    }

    function buildOpinionCompareRow(item, opinion, row, cardIndex, opinionIndex) {
      const reason = opinionReasonLabel(opinion.reason || "No reason provided.");
      const reasonId = "reason-" + cardIndex + "-" + opinionIndex;
      const showExpand = reason.length > 96;
      const opinionKey = extractOpinionEvidenceKey(item, opinion, cardIndex, opinionIndex);
      const identityText = opinionIdentitySummary(opinion);
      const sourceMetaLines = [opinionSourceMeta(opinion), roleModelLabel(opinion)].filter(function (part) {
        return part && part !== "-";
      });
      return '<tr data-opinion-key="' + esc(opinionKey) + '" class="' + (opinionIndex > 2 ? 'compare-extra' : '') + '"' + (opinionIndex > 2 ? ' hidden' : '') + '>' +
        '<td><div class="opinion-source">' + esc(reviewSourceLabel(opinion)) + '</div><div class="opinion-submeta">' + esc(sourceMetaLines.length ? sourceMetaLines.join(" | ") : "审核记录") + '</div></td>' +
        '<td><div class="opinion-action">' + '<span class="chip">' + esc(opinionDecisionLabel(opinion)) + '</span>' + adjudicationRoleBadge(opinion) + '</div></td>' +
        '<td><div>' + esc(formatOpinionValue(opinion)) + '</div><div class="opinion-submeta">置信度 ' + esc(confidenceLabel(opinion.confidence)) + '</div>' + (identityText ? '<div class="opinion-submeta">' + esc(identityText) + '</div>' : '') + '</td>' +
        '<td><div class="opinion-submeta" style="margin-top:0;">' + esc(formatLocatorSummary(opinion)) + '</div><div class="reason-block"><div id="' + esc(reasonId) + '" class="reason-text' + (showExpand ? ' truncated' : '') + '">' + esc(reason) + '</div>' +
        (showExpand ? '<button class="expand-link" type="button" data-action="toggle-reason" data-target="' + esc(reasonId) + '">展开理由</button>' : '') +
        '</div></td>' +
        '<td><div class="opinion-action-stack">' + buildEvidenceAction(opinion, item, row, cardIndex, opinionIndex) + buildPdfAction(opinion, row) + buildAdoptOpinionAction(item, opinion, cardIndex, opinionIndex) + '</div></td>' +
      '</tr>';
    }

    function buildConflictGroupCard(item, row, index) {
      const opinions = Array.isArray(item.opinions) ? item.opinions : [];
      const extraCount = Math.max(opinions.length - 3, 0);
      const severity = groupSeverity(item);
      const extraId = "conflict-extra-" + index;
      const bestPage = bestOpenPage(row, opinions);
      const adjudication = item.adjudication || {};
      const blockedReasons = Array.isArray(adjudication.blocked_reasons) ? adjudication.blocked_reasons : [];
      const summary = itemTargetSummary(item);
      const anchor = itemAnchorSummary(item);
      const actionLabel = isDftConflictItem(item) ? "前往 DFT 详情确认处理" : adjudicationActionLabel(adjudication.recommended_action);
      return '<section class="conflict-card is-' + esc(severity) + '">' +
        '<div class="conflict-card-head">' +
        '<div class="conflict-card-left">' +
        '<div class="conflict-title">' + esc(targetSummaryLabel(item)) + '</div>' +
        '<div class="conflict-meta">' + esc(formatTargetType(item.target_type)) + ' / ' + esc(item.field_name || "field") + '</div>' +
        '<div class="conflict-meta">target_id: ' + esc(item.target_id || "-") + '</div>' +
        '</div>' +
        '<div class="conflict-card-right">' +
        (item.conflict_types || []).map(function (type) {
          return '<span class="chip ' + esc(conflictSeverityClass(type)) + '" title="' + esc(type) + '">' + esc(formatConflictType(type)) + '</span>';
        }).join("") +
        '<span class="chip">审核来源 ' + esc(toCount(item.reviewer_count)) + '</span>' +
        buildPdfButton(row, bestPage, "当前定位不足，不能可靠跳页") +
        '</div>' +
        '</div>' +
        '<div class="conflict-body">' +
        '<div class="conflict-object-grid">' +
          '<div class="conflict-object-item"><div class="conflict-object-label">争议字段</div><div class="conflict-object-value">' + esc(formatTargetType(item.target_type) + " / " + (item.field_name || "-")) + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">对象</div><div class="conflict-object-value">' + esc(targetSummaryLabel(item)) + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">证据锚点</div><div class="conflict-object-value">' + esc(anchorSummaryText(anchor)) + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">建议动作</div><div class="conflict-object-value">' + esc(actionLabel) + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">能量类型</div><div class="conflict-object-value">' + esc(summary.property_type || summary.normalized_energy_type || "-") + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">材料 / 结构</div><div class="conflict-object-value">' + esc(joinDisplayParts([summary.normalized_material, summary.structure_name]) || "-") + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">吸附物 / 步骤</div><div class="conflict-object-value">' + esc(joinDisplayParts([summary.adsorbate, summary.reaction_step]) || "-") + '</div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">冲突类型</div><div class="conflict-chip-row">' + (item.conflict_types || []).map(function (type) {
            return '<span class="chip ' + esc(conflictSeverityClass(type)) + '" title="' + esc(type) + '">' + esc(formatConflictType(type)) + '</span>';
          }).join("") + '</div></div>' +
        '</div>' +
        '<section class="evidence-preview-shell"><div id="conflictEvidencePanel">' + buildEvidencePreviewPlaceholder("选择下方意见后查看原文片段、页码和定位状态。") + '</div></section>' +
        '<div class="conflict-object-grid">' +
          '<div class="conflict-object-item"><div class="conflict-object-label">AI 裁定</div><div class="conflict-object-value"><span class="chip ' + esc(adjudicationClass(adjudication.adjudication_mode)) + '">' + esc(adjudicationLabel(adjudication.adjudication_mode)) + '</span></div></div>' +
          '<div class="conflict-object-item"><div class="conflict-object-label">裁定理由</div><div class="conflict-object-value">' + esc(adjudication.reason_summary || "暂无 AI 裁定摘要。") + '</div></div>' +
          (blockedReasons.length ? '<div class="conflict-object-item"><div class="conflict-object-label">阻断原因</div><div class="conflict-object-value">' + esc(blockedReasons.join(", ")) + '</div></div>' : '') +
        '</div>' +
        '<div class="conflict-card-actions">' + buildConflictActionButtons(item, row, index) + '</div>' +
        '<div class="compare-table-wrap"><table class="compare-table"><thead><tr>' +
        '<th class="compare-col-source">来源</th>' +
        '<th class="compare-col-decision">结论</th>' +
        '<th class="compare-col-value">数值 / 置信度</th>' +
        '<th class="compare-col-reason">定位 / 理由</th>' +
        '<th class="compare-col-action">操作</th>' +
        '</tr></thead><tbody id="' + esc(extraId) + '">' + opinions.map(function (opinion, opinionIndex) {
          return buildOpinionCompareRow(item, opinion, row, index, opinionIndex);
        }).join("") + '</tbody></table></div>' +
        (extraCount > 0 ? '<div class="conflict-card-actions"><button class="expand-link" type="button" data-action="toggle-opinions" data-target="' + esc(extraId) + '">展开全部意见（+' + esc(extraCount) + '）</button></div>' : '') +
        '</div>' +
      '</section>';
    }

    async function openRowConflictModalLegacy(row) {
      modalContent("冲突详情", [row.title || "", "正在读取只读冲突聚合…"].filter(Boolean).join(" | "), '<div class="detail-value muted">加载中...</div>');
      try {
        const params = new URLSearchParams({
          paper_id: row.paper_id,
          include_non_conflicts: "false",
          limit: "200"
        });
        const payload = await fetchJSON("/api/workbench/review-conflicts?" + params.toString());
        const conflictRows = Array.isArray(payload.rows) ? payload.rows : [];
        const html = conflictRows.length
          ? '<div class="conflict-stack">' + conflictRows.map(function (item) {
              const opinions = Array.isArray(item.opinions) ? item.opinions : [];
              return '<div class="conflict-card">' +
                '<div class="conflict-head">' +
                  '<div class="conflict-title">' + esc((item.target_type || "target") + " / " + (item.field_name || "field")) + '</div>' +
                  '<span class="chip bad">' + esc((item.conflict_types || []).join(", ") || "conflict") + '</span>' +
                  '<span class="chip">reviewers ' + esc(toCount(item.reviewer_count)) + '</span>' +
                '</div>' +
                '<div class="conflict-meta">target_id: ' + esc(item.target_id || "-") + '</div>' +
                '<div class="opinion-list">' + opinions.map(function (opinion) {
                  const evidence = opinion.evidence && typeof opinion.evidence === "object" ? opinion.evidence : {};
                  const locator = evidence.locator && typeof evidence.locator === "object" ? evidence.locator : evidence;
                  const pageText = hasOwnValue(locator.page) ? ("page " + locator.page) : "";
                  return '<div class="opinion-item">' +
                    '<div class="opinion-topline">' +
                      '<div class="opinion-source">' + esc(opinion.source_label || opinion.source || opinion.agent_role || opinion.model_name || opinion.source_type || "review") + '</div>' +
                      '<span class="chip">' + esc(opinion.decision || opinion.status || "review") + '</span>' +
                      (hasOwnValue(opinion.confidence) ? '<span class="chip">confidence ' + esc(opinion.confidence) + '</span>' : '') +
                    '</div>' +
                    '<div class="detail-value muted">' + esc([opinion.value, opinion.unit].filter(Boolean).join(" ")) + '</div>' +
                    '<div class="detail-value muted">' + esc([pageText, locator.locator_status || "", opinion.reason || ""].filter(Boolean).join(" | ")) + '</div>' +
                  '</div>';
                }).join("") + '</div>' +
              '</div>';
            }).join("") + '</div>'
          : '<div class="detail-value muted">当前没有可展示的字段冲突明细。</div>';
        modalContent("冲突详情", [row.title || "", "只读聚合，不自动合并"].filter(Boolean).join(" | "), html);
      } catch (error) {
        modalContent("冲突详情", row.title || "", '<div class="error" style="margin:0;">加载冲突失败：' + esc(error.message) + '</div>');
      }
    }

    async function openRowConflictModalV2(row) {
      modalContent("冲突详情", [row.title || "", "只读聚合，不自动合并"].filter(Boolean).join(" | "), '<div class="detail-value muted">加载中...</div>');
      try {
        const params = new URLSearchParams({
          paper_id: row.paper_id,
          include_non_conflicts: "false",
          limit: "200"
        });
        const payload = await fetchJSON("/api/workbench/review-conflicts?" + params.toString());
        const conflictRows = Array.isArray(payload.rows) ? payload.rows : [];
        const summary = buildConflictSummary(conflictRows);
        const adjudicationSummary = payload.adjudication_summary || {};
        conflictState.activeRow = row;
        conflictState.groups = conflictRows;
        conflictState.activeGroupIndex = 0;
        conflictState.activeOpinionKey = null;
        conflictState.evidenceCache = {};
        conflictState.requestToken = 0;
        const html = conflictRows.length
          ? '<div class="conflict-workbench">' +
            buildConflictStatusPanel(summary, adjudicationSummary) +
            '<div class="conflict-workbench-grid' + (conflictState.listCollapsed ? ' is-list-collapsed' : '') + '">' +
              '<aside class="conflict-list-panel">' +
                '<div class="conflict-panel-head">' +
                  '<div class="conflict-panel-copy"><div class="conflict-panel-title">冲突列表</div>' +
                  '<div class="conflict-panel-note">选择一条后，在右侧核对证据和裁定。</div></div>' +
                '</div>' +
                '<div class="conflict-list">' + conflictRows.map(function (item, index) {
                  return buildConflictListItem(item, index);
                }).join("") + '</div>' +
              '</aside>' +
              '<section class="conflict-detail-panel" id="selectedConflictPanel">' + buildConflictGroupCard(conflictRows[0], row, 0) + '</section>' +
            '</div>' +
            '<div class="conflict-footer-note">所有执行动作都会走既有 verify / reject / correction 安全链路，并保留审计记录。</div>'
            + '</div>'
          : '<div class="detail-value muted">当前没有可展示的字段冲突明细。</div>';
        modalContent("冲突详情", [row.title || "", "只读聚合，不自动合并"].filter(Boolean).join(" | "), html);
        if (conflictRows.length) previewFirstEvidenceForConflict(0);
      } catch (error) {
        modalContent("冲突详情", row.title || "", '<div class="error" style="margin:0;">加载冲突失败：' + esc(error.message) + '</div>');
      }
    }

    function handleInfoModalAction(event) {
      const actionNode = event.target.closest("[data-action]");
      if (!actionNode) return;
      const action = actionNode.getAttribute("data-action");
      const targetId = actionNode.getAttribute("data-target");
      if (!action) return;
      event.preventDefault();
      if (action === "select-conflict") {
        const groupIndex = Number(actionNode.getAttribute("data-group-index"));
        renderSelectedConflict(groupIndex);
      } else if (action === "toggle-conflict-list") {
        setConflictListCollapsed(!conflictState.listCollapsed);
      } else if (action === "toggle-opinions") {
        if (!targetId) return;
        const tbody = document.getElementById(targetId);
        if (!tbody) return;
        const extras = Array.from(tbody.querySelectorAll(".compare-extra"));
        const expanded = extras.some(function (rowNode) { return !rowNode.hidden; });
        extras.forEach(function (rowNode) {
          rowNode.hidden = expanded;
        });
        actionNode.textContent = expanded ? ("展开全部意见（+" + extras.length + "）") : "收起额外意见";
      } else if (action === "toggle-reason") {
        if (!targetId) return;
        const reasonNode = document.getElementById(targetId);
        if (!reasonNode) return;
        const expanded = !reasonNode.classList.contains("truncated");
        if (expanded) {
          reasonNode.classList.add("truncated");
          actionNode.textContent = "展开理由";
        } else {
          reasonNode.classList.remove("truncated");
          actionNode.textContent = "收起理由";
        }
      } else if (action === "view-evidence") {
        const groupIndex = Number(actionNode.getAttribute("data-group-index"));
        const opinionIndex = Number(actionNode.getAttribute("data-opinion-index"));
        if (!Number.isFinite(groupIndex) || !Number.isFinite(opinionIndex)) return;
        const item = conflictState.groups[groupIndex];
        const opinion = item && Array.isArray(item.opinions) ? item.opinions[opinionIndex] : null;
        if (!item || !opinion || !conflictState.activeRow) return;
        const opinionKey = actionNode.getAttribute("data-opinion-key") || extractOpinionEvidenceKey(item, opinion, groupIndex, opinionIndex);
        conflictState.activeOpinionKey = opinionKey;
        const requestToken = conflictState.requestToken + 1;
        conflictState.requestToken = requestToken;
        renderEvidencePreview({ loading: true });
        loadOpinionEvidence(conflictState.activeRow, item, opinion, opinionKey).then(function (payload) {
          if (conflictState.requestToken !== requestToken) return;
          conflictState.activeOpinionKey = opinionKey;
          renderEvidencePreview(payload);
        }).catch(function (error) {
          if (conflictState.requestToken !== requestToken) return;
          renderEvidencePreview({
            targetType: formatTargetType(item.target_type),
            fieldName: item.field_name || "-",
            reviewSource: reviewSourceLabel(opinion),
            locatorStatus: "missing_locator",
            sourceType: formatEvidenceSourceType(item.target_type),
            sourceLabel: deriveEvidenceSourceLabel(opinionEvidencePayload(opinion), item, opinion),
            excerpt: "",
            nearbyContext: [],
            note: "加载原文片段失败：" + error.message
          });
        });
      } else if (action === "accept-ai" || action === "draft-correction" || action === "adopt-opinion" || action === "reject-all-opinions") {
        const groupIndex = Number(actionNode.getAttribute("data-group-index"));
        if (!Number.isFinite(groupIndex)) return;
        const item = conflictState.groups[groupIndex];
        if (!item || !conflictState.activeRow) return;
        if (isDftConflictItem(item)) {
          showToast("DFT final truth 不在旧 AI 裁定入口处理；请前往 DFT 详情页 verify/reject。");
          return;
        }
        if (action === "accept-ai") {
          executeAcceptAiAdjudication(conflictState.activeRow, item);
          return;
        }
        if (action === "draft-correction") {
          executeDraftCorrection(conflictState.activeRow, item);
          return;
        }
        if (action === "reject-all-opinions") {
          executeManualConflictDecision(conflictState.activeRow, item, "reject_all", null);
          return;
        }
        executeManualConflictDecision(conflictState.activeRow, item, "adopt_opinion", actionNode.getAttribute("data-source-id"));
      }
    }

    function handleRowAction(event) {
      const actionNode = event.target.closest("[data-action]");
      if (!actionNode) return;
      const action = actionNode.getAttribute("data-action");
      const rowNode = event.target.closest("tr[data-paper-id]");
      if (!rowNode) return;
      const row = state.rows.find(function (item) {
        return String(item.paper_id) === String(rowNode.getAttribute("data-paper-id"));
      });
      if (!row) return;
      event.preventDefault();
      if (action === "open-details") {
        const payload = buildRowDetailModal(row);
        modalContent(payload.title, payload.subtitle, payload.html);
      } else if (action === "open-conflicts") {
        openRowConflictModalV2(row);
      }
    }

    function renderRows() {
      renderStats();
      const filtered = sortedRows(filteredRows());
      const rows = currentVisibleRows();
      syncSelectAllVisible(rows);
      const selectedCount = selectedPromptRows().length;
      syncWebAiReturnEntry();
      const library = getValue("libraryFilter") || "全部文献库";
      const loadedCount = Number(state.rows.length || 0);
      const knownTotal = Number(state.metadata.total || loadedCount || 0);
      const truncationText = knownTotal > loadedCount ? " | 已加载 " + loadedCount + " / 总 " + knownTotal + " 篇" : "";
      const hiddenSiCount = Number(state.metadata.hidden_supplementary_count || 0);
      const hiddenSiText = hiddenSiCount > 0 ? " | 已隐藏 SI " + hiddenSiCount + " 篇" : "";
      document.getElementById("queueMeta").textContent = library + " | 当前筛选 " + filtered.length + " 篇" + hiddenSiText + truncationText;
      if (getQueryPaperId()) {
        document.getElementById("queueMeta").textContent += " | 当前文献过滤";
      }
      if (state.metadata.focus_support_pair_label) {
        document.getElementById("queueMeta").textContent += " | " + state.metadata.focus_support_pair_label;
      }
      const tbody = document.getElementById("rows");
      if (selectedCount) {
        document.getElementById("queueMeta").textContent += " | 已选 " + selectedCount + " 篇";
      }
      renderPagination(filtered.length, rows);
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6"><div class="empty">暂无匹配文献</div></td></tr>';
        updateStickyLayout();
        return;
      }

      tbody.innerHTML = rows.map(function (row, idx) {
        const inspectTarget = getInspectTarget(row);
        const detailParams = new URLSearchParams();
        detailParams.set("paper_id", row.paper_id);
        detailParams.set("tab", inspectTarget.tab);
        if (row.library_name) detailParams.set("library_name", row.library_name);
        const detailUrl = "../literature_library/index.html?" + detailParams.toString();
        const extraction = compactExtractionMeta(row);
        const progress = compactManualReviewProgress(row);
        const suspectedMissing = extraction.suspectedMissing;
        const dftConflictCount = toCount(row.dft_review_conflict_count);
        const dftConflictTotalCount = toCount(row.dft_review_conflict_total_count);
        const dftBadgeLabel = extraction.label;
        const dftBadgeClass = extraction.className || (row.dft_completeness_status === "DB_Ready" ? "ok" : "warn");
        const dftBadgeTitle = extraction.tip || "当前 DFT 提取状态摘要。";
        const auditMetrics = ['候选 ' + esc(toCount(row.dft_candidate_count))];
        const activeDftCount = hasOwnValue(row.active_dft_candidate_count)
          ? toCount(row.active_dft_candidate_count)
          : (row.has_active_dft_candidates !== undefined ? (row.has_active_dft_candidates ? toCount(row.dft_candidate_count) : 0) : null);
        const dftObjectReviewCount = toCount(row.dft_object_review_audit_count);
        if (activeDftCount > 0) {
          auditMetrics.push('<span class="active">待处理 ' + esc(activeDftCount) + '</span>');
        }
        if (suspectedMissing > 0) {
          auditMetrics.push('<span class="active">疑似漏提 ' + esc(suspectedMissing) + '</span>');
        }
        const group = supplementaryGroup(row);
        const supportLifecycleOpen = group
          ? toCount(
              group.support_dft_lifecycle_open_count != null
                ? group.support_dft_lifecycle_open_count
                : group.support_active_dft_candidate_count
            )
          : 0;
        if (group && group.role === "main" && supportLifecycleOpen > 0) {
          auditMetrics.push('<span title="' + esc(supplementaryGroupTip(row)) + '">' + esc(group.support_dft_lifecycle_label || "SI 证据待闭环") + ' ' + esc(supportLifecycleOpen) + '</span>');
        } else if (group && group.role === "supplementary") {
          auditMetrics.push('<span title="' + esc(supplementaryGroupTip(row)) + '">归属 ' + esc(group.main_paper_code || "主文献") + '</span>');
        }
        if (toCount(row.external_audit_count) > 0) {
          auditMetrics.push('外部审核 ' + esc(toCount(row.external_audit_count)));
        }
        if (dftObjectReviewCount > 0) {
          auditMetrics.push('DFT 审核 ' + esc(dftObjectReviewCount));
        }
        if (toCount(row.paper_note_count) > 0) {
          auditMetrics.push('AI 笔记 ' + esc(toCount(row.paper_note_count)));
        }
        const doiInline = row.doi
          ? '<span class="mono paper-doi-inline" title="DOI: ' + esc(row.doi) + '">DOI: ' + esc(row.doi) + '</span>'
          : '';
        const titleText = row.title || "未命名文献";
        const journalText = row.journal || "未知期刊";
        const displayCode = row.paper_code || "";
        const groupLabel = supplementaryGroupLabel(row);
        const groupTip = supplementaryGroupTip(row);
        const groupInline = groupLabel
          ? '<span>|</span><span class="paper-si-inline" title="' + esc(groupTip || groupLabel) + '">' + esc(groupLabel) + '</span>'
          : "";
        const yearText = row.year ? esc(row.year) : '<span class="muted">-</span>';
        const checked = selectedPaperIds.has(String(row.paper_id)) ? " checked" : "";

        return '<tr data-paper-id="' + esc(row.paper_id) + '">' +
          '<td class="col-divider" style="text-align:center;"><span class="row-select-cell"><input type="checkbox" aria-label="选择文献 ' + esc(displayCode || row.paper_id) + '" onchange="togglePaperSelection(\'' + esc(row.paper_id) + '\', this.checked)"' + checked + '><span class="mono row-paper-code" title="文献短号：' + esc(displayCode || row.paper_id) + '">' + esc(displayCode || "-") + '</span></span></td>' +
          '<td class="col-divider" style="text-align:center;"><span style="font-size:13px;">' + yearText + '</span></td>' +
          '<td class="col-divider">' +
            '<div class="paper-title" title="' + esc(titleText) + '">' + esc(titleText) + '</div>' +
            '<div class="paper-meta-line"><span class="paper-doi-inline" title="' + esc(journalText) + '">' + esc(journalText) + '</span>' + groupInline + (doiInline ? '<span>|</span>' + doiInline : '') + '</div>' +
          '</td>' +
          '<td class="col-divider status-cell">' +
            '<div class="status-cluster">' +
              '<div class="status-row">' +
                compactPdfChip(row) +
              '</div>' +
              '<div class="status-row">' +
                compactModuleProgressChip("图表", progress.figures, progress.figures ? "图表部分已标记完成。" : "图表部分尚未标记完成。") +
                compactModuleProgressChip("DFT", progress.dft, progress.dft ? "DFT 部分已标记完成。" : "DFT 部分尚未标记完成。") +
                compactModuleProgressChip("内容解析", progress.content, progress.content ? "内容解析部分已标记完成。" : "内容解析部分尚未标记完成。") +
              '</div>' +
            '</div>' +
            '</td>' +
          '<td class="col-divider audit-cell">' +
            '<div class="audit-summary">' +
              '<div class="audit-topline">' +
                '<div class="audit-topline-left">' +
                  '<span class="chip compact ' + dftBadgeClass + '" title="' + esc(dftBadgeTitle) + '">' + esc(dftBadgeLabel) + '</span>' +
                  renderConflictChip(
                    dftConflictCount,
                    dftConflictTotalCount,
                    "冲突",
                    "已处理冲突",
                    "当前 DFT 审计仍有未收口冲突，点击查看只读冲突聚合详情。",
                    "这篇文献历史上出现过 DFT 冲突，但当前未收口冲突已处理完。"
                  ) +
                '</div>' +
                '<button class="btn btn-ghost btn-sm btn-chip" type="button" data-action="open-details">查看详情</button>' +
              '</div>' +
              '<div class="muted audit-metrics"><span>' + auditMetrics.join('</span><span>') + '</span></div>' +
            '</div>' +
          '</td>' +
          '<td class="action-cell">' +
            '<div class="actions">' +
              '<a class="btn btn-ghost btn-sm" href="' + esc(detailUrl) + '" title="' + esc(inspectTarget.note) + '">查看</a>' +
              '<button class="btn btn-ghost btn-sm" type="button" title="重建这篇文献的 AI 材料" onclick="preparePaper(\'' + esc(row.paper_id) + '\')">重建</button>' +
              '<button class="btn btn-tinted btn-sm" type="button" title="确认完整" onclick="humanConfirm(\'' + esc(row.paper_id) + '\')">确认</button>' +
            '</div>' +
          '</td>' +
        '</tr>';
      }).join("");
      updateStickyLayout();
    }

    async function loadReviewCenter() {
      try {
        const library = getValue("libraryFilter");
        let data = null;
        let lastError = null;
        for (const fetchLimit of REVIEW_CENTER_FETCH_LIMITS) {
          try {
            const params = new URLSearchParams({
              limit: String(fetchLimit),
              sort_by: getValue("sortFilter") || "recent",
              summary_only: "true",
            });
            if (library) params.set("library_name", library);
            const focusPaperId = getQueryPaperId();
            if (focusPaperId) params.set("paper_id", focusPaperId);
            data = await fetchJSON("/api/workbench/review-center?" + params.toString());
            break;
          } catch (error) {
            lastError = error;
            if (!(error && Number(error.status) === 422)) {
              throw error;
            }
          }
        }
        if (!data) {
          throw lastError || new Error("审核中心数据读取失败");
        }
        const focusPaperId = getQueryPaperId();
        const incomingRows = data.rows || [];
        const focusPairInfo = focusPaperId ? await loadFocusedSupportPairInfo(focusPaperId, incomingRows) : null;
        const hiddenSupplementaryCount = focusPaperId
          ? 0
          : incomingRows.filter(isSupportingInformationRow).length;
        state.rows = focusPaperId
          ? sortFocusedSupportPairRows(incomingRows.filter(function (row) {
              return focusPairInfo && focusPairInfo.ids.has(String(row.paper_id || ""));
            }), focusPairInfo)
          : incomingRows.filter(function (row) { return !isSupportingInformationRow(row); });
        const availableIds = new Set(state.rows.map(function (row) { return String(row.paper_id); }));
        Array.from(selectedPaperIds).forEach(function (paperId) {
          if (!availableIds.has(String(paperId))) selectedPaperIds.delete(paperId);
        });
        const focusMainRow = focusPaperId && state.rows.find(function (row) {
          return String(row && row.paper_id || "") === String(focusPaperId) && !isSupportingInformationRow(row);
        });
        if (focusMainRow && selectedPaperIds.size === 0) {
          selectedPaperIds.add(String(focusMainRow.paper_id));
        }
        state.metadata = Object.assign({}, data.metadata || {}, {
          hidden_supplementary_count: hiddenSupplementaryCount,
          focus_main_paper_id: focusPairInfo ? focusPairInfo.mainPaperId : null,
          focus_support_pair_label: focusPaperId ? buildFocusedSupportPairLabel(state.rows) : null
        });
        renderStatusFilterOptions();
        ensureValidPage(state.rows.length);
        renderRows();
        updateManualReviewContextFromRows();
        await refreshManualReviewScope();
        await loadReviewScopeCandidates();
        await refreshDftReviewPreview();
      } catch (error) {
        document.getElementById("queueMeta").textContent = "读取失败";
        document.getElementById("rows").innerHTML = '<tr><td colspan="6"><div class="error">加载失败：' + esc(error.message) + '</div></td></tr>';
        document.getElementById("paginationMeta").textContent = "分页信息读取失败";
        document.getElementById("paginationBar").innerHTML = "";
        showToast("审核中心加载失败：" + error.message);
      }
    }

    async function preparePaper(paperId) {
      try {
        await fetchJSON("/api/papers/" + encodeURIComponent(paperId) + "/prepare-ai-context", {
          method: "POST"
        });
        showToast("已刷新这篇文献的 AI 审核材料，后续可由 IDE-AI 继续解析。");
        await loadReviewCenter();
      } catch (error) {
        showToast("重建材料失败：" + error.message);
      }
    }

    async function prepareLibrary() {
      try {
        const data = await fetchJSON("/api/workbench/prepare-active-library?render_pages=false&limit=50", { method: "POST" });
        showToast("当前库 AI 材料重建完成：" + Number(data.prepared || 0) + " 篇。");
        await loadReviewCenter();
      } catch (error) {
        showToast("批量重建材料失败：" + error.message);
      }
    }

    async function executeAcceptAiAdjudication(row, item) {
      try {
        const result = await fetchJSON("/api/workbench/review-conflicts/accept-ai", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: row.paper_id,
            target_type: item.target_type,
            target_id: item.target_id,
            field_name: item.field_name,
            reviewer: "review_center",
          })
        });
        showToast("AI 裁定已执行：" + result.action);
        await loadReviewCenter();
        await openRowConflictModalV2(row);
      } catch (error) {
        showToast("执行 AI 裁定失败：" + error.message);
      }
    }

    async function executeDraftCorrection(row, item) {
      try {
        const adjudication = item.adjudication || {};
        const payload = adjudication.recommended_payload || {};
        const result = await fetchJSON("/api/workbench/review-conflicts/accept-ai", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: row.paper_id,
            target_type: item.target_type,
            target_id: item.target_id,
            field_name: item.field_name,
            reviewer: "review_center",
          })
        });
        showToast("修正草案已生成：" + (payload.proposed_value != null ? payload.proposed_value : result.action));
        await loadReviewCenter();
        await openRowConflictModalV2(row);
      } catch (error) {
        showToast("生成修正草案失败：" + error.message);
      }
    }

    async function executeManualConflictDecision(row, item, resolution, opinionSourceId) {
      const prompt = resolution === "reject_all"
        ? "这会将当前冲突项按“不采用”处理，并走现有 reject gate。是否继续？"
        : "这会采用所选 AI 意见，并走现有 correction / verify gate。是否继续？";
      if (!window.confirm(prompt)) return;
      try {
        const result = await fetchJSON("/api/workbench/review-conflicts/manual-decision", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: row.paper_id,
            target_type: item.target_type,
            target_id: item.target_id,
            field_name: item.field_name,
            resolution: resolution,
            reviewer: "review_center",
            opinion_source_id: opinionSourceId || null,
          })
        });
        showToast("确认裁决已执行：" + (result.action || resolution));
        await loadReviewCenter();
        await openRowConflictModalV2(row);
      } catch (error) {
        showToast("确认裁决失败：" + error.message);
      }
    }

    function batchActionLabel(mode) {
      if (mode === "prepare_suspected_missing") return "批量准备疑似漏提论文的 AI 材料";
      return "批量重建 AI 解析材料";
    }

    async function runBatchStage2(mode, ids) {
      const paperIds = Array.from(new Set(ids || []));
      if (!paperIds.length) {
        showToast("当前筛选没有可执行的论文。");
        return;
      }
      try {
        const result = await fetchJSON("/api/workbench/review-center/prepare-ai-materials", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paper_ids: paperIds, mode: mode, reviewer: "review_center" })
        });
        const failed = Number(result.failed || 0);
        showToast(batchActionLabel(result.mode || mode) + "完成：成功 " + Number(result.completed || 0) + "，失败 " + failed + (failed ? "。请查看失败摘要。" : "。"));
        await loadReviewCenter();
      } catch (error) {
        showToast(batchActionLabel(mode) + "失败：" + error.message);
      }
    }

    function batchReparseFiltered() {
      runBatchStage2("prepare_filtered", filteredPaperIds());
    }

    function batchDeepParseMissing() {
      runBatchStage2("prepare_suspected_missing", filteredPaperIds(function (row) {
        return toCount(row.suspected_missing_dft_count) > 0 || row.workflow_status === "Unparsed";
      }));
    }

    function handleMaterialActionSelect() {
      const select = document.getElementById("materialActionSelect");
      if (!select || !select.value) return;
      const action = select.value;
      select.value = "";
      if (action === "prepare_filtered") {
        batchReparseFiltered();
        return;
      }
      if (action === "prepare_suspected_missing") {
        batchDeepParseMissing();
        return;
      }
      if (action === "prepare_library") {
        prepareLibrary();
      }
    }

    async function humanConfirm(paperId) {
      if (!window.confirm("确认你已经检查过这篇文献的证据链和风险状态？")) return;
      try {
        await fetchJSON("/api/workbench/papers/" + encodeURIComponent(paperId) + "/human-confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_human_review: true, reviewer: "human", target_status: "Human_Confirmed" })
        });
        showToast("已记录确认");
        await loadReviewCenter();
      } catch (error) {
        showToast("确认失败：" + error.message);
      }
    }

    document.addEventListener("DOMContentLoaded", function () {
      restoreReviewCenterFilterState();
      restoreManualReviewContext();
      TopNav.init({ currentPage: "review-center", mountId: "topnav-mount" });
      document.getElementById("rows").addEventListener("click", handleRowAction);
      document.getElementById("tableWrap").addEventListener("scroll", syncExternalTableHead, { passive: true });
      document.getElementById("infoModalBody").addEventListener("click", handleInfoModalAction);
      document.getElementById("infoOverlay").addEventListener("click", function (event) {
        if (event.target && event.target.id === "infoOverlay") closeInfoOverlay();
      });
      document.getElementById("webAiReturnOverlay").addEventListener("click", function (event) {
        if (event.target && event.target.id === "webAiReturnOverlay") closeWebAiReturnDialog();
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          closeInfoOverlay();
          closeWebAiReturnDialog();
        }
      });
      window.addEventListener("resize", updateStickyLayout);
      loadLibraries().finally(loadReviewCenter);
    });
