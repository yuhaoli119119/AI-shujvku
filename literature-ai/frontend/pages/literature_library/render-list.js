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
        if (["Needs_Human_Confirmation", "Gemini_Flagged", "Evidence_Insufficient", "Rejected", "Suspected_Missing", "Unparsed"].includes(status)) return "meta";
        return "meta";
    }

    function workflowMeta(status) {
        const mapping = {
            Imported: "已导入", Quality_Checked: "已检质量", Parsed_Material_Ready: "材料已就绪",
            Unparsed: "未解析", Initial_Parsed: "初步解析", Suspected_Missing: "疑似漏提",
            AI_Rescanned: "AI已重扫", Human_Complete: "人工已确认", DB_Ready: "可入库",
            Codex_Candidate: "系统候选", Gemini_Verified: "Gemini核验", Gemini_Revised: "Gemini修订",
            Gemini_Flagged: "Gemini标红", Evidence_Insufficient: "证据不足", Rejected: "已废弃",
            Needs_Human_Confirmation: "待人工审核"
        };
        return mapping[status] || status || "Imported";
    }

    const tbodyHtml = state.papers.map(function(paper, idx) {
        const active = paper.id === state.selectedPaperId ? " active" : "";
        const titleLine = esc(paper.title_zh || paper.title || "未命名文献");
        const originalTitle = paper.title_zh && paper.title ? '<div class="paper-original-title" style="margin-top:2px;" title="' + esc(paper.title) + '">' + esc(paper.title) + '</div>' : '';
        const pdfSizeStr = paper.pdf_size ? ' | ' + formatFileSize(paper.pdf_size) : '';
        const metaLine = esc(paper.journal || "未知期刊") + (paper.doi ? ' | DOI: ' + esc(paper.doi) : '') + pdfSizeStr;
        
        let wfChip = "";
        if (paper.workflow_status && paper.workflow_status !== "Imported") {
            wfChip = '<span class="status-chip ' + workflowClass(paper.workflow_status) + '" title="审核进度: ' + esc(paper.workflow_status) + '">' + esc(workflowMeta(paper.workflow_status)) + '</span>';
        }
        
        let dftCandChip = "";
        if (paper.has_dft_candidates) {
            dftCandChip = '<span class="status-chip meta" title="系统检测到有需要进行人工审核或确认的 DFT 候选记录" style="border-color: rgba(239, 68, 68, 0.3); color: #ef4444; background: #fef2f2;">待审DFT候选</span>';
        }

        return (
            '<tr class="paper-row' + active + '" data-id="' + paper.id + '" onclick="selectPaperById(\'' + paper.id + '\')" ondblclick="openWorkspaceForPaper(\'' + paper.id + '\')">' +
                '<td style="text-align:center; color:var(--color-text-secondary);">' + (paper.serial_number ? formatSerialNumber(paper.serial_number) : (idx + 1)) + '</td>' +
                '<td style="text-align:center; color:var(--color-text-secondary);">' + esc(paper.year || "-") + '</td>' +
                '<td style="text-align:center; color:var(--color-text-secondary); font-weight:600;">' + esc(paperTypeLabel(paper.paper_type)) + '</td>' +
                '<td style="text-align:center; vertical-align:middle;">' + renderImpactFactor(paper) + '</td>' +
                '<td class="col-divider" style="text-align:left; padding-left:16px;">' +
                    '<div class="paper-title" title="' + esc(paper.title || "未命名文献") + '">' + titleLine + '</div>' +
                    originalTitle +
                    '<div class="paper-meta" style="margin-top:4px;">' + metaLine + '</div>' +
                '</td>' +
                '<td class="col-divider" style="text-align:center;">' +
                    '<div style="display:flex; flex-wrap:wrap; gap:4px; align-items:center; justify-content:center;">' +
                        paperStatusChip(paper) + 
                        wfChip +
                        dftCandChip +
                        (paper.pdf_quality_status ? '<span class="status-chip ' + (paper.pdf_quality_status === 'Good' ? 'full' : 'meta') + '">' + esc(paper.pdf_quality_status) + '</span>' : '') +
                        (paper.dft_completeness_status ? '<span class="status-chip ' + (paper.dft_completeness_status === 'DB_Ready' ? 'full' : 'meta') + '">' + esc(paper.dft_completeness_status) + '</span>' : '') +
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
            '<th style="width:40px; text-align:center;">#</th>' +
            '<th style="width:50px; text-align:center;">年份</th>' +
            '<th style="width:60px; text-align:center;">类型</th>' +
            '<th style="width:60px; text-align:center;">IF</th>' +
            '<th class="col-divider" style="text-align:center;">文献标题</th>' +
            '<th class="col-divider" style="width:180px; text-align:center;">流程与质量</th>' +
            '<th style="width:160px; text-align:center;">数据统计</th>' +
        '</tr></thead>' +
        '<tbody>' + tbodyHtml + '</tbody>' +
    '</table>';
}

window.impactFactorCache = JSON.parse(localStorage.getItem('impactFactors') || '{}');

function renderImpactFactor(paper) {
    const journal = (paper.journal || "未知期刊").trim();
    if (!journal || journal === "未知期刊") {
        return '<span style="color:var(--color-text-tertiary);font-size:12px;">-</span>';
    }
    const cached = window.impactFactorCache[journal];
    if (cached) {
        return '<span class="status-chip full" style="background:#fdf4ff;color:#86198f;border-color:#f5d0fe;font-weight:700;">IF: ' + cached + '</span>';
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
