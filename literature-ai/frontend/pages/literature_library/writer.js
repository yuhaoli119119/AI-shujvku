async function ensureWriterStatus() {
    state.writerStatus = {
        backend_used: "disabled",
        llm_status: "disabled",
        llm_error: "网页端写作模型已停用；请在 IDE AI 中完成写作整理，优先走 MCP，若当前会话未暴露 MCP 工具可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。"
    };
    renderWriterStatus();
}

function renderWriterStatus() {
    if (!state.writerStatus) return;
    const box = $("writerStatusBox");
    if (box) {
        box.innerHTML =
            '<strong>网页端写作模型已停用</strong>' +
            '<span class="subtle" style="margin-left:8px;">' + esc(writerStatusText(state.writerStatus)) + '</span>';
    }
}

function writerStatusText(data) {
    if (!data) return "未返回";
    if (data.llm_status === "disabled") return data.llm_error || "请使用 IDE AI。";
    if (data.llm_status === "ok") return "AI 已完成生成";
    if (data.llm_status === "fallback") return "已使用规则兜底生成，建议检查 API 设置";
    if (data.llm_error) return "生成遇到问题：" + data.llm_error;
    return data.llm_status || "已返回";
}

function renderWriterStatusSummary(data) {
    const guardActions = data && data.guard_actions ? Object.keys(data.guard_actions).length : 0;
    const citationGuard = data && data.citation_guard ? Object.values(data.citation_guard).filter(Boolean).length : 0;
    return '<div class="writer-block"><h3>写作生成状态</h3>' +
        '<div class="readable-grid">' +
            '<div class="readable-field"><div class="k">生成方式</div><div class="v">' + esc(data && data.backend_used === "llm" ? "AI 生成" : "规则兜底") + '</div></div>' +
            '<div class="readable-field"><div class="k">状态</div><div class="v">' + esc(writerStatusText(data)) + '</div></div>' +
            '<div class="readable-field"><div class="k">引用检查</div><div class="v">' + esc(citationGuard ? "已检查引用证据" : "未发现可检查的引用证据") + '</div></div>' +
            '<div class="readable-field"><div class="k">安全修正</div><div class="v">' + esc(guardActions ? "已自动移除或替换缺少证据的句子" : "未触发自动修正") + '</div></div>' +
        '</div>' +
    '</div>';
}

async function generateWriterDraft() {
    showToast("网页端写作模型已停用，请在 IDE AI 中完成写作整理；优先走 MCP，若当前会话未暴露工具可改用仓库内 `literature-ai/backend` 的 `app.mcp.*`。", "info");
    if (typeof loadAgentGuide === "function") await loadAgentGuide();
}
