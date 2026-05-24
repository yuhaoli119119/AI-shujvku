async function runInternalAIParse() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    switchTab("review");
    showProgress("内部 AI 正在审查并写回候选项...");
    try {
        const data = await fetchJSON(EXTERNAL_API + "/papers/" + state.selectedPaperId + "/internal-parse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_label: "内部AI解析",
                auto_apply: true
            })
        });
        $("externalRawText").value = "";
        showToast("内部 AI 解析完成。", "success");
        $("externalRuns").insertAdjacentHTML("afterbegin",
            '<div class="section-card"><h3>最近一次内部 AI 解析</h3><div class="mono">' + esc(JSON.stringify(data, null, 2)) + "</div></div>"
        );
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        $("externalRuns").innerHTML = '<div class="workspace-empty">内部 AI 解析失败：' + esc(error.message) + "</div>";
        showToast("内部 AI 解析失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadAgentGuide() {
    if (state.currentTab !== "review") {
        switchTab("review");
    }
    try {
        const guide = await fetchJSON("/api/system/agent-guide");
        $("externalRuns").innerHTML =
            '<div class="section-card"><h3>IDE / MCP AI 连接指南</h3>' +
            '<div class="subtle">外部 IDE AI 可以按这里的入口读取文献、追加 notes、提出 corrections、触发 parse；网页内部 AI 则使用本页“内部 AI 解析”直接写回候选项。</div>' +
            '<div class="mono" style="margin-top:12px;">' + esc(JSON.stringify(guide, null, 2)) + "</div></div>" +
            $("externalRuns").innerHTML;
    } catch (error) {
        showToast("读取 IDE / MCP 指南失败：" + error.message, "error");
    }
}

async function importExternalAnalysis() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const raw = $("externalRawText").value.trim();
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
        await fetchJSON(EXTERNAL_API + "/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                paper_id: state.selectedPaperId,
                source: $("externalSource").value.trim() || "manual",
                source_label: $("externalSourceLabel").value.trim() || "外部AI复核",
                raw_text: typeof rawPayload === "string" ? rawPayload : null,
                raw_payload: rawPayload
            })
        });
        showToast("外部 AI 审核结果已导入。", "success");
        $("externalRawText").value = "";
        await loadExternalRuns();
    } catch (error) {
        showToast("导入失败：" + error.message, "error");
    }
    hideProgress();
}

async function loadExternalRuns() {
    if (!state.selectedPaperId) return;
    $("externalRuns").innerHTML = '<div class="workspace-empty">正在加载审核记录...</div>';
    try {
        const runs = await fetchJSON(EXTERNAL_API + "/runs?paper_id=" + encodeURIComponent(state.selectedPaperId));
        state.externalRuns = runs || [];
        if (!state.externalRuns.length) {
            $("externalRuns").innerHTML = '<div class="workspace-empty">当前文献还没有审核记录。</div>';
            return;
        }
        $("externalRuns").innerHTML = state.externalRuns.map(function(run) {
            const pending = (run.candidates || []).filter(function(item) {
                return item.status === "pending" || item.status === "requires_resolution";
            });
            return (
                '<div class="run-card">' +
                    '<h4>' + esc(run.source_label || run.source || "未命名审核源") + "</h4>" +
                    '<div class="subtle">创建时间：' + esc(formatDate(run.created_at)) + " | 映射状态：" + esc(run.mapping_status || "-") + "</div>" +
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
    } catch (error) {
        $("externalRuns").innerHTML = '<div class="workspace-empty">审核记录加载失败：' + esc(error.message) + "</div>";
    }
}

function renderCandidates(candidates) {
    if (!candidates.length) {
        return '<div class="candidate-card"><div class="muted">没有候选项。</div></div>';
    }
    return candidates.map(function(item) {
        return (
            '<div class="candidate-card">' +
                '<h4>' + esc(item.candidate_type || "candidate") + " | 状态：" + esc(item.status || "-") + "</h4>" +
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
    $("aggregateResult").innerHTML = '<div class="workspace-empty">正在加载聚合视图...</div>';
    try {
        state.aggregateData = await fetchJSON(API_BASE + "/aggregate");
        $("aggregateResult").innerHTML =
            '<div class="section-card"><h3>吸附物聚合</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.adsorbate_groups || {}, null, 2)) + "</div></div>" +
            '<div class="section-card"><h3>催化剂聚合</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.catalyst_groups || {}, null, 2)) + "</div></div>" +
            '<div class="section-card"><h3>可能别名</h3><div class="mono">' + esc(JSON.stringify(state.aggregateData.possible_name_aliases || [], null, 2)) + "</div></div>";
    } catch (error) {
        $("aggregateResult").innerHTML = '<div class="workspace-empty">聚合视图加载失败：' + esc(error.message) + "</div>";
    }
}
