function renderInternalAIConfigGuide(message, status) {
    const guide = $("internalAIConfigGuide");
    const missingMap = {
        internal_parser_api_base: "内部解析 API Base URL（复用 Writer LLM）",
        internal_parser_api_key: "内部解析 API Key（复用 Writer LLM）",
        internal_parser_model: "内部解析 Model（复用 Writer LLM）",
        writer_api_base: "Writer API Base URL",
        writer_api_key: "Writer API Key",
        writer_model: "Writer Model"
    };
    const missing = status && status.missing ? status.missing.map(function(item) {
        return missingMap[item] || item;
    }) : [];
    const detail = missing.length ? "缺少：" + missing.join(" / ") + "。" : "";
    if (guide) {
        guide.innerHTML =
            '<div class="section-card" style="border-color:var(--color-warning);background:var(--color-warning-bg);">' +
            '<div class="subtle" style="color:var(--color-warning);">' + esc(message) + "</div>" +
            (detail ? '<div class="subtle" style="margin-top:8px;">' + esc(detail) + "</div>" : "") +
            '<div class="modal-actions" style="justify-content:flex-start;">' +
            '<button class="btn primary small" onclick="window.location.href=\'../settings/index.html\'">打开设置页</button>' +
            "</div></div>";
    }
}

async function ensureInternalAIConfigured() {
    try {
        const status = await fetchJSON("/api/settings/status");
        const internalParser = status && (status.internal_parser || status.writer);
        if (internalParser && internalParser.configured) {
            return true;
        }
        const message = "内部解析配置未完成：它复用 Writer LLM 连接，不使用 Embedding 配置。请在设置 -> API 配置中补 Writer LLM 的 Base URL / API Key / Model。";
        renderInternalAIConfigGuide(message, internalParser || null);
        showToast(message, "error");
        return false;
    } catch (error) {
        showToast("无法确认内部解析配置状态：" + error.message, "error");
        return false;
    }
}

async function runInternalAIParse() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    switchTab("review");
    if (!(await ensureInternalAIConfigured())) {
        hideProgress(true);
        return;
    }
    showProgress("网页内 AI 正在生成候选项，完成后可生成待确认记录。");
    let hideImmediately = false;
    try {
        const data = await fetchJSON(EXTERNAL_API + "/papers/" + state.selectedPaperId + "/internal-parse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_label: "网页内 AI 建议候选",
                auto_apply: false
            })
        });
        const extRawText = $("externalRawText");
        if (extRawText) extRawText.value = "";
        showToast("网页内 AI 建议候选已生成，请人工确认后再处理。", "success");
        const extRuns = $("externalRuns");
        if (extRuns) {
            extRuns.insertAdjacentHTML(
                "afterbegin",
                '<div class="section-card"><h3>最近一次网页内 AI 建议候选</h3><div class="subtle">默认只生成候选，不会生成待确认记录。</div><div class="mono">' +
                    esc(JSON.stringify(data, null, 2)) +
                    "</div></div>"
            );
        }
        await loadExternalRuns();
    } catch (error) {
        hideImmediately = true;
        const guide = $("internalAIConfigGuide");
        const internalParserMessage = "内部解析配置未完成：它复用 Writer LLM 连接，不使用 Embedding 配置。请在设置 -> API 配置中补 Writer LLM 的 Base URL / API Key / Model。";
        const message = "网页内 AI 尚未配置，请到 设置 -> API 配置 中填写 Writer API Key / Base URL / Model。";
        if (error.status === 400 && String(error.message || "").includes("Internal AI is not configured")) {
            hideProgress(true);
            if (guide) {
                guide.innerHTML =
                    '<div class="section-card" style="border-color:var(--color-warning);background:var(--color-warning-bg);">' +
                    '<div class="subtle" style="color:var(--color-warning);">' + internalParserMessage + "</div>" +
                    '<div class="modal-actions" style="justify-content:flex-start;">' +
                    '<button class="btn primary small" onclick="window.location.href=\'../settings/index.html\'">打开设置页</button>' +
                    "</div></div>";
            }
            showToast(internalParserMessage, "error");
        } else {
            const extRuns = $("externalRuns");
            if (extRuns) {
                extRuns.innerHTML = '<div class="workspace-empty">网页内 AI 建议候选生成失败：' + esc(error.message) + "</div>";
            }
            showToast("网页内 AI 建议候选生成失败：" + error.message, "error");
        }
    }
    hideProgress(hideImmediately);
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
                '<div class="section-card"><h3>IDE / MCP AI 建议指南</h3>' +
                '<div class="subtle">外部 IDE / MCP AI 可以按这里的入口读取文献、追加 notes、提出 corrections 或触发 parse；本区只展示指南，不会生成待确认记录。</div>' +
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
    showProgress("正在导入外部 AI 候选建议...");
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
                source_label: extSourceLabel ? (extSourceLabel.value.trim() || "外部 AI 候选建议") : "外部 AI 候选建议",
                raw_text: typeof rawPayload === "string" ? rawPayload : null,
                raw_payload: rawPayload
            })
        });
        showToast("外部 AI 候选建议已导入。", "success");
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
    if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">正在加载 AI 候选记录...</div>';
    try {
        const runs = await fetchJSON(EXTERNAL_API + "/runs?paper_id=" + encodeURIComponent(state.selectedPaperId));
        state.externalRuns = runs || [];
        if (!state.externalRuns.length) {
            if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">当前文献还没有 AI 候选记录。</div>';
            return;
        }
        if (extRuns) {
            extRuns.innerHTML = state.externalRuns.map(function(run) {
                const pending = (run.candidates || []).filter(function(item) {
                    return item.status === "pending" || item.status === "requires_resolution";
                });
                return (
                    '<div class="run-card">' +
                        '<h4>' + esc(run.source_label || uiLabel("source", run.source) || "未命名候选源") + "</h4>" +
                        '<div class="subtle">创建时间：' + esc(formatDate(run.created_at)) + " | 映射状态：" + esc(uiLabel("mapping_status", run.mapping_status || "-")) + "</div>" +
                        (run.mapping_error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">错误：' + esc(run.mapping_error) + "</div>" : "") +
                        (run.raw_text ? '<div class="mono" style="margin-top:10px;">' + esc(ellipsis(run.raw_text, 1200)) + "</div>" : "") +
                        '<div class="candidate-toolbar" style="margin-top:12px;">' +
                            '<button class="btn blue small" onclick="materializeRun(\'' + run.id + '\')">批量生成待确认记录</button>' +
                            '<button class="btn ghost small" onclick="materializeSelectedCandidates(\'' + run.id + '\')">选中生成待确认记录</button>' +
                            '<button class="btn ghost small" onclick="toggleRunCandidates(\'' + run.id + '\')">展开候选项（' + (run.candidates || []).length + "）</button>" +
                        "</div>" +
                        '<div id="run-candidates-' + run.id + '" style="display:none;">' +
                            renderCandidates(run.id, run.candidates || []) +
                        "</div>" +
                        (pending.length
                            ? '<div class="subtle" style="margin-top:10px;">待处理候选项：' + pending.length + " 个</div>"
                            : '<div class="subtle" style="margin-top:10px;">当前 run 没有待处理候选项。</div>') +
                    "</div>"
                );
            }).join("");
        }
    } catch (error) {
        if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">AI 候选记录加载失败：' + esc(error.message) + "</div>";
    }
}

function renderCandidates(runId, candidates) {
    if (!candidates.length) {
        return '<div class="candidate-card"><div class="muted">没有候选项。</div></div>';
    }
    return candidates.map(function(item) {
        var candidateId = String(item.id || "");
        var isPending = item.status === "pending" || item.status === "requires_resolution";
        var checkbox = isPending && candidateId
            ? '<label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;margin:0;"><input type="checkbox" class="candidate-select" data-run-id="' + escAttr(runId) + '" value="' + escAttr(candidateId) + '">选择</label>'
            : '<span class="muted" style="font-size:12px;">已处理</span>';
        var singleAction = isPending && candidateId
            ? '<button class="btn ghost small" onclick="materializeCandidate(\'' + escAttr(runId) + '\', \'' + escAttr(candidateId) + '\')">生成待确认记录</button>'
            : "";
        var candidateLabel = "";
        if (item.candidate_type === "correction") {
            candidateLabel = '<span style="background:var(--color-warning-bg);color:var(--color-warning);border:1px solid var(--color-warning)40;padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">AI 建议 / 待生成记录</span>';
        } else if (item.candidate_type === "note") {
            candidateLabel = '<span style="background:var(--color-primary-bg);color:var(--color-primary);border:1px solid var(--color-primary)40;padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">AI 笔记建议 / 待生成记录</span>';
        } else if (item.candidate_type === "relationship") {
            candidateLabel = '<span style="background:var(--color-primary-bg);color:var(--color-primary);border:1px solid var(--color-primary)40;padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">AI 关联建议 / 待生成记录</span>';
        } else {
            candidateLabel = '<span style="background:var(--color-surface-alt);color:var(--color-text-secondary);border:1px solid var(--color-border);padding:1px 6px;font-size:10px;font-weight:700;border-radius:var(--radius-pill);margin-left:4px;">候选建议 / 待生成记录</span>';
        }
        return (
            '<div class="candidate-card">' +
                '<div style="display:flex;justify-content:space-between;gap:10px;align-items:center;">' +
                    '<h4>' + esc(item.candidate_type || "候选建议") + candidateLabel + " | 状态：" + esc(uiLabel("candidate_status", item.status || "-")) + "</h4>" +
                    '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' + checkbox + singleAction + "</div>" +
                "</div>" +
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
    var run = (state.externalRuns || []).find(function(item) { return item.id === runId; });
    var pendingCount = run ? (run.candidates || []).filter(function(item) {
        return item.status === "pending" || item.status === "requires_resolution";
    }).length : 0;
    if (!pendingCount) {
        showToast("当前 run 没有可生成的候选建议。", "error");
        return;
    }
    var ok = confirm(
        "将处理 " + pendingCount + " 个 AI 建议候选，生成待确认记录。\n\n" +
        "这不是人工 verified，只是生成待确认记录；仍需在人工确认工作台核对证据并确认。\n\n" +
        "是否继续？"
    );
    if (!ok) return;
    showProgress("正在生成待确认记录...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/materialize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ explicit_all: true, created_by: "web_user" })
        });
        showToast("已生成待确认记录。", "success");
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("生成待确认记录失败：" + error.message, "error");
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
        showToast("请先选择要生成记录的 AI 候选建议。", "error");
        return;
    }
    await materializeCandidateIds(runId, ids);
}

async function materializeCandidateIds(runId, candidateIds) {
    if (!candidateIds.length) return;
    var ok = confirm(
        "将处理 " + candidateIds.length + " 个 AI 建议候选，生成待确认记录。\n\n" +
        "这不是人工 verified，只是生成待确认记录；仍需在人工确认工作台核对证据并确认。\n\n" +
        "是否继续？"
    );
    if (!ok) return;
    showProgress("正在生成待确认记录...");
    try {
        await fetchJSON(EXTERNAL_API + "/runs/" + runId + "/materialize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ candidate_ids: candidateIds, created_by: "web_user" })
        });
        showToast("已生成待确认记录。", "success");
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("生成待确认记录失败：" + error.message, "error");
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
