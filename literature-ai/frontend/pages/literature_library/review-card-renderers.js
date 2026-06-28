// Review cards, DFT readiness, and compact analysis renderers.
function codexItemActionHtml(itemType, item) {
    if (!itemType || !item || !item.id) return "";
    return '<button class="btn ghost small" type="button" title="复制此项、证据定位、邻近正文和 AI 审核协议" onclick="event.stopPropagation(); copyCodexItem(\'' +
        escAttr(itemType) + '\', \'' + escAttr(item.id) + '\')">复制审核提示</button>';
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

async function refreshDftAutomationSummaryBadges(container, paperId, renderSeq) {
    const targetPaperId = paperId || state.selectedPaperId;
    if (!container || !targetPaperId) return;
    try {
        const rows = await fetchSelectedDftReviewRows(200, targetPaperId);
        if (
            state.selectedPaperId !== targetPaperId ||
            (renderSeq && state.dftReadinessRenderSeq !== renderSeq) ||
            !container.isConnected
        ) {
            return;
        }
        const classified = classifyDftAutomationRows(rows);
        const setText = function(role, value) {
            const el = container.querySelector('[data-role="' + role + '"]');
            if (el) el.textContent = value;
        };
        setText("dft-new-review-count", "下一轮审核 / 补证据 " + classified.newReview.length);
        setText("dft-conflict-count", "第三轮 AI 裁决 " + classified.conflicts.length);
        setText(
            "dft-next-action",
            "生成下一轮 AI 审核任务（" + (classified.newReview.length + classified.conflicts.length) + "）"
        );
    } catch (_) {
        const pending = container.querySelector('[data-role="dft-new-review-count"]');
        if (pending) pending.textContent = "新数据审核 ?";
    }
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
            "；需第三AI裁决 " + Number(summary && summary.need_third_ai_count || 0) +
            "；需补字段 " + Number(summary && summary.need_repair_count || 0),
            "success"
        );
        await refreshSelectedPaperDetail({ reason: "settle_ai_dft_reviews", mode: "full" });
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
            needs_third_ai: 0,
            missing_evidence_anchor: 1,
            waiting_second_ai: 2,
            missing_material_binding: 3,
            rejected_consensus_pending_write: 4,
            unknown_blocked: 5,
            exportable: 9
        };
        const left = priority[a.dft_workflow_state] == null ? 6 : priority[a.dft_workflow_state];
        const right = priority[b.dft_workflow_state] == null ? 6 : priority[b.dft_workflow_state];
        if (left !== right) return left - right;
        return String(a.property_type || "").localeCompare(String(b.property_type || ""));
    });
}

function renderDftExportReadiness(detail) {
    const readiness = detail && detail.codex_context && detail.codex_context.dft_export_readiness;
    const fallbackTotal = Array.isArray(detail && detail.dft_results_items) ? detail.dft_results_items.length : 0;
    const hasReadiness = !!readiness;
    const readinessData = readiness || {};
    const rejectedCount = Number(readinessData.rejected_count || 0);
    const blockedCount = Number(readinessData.blocked_count || 0);
    const pendingCount = Math.max(0, blockedCount);
    const completionControls = hasReadiness && pendingCount === 0
        ? renderManualReviewCompletionControls(detail, "dft")
        : '<span class="status-chip subtle">未完成</span>';
    const reasons = Object.keys(readinessData.blocked_reasons || {}).map(function(reason) {
        return (DFT_BLOCK_REASON_LABELS[reason] || reason) + " " + readinessData.blocked_reasons[reason] + " 条";
    }).join("、");
    return '<div class="section-card figure-audit-note" data-role="dft-status-panel" data-paper-id="' + escAttr(detail && (detail.paper_id || detail.id) || "") + '">' +
        '<h3>DFT 数据状态</h3>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 10px;">' +
            completionControls +
            (hasReadiness
                ? '<span class="status-chip parsed">可导出 ' + Number(readiness.eligible_count || 0) + '</span>' +
                  '<span class="status-chip meta">待完成 ' + pendingCount + '</span>' +
                  (rejectedCount ? '<span class="status-chip muted">\u5df2\u62d2\u7edd ' + rejectedCount + '</span>' : '') +
                  '<span class="status-chip">候选总数 ' + Number(readiness.total_candidates || 0) + '</span>'
                : '<span class="status-chip meta">安全状态加载中</span>' +
                  '<span class="status-chip">候选总数 ' + fallbackTotal + '</span>') +
        '</div>' +
        '<div class="subtle">处理方式：点击“生成下一轮 AI 审核任务”后导入下一轮审核结果；同一 AI/模型可以重复审核，每次成功回写按独立 candidate_id 计一票。每条有效意见仍必须提供 evidence_location.page 和 quoted_text。系统会先刷新审核状态，只把缺下一轮有效意见、缺证据或真正冲突的记录放进下一轮。最终 verify/reject 仍需人工处理。</div>' +
        (reasons ? '<div class="subtle" style="margin-top:6px;">当前阻断：' + esc(reasons) + '</div>' : '') +
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
        await refreshSelectedPaperDetail({ reason: "reset_dft_ai_reviews", mode: "full" });
    } catch (error) {
        showToast("清除 DFT AI 审核失败：" + error.message, "error");
    }
}

function writingCardReviewMeta(item) {
    if (item && item.can_use_for_writing) {
        return { label: "可直接参考", className: "high", tip: "这张写作卡已满足当前写作使用条件。" };
    }
    const reasons = Array.isArray(item && item.blocked_reasons) ? item.blocked_reasons : [];
    if (reasons.length) {
        return { label: "需先核对", className: "medium", tip: "当前仍有待处理项：" + reasons.join("、") };
    }
    return { label: "草稿候选", className: "unknown", tip: "这是自动生成的写作草稿，使用前建议先核对证据。" };
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
        return '<div class="section-card"><h3>写作卡片</h3><div class="muted">暂无内容。</div></div>';
    }
    const intro = '<div class="section-card figure-audit-note"><h3>写作卡片说明</h3><div class="subtle">这里优先显示适合写作时直接阅读的短摘要。详细逻辑、证据链和阻塞原因默认折叠，避免一上来铺成整页长文本。</div></div>';
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
            writingCardLogicBlock("摘要写法", item && item.abstract_logic),
            writingCardLogicBlock("引言写法", item && item.introduction_logic),
            writingCardLogicBlock("讨论写法", item && item.discussion_logic)
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
                        '<div><h3 style="margin:0;">写作卡片 ' + (items.length > 1 ? (index + 1) : "") + '</h3><div class="knowledge-card-use">适合用来组织引言、摘要和讨论的写作骨架</div></div>' +
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
            '<div class="writing-card-summary-grid">' + (summaryBlocks || '<div class="muted">这张写作卡还没有生成可直接阅读的短摘要。</div>') + '</div>' +
            '<details class="knowledge-details">' +
                '<summary>展开写作逻辑与限制</summary>' +
                details +
                blocked +
            '</details>' +
        '</details>';
    }).join("");
}

function renderReadableCards(title, items, options) {
    options = options || {};
    if (!items || !items.length) {
        if (title === "电化学性能") {
            return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">当前没有结构化电化学性能数据。该模块来自实验/电化学信号的 Stage 2 抽取，或由 IDE AI 通过 import_analysis 回写；纯计算论文通常为空。</div></div>';
        }
        if (title === "机理声明") {
            return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">当前没有结构化机理声明。该模块来自 Stage 2 机理规则抽取，或由 IDE AI 通过 import_analysis 回写；写作卡只引用这些证据，不承载原始结构化数据。</div></div>';
        }
        return '<div class="section-card"><h3>' + esc(title) + '</h3><div class="muted">暂无内容。</div></div>';
    }
    if (title === "写作卡片") {
        return renderWritingCardsCompact(items);
    }
    const keySets = {
        "DFT 设置": ["software", "functional", "dispersion_correction", "pseudopotential", "cutoff_energy_ev", "cutoff_energy", "k_points", "convergence_settings", "vacuum_thickness_a", "vacuum_thickness"],
        "催化剂样本": ["name", "catalyst_type", "metal_centers", "coordination", "support", "synthesis_method", "evidence_text", "confidence"],
        "DFT 结果": ["catalyst", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "evidence_text", "confidence"],
        "候选 DFT 数据": ["candidate_status", "catalyst_sample_id", "active_site_instance_key", "catalyst", "material_identity", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "source_figure", "evidence_text", "confidence"],
        "DFT 候选结果": ["candidate_status", "catalyst_sample_id", "active_site_instance_key", "catalyst", "material_identity", "adsorbate", "energy_type", "property_type", "value", "unit", "reaction_step", "source_section", "source_figure", "evidence_text", "confidence"],
        "电化学性能": ["sulfur_loading", "sulfur_content", "electrolyte_sulfur_ratio", "capacity", "cycle_number", "rate", "decay_per_cycle", "evidence_text", "confidence"],
        "机理声明": ["claim_type", "claim_text", "key_species", "mechanism_direction", "evidence_text", "confidence"],
        "写作卡片": ["paper_type", "research_gap", "proposed_solution", "core_hypothesis", "evidence_text"],
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
        const heading = title + (items.length > 1 ? " " + (index + 1) : "");
        const itemType = CODEX_ITEM_TYPE_BY_CARD_TITLE[title];
        const action = codexItemActionHtml(itemType, item);
        const dftStatusChip = itemType === "dft_result" ? renderDftItemStatusChip(item) : "";
        const dftAiChip = itemType === "dft_result" ? renderDftAiOpinionChip(item) : "";
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
