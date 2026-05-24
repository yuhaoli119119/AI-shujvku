async function ensureWriterStatus() {
    if (state.writerStatus) {
        renderWriterStatus();
        return;
    }
    try {
        state.writerStatus = await fetchJSON(WRITER_API + "/status");
        renderWriterStatus();
    } catch (error) {
        $("writerStatusBox").textContent = "写作器状态读取失败：" + error.message;
    }
}

function renderWriterStatus() {
    if (!state.writerStatus) return;
    $("writerStatusBox").innerHTML =
        "后端：<strong>" + esc(state.writerStatus.backend_used || "-") + "</strong> | " +
        "状态：<strong>" + esc(state.writerStatus.llm_status || "-") + "</strong> | " +
        (state.writerStatus.llm_error ? "错误：" + esc(state.writerStatus.llm_error) : "LLM 已就绪");
}

async function generateWriterDraft() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const topic = $("writerTopic").value.trim();
    if (!topic) {
        showToast("请输入写作主题。", "error");
        return;
    }
    showProgress("内部 AI 正在整理归纳...");
    try {
        const data = await fetchJSON(WRITER_API + "/draft", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                topic: topic,
                paper_ids: [state.selectedPaperId],
                user_notes: $("writerNotes").value.trim() || null,
                sections: ["outline", "introduction", "dft_results", "discussion", "figure_storyline"],
                limit_per_type: Number($("writerLimit").value || 5)
            })
        });
        $("writerResult").innerHTML =
            '<div class="writer-block"><h3>写作器返回状态</h3><div class="mono">' + esc(JSON.stringify({
                backend_used: data.backend_used,
                llm_status: data.llm_status,
                llm_error: data.llm_error,
                guard_actions: data.guard_actions,
                citation_guard: data.citation_guard
            }, null, 2)) + "</div></div>" +
            '<div class="section-card"><h3>提纲</h3><div class="prewrap">' + esc((data.outline || []).join("\n")) + "</div></div>" +
            '<div class="section-card"><h3>引言</h3><div class="prewrap">' + esc(data.introduction || "") + "</div></div>" +
            '<div class="section-card"><h3>DFT 结果整理</h3><div class="prewrap">' + esc(data.dft_results || "") + "</div></div>" +
            '<div class="section-card"><h3>讨论</h3><div class="prewrap">' + esc(data.discussion || "") + "</div></div>" +
            '<div class="section-card"><h3>图文叙事</h3><div class="prewrap">' + esc((data.figure_storyline || []).join("\n")) + "</div></div>" +
            '<div class="section-card"><h3>Prompt 预览</h3><div class="mono">' + esc(data.prompt_preview || "") + "</div></div>";
        showToast("内部 AI 整理完成。", "success");
    } catch (error) {
        $("writerResult").innerHTML = '<div class="workspace-empty">写作失败：' + esc(error.message) + "</div>";
        showToast("内部 AI 整理失败：" + error.message, "error");
    }
    hideProgress();
}
