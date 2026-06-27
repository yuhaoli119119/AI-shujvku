var DFT_BLOCK_REASON_LABELS = {
    missing_review: "尚未完成审核",
    unsafe_review: "审核状态不安全",
    missing_evidence: "缺证据引用/定位",
    missing_evidence_text: "缺证据原文",
    unsafe_locator: "PDF 定位不可靠",
    missing_material_identity: "缺材料/结构绑定"
};

function dftMissingReviewLabel(item) {
    if (!item) return DFT_BLOCK_REASON_LABELS.missing_review;
    const audits = uniqueDftReviewSubmissions(
        (Array.isArray(item.object_review_audits) ? item.object_review_audits : []).filter(dftOpinionHasAnchor)
    );
    if (audits.length === 0) return "尚无 AI 对象审核";
    if (audits.length === 1) return "仅有一个审核提交，等待下一轮";
    const classified = classifyDftAutomationRows([item]);
    if (classified.consensus.length) return "双 AI 一致，待系统写回";
    if (classified.conflicts.length) return "多 AI 意见有冲突，待裁决";
    return "多 AI 审核尚未形成可写回结论";
}

function dftBlockedReasonText(reasons, item) {
    if (item && item.dft_workflow_reason) return item.dft_workflow_reason;
    return (Array.isArray(reasons) ? reasons : []).map(function(reason) {
        if (reason === "missing_review") return dftMissingReviewLabel(item);
        return DFT_BLOCK_REASON_LABELS[reason] || reason;
    }).join("、");
}

function dftWorkflowMeta(item) {
    const stateValue = String(item && item.dft_workflow_state || "").trim();
    if (!stateValue && !(item && item.dft_workflow_label)) return null;
    const classByState = {
        exportable: "parsed",
        waiting_second_ai: "meta",
        missing_evidence_anchor: "failed",
        missing_material_binding: "failed",
        needs_third_ai: "failed",
        rejected_consensus_pending_write: "failed",
        rejected: "failed",
        unknown_blocked: "meta"
    };
    return {
        state: stateValue,
        label: item.dft_workflow_label || stateValue || "状态待判定",
        className: classByState[stateValue] || "meta",
        reason: item.dft_workflow_reason || "DFT workflow state is computed by the backend safety gate.",
        action: item.next_required_action || "none"
    };
}

function dftEvidencePayload(item) {
    return item && item.evidence_payload && typeof item.evidence_payload === "object" ? item.evidence_payload : {};
}

function dftSourceLabel(sourceType) {
    const value = String(sourceType || "unknown");
    const labels = {
        main_text: "正文",
        supplementary_information: "SI",
        supporting_reference: "支撑文献",
        unknown: "未知"
    };
    return labels[value] || value;
}

function dftEvidenceSourceMeta(item) {
    const payload = dftEvidencePayload(item);
    const location = payload.evidence_location && typeof payload.evidence_location === "object" ? payload.evidence_location : {};
    const sourceType = payload.source_document_type || location.source_document_type || "unknown";
    const locator = payload.source_locator || location.source_locator || location.locator || item.source_section || item.source_figure || "";
    const page = payload.page || location.page || "";
    const table = payload.table || location.table || "";
    const supporting = Array.isArray(payload.supporting_evidence) ? payload.supporting_evidence : [];
    return {
        sourceType: sourceType,
        sourceLabel: dftSourceLabel(sourceType),
        locator: locator || table,
        page: page,
        supportingCount: supporting.length,
        borrowed: sourceType === "supporting_reference" || payload.borrowed_from_reference === true
    };
}

function renderDftEvidenceSource(item) {
    const meta = dftEvidenceSourceMeta(item || {});
    const locatorText = [meta.locator, meta.page ? "p." + meta.page : ""].filter(Boolean).join(", ");
    return '<div class="knowledge-detail-block"><div class="knowledge-detail-title">证据来源</div>' +
        '<div class="knowledge-detail-text">' +
            '来源：' + esc(meta.sourceLabel) +
            (locatorText ? '；定位：' + esc(locatorText) : '') +
            '；重复证据：' + esc(meta.supportingCount) + ' 处' +
        '</div></div>' +
        (meta.borrowed
            ? '<div class="figure-warning" style="margin-top:10px;"><strong>支撑文献数据</strong><div>不计入当前主文献导出，需单独入库/核验原文。</div></div>'
            : '');
}

function renderDftItemSafety(item) {
    const safety = item && item.export_safety;
    const workflow = dftWorkflowMeta(item);
    if (!safety) {
        return '<div class="figure-warning" style="margin-top:12px;">' +
            '<strong>安全状态待加载</strong>' +
            '<div>' + esc(workflow && workflow.reason || "这条 DFT 记录暂未拿到导出安全门详情；仍可人工拒绝，接受入库时后端会重新校验。") + '</div>' +
            renderDftDecisionActions(item, false) +
        '</div>';
    }
    const exportable = safety.is_exportable === true || safety.eligible === true;
    const reasons = dftBlockedReasonText(safety.blocked_reasons, item);
    return '<div class="figure-warning" style="margin-top:12px;">' +
        '<strong>' + (exportable ? "已审核可导出" : "候选不可进入正式数据库") + '</strong>' +
        '<div>' + (exportable
            ? esc(workflow && workflow.reason || "该条记录已满足人工核验、证据原文和准确 PDF 定位要求。")
            : "阻断原因：" + esc(reasons || "待按 AI 协议和 PDF 证据检查")) + '</div>' +
        (workflow && workflow.action && workflow.action !== "none"
            ? '<div class="subtle" data-role="dft-next-required-action" style="margin-top:6px;">下一步：' + esc(workflow.action) + '</div>'
            : '') +
        renderDftDecisionActions(item, exportable) +
    '</div>';
}

function isFinalizedDftResult(item, exportable) {
    if (!item) return false;
    const safety = item.export_safety || {};
    if (exportable || safety.is_exportable === true || safety.eligible === true) return true;
    const candidateStatus = String(item.candidate_status || "").trim().toLowerCase();
    if (["ml_ready", "rejected", "human_confirmed", "citation_ready", "verified", "human_verified"].includes(candidateStatus)) {
        return true;
    }
    const workflowState = String(item.dft_workflow_state || "").trim().toLowerCase();
    if (["exportable", "rejected"].includes(workflowState)) return true;
    const reviewStatuses = String(safety.review_status || "")
        .toLowerCase()
        .split(",")
        .map(function(part) { return part.trim(); })
        .filter(Boolean);
    return reviewStatuses.includes("rejected");
}

function renderDftDecisionActions(item, exportable) {
    const resultId = dftResultId(item);
    if (!resultId) return "";
    if (isFinalizedDftResult(item, exportable)) return "";
    const safety = item && item.export_safety;
    const reviewStatuses = String((safety && safety.review_status) || "")
        .toLowerCase()
        .split(",")
        .map(function(part) { return part.trim(); })
        .filter(Boolean);
    const candidateStatus = String(item && item.candidate_status || "").trim().toLowerCase();
    const workflowState = String(item && item.dft_workflow_state || "").trim().toLowerCase();
    if (candidateStatus === "rejected" || workflowState === "rejected" || reviewStatuses.includes("rejected")) {
        return "";
    }
    if (exportable) {
        return "";
    }
    return '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">' +
        '<button class="btn primary small" type="button" onclick="acceptDftResult(\'' + escAttr(resultId) + '\')">接受入库</button>' +
        '<button class="btn ghost small" type="button" onclick="rejectDftResult(\'' + escAttr(resultId) + '\')">拒绝</button>' +
    '</div>';
}

function dftItemStatusMeta(item) {
    const workflow = dftWorkflowMeta(item);
    if (workflow) {
        return {
            label: workflow.label,
            className: workflow.className,
            title: workflow.reason
        };
    }
    const safety = item && item.export_safety;
    if (!safety) {
        return {
            label: "状态待加载",
            className: "meta",
            title: "这条 DFT 记录的入库安全状态还没有加载完成。"
        };
    }
    const exportable = safety.is_exportable === true || safety.eligible === true;
    const reviewStatuses = String(safety.review_status || "")
        .toLowerCase()
        .split(",")
        .map(function(part) { return part.trim(); })
        .filter(Boolean);
    if (reviewStatuses.includes("rejected")) {
        return {
            label: "已拒绝",
            className: "muted",
            title: "这条 DFT 候选已经被拒绝，不再属于待处理项。"
        };
    }
    const blockedReasons = Array.isArray(safety.blocked_reasons) ? safety.blocked_reasons : [];
    const reasons = dftBlockedReasonText(blockedReasons, item);
    return {
        label: exportable ? "可导出" : "需处理",
        className: exportable ? "parsed" : "meta",
        title: exportable
            ? "这条 DFT 候选已通过当前导出安全门。"
            : ("阻断原因：" + (reasons || "待按 AI 协议和 PDF 证据检查"))
    };
}

function renderDftItemStatusChip(item) {
    const meta = dftItemStatusMeta(item);
    if (!meta) return "";
    return '<span class="status-chip ' + meta.className + '" title="' + escAttr(meta.title) + '">' + esc(meta.label) + '</span>';
}

function dftAiOpinionMeta(item) {
    if (item && item.ai_review_display_status) {
        return {
            label: item.ai_review_display_label || dftAiReviewDisplayFallbackLabel(item.ai_review_display_status),
            className: item.ai_review_display_class || dftAiReviewDisplayFallbackClass(item.ai_review_display_status),
            title: item.ai_review_display_reason || "AI 审核展示状态由后端导出安全门和对象审核记录统一计算。"
        };
    }
    const audits = item && Array.isArray(item.object_review_audits) ? item.object_review_audits : [];
    if (!audits.length) {
        const candidateStatus = String(item && item.candidate_status || "").trim().toLowerCase();
        const importPolicy = String(item && item.evidence_payload && item.evidence_payload.import_policy || "").trim().toLowerCase();
        if (candidateStatus === "new_candidate" || importPolicy === "new_candidate_unverified_dft_result") {
            return {
                label: "待对象审核",
                className: "meta",
                title: "这条 DFT 数据由 AI 新发现并写入候选队列，必须完成对象级证据审核后才能进入正式数据库。"
            };
        }
        return {
            label: "无 AI 意见",
            className: "ok",
            title: "这条 DFT 没有对象级 AI 修正意见；若同时显示可导出，表示已通过当前安全门。"
        };
    }
    const sources = {};
    let hasReject = false;
    let hasProposed = false;
    let hasPass = false;
    let hasNeedsHuman = false;
    audits.forEach(function(audit) {
        const source = audit.source_label || audit.source || "unknown";
        sources[source] = true;
        const decision = dftOpinionDecision(audit);
        if (isNegativeDftDecision(decision)) hasReject = true;
        if (decision === "PROPOSED") hasProposed = true;
        if (decision === "PASS") hasPass = true;
        if (decision === "NEEDS_HUMAN") hasNeedsHuman = true;
    });
    const sourceCount = Object.keys(sources).length;
    const safety = item && item.export_safety;
    const exportable = item && (
        item.is_exportable === true ||
        (safety && (safety.is_exportable === true || safety.eligible === true))
    );
    const hasUnresolvedConflicts = Number(item && item.conflict_count || 0) > 0 ||
        Boolean(item && Array.isArray(item.field_conflicts) && item.field_conflicts.length);
    if (hasUnresolvedConflicts && hasProposed) {
        return {
            label: "AI 已提修正",
            className: "meta",
            title: "当前仍有字段冲突未解，AI 修正意见还不能视为已采纳。"
        };
    }
    if (exportable && hasReject) {
        return {
            label: "AI 意见已收敛",
            className: "ok",
            title: "这条 DFT 已通过导出安全门；历史拒绝意见已被后续修正、替代或安全门结果收敛，不再阻断当前记录。"
        };
    }
    if (exportable && hasProposed) {
        return {
            label: "已采纳 AI 修正",
            className: "ok",
            title: "这条 DFT 已采纳 AI 修正意见，并已通过导出安全门。"
        };
    }
    if (exportable && hasPass) {
        return {
            label: sourceCount >= 2 ? "AI 字段通过" : "AI 确认字段",
            className: "ok",
            title: "AI 审核意见不阻断当前导出安全门。"
        };
    }
    if (hasReject && (hasPass || hasProposed)) {
        return {
            label: "AI 冲突",
            className: "failed",
            title: "至少一个审核提交建议拒绝，同时存在保留/通过意见，必须人工裁决。"
        };
    }
    if (hasReject) {
        return {
            label: sourceCount >= 2 ? "AI 一致拒绝" : "AI 建议拒绝",
            className: "failed",
            title: "AI 审核意见认为这条 DFT 候选应拒绝或删除。"
        };
    }
    if (hasNeedsHuman) {
        return {
            label: "AI 无法确认",
            className: "meta",
            title: "AI 无法从当前证据确认这条 DFT 候选，需要下一轮审核提交补证据或人工裁决。"
        };
    }
    if (hasProposed && sourceCount >= 2) {
        return {
            label: "AI 修正待采纳",
            className: "meta",
            title: "已有 AI 修正意见和字段确认，但尚未形成可写回的一致提交；需要采纳修正并跑安全门。"
        };
    }
    if (hasProposed) {
        return {
            label: "AI 已提修正",
            className: "meta",
            title: "AI 已提出材料、单位、证据定位等修正，尚未采纳为最终字段。"
        };
    }
    if (hasPass) {
        return {
            label: sourceCount >= 2 ? "AI 字段通过" : "AI 确认字段",
            className: "ok",
            title: "AI 只确认了部分字段，不等于整条 DFT 候选已经满足入库条件。"
        };
    }
    return {
        label: "AI 意见待判定",
        className: "meta",
        title: "这条 DFT 候选有 AI 审核记录，但系统无法归类为通过、修正或拒绝。"
    };
}

function dftAiReviewDisplayFallbackLabel(status) {
    const value = String(status || "").trim();
    return {
        no_ai_opinion: "无 AI 意见",
        exportable_with_historical_reject: "AI 意见已收敛",
        converged_adopted: "已采纳 AI 修正",
        pass_exportable: "AI 字段通过",
        conflict: "AI 冲突",
        rejected: "AI 一致拒绝",
        reject_suggested: "AI 建议拒绝",
        needs_human: "AI 无法确认",
        proposed: "AI 已提修正",
        pass_partial: "AI 字段通过",
        unknown: "AI 意见待判定"
    }[value] || "AI 意见待判定";
}

function dftAiReviewDisplayFallbackClass(status) {
    const value = String(status || "").trim();
    if (["conflict", "rejected", "reject_suggested"].includes(value)) return "failed";
    if (["exportable_with_historical_reject", "converged_adopted", "pass_exportable", "pass_partial", "no_ai_opinion"].includes(value)) return "ok";
    return "meta";
}

function renderDftAiOpinionChip(item) {
    const meta = dftAiOpinionMeta(item);
    if (!meta) return "";
    return '<span class="status-chip ' + meta.className + '" title="' + escAttr(meta.title) + '">' + esc(meta.label) + '</span>';
}
