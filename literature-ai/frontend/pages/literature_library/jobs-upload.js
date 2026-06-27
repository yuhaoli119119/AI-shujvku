async function downloadIdentifier(identifier) {
    if (!identifier) return;
    showProgress("正在创建后台收录任务...");
    try {
        const job = await fetchJSON(API_BASE + "/discovery/download/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ identifier: identifier, providers: [], library_name: getCurrentLibraryName() })
        });
        showToast("收录任务已进入后台队列。", "success");
        renderQueuedIngestJob(job);
        pollWorkflowIngestJob(job.job_id);
    } catch (error) {
        const detail = error.detail;
        if (detail && detail.status === "already_exists") {
            showToast("收录失败：该文献已存在", "error");
            showAlreadyExistsPrompt(detail.paper_id, detail.title || "已存在文献");
        } else {
            showToast("收录失败：" + error.message, "error");
        }
    }
    hideProgress();
}

function downloadByDOI() {
    const doiInput = $("doiInput");
    const identifier = doiInput ? doiInput.value.trim() : "";
    if (!identifier) {
        showToast("请输入 DOI 或 URL。", "error");
        return;
    }
    downloadIdentifier(identifier).then(function() {
        if (doiInput) doiInput.value = "";
    });
}

async function uploadPDF(input) {
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append("file", file);
    formData.append("library_name", getCurrentLibraryName());
    showProgress("正在上传并加入后台队列：" + file.name);
    try {
        const job = await fetchJSON(API_BASE + "/ingest/upload/jobs", {
            method: "POST",
            body: formData
        });
        showToast("上传成功，已进入后台解析队列。", "success");
        renderQueuedIngestJob(job);
        pollWorkflowIngestJob(job.job_id);
    } catch (error) {
        showToast("上传失败：" + error.message, "error");
    } finally {
        input.value = "";
        hideProgress();
    }
}

function triggerSupplementaryUpload() {
    closeDropdowns();
    const selectedPaper = getSelectedPaperForSupplementaryUpload();
    if (!selectedPaper || !state.selectedPaperId) {
        showToast("请先点击左侧主文献行，打开主文献详情后再上传 SI。", "error");
        return;
    }
    if (isSupplementaryPaperType(selectedPaper.paper_type)) {
        showToast("当前选中的是 SI，请选择它对应的主文献后再上传支撑文献。", "error");
        return;
    }
    const input = $("supplementaryPdfUpload");
    if (input) input.click();
}

async function uploadSupplementaryPDF(input) {
    if (!input.files || !input.files.length) return;
    const selectedPaper = getSelectedPaperForSupplementaryUpload();
    if (!selectedPaper || !state.selectedPaperId) {
        input.value = "";
        showToast("请先点击左侧主文献行，打开主文献详情后再上传 SI。", "error");
        return;
    }
    if (isSupplementaryPaperType(selectedPaper.paper_type)) {
        input.value = "";
        showToast("当前选中的是 SI，请选择它对应的主文献后再上传支撑文献。", "error");
        return;
    }
    const file = input.files[0];
    const mainPaperId = stablePaperIdOf(selectedPaper) || canonicalPaperId(state.selectedPaperId);
    if (!mainPaperId) {
        input.value = "";
        showToast("请先点击左侧主文献行，打开主文献详情后再上传 SI。", "error");
        return;
    }
    const formData = new FormData();
    formData.append("file", file);
    showProgress("正在上传支撑文献 / SI：" + file.name);
    try {
        const job = await fetchJSON(API_BASE + "/" + encodeURIComponent(mainPaperId) + "/supplementary/upload/jobs", {
            method: "POST",
            body: formData
        });
        showToast("SI 上传成功，已进入后台解析队列。", "success");
        renderQueuedIngestJob(job);
        pollWorkflowIngestJob(job.job_id, { mainPaperId: mainPaperId });
    } catch (error) {
        const detailText = typeof error.message === "string" ? error.message : "";
        if (detailText === "Paper not found" || detailText.indexOf("uuid") >= 0) {
            showToast("当前主文献引用已失效，请重新点击左侧主文献行，打开主文献详情后再上传 SI。", "error");
        } else {
            showToast("SI 上传失败：" + error.message, "error");
        }
    } finally {
        input.value = "";
        hideProgress();
    }
}

async function rerunExtraction() {
    if (!state.selectedPaperId) return;
    showProgress("正在刷新当前文献的 AI 解析材料...");
    try {
        const data = await fetchJSON(API_BASE + "/" + state.selectedPaperId + "/prepare-ai-context", { method: "POST" });
        showToast("AI 解析材料已刷新，可继续由 IDE-AI 接手。", "success");
        const summary = $("summaryContent");
        if (summary) {
            summary.insertAdjacentHTML("afterbegin",
                '<div class="section-card"><h3>最近一次 IDE AI 材料刷新结果</h3><div class="subtle">状态：' + esc(data.status || data.job_status || "已提交") + (data.external_ai_ready ? " | IDE AI 可继续接手" : " | 仍需补齐材料或人工检查") + (data.job_id ? " | 任务：" + esc(data.job_id) : "") + "</div></div>"
            );
        }
        await refreshSelectedPaperDetail({ reason: "prepare_ai_context" });
    } catch (error) {
        showToast("刷新 AI 解析材料失败：" + error.message, "error");
    }
    hideProgress();
}

async function reparseSelectedPaper() {
    if (!state.selectedPaperId) return;
    showProgress("正在基于当前 PDF 重新解析文献...");
    try {
        const data = await fetchJSON(API_BASE + "/" + state.selectedPaperId + "/reparse", { method: "POST" });
        showToast("重新解析完成，可重新检查章节、图表和 DFT 候选。", "success");
        const summary = $("summaryContent");
        if (summary) {
            summary.insertAdjacentHTML("afterbegin",
                '<div class="section-card"><h3>最近一次重新解析结果</h3><div class="subtle">状态：' + esc(data.status || "completed") + (data.workflow_status ? " | workflow=" + esc(data.workflow_status) : "") + (data.workspace_path ? " | workspace=" + esc(data.workspace_path) : "") + "</div></div>"
            );
        }
        await refreshSelectedPaperDetail({ reason: "reparse_completed", mode: "full" });
    } catch (error) {
        showToast("重新解析失败：" + error.message, "error");
    }
    hideProgress();
}

function showAlreadyExistsPrompt(paperId, title) {
    const existing = document.querySelector(".already-exists-toast");
    if (existing) existing.remove();

    const container = document.createElement("div");
    container.className = "toast error already-exists-toast";
    container.style.display = "flex";
    container.style.flexDirection = "column";
    container.style.gap = "8px";
    container.style.padding = "16px";
    container.style.maxWidth = "360px";
    container.style.background = "var(--color-surface)";
    container.style.border = "1px solid var(--color-danger)";
    container.style.color = "var(--color-text)";
    container.style.boxShadow = "var(--shadow-elevated)";
    container.style.position = "fixed";
    container.style.right = "18px";
    container.style.top = "18px";
    container.style.zIndex = "3100";

    container.innerHTML =
        '<div style="font-weight:700;color:var(--color-danger);font-size:14px;margin-bottom:2px;">⚠️ 文献已存在</div>' +
        '<div style="font-size:13px;color:var(--color-text-secondary);word-break:break-all;">' + esc(title) + '</div>' +
        '<div style="display:flex;gap:8px;margin-top:6px;justify-content:flex-end;">' +
            '<button class="btn primary small" id="jumpToPaperBtn" style="height:28px;padding:0 10px;font-size:12px;">跳转查看</button>' +
            '<button class="btn ghost small" id="closeExistsToastBtn" style="height:28px;padding:0 10px;font-size:12px;">关闭</button>' +
        '</div>';

    document.body.appendChild(container);

    container.querySelector("#jumpToPaperBtn").onclick = function(e) {
        e.preventDefault();
        e.stopPropagation();
        loadPaperDetail(paperId);
        container.remove();
    };
    container.querySelector("#closeExistsToastBtn").onclick = function(e) {
        e.preventDefault();
        e.stopPropagation();
        container.remove();
    };

    setTimeout(function() {
        if (container.parentNode) {
            container.style.opacity = "0";
            setTimeout(function() { container.remove(); }, 280);
        }
    }, 8000);
}

function showIdentityConfirmationPrompt(paperId, file, detail) {
    const existing = document.getElementById("identityConfirmModal");
    if (existing) existing.remove();

    const target = detail.target || {};
    const incoming = detail.incoming || {};
    const matchScore = detail.match_score;
    const matchScoreText = matchScore != null ? (Number(matchScore) * 100).toFixed(0) + "%" : "未知";
    const doiText = (doi) => (doi && doi.trim()) ? doi : "未识别";
    const yearText = (year) => (year != null && year !== "") ? year : "未识别";

    const container = document.createElement("div");
    container.id = "identityConfirmModal";
    container.className = "modal-overlay";
    container.style.cssText = "display: flex; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 3200; justify-content: center; align-items: center; padding: 20px;";

    container.innerHTML =
        '<div class="modal" style="max-width: 600px; width: 100%; background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-lg); padding: 24px; box-shadow: var(--shadow-elevated);">' +
            '<div class="modal-title-row" style="margin-bottom: 16px;">' +
                '<h3 style="margin: 0; color: var(--color-warning);">⚠️ 需要确认文献身份</h3>' +
            '</div>' +
            '<div style="margin-bottom: 18px; font-size: 14px; line-height: 1.5;">' +
                '<p style="margin-top: 0; color: var(--color-text-secondary);">系统认为这份 PDF 与当前 metadata-only 条目匹配置信度较低（匹配度：<strong style="color: var(--color-primary);">' + matchScoreText + '</strong>）。</p>' +
                '<p style="margin-bottom: 12px;"><strong>匹配原因：</strong> ' + esc(detail.match_reason || "未知") + '</p>' +

                '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; background: var(--color-surface-alt); padding: 12px; border-radius: var(--radius); border: 1px solid var(--color-border);">' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">当前条目信息 (Target)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(target.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> ' + esc(doiText(target.doi)) + '</div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(target.year)) + '</div>' +
                    '</div>' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">上传 PDF 信息 (Incoming)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(incoming.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> ' + esc(doiText(incoming.doi)) + '</div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(incoming.year)) + '</div>' +
                    '</div>' +
                '</div>' +

                '<div style="border-left: 4px solid var(--color-warning); background: var(--color-warning-bg); padding: 10px; border-radius: var(--radius); color: var(--color-warning); font-weight: bold; font-size: 13px;">' +
                    '风险提示：系统认为这份 PDF 与当前 metadata-only 条目匹配置信度较低。确认后会绑定到当前文献条目，并保留当前 paper_id。' +
                '</div>' +
            '</div>' +
            '<div class="modal-actions" style="display: flex; gap: 12px; justify-content: flex-end;">' +
                '<button class="btn ghost" id="confirmCancelBtn">取消</button>' +
                '<button class="btn primary" id="confirmAttachBtn">确认绑定</button>' +
            '</div>' +
        '</div>';

    document.body.appendChild(container);

    container.querySelector("#confirmCancelBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
    };
    container.querySelector("#confirmAttachBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
        attachPDFToPaperFile(paperId, file, true);
    };
}

function showIdentityMismatchPrompt(detail) {
    const existing = document.getElementById("identityMismatchModal");
    if (existing) existing.remove();

    const target = detail.target || {};
    const incoming = detail.incoming || {};
    const doiText = (doi) => (doi && doi.trim()) ? doi : "未识别";
    const yearText = (year) => (year != null && year !== "") ? year : "未识别";

    const container = document.createElement("div");
    container.id = "identityMismatchModal";
    container.className = "modal-overlay";
    container.style.cssText = "display: flex; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 3200; justify-content: center; align-items: center; padding: 20px;";

    container.innerHTML =
        '<div class="modal" style="max-width: 600px; width: 100%; background: var(--color-surface); border: 1px solid var(--color-border-strong); border-radius: var(--radius-lg); padding: 24px; box-shadow: var(--shadow-elevated);">' +
            '<div class="modal-title-row" style="margin-bottom: 16px;">' +
                '<h3 style="margin: 0; color: var(--color-danger);">❌ 文献身份冲突</h3>' +
            '</div>' +
            '<div style="margin-bottom: 18px; font-size: 14px; line-height: 1.5;">' +
                '<p style="margin-top: 0; color: var(--color-danger); font-weight: bold;">目标条目和上传 PDF 的 DOI 冲突，系统已阻止绑定。请检查是否上传错 PDF，或将 PDF 作为新文献导入。</p>' +
                '<p style="margin-bottom: 12px;"><strong>匹配原因：</strong> ' + esc(detail.match_reason || "未知") + '</p>' +

                '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; background: var(--color-surface-alt); padding: 12px; border-radius: var(--radius); border: 1px solid var(--color-border);">' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">当前条目信息 (Target)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(target.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> <strong style="color: var(--color-danger);">' + esc(doiText(target.doi)) + '</strong></div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(target.year)) + '</div>' +
                    '</div>' +
                    '<div>' +
                        '<h4 style="margin: 0 0 8px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border); padding-bottom: 4px;">上传 PDF 信息 (Incoming)</h4>' +
                        '<div style="margin-bottom: 6px; font-weight: 500;"><strong>标题:</strong> ' + esc(incoming.title || "未知") + '</div>' +
                        '<div style="margin-bottom: 6px;"><strong>DOI:</strong> <strong style="color: var(--color-danger);">' + esc(doiText(incoming.doi)) + '</strong></div>' +
                        '<div><strong>年份:</strong> ' + esc(yearText(incoming.year)) + '</div>' +
                    '</div>' +
                '</div>' +
            '</div>' +
            '<div class="modal-actions" style="display: flex; gap: 12px; justify-content: flex-end;">' +
                '<button class="btn ghost" id="mismatchCancelBtn">取消</button>' +
                '<button class="btn primary" id="mismatchUploadNewBtn">作为新文献上传</button>' +
            '</div>' +
        '</div>';

    document.body.appendChild(container);

    container.querySelector("#mismatchCancelBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
    };
    container.querySelector("#mismatchUploadNewBtn").onclick = function(e) {
        e.preventDefault();
        container.remove();
        closeAddLiteraturePanel();
        const pdfUpload = document.getElementById("pdfUpload");
        if (pdfUpload) pdfUpload.click();
    };
}

async function attachPDFToPaperFile(paperId, file, confirmIdentityMismatch) {
    if (!paperId || !file) return;
    const formData = new FormData();
    formData.append("file", file);
    formData.append("confirm_identity_mismatch", confirmIdentityMismatch ? "true" : "false");

    showProgress("正在上传并关联 PDF：" + file.name);
    let keepProgress = false;
    try {
        const data = await fetchJSON(API_BASE + "/" + paperId + "/attach-pdf/jobs", {
            method: "POST",
            body: formData
        });
        const jobId = data && data.job_id ? String(data.job_id).slice(0, 8) : "queued";
        if (confirmIdentityMismatch) {
            showToast("确认绑定任务已进入后台队列：" + jobId, "success");
        } else {
            showToast("PDF 关联任务已进入后台队列：" + jobId, "success");
        }
        renderQueuedIngestJob(data);
        pollWorkflowIngestJob(data.job_id, { paperId: paperId, file: file });
        state.selectedPaperId = paperId;
        closeAddLiteraturePanel();
    } catch (error) {
        const detail = error.detail;
        if (detail && typeof detail === "object") {
            if (detail.status === "needs_confirmation") {
                keepProgress = true;
                hideProgress();
                showIdentityConfirmationPrompt(paperId, file, detail);
                return;
            } else if (detail.status === "identity_mismatch") {
                keepProgress = true;
                hideProgress();
                showIdentityMismatchPrompt(detail);
                return;
            } else if (detail.status === "already_exists") {
                keepProgress = true;
                hideProgress();
                showToast("系统发现该文献已有 PDF，未覆盖已有文件。", "error");
                showAlreadyExistsPrompt(detail.target_paper_id || detail.paper_id, detail.target?.title || detail.incoming?.title || detail.title || "已存在文献");
                return;
            }
        }
        showToast("关联失败：" + error.message, "error");
    } finally {
        if (!keepProgress) {
            hideProgress();
        }
    }
}

function triggerAttachPDF() {
    const selectEl = $("attachPaperSelect");
    if (!selectEl || !selectEl.value) {
        showToast("请先选择一个元数据文献条目。", "error");
        return;
    }
    const fileInput = $("attachPdfInputModal");
    if (fileInput) fileInput.click();
}

async function uploadAttachPDFModal(input) {
    if (!input.files || !input.files.length) return;
    const selectEl = $("attachPaperSelect");
    if (!selectEl || !selectEl.value) {
        showToast("未选中目标文献条目。", "error");
        return;
    }
    const paperId = selectEl.value;
    const file = input.files[0];
    try {
        await attachPDFToPaperFile(paperId, file);
    } finally {
        input.value = "";
    }
}

async function attachPDFToPaperDetail(input, paperId) {
    if (!input.files || !input.files.length) return;
    const file = input.files[0];
    try {
        await attachPDFToPaperFile(paperId, file);
    } finally {
        input.value = "";
    }
}

async function loadMetadataOnlyPapers() {
    const selectEl = $("attachPaperSelect");
    if (!selectEl) return;
    selectEl.innerHTML = '<option value="">正在加载元数据条目...</option>';
    try {
        const params = new URLSearchParams();
        params.set("limit", 100);
        const libraryName = getCurrentLibraryName();
        if (libraryName) params.set("library_name", libraryName);

        const papers = await fetchJSON(API_BASE + "/?" + params.toString());
        const metaOnly = (papers || []).filter(function(p) { return p.oa_status === "metadata_only"; });

        if (metaOnly.length === 0) {
            selectEl.innerHTML = '<option value="">无待上传 PDF 的元数据条目</option>';
        } else {
            selectEl.innerHTML = '<option value="">-- 请选择文献 --</option>' +
                metaOnly.map(function(p) {
                    return '<option value="' + p.id + '">' + esc(p.title || "未命名文献") + '</option>';
                }).join("");
        }
    } catch (error) {
        selectEl.innerHTML = '<option value="">加载失败：' + esc(error.message) + '</option>';
    }
}
