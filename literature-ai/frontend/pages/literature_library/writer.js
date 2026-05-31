async function ensureWriterStatus() {
    if (state.writerStatus) {
        renderWriterStatus();
        return;
    }
    try {
        state.writerStatus = await fetchJSON(WRITER_API + "/status");
        renderWriterStatus();
    } catch (error) {
        const box = $("writerStatusBox");
        if (box) box.textContent = "写作器状态读取失败：" + error.message;
    }
}

function renderWriterStatus() {
    if (!state.writerStatus) return;
    const box = $("writerStatusBox");
    if (box) {
        box.innerHTML =
            '<strong>' + esc(state.writerStatus.llm_error ? "写作服务需要检查" : "写作服务已就绪") + '</strong>' +
            '<span class="subtle" style="margin-left:8px;">' + esc(writerStatusText(state.writerStatus)) + '</span>';
    }
}

function writerStatusText(data) {
    if (!data) return "未返回";
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
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const topicEl = $("writerTopic");
    const topic = topicEl ? topicEl.value.trim() : "";
    if (!topic) {
        showToast("请输入写作主题。", "error");
        return;
    }
    showProgress("内部 AI 正在整理归纳...");
    try {
        const notesEl = $("writerNotes");
        const limitEl = $("writerLimit");
        const data = await fetchJSON(WRITER_API + "/draft", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                topic: topic,
                paper_ids: [state.selectedPaperId],
                user_notes: notesEl ? notesEl.value.trim() || null : null,
                sections: ["outline", "introduction", "dft_results", "discussion", "figure_storyline"],
                limit_per_type: Number(limitEl ? limitEl.value || 5 : 5)
            })
        });
        const resultEl = $("writerResult");
        if (resultEl) {
            resultEl.innerHTML =
                renderWriterStatusSummary(data) +
                '<div class="section-card"><h3>提纲</h3><div class="prewrap">' + esc((data.outline || []).join("\n")) + "</div></div>" +
                '<div class="section-card"><h3>引言</h3><div class="prewrap">' + esc(data.introduction || "") + "</div></div>" +
                '<div class="section-card"><h3>DFT 结果整理</h3><div class="prewrap">' + esc(data.dft_results || "") + "</div></div>" +
                '<div class="section-card"><h3>讨论</h3><div class="prewrap">' + esc(data.discussion || "") + "</div></div>" +
                '<div class="section-card"><h3>图文叙事</h3><div class="prewrap">' + esc((data.figure_storyline || []).join("\n")) + "</div></div>";
        }
        showToast("内部 AI 整理完成。", "success");
    } catch (error) {
        const resultEl = $("writerResult");
        if (resultEl) resultEl.innerHTML = '<div class="workspace-empty">写作失败：' + esc(error.message) + "</div>";
        showToast("内部 AI 整理失败：" + error.message, "error");
    }
    hideProgress();
}
