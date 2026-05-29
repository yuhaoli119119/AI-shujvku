const SIDEBAR_STORAGE_KEY = "writing-assistant-layout";
const SIDEBAR_DEFAULT_WIDTH = 420;
const SIDEBAR_MIN_WIDTH = 320;
const SIDEBAR_MAX_WIDTH = 640;

document.addEventListener("DOMContentLoaded", () => {
    TopNav.init({ currentPage: "writing-assistant" });
    initializeValidationBehavior();
    initializeSidebarLayout();
});

function initializeValidationBehavior() {
    const writingText = document.getElementById("writingText");
    if (!writingText) return;

    writingText.addEventListener("input", () => {
        const alertDiv = document.getElementById("validationAlert");
        if (alertDiv) {
            alertDiv.style.display = "none";
            alertDiv.innerText = "";
        }
    });
}

function initializeSidebarLayout() {
    const layout = document.getElementById("assistantLayout");
    const toggleButton = document.getElementById("sidebarToggleButton");
    const restoreButton = document.getElementById("sidebarRestoreButton");
    const resizer = document.getElementById("panelResizer");
    const navButtons = Array.from(document.querySelectorAll(".panel-nav-item"));
    const sections = Array.from(document.querySelectorAll(".panel-section"));

    if (!layout) return;

    const stored = readSidebarState();
    setSidebarWidth(stored.width || SIDEBAR_DEFAULT_WIDTH, false);
    setSidebarHidden(Boolean(stored.hidden), false);
    setActivePanelSection(stored.activeSection || "context", false);

    navButtons.forEach(button => {
        button.addEventListener("click", () => {
            setActivePanelSection(button.dataset.panelSection || "context");
        });
    });

    if (toggleButton) {
        toggleButton.addEventListener("click", () => setSidebarHidden(true));
    }

    if (restoreButton) {
        restoreButton.addEventListener("click", () => setSidebarHidden(false));
    }

    if (resizer) {
        resizer.addEventListener("pointerdown", event => {
            if (window.innerWidth <= 1024) return;
            event.preventDefault();
            document.body.classList.add("is-resizing-sidebar");

            const onMove = moveEvent => {
                const bounds = layout.getBoundingClientRect();
                const nextWidth = clamp(moveEvent.clientX - bounds.left, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH);
                setSidebarWidth(nextWidth, false);
            };

            const onStop = () => {
                document.body.classList.remove("is-resizing-sidebar");
                document.removeEventListener("pointermove", onMove);
                document.removeEventListener("pointerup", onStop);
                document.removeEventListener("pointercancel", onStop);
                persistSidebarState();
            };

            document.addEventListener("pointermove", onMove);
            document.addEventListener("pointerup", onStop);
            document.addEventListener("pointercancel", onStop);
        });
    }

    window.addEventListener("resize", () => {
        if (window.innerWidth <= 1024) {
            layout.style.removeProperty("--assistant-sidebar-width");
        } else {
            const state = readSidebarState();
            setSidebarWidth(state.width || SIDEBAR_DEFAULT_WIDTH, false);
        }
    });

    if (sections.length === 0) {
        setSidebarHidden(false, false);
    }
}

function readSidebarState() {
    try {
        const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
        if (!raw) return {};
        return JSON.parse(raw) || {};
    } catch (_) {
        return {};
    }
}

function persistSidebarState() {
    try {
        const layout = document.getElementById("assistantLayout");
        const hidden = layout ? layout.classList.contains("sidebar-hidden") : false;
        const activeButton = document.querySelector(".panel-nav-item.active");
        const widthValue = layout ? layout.style.getPropertyValue("--assistant-sidebar-width") : "";
        const width = parseInt(widthValue, 10) || SIDEBAR_DEFAULT_WIDTH;
        const activeSection = activeButton ? activeButton.dataset.panelSection : "context";

        window.localStorage.setItem(SIDEBAR_STORAGE_KEY, JSON.stringify({
            hidden,
            width,
            activeSection
        }));
    } catch (_) {
        // Ignore storage failures.
    }
}

function setSidebarWidth(width, persist = true) {
    const layout = document.getElementById("assistantLayout");
    if (!layout || window.innerWidth <= 1024) return;
    const nextWidth = clamp(width, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH);
    layout.style.setProperty("--assistant-sidebar-width", `${nextWidth}px`);
    if (persist) persistSidebarState();
}

function setSidebarHidden(hidden, persist = true) {
    const layout = document.getElementById("assistantLayout");
    const toggleButton = document.getElementById("sidebarToggleButton");
    const restoreButton = document.getElementById("sidebarRestoreButton");

    if (!layout) return;
    layout.classList.toggle("sidebar-hidden", hidden);

    if (toggleButton) {
        toggleButton.innerText = hidden ? "左侧栏已隐藏" : "隐藏侧栏";
        toggleButton.disabled = hidden;
    }

    if (restoreButton) {
        restoreButton.hidden = !hidden;
    }

    if (persist) persistSidebarState();
}

function setActivePanelSection(sectionName, persist = true) {
    const navButtons = Array.from(document.querySelectorAll(".panel-nav-item"));
    const sections = Array.from(document.querySelectorAll(".panel-section"));

    navButtons.forEach(button => {
        const isActive = button.dataset.panelSection === sectionName;
        button.classList.toggle("active", isActive);
        button.setAttribute("aria-selected", String(isActive));
    });

    sections.forEach(section => {
        const isActive = section.dataset.panelContent === sectionName;
        section.classList.toggle("active", isActive);
    });

    if (persist) persistSidebarState();
}

function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
}

function formatTierLabel(tier) {
    const labels = {
        strong: "高",
        moderate: "中",
        weak: "低"
    };
    return labels[tier] || String(tier || "-");
}

function formatEvidenceStatus(status) {
    const labels = {
        confirmed: "confirmed",
        safe_verified: "safe_verified",
        metadata_only: "metadata_only",
        pending_review: "pending_review",
        unverified: "unverified",
        unknown: "unknown"
    };
    return labels[status] || String(status || "-");
}

function formatWarningText(warning) {
    const map = {
        suggestion_only_needs_human_verification: "仅为建议，需先完成人工核验。",
        impact_factor_needs_metadata: "影响因子缺失，需要补充元数据。",
        needs_manual_verification_before_use: "使用前必须完成人工核验。"
    };
    return map[warning] || String(warning || "-");
}

function formatChecklistItem(item) {
    const map = {
        "Verify evidence": "核对证据原文",
        "Check metadata": "核对元数据",
        "Confirm citation fit": "确认与当前上下文是否匹配"
    };
    return map[item] || String(item || "-");
}

function formatExcludedReason(reason) {
    if (reason === "exclude_from_citation=true") return "已标记为不可引用";
    if (reason === "citation_priority=exclude") return "引用优先级被排除";
    if (reason === "year_below_min" || reason === "year_above_max") return "年份不在筛选范围内";
    if (reason === "journal_include_filter_mismatch") return "不在包含期刊范围内";
    if (reason === "journal_exclude_filter_match") return "命中排除期刊条件";
    if (reason === "impact_factor_below_min" || reason === "impact_factor_above_max") return "影响因子不在筛选范围内";
    if (reason === "needs_metadata_excluded_by_impact_factor_min" || reason === "needs_metadata_excluded_by_impact_factor_max") return "缺少影响因子元数据";
    if (String(reason || "").endsWith("filter_mismatch")) return "与当前筛选条件不匹配";
    return String(reason || "已排除");
}

function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    }[char]));
}

function encodeCandidate(cand) {
    return encodeURIComponent(JSON.stringify(cand || {}));
}

function decodeCandidate(encoded) {
    try {
        return JSON.parse(decodeURIComponent(encoded));
    } catch (_) {
        return {};
    }
}

async function retrieveCandidates() {
    const alertDiv = document.getElementById("validationAlert");
    const container = document.getElementById("candidatesContainer");
    const loading = document.getElementById("loadingIndicator");
    const resultsCount = document.getElementById("resultsCount");
    const excludedCount = document.getElementById("excludedCount");
    const excludedCollapsible = document.getElementById("excludedCollapsible");
    const excludedList = document.getElementById("excludedList");

    window.currentDraftProposals = {};

    alertDiv.style.display = "none";
    alertDiv.innerText = "";

    const textVal = document.getElementById("writingText").value.trim();
    if (!textVal) {
        alertDiv.innerText = "请先输入句子或段落上下文，再检索候选。";
        alertDiv.style.display = "block";
        setActivePanelSection("context");
        return;
    }

    const tokens = tokenizeText(textVal);
    if (tokens.length < 2) {
        alertDiv.innerText = "输入文本至少需要包含两个可检索术语，例如关键词或非停用词。";
        alertDiv.style.display = "block";
        setActivePanelSection("context");
        return;
    }

    loading.style.display = "flex";
    container.innerHTML = "";
    resultsCount.innerText = "0";
    excludedCount.innerText = "0";
    excludedCollapsible.style.display = "none";
    excludedList.innerHTML = "";

    const yearMin = document.getElementById("filterYearMin").value;
    const yearMax = document.getElementById("filterYearMax").value;
    const ifMin = document.getElementById("filterIFMin").value;
    const ifMax = document.getElementById("filterIFMax").value;
    const journalInc = document.getElementById("filterJournalInc").value;
    const journalExc = document.getElementById("filterJournalExc").value;
    const citPriority = document.getElementById("filterCitationPriority").value;
    const exclCitation = document.getElementById("filterExcludeFromCitation").value;

    const filters = {};
    if (yearMin) filters.year_min = parseInt(yearMin, 10);
    if (yearMax) filters.year_max = parseInt(yearMax, 10);
    if (ifMin) filters.impact_factor_min = parseFloat(ifMin);
    if (ifMax) filters.impact_factor_max = parseFloat(ifMax);
    if (journalInc) filters.journal_include = journalInc.split(",").map(s => s.trim()).filter(Boolean);
    if (journalExc) filters.journal_exclude = journalExc.split(",").map(s => s.trim()).filter(Boolean);
    if (citPriority) filters.citation_priority = citPriority;
    if (exclCitation) filters.exclude_from_citation = exclCitation === "true";
    if (document.getElementById("filterNeedsMetadata").checked) filters.needs_metadata = true;
    if (document.getElementById("filterHasPdf").checked) filters.has_pdf = true;
    if (document.getElementById("filterHasParsedText").checked) filters.has_parsed_text = true;
    if (document.getElementById("filterHasExtractionOutput").checked) filters.has_extraction_output = true;
    if (document.getElementById("filterHasVerifiedEvidence").checked) filters.has_verified_evidence = true;
    if (document.getElementById("filterHasSafeVerifiedEvidence").checked) filters.has_safe_verified_evidence = true;

    const payload = {
        text: textVal,
        max_candidates: parseInt(document.getElementById("maxCandidates").value, 10) || 10,
        filters,
        include_unverified_suggestions: document.getElementById("includeUnverifiedSuggestions").checked,
        include_pending_review: document.getElementById("includePendingReview").checked
    };

    try {
        const response = await fetch("/api/writing/citation-candidates", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const errText = await response.text();
            let parsedErr;
            try {
                parsedErr = JSON.parse(errText);
            } catch (_) {
                parsedErr = null;
            }
            const detailMsg = parsedErr && parsedErr.detail ? parsedErr.detail : errText;
            throw new Error(`API 错误：${detailMsg}`);
        }

        const data = await response.json();
        renderResults(data);
        showToast("候选检索完成。", "success");
    } catch (err) {
        console.error("Retrieval error:", err);
        alertDiv.innerText = err.message || "从后端检索引用候选失败。";
        alertDiv.style.display = "block";
        showToast("检索失败，请查看错误提示。", "error");

        container.innerHTML = `
            <div class="empty-state">
                <h3>检索失败</h3>
                <p>${escapeHtml(err.message)}</p>
            </div>
        `;
    } finally {
        loading.style.display = "none";
    }
}

function renderResults(data) {
    const container = document.getElementById("candidatesContainer");
    const resultsCount = document.getElementById("resultsCount");
    const excludedCount = document.getElementById("excludedCount");
    const excludedCollapsible = document.getElementById("excludedCollapsible");
    const excludedList = document.getElementById("excludedList");

    const candidates = data.candidates || [];
    const excludedReasons = data.excluded_reasons || [];

    resultsCount.innerText = String(candidates.length);

    if (candidates.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <h3>未找到候选</h3>
                <p>当前写作上下文与筛选条件下没有匹配候选。请尝试调整文本或放宽筛选条件。</p>
            </div>
        `;
    } else {
        container.innerHTML = "";
        candidates.forEach(cand => {
            const card = document.createElement("div");
            card.className = "candidate-card";

            let badgeText = "";
            let badgeClass = "";
            let borderClass = "";

            if (cand.can_be_used_as_confirmed_citation === true) {
                badgeText = "API 明确允许作为已确认候选";
                badgeClass = "badge-confirmed";
                borderClass = "border-confirmed";
            } else if (cand.requires_human_verification === true && cand.evidence_status !== "metadata_only") {
                badgeText = "需要人工核验";
                badgeClass = "badge-needs-verification";
                borderClass = "border-needs-verification";
            } else {
                badgeText = "仅元数据建议，暂不能作为证据";
                badgeClass = "badge-metadata-only";
                borderClass = "border-metadata-only";
            }

            card.classList.add(borderClass);

            let snippetsHtml = "";
            if (cand.supporting_snippets && cand.supporting_snippets.length > 0) {
                const listItems = cand.supporting_snippets.map(snip => {
                    const textWithHighlights = highlightQueryTerms(snip.text, data.query_text);
                    const sourceLabel = snip.source ? `来源：${escapeHtml(snip.source)}` : "";
                    const pageLabel = snip.page ? `页码：${escapeHtml(snip.page)}` : "";
                    const statusLabel = snip.locator_status ? `定位：${escapeHtml(snip.locator_status)}` : "";
                    const metaLabel = [sourceLabel, pageLabel, statusLabel].filter(Boolean).join(" | ");
                    return `
                        <div class="snippet-card">
                            <div>${textWithHighlights}</div>
                            <div class="snippet-source">${metaLabel}</div>
                        </div>
                    `;
                }).join("");

                snippetsHtml = `
                    <div class="snippets-section">
                        <div class="snippets-title">证据片段</div>
                        <div class="snippets-list">${listItems}</div>
                    </div>
                `;
            }

            let warningsHtml = "";
            if (cand.warnings && cand.warnings.length > 0) {
                const warningItems = cand.warnings.map(w => `<div class="card-warning-message">${escapeHtml(formatWarningText(w))}</div>`).join("");
                warningsHtml = `
                    <div class="card-warning-box">
                        <div class="card-warning-icon">!</div>
                        <div class="card-warning-list">${warningItems}</div>
                    </div>
                `;
            }

            const ifVal = cand.impact_factor !== null ? cand.impact_factor : "-";
            const ifStatus = cand.impact_factor_status || "needs_metadata";
            const yearStr = cand.year || "-";
            const journalStr = cand.journal || "-";
            const recTier = cand.recommendation_tier || "weak";
            const scoreVal = cand.recommendation_score !== undefined ? cand.recommendation_score : 0;
            const encodedCandidate = encodeCandidate(cand);

            card.innerHTML = `
                <div class="card-header">
                    <div class="card-title-area">
                        <h4 class="card-title">${escapeHtml(cand.title || "未命名文献")}</h4>
                        <div class="card-metadata">
                            <span class="metadata-separator">${escapeHtml(journalStr)}</span>
                            <span class="metadata-separator">年份：${escapeHtml(yearStr)}</span>
                            <span>IF：${escapeHtml(ifVal)} (${escapeHtml(ifStatus)})</span>
                        </div>
                    </div>
                    <div class="safety-badge ${badgeClass}">${badgeText}</div>
                </div>

                ${warningsHtml}

                <div class="card-details-grid">
                    <div class="detail-item">
                        <span class="detail-label">推荐分</span>
                        <span class="detail-value">${escapeHtml(scoreVal)}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">推荐层级</span>
                        <span class="detail-value">
                            <span class="tier-badge tier-${escapeHtml(recTier)}">${escapeHtml(formatTierLabel(recTier))}</span>
                        </span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">证据状态</span>
                        <span class="detail-value">${escapeHtml(formatEvidenceStatus(cand.evidence_status))}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">引用优先级</span>
                        <span class="detail-value">${escapeHtml(cand.citation_priority || "-")}</span>
                    </div>
                </div>

                ${snippetsHtml}

                <div class="card-reason-box">
                    <strong>推荐理由：</strong>${escapeHtml(cand.reason || "-")}
                </div>

                <div id="proposalContainer-${escapeHtml(cand.paper_id)}" class="proposal-container" style="display: none;"></div>

                <div class="card-actions">
                    <button class="btn btn-sm btn-primary" type="button" onclick="generateDraftProposalFromEncoded('${encodedCandidate}')">生成引用建议草稿</button>
                    <button class="btn btn-sm btn-ghost" type="button" onclick="copyCardTitle('${escapeJsString(cand.title)}')">复制标题</button>
                    <button class="btn btn-sm btn-outline" type="button" onclick="copyCardInfoFromEncoded('${encodedCandidate}')">复制候选信息</button>
                </div>
            `;
            container.appendChild(card);
        });
    }

    excludedCount.innerText = String(excludedReasons.length);
    if (excludedReasons.length > 0) {
        excludedCollapsible.style.display = "block";
        excludedList.innerHTML = excludedReasons.map(item => `
            <div class="excluded-item">
                <div class="excluded-item-info">
                    <div class="excluded-item-title">Paper ID: ${escapeHtml(item.paper_id)}</div>
                </div>
                <span class="excluded-item-reason">${escapeHtml(formatExcludedReason(item.reason))}</span>
            </div>
        `).join("");
    } else {
        excludedCollapsible.style.display = "none";
    }
}

function clearFilters() {
    document.getElementById("filterYearMin").value = "";
    document.getElementById("filterYearMax").value = "";
    document.getElementById("filterIFMin").value = "";
    document.getElementById("filterIFMax").value = "";
    document.getElementById("filterJournalInc").value = "";
    document.getElementById("filterJournalExc").value = "";
    document.getElementById("filterCitationPriority").value = "";
    document.getElementById("filterExcludeFromCitation").value = "";
    document.getElementById("filterNeedsMetadata").checked = false;
    document.getElementById("filterHasPdf").checked = false;
    document.getElementById("filterHasParsedText").checked = false;
    document.getElementById("filterHasExtractionOutput").checked = false;
    document.getElementById("filterHasVerifiedEvidence").checked = false;
    document.getElementById("filterHasSafeVerifiedEvidence").checked = false;
    showToast("筛选条件已清空。", "success");
}

function copyCardTitle(title) {
    writeClipboardText(title).then(() => {
        showToast("标题已复制。", "success");
    }).catch(() => {
        showToast("复制标题失败。", "error");
    });
}

function copyCardInfoFromEncoded(encodedCand) {
    copyCardInfo(decodeCandidate(encodedCand));
}

function copyCardInfo(cand) {
    let warningText = "无";
    if (cand.warnings && cand.warnings.length > 0) {
        warningText = cand.warnings.map(formatWarningText).join("；");
    }

    let safetyLabel = "仅元数据建议";
    if (cand.can_be_used_as_confirmed_citation === true) {
        safetyLabel = "API 明确允许作为已确认候选";
    } else if (cand.requires_human_verification === true) {
        safetyLabel = "需要人工核验";
    }

    const infoString = [
        `标题：${cand.title || "未命名"}`,
        `期刊：${cand.journal || "未知"}`,
        `年份：${cand.year || "未知"}`,
        `影响因子：${cand.impact_factor !== null ? cand.impact_factor : "N/A"}`,
        `推荐分：${cand.recommendation_score || 0}（${cand.recommendation_tier || "weak"}）`,
        `安全分类：${safetyLabel}`,
        `证据状态：${cand.evidence_status || "unknown"}`,
        `警告：${warningText}`
    ].join("\n");

    writeClipboardText(infoString).then(() => {
        showToast("候选信息已复制。", "success");
    }).catch(() => {
        showToast("复制候选信息失败。", "error");
    });
}

function tokenizeText(text) {
    const stopwords = new Set([
        "and", "are", "can", "for", "from", "has", "have", "into", "that", "the", "their", "this", "with", "within"
    ]);
    const regex = /[A-Za-z0-9][A-Za-z0-9\-]+/g;
    const tokens = [];
    let match;
    while ((match = regex.exec(text)) !== null) {
        const value = match[0].toLowerCase();
        if (value.length > 2 && !stopwords.has(value)) {
            tokens.push(value);
        }
    }
    return [...new Set(tokens)];
}

function highlightQueryTerms(snippet, queryText) {
    const queryTokens = tokenizeText(queryText || "");
    if (queryTokens.length === 0) return escapeHtml(snippet);

    queryTokens.sort((a, b) => b.length - a.length);
    let highlighted = escapeHtml(snippet);
    queryTokens.forEach(token => {
        const escaped = token.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
        const reg = new RegExp(`\\b(${escaped})\\b`, "gi");
        highlighted = highlighted.replace(reg, "<mark>$1</mark>");
    });
    return highlighted;
}

function showToast(message, type = "success") {
    const toastContainer = document.getElementById("toastContainer");
    if (!toastContainer) return;
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.innerText = message;
    toastContainer.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = "0";
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function escapeJsString(str) {
    return String(str || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

function generateDraftProposalFromEncoded(encodedCand) {
    generateDraftProposal(decodeCandidate(encodedCand));
}

async function generateDraftProposal(cand) {
    const textVal = document.getElementById("writingText").value.trim();
    if (!textVal) {
        showToast("请先输入文本上下文，再生成草稿。", "error");
        setActivePanelSection("context");
        return;
    }

    if (!cand.paper_id) {
        showToast("候选缺少 paper_id，已阻止生成。", "error");
        return;
    }

    const container = document.getElementById(`proposalContainer-${cand.paper_id}`);
    if (!container) return;

    container.style.display = "block";
    container.innerHTML = '<div class="proposal-loading">正在生成建议草稿...</div>';

    const payload = {
        text: textVal,
        selected_paper_id: cand.paper_id,
        citation_marker: cand.citation_marker || `[Draft_${cand.paper_id.substring(0, 6)}]`,
        insertion_mode: "parenthetical",
        citation_style: "draft_author_year",
        candidate_evidence_status: cand.evidence_status || "unknown",
        candidate_can_be_used_as_confirmed_citation: cand.can_be_used_as_confirmed_citation || false,
        candidate_requires_human_verification: cand.requires_human_verification || false,
        supporting_snippet: (cand.supporting_snippets && cand.supporting_snippets.length > 0) ? cand.supporting_snippets[0].text : "",
        user_note: ""
    };

    try {
        const response = await fetch("/api/writing/citation-insertion-draft", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`API 错误：${errText}`);
        }

        const data = await response.json();
        if (!window.currentDraftProposals) window.currentDraftProposals = {};
        window.currentDraftProposals[cand.paper_id] = data;
        renderDraftProposal(cand.paper_id, data);
    } catch (err) {
        console.error("Draft generation error:", err);
        container.innerHTML = `<div class="proposal-error">生成草稿失败：${escapeHtml(err.message)}</div>`;
        showToast("建议草稿生成失败。", "error");
    }
}

function renderDraftProposal(paperId, data) {
    const container = document.getElementById(`proposalContainer-${paperId}`);
    if (!container) return;

    if (data.proposal_status === "blocked_excluded_from_citation") {
        container.innerHTML = `
            <div class="proposal-blocked">
                <div class="blocked-text">
                    <strong>已阻止</strong><br>
                    该文献已被排除引用，因此未生成建议草稿。
                </div>
            </div>
            ${renderBlockedActions(data.blocked_actions)}
        `;
        return;
    }

    let safetyBanner = "";
    if (data.can_insert_as_confirmed_citation === true) {
        safetyBanner = '<div class="proposal-banner banner-confirmed">该草稿基于 API 允许作为已确认候选的记录生成，使用前仍需人工复核。</div>';
    } else if (data.requires_human_verification === true) {
        safetyBanner = '<div class="proposal-banner banner-warning">该草稿仅供参考，引用前必须完成人工核验。</div>';
    } else if (data.evidence_status === "metadata_only") {
        safetyBanner = '<div class="proposal-banner banner-metadata">该草稿仅基于元数据建议生成，暂不能作为证据使用。</div>';
    }

    let warningsHtml = "";
    if (data.warnings && data.warnings.length > 0) {
        warningsHtml = `
            <div class="proposal-warnings">
                <strong>提示：</strong>
                <ul>
                    ${data.warnings.map(w => `<li>${escapeHtml(formatWarningText(w))}</li>`).join("")}
                </ul>
            </div>
        `;
    }

    let checklistHtml = "";
    if (data.human_review_checklist && data.human_review_checklist.length > 0) {
        checklistHtml = `
            <div class="proposal-checklist">
                <strong>人工复核清单：</strong>
                <ul>
                    ${data.human_review_checklist.map(item => `<li><input type="checkbox" disabled> ${escapeHtml(formatChecklistItem(item))}</li>`).join("")}
                </ul>
            </div>
        `;
    }

    const draftText = data.draft_text || "未返回草稿文本。";

    container.innerHTML = `
        <div class="proposal-content">
            ${safetyBanner}
            ${warningsHtml}

            <div class="draft-text-box">
                <div class="draft-label">引用建议草稿：</div>
                <div class="draft-text">${escapeHtml(draftText)}</div>
                <div class="draft-marker">标记：${escapeHtml(data.citation_marker || "")}</div>
            </div>

            ${checklistHtml}
            ${renderBlockedActions(data.blocked_actions)}

            <div class="proposal-actions">
                <button class="btn btn-sm btn-outline" type="button" onclick="copyDraftProposal('${paperId}')">复制建议草稿</button>
            </div>
        </div>
    `;
}

function renderBlockedActions(blockedActions) {
    if (!blockedActions || blockedActions.length === 0) return "";
    return `
        <div class="blocked-actions-audit">
            <strong>已阻止的安全动作：</strong>
            <ul>
                ${blockedActions.map(action => `<li>${escapeHtml(action)}</li>`).join("")}
            </ul>
        </div>
    `;
}

function copyDraftProposal(paperId) {
    if (!window.currentDraftProposals) return;
    const data = window.currentDraftProposals[paperId];
    if (!data) return;

    const parts = [
        `草稿状态：${data.proposal_status}`,
        `证据状态：${data.evidence_status}`,
        `需要人工核验：${data.requires_human_verification}`
    ];

    if (data.warnings && data.warnings.length > 0) {
        parts.push(`提示：${data.warnings.map(formatWarningText).join(" | ")}`);
    }

    parts.push(`草稿文本：${data.draft_text || ""}`);

    if (data.human_review_checklist && data.human_review_checklist.length > 0) {
        parts.push(`复核清单：\n- ${data.human_review_checklist.map(formatChecklistItem).join("\n- ")}`);
    }

    writeClipboardText(parts.join("\n\n")).then(() => {
        showToast("建议草稿已复制。", "success");
    }).catch(() => {
        showToast("复制建议草稿失败。", "error");
    });
}

function writeClipboardText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        return navigator.clipboard.writeText(text).catch(() => fallbackCopyText(text));
    }
    return fallbackCopyText(text);
}

function fallbackCopyText(text) {
    return new Promise((resolve, reject) => {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();

        try {
            if (document.execCommand("copy")) {
                resolve();
            } else {
                reject(new Error("Copy command failed"));
            }
        } catch (err) {
            reject(err);
        } finally {
            textarea.remove();
        }
    });
}
