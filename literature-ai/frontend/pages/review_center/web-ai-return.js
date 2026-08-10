    // WEB_AI_RETURN_FEATURE_START
    function focusedSingleMainPaperRow() {
      const focusPaperId = getQueryPaperId();
      if (!focusPaperId) return null;
      const row = (state.rows || []).find(function (item) {
        return String(item && item.paper_id || "") === String(focusPaperId);
      });
      if (!row || isSupportingInformationRow(row)) return null;
      return row;
    }

    function selectedWebAiReturnTarget() {
      const rows = selectedPromptRows();
      const target = rows.length === 1 && !isSupportingInformationRow(rows[0])
        ? rows[0]
        : (rows.length !== 1 ? focusedSingleMainPaperRow() : null);
      if (target) clearMismatchedManualReviewContext(target);
      return target;
    }

    function setWebAiValidationBox(html, stateClass) {
      const box = document.getElementById("webAiValidationResult");
      if (!box) return;
      box.className = "web-ai-validation-box" + (stateClass ? " " + stateClass : "");
      box.innerHTML = html;
    }

    function syncWebAiReturnModeButtons() {
      const isEvidenceMode = webAiReturnState.mode === "evidence";
      const applyButton = document.getElementById("webAiApplyEvidenceBtn");
      const finalizeButton = document.getElementById("webAiFinalizeEvidenceBtn");
      const copyButton = document.getElementById("webAiCopyInstructionBtn");
      if (applyButton) {
        applyButton.style.display = isEvidenceMode ? "" : "none";
        applyButton.disabled = true;
      }
      if (copyButton) {
        copyButton.style.display = "";
        copyButton.textContent = isEvidenceMode ? "复制本地 AI 全量图片复核指令" : "复制本地 AI 处理指令";
        copyButton.disabled = true;
      }
      if (finalizeButton) {
        finalizeButton.style.display = isEvidenceMode ? "" : "none";
        finalizeButton.disabled = true;
      }
    }

    function clearWebAiReturnTransientState(message) {
      const textarea = document.getElementById("webAiReturnJson");
      if (textarea) textarea.value = "";
      webAiReturnState.validatedRawText = "";
      webAiReturnState.validationResponse = null;
      syncWebAiReturnModeButtons();
      setWebAiValidationBox(esc(message || "尚未校验。校验成功也不会自动入库。"), "");
    }

    function closeWebAiReturnDialog() {
      const overlay = document.getElementById("webAiReturnOverlay");
      if (!overlay) return;
      overlay.classList.remove("open");
      overlay.setAttribute("aria-hidden", "true");
    }

    function syncWebAiReturnEntry() {
      const target = selectedWebAiReturnTarget();
      const nextPaperId = target ? String(target.paper_id || "") : "";
      const nextPaperCode = target ? String(target.paper_code || "") : "";
      const button = document.getElementById("webAiReturnEntryBtn");
      if (button) {
        button.disabled = !target;
        button.textContent = target
          ? "回传 DFT JSON（" + (nextPaperCode || nextPaperId.slice(0, 8)) + "）"
          : "回传 DFT JSON";
        button.title = target
          ? "当前论文号：" + (nextPaperCode || "-")
          : "请先只选择一篇主文献";
      }
      if (webAiReturnState.paperId && webAiReturnState.paperId !== nextPaperId) {
        clearWebAiReturnTransientState("已切换文献，先前粘贴内容和校验结果已清除。");
        closeWebAiReturnDialog();
      }
      webAiReturnState.paperId = nextPaperId;
      webAiReturnState.paperCode = nextPaperCode;
      if (!nextPaperId && webAiReturnState.validationResponse) {
        clearWebAiReturnTransientState("当前没有唯一主文献，临时结果已清除。");
      }
    }

    function openWebAiReturnDialog(mode) {
      const normalizedMode = mode === "evidence" ? "evidence" : "dft";
      const target = selectedWebAiReturnTarget();
      if (!target) return;
      if (normalizedMode === "evidence" && !requireSelectedMainEvidenceScope(target)) return;
      const targetId = String(target.paper_id || "");
      if (
        (webAiReturnState.paperId && webAiReturnState.paperId !== targetId) ||
        webAiReturnState.mode !== normalizedMode
      ) {
        clearWebAiReturnTransientState("已切换文献，先前临时内容已清除。");
      }
      webAiReturnState.mode = normalizedMode;
      webAiReturnState.paperId = targetId;
      webAiReturnState.paperCode = String(target.paper_code || "");
      if (normalizedMode === "evidence" && (!manualReviewContext.bundleId || !manualReviewContext.bundleFingerprint)) {
        showToast("请先导出当前固定范围的图表证据包，再回传 JSON。");
        return;
      }
      document.getElementById("webAiReturnTitle").textContent =
        "回传网页 AI " + webAiModeLabel(normalizedMode) + " JSON";
      document.getElementById("webAiReturnSubtitle").textContent =
        normalizedMode === "evidence"
          ? evidenceScopeLabel()
          : "当前论文号：" + (webAiReturnState.paperCode || "-") +
            " | 模式：" + webAiModeLabel(normalizedMode);
      syncWebAiReturnModeButtons();
      setWebAiValidationBox(
        normalizedMode === "evidence"
          ? "尚未校验。网页 AI 图表 JSON 校验并应用后，必须复制本地 AI 全量图片复核指令；全部图片复核完成后才能导出 DFT 包。"
          : "尚未校验。DFT JSON 校验成功也不会自动入库。",
        ""
      );
      const overlay = document.getElementById("webAiReturnOverlay");
      overlay.classList.add("open");
      overlay.setAttribute("aria-hidden", "false");
      document.getElementById("webAiReturnJson").focus();
    }

    function handleWebAiReturnInput() {
      webAiReturnState.validatedRawText = "";
      webAiReturnState.validationResponse = null;
      const copyButton = document.getElementById("webAiCopyInstructionBtn");
      if (copyButton) copyButton.disabled = true;
      const applyButton = document.getElementById("webAiApplyEvidenceBtn");
      if (applyButton) applyButton.disabled = true;
      setWebAiValidationBox(
        webAiReturnState.mode === "evidence"
          ? "内容已修改，请重新校验。校验成功前不能应用图表结果。"
          : "内容已修改，请重新校验。校验成功前不能复制导入指令。",
        ""
      );
    }

    const WEB_AI_RETURN_MAX_FILE_BYTES = 5 * 1024 * 1024;

    function setWebAiFileDropActive(active) {
      const textarea = document.getElementById("webAiReturnJson");
      if (textarea) textarea.classList.toggle("is-dragging", Boolean(active));
    }

    function handleWebAiFileDrag(event) {
      if (event) {
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      }
      setWebAiFileDropActive(true);
    }

    function handleWebAiFileDragLeave(event) {
      if (event) event.preventDefault();
      setWebAiFileDropActive(false);
    }

    async function handleWebAiFileDrop(event) {
      if (event) event.preventDefault();
      setWebAiFileDropActive(false);
      const files = Array.from(event && event.dataTransfer && event.dataTransfer.files || []);
      if (files.length !== 1) {
        handleWebAiReturnInput();
        renderWebAiValidationFailure([
          { code: "file_read_error", message: "请一次只拖入一个 .json 文件" }
        ], []);
        return;
      }
      const file = files[0];
      const fileName = String(file.name || "");
      if (!fileName.toLowerCase().endsWith(".json")) {
        handleWebAiReturnInput();
        renderWebAiValidationFailure([
          { code: "file_read_error", message: "仅支持 .json 文件" }
        ], []);
        return;
      }
      if (Number(file.size || 0) > WEB_AI_RETURN_MAX_FILE_BYTES) {
        handleWebAiReturnInput();
        renderWebAiValidationFailure([
          { code: "file_read_error", message: "JSON 文件不能超过 5 MB" }
        ], []);
        return;
      }
      try {
        const rawText = await file.text();
        if (!String(rawText || "").trim()) throw new Error("文件内容为空");
        const textarea = document.getElementById("webAiReturnJson");
        textarea.value = rawText;
        handleWebAiReturnInput();
        setWebAiValidationBox(
          "已在当前页面内存中读取文件：" + esc(fileName) + "。请点击“校验结果”。",
          ""
        );
        textarea.focus();
      } catch (error) {
        handleWebAiReturnInput();
        renderWebAiValidationFailure([
          { code: "file_read_error", message: error.message || "无法读取文件" }
        ], []);
      }
    }

    function clearWebAiReturnInput() {
      clearWebAiReturnTransientState();
      const textarea = document.getElementById("webAiReturnJson");
      if (textarea) textarea.focus();
    }

    const WEB_AI_VALIDATION_ERROR_LABELS = {
      schema_validation_error: "JSON 结构不符合 return_schema.json",
      paper_id_mismatch: "paper_id 与当前选中文献不一致",
      paper_code_mismatch: "论文号与当前选中文献不一致",
      stale_or_mismatched_bundle: "核验包已过期或不属于当前文献，请重新导出核验包",
      run_scope_mismatch: "审核范围与当前固定 AI 批次不一致",
      field_out_of_scope: "包含超出 DFT 核验范围的字段",
      unknown_target_id: "目标 DFT 记录不存在或不属于当前主文献",
      unknown_evidence_id: "引用的 evidence_id 不存在于当前核验包",
      new_candidate_requires_reviewed_evidence: "新增 DFT 候选只能引用已完成且未过期的图表证据",
      unreviewed_supporting_evidence_requires_human: "未审核 SI 线索只能返回 NEEDS_HUMAN",
      duplicate_existing_terminal_candidate: "新增候选与现有终态 DFT 数据重复",
      terminal_context_dedupe_analysis_required: "新增候选必须提供终态上下文去重分析",
      review_mode_mismatch: "DFT 审核模式与当前目标集合不一致",
      unknown_figure_id: "目标图片不存在或不属于当前核验包",
      unknown_table_id: "目标表格不存在或不属于当前核验包",
      unknown_source_paper_id: "source_paper_id 不属于当前主文/SI 材料包",
      unknown_dft_candidate_evidence_id: "图表 DFT 候选引用的 evidence_id 不存在",
      evidence_not_checked: "该结论要求 evidence_checked=true",
      incomplete_candidate_coverage: "标记完成时仍有 DFT 候选未逐条审核",
      incomplete_missing_data_search: "标记完成前必须扫描包内全部合格证据并确认 DFT 查漏已完成",
      coverage_acknowledgement_mismatch: "覆盖清单与当前 DFT 候选集合不一致",
      incomplete_figure_coverage: "标记完成时仍有图片未逐项审核",
      incomplete_table_coverage: "标记完成时仍有表格未逐项审核",
      invalid_markdown_table: "表格 UPDATE/CREATE 必须返回完整 markdown 表格",
      merge_requires_source_table_id_and_target_table_id: "MERGE 必须同时给 source_table_id 和 target_table_id",
      merge_source_and_target_table_ids_must_differ: "MERGE 的 source_table_id 和 target_table_id 不能相同",
      apply_requires_completed_review: "只有 overall_status=completed 的图表审核结果才允许应用",
      duplicate_or_conflicting_figure_action: "同一图片出现重复或冲突操作",
      duplicate_or_conflicting_table_action: "同一表格出现重复或冲突操作",
      missing_evidence_ids_for_modification: "修改类操作必须带有效 evidence_ids",
      unresolved_actions_present: "仍有未解决图表操作",
      invalid_json: "无法解析为 JSON",
      file_read_error: "无法读取拖入的 JSON 文件",
      validation_request_failed: "校验请求失败",
      apply_request_failed: "应用请求失败"
    };

    function evidenceScopeMismatchIssues(payloadOrResponse) {
      const value = payloadOrResponse || {};
      if (!activeEvidenceScope()) {
        return [{ code: "run_scope_mismatch", message: "当前没有已确认的图表审核范围" }];
      }
      const expectedScope = manualReviewContext.runId ? "external_analysis_run" : "paper";
      const issues = [];
      if (String(value.scope_type || "") !== expectedScope) {
        issues.push({ code: "run_scope_mismatch", message: "scope_type 与当前固定审核范围不一致" });
      }
      if (String(value.run_id || "") !== manualReviewContext.runId) {
        issues.push({ code: "run_scope_mismatch", message: "run_id 与当前固定审核范围不一致" });
      }
      if (manualReviewContext.bundleFingerprint && String(value.bundle_fingerprint || "") !== manualReviewContext.bundleFingerprint) {
        issues.push({ code: "stale_or_mismatched_bundle", message: "bundle_fingerprint 与已导出的当前审核包不一致" });
      }
      return issues;
    }

    function dedupeValidationIssues(issues) {
      const grouped = new Map();
      (Array.isArray(issues) ? issues : []).forEach(function(issue) {
        const code = String(issue && issue.code || "validation_error");
        const message = String(issue && issue.message || "");
        const key = code + "\u0000" + message;
        const refs = ["action_ref", "target_id", "figure_id", "table_id"].map(function(field) {
          return issue && issue[field] ? field + "=" + issue[field] : "";
        }).filter(Boolean);
        if (!grouped.has(key)) grouped.set(key, { code: code, message: message, refs: [] });
        const entry = grouped.get(key);
        refs.forEach(function(ref) { if (!entry.refs.includes(ref)) entry.refs.push(ref); });
      });
      return Array.from(grouped.values());
    }

    function webAiValidationIssueText(issue) {
      const code = String(issue && issue.code || "validation_error");
      const label = WEB_AI_VALIDATION_ERROR_LABELS[code] || "校验未通过";
      const detail = String(issue && issue.message || "").trim();
      return "[" + code + "] " + label + (detail ? "：" + detail : "");
    }

    function renderWebAiValidationFailure(errors, warnings) {
      const normalizedErrors = dedupeValidationIssues(errors);
      webAiReturnState.lastValidationIssues = normalizedErrors;
      webAiReturnState.lastValidationWarnings = Array.isArray(warnings) ? warnings : [];
      const dftEvidenceMismatch = webAiReturnState.mode === "dft" && (
        normalizedErrors.some(function(item) { return item.code === "unrelated_evidence_id"; }) ||
        webAiReturnState.lastValidationWarnings.some(function(item) { return item && item.code === "completed_with_uncertainties"; })
      );
      const errorItems = normalizedErrors.map(function (item) {
        return "<li>" + esc(webAiValidationIssueText(item)) +
          (item.refs.length ? " <span class=\"muted\">（相关 " + esc(item.refs.join("，")) + "）</span>" : "") + "</li>";
      }).join("");
      const warningItems = (warnings || []).map(function (item) {
        return "<li>[" + esc(item && item.code || "warning") + "] " + esc(item && item.message || "需要本地 AI 复核") + "</li>";
      }).join("");
      const copyButton = document.getElementById("webAiCopyInstructionBtn");
      if (copyButton) copyButton.disabled = true;
      setWebAiValidationBox(
        "<strong>校验失败，已停止。</strong>" +
        (errorItems ? "<ul>" + errorItems + "</ul>" : "") +
        (warningItems ? "<div><strong>提示</strong><ul>" + warningItems + "</ul></div>" : "") +
        (dftEvidenceMismatch
          ? '<div style="margin-top:10px;"><strong>这是证据语义不匹配，不是 JSON 格式错误。</strong><button class="btn btn-tinted btn-sm" type="button" style="margin-left:8px;" onclick="copyLocalAiDftEvidenceVerificationInstruction()">复制本地 AI 精确核验指令</button><span class="muted" style="margin-left:8px;">不要再把同一份 JSON 反复交回网页 AI；本地 AI 需逐条核对目标和 PDF 证据，不能通过时只报告阻塞项。</span></div>'
          : webAiReturnState.mode === "dft" && normalizedErrors.length
          ? '<div style="margin-top:10px;"><button class="btn btn-tinted btn-sm" type="button" onclick="copyWebAiJsonRepairPrompt()">复制 JSON 修复提示</button><span class="muted" style="margin-left:8px;">把原 JSON 和本次错误一起交回网页 AI，只修格式后以 JSON 文件附件回复。</span></div>'
          : "") +
        (webAiReturnState.mode === "evidence" && normalizedErrors.length
          ? '<div style="margin-top:10px;"><button class="btn btn-tinted btn-sm" type="button" onclick="copyWebAiEvidenceJsonRepairPrompt()">复制图表 JSON 修复提示</button><span class="muted" style="margin-left:8px;">这是网页 AI JSON 的格式/证据契约问题；先交回网页 AI修复，不需要发送给本地 AI。</span></div>'
          : ""),
        "is-error"
      );
    }

    async function copyWebAiEvidenceJsonRepairPrompt() {
      const textarea = document.getElementById("webAiReturnJson");
      const rawText = String(textarea && textarea.value || "").trim();
      const issues = Array.isArray(webAiReturnState.lastValidationIssues)
        ? webAiReturnState.lastValidationIssues.map(webAiValidationIssueText)
        : [];
      if (!rawText || !issues.length) {
        showToast("当前没有可复制的 JSON 和校验错误。");
        return;
      }
      const outputName = (webAiReturnState.paperCode || "paper") + "_chart_review_result.json";
      const prompt = [
        "只修复下面图表审核 JSON 的格式和证据契约错误，不要重新分析论文，不要改变没有被错误点名的科学结论。",
        "",
        "本次系统校验错误：",
        issues.map(function(item, index) { return String(index + 1) + ". " + item; }).join("\n"),
        "",
        "强制规则：",
        "- 每一条 figure_actions/table_actions 都必须有一个或多个真实 evidence_ids；只能从原审核包的 manifest.json、parsed/extracted_figures.json 或 parsed/extracted_tables.json 复制，禁止编造。",
        "- 每一条 CREATE figure 必须有 source_paper_id、page、bbox_norm、evidence_checked=true 和真实 evidence_ids。",
        "- 若某个新增图/表无法对应包内 evidence_id，删除该不受支持的 CREATE 动作；不要留空 evidence_ids，也不要把它改成没有 figure_id/table_id 的 NEEDS_HUMAN。",
        "- 不得修改 schema_version、bundle_fingerprint、paper_id、paper_code、scope_type、run_id、chart_run_id。",
        "- 修复后重新解析 JSON，并按 return_schema.json 自检。",
        "- 最终保存为 " + outputName + "，并以 JSON 文件附件回复；不要把 JSON 粘贴到聊天正文，不要 Markdown、解释或代码块。",
        "",
        "待修复的原始 JSON：",
        rawText
      ].join("\n");
      try {
        await copyTextToClipboard(prompt);
        showToast("图表 JSON 修复提示已复制，请交回网页 AI。", "success");
      } catch (error) {
        showToast("复制失败，请检查浏览器剪贴板权限。", "error");
      }
    }

    async function copyWebAiJsonRepairPrompt() {
      const textarea = document.getElementById("webAiReturnJson");
      const rawText = String(textarea && textarea.value || "").trim();
      const issues = Array.isArray(webAiReturnState.lastValidationIssues)
        ? webAiReturnState.lastValidationIssues.map(webAiValidationIssueText)
        : [];
      if (!rawText || !issues.length) {
        showToast("当前没有可复制的 JSON 和校验错误。");
        return;
      }
      const outputName = (webAiReturnState.paperCode || "paper") + "_web_ai_result.json";
      const prompt = [
        "只修复下面 JSON 的格式和契约错误，不要重新分析论文，不要改变没有被错误点名的科学结论。",
        "",
        "本次系统校验错误：",
        issues.map(function(item, index) { return String(index + 1) + ". " + item; }).join("\n"),
        "",
        "强制规则：",
        "- target_id=\"new\" 当且仅当 decision=\"new_candidate\"。",
        "- PASS、REVISE、REJECT、NEEDS_HUMAN 必须使用原模板/checklist 中真实已有的 target_id，不能使用 new。",
        "- 不得修改 schema_version、bundle_fingerprint、chart_scope_type、chart_run_id、review_mode、figure_table_completed_snapshot_fingerprint、paper_id、paper_code。",
        "- 修复后重新解析 JSON，并按 return_schema.json 自检。",
        "- 最终保存为 " + outputName + "，并以 JSON 文件附件回复；不要把 JSON 粘贴到聊天正文，不要 Markdown、解释或代码块。",
        "",
        "待修复的原始 JSON：",
        rawText
      ].join("\n");
      try {
        await copyTextToClipboard(prompt);
        showToast("JSON 修复提示已复制，请交回网页 AI。", "success");
      } catch (error) {
        showToast("复制失败，请检查浏览器剪贴板权限。", "error");
      }
    }

    async function copyLocalAiDftEvidenceVerificationInstruction() {
      const textarea = document.getElementById("webAiReturnJson");
      const rawText = String(textarea && textarea.value || "").trim();
      const issues = Array.isArray(webAiReturnState.lastValidationIssues)
        ? webAiReturnState.lastValidationIssues.map(webAiValidationIssueText)
        : [];
      const warnings = Array.isArray(webAiReturnState.lastValidationWarnings)
        ? webAiReturnState.lastValidationWarnings.map(webAiValidationIssueText)
        : [];
      if (!rawText || !issues.length) {
        showToast("当前没有可交给本地 AI 的 DFT JSON 和校验错误。");
        return;
      }
      const prompt = [
        "这是一次 DFT 证据语义核验，不是格式修复，也不是导入任务。不要修改项目代码。",
        "当前网页 AI JSON 未通过校验；禁止 apply、import_analysis 或直接写数据库。",
        "",
        "当前论文号：" + (webAiReturnState.paperCode || "-"),
        "paper_id：" + (webAiReturnState.paperId || "-"),
        "",
        "校验错误：",
        issues.concat(warnings).map(function(item, index) { return String(index + 1) + ". " + item; }).join("\n"),
        "",
        "执行要求：",
        "1. 打开原 DFT 审核包，读取 parsed/dft_review_checklist.json、manifest.json 和 evidence/；按唯一 evidence_id 调用 get_codex_item，按唯一 (paper_id,page) 调用 read_paper_page，并把成功结果映射回所有相关候选。read_paper_page 返回数据库已存页面布局，不等于重新打开原 PDF。",
        "2. 对每条 unrelated_evidence_id：只能保留真正包含该目标数值或明确对应来源图/表/段落的 reviewed evidence_id。不要用同一篇论文里的任意图表凑数，不要编造 evidence_id。",
        "3. 若审核包中没有任何已审核证据直接支持某个目标：不要伪造 PASS、REVISE、REJECT、NEEDS_HUMAN 或 new_candidate；列出该 target_id 和原因并停止，等待补充合格证据。",
        "4. overall_status=completed 但 uncertainties 非空时，不得宣称完成；先逐项消除不确定性，或明确报告阻塞项。",
        "5. 只有所有保留审核意见都与真实证据逐条匹配后，才生成新的 JSON 文件并重新 validate；validate 通过前绝不 apply 或 import。",
        "6. 最后只报告：可安全重提校验的条目、缺证据的 target_id、是否仍有阻塞项。",
        "",
        "网页 AI 的原始 JSON（仅作待核对草稿）：",
        "-----BEGIN WEB AI REVIEW JSON-----",
        rawText,
        "-----END WEB AI REVIEW JSON-----"
      ].join("\n");
      try {
        await copyTextToClipboard(prompt);
        showToast("已复制本地 AI DFT 精确核验指令；不要再反复提交同一份网页 AI JSON。", "success");
      } catch (error) {
        showToast("复制失败，请检查浏览器剪贴板权限。", "error");
      }
    }

    function extractFirstJsonPayload(text) {
      const source = String(text || "");
      const starts = [];
      for (let index = 0; index < source.length; index += 1) {
        const char = source[index];
        if (char === "{" || char === "[") starts.push(index);
      }
      for (let s = 0; s < starts.length; s += 1) {
        const start = starts[s];
        const opener = source[start];
        const stack = [opener === "{" ? "}" : "]"];
        let inString = false;
        let escaping = false;
        for (let index = start + 1; index < source.length; index += 1) {
          const char = source[index];
          if (inString) {
            if (escaping) {
              escaping = false;
            } else if (char === "\\") {
              escaping = true;
            } else if (char === "\"") {
              inString = false;
            }
            continue;
          }
          if (char === "\"") {
            inString = true;
            continue;
          }
          if (char === "{" || char === "[") {
            stack.push(char === "{" ? "}" : "]");
          } else if (char === "}" || char === "]") {
            if (!stack.length || char !== stack[stack.length - 1]) break;
            stack.pop();
            if (!stack.length) return source.slice(start, index + 1).trim();
          }
        }
      }
      return "";
    }

    function parseWebAiJsonText(rawText) {
      const text = String(rawText || "").replace(/^\uFEFF/, "").trim();
      const candidates = [text];
      const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
      if (fenced && fenced[1]) candidates.push(fenced[1].trim());
      const extracted = extractFirstJsonPayload(text);
      if (extracted) candidates.push(extracted);
      let lastError = null;
      for (let index = 0; index < candidates.length; index += 1) {
        const candidate = candidates[index];
        if (!candidate) continue;
        try {
          const parsed = JSON.parse(candidate);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("返回内容必须是 JSON 对象，不能是数组或空值。");
          }
          return parsed;
        } catch (error) {
          lastError = error;
        }
      }
      throw lastError || new Error("无法解析为 JSON。");
    }

    function renderWebAiExecutionPlan(plan) {
      const items = Array.isArray(plan) ? plan.slice(0, 24) : [];
      if (!items.length) {
        return '<div class="muted" style="margin-top:8px;">没有生成自动执行计划。</div>';
      }
      const rows = items.map(function (item) {
        const blocked = Array.isArray(item.blocked_reasons) && item.blocked_reasons.length
          ? item.blocked_reasons.join(", ")
          : "可自动处理";
        return "<li>" +
          "<strong>" + esc(String(item.category || "-") + " / " + String(item.action || "-")) + "</strong>" +
          " → " + esc(String(item.target_id || "-")) +
          "；auto_apply=" + esc(String(Boolean(item.auto_apply))) +
          "；" + esc(blocked) +
          "</li>";
      }).join("");
      const extra = plan.length > items.length
        ? '<div class="muted">仅显示前 ' + esc(String(items.length)) + ' 条，共 ' + esc(String(plan.length)) + ' 条。</div>'
        : "";
      return '<div style="margin-top:10px;"><strong>执行计划：</strong><ul>' + rows + "</ul>" + extra + "</div>";
    }


    function renderWebAiUnresolvedActions(actions) {
      const items = Array.isArray(actions) ? actions.slice(0, 16) : [];
      if (!items.length) {
        return '<div style="margin-top:10px;"><strong>unresolved_actions：</strong>无</div>';
      }
      const rows = items.map(function (item) {
        const blocked = Array.isArray(item.blocked_reasons) && item.blocked_reasons.length
          ? item.blocked_reasons.join(", ")
          : "requires_local_ai";
        return "<li>" +
          "<strong>" + esc(String(item.category || "-") + " / " + String(item.action || "-")) + "</strong>" +
          " → " + esc(String(item.target_id || "-")) +
          "；" + esc(blocked) +
          "</li>";
      }).join("");
      const extra = actions.length > items.length
        ? '<div class="muted">仅显示前 ' + esc(String(items.length)) + ' 条，共 ' + esc(String(actions.length)) + ' 条。</div>'
        : "";
      return '<div style="margin-top:10px;"><strong>unresolved_actions：</strong><ul>' + rows + "</ul>" + extra + "</div>";
    }

    function renderWebAiValidationSuccess(data) {
      const warnings = Array.isArray(data.warnings) ? data.warnings : [];
      const warningText = warnings.length
        ? warnings.map(function (item) {
            return "[" + String(item && item.code || "warning") + "] " + String(item && item.message || "需要本地 AI 复核");
          }).join("；")
        : "无";
      if (webAiReturnState.mode === "evidence") {
        const applyButton = document.getElementById("webAiApplyEvidenceBtn");
        const copyButton = document.getElementById("webAiCopyInstructionBtn");
        if (applyButton) applyButton.disabled = false;
        if (copyButton) copyButton.disabled = true;
        setWebAiValidationBox(
          "<strong>图表 JSON 校验通过，尚未应用。</strong>" +
          '<div class="web-ai-validation-summary">' +
            '<div class="web-ai-validation-item"><strong>paper_code</strong>' + esc(data.paper_code || "-") + "</div>" +
            '<div class="web-ai-validation-item"><strong>stage_status</strong>' + esc(String(data.stage_status || "-")) + "</div>" +
            '<div class="web-ai-validation-item"><strong>apply_ready</strong>' + esc(String(Boolean(data.apply_ready))) + "</div>" +
            '<div class="web-ai-validation-item"><strong>auto_apply_count</strong>' + esc(String(data.auto_apply_count || 0)) + "</div>" +
            '<div class="web-ai-validation-item"><strong>unresolved_count</strong>' + esc(String(data.unresolved_count || 0)) + "</div>" +
            '<div class="web-ai-validation-item"><strong>validate_writes_database</strong>' + esc(String(Boolean(data && data.safety && data.safety.validate_writes_database))) + "</div>" +
          "</div>" +
          "<div style=\"margin-top:10px;\"><strong>warnings：</strong>" + esc(warningText) + "</div>" +
          renderWebAiUnresolvedActions(data.unresolved_actions || []) +
          renderWebAiExecutionPlan(data.execution_plan || []) +
          '<div style="margin-top:10px;">下一步：先点击“应用图表结果”，然后无论网页 AI 是否报告问题，都必须执行本地 AI 全量图片复核。</div>',
          "is-success"
        );
        return;
      }
      const copyButton = document.getElementById("webAiCopyInstructionBtn");
      if (copyButton) copyButton.disabled = false;
      const writesDatabase = Boolean(data && data.safety && data.safety.writes_database);
      const hasImportRequest = Boolean(data && data.import_analysis_request);
      const coverage = data && data.coverage ? data.coverage : {};
      const coverageText = String(coverage.reviewed_existing_count || 0) + "/" + String(coverage.expected_count || 0);
      const missingTargets = Array.isArray(coverage.missing_target_ids) ? coverage.missing_target_ids : [];
      setWebAiValidationBox(
        "<strong>DFT 全量核验结果校验通过（已有数据+查漏），但尚未入库。</strong>" +
        '<div class="web-ai-validation-summary">' +
          '<div class="web-ai-validation-item"><strong>paper_code</strong>' + esc(data.paper_code || "-") + "</div>" +
          '<div class="web-ai-validation-item"><strong>review_mode</strong>' + esc(String(data.review_mode || "-")) + "</div>" +
          '<div class="web-ai-validation-item"><strong>validated_audit_count</strong>' + esc(String(data.validated_audit_count || 0)) + "</div>" +
          '<div class="web-ai-validation-item"><strong>coverage</strong>' + esc(coverageText) + "</div>" +
          '<div class="web-ai-validation-item"><strong>coverage_complete</strong>' + esc(String(Boolean(coverage.coverage_complete))) + "</div>" +
          '<div class="web-ai-validation-item"><strong>writes_database</strong>' + esc(String(writesDatabase)) + "</div>" +
          '<div class="web-ai-validation-item"><strong>import_analysis_request</strong>' + (hasImportRequest ? "已生成" : "未生成") + "</div>" +
        "</div>" +
        (missingTargets.length ? '<div style="margin-top:10px;"><strong>missing_target_ids：</strong>' + esc(missingTargets.slice(0, 20).join(", ")) + "</div>" : "") +
        "<div style=\"margin-top:10px;\"><strong>warnings：</strong>" + esc(warningText) + "</div>",
        "is-success"
      );
    }

    async function validateWebAiReturnJson() {
      const target = selectedWebAiReturnTarget();
      if (!requireSelectedMainEvidenceScope(target)) return;
      if (!target || String(target.paper_id || "") !== webAiReturnState.paperId) {
        clearWebAiReturnTransientState("当前文献选择已变化，请重新打开回传入口。");
        renderWebAiValidationFailure([{ code: "paper_id_mismatch", message: "当前选择与弹窗目标不一致" }], []);
        return;
      }
      const textarea = document.getElementById("webAiReturnJson");
      const rawText = String(textarea && textarea.value || "").trim();
      if (!rawText) {
        renderWebAiValidationFailure([{ code: "invalid_json", message: "请先粘贴网页 AI 返回 JSON" }], []);
        return;
      }
      let parsed;
      try {
        parsed = parseWebAiJsonText(rawText);
      } catch (error) {
        webAiReturnState.validatedRawText = "";
        webAiReturnState.validationResponse = null;
        document.getElementById("webAiCopyInstructionBtn").disabled = true;
        const applyButton = document.getElementById("webAiApplyEvidenceBtn");
        if (applyButton) applyButton.disabled = true;
        renderWebAiValidationFailure([{ code: "invalid_json", message: error.message }], []);
        return;
      }
      if (webAiReturnState.mode === "evidence") {
        const scopeIssues = evidenceScopeMismatchIssues(parsed);
        if (scopeIssues.length) {
          renderWebAiValidationFailure(scopeIssues, []);
          return;
        }
      }
      const validateButton = document.getElementById("webAiValidateBtn");
      validateButton.disabled = true;
      validateButton.textContent = "校验中...";
      document.getElementById("webAiCopyInstructionBtn").disabled = true;
      const applyButton = document.getElementById("webAiApplyEvidenceBtn");
      if (applyButton) applyButton.disabled = true;
      try {
        const endpoint = webAiReturnState.mode === "evidence"
          ? "/api/papers/" + encodeURIComponent(target.paper_id) + "/evidence-review-result/validate" + (manualReviewContext.runId ? "?run_id=" + encodeURIComponent(manualReviewContext.runId) : "")
          : "/api/papers/" + encodeURIComponent(target.paper_id) + "/dft-review-result/validate";
        const data = await fetchJSON(
          endpoint,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(parsed)
          }
        );
        const hasExpectedResult = webAiReturnState.mode === "evidence"
          ? Boolean(data && data.valid === true && Array.isArray(data.execution_plan))
          : Boolean(data && data.valid === true && data.import_analysis_request);
        if (!hasExpectedResult) {
          webAiReturnState.validatedRawText = "";
          webAiReturnState.validationResponse = null;
          renderWebAiValidationFailure(data && data.errors || [], data && data.warnings || []);
          return;
        }
        if (webAiReturnState.mode === "evidence") {
          const scopeIssues = evidenceScopeMismatchIssues(data);
          if (scopeIssues.length) {
            webAiReturnState.validatedRawText = "";
            webAiReturnState.validationResponse = null;
            renderWebAiValidationFailure(scopeIssues, []);
            return;
          }
        }
        webAiReturnState.validatedRawText = rawText;
        webAiReturnState.validationResponse = data;
        renderWebAiValidationSuccess(data);
      } catch (error) {
        webAiReturnState.validatedRawText = "";
        webAiReturnState.validationResponse = null;
        renderWebAiValidationFailure([
          { code: "validation_request_failed", message: error.message }
        ], []);
      } finally {
        validateButton.disabled = false;
        validateButton.textContent = "校验结果";
      }
    }

    async function buildLocalAiImportInstruction() {
      let response = webAiReturnState.validationResponse;
      const textarea = document.getElementById("webAiReturnJson");
      const rawText = String(textarea && textarea.value || "").trim();
      if (webAiReturnState.mode === "evidence") {
        if (!webAiReturnState.paperId || !activeEvidenceScope()) {
          throw new Error("请先确认固定图表审核范围。");
        }
        if (!response || response.valid !== true) {
          response = await fetchJSON(
            "/api/papers/" + encodeURIComponent(webAiReturnState.paperId) + "/chart-review-task" + (manualReviewContext.runId ? "?run_id=" + encodeURIComponent(manualReviewContext.runId) : "")
          );
        }
        const unresolvedActions = Array.isArray(response.unresolved_actions) ? response.unresolved_actions : [];
        const unresolvedSummary = unresolvedActions.length
          ? unresolvedActions.map(function (item, index) {
              const blocked = Array.isArray(item.blocked_reasons) && item.blocked_reasons.length
                ? item.blocked_reasons.join(", ")
                : "-";
              const evidenceIds = Array.isArray(item.evidence_ids) && item.evidence_ids.length
                ? item.evidence_ids.join(", ")
                : "-";
              const reason = String(item.reason || "-");
              return [
                String(index + 1) + ". " + String(item.op_id || item.action_ref || "-"),
                "   category/action: " + String(item.category || "-") + " / " + String(item.action || "-"),
                "   target_id: " + String(item.target_id || "-"),
                "   source_paper_id: " + String(item.source_paper_id || "-"),
                "   evidence_ids: " + evidenceIds,
                "   blocked_reasons: " + blocked,
                "   reason: " + (reason.length > 700 ? reason.slice(0, 700) + "..." : reason)
              ].join("\n");
            }).join("\n\n")
          : "当前没有网页 AI 遗留问题，但本地 AI 仍须逐张核验全部范围内图片，不能直接 finalize。";
        return [
          "执行图表证据阶段的本地 AI 全量图片复核，不修改代码。",
          "",
          "当前论文号：" + (webAiReturnState.paperCode || "-"),
          "paper_id：" + webAiReturnState.paperId,
          "兼容协议名称：get_chart_review_task(paper_id)；run-scoped 调用附带 run_id。",
          evidenceScopeLabel(),
          "stage_status：" + String(response.stage_status || "-"),
          "unresolved_count：" + String(response.unresolved_count || unresolvedActions.length || 0),
          "bundle_fingerprint：" + String(response.bundle_fingerprint || "-"),
          "",
          "下面列出网页 AI 遗留问题；此外必须重新核验 get_chart_review_task 返回的每一张范围内图片。",
          "",
          unresolvedSummary,
          "",
          "执行要求：",
          "1. 只使用当前会话已认证的 Literature AI MCP；不要改 MCP 配置，不要发不带认证头的裸请求。",
          "2. 调用 get_chart_review_task(paper_id, run_id) 读取当前 run 任务和 PDF/页面信息；run_id=" + (new URLSearchParams(window.location.search).get("run_id") || "-") + "。",
          "3. 对 get_chart_review_task 返回的每一张 figure 调用 get_codex_item，并用 read_paper_page 对照其来源 PDF 页；不得只处理 unresolved_actions。",
          "4. 每张图片都要独立判断 KEEP、RECROP、CREATE、REJECT 或 NEEDS_HUMAN；表格沿用网页 AI 已应用结果，除非表格本身仍有 unresolved_actions。",
          "5. 每一个非 NEEDS_HUMAN 的 figure_action 都必须附 local_ai_verification={verified_against_pdf:true,used_tools:['get_codex_item','read_paper_page'],verification_note:'逐图核验说明'}。",
          "6. 使用当前 get_chart_review_task 返回的 bundle_fingerprint 构造完整 review_result；review_source_type 必须为 local_ai，并填写当前本地 AI 身份；不要沿用旧网页 JSON 的 bundle_fingerprint。",
          "7. 构造完整 review_result 后调用 resolve_chart_review_actions(paper_id, review_result, run_id)。",
          "8. 只有服务器返回所有范围内图片均已本地核验且 unresolved_count=0，才调用 finalize_chart_review(paper_id, review_result, run_id)。",
          "兼容协议名称：resolve_chart_review_actions(paper_id, review_result)；finalize_chart_review(paper_id, review_result)；run-scoped 时必须附带 run_id。",
          "9. 最后只报告 stage_status、completed_snapshot_fingerprint、unresolved_count。"
        ].join("\n");
      }
      if (webAiReturnState.mode !== "dft") {
        throw new Error("当前不是 DFT 终审模式。");
      }
      if (!response || response.valid !== true || !response.import_analysis_request || rawText !== webAiReturnState.validatedRawText) {
        throw new Error("当前结果尚未通过校验，或粘贴内容已发生变化。");
      }
      return [
        "你是本地校验和导入执行者。网页 AI 只提供审核建议，不能直接写数据库。",
        "",
        "当前论文号：" + (webAiReturnState.paperCode || "-"),
        "paper_id：" + webAiReturnState.paperId,
        "",
        "执行要求：",
        "1. 先把下方原始 JSON 重新 POST 到 /api/papers/" + webAiReturnState.paperId + "/dft-review-result/validate，防止核验包或数据库状态已过期。",
        "2. 如果新返回 valid=false，立即停止，不调用 import_analysis，并向用户报告 errors 和 warnings。",
        "3. 如果新返回 coverage.coverage_complete=false，立即停止；缺失 target_id 需要重新交给网页 AI 或由本地 AI 补齐 NEEDS_HUMAN/PASS/REVISE/REJECT 意见。",
        "4. 如果新返回 valid=true，只使用这次新返回的 import_analysis_request；不得使用旧校验结果。",
        "5. 按 import_analysis_request.raw_payload.local_ai_verification_plan 执行读取：每个唯一 evidence_id 调用一次 get_codex_item，每个唯一 (source_paper_id,page) 调用一次 read_paper_page；相同 evidence_id 或相同 (paper_id,page) 的成功结果可跨 audit 复用，但每条 audit 都必须逐条覆盖其全部 required_evidence_checks。不得把 target_id='new' 或 temporary_id 当作 get_codex_item 的 UUID；新候选应读取要求中给出的真实 Figure/Table/Section 证据对象。",
        "6. read_paper_page 只返回数据库已存的页面布局内容，不会重新打开原始 PDF；本地 AI 还必须结合审核包内 source/main.pdf、source/si/*.pdf 及对应页面证据作出判断。不得只重新 validate 后直接导入。",
        "7. 核对通过后，把每条 object_review_audits 的 source/source_label/agent_role 改成本地 AI 身份，并分别设置 local_ai_verification={verified_against_pdf:true,used_tools:['get_codex_item','read_paper_page'],checked_evidence_ids:[...],checked_pages:[{paper_id:'...',page:1}],verification_note:'...'}；checked_pages 不能只写页码。",
        "8. 确认 DFT bundle 中 parsed/curated_figure_table_evidence_snapshot.json 的 stage_status 为 completed/not_required、rag_quality_status 不是 blocked，且 completed_snapshot_fingerprint 与 import_analysis_request 一致；否则停止并要求先完成图表证据整理。",
        "9. 优先调用当前会话已认证 MCP 的 import_analysis；禁止直接写 PostgreSQL，禁止调用 service/session/model 绕过公开入口。写入后回读 dft_readback.object_versions、candidate_status、conflicts、export_safety 和 unfinished_items。",
        "10. 校验成功本身不等于入库，也不等于已确认、verified 或 ML_Ready。",
        "11. 导入后回读并报告 run_id、候选数量、冲突项、需要确认处理的项目以及未完成事项。",
        "",
        "网页 AI 返回的原始 JSON：",
        "-----BEGIN WEB AI REVIEW JSON-----",
        rawText,
        "-----END WEB AI REVIEW JSON-----"
      ].join("\n");
    }

    async function copyLocalAiImportInstruction() {
      try {
        const instruction = await buildLocalAiImportInstruction();
        await copyTextToClipboard(instruction);
        showToast(webAiReturnState.mode === "evidence"
          ? "已复制本地 AI 全量图片复核指令；请通过已认证 MCP 逐图核验、批量 resolve 并 finalize。"
          : "已复制本地 AI 处理指令；仍需由本地 AI 重新校验后受控导入。");
      } catch (error) {
        document.getElementById("webAiCopyInstructionBtn").disabled = true;
        showToast(error.message);
      }
    }

    async function copyLocalAiChartReviewInstructionFromMenu() {
      if (webAiReturnState.mode !== "evidence") {
        openWebAiReturnDialog("evidence");
      }
      await copyLocalAiImportInstruction();
    }

    async function applyWebAiEvidenceResult() {
      if (webAiReturnState.mode !== "evidence") {
        showToast("当前不是图表证据整理模式。");
        return;
      }
      const target = selectedWebAiReturnTarget();
      if (!requireSelectedMainEvidenceScope(target)) return;
      const textarea = document.getElementById("webAiReturnJson");
      const rawText = String(textarea && textarea.value || "").trim();
      if (!target || String(target.paper_id || "") !== webAiReturnState.paperId) {
        renderWebAiValidationFailure([{ code: "paper_id_mismatch", message: "当前选择与弹窗目标不一致" }], []);
        return;
      }
      if (!webAiReturnState.validationResponse || webAiReturnState.validationResponse.valid !== true || rawText !== webAiReturnState.validatedRawText) {
        showToast("请先校验当前图表 JSON。");
        return;
      }
      let parsed;
      try {
        parsed = parseWebAiJsonText(rawText);
      } catch (error) {
        renderWebAiValidationFailure([{ code: "invalid_json", message: error.message }], []);
        return;
      }
      const scopeIssues = evidenceScopeMismatchIssues(parsed).concat(evidenceScopeMismatchIssues(webAiReturnState.validationResponse));
      if (scopeIssues.length) {
        renderWebAiValidationFailure(scopeIssues, []);
        return;
      }
      const applyButton = document.getElementById("webAiApplyEvidenceBtn");
      if (!window.confirm("确认应用图表审核 JSON？\n" + evidenceScopeLabel() + "\n系统只会处理当前范围内的图/表；不会自动替你切换为整篇论文。")) {
        return;
      }
      applyButton.disabled = true;
      applyButton.textContent = "应用中...";
      try {
        const data = await fetchJSON(
            "/api/papers/" + encodeURIComponent(target.paper_id) + "/evidence-review-result/apply" + (manualReviewContext.runId ? "?run_id=" + encodeURIComponent(manualReviewContext.runId) : ""),
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(parsed)
          }
        );
        if (data && data.apply_ready === false && Array.isArray(data.apply_blocking_errors) && data.apply_blocking_errors.length) {
          renderWebAiValidationFailure(data.apply_blocking_errors, data.warnings || []);
          return;
        }
        if (!data || data.valid !== true) {
          renderWebAiValidationFailure(data && data.errors || [{ code: "apply_request_failed", message: "系统未返回有效应用结果" }], data && data.warnings || []);
          return;
        }
        const returnedScopeIssues = evidenceScopeMismatchIssues(data);
        if (returnedScopeIssues.length) {
          renderWebAiValidationFailure(returnedScopeIssues, []);
          return;
        }
        const skipped = Array.isArray(data.skipped) ? data.skipped.length : 0;
        const applied = Array.isArray(data.applied) ? data.applied.length : Number(data.applied_count || 0);
        const completed = Boolean(data && data.chart_review_completed);
        const copyButton = document.getElementById("webAiCopyInstructionBtn");
        const finalizeButton = document.getElementById("webAiFinalizeEvidenceBtn");
        if (copyButton) copyButton.disabled = false;
        if (finalizeButton) finalizeButton.disabled = true;
        setWebAiValidationBox(
          "<strong>" + (completed ? "图表两级审核已完成。" : "网页 AI 图表结果已应用，等待本地 AI 逐图复核。") + "</strong>" +
          '<div class="web-ai-validation-summary">' +
            '<div class="web-ai-validation-item"><strong>stage_status</strong>' + esc(String(data.stage_status || "-")) + "</div>" +
            '<div class="web-ai-validation-item"><strong>applied_count</strong>' + esc(String(applied)) + "</div>" +
            '<div class="web-ai-validation-item"><strong>skipped_count</strong>' + esc(String(skipped)) + "</div>" +
            '<div class="web-ai-validation-item"><strong>unresolved_count</strong>' + esc(String(data.unresolved_count || 0)) + "</div>" +
            '<div class="web-ai-validation-item"><strong>completed_snapshot</strong>' + esc(String(data.completed_snapshot_fingerprint || "-").slice(0, 16)) + "</div>" +
          "</div>" +
          renderWebAiUnresolvedActions(data.unresolved_actions || []) +
          '<div style="margin-top:10px;">' + (completed ? '全部图片已有本地 AI 核验记录，可以继续 DFT。' : '下一步：复制本地 AI 全量图片复核指令；每张范围内图片都必须对照 PDF，随后由本地 AI resolve 并 finalize。') + '</div>',
          completed ? "is-success" : ""
        );
        webAiReturnState.validationResponse = data;
        showToast(completed ? "图表两级审核已完成；可以继续 DFT。" : "网页 AI 结果已应用；现在必须执行本地 AI 全量图片复核。");
        await loadReviewCenter();
      } catch (error) {
        renderWebAiValidationFailure([{ code: "apply_request_failed", message: error.message }], []);
      } finally {
        applyButton.textContent = "应用图表结果";
        applyButton.disabled = true;
      }
    }

    async function finalizeWebAiEvidenceReview() {
      if (!activeEvidenceScope() || webAiReturnState.mode !== "evidence" || !webAiReturnState.validationResponse) {
        showToast("没有可 finalize 的当前固定图表审核范围。");
        return;
      }
      const button = document.getElementById("webAiFinalizeEvidenceBtn");
      if (!window.confirm("确认完成图表审核？\n" + evidenceScopeLabel() + "\n这一步只 finalize 已应用的当前范围，不会重新 apply JSON。")) return;
      button.disabled = true;
      button.textContent = "完成中...";
      try {
        const data = await fetchJSON(
          "/api/papers/" + encodeURIComponent(manualReviewContext.paperId) + "/chart-review-result/finalize" + (manualReviewContext.runId ? "?run_id=" + encodeURIComponent(manualReviewContext.runId) : ""),
          { method: "POST" }
        );
        const scopeIssues = evidenceScopeMismatchIssues(data);
        if (scopeIssues.length || !data.chart_review_completed) {
          renderWebAiValidationFailure(scopeIssues.length ? scopeIssues : (data.finalize_blocking_errors || data.unresolved_actions || []), []);
          return;
        }
        setWebAiValidationBox("<strong>图表审核已 finalize。</strong><div style=\"margin-top:8px;\">" + esc(evidenceScopeLabel()) + "</div>", "is-success");
        showToast("图表审核已完成；现在可以继续 DFT 终审。");
        await loadReviewCenter();
      } catch (error) {
        renderWebAiValidationFailure([{ code: "apply_request_failed", message: error.message }], []);
      } finally {
        button.textContent = "完成图表审核";
        button.disabled = false;
      }
    }
    // WEB_AI_RETURN_FEATURE_END
