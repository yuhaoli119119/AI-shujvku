function renderInternalAIConfigGuide(message, status) {
    const guide = $("internalAIConfigGuide");
    if (guide) {
        guide.innerHTML =
            '<div class="section-card">' +
            '<h3>IDE AI 审阅入口</h3>' +
            '<div class="subtle">' + esc(message) + "</div>" +
            '<div class="modal-actions" style="justify-content:flex-start;">' +
            '<button class="btn primary small" onclick="loadAgentGuide()">显示 IDE AI 指南</button>' +
            "</div></div>";
    }
}

function candidateTypeLabel(type) {
    if (type === "correction") return "修正建议";
    if (type === "note") return "阅读笔记";
    if (type === "relationship") return "文献关系";
    return "IDE AI 回写项";
}

function renderCandidatePayload(payload) {
    payload = payload || {};
    const fields = [
        ["content", "内容"],
        ["field_name", "字段"],
        ["quoted_text", "原文依据"],
        ["page", "页码"],
        ["section_title", "章节"],
        ["mapping_reason", "判断理由"],
        ["reason", "原因"],
        ["source_paper_id", "来源论文"],
        ["target_paper_id", "目标论文"],
        ["relationship_type", "关系类型"]
    ];
    const html = fields.map(function(pair) {
        const value = payload[pair[0]];
        if (value === null || value === undefined || value === "") return "";
        return '<div class="readable-field"><div class="k">' + esc(pair[1]) + '</div><div class="v">' + esc(Array.isArray(value) ? value.join("；") : value) + '</div></div>';
    }).filter(Boolean).join("");
    return html
        ? '<div class="readable-grid candidate-readable">' + html + '</div>'
        : '<div class="muted">这个候选没有可展示的结构化字段。</div>';
}

function renderInternalParseSummary(data) {
    return '<div class="section-card"><h3>网页端解析已停用</h3>' +
        '<div class="subtle">请在 IDE 中优先使用 MCP 读取材料、核验证据。普通非 DFT 字段通过 import_analysis 回写；表格对象修改/新建/合并/删除必须直接调用 update_table、create_table、merge_table、delete_table。如果当前会话没暴露 MCP 工具，可使用仓库内 `literature-ai/backend` 的 `app.mcp.context.mcp_auth_context` + `app.mcp.server` 受控调用同一套公开 MCP 工具。禁止直接操作 service、session、model 或数据库。</div>' +
    '</div>';
}

async function ensureInternalAIConfigured() {
    const message = "网页端解析审阅已停用。请优先使用 IDE MCP；工具未注入时可通过项目内 mcp_auth_context + app.mcp.server 受控调用公开 MCP 工具，不能直接写数据库。";
    renderInternalAIConfigGuide(message, null);
    showToast(message, "info");
    return false;
}

async function runInternalAIParse() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    switchTab("review");
    await ensureInternalAIConfigured();
    await loadAgentGuide();
    hideProgress(true);
}

async function loadAgentGuide() {
    if (state.currentTab !== "review") {
        switchTab("review");
    }
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        const mcpGuide = $("mcpGuideBox");
        if (mcpGuide) {
            const entry = guide.recommended_entrypoint || {};
            const endpoints = Array.isArray(guide.http_endpoints) ? guide.http_endpoints : [];
            const tools = guide.mcp && Array.isArray(guide.mcp.common_tools) ? guide.mcp.common_tools : [];
            mcpGuide.innerHTML =
                '<div class="section-card"><h3>IDE AI 审阅指南</h3>' +
                '<div class="subtle">IDE AI 优先使用当前会话的 MCP 工具；工具未注入时，可通过仓库内 `app.mcp.context.mcp_auth_context` 与 `app.mcp.server` 受控调用同一套公开 MCP 工具。正式 HTTP API只补充其已覆盖的读取和操作，不能替代没有 HTTP 等价入口的 MCP 工具。本区只展示入口，不复制正式任务提示词；请回审核中心按单篇文献发起 AI 审核任务。</div>' +
                '<div class="readable-grid" style="margin-top:10px;">' +
                    '<div class="readable-field"><div class="k">推荐入口</div><div class="v">' + esc((entry.method || "") + " " + (entry.path || "")) + '</div></div>' +
                    '<div class="readable-field"><div class="k">适用场景</div><div class="v">' + esc(entry.description || "通过外部工具读取和审阅文献。") + '</div></div>' +
                    '<div class="readable-field"><div class="k">MCP 地址</div><div class="v">' + esc((guide.mcp && guide.mcp.url) || "/mcp") + '</div></div>' +
                    '<div class="readable-field"><div class="k">常用工具</div><div class="v">' + esc(tools.join("、") || "-") + '</div></div>' +
                '</div>' +
                '<details><summary>查看可用入口</summary>' +
                    endpoints.map(function(item) {
                        return '<div class="readable-field" style="margin-top:8px;"><div class="k">' + esc(item.name || item.path || "接口") + '</div><div class="v">' + esc((item.method || "") + " " + (item.path || "")) + '<br>' + esc(item.purpose || "") + '</div></div>';
                    }).join("") +
                '</details></div>';
        }
    } catch (error) {
        showToast("读取 IDE AI 指南失败：" + error.message, "error");
    }
}

async function importExternalAnalysis() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const extRawText = $("externalRawText");
    const raw = extRawText ? extRawText.value.trim() : "";
    if (!raw) {
        showToast("请粘贴 IDE AI 返回结果。", "error");
        return;
    }
    showProgress("正在导入 IDE AI 回写结果...");
    let rawPayload = raw;
    try {
        rawPayload = JSON.parse(raw);
    } catch (_) {}
    try {
        const extSource = $("externalSource");
        const extSourceLabel = $("externalSourceLabel");
        await fetchJSON(EXTERNAL_API + "/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                paper_id: state.selectedPaperId,
                source: normalizeExternalSourceForApi(extSource ? extSource.value : "manual"),
                source_label: extSourceLabel ? (extSourceLabel.value.trim() || "IDE AI 回写结果") : "IDE AI 回写结果",
                raw_text: typeof rawPayload === "string" ? rawPayload : null,
                raw_payload: rawPayload
            })
        });
        showToast("IDE AI 回写结果已导入。", "success");
        if (extRawText) extRawText.value = "";
        await loadExternalRuns();
    } catch (error) {
        showToast("导入失败：" + error.message, "error");
    }
    hideProgress();
}

function externalCandidateAction(item) {
    item = item || {};
    const explicitMode = String(item.action_mode || "").trim();
    const explicitScope = String(item.action_scope || "").trim();
    if (["materialize", "apply_review_rules", "readonly"].includes(explicitMode)) {
        return {
            mode: explicitMode,
            scope: explicitScope === "run" ? "run" : "candidate"
        };
    }
    const candidateType = String(item.candidate_type || "").trim().toLowerCase();
    const status = String(item.status || "").trim().toLowerCase();
    if (
        candidateType === "object_review_audit" &&
        ["candidate", "pending", "requires_resolution"].includes(status)
    ) {
        return { mode: "apply_review_rules", scope: "run" };
    }
    if (["note", "correction", "relationship"].includes(candidateType) && status === "pending") {
        return { mode: "materialize", scope: "candidate" };
    }
    return { mode: "readonly", scope: "candidate" };
}

function externalRunActions(run) {
    const candidates = Array.isArray(run && run.candidates) ? run.candidates : [];
    return {
        reviewRules: candidates.filter(function(item) {
            return externalCandidateAction(item).mode === "apply_review_rules";
        }),
        materializable: candidates.filter(function(item) {
            return externalCandidateAction(item).mode === "materialize";
        })
    };
}

async function loadExternalRuns() {
    if (!state.selectedPaperId) return;
    const extRuns = $("externalRuns");
    const reasonBanner = state.qualityReasonContext
        ? '<div class="section-card" style="border-color:var(--color-warning);"><h3>DFT 质量处理入口</h3><div class="subtle">来自 blocked reason：' + esc(state.qualityReasonContext) + '。请核对本论文的 review 状态、证据链和定位信息。</div></div>'
        : "";
    const contextGuide = $("internalAIConfigGuide");
    if (reasonBanner && contextGuide && contextGuide.getAttribute("data-quality-reason") !== state.qualityReasonContext) {
        contextGuide.setAttribute("data-quality-reason", state.qualityReasonContext);
        contextGuide.insertAdjacentHTML("afterbegin", reasonBanner);
    }
    if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">正在加载 IDE AI 回写记录...</div>';
    try {
        const runs = await fetchJSON(EXTERNAL_API + "/runs?paper_id=" + encodeURIComponent(state.selectedPaperId));
        state.externalRuns = runs || [];
        if (state.currentTab === "review" && state.selectedPaperId) {
            rerenderSelectedDetail(state.selectedPaperId);
            if (extRuns) extRuns.innerHTML = "";
            return;
        }
        if (!state.externalRuns.length) {
            if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">当前文献还没有 IDE AI 回写记录。请在 IDE 中优先读取 MCP 材料；普通非 DFT 字段通过 import_analysis 回写，表格对象整理直接调用 update_table/create_table/merge_table/delete_table。如果当前会话没暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。确认前不会写入正式数据。</div>';
            return;
        }
        if (extRuns) {
            extRuns.innerHTML = state.externalRuns.map(function(run) {
                const actions = externalRunActions(run);
                const actionableCount = actions.reviewRules.length + actions.materializable.length;
                const primaryAction = actions.reviewRules.length
                    ? '<button class="btn blue small" onclick="applyReviewRulesForRun(\'' + run.id + '\')">应用本 run 审核规则（含 DFT）</button>'
                    : (actions.materializable.length
                        ? '<button class="btn blue small" onclick="materializeRun(\'' + run.id + '\')">批量应用/记录</button>'
                        : '');
                const selectedAction = actions.materializable.length
                    ? '<button class="btn ghost small" onclick="materializeSelectedCandidates(\'' + run.id + '\')">选中应用/记录</button>'
                    : '';
                return (
                    '<div class="run-card">' +
                        '<h4>' + esc(run.source_label || uiLabel("source", run.source) || "未命名候选源") + "</h4>" +
                        '<div class="subtle">创建时间：' + esc(formatDate(run.created_at)) + " | 映射状态：" + esc(uiLabel("mapping_status", run.mapping_status || "-")) + "</div>" +
                        '<div class="subtle" style="margin-top:8px;">用途：这里显示 IDE AI 通过 import_analysis 回写的结果。阅读笔记用于快速理解论文；修正/关联建议用于补全或纠错。DFT 数据仍按审核中心的多 AI 冲突流程处理。</div>' +
                        (run.mapping_error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">错误：' + esc(run.mapping_error) + "</div>" : "") +
                        '<div class="candidate-toolbar" style="margin-top:12px;">' +
                            primaryAction +
                            selectedAction +
                            '<button class="btn ghost small" onclick="toggleRunCandidates(\'' + run.id + '\')">展开审阅项（' + (run.candidates || []).length + "）</button>" +
                            '<button class="btn ghost small" onclick="deleteExternalRun(\'' + run.id + '\')">删除记录</button>' +
                        "</div>" +
                        '<div id="run-candidates-' + run.id + '" style="display:none;">' +
                            renderCandidates(run.id, run.candidates || []) +
                        "</div>" +
                        (actionableCount
                            ? '<div class="subtle" style="margin-top:10px;">待处理候选项：' + actionableCount + " 个</div>"
                            : '<div class="subtle" style="margin-top:10px;">当前 run 没有待处理候选项。</div>') +
                    "</div>"
                );
            }).join("");
        }
    } catch (error) {
        if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">IDE AI 回写记录加载失败：' + esc(error.message) + "</div>";
    }
}

function renderCandidates(runId, candidates) {
    if (!candidates.length) {
        return '<div class="candidate-card"><div class="muted">没有审阅项。</div></div>';
    }
    return candidates.map(function(item) {
        var candidateId = String(item.id || "");
        var action = externalCandidateAction(item);
        var checkbox = action.mode === "materialize" && candidateId
            ? '<label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;margin:0;"><input type="checkbox" class="candidate-select" data-run-id="' + escAttr(runId) + '" value="' + escAttr(candidateId) + '">选择</label>'
            : "";
        var singleAction = "";
        if (action.mode === "materialize" && candidateId) {
            singleAction = '<button class="btn ghost small" onclick="materializeCandidate(\'' + escAttr(runId) + '\', \'' + escAttr(candidateId) + '\')">应用/记录</button>';
        } else if (action.mode === "apply_review_rules") {
            singleAction = '<button class="btn ghost small" onclick="applyReviewRulesForRun(\'' + escAttr(runId) + '\')">按本 run 审核规则处理</button>';
        } else if (item.status === "requires_resolution") {
            singleAction = '<span class="muted" style="font-size:12px;">需人工处理</span>';
        } else if (["materialized", "ai_reviewed", "ai_applied"].includes(String(item.status || "").toLowerCase())) {
            singleAction = '<span class="muted" style="font-size:12px;">已处理</span>';
        } else if (String(item.status || "").toLowerCase() === "skipped") {
            singleAction = '<span class="muted" style="font-size:12px;">已跳过</span>';
        } else {
            singleAction = '<span class="muted" style="font-size:12px;">只读候选</span>';
        }
        var candidateLabel = "";
        if (item.candidate_type === "correction") {
            candidateLabel = '<span style="background:var(--color-warning-bg);color:var(--color-warning);border:1px solid var(--color-warning)40;padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">AI 建议 / 待应用</span>';
        } else if (item.candidate_type === "note") {
            candidateLabel = '<span style="background:var(--color-primary-bg);color:var(--color-primary);border:1px solid var(--color-primary)40;padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">AI 笔记建议 / 待记录</span>';
        } else if (item.candidate_type === "relationship") {
            candidateLabel = '<span style="background:var(--color-primary-bg);color:var(--color-primary);border:1px solid var(--color-primary)40;padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">AI 关联建议 / 待记录</span>';
        } else if (item.candidate_type === "object_review_audit") {
            candidateLabel = '<span style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">审核候选 / 按 run 处理</span>';
        } else {
            candidateLabel = '<span style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">候选建议 / 待记录</span>';
        }
        return (
            '<div class="candidate-card">' +
                '<div style="display:flex;justify-content:space-between;gap:10px;align-items:center;">' +
                    '<h4>' + esc(candidateTypeLabel(item.candidate_type)) + candidateLabel + " | 状态：" + esc(uiLabel("candidate_status", item.status || "-")) + "</h4>" +
                    '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' + checkbox + singleAction + "</div>" +
                "</div>" +
                '<div class="subtle">置信度：' + esc(item.confidence == null ? "-" : item.confidence) + " | 目标类型：" + esc(item.materialized_target_type || "-") + "</div>" +
                renderCandidatePayload(item.normalized_payload || {}) +
            "</div>"
        );
    }).join("");
}

function toggleRunCandidates(runId) {
    const el = $("run-candidates-" + runId);
    if (!el) return;
    el.style.display = el.style.display === "none" ? "block" : "none";
}

async function materializeRun(runId) {
    var run = (state.externalRuns || []).find(function(item) { return item.id === runId; });
    var actions = externalRunActions(run);
    if (actions.reviewRules.length) {
        await applyReviewRulesForRun(runId);
        return;
    }
    var candidateIds = actions.materializable.map(function(item) { return String(item.id || ""); }).filter(Boolean);
    if (!candidateIds.length) {
        showToast("当前 run 没有可生成的候选建议。", "error");
        return;
    }
    var ok = confirm(
        "将应用/记录 " + candidateIds.length + " 个非 DFT IDE AI 回写项。\n\n" +
        "后续 AI 可再次覆盖这些非 DFT 字段。\n\n" +
        "是否继续？"
    );
    if (!ok) return;
    showProgress("正在应用/记录 AI 回写...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/materialize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidate_ids: candidateIds, created_by: "web_user" })
        });
        showToast("非 DFT AI 回写已处理。", "success");
        await loadExternalRuns();
        await refreshSelectedPaperDetail({ reason: "external_run_materialized" });
    } catch (error) {
        showToast("处理 AI 回写失败：" + error.message, "error");
    }
    hideProgress();
}

async function applyReviewRulesForRun(runId) {
    var run = (state.externalRuns || []).find(function(item) { return item.id === runId; });
    var actions = externalRunActions(run);
    if (!actions.reviewRules.length) {
        showToast("当前 run 没有待应用的审核规则候选。", "error");
        return;
    }
    var totalCount = actions.reviewRules.length + actions.materializable.length;
    var ok = confirm(
        "这是整 run 操作，将按审核规则处理该 run 的 " + totalCount + " 个可处理项。\n\n" +
        "DFT 项只会生成或更新审核候选、issue 和共识状态，不会自动变成人工 verified，也不会绕过导出安全门。\n\n" +
        "是否继续？"
    );
    if (!ok) return;
    showProgress("正在应用本 run 审核规则...");
    try {
        var result = await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/apply-review-rules", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reviewer: "web_user" })
        });
        var summary = result && result.auto_apply_summary ? result.auto_apply_summary : {};
        var newDft = summary.new_dft_candidates || {};
        showToast("审核规则已执行；新物化 DFT 候选 " + Number(newDft.materialized_count || 0) + " 个。", "success");
        await loadExternalRuns();
        await refreshSelectedPaperDetail({ reason: "external_run_review_rules_applied" });
    } catch (error) {
        showToast("应用审核规则失败：" + error.message, "error");
    }
    hideProgress();
}

async function deleteExternalRun(runId) {
    var ok = confirm("删除这条 IDE AI 回写记录？已生成的人工确认记录不会被删除。");
    if (!ok) return;
    showProgress("正在删除 IDE AI 回写记录...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/delete", { method: "POST" });
        showToast("IDE AI 回写记录已删除。", "success");
        await loadExternalRuns();
    } catch (error) {
        showToast("删除失败：" + error.message, "error");
    }
    hideProgress();
}

async function materializeCandidate(runId, candidateId) {
    await materializeCandidateIds(runId, [candidateId]);
}

async function materializeSelectedCandidates(runId) {
    var ids = Array.from(document.querySelectorAll('.candidate-select[data-run-id="' + runId + '"]:checked')).map(function(input) {
        return input.value;
    });
    if (!ids.length) {
        showToast("请先选择要生成记录的 IDE AI 回写候选。", "error");
        return;
    }
    await materializeCandidateIds(runId, ids);
}

async function materializeCandidateIds(runId, candidateIds) {
    if (!candidateIds.length) return;
    var run = (state.externalRuns || []).find(function(item) { return item.id === runId; });
    var byId = new Map(((run && run.candidates) || []).map(function(item) { return [String(item.id || ""), item]; }));
    var invalidIds = candidateIds.filter(function(candidateId) {
        var candidate = byId.get(String(candidateId));
        return !candidate || externalCandidateAction(candidate).mode !== "materialize";
    });
    if (invalidIds.length) {
        showToast("所选项目包含不能按单候选应用的审核项；DFT 审核必须按整个 run 处理。", "error");
        return;
    }
    var ok = confirm(
        "将处理 " + candidateIds.length + " 个 IDE AI 回写项。\n\n" +
        "这里只包含可按单候选执行的非 DFT 项，后续 AI 可再次覆盖。\n\n" +
        "是否继续？"
    );
    if (!ok) return;
    showProgress("正在应用/记录 AI 回写...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/materialize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidate_ids: candidateIds, created_by: "web_user" })
        });
        showToast("AI 回写已处理。", "success");
        await loadExternalRuns();
        await refreshSelectedPaperDetail({ reason: "external_candidates_materialized" });
    } catch (error) {
        showToast("处理 AI 回写失败：" + error.message, "error");
    }
    hideProgress();
}

function renderAggregateGroups(title, groups, emptyText) {
    groups = groups || {};
    const entries = Object.entries(groups);
    if (!entries.length) {
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">' + esc(emptyText || "暂无数据。") + '</div></div>';
    }
    return '<div class="section-card"><h3>' + esc(title) + '</h3>' + entries.map(function(pair) {
        const name = pair[0];
        const items = Array.isArray(pair[1]) ? pair[1] : [];
        return '<div class="readable-card" style="margin-top:10px;">' +
            '<h4>' + esc(name || "未命名") + ' <span class="muted">(' + esc(items.length) + ' 条)</span></h4>' +
            '<div class="readable-grid">' + items.slice(0, 8).map(function(item) {
                return '<div class="readable-field">' +
                    '<div class="k">' + esc(item.name || item.catalyst_type || item.paper_id || "记录") + '</div>' +
                    '<div class="v">' +
                        (item.catalyst_type ? '类型：' + esc(item.catalyst_type) + '<br>' : '') +
                        (item.metal_centers && item.metal_centers.length ? '金属中心：' + esc(item.metal_centers.join("、")) + '<br>' : '') +
                        (item.support ? '载体：' + esc(item.support) + '<br>' : '') +
                        (item.synthesis_method ? '合成：' + esc(item.synthesis_method) : '') +
                    '</div>' +
                '</div>';
            }).join("") + '</div>' +
        '</div>';
    }).join("") + '</div>';
}

function renderAggregateAliases(items) {
    items = items || [];
    if (!items.length) {
        return '<div class="section-card"><h3>可能别名</h3><div class="muted">暂无需要人工合并的别名。</div></div>';
    }
    return '<div class="section-card"><h3>可能别名</h3><div class="readable-grid">' + items.map(function(item) {
        return '<div class="readable-field"><div class="k">' + esc(item.name || item.alias || "别名") + '</div><div class="v">' + esc(item.reason || item.note || "需要人工确认是否为同一名称。") + '</div></div>';
    }).join("") + '</div></div>';
}

async function loadAggregate() {
    const aggResult = $("aggregateResult");
    if (aggResult) aggResult.innerHTML = '<div class="workspace-empty">正在加载聚合视图...</div>';
    try {
        const params = new URLSearchParams();
        const libraryName = getCurrentLibraryName();
        if (libraryName) params.set("library_name", libraryName);
        const query = params.toString();
        state.aggregateData = await fetchJSON(API_BASE + "/aggregate" + (query ? "?" + query : ""));
        if (aggResult) {
            aggResult.innerHTML =
                renderAggregateGroups("吸附物聚合", state.aggregateData.adsorbate_groups || {}, "暂无吸附物聚合结果。") +
                renderAggregateGroups("催化剂聚合", state.aggregateData.catalyst_groups || {}, "暂无催化剂聚合结果。") +
                renderAggregateAliases(state.aggregateData.possible_name_aliases || []);
        }
    } catch (error) {
        if (aggResult) aggResult.innerHTML = '<div class="workspace-empty">聚合视图加载失败：' + esc(error.message) + "</div>";
    }
}
