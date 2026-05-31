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

function candidateTypeLabel(type) {
    if (type === "correction") return "修正建议";
    if (type === "note") return "阅读笔记";
    if (type === "relationship") return "文献关系";
    return "AI 审阅项";
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
    const candidates = data && Array.isArray(data.candidates) ? data.candidates : [];
    const pending = candidates.filter(function(item) {
        return item.status === "pending" || item.status === "requires_resolution";
    }).length;
    return '<div class="section-card"><h3>最近一次 AI 详细审阅</h3>' +
        '<div class="subtle">AI 会生成阅读笔记、修正建议和文献关联建议，但不会直接改库。需要在下方审阅记录里点“生成待确认记录”，再由人工核对证据并确认。</div>' +
        '<div class="readable-grid" style="margin-top:10px;">' +
            '<div class="readable-field"><div class="k">审阅项总数</div><div class="v">' + esc(candidates.length) + '</div></div>' +
            '<div class="readable-field"><div class="k">待处理</div><div class="v">' + esc(pending) + '</div></div>' +
            '<div class="readable-field"><div class="k">下一步</div><div class="v">展开审阅项，选择可信内容并生成待确认记录。</div></div>' +
        '</div>' +
        '<details class="debug-json"><summary>查看原始返回</summary><div class="mono">' + esc(JSON.stringify(data || {}, null, 2)) + '</div></details>' +
    '</div>';
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
    showProgress("网页内 AI 正在生成详细审阅，完成后可选择内容生成待确认记录。");
    let hideImmediately = false;
    try {
        const data = await fetchJSON(EXTERNAL_API + "/papers/" + state.selectedPaperId + "/internal-parse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_label: "网页内 AI 详细审阅",
                auto_apply: false
            })
        });
        const extRawText = $("externalRawText");
        if (extRawText) extRawText.value = "";
        showToast("网页内 AI 详细审阅已生成，请人工确认后再处理。", "success");
        const extRuns = $("externalRuns");
        if (extRuns) {
            extRuns.insertAdjacentHTML(
                "afterbegin",
                renderInternalParseSummary(data)
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
                extRuns.innerHTML = '<div class="workspace-empty">网页内 AI 详细审阅生成失败：' + esc(error.message) + "</div>";
            }
            showToast("网页内 AI 详细审阅生成失败：" + error.message, "error");
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
            const entry = guide.recommended_entrypoint || {};
            const endpoints = Array.isArray(guide.http_endpoints) ? guide.http_endpoints : [];
            const tools = guide.mcp && Array.isArray(guide.mcp.common_tools) ? guide.mcp.common_tools : [];
            mcpGuide.innerHTML =
                '<div class="section-card"><h3>IDE / MCP AI 审阅指南</h3>' +
                '<div class="subtle">外部 IDE / MCP AI 可以读取文献、追加笔记、提出修正或触发解析；本区只展示入口，不会自动写入正式数据。</div>' +
                '<div class="readable-grid" style="margin-top:10px;">' +
                    '<div class="readable-field"><div class="k">推荐入口</div><div class="v">' + esc((entry.method || "") + " " + (entry.path || "")) + '</div></div>' +
                    '<div class="readable-field"><div class="k">适用场景</div><div class="v">' + esc(entry.description || "通过外部工具读取和审阅文献。") + '</div></div>' +
                    '<div class="readable-field"><div class="k">MCP 地址</div><div class="v">' + esc((guide.mcp && guide.mcp.url) || "/mcp") + '</div></div>' +
                    '<div class="readable-field"><div class="k">常用工具</div><div class="v">' + esc(tools.join("、") || "-") + '</div></div>' +
                '</div>' +
                '<details class="debug-json"><summary>查看接口清单</summary>' +
                    endpoints.map(function(item) {
                        return '<div class="readable-field" style="margin-top:8px;"><div class="k">' + esc(item.name || item.path || "接口") + '</div><div class="v">' + esc((item.method || "") + " " + (item.path || "")) + '<br>' + esc(item.purpose || "") + '</div></div>';
                    }).join("") +
                '</details></div>';
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
    showProgress("正在导入外部 AI 审阅结果...");
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
                source_label: extSourceLabel ? (extSourceLabel.value.trim() || "外部 AI 审阅结果") : "外部 AI 审阅结果",
                raw_text: typeof rawPayload === "string" ? rawPayload : null,
                raw_payload: rawPayload
            })
        });
        showToast("外部 AI 审阅结果已导入。", "success");
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
    const reasonBanner = state.qualityReasonContext
        ? '<div class="section-card" style="border-color:var(--color-warning);"><h3>DFT 质量处理入口</h3><div class="subtle">来自 blocked reason：' + esc(state.qualityReasonContext) + '。请核对本论文的 review 状态、证据链和定位信息。</div></div>'
        : "";
    const contextGuide = $("internalAIConfigGuide");
    if (reasonBanner && contextGuide && contextGuide.getAttribute("data-quality-reason") !== state.qualityReasonContext) {
        contextGuide.setAttribute("data-quality-reason", state.qualityReasonContext);
        contextGuide.insertAdjacentHTML("afterbegin", reasonBanner);
    }
    if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">正在加载 AI 审阅记录...</div>';
    try {
        const runs = await fetchJSON(EXTERNAL_API + "/runs?paper_id=" + encodeURIComponent(state.selectedPaperId));
        state.externalRuns = runs || [];
        if (!state.externalRuns.length) {
            if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">当前文献还没有 AI 审阅记录。点“生成详细审阅”后，AI 会先产出阅读笔记、修正建议和关联建议；确认前不会写入正式数据。</div>';
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
                        '<div class="subtle" style="margin-top:8px;">用途：这里是 AI 的详细审阅草稿。阅读笔记用于快速理解论文；修正/关联建议用于补全或纠错。点击“生成待确认记录”后，还需要人工在 review 流程里确认，才算可靠数据。</div>' +
                        (run.mapping_error ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">错误：' + esc(run.mapping_error) + "</div>" : "") +
                        (run.raw_text ? '<div class="mono" style="margin-top:10px;">' + esc(ellipsis(run.raw_text, 1200)) + "</div>" : "") +
                        '<div class="candidate-toolbar" style="margin-top:12px;">' +
                            '<button class="btn blue small" onclick="materializeRun(\'' + run.id + '\')">批量生成待确认记录</button>' +
                            '<button class="btn ghost small" onclick="materializeSelectedCandidates(\'' + run.id + '\')">选中生成待确认记录</button>' +
                            '<button class="btn ghost small" onclick="toggleRunCandidates(\'' + run.id + '\')">展开审阅项（' + (run.candidates || []).length + "）</button>" +
                            '<a class="btn ghost small" style="text-decoration:none;" href="/pages/external_analysis_workbench/index.html?paper_id=' + encodeURIComponent(state.selectedPaperId) + '">人工确认工作台</a>' +
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
        if (extRuns) extRuns.innerHTML = '<div class="workspace-empty">AI 审阅记录加载失败：' + esc(error.message) + "</div>";
    }
}

function renderCandidates(runId, candidates) {
    if (!candidates.length) {
        return '<div class="candidate-card"><div class="muted">没有审阅项。</div></div>';
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
                    '<h4>' + esc(candidateTypeLabel(item.candidate_type)) + candidateLabel + " | 状态：" + esc(uiLabel("candidate_status", item.status || "-")) + "</h4>" +
                    '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' + checkbox + singleAction + "</div>" +
                "</div>" +
                '<div class="subtle">置信度：' + esc(item.confidence == null ? "-" : item.confidence) + " | 目标类型：" + esc(item.materialized_target_type || "-") + "</div>" +
                renderCandidatePayload(item.normalized_payload || {}) +
                '<details class="debug-json"><summary>查看原始候选数据</summary><div class="mono">' + esc(JSON.stringify(item.normalized_payload || {}, null, 2)) + "</div></details>" +
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
        "将处理 " + pendingCount + " 个 AI 审阅项，生成待确认记录。\n\n" +
        "这不是人工 verified，只是把 AI 草稿送进人工确认流程；请去“人工确认工作台”核对证据并确认。\n\n" +
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
        showToast("已生成待确认记录，请在人工确认工作台核对证据。", "success");
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
        "将处理 " + candidateIds.length + " 个 AI 审阅项，生成待确认记录。\n\n" +
        "这不是人工 verified，只是把 AI 草稿送进人工确认流程；请去“人工确认工作台”核对证据并确认。\n\n" +
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
        showToast("已生成待确认记录，请在人工确认工作台核对证据。", "success");
        await loadExternalRuns();
        await loadPaperDetail(state.selectedPaperId);
    } catch (error) {
        showToast("生成待确认记录失败：" + error.message, "error");
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
        return '<div class="readable-field"><div class="k">' + esc(item.name || item.alias || "别名") + '</div><div class="v">' + esc(item.reason || item.note || JSON.stringify(item)) + '</div></div>';
    }).join("") + '</div></div>';
}

async function loadAggregate() {
    const aggResult = $("aggregateResult");
    if (aggResult) aggResult.innerHTML = '<div class="workspace-empty">正在加载聚合视图...</div>';
    try {
        state.aggregateData = await fetchJSON(API_BASE + "/aggregate");
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
