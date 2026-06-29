// Audit logs, notes, DFT review actions, and figure maintenance actions.
function renderAiAuditTrail(items) {
    const aiItems = (items || []).filter(function(item) {
        const payload = item.review_payload || {};
        return payload.latest_ai_audit || (payload.ai_audits && payload.ai_audits.length);
    }).slice(0, 12);
    if (!aiItems.length) {
        return "";
    }
    return (
        '<div id="aiAuditTrailPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;">' +
            '<h3>AI 审核建议记录</h3>' +
            '<div class="subtle">这里显示 AI / GLM / 外部审核提交写入的审核意见。AI 结论不会直接进入可信数据库；冲突项会标记为 review_conflict 并要求人工确认。</div>' +
            '<div style="display:grid;gap:10px;margin-top:12px;">' +
                aiItems.map(renderAiAuditTrailItem).join("") +
            '</div>' +
        '</div>'
    );
}

function taskLogActorLabel(value) {
    const normalized = compactText(value).toLowerCase();
    if (!normalized) return "system";
    if (normalized === "ide_ai") return "IDE AI";
    return compactText(value);
}

function taskLogTargetLabel(targetType, targetId, fieldName) {
    const bits = [compactText(targetType), compactText(fieldName)];
    const base = bits.filter(Boolean).join(" / ");
    if (!compactText(targetId)) return base || "paper";
    return (base || "target") + " / " + String(targetId).slice(0, 8);
}

function collectTaskLogEntries(detail, audit) {
    const entries = [];
    const seen = new Set();
    const pushEntry = function(entry) {
        if (!entry) return;
        const key = [
            entry.time || "",
            entry.actor || "",
            entry.action || "",
            entry.target || "",
            entry.detail || ""
        ].join("|");
        if (seen.has(key)) return;
        seen.add(key);
        entries.push(entry);
    };

    (state.externalRuns || []).forEach(function(run) {
        const candidates = Array.isArray(run && run.candidates) ? run.candidates : [];
        const counts = {};
        candidates.forEach(function(item) {
            const type = compactText(item && item.candidate_type) || "candidate";
            counts[type] = (counts[type] || 0) + 1;
        });
        const summary = Object.keys(counts).sort().map(function(type) {
            return type + " x" + counts[type];
        }).join(", ");
        pushEntry({
            time: run.created_at || null,
            actor: taskLogActorLabel(run.source_label || run.source || "ide_ai"),
            action: "import_analysis 导入",
            target: "paper",
            detail: summary || "no candidates",
            tone: "meta"
        });
    });

    (detail.paper_notes || []).forEach(function(note) {
        pushEntry({
            time: note.created_at || null,
            actor: taskLogActorLabel(note.source || "ide_ai"),
            action: "写入 AI 笔记",
            target: taskLogTargetLabel("paper_note", "", note.field_name || "overall"),
            detail: clipText(note.content || "", 180),
            tone: "meta"
        });
    });

    [
        ["figure", detail.figures || []],
        ["dft_result", detail.dft_results_items || []],
        ["writing_card", detail.writing_cards_items || []],
        ["mechanism_claim", detail.mechanism_claims_items || []],
        ["table", detail.tables || []]
    ].forEach(function(group) {
        const targetType = group[0];
        (group[1] || []).forEach(function(item) {
            (item.object_review_audits || []).slice(0, 3).forEach(function(auditItem) {
                const decision = compactText(auditItem.decision || auditItem.verification_status || "reviewed");
                pushEntry({
                    time: auditItem.created_at || auditItem.reviewed_at || null,
                    actor: taskLogActorLabel(auditItem.source_label || auditItem.source || auditItem.reviewer || "ide_ai"),
                    action: "对象审核",
                    target: taskLogTargetLabel(targetType, item.id, auditItem.field_name || item.figure_label || item.claim_type || ""),
                    detail: decision,
                    tone: /reject|conflict|need|repair|block/i.test(decision) ? "danger" : "ok"
                });
            });
        });
    });

    ((audit && audit.items) || []).forEach(function(item) {
        const payload = item.review_payload || {};
        const latest = payload.latest_ai_audit || ((payload.ai_audits || []).slice(-1)[0]) || {};
        if (!latest || (!latest.reviewer && !latest.model_name && !latest.decision)) return;
        const decision = compactText(latest.decision || item.reviewer_status || "reviewed");
        pushEntry({
            time: latest.created_at || item.reviewed_at || item.updated_at || item.created_at || null,
            actor: taskLogActorLabel(latest.reviewer || item.reviewer || "ide_ai"),
            action: "字段审核",
            target: taskLogTargetLabel(item.target_type, item.target_id, item.field_name),
            detail: decision,
            tone: /reject|conflict/i.test(decision) ? "danger" : "ok"
        });
    });

    return entries.sort(function(a, b) {
        const left = a.time ? Date.parse(a.time) : 0;
        const right = b.time ? Date.parse(b.time) : 0;
        return right - left;
    }).slice(0, 40);
}

function renderTaskLogPanel(detail, audit) {
    const entries = collectTaskLogEntries(detail || {}, audit || null);
    if (!entries.length) {
        return '<div id="taskLogPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;"><h3>任务日志</h3><div class="muted">当前还没有可展示的任务日志。</div></div>';
    }
    return (
        '<div id="taskLogPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;">' +
            '<h3>任务日志</h3>' +
            '<div class="subtle">这里只按时间记录 AI 何时导入、审核、写回了什么。</div>' +
            '<div style="display:grid;gap:10px;margin-top:12px;">' +
                entries.map(function(entry) {
                    return '<div class="candidate-card">' +
                        '<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap;">' +
                            '<strong>' + esc(entry.actor || "system") + '</strong>' +
                            '<span class="status-chip ' + escAttr(entry.tone || "meta") + '">' + esc(entry.time ? formatDate(entry.time) : "time unknown") + '</span>' +
                        '</div>' +
                        '<div style="margin-top:6px;">' + esc(entry.action || "action") + ' | ' + esc(entry.target || "paper") + '</div>' +
                        (entry.detail ? '<div class="subtle" style="margin-top:6px;">' + esc(entry.detail) + '</div>' : '') +
                    '</div>';
                }).join("") +
            '</div>' +
        '</div>'
    );
}

function renderPaperNotesPanel(notes) {
    const rows = (notes || []).slice(0, 12);
    if (!rows.length) return "";
    return (
        '<div id="paperNotesPanel" class="section-card" style="border:1px solid var(--color-border);margin-bottom:16px;">' +
            '<h3>IDE AI 回写笔记</h3>' +
            '<div class="subtle">这里显示 import_analysis 写入的 review_notes。它表示 AI 已经给出审核意见，但不等于 DFT 数据已自动确认入库。</div>' +
            '<div style="display:grid;gap:10px;margin-top:12px;">' +
                rows.map(renderPaperNoteItem).join("") +
            '</div>' +
        '</div>'
    );
}

function renderPaperNoteItem(note) {
    const meta = [
        note.source || "ide_ai",
        note.field_name || "overall",
        note.page ? ("p." + note.page) : "",
        note.created_at ? formatDate(note.created_at) : ""
    ].filter(Boolean).join(" / ");
    return (
        '<div class="candidate-card">' +
            '<div class="subtle">' + esc(meta) + '</div>' +
            '<div style="margin-top:8px;">' + esc(note.content || "") + '</div>' +
            (note.quoted_text ? '<div class="mono" style="margin-top:8px;">' + esc(note.quoted_text) + '</div>' : '') +
        '</div>'
    );
}

function loadFigureCardImage(container) {
    if (!container || container.getAttribute("data-loaded") === "1") return;
    const src = container.getAttribute("data-figure-src");
    if (!src) return;
    container.setAttribute("data-loaded", "1");
    container.innerHTML =
        '<img src="' + escAttr(src) + '" loading="lazy" decoding="async" ' +
        'style="max-width:100%;max-height:400px;border:1px solid var(--color-border);border-radius:var(--radius-sm);object-fit:contain;" ' +
        'alt="提取的文献图片" />';
}

function renderAiAuditTrailItem(item) {
    const payload = item.review_payload || {};
    const audits = payload.ai_audits || [];
    const latest = payload.latest_ai_audit || audits[audits.length - 1] || {};
    const protocol = latest.protocol || {};
    const conflict = payload.review_conflict || item.reviewer_status === "review_conflict";
    const hash = protocol.sha256 ? String(protocol.sha256).slice(0, 12) : "-";
    return (
        '<div class="candidate-card" style="' + (conflict ? 'border-color:var(--color-danger);' : '') + '">' +
            '<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;">' +
                '<strong>' + esc(item.target_type || "-") + " / " + esc(item.field_name || "-") + '</strong>' +
                '<span class="badge ' + (conflict ? 'danger' : 'ok') + '">' + esc(item.reviewer_status || "-") + '</span>' +
            '</div>' +
            '<div class="subtle" style="margin-top:6px;">AI：' + esc(latest.reviewer || item.reviewer || "-") +
                ' ｜ 模型：' + esc(latest.model_name || "-") +
                ' ｜ 角色：' + esc(latest.agent_role || "-") +
                ' ｜ 决策：' + esc(latest.decision || "-") +
            '</div>' +
            '<div class="subtle">协议：' + esc(protocol.version || protocol.key || "-") + ' ｜ hash：' + esc(hash) + '</div>' +
            (item.reviewer_note ? '<div style="margin-top:8px;">' + esc(item.reviewer_note) + '</div>' : '') +
            (conflict ? '<div class="subtle" style="margin-top:8px;color:var(--color-danger);">AI 结论冲突：该条必须人工确认后才能进入可信数据。</div>' : '') +
        '</div>'
    );
}

async function verifyDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法核验。", "error");
        return;
    }
    const ok = window.confirm("请确认你已经按 AI 解析协议，对照 PDF 原文、证据文本、页码/章节/表格/图号和重复项检查过这条 DFT 候选。确认后，该记录才会尝试进入可导出安全门。");
    if (!ok) return;
    try {
        showToast("正在写入 DFT 核验记录...", "info");
        const data = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(itemId) + "/verify",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_reviewed_against_pdf: true,
                    reviewer: "user_codex_review",
                    reviewer_note: "Verified from the Literature Library DFT panel after checking Codex item context and evidence."
                })
            }
        );
        const safety = data && data.export_safety;
        showToast(
            safety && safety.is_exportable
                ? "DFT 候选已审核可导出。"
                : "DFT 核验已记录，但仍有安全门阻断项。",
            safety && safety.is_exportable ? "success" : "info"
        );
        await refreshSelectedPaperDetail({ reason: "verify_dft_result", mode: "full" });
    } catch (error) {
        showToast("DFT 核验失败：" + error.message, "error");
    }
}

var activeDftEditItemId = null;

function dftEditValue(value) {
    return value === null || value === undefined ? "" : String(value);
}

function dftEditComparable(value, numeric) {
    if (value === null || value === undefined || value === "") return null;
    return numeric ? Number(value) : String(value).trim();
}

function populateDftEditCatalystSamples(item) {
    const select = document.getElementById("dftEditCatalystSample");
    if (!select) return;
    const samples = state.selectedPaper && Array.isArray(state.selectedPaper.catalyst_samples_items)
        ? state.selectedPaper.catalyst_samples_items
        : [];
    const currentId = dftEditValue(item && item.catalyst_sample_id);
    select.innerHTML = '<option value="">未关联催化剂样本</option>' + samples.map(function(sample) {
        const sampleId = String(sample.id || "");
        const label = sample.name || sample.catalyst || sample.material_identity || sampleId;
        return '<option value="' + escAttr(sampleId) + '">' + esc(label) + '</option>';
    }).join("");
    select.value = currentId;
    if (currentId && select.value !== currentId) {
        select.insertAdjacentHTML("beforeend", '<option value="' + escAttr(currentId) + '">' + esc(currentId) + '</option>');
        select.value = currentId;
    }
}

function dftEditDisplayNumber(itemId) {
    if (!state.selectedPaper || !itemId) return "";
    const targetId = String(itemId);
    const items = dftResultsWithSafety(state.selectedPaper);
    for (var i = 0; i < items.length; i += 1) {
        if (dftResultId(items[i]) === targetId) return String(i + 1);
    }
    return "";
}

function dftEditField(label, controlHtml, className) {
    return '<label class="' + escAttr(className || "") + '"><span>' + esc(label) + '</span>' + controlHtml + '</label>';
}

function renderDftEditDialogBody(item) {
    const fields = [
        dftEditField("关联催化剂样本", '<select id="dftEditCatalystSample"></select>'),
        dftEditField("吸附物", '<input id="dftEditAdsorbate" type="text" value="' + escAttr(dftEditValue(item.adsorbate)) + '">'),
        dftEditField("性质/能量类型", '<input id="dftEditPropertyType" type="text" value="' + escAttr(dftEditValue(item.property_type || item.energy_type)) + '">'),
        dftEditField("数值", '<input id="dftEditValue" type="number" step="any" value="' + escAttr(dftEditValue(item.value)) + '">'),
        dftEditField("单位", '<input id="dftEditUnit" type="text" value="' + escAttr(dftEditValue(item.unit)) + '">'),
        dftEditField("置信度", '<input id="dftEditConfidence" type="number" min="0" max="1" step="0.01" value="' + escAttr(dftEditValue(item.confidence)) + '">'),
        dftEditField("反应步骤", '<input id="dftEditReactionStep" type="text" value="' + escAttr(dftEditValue(item.reaction_step)) + '">', "dft-edit-wide"),
        dftEditField("来源章节", '<input id="dftEditSourceSection" type="text" value="' + escAttr(dftEditValue(item.source_section)) + '">'),
        dftEditField("来源图/表", '<input id="dftEditSourceFigure" type="text" value="' + escAttr(dftEditValue(item.source_figure)) + '">'),
        dftEditField("证据原文", '<textarea id="dftEditEvidenceText" rows="4">' + esc(dftEditValue(item.evidence_text)) + '</textarea>', "dft-edit-wide"),
        dftEditField("修改原因", '<textarea id="dftEditReason" rows="3" placeholder="例如：对照原 PDF 表 2 后确认数值应为 -1.25 eV"></textarea>', "dft-edit-wide")
    ].join("");
    return '<div class="dft-detail-edit-grid dft-edit-grid">' + fields + '</div>' +
        '<div class="dft-edit-note">保存会写入人工修正审计，并将这条数据退回待核验；需要再次“接受入库”后才能恢复可导出状态。</div>' +
        '<div class="modal-actions">' +
            '<button class="btn ghost" type="button" onclick="closeDftEditDialog()">取消编辑</button>' +
            '<button id="dftEditSubmit" class="btn primary" type="button" onclick="submitDftEdit()">保存修改</button>' +
        '</div>';
}

function openDftEditDialog(itemId) {
    const item = selectedDftItemById(itemId);
    if (!item) {
        showToast("未找到要修改的 DFT 数据。", "error");
        return;
    }
    activeDftEditItemId = String(itemId);
    const dialog = document.getElementById("dftDetailDialog");
    const title = document.getElementById("dftDetailTitle");
    const locator = document.getElementById("dftDetailLocator");
    const body = document.getElementById("dftDetailBody");
    if (!dialog || !title || !locator || !body) return;
    const displayNumber = dftEditDisplayNumber(activeDftEditItemId);
    title.textContent = displayNumber ? ("修改 DFT #" + displayNumber + " 数据") : "修改 DFT 数据";
    locator.textContent = "完整 ID 已隐藏，可在卡片表头使用“复制 ID”。";
    body.innerHTML = renderDftEditDialogBody(item);
    populateDftEditCatalystSamples(item);
    dialog.style.display = "flex";
}

function closeDftEditDialog() {
    const item = selectedDftItemById(activeDftEditItemId);
    const title = document.getElementById("dftDetailTitle");
    const locator = document.getElementById("dftDetailLocator");
    const body = document.getElementById("dftDetailBody");
    if (item && title && locator && body && typeof renderDftDetailDialogBody === "function") {
        const displayNumber = dftEditDisplayNumber(activeDftEditItemId);
        title.textContent = displayNumber ? ("DFT #" + displayNumber + " 数据详情") : "DFT 数据详情";
        locator.textContent = "完整 ID 已隐藏，可在卡片表头使用“复制 ID”。";
        body.innerHTML = renderDftDetailDialogBody(item);
        activeDftEditItemId = null;
        return;
    }
    if (typeof closeDftDetailDialog === "function") closeDftDetailDialog();
    activeDftEditItemId = null;
}

async function submitDftEdit() {
    const item = selectedDftItemById(activeDftEditItemId);
    if (!item || !state.selectedPaperId) {
        showToast("当前 DFT 数据已失效，请刷新后重试。", "error");
        return;
    }
    const reason = document.getElementById("dftEditReason").value.trim();
    if (!reason) {
        showToast("请填写修改原因。", "error");
        return;
    }
    const submitted = {
        catalyst_sample_id: dftEditComparable(document.getElementById("dftEditCatalystSample").value, false),
        adsorbate: dftEditComparable(document.getElementById("dftEditAdsorbate").value, false),
        property_type: dftEditComparable(document.getElementById("dftEditPropertyType").value, false),
        value: dftEditComparable(document.getElementById("dftEditValue").value, true),
        unit: dftEditComparable(document.getElementById("dftEditUnit").value, false),
        confidence: dftEditComparable(document.getElementById("dftEditConfidence").value, true),
        reaction_step: dftEditComparable(document.getElementById("dftEditReactionStep").value, false),
        source_section: dftEditComparable(document.getElementById("dftEditSourceSection").value, false),
        source_figure: dftEditComparable(document.getElementById("dftEditSourceFigure").value, false),
        evidence_text: dftEditComparable(document.getElementById("dftEditEvidenceText").value, false)
    };
    const current = {
        catalyst_sample_id: dftEditComparable(item.catalyst_sample_id, false),
        adsorbate: dftEditComparable(item.adsorbate, false),
        property_type: dftEditComparable(item.property_type || item.energy_type, false),
        value: dftEditComparable(item.value, true),
        unit: dftEditComparable(item.unit, false),
        confidence: dftEditComparable(item.confidence, true),
        reaction_step: dftEditComparable(item.reaction_step, false),
        source_section: dftEditComparable(item.source_section, false),
        source_figure: dftEditComparable(item.source_figure, false),
        evidence_text: dftEditComparable(item.evidence_text, false)
    };
    const updates = {};
    Object.keys(submitted).forEach(function(field) {
        if (submitted[field] !== current[field]) updates[field] = submitted[field];
    });
    if (!Object.keys(updates).length) {
        showToast("没有检测到字段变化。", "info");
        return;
    }
    const submitButton = document.getElementById("dftEditSubmit");
    if (submitButton) submitButton.disabled = true;
    try {
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(activeDftEditItemId),
            {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_manual_update: true,
                    updates: updates,
                    reason: reason,
                    reviewer: "literature_library_user",
                    evidence_payload: item.evidence_payload || {
                        section: item.source_section || null,
                        figure: item.source_figure || null,
                        quoted_text: item.evidence_text || null
                    }
                })
            }
        );
        if (typeof closeDftDetailDialog === "function") closeDftDetailDialog();
        activeDftEditItemId = null;
        showToast("DFT 数据已修改，并已退回待核验。", "success");
        await refreshSelectedPaperDetail({ reason: "manual_update_dft_result", mode: "full" });
    } catch (error) {
        showToast("DFT 数据修改失败：" + error.message, "error");
    } finally {
        if (submitButton) submitButton.disabled = false;
    }
}

async function acceptDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法接受入库。", "error");
        return;
    }
    const item = selectedDftItemById(itemId);
    const opinionMeta = dftAiOpinionMeta(item);
    if (opinionMeta && opinionMeta.label === "AI 冲突") {
        showToast("这条 DFT 存在 AI 冲突，不能直接接受入库；请展开查看后人工拒绝或重新处理。", "error");
        return;
    }
    const opinions = importedDftAcceptanceOpinions(item);
    const ok = window.confirm(opinions.length
        ? "确认应用这条 DFT 的 AI 修正意见，并接受入库吗？"
        : "确认接受这条 DFT 数据并入库吗？");
    if (!ok) return;
    try {
        showToast(opinions.length ? "正在应用 AI 修正并接受入库..." : "正在写入 DFT 接受结果...", "info");
        let data;
        if (opinions.length) {
            for (var i = 0; i < opinions.length; i += 1) {
                data = await fetchJSON(
                    API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
                    "/dft-results/" + encodeURIComponent(itemId) + "/apply-imported-opinion",
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            reviewer: "literature_library_dft",
                            opinion: opinions[i]
                        })
                    }
                );
            }
        } else {
            data = await fetchJSON(
                API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
                "/dft-results/" + encodeURIComponent(itemId) + "/verify",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        confirm_reviewed_against_pdf: true,
                        reviewer: "literature_library_dft",
                        reviewer_note: "Accepted from the Literature Library DFT panel."
                    })
                }
            );
        }
        const safety = (data && data.review_result && data.review_result.export_safety) || (data && data.export_safety) || {};
        showToast(
            safety && safety.is_exportable
                ? "这条 DFT 已应用 AI 修正并入库。"
                : "已应用接受结果，但这条 DFT 还有阻断项。",
            safety && safety.is_exportable ? "success" : "info"
        );
        if (typeof closeDftDetailDialog === "function") closeDftDetailDialog();
        await refreshSelectedPaperDetail({ reason: "accept_dft_result", mode: "full" });
    } catch (error) {
        showToast("接受入库失败：" + error.message, "error");
    }
}

async function rejectDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法拒绝。", "error");
        return;
    }
    const ok = window.confirm("确认拒绝这条 DFT 数据吗？");
    if (!ok) return;
    try {
        showToast("正在写入 DFT 拒绝结果...", "info");
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(itemId) + "/reject",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_reject_candidate: true,
                    reviewer: "literature_library_dft",
                    reviewer_note: "Rejected from the Literature Library DFT panel."
                })
            }
        );
        const detailItems = state.selectedPaper && Array.isArray(state.selectedPaper.dft_results_items)
            ? state.selectedPaper.dft_results_items
            : [];
        detailItems.forEach(function(item) {
            if (dftResultId(item) !== String(itemId)) return;
            item.candidate_status = "Rejected";
            item.dft_workflow_state = "rejected";
            item.dft_workflow_label = "已拒绝";
            item.dft_workflow_reason = "这条 DFT 已被人工拒绝，当前为终态。";
            item.next_required_action = "none";
            if (item.export_safety) item.export_safety.review_status = "rejected";
        });
        rerenderSelectedDetail(state.selectedPaperId);
        showToast("这条 DFT 已拒绝。", "success");
        if (typeof closeDftDetailDialog === "function") closeDftDetailDialog();
        await refreshSelectedPaperDetail({ reason: "reject_dft_result", mode: "full" });
    } catch (error) {
        showToast("拒绝失败：" + error.message, "error");
    }
}

async function revokeDftResult(itemId) {
    if (!state.selectedPaperId || !itemId) {
        showToast("当前 DFT 记录无法取消入库。", "error");
        return;
    }
    const item = selectedDftItemById(itemId);
    const resultId = dftResultId(item) || String(itemId);
    const ok = window.confirm("确认把这条 DFT 从已入库退回待处理吗？");
    if (!ok) return;
    try {
        showToast("正在取消这条 DFT 的入库状态...", "info");
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/dft-results/" + encodeURIComponent(resultId) + "/revoke-review",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    reviewer: "literature_library_dft",
                    reviewer_note: "Revoked from the Literature Library DFT panel.",
                    field_names: []
                })
            }
        );
        showToast("这条 DFT 已退回待处理。", "success");
        await refreshSelectedPaperDetail({ reason: "revoke_dft_result", mode: "full" });
    } catch (error) {
        showToast("取消入库失败：" + error.message, "error");
    }
}

async function directDeleteFigure(paperId, figureId, button) {
    const reason = window.prompt("请输入直接删除这张污染/重复图片的理由：");
    if (!reason || !reason.trim()) return;
    const figures = state.selectedPaper && Array.isArray(state.selectedPaper.figures) ? state.selectedPaper.figures : [];
    const figure = figures.find(function(item) { return String(item.id) === String(figureId); }) || {};
    try {
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(paperId) + "/figures/" + encodeURIComponent(figureId) + "/delete",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_direct_delete: true,
                    reviewer: "literature_library_user",
                    reason: reason.trim(),
                    evidence_payload: {
                        page: figure.page || null,
                        figure_label: figure.figure_label || null,
                        caption: figure.caption || null,
                    },
                    delete_image_file: true,
                }),
            }
        );
        if (state.selectedPaper && Array.isArray(state.selectedPaper.figures)) {
            state.selectedPaper.figures = state.selectedPaper.figures.filter(function(item) {
                return String(item.id) !== String(figureId);
            });
        }
        const card = button && button.closest ? button.closest("details.figure-card") : null;
        if (card) card.remove();
        showToast("污染/重复图片已删除。", "success");
    } catch (error) {
        showToast("图片删除失败：" + error.message, "error");
    }
}

async function recropPaperFigures(paperId) {
    if (!paperId) {
        showToast("当前文献无法重新裁图。", "error");
        return;
    }
    const ok = window.confirm("将重新根据 PDF 页码、图注和候选裁剪框定位图片。裁剪图仍然只是候选证据，需要人工核对原 PDF 页。是否继续？");
    if (!ok) return;
    try {
        showToast("正在重新定位/重裁图片...", "info");
        const data = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(paperId) + "/figures/recrop",
            { method: "POST" }
        );
        showToast("图片重裁完成：" + (data.extracted_count || 0) + " / " + (data.figure_count || 0), "success");
        if (state.selectedPaperId === paperId) {
            await refreshSelectedPaperDetail({ reason: "recrop_figures", mode: "full" });
        } else {
            await loadPaperDetail(paperId);
        }
    } catch (error) {
        showToast("图片重裁失败：" + error.message, "error");
    }
}

async function promptAddRelationship(paperId) {
    const targetId = window.prompt("请输入支撑文献 SI 的 ID 或短号（例如 U0094）:");
    if (!targetId) return;

    try {
        showToast("正在绑定支撑文献...", "info");
        await fetchJSON(API_BASE + "/" + encodeURIComponent(paperId) + "/relationships", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                target_paper_id: targetId.trim(),
                relationship_type: "supplementary",
                note: "Manual SI binding"
            })
        });
        showToast("支撑文献绑定成功", "success");
        if (state.selectedPaperId === paperId) {
            await refreshSelectedPaperDetail({ reason: "relationship_created", mode: "full" });
        } else {
            await loadPaperDetail(paperId);
        }
    } catch (e) {
        showToast("绑定失败: " + e.message, "error");
    }
}
