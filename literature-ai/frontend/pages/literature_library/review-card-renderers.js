// Review cards, DFT readiness, and compact analysis renderers.
function codexItemActionHtml(itemType, item) {
    if (!itemType || !item || !item.id) return "";
    const figureMetadataOnly = itemType === "figure";
    return '<button class="btn ghost small" type="button" title="复制此项、证据定位、邻近正文和 AI 审核协议" onclick="event.stopPropagation(); copyCodexItem(\'' +
        escAttr(itemType) + '\', \'' + escAttr(item.id) + '\')">' +
        (figureMetadataOnly ? '复制图片说明核对提示' : '复制审核提示') + '</button>';
}

function figureReviewSummaryHtml(item) {
    const imageReview = item.image_review || {};
    const cropStatus = item.crop_status || imageReview.crop_status || "unknown";
    const flags = Array.isArray(item.flags) && item.flags.length ? item.flags : (Array.isArray(imageReview.flags) ? imageReview.flags : []);
    const reliabilityStatus = item.figure_reliability_status || (imageReview.review_required ? "needs_review" : "reliable");
    const reliabilityWarnings = Array.isArray(item.figure_reliability_warnings) && item.figure_reliability_warnings.length
        ? item.figure_reliability_warnings
        : figureIssuesFromFlags(flags);
    const reviewRequired = item.review_required === true || imageReview.review_required === true;
    const auditCount = Number(item.object_review_audit_count || (item.object_review_audits && item.object_review_audits.length) || 0);
    const conflictCount = Number(item.conflict_count || (item.field_conflicts && item.field_conflicts.length) || 0);
    const latest = item.latest_object_review_audit || ((item.object_review_audits || [])[0]) || null;
    const latestHtml = latest
        ? '<div class="figure-review-latest"><strong>Latest audit:</strong> ' +
            esc(latest.source_label || latest.source || "unknown") +
            ' | decision=' + esc(latest.decision || "-") +
            ' | confidence=' + esc(latest.confidence == null ? "-" : latest.confidence) +
            ' | verification=' + esc(latest.verification_status || "unverified") +
            '</div>'
        : '<div class="subtle">Latest audit: none</div>';
    const conflictHtml = conflictCount
        ? '<div class="subtle">Conflict fields: ' + esc((item.field_conflicts || []).map(function(row) { return row.field_name || "-"; }).join(", ")) + '</div>'
        : "";
    const issueChips = reliabilityWarnings.length
        ? reliabilityWarnings.map(function(code) {
            return '<span class="status-chip danger" title="' + esc(code) + '">' + esc(figureIssueLabel(code)) + '</span>';
        }).join("")
        : '<span class="status-chip ok">no figure warnings</span>';
    const sizeBits = [
        imageReview.pixel_size ? "pixel " + imageReview.pixel_size.width + "x" + imageReview.pixel_size.height : null,
        imageReview.bbox_size_points ? "bbox " + imageReview.bbox_size_points.width + "x" + imageReview.bbox_size_points.height : null,
        imageReview.full_page_image_path ? "full-page snapshot present" : "missing full-page snapshot"
    ].filter(Boolean).join(" | ");
    const auditChecklist = '<div class="subtle">Figure audit checklist: confirm the paper&apos;s total figure/subfigure coverage matches the PDF with no missing figures, check whether the crop is too large or too small, whether the crop matches the correct figure/subfigure, whether axes/legends/labels/panels are cut off, and whether the summary explains the visual content instead of repeating the caption.</div>';
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<span class="status-chip">Page ' + esc(item.page || "-") + '</span>' +
            '<span class="status-chip">Crop status: ' + esc(figureCropStatusLabel(cropStatus)) + '</span>' +
            '<span class="status-chip ' + (reliabilityWarnings.length ? 'danger' : 'ok') + '">Figure reliability: ' + esc(figureReliabilityLabel(reliabilityStatus)) + '</span>' +
            '<span class="status-chip ' + (reviewRequired ? 'danger' : 'ok') + '">Image review: ' + (reviewRequired ? 'required' : 'not required') + '</span>' +
            '<span class="status-chip">Object audits ' + auditCount + '</span>' +
            '<span class="status-chip ' + (conflictCount ? 'danger' : '') + '">Conflicts ' + conflictCount + '</span>' +
        '</div>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;">' + issueChips + '</div>' +
        (flags.length ? '<div class="subtle">Flags: ' + esc(flags.join(", ")) + '</div>' : '<div class="subtle">Flags: 0</div>') +
        (sizeBits ? '<div class="subtle">Figure artifact detail: ' + esc(sizeBits) + '</div>' : '') +
        auditChecklist +
        latestHtml +
        conflictHtml +
    '</div>';
}

function figureCropStatusLabel(status) {
    const mapping = {
        unknown: "未分类/待核对",
        candidate_crop: "候选截图",
        needs_review: "待核对",
        verified: "已核对",
        missing: "缺失"
    };
    return mapping[status] || status || "未分类/待核对";
}

function figureReliabilityLabel(status) {
    const mapping = {
        reliable: "reliable candidate",
        candidate_reliable: "reliable candidate",
        needs_review: "needs review",
        unknown: "未分类/待核对"
    };
    return mapping[status] || status || "未分类/待核对";
}

function figureIssueLabel(code) {
    const mapping = {
        missing_full_page_snapshot: "missing full-page snapshot",
        small_crop: "small crop",
        missing_bbox: "missing bbox",
        extreme_aspect_ratio: "extreme aspect ratio",
        caption_only: "caption only",
        missing_image: "missing image",
        missing_page: "missing page"
    };
    return mapping[code] || code;
}

function figureIssuesFromFlags(flags) {
    const mapping = {
        missing_full_page_snapshot: "missing_full_page_snapshot",
        small_crop_or_subfigure: "small_crop",
        missing_parser_bbox: "missing_bbox",
        extreme_aspect_ratio: "extreme_aspect_ratio",
        caption_only: "caption_only",
        missing_image_path: "missing_image",
        missing_image_file: "missing_image",
        missing_pdf_page: "missing_page"
    };
    const issues = [];
    (Array.isArray(flags) ? flags : []).forEach(function(flag) {
        const issue = mapping[flag] || null;
        if (issue && !issues.includes(issue)) issues.push(issue);
    });
    return issues;
}

function selectedDftItemById(itemId) {
    if (!state.selectedPaper || !itemId) return null;
    const items = dftResultsWithSafety(state.selectedPaper);
    for (var i = 0; i < items.length; i += 1) {
        if (dftResultId(items[i]) === String(itemId)) return items[i];
    }
    return null;
}

function dftResultId(item) {
    if (!item) return "";
    return String(
        item.id ||
        item.record_id ||
        (item.export_safety && item.export_safety.record_id) ||
        ""
    ).trim();
}

async function settleAiDftReviews() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    try {
        showToast("正在结算当前论文已有的 DFT AI 审核...", "info");
        const summary = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/settle-ai-dft-reviews",
            { method: "POST" }
        );
        showToast(
            "已结算 " + Number(summary && summary.auto_applied_count || 0) +
            " 条；可导出 " + Number(summary && summary.exportable_count || 0) +
            "；待确认 " + Number(summary && summary.needs_human_count || 0) +
            "；需补字段 " + Number(summary && summary.need_repair_count || 0),
            "success"
        );
        await refreshSelectedPaperDetail({ reason: "settle_ai_dft_reviews", mode: "dft" });
    } catch (error) {
        showToast("结算现有 AI 审核失败：" + error.message, "error");
    }
}

function dftResultsWithSafety(detail) {
    const items = detail.dft_results_items || [];
    const readiness = detail.codex_context && detail.codex_context.dft_export_readiness;
    const safetyById = {};
    ((readiness && readiness.items) || []).forEach(function(item) {
        safetyById[String(item.record_id || "")] = item;
    });
    return items.map(function(item) {
        const recordId = dftResultId(item);
        const safety = safetyById[recordId];
        if (!safety) {
            return Object.assign({}, item, { record_id: recordId });
        }
        const reviewStatuses = String(safety.review_status || "")
            .toLowerCase()
            .split(",")
            .map(function(part) { return part.trim(); })
            .filter(Boolean);
        let effectiveCandidateStatus = item.candidate_status;
        if (reviewStatuses.includes("rejected")) {
            effectiveCandidateStatus = "Rejected";
        } else if (safety.is_exportable === true || safety.eligible === true) {
            effectiveCandidateStatus = "ML_Ready";
        } else if (reviewStatuses.includes("verified")) {
            effectiveCandidateStatus = "human_reviewed_needs_evidence";
        }
        return Object.assign({}, item, {
            record_id: recordId,
            export_safety: safety,
            candidate_status: effectiveCandidateStatus,
            dft_workflow_state: item.dft_workflow_state || safety.dft_workflow_state,
            dft_workflow_label: item.dft_workflow_label || safety.dft_workflow_label,
            dft_workflow_reason: item.dft_workflow_reason || safety.dft_workflow_reason,
            valid_ai_opinion_count: item.valid_ai_opinion_count == null ? safety.valid_ai_opinion_count : item.valid_ai_opinion_count,
            raw_ai_opinion_count: item.raw_ai_opinion_count == null ? safety.raw_ai_opinion_count : item.raw_ai_opinion_count,
            effective_ai_opinions: item.effective_ai_opinions || safety.effective_ai_opinions,
            next_required_action: item.next_required_action || safety.next_required_action
        });
    }).sort(function(a, b) {
        const aExportable = String(a.dft_workflow_state || "") === "exportable" || (a.export_safety && (a.export_safety.is_exportable || a.export_safety.eligible));
        const bExportable = String(b.dft_workflow_state || "") === "exportable" || (b.export_safety && (b.export_safety.is_exportable || b.export_safety.eligible));
        if (aExportable !== bExportable) return aExportable ? 1 : -1;
        const priority = {
            needs_human: 0,
            missing_evidence_anchor: 1,
            waiting_ai_review: 2,
            missing_material_binding: 3,
            review_pending_apply: 4,
            unknown_blocked: 5,
            exportable: 9
        };
        const left = priority[a.dft_workflow_state] == null ? 6 : priority[a.dft_workflow_state];
        const right = priority[b.dft_workflow_state] == null ? 6 : priority[b.dft_workflow_state];
        if (left !== right) return left - right;
        return String(a.property_type || "").localeCompare(String(b.property_type || ""));
    });
}

function dftReviewSummary(detail) {
    const items = dftResultsWithSafety(detail || {});
    const page = detail && detail.dft_results_page || {};
    const total = Number(page.total || (detail && detail.counts && detail.counts.dft_results) || items.length);
    let reviewed = 0;
    let rejected = 0;
    let needsHuman = 0;
    items.forEach(function(item) {
        const candidateStatus = String(item && item.candidate_status || "").trim().toLowerCase();
        const workflowState = String(item && item.dft_workflow_state || "").trim().toLowerCase();
        const displayStatus = String(item && item.ai_review_display_status || "").trim().toLowerCase();
        const safety = item && item.export_safety || {};
        const reviewStatuses = String(safety.review_status || "").toLowerCase().split(",").map(function(value) {
            return value.trim();
        });
        const isRejected = candidateStatus === "rejected" || workflowState === "rejected" ||
            displayStatus === "rejected" || reviewStatuses.includes("rejected");
        const isReviewed = isRejected || item.is_exportable === true || safety.eligible === true ||
            safety.is_exportable === true || workflowState === "exportable" ||
            ["ml_ready", "ai_verified_ml_ready", "human_confirmed", "citation_ready", "verified", "human_verified"].includes(candidateStatus);
        const requiresHuman = displayStatus === "needs_human" || candidateStatus === "needs_human_confirmation" ||
            workflowState === "needs_human";
        if (isReviewed) reviewed += 1;
        if (isRejected) rejected += 1;
        if (!isReviewed && requiresHuman) needsHuman += 1;
    });
    return {
        loaded: items.length,
        total: total,
        complete: hasCompleteDftResults(detail),
        reviewed: reviewed,
        rejected: rejected,
        needsHuman: needsHuman,
        pending: Math.max(0, total - reviewed),
    };
}

function renderDftExportReadiness(detail) {
    const summary = dftReviewSummary(detail);
    const completionControls = summary.complete && summary.pending === 0
        ? renderManualReviewCompletionControls(detail, "dft")
        : '<span class="status-chip subtle">未完成</span>';
    return '<div class="section-card figure-audit-note" data-role="dft-status-panel" data-paper-id="' + escAttr(detail && (detail.paper_id || detail.id) || "") + '">' +
        '<h3>DFT 数据状态</h3>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 10px;">' +
            completionControls +
            '<span class="status-chip parsed">已审核 ' + summary.reviewed + '</span>' +
            '<span class="status-chip meta">待审核 ' + summary.pending + '</span>' +
            (summary.needsHuman ? '<span class="status-chip failed">待人工确认 ' + summary.needsHuman + '</span>' : '') +
            (summary.rejected ? '<span class="status-chip muted">已拒绝 ' + summary.rejected + '</span>' : '') +
            '<span class="status-chip">候选总数 ' + summary.total + '</span>' +
        '</div>' +
        (!summary.complete ? '<div class="subtle">审核状态正在随 DFT 数据加载更新（' + summary.loaded + ' / ' + summary.total + '）。</div>' : '') +
    '</div>';
}

async function resetDftAiReviewsForPaper() {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const ok = window.confirm(
        "确认清除当前文献的 DFT AI 审核记录并重新核验吗？\n\n" +
        "这会删除 DFT AI 审核/冲突意见，把 DFT 候选退回待审核；不会删除候选 DFT 数据本身。"
    );
    if (!ok) return;
    try {
        showToast("正在清除当前文献的 DFT AI 审核状态...", "info");
        const summary = await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) + "/dft-ai-reviews/reset",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    confirm_reset_dft_ai_reviews: true,
                    reviewer: "literature_library_dft",
                    keep_dft_candidates: true
                })
            }
        );
        showToast(
            "已清除 DFT AI 审核：" +
            "对象意见 " + Number(summary && summary.deleted_object_review_candidates || 0) +
            " 条，字段审核 " + Number(summary && summary.deleted_field_reviews || 0) +
            " 条；候选退回 " + Number(summary && summary.reset_dft_results || 0) + " 条。",
            "success"
        );
        await refreshSelectedPaperDetail({ reason: "reset_dft_ai_reviews", mode: "dft" });
    } catch (error) {
        showToast("清除 DFT AI 审核失败：" + error.message, "error");
    }
}

function writingCardReviewMeta(item) {
    if (item && item.can_use_for_writing) {
        return { label: "可直接参考", className: "high", tip: "这组论文重点已满足当前写作使用条件。" };
    }
    const reasons = Array.isArray(item && item.blocked_reasons) ? item.blocked_reasons : [];
    if (reasons.length) {
        return { label: "需先核对", className: "medium", tip: "当前仍有待处理项：" + reasons.join("、") };
    }
    return { label: "提取候选", className: "unknown", tip: "这是自动提取的论文重点，使用前必须核对证据。" };
}

function writingCardLogicBlock(label, value) {
    const textValue = compactText(value);
    if (!textValue) return "";
    return '<div class="knowledge-detail-block"><div class="knowledge-detail-title">' + esc(label) + '</div><div class="knowledge-detail-text">' + esc(textValue) + '</div></div>';
}

function writingCardAuditSummaryHtml(item) {
    const auditCount = Number(item && (item.object_review_audit_count || (item.object_review_audits && item.object_review_audits.length)) || 0);
    const conflictCount = Number(item && (item.conflict_count || (item.field_conflicts && item.field_conflicts.length)) || 0);
    const latest = (item && (item.latest_object_review_audit || ((item.object_review_audits || [])[0]))) || null;
    const evidenceStatus = item && (item.evidence_status || item.evidence_chain_status) || "missing";
    const safetyStatus = item && (item.safety_status || item.review_gate_status) || "blocked";
    const safeVerified = Boolean(item && (item.safe_verified || item.can_use_for_writing));
    const latestHtml = latest
        ? '<div class="figure-review-latest"><strong>Latest audit:</strong> ' +
            esc(latest.source_label || latest.source || "unknown") +
            ' | decision=' + esc(latest.decision || "-") +
            ' | confidence=' + esc(latest.confidence == null ? "-" : latest.confidence) +
            ' | verification=' + esc(latest.verification_status || "unverified") +
            '</div>'
        : '<div class="subtle">Latest audit: none</div>';
    const conflictHtml = conflictCount
        ? '<div class="subtle">Conflict fields: ' + esc((item.field_conflicts || []).map(function(row) { return row.field_name || "-"; }).join(", ")) + '</div>'
        : "";
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<span class="status-chip">Object audits ' + auditCount + '</span>' +
            '<span class="status-chip ' + (conflictCount ? 'danger' : '') + '">Conflicts ' + conflictCount + '</span>' +
            '<span class="status-chip">Evidence status: ' + esc(prettifyToken(evidenceStatus)) + '</span>' +
            '<span class="status-chip ' + (safeVerified ? 'ok' : 'danger') + '">Safety: ' + esc(prettifyToken(safetyStatus)) + '</span>' +
        '</div>' +
        latestHtml +
        conflictHtml +
    '</div>';
}

function mechanismClaimAuditSummaryHtml(item) {
    const auditCount = Number(item && (item.object_review_audit_count || (item.object_review_audits && item.object_review_audits.length)) || 0);
    const conflictCount = Number(item && (item.conflict_count || (item.field_conflicts && item.field_conflicts.length)) || 0);
    const latest = (item && (item.latest_object_review_audit || ((item.object_review_audits || [])[0]))) || null;
    const evidenceStatus = item && item.evidence_status ? item.evidence_status : (compactText(item && item.evidence_text) ? "present" : "missing");
    const locatorStatus = item && item.locator_status ? item.locator_status : (compactText(item && item.evidence_text) ? "text_only" : "missing_locator");
    const confidenceStatus = item && item.confidence_status ? item.confidence_status : (item && item.confidence != null ? "candidate" : "missing");
    const latestHtml = latest
        ? '<div class="figure-review-latest"><strong>Latest audit:</strong> ' +
            esc(latest.source_label || latest.source || "unknown") +
            ' | decision=' + esc(latest.decision || "-") +
            ' | confidence=' + esc(latest.confidence == null ? "-" : latest.confidence) +
            ' | verification=' + esc(latest.verification_status || "unverified") +
            '</div>'
        : '<div class="subtle">Latest audit: none</div>';
    const conflictHtml = conflictCount
        ? '<div class="subtle">Conflict fields: ' + esc((item.field_conflicts || []).map(function(row) { return row.field_name || "-"; }).join(", ")) + '</div>'
        : "";
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            '<span class="status-chip">Object audits ' + auditCount + '</span>' +
            '<span class="status-chip ' + (conflictCount ? 'danger' : '') + '">Conflicts ' + conflictCount + '</span>' +
            '<span class="status-chip">Evidence status: ' + esc(prettifyToken(evidenceStatus)) + '</span>' +
            '<span class="status-chip">Locator: ' + esc(prettifyToken(locatorStatus)) + '</span>' +
            '<span class="status-chip">Confidence: ' + esc(prettifyToken(confidenceStatus)) + '</span>' +
        '</div>' +
        latestHtml +
        conflictHtml +
    '</div>';
}

function dftConflictSummaryHtml(item) {
    const conflicts = Array.isArray(item && item.field_conflicts) ? item.field_conflicts : [];
    const conflictCount = Number(item && (item.conflict_count || conflicts.length) || 0);
    if (!conflictCount) return "";
    const fields = [];
    [item && item.affected_field_names, item && item.conflict_field_names].concat(
        conflicts.map(function(conflict) {
            return conflict && (conflict.affected_field_names || conflict.conflict_field_names);
        })
    ).forEach(function(values) {
        (Array.isArray(values) ? values : []).forEach(function(field) {
            const normalized = compactText(field);
            if (normalized && !fields.includes(normalized)) fields.push(normalized);
        });
    });
    return '<div class="figure-review-summary" style="margin-top:12px;display:grid;gap:8px;">' +
        '<div><span class="status-chip danger">Conflicts ' + conflictCount + '</span></div>' +
        (fields.length ? '<div class="subtle">Conflict fields: ' + esc(fields.join(", ")) + '</div>' : '') +
    '</div>';
}

function isPendingNavigationItem(itemType, item) {
    const target = state.pendingNavigationTarget;
    return !!(
        target && item && item.id &&
        target.itemType === itemType &&
        String(target.targetId) === String(item.id)
    );
}

function renderWritingCardsCompact(items) {
    if (!items || !items.length) {
        return '<div class="section-card"><h3>论文重点</h3><div class="muted">暂无内容。</div></div>';
    }
    const intro = '<div class="section-card figure-audit-note"><h3>论文重点说明</h3><div class="subtle">这里显示从全文提取的研究空白、方案和核心假设。证据状态与阻塞原因默认折叠，正式写作前必须到统一内容审核页核对。</div></div>';
    return intro + items.map(function(item, index) {
        const review = writingCardReviewMeta(item);
        const action = codexItemActionHtml("writing_card", item);
        const evidenceStatus = item && item.evidence_chain_status ? prettifyToken(item.evidence_chain_status) : "未提供";
        const summaryBlocks = [
            { label: "研究空白", value: item && item.research_gap },
            { label: "拟解决方案", value: item && item.proposed_solution },
            { label: "核心假设", value: item && item.core_hypothesis }
        ].filter(function(block) {
            return compactText(block.value);
        }).map(function(block) {
            return '<div class="writing-card-summary-block"><div class="writing-card-summary-title">' + esc(block.label) + '</div><div class="writing-card-summary-text">' + esc(clipText(block.value, 160)) + '</div></div>';
        }).join("");
        const details = [
            writingCardLogicBlock("证据链状态", item && item.evidence_chain_status)
        ].filter(Boolean).join("");
        const auditSummary = writingCardAuditSummaryHtml(item || {});
        const blocked = Array.isArray(item && item.blocked_reasons) && item.blocked_reasons.length
            ? '<div class="knowledge-detail-block"><div class="knowledge-detail-title">当前限制</div><div class="knowledge-detail-text">' + esc(item.blocked_reasons.join("、")) + '</div></div>'
            : "";
        const navigationAttrs = ' data-codex-item-type="writing_card" data-target-id="' + escAttr(String(item && item.id || "")) + '"' +
            (isPendingNavigationItem("writing_card", item) ? " open" : "");
        return '<details class="section-card writing-card-compact"' + navigationAttrs + '>' +
            '<summary style="display:flex; justify-content:space-between; align-items:flex-start; flex:1; width:100%;">' +
                '<div style="flex:1;">' +
                    '<div class="knowledge-card-head">' +
                        '<div><h3 style="margin:0;">论文重点 ' + (items.length > 1 ? (index + 1) : "") + '</h3><div class="knowledge-card-use">研究空白、方案与核心假设的结构化提取</div></div>' +
                        '<div class="knowledge-card-actions">' + action + '</div>' +
                    '</div>' +
                    '<div class="knowledge-tag-row">' +
                        '<span class="status-chip meta">' + esc(paperTypeLabel(item && item.paper_type)) + '</span>' +
                        '<span class="status-chip confidence-' + esc(review.className) + '" title="' + esc(review.tip) + '">' + esc(review.label) + '</span>' +
                        '<span class="status-chip" title="当前证据链状态">' + esc(evidenceStatus) + '</span>' +
                    '</div>' +
                '</div>' +
            '</summary>' +
            auditSummary +
            '<div class="writing-card-summary-grid">' + (summaryBlocks || '<div class="muted">当前还没有提取出可直接阅读的论文重点。</div>') + '</div>' +
            '<details class="knowledge-details">' +
                '<summary>展开证据状态与限制</summary>' +
                details +
                blocked +
            '</details>' +
        '</details>';
    }).join("");
}

function shortDftResultId(value) {
    const text = String(value || "").trim();
    if (text.length <= 18) return text;
    return text.slice(0, 8) + "\u2026" + text.slice(-6);
}

function dftLocatorClipboardText(resultId, index) {
    return "DFT #" + (Number(index) + 1) + "; dft_result_id=" + String(resultId || "").trim();
}

async function copyDftLocator(event, resultId, index) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const text = dftLocatorClipboardText(resultId, index);
    try {
        await navigator.clipboard.writeText(text);
        showToast("DFT 定位信息已复制。", "success");
    } catch (error) {
        showToast("复制 DFT 定位信息失败：" + error.message, "error");
    }
}

function renderDftRecordLocator(item, index) {
    const resultId = dftResultId(item);
    if (!resultId) return "";
    return '<div class="dft-record-locator" data-role="dft-record-locator" data-dft-result-id="' + escAttr(resultId) + '">' +
        '<span class="dft-record-number" data-role="dft-record-number">DFT #' + (index + 1) + '</span>' +
    '</div>';
}

function dftCompactReadableValue(value, fallback) {
    const text = readableValue(value);
    return text && text !== "-" ? text : (fallback || "-");
}

function dftCompactCatalystLabel(item) {
    item = item || {};
    const sample = item.bound_catalyst_sample || {};
    return sample.name || sample.material_identity || item.catalyst || item.material_identity || "未绑定催化剂";
}

function dftCompactConfidenceLabel(value) {
    if (value === null || value === undefined || value === "") return "";
    const numeric = Number(value);
    if (!Number.isNaN(numeric) && numeric >= 0 && numeric <= 1) return Math.round(numeric * 100) + "%";
    return String(value);
}

function dftCompactChip(label, className, title) {
    if (!label) return "";
    return '<span class="status-chip ' + escAttr(className || "meta") + '"' +
        (title ? ' title="' + escAttr(title) + '"' : "") + '>' + esc(label) + '</span>';
}

function dftCompactEvidenceText(item) {
    const text = compactText(item && item.evidence_text);
    if (!text) return "";
    return '<div class="dft-compact-detail-block"><div class="dft-compact-detail-title">证据原文</div>' +
        '<div class="dft-compact-detail-text">' + esc(clipText(text, 360)) + '</div></div>';
}

function dftCompactAuditText(item) {
    const latest = item && (item.latest_object_review_audit || ((item.object_review_audits || [])[0]));
    if (!latest) return "";
    const bits = [
        latest.source_label || latest.source || "",
        latest.decision ? ("decision=" + latest.decision) : "",
        latest.reason || ""
    ].filter(Boolean).join(" | ");
    return '<div class="dft-compact-detail-block"><div class="dft-compact-detail-title">最新审核</div>' +
        '<div class="dft-compact-detail-text">' + esc(clipText(bits, 260)) + '</div></div>';
}

function dftCompactMetaRows(item) {
    const rows = [
        ["样本 ID", item && item.catalyst_sample_id],
        ["活性位点", item && item.active_site_instance_key],
        ["来源章节", item && item.source_section],
        ["来源图/表", item && item.source_figure],
        ["候选状态", item && item.candidate_status]
    ].filter(function(row) {
        const value = dftCompactReadableValue(row[1], "");
        return value && value !== "-";
    });
    if (!rows.length) return "";
    return '<div class="dft-compact-meta-grid">' + rows.map(function(row) {
        return '<div><span>' + esc(row[0]) + '</span><strong>' + esc(dftCompactReadableValue(row[1], "")) + '</strong></div>';
    }).join("") + '</div>';
}

function closeDftDetailDialog() {
    const dialog = document.getElementById("dftDetailDialog");
    if (dialog) dialog.style.display = "none";
    if (typeof activeDftEditItemId !== "undefined") activeDftEditItemId = null;
}

function dftDetailDialogRow(label, value, options) {
    options = options || {};
    const text = options.html ? value : esc(dftCompactReadableValue(value, ""));
    if (!options.html && (!text || text === "-")) return "";
    return '<div class="dft-detail-dialog-label">' + esc(label) + '</div>' +
        '<div class="dft-detail-dialog-cell ' + escAttr(options.className || "") + '">' + (text || "-") + '</div>';
}

function dftDetailAuditSummary(item) {
    const latest = item && (item.latest_object_review_audit || ((item.object_review_audits || [])[0]));
    if (!latest) return "";
    return [
        latest.source_label || latest.source || "",
        latest.decision ? ("decision=" + latest.decision) : "",
        latest.confidence == null ? "" : ("confidence=" + latest.confidence),
        latest.reason || ""
    ].filter(Boolean).join(" | ");
}

function renderDftDetailDialogBody(item) {
    const catalyst = dftCompactCatalystLabel(item);
    const adsorbate = dftCompactReadableValue(item && item.adsorbate, "");
    const propertyType = dftCompactReadableValue(item && (item.property_type || item.energy_type), "");
    const value = dftCompactReadableValue(item && item.value, "");
    const unit = dftCompactReadableValue(item && item.unit, "");
    const reactionStep = dftCompactReadableValue(item && item.reaction_step, "");
    const confidence = dftCompactConfidenceLabel(item && item.confidence);
    const rows = [
        dftDetailDialogRow("催化剂", catalyst, { className: "strong" }),
        dftDetailDialogRow("吸附物", adsorbate),
        dftDetailDialogRow("性质", propertyType),
        dftDetailDialogRow("数值", '<span class="dft-table-value"><strong>' + esc(value || "-") + '</strong>' + (unit ? '<span>' + esc(unit) + '</span>' : '') + '</span>', { html: true }),
        dftDetailDialogRow("反应步骤", reactionStep, { className: "wide" }),
        dftDetailDialogRow("来源章节", item && item.source_section),
        dftDetailDialogRow("来源图/表", item && item.source_figure),
        dftDetailDialogRow("置信度", confidence),
        dftDetailDialogRow("候选状态", item && item.candidate_status),
        dftDetailDialogRow("关联样本", item && item.catalyst_sample_id, { className: "wide mono" }),
        dftDetailDialogRow("证据原文", item && item.evidence_text, { className: "wide long" }),
        dftDetailDialogRow("最新审核", dftDetailAuditSummary(item), { className: "wide long" })
    ].filter(Boolean).join("");
    return '<div class="dft-detail-dialog-grid">' + rows + '</div>' +
        '<div class="dft-detail-dialog-section">' + renderDftEvidenceSource(item) + '</div>' +
        '<div class="dft-detail-dialog-section">' + dftConflictSummaryHtml(item) + '</div>' +
        '<div class="dft-detail-dialog-section">' + renderDftItemSafety(item) + '</div>';
}

function openDftDetailDialog(event, resultId, index) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const item = selectedDftItemById(resultId);
    if (!item) {
        showToast("未找到这条 DFT 数据。", "error");
        return;
    }
    const dialog = document.getElementById("dftDetailDialog");
    const title = document.getElementById("dftDetailTitle");
    const locator = document.getElementById("dftDetailLocator");
    const body = document.getElementById("dftDetailBody");
    if (!dialog || !title || !locator || !body) return;
    title.textContent = "DFT #" + (Number(index) + 1) + " 数据详情";
    locator.textContent = "完整 ID 已隐藏，可在卡片表头使用“复制 ID”。";
    body.innerHTML = renderDftDetailDialogBody(item);
    dialog.style.display = "flex";
}

function dftCompactTableRow(label, value, options) {
    options = options || {};
    const content = options.html ? value : esc(value || "-");
    return '<div class="dft-table-label">' + esc(label) + '</div>' +
        '<div class="dft-table-cell ' + escAttr(options.className || "") + '">' + content + '</div>';
}

function renderDftCompactReadableCard(item, index, title, keys) {
    const resultId = dftResultId(item);
    const itemType = "dft_result";
    const itemTypeAttr = ' data-codex-item-type="' + escAttr(itemType) + '"';
    const targetIdAttr = resultId ? ' data-target-id="' + escAttr(resultId) + '"' : "";
    const statusChip = renderDftItemStatusChip(item);
    const aiChip = renderDftAiOpinionChip(item);
    const copyButton = resultId
        ? '<button class="btn ghost small" type="button" data-role="copy-dft-locator" onclick="copyDftLocator(event, \'' + escAttr(resultId) + '\', ' + index + ')">复制 ID</button>'
        : "";
    const action = codexItemActionHtml(itemType, item);
    const terminalDecisionActions = typeof isFinalizedDftResult === "function" &&
        typeof renderDftDecisionActions === "function" &&
        isFinalizedDftResult(item, isDftItemExportable(item))
        ? renderDftDecisionActions(item, isDftItemExportable(item))
        : "";
    const catalyst = dftCompactCatalystLabel(item);
    const adsorbate = dftCompactReadableValue(item && item.adsorbate, "吸附物未记录");
    const propertyType = dftCompactReadableValue(item && (item.property_type || item.energy_type), "性质未记录");
    const value = dftCompactReadableValue(item && item.value, "-");
    const unit = dftCompactReadableValue(item && item.unit, "");
    const reactionStep = dftCompactReadableValue(item && item.reaction_step, "反应步骤未记录");
    const confidence = dftCompactConfidenceLabel(item && item.confidence);
    const sourceLabel = dftCompactReadableValue(item && (item.source_figure || item.source_section), "");
    const summaryChips = [
        sourceLabel ? dftCompactChip("来源 " + sourceLabel, "meta", sourceLabel) : "",
        confidence ? dftCompactChip("置信度 " + confidence, "meta") : "",
        statusChip,
        aiChip
    ].filter(Boolean).join("");
    const tableRows = [
        dftCompactTableRow("催化剂", catalyst, { className: "dft-table-strong" }),
        dftCompactTableRow("吸附物", adsorbate),
        dftCompactTableRow("性质", propertyType),
        dftCompactTableRow("反应步骤", reactionStep),
        dftCompactTableRow(
            "数值",
            '<span class="dft-table-value" data-role="dft-core-value"><strong>' + esc(value) + '</strong>' +
                (unit ? '<span>' + esc(unit) + '</span>' : '') + '</span>',
            { html: true }
        ),
        dftCompactTableRow("状态", summaryChips || "-", { html: true, className: "dft-table-status" })
    ].join("");
    const focusClass = isPendingNavigationItem(itemType, item) ? " deep-link-focus" : "";
    return '<div class="section-card readable-card dft-compact-card' + focusClass + '"' + itemTypeAttr + targetIdAttr +
        (resultId ? ' data-dft-result-id="' + escAttr(resultId) + '"' : "") + '>' +
        '<div class="dft-compact-head">' +
            '<button class="dft-compact-expand" type="button" onclick="openDftDetailDialog(event, \'' + escAttr(resultId) + '\', ' + index + ')">' +
                renderDftRecordLocator(item, index) +
                '<span class="dft-compact-expand-hint">详情</span>' +
            '</button>' +
            '<div class="dft-compact-actions">' + copyButton + action + terminalDecisionActions + '</div>' +
        '</div>' +
        '<div class="dft-record-table" data-role="dft-record-table">' + tableRows + '</div>' +
    '</div>';
}

function renderReadableCards(title, items, options) {
    options = options || {};
    if (!items || !items.length) {
        if (title === "电化学性能") {
            return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">当前没有结构化电化学性能数据。该模块来自实验/电化学信号的 Stage 2 抽取，或由 IDE AI 通过 import_analysis 回写；纯计算论文通常为空。</div></div>';
        }
        if (title === "机理声明") {
            return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">当前没有结构化机理声明。该模块来自 Stage 2 机理规则抽取，或由 IDE AI 通过 import_analysis 回写；论文重点只引用这些证据，不承载原始结构化数据。</div></div>';
        }
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    if (title === "写作卡片" || title === "论文重点") {
        return renderWritingCardsCompact(items);
    }
    const keySets = {
        "DFT 设置": ["software", "functional", "dispersion_correction", "pseudopotential", "cutoff_energy_ev", "cutoff_energy", "k_points", "convergence_settings", "vacuum_thickness_a", "vacuum_thickness"],
        "催化剂样本": ["name", "catalyst_type", "metal_centers", "coordination", "support", "synthesis_method", "evidence_text", "confidence"],
        "DFT 结果": ["catalyst", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "evidence_text", "confidence"],
        "候选 DFT 数据": ["candidate_status", "catalyst_sample_id", "active_site_instance_key", "catalyst", "material_identity", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "source_figure", "evidence_text", "confidence"],
        "DFT 候选结果": ["candidate_status", "catalyst_sample_id", "active_site_instance_key", "catalyst", "material_identity", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "source_figure", "evidence_text", "confidence"],
        "电化学性能": ["sulfur_loading", "sulfur_content", "electrolyte_sulfur_ratio", "capacity", "cycle_number", "rate", "decay_per_cycle", "evidence_text", "confidence"],
        "机理声明": ["claim_type", "evidence_types", "claim_text", "evidence_text", "confidence"],
        "机理知识": ["candidate_status", "review_status", "can_use_for_writing", "can_use_for_citation", "claim_type", "claim_text", "evidence_text", "confidence"],
        "机理内容": ["candidate_status", "review_status", "can_use_for_writing", "can_use_for_citation", "claim_type", "claim_text", "evidence_text", "confidence"],
        "写作卡片": ["paper_type", "research_gap", "proposed_solution", "core_hypothesis", "evidence_text"],
        "论文重点": ["paper_type", "research_gap", "proposed_solution", "core_hypothesis", "evidence_text"],
        "表格": ["source_document_type", "related_paper_code", "caption", "page", "markdown_content"],
        "出站关联": ["relationship_type", "target_title", "target_doi", "reason"],
        "入站关联": ["relationship_type", "source_title", "source_doi", "reason"]
    };
    let keys = keySets[title] ? keySets[title].slice() : Object.keys(items[0] || {}).filter(function(key) {
        return !["id", "paper_id", "raw_json", "created_at", "updated_at"].includes(key);
    }).slice(0, 10);
    const longFields = ["evidence_text", "markdown_content", "reason", "claim_text", "research_gap", "proposed_solution", "core_hypothesis", "caption"];
    keys.sort(function(a, b) {
        const aLong = longFields.includes(a) ? 1 : 0;
        const bLong = longFields.includes(b) ? 1 : 0;
        return aLong - bLong;
    });
    function renderReadableCardItem(item, index) {
        if (isDftCandidateCardTitle(title)) {
            return renderDftCompactReadableCard(item, index, title, keys);
        }
        const itemType = CODEX_ITEM_TYPE_BY_CARD_TITLE[title];
        const heading = itemType === "dft_result"
            ? title + " #" + (index + 1)
            : title + (items.length > 1 ? " " + (index + 1) : "");
        const action = codexItemActionHtml(itemType, item);
        const dftStatusChip = itemType === "dft_result" ? renderDftItemStatusChip(item) : "";
        const dftAiChip = itemType === "dft_result" ? renderDftAiOpinionChip(item) : "";
        const dftRecordLocator = itemType === "dft_result" ? renderDftRecordLocator(item, index) : "";
        const mechanismAuditSummary = itemType === "mechanism_claim" ? mechanismClaimAuditSummaryHtml(item || {}) : "";
        const dftEvidenceSource = itemType === "dft_result" ? renderDftEvidenceSource(item) : "";
        const dftConflictSummary = itemType === "dft_result" ? dftConflictSummaryHtml(item) : "";
        const safety = (title === "DFT 结果" || title === "候选 DFT 数据" || title === "DFT 候选结果") ? renderDftItemSafety(item) : "";
        const tableReviewChip = title === "\u8868\u683c" ? tableReviewChipHtml(item) : "";
        const tableSourceChip = title === "\u8868\u683c" ? tableSourceChipHtml(item) : "";
        const itemTypeAttr = itemType ? ' data-codex-item-type="' + escAttr(itemType) + '"' : "";
        const targetIdAttr = item && item.id ? ' data-target-id="' + escAttr(String(item.id)) + '"' : "";
        const openAttr = isPendingNavigationItem(itemType, item) ? " open" : "";
        return '<details class="section-card readable-card"' + itemTypeAttr + targetIdAttr + openAttr + '>' +
             '<summary><div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;flex:1;width:100%;"><h3 style="margin:0;">' + esc(heading) + '</h3><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">' + dftStatusChip + dftAiChip + tableSourceChip + tableReviewChip + action + '</div></div></summary>' +
             '<div style="margin-top:10px;">' +
            dftRecordLocator +
             renderReadableFields(item || {}, keys) +
            dftEvidenceSource +
            dftConflictSummary +
            mechanismAuditSummary +
            safety +
            '</div>' +
        '</details>';
    }
    if (isDftCandidateCardTitle(title)) {
        return renderDftSampleGroups(items, renderReadableCardItem, options);
    }
    return items.map(renderReadableCardItem).join("");
}

function renderComprehensiveAnalysis(data) {
    if (!data || !Object.keys(data).length) {
        return '<div class="section-card"><h3>综合解析</h3><div class="muted">暂无综合解析。</div></div>';
    }
    const summary = data.layman_summary || {};
    const logic = data.writing_logic || {};
    return '<details class="section-card readable-card"><summary><h3>综合解析</h3></summary>' +
        renderReadableFields({
            one_sentence_takeaway: summary.one_sentence_takeaway,
            real_world_impact: summary.real_world_impact,
            research_gap: logic.research_gap_framing,
            core_hypothesis: logic.core_hypothesis,
            conclusion_mapping: logic.conclusion_mapping
        }, ["one_sentence_takeaway", "real_world_impact", "research_gap", "core_hypothesis", "conclusion_mapping"]) +
    '</details>';
}
