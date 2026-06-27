function isNegativeDftDecision(decision) {
    const value = String(decision || "").trim().toUpperCase();
    return ["REJECT", "REJECTED", "BLOCK", "DENY", "DROP"].includes(value);
}

function sortDftAuditsNewestFirst(a, b) {
    return String(b && b.created_at || "").localeCompare(String(a && a.created_at || ""));
}

function importedDftAcceptanceOpinions(item) {
    const audits = item && Array.isArray(item.object_review_audits) ? item.object_review_audits.slice() : [];
    const hasWholeRowProposed = audits.some(function(audit) {
        return audit &&
            String(audit.decision || "").trim().toUpperCase() === "PROPOSED" &&
            String(audit.field_name || "").trim() === "dft_results";
    });
    const seen = {};
    return audits
        .filter(function(audit) {
            const decision = dftOpinionDecision(audit);
            if (!audit || isNegativeDftDecision(audit.decision) || !decision) return false;
            if (!["PASS", "PROPOSED"].includes(decision)) return false;
            if (
                hasWholeRowProposed &&
                decision === "PASS" &&
                String(audit.field_name || "").trim() === "value"
            ) {
                return false;
            }
            return true;
        })
        .sort(function(a, b) {
            const aWholeRow = String(a.field_name || "") === "dft_results" ? 0 : 1;
            const bWholeRow = String(b.field_name || "") === "dft_results" ? 0 : 1;
            if (aWholeRow !== bWholeRow) return aWholeRow - bWholeRow;
            return sortDftAuditsNewestFirst(a, b);
        })
        .filter(function(audit) {
            const key = [
                audit.field_name || "",
                audit.decision || "",
                JSON.stringify(audit.corrected_value == null ? "" : audit.corrected_value),
                audit.reason || ""
            ].join("|");
            if (seen[key]) return false;
            seen[key] = true;
            return true;
        });
}

function dftOpinionDecision(audit) {
    const decision = String(audit && audit.decision || "").trim().toUpperCase();
    if (["CONFIRMED", "ACCEPT", "ACCEPTED", "APPROVED", "VERIFIED", "OK"].includes(decision)) return "PASS";
    if (["CONFIRMED_WITH_CORRECTIONS", "CORRECTED", "REVISE", "REVISION"].includes(decision)) return "PROPOSED";
    return decision;
}

function dftOpinionSource(audit) {
    return String(audit && (audit.source_label || audit.source || "unknown") || "unknown");
}

function dftOpinionHasAnchor(audit) {
    const loc = audit && audit.evidence_location;
    if (!loc || typeof loc !== "object") return false;
    const page = loc.page;
    const quoted = loc.quoted_text || loc.evidence_text;
    return page != null && String(page).trim() && quoted != null && String(quoted).trim();
}

function dftOpinionHasAnyLocation(audit) {
    const loc = audit && audit.evidence_location;
    if (!loc) return false;
    if (typeof loc === "string") return Boolean(loc.trim());
    if (typeof loc !== "object") return false;
    return ["page", "section", "section_title", "figure", "figure_id", "table", "table_id", "quoted_text", "evidence_text", "bbox"]
        .some(function(key) {
            return loc[key] != null && String(loc[key]).trim();
        });
}

function dftWholeRowProposal(row) {
    const audits = row && Array.isArray(row.object_review_audits) ? row.object_review_audits.slice() : [];
    return audits
        .filter(function(audit) {
            return ["PROPOSED", "REVISE", "NEW_CANDIDATE"].includes(dftOpinionDecision(audit)) &&
                String(audit.field_name || "").trim() === "dft_results" &&
                audit.corrected_value &&
                typeof audit.corrected_value === "object" &&
                dftOpinionHasAnchor(audit);
        })
        .sort(sortDftAuditsNewestFirst)[0] || null;
}

function normalizeDftDecisionValue(value, unit) {
    const numeric = Number(value);
    const rawUnit = String(unit || "").trim();
    const unitKey = rawUnit.toLowerCase().replace(/\s+/g, "");
    if (!Number.isFinite(numeric)) return { value: null, unit: rawUnit };
    if (["e", "|e|", "electron", "electrons"].includes(unitKey)) {
        return { value: numeric, unit: "e" };
    }
    if (unitKey === "mev") return { value: numeric / 1000, unit: "eV" };
    if (unitKey === "ev") return { value: numeric, unit: "eV" };
    if (unitKey.includes("gpu")) {
        const asciiKey = Array.from(unitKey).filter(function(ch) { return ch.charCodeAt(0) < 128; }).join("");
        const scaled = ["10^3", "x10^3", "103"].some(function(marker) { return asciiKey.includes(marker); }) ||
            (asciiKey.startsWith("10") && asciiKey !== "gpu");
        return { value: scaled ? numeric * 1000 : numeric, unit: "GPU" };
    }
    return { value: numeric, unit: rawUnit };
}

function dftAuditNormalizedTarget(row, audit) {
    const corrected = audit && audit.corrected_value;
    if (corrected && typeof corrected === "object") {
        return normalizeDftDecisionValue(corrected.value, corrected.unit || row.unit);
    }
    return normalizeDftDecisionValue(corrected == null ? row.value : corrected, row.unit);
}

function dftSameNormalizedValue(left, right) {
    if (!left || !right || left.value == null || right.value == null) return false;
    if (String(left.unit || "").toLowerCase() !== String(right.unit || "").toLowerCase()) return false;
    const tolerance = Math.max(1e-9, Math.abs(left.value) * 1e-6);
    return Math.abs(left.value - right.value) <= tolerance;
}

function dftAuditMaterialIdentity(audit) {
    const corrected = audit && audit.corrected_value;
    const value = corrected && typeof corrected === "object"
        ? (corrected.material_identity || corrected.material || corrected.catalyst || corrected.structure_name)
        : "";
    return String(value || audit && (audit.normalized_material || audit.normalized_material_or_catalyst) || "")
        .trim()
        .toLowerCase();
}

function dftIndependentOpinionsAgree(row, opinions) {
    const normalized = (opinions || []).map(function(audit) { return dftAuditNormalizedTarget(row, audit); });
    if (normalized.length < 2 || !normalized.every(function(item) {
        return dftSameNormalizedValue(normalized[0], item);
    })) return false;
    const materials = (opinions || []).map(dftAuditMaterialIdentity).filter(Boolean);
    if (materials.length < 2) return true;
    return materials.every(function(material) {
        return material === materials[0] || material.includes(materials[0]) || materials[0].includes(material);
    });
}

function dftSupportingValuePass(row, proposal) {
    if (!proposal) return null;
    const proposedTarget = dftAuditNormalizedTarget(row, proposal);
    const proposalSource = dftOpinionSource(proposal);
    const audits = row && Array.isArray(row.object_review_audits) ? row.object_review_audits : [];
    return audits.find(function(audit) {
        if (!["PASS", "PROPOSED", "REVISE", "NEW_CANDIDATE"].includes(dftOpinionDecision(audit))) return false;
        if (!["value", "dft_results"].includes(String(audit.field_name || "").trim())) return false;
        if (dftOpinionSource(audit) === proposalSource) return false;
        if (!dftOpinionHasAnchor(audit)) return false;
        return dftSameNormalizedValue(proposedTarget, dftAuditNormalizedTarget(row, audit));
    }) || null;
}

function dftExtractDuplicateTargetId(audit) {
    const text = [
        audit && audit.duplicate_of,
        audit && audit.reason,
        audit && audit.corrected_value && audit.corrected_value.duplicate_of
    ].filter(Boolean).join(" ");
    const match = text.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    return match ? match[0] : "";
}

function dftAnyItemById(resultId) {
    if (!state.selectedPaper || !resultId) return null;
    const items = dftResultsWithSafety(state.selectedPaper);
    return items.find(function(item) { return dftResultId(item) === String(resultId); }) || null;
}

function dftIsAutoRejectDuplicate(row) {
    const audits = row && Array.isArray(row.object_review_audits) ? row.object_review_audits : [];
    const rejectAudit = audits.find(function(audit) {
        return isNegativeDftDecision(audit && audit.decision) && /duplicate/i.test(String(audit.reason || audit.duplicate_of || ""));
    });
    if (!rejectAudit) return false;
    const duplicateId = dftExtractDuplicateTargetId(rejectAudit);
    const target = dftAnyItemById(duplicateId);
    if (!target) return false;
    const left = normalizeDftDecisionValue(row.value, row.unit);
    const right = normalizeDftDecisionValue(target.value, target.unit);
    return dftSameNormalizedValue(left, right);
}

function classifyDftAutomationRows(rows) {
    const result = { consensus: [], conflicts: [], newReview: [], autoAccept: [], autoReject: [] };
    (rows || []).forEach(function(row) {
        if (!row || row.is_exportable === true) return;
        const workflowState = String(row.dft_workflow_state || "").trim();
        if (workflowState === "needs_third_ai") {
            result.conflicts.push(row);
            return;
        }
        if (workflowState === "waiting_second_ai" || workflowState === "missing_evidence_anchor" || workflowState === "missing_material_binding") {
            result.newReview.push(row);
            return;
        }
        if (workflowState === "rejected_consensus_pending_write") {
            result.consensus.push(row);
            return;
        }
        const submissions = uniqueDftReviewSubmissions(
            (Array.isArray(row.object_review_audits) ? row.object_review_audits : [])
                .filter(dftOpinionHasAnchor)
                .sort(sortDftAuditsNewestFirst)
        );
        const repairReasons = new Set(["missing_material_identity", "missing_evidence", "missing_evidence_text", "unsafe_locator"]);
        const blockedReasons = Array.isArray(row.blocked_reasons) ? row.blocked_reasons : [];
        const hasReject = submissions.some(function(audit) { return isNegativeDftDecision(audit && audit.decision); });
        const hasPositive = submissions.some(function(audit) {
            const decision = dftOpinionDecision(audit);
            return ["PASS", "PROPOSED", "REVISE", "NEW_CANDIDATE"].includes(decision);
        });
        const allReject = submissions.length > 0 && submissions.every(function(audit) {
            return isNegativeDftDecision(audit && audit.decision);
        });
        if (hasReject && hasPositive) {
            result.conflicts.push(row);
            return;
        }
        if (submissions.length < 2 || blockedReasons.some(function(reason) { return repairReasons.has(reason); })) {
            result.newReview.push(row);
            return;
        }
        if (dftIsAutoRejectDuplicate(row)) {
            result.consensus.push(row);
            return;
        }
        if (allReject) {
            result.consensus.push(row);
            return;
        }
        const proposal = dftWholeRowProposal(row);
        const support = dftSupportingValuePass(row, proposal);
        if (proposal && support && dftIndependentOpinionsAgree(row, submissions)) {
            result.consensus.push(row);
            return;
        }
        if (proposal) {
            result.conflicts.push(row);
            return;
        }
        if (dftIndependentOpinionsAgree(row, submissions)) {
            result.consensus.push(row);
            return;
        }
        result.conflicts.push(row);
    });
    return result;
}

function uniqueDftReviewSubmissions(audits) {
    const seenCandidateIds = new Set();
    return (audits || []).filter(function(audit) {
        const candidateId = String(audit && audit.candidate_id || "").trim();
        if (!candidateId) return true;
        if (seenCandidateIds.has(candidateId)) return false;
        seenCandidateIds.add(candidateId);
        return true;
    });
}
