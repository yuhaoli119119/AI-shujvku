function renderPaperList() {
    const container = $("paperList");
    const meta = $("paperListMeta");
    if (meta) {
        const total = Number(state.currentLibraryTotal || 0);
        const shown = state.papers.length;
        const rangeStart = shown ? state.currentOffset + 1 : 0;
        const rangeEnd = state.currentOffset + shown;
        meta.textContent = total
            ? "全库 " + total + " 篇 · 本页 " + rangeStart + "-" + rangeEnd
            : "本页 " + shown + " 篇";
    }
    if (!container) return;
    if (!state.papers.length) {
        container.innerHTML = '<div class="list-empty">当前条件下没有文献</div>';
        if (state.currentLibraryTotal === 0) {
            renderLibraryEmptyState();
        } else {
            renderNoSelectionState();
        }
        return;
    }

    function workflowClass(status) {
        if (["Human_Confirmed", "ML_Ready", "Citation_Ready", "Human_Complete", "DB_Ready"].includes(status)) return "full";
        if (["Parsed_Material_Ready", "Initial_Parsed", "AI_Rescanned"].includes(status)) return "parsed";
        if (["Needs_Human_Confirmation", "Gemini_Flagged", "Evidence_Insufficient", "Rejected", "Suspected_Missing", "Unparsed"].includes(status)) return "meta";
        return "meta";
    }

    function workflowMeta(status) {
        const mapping = {
            Imported: "已导入",
            Quality_Checked: "已检质量",
            Parsed_Material_Ready: "材料已就绪",
            Unparsed: "未解析",
            Initial_Parsed: "初步解析",
            Suspected_Missing: "疑似漏提",
            AI_Rescanned: "AI已重扫",
            Human_Complete: "人工已确认",
            DB_Ready: "可入库",
            Codex_Candidate: "系统候选",
            Gemini_Verified: "AI核验",
            Gemini_Revised: "AI修订",
            Gemini_Flagged: "AI标红",
            Evidence_Insufficient: "证据不足",
            Rejected: "已废弃",
            Needs_Human_Confirmation: "待人工审核"
        };
        return mapping[status] || status || "Imported";
    }

    function qualityLabel(status) {
        const mapping = {
            A_text_readable: "A 可读",
            B_text_partial: "B 部分可读",
            C_scan_clear: "C 扫描清晰",
            D_scan_unclear: "D 扫描不清",
            Broken: "文件异常",
            Good: "质量良好"
        };
        return mapping[status] || status || "";
    }

    function qualityChipClass(status) {
        if (status === "A_text_readable" || status === "Good") return "parsed";
        if (status === "B_text_partial" || status === "C_scan_clear") return "meta";
        if (status === "D_scan_unclear" || status === "Broken") return "failed";
        return "meta";
    }

    function dftCompletenessLabel(status) {
        const mapping = {
            Unparsed: "未解析",
            Initial_Parsed: "初步解析",
            Suspected_Missing: "疑似漏提",
            Human_Complete: "人工确认",
            DB_Ready: "可入库"
        };
        return mapping[status] || status || "";
    }

    function compactPdfStatus(paper) {
        const status = paper && paper.pdf_artifact_status && typeof paper.pdf_artifact_status === "object" ? paper.pdf_artifact_status : {};
        return {
            hasPdf: (paper && paper.pdf_exists === true) || paperHasPdf(paper) || status.pdf_exists === true,
            pathKind: paper && paper.pdf_path_kind !== undefined && paper.pdf_path_kind !== null && paper.pdf_path_kind !== ""
                ? paper.pdf_path_kind
                : (status.pdf_path_kind || "unknown"),
            size: paper && paper.pdf_file_size !== undefined && paper.pdf_file_size !== null && paper.pdf_file_size !== ""
                ? paper.pdf_file_size
                : status.pdf_file_size,
            blockers: Array.isArray(status.blocking_errors) ? status.blocking_errors : []
        };
    }

    function compactPdfDisplayState(paper) {
        const pdf = compactPdfStatus(paper);
        const workflowStatus = String(paper && paper.workflow_status || "").trim();
        const quality = String(paper && paper.pdf_quality_status || "").trim().toLowerCase();
        const broken = quality === "broken";
        const blocked = Array.isArray(pdf.blockers) && pdf.blockers.length > 0;
        const progress = compactManualReviewProgress(paper);
        const hasInitialParse = !!(
            paper.has_parsed_content ||
            (paper && (paper.tei_path || paper.markdown_path)) ||
            (paper && paper.counts && Number(paper.counts.sections || 0) > 0) ||
            (paper && paper.counts && Number(paper.counts.figures || 0) > 0) ||
            (paper && paper.counts && Number(paper.counts.dft_results || 0) > 0) ||
            progress.figures ||
            progress.dft ||
            progress.content ||
            [
                "Imported",
                "Quality_Checked",
                "Parsed_Material_Ready",
                "Initial_Parsed",
                "AI_Rescanned",
                "Suspected_Missing",
                "Human_Complete",
                "DB_Ready",
                "Human_Confirmed",
                "ML_Ready",
                "Citation_Ready",
                "Needs_Human_Confirmation",
                "Gemini_Flagged",
                "Gemini_Verified",
                "Gemini_Revised",
                "Evidence_Insufficient",
                "Rejected"
            ].includes(workflowStatus)
        );
        const unusable = pdf.hasPdf && (broken || blocked || !hasInitialParse);
        return {
            pdf: pdf,
            workflowStatus: workflowStatus,
            hasInitialParse: hasInitialParse,
            unusable: unusable,
            label: !pdf.hasPdf ? "无 PDF" : (unusable ? "PDF 不可用" : "PDF 可用"),
            chipClass: !pdf.hasPdf ? "meta" : (unusable ? "failed" : "full"),
            summaryText: !pdf.hasPdf
                ? "当前文献没有可用 PDF 文件。"
                : (unusable
                    ? "PDF 存在，但文件状态异常、被阻塞，或系统尚未完成初步解析。"
                    : "PDF 文件存在，且系统已经完成初步解析。")
        };
    }

    function compactPdfChip(paper) {
        const display = compactPdfDisplayState(paper);
        const pdf = display.pdf;
        const title = [
            display.summaryText,
            "path kind: " + pdf.pathKind,
            pdf.size !== undefined && pdf.size !== null && pdf.size !== "" ? "size: " + pdf.size + " bytes" : null,
            display.workflowStatus ? "workflow: " + display.workflowStatus : null,
            paper.pdf_quality_status ? "quality: " + paper.pdf_quality_status : null,
            pdf.blockers.length ? "blockers: " + pdf.blockers.join(", ") : null
        ].filter(Boolean).join("\n");
        return '<span class="status-chip ' + display.chipClass + '" title="' + esc(title) + '">' + esc(display.label) + '</span>';
    }

    function compactManualReviewProgress(paper) {
        const source = paper && paper.manual_review_progress && typeof paper.manual_review_progress === "object"
            ? paper.manual_review_progress
            : {};
        const normalize = function (key) {
            const value = source[key];
            if (value && typeof value === "object") return !!value.completed;
            return !!value;
        };
        return {
            figures: normalize("figures"),
            dft: normalize("dft"),
            content: normalize("content")
        };
    }

    function compactModuleProgressChip(label, completed, title) {
        return '<span class="status-chip ' + (completed ? "full" : "none") + '" title="' + esc(title) + '">' + esc(label) + '</span>';
    }

    function renderConflictChip(activeCount, totalCount, activeLabel, historyLabel, titleActive, titleHistory) {
        const active = Number(activeCount || 0);
        const total = Number(totalCount || 0);
        if (active > 0) {
            return '<span class="status-chip failed" title="' + esc(titleActive) + '">' + esc(activeLabel + " " + active) + '</span>';
        }
        if (total > 0) {
            return '<span class="status-chip none" title="' + esc(titleHistory) + '">' + esc(historyLabel + " " + total) + '</span>';
        }
        return "";
    }

    function isArxivPreprint(paper) {
        const journal = String(paper && paper.journal || "").trim().toLowerCase();
        const doi = String(paper && paper.doi || "").trim().toLowerCase();
        return journal === "arxiv" || doi.startsWith("10.48550/arxiv.");
    }

    function renderVenueMeta(paper) {
        if (isArxivPreprint(paper)) {
            return "预印本: arXiv";
        }
        if (paper && paper.journal) {
            return "期刊: " + paper.journal;
        }
        return "期刊: 未知";
    }

    const tbodyHtml = state.papers.map(function(paper, idx) {
        const stablePaperId = paper.paper_id || paper.id;
        const active = stablePaperId === state.selectedPaperId ? " active" : "";
        const titleLine = esc(paper.title_zh || paper.title || "未命名文献");
        const originalTitle = paper.title_zh && paper.title ? '<div class="paper-original-title" style="margin-top:2px;" title="' + esc(paper.title) + '">' + esc(paper.title) + '</div>' : '';
        const pdfSizeStr = paper.pdf_size ? ' | ' + formatFileSize(paper.pdf_size) : '';
        const displayCode = paper.paper_code || "";
        const metaLine = esc(renderVenueMeta(paper)) + (paper.doi ? ' | DOI: ' + esc(paper.doi) : '') + pdfSizeStr;

        const progress = compactManualReviewProgress(paper);
        const dftConflictCount = Number(paper.dft_review_conflict_count || 0);

        return (
            '<tr class="paper-row' + active + '" data-id="' + stablePaperId + '" onclick="selectPaperById(\'' + stablePaperId + '\')" ondblclick="openWorkspaceForPaper(\'' + stablePaperId + '\')">' +
                '<td class="col-divider" style="text-align:center; color:var(--color-text-secondary);">' + esc(displayCode || (idx + 1)) + '</td>' +
                '<td style="text-align:center; color:var(--color-text-secondary);">' + esc(paper.year || "-") + '</td>' +
                '<td style="text-align:center; color:var(--color-text-secondary); font-weight:600;">' + esc(paperTypeLabel(paper.paper_type)) + '</td>' +
                '<td class="col-divider" style="text-align:center; vertical-align:middle;">' + renderImpactFactor(paper) + '</td>' +
                '<td class="col-divider" style="text-align:left; padding-left:16px;">' +
                    '<div class="paper-title" title="' + esc(paper.title || "未命名文献") + '">' + titleLine + '</div>' +
                    originalTitle +
                    '<div class="paper-meta" style="margin-top:4px;">' + metaLine + '</div>' +
                '</td>' +
                '<td class="col-divider" style="text-align:center;">' +
                    '<div style="display:flex; flex-direction:column; gap:6px; align-items:center; justify-content:center;">' +
                        '<div style="display:flex; flex-wrap:wrap; gap:4px; align-items:center; justify-content:center;">' +
                            compactPdfChip(paper) +
                        '</div>' +
                        '<div style="display:flex; flex-wrap:wrap; gap:4px; align-items:center; justify-content:center;">' +
                            compactModuleProgressChip("图表", progress.figures, progress.figures ? "图表部分已标记完成。" : "图表部分尚未标记完成。") +
                            compactModuleProgressChip("DFT", progress.dft, progress.dft ? "DFT 部分已标记完成。" : "DFT 部分尚未标记完成。") +
                            compactModuleProgressChip("内容解析", progress.content, progress.content ? "内容解析部分已标记完成。" : "内容解析部分尚未标记完成。") +
                            renderConflictChip(
                                dftConflictCount,
                                0,
                                "DFT 冲突",
                                "DFT 已处理冲突",
                                "当前 DFT 审计仍有未收口冲突。",
                                "这篇文献历史上出现过 DFT 冲突，但当前已处理完。"
                            ) +
                        '</div>' +
                    '</div>' +
                '</td>' +
                '<td style="text-align:center;">' +
                    '<div class="paper-meta" style="display:flex; flex-wrap:wrap; gap:4px 12px; font-size:12px; line-height:1.5; justify-content:center;">' +
                        '<span title="线索/章节数量">章节: <strong>' + (paper.counts && paper.counts.sections ? paper.counts.sections : 0) + '</strong></span>' +
                        '<span title="解析图表数量">图表: <strong>' + (paper.counts && paper.counts.figures ? paper.counts.figures : 0) + '</strong></span>' +
                        '<span title="DFT提取数据">DFT: <strong>' + (paper.counts && paper.counts.dft_results ? paper.counts.dft_results : 0) + '</strong></span>' +
                        '<span title="关联写作卡片">写作卡: <strong>' + (paper.counts && paper.counts.writing_cards ? paper.counts.writing_cards : 0) + '</strong></span>' +
                    '</div>' +
                '</td>' +
            '</tr>'
        );
    }).join("");

    container.innerHTML = '<table class="paper-table">' +
        '<thead><tr>' +
            '<th class="col-divider" style="width:75px; text-align:center;">#</th>' +
            '<th style="width:5%; text-align:center;">年份</th>' +
            '<th style="width:6%; text-align:center;">类型</th>' +
            '<th class="col-divider" style="width:6%; text-align:center;">IF</th>' +
            '<th class="col-divider" style="text-align:center;">文献标题</th>' +
            '<th class="col-divider" style="width:18%; text-align:center;">文献状态</th>' +
            '<th style="width:17%; text-align:center;">数据统计</th>' +
        '</tr></thead>' +
        '<tbody>' + tbodyHtml + '</tbody>' +
    '</table>';
}

function renderImpactFactor(paper) {
    const impactFactor = Number(paper && paper.impact_factor);
    if (paper && paper.impact_factor !== null && paper.impact_factor !== undefined && Number.isFinite(impactFactor)) {
        const metadata = [paper.impact_factor_source, paper.impact_factor_year].filter(function(value) {
            return value !== null && value !== undefined && String(value).trim() !== "";
        }).join(" · ");
        const title = metadata ? ' title="' + esc(metadata) + '"' : "";
        return '<span' + title + ' style="color:var(--color-text-primary);font-size:13px;font-weight:600;">' + esc(String(paper.impact_factor)) + '</span>';
    }
    return '<span style="color:var(--color-text-tertiary);font-size:12px;">-</span>';
}

function formatFileSize(bytes) {
    if (!bytes || isNaN(bytes)) return "";
    const mb = bytes / (1024 * 1024);
    if (mb >= 1) return mb.toFixed(1) + " MB";
    const kb = bytes / 1024;
    return kb.toFixed(0) + " KB";
}

function normalizePaperTypeLabel(value) {
    const raw = String(value || "").trim();
    if (!raw || raw.toLowerCase() === "unknown") return "未知类型";
    return raw;
}

function renderPaperListSkeleton() {
    const container = $("paperList");
    if (!container) return;
    let skeletonHtml = "";
    for (let i = 0; i < 5; i++) {
        skeletonHtml +=
            '<div class="skeleton-card">' +
                '<div class="skeleton skeleton-title"></div>' +
                '<div class="skeleton skeleton-meta"></div>' +
                '<div class="skeleton skeleton-badge"></div>' +
            '</div>';
    }
    container.innerHTML = skeletonHtml;
}
