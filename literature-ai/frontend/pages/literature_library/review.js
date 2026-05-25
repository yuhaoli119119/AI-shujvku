async function runInternalAIParse() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    switchTab("review");
    showProgress("网页内 AI 正在生成候选项，完成后请手动确认写回...");
    try {
        const data = await fetchJSON(EXTERNAL_API + "/papers/" + state.selectedPaperId + "/internal-parse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_label: "网页内 AI 分析",
                auto_apply: false
            })
        });
        const extRawText = $("externalRawText");
        if (extRawText) extRawText.value = "";
        showToast("网页内 AI 候选项已生成，请手动确认后写回。", "success");
        const extRuns = $("externalRuns");
        if (extRuns) {
            extRuns.insertAdjacentHTML("afterbegin",
                '<div class="section-card"><h3>最近一次网页内 AI 分析</h3><div class="subtle">默认不会自动写回数据库。</div><div class="mono">' + esc(JSON.stringify(data, null, 2)) + "</div></div>"
            );
        }
        await loadExternalRuns();
    } catch (error) {
        const guide = $("internalAIConfigGuide");
        const message = "网页内 AI 尚未配置，请到 设置 -> API 配置 中填写 Writer API Key / Base URL / Model。";
        if (error.status === 400 && String(error.message || "").includes("Internal AI is not configured")) {
            if (guide) guide.innerHTML = '<div class="section-card" style="border-color:var(--color-warning);background:var(--color-warning-bg);"><div class="subtle" style="color:var(--color-warning);">' + message + '</div><div class="modal-actions" style="justify-content:flex-start;"><button class="btn primary small" onclick="window.location.href=\'../settings/index.html\'">打开设置页</button></div></div>';
            showToast(message, "error");
        } else {
            const extRuns = $("externalRuns");
            if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">网页内 AI 分析失败：' + esc(error.message) + "</div>";
            showToast("网页内 AI 分析失败：" + error.message, "error");
        }
    }
    hideProgress();
}

async function loadAgentGuide() {
    if (state.currentTab !== "review") {
        switchTab("review");
    }
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        const mcpGuide = $("mcpGuideBox");
        if (mcpGuide) {
            mcpGuide.innerHTML =
                '<div class="section-card"><h3>IDE / MCP AI 分析指南</h3>' +
                '<div class="subtle">外部 IDE / MCP AI 可以按这里的入口读取文献、追加 notes、提出 corrections 或触发 parse；本区只展示指南，不自动写回。</div>' +
                '<div class="mono" style="margin-top:12px;">' + esc(JSON.stringify(guide, null, 2)) + "</div></div>";
        }
    } catch (error) {
        showToast("读取 IDE / MCP 指南失败：" + error.message, "error");
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
        showToast("请粘贴外部 AI 返回结果。", "error");
        return;
    }
    showProgress("正在导入外部 AI 审核结果...");
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
                source_label: extSourceLabel ? extSourceLabel.value.trim() || "外部AI复核" : "外部AI复核",
                raw_text: typeof rawPayload === "string" ? rawPayload : null,
                raw_payload: rawPayload
            })
        });
        showToast("外部 AI 审核结果已导入。", "success");
        if (extRawText) extRawText.value = "";
        await loadExternalRuns();
    } catch (error) {
        showToast("导入失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadExternalRuns() {
    if (!state.selectedPaperId) return;
    const extRuns = $("externalRuns");
    if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">正在加载审核记录...</div>';
    try {
        const runs = await fetchJSON(EXTERNAL_API + "/runs?paper_id=" + encodeURIComponent(state.selectedPaperId));
        state.externalRuns = runs || [];
        if (!state.externalRuns.length) {
            if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">当前文献还没有审核记录。</div>';
            return;
        }
        if (extRuns) {
            extRuns.innerHTML = state.externalRuns.map(function(run) {
                const pending = (run.candidates || []).filter(function(item) {
                    return item.status === "pending" || item.status === "requires_resolution";
                });
                return (
                    '<div class="run-card">' +
                        '<h4>' + esc(run.source_label || uiLabel("source", run.source) || "未命名审核源") + "</h4>" +
                        '<div class="subtle">创建时间：' + esc(formatDate(run.created_at)) + " | 映射状态：" + esc(uiLabel("mapping_status", run.mapping_status || "-")) + "</div>" +
                        (run.mapping_error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">错误：' + esc(run.mapping_error) + "</div>" : "") +
                        (run.raw_text ? '<div class="mono" style="margin-top:10px;">' + esc(ellipsis(run.raw_text, 1200)) + "</div>" : "") +
                        '<div class="candidate-toolbar" style="margin-top:12px;">' +
                            '<button class="btn blue small" onclick="materializeRun(\'' + run.id + '\')">全部写回数据库</button>' +
                            '<button class="btn ghost small" onclick="toggleRunCandidates(\'' + run.id + '\')">展开候选项（' + (run.candidates || []).length + "）</button>" +
                        '</div>' +
                        '<div id="run-candidates-' + run.id + '" style="display:none;">' +
                            renderCandidates(run.candidates || []) +
                        '</div>' +
                        (pending.length ? '<div class="subtle" style="margin-top:10px;">待处理候选项：' + pending.length + " 个</div>" : '<div class="subtle" style="margin-top:10px;">当前 run 没有待处理候选项。</div>') +
                    "</div>"
                );
            }).join("");
        }
    } catch (error) {
        if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">审核记录加载失败：' + esc(error.message) + "</div>";
    }
}

function renderCandidates(candidates) {
    if (!candidates.length) {
        return '<div class="candidate-card"><div class="muted">没有候选项。</div></div>';
    }
    return candidates.map(function(item) {
        return (
            '<div class="candidate-card">' +
                '<h4>' + esc(item.candidate_type || "候选项") + " | 状态：" + esc(uiLabel("candidate_status", item.status || "-")) + "</h4>" +
                '<div class="subtle">置信度：' + esc(item.confidence == null ? "-" : item.confidence) + " | 目标类型：" + esc(item.materialized_target_type || "-") + "</div>" +
                '<div class="mono" style="margin-top:10px;">' + esc(JSON.stringify(item.normalized_payload || {}, null, 2)) + "</div>" +
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
    showProgress("正在把候选项写回数据库...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/materialize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidate_ids: [], created_by: "web_user" })
        });
        showToast("候选项已写回数据库。", "success");
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("写回失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadAggregate() {
    const aggResult = $("aggregateResult");
    if (aggResult) aggResult.innerHTML = '<div class="workspace-empty">正在加载聚合视图...</div>';
    try {
        state.aggregateData = await fetchJSON(API_BASE + "/aggregate");
        if (aggResult) {
            aggResult.innerHTML =
                '<div class="section-card"><h3>吸附物聚合</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.adsorbate_groups || {}, null, 2)) + "</div></div>" +
                '<div class="section-card"><h3>催化剂聚合</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.catalyst_groups || {}, null, 2)) + "</div></div>" +
                '<div class="section-card"><h3>可能别名</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.possible_name_aliases || [], null, 2)) + "</div></div>";
        }
    } catch (error) {
        if (aggResult) aggResult.innerHTML = '<div class="workspace-empty">聚合视图加载失败：' + esc(error.message) + "</div>";
    }
}
