document.addEventListener('DOMContentLoaded', () => {
    // Mount top navigation
    TopNav.init({ currentPage: 'writing-assistant' });
    
    // Clear validation error on textarea input
    const writingText = document.getElementById('writingText');
    if (writingText) {
        writingText.addEventListener('input', () => {
            const alertDiv = document.getElementById('validationAlert');
            if (alertDiv) {
                alertDiv.style.display = 'none';
                alertDiv.innerText = '';
            }
        });
    }
});

/**
 * Retrieve citation candidates from the backend API.
 */
async function retrieveCandidates() {
    const alertDiv = document.getElementById('validationAlert');
    const container = document.getElementById('candidatesContainer');
    const loading = document.getElementById('loadingIndicator');
    const resultsCount = document.getElementById('resultsCount');
    const excludedCount = document.getElementById('excludedCount');
    const excludedCollapsible = document.getElementById('excludedCollapsible');
    const excludedList = document.getElementById('excludedList');
    
    // Clear previous proposals globally
    window.currentDraftProposals = {};
    
    alertDiv.style.display = 'none';
    alertDiv.innerText = '';
    
    const textVal = document.getElementById('writingText').value.trim();
    if (!textVal) {
        alertDiv.innerText = 'Error: Please enter sentences or paragraph context before retrieving candidates.';
        alertDiv.style.display = 'block';
        return;
    }
    
    // Tokenize check - backend expects at least 2 searchable terms
    const tokens = tokenizeText(textVal);
    if (tokens.length < 2) {
        alertDiv.innerText = 'Error: The pasted text must contain at least two searchable terms (e.g. keywords that are not stopwords).';
        alertDiv.style.display = 'block';
        return;
    }
    
    // Show loading spinner
    loading.style.display = 'flex';
    container.innerHTML = '';
    resultsCount.innerText = '0';
    excludedCount.innerText = '0';
    excludedCollapsible.style.display = 'none';
    excludedList.innerHTML = '';
    
    // Parse filters
    const yearMin = document.getElementById('filterYearMin').value;
    const yearMax = document.getElementById('filterYearMax').value;
    const ifMin = document.getElementById('filterIFMin').value;
    const ifMax = document.getElementById('filterIFMax').value;
    const journalInc = document.getElementById('filterJournalInc').value;
    const journalExc = document.getElementById('filterJournalExc').value;
    const citPriority = document.getElementById('filterCitationPriority').value;
    const exclCitation = document.getElementById('filterExcludeFromCitation').value;
    
    const filters = {};
    if (yearMin) filters.year_min = parseInt(yearMin, 10);
    if (yearMax) filters.year_max = parseInt(yearMax, 10);
    if (ifMin) filters.impact_factor_min = parseFloat(ifMin);
    if (ifMax) filters.impact_factor_max = parseFloat(ifMax);
    
    if (journalInc) {
        filters.journal_include = journalInc.split(',').map(s => s.trim()).filter(Boolean);
    }
    if (journalExc) {
        filters.journal_exclude = journalExc.split(',').map(s => s.trim()).filter(Boolean);
    }
    
    if (citPriority) filters.citation_priority = citPriority;
    
    // Boolean filters
    if (document.getElementById('filterNeedsMetadata').checked) filters.needs_metadata = true;
    if (document.getElementById('filterHasPdf').checked) filters.has_pdf = true;
    if (document.getElementById('filterHasParsedText').checked) filters.has_parsed_text = true;
    if (document.getElementById('filterHasExtractionOutput').checked) filters.has_extraction_output = true;
    if (document.getElementById('filterHasVerifiedEvidence').checked) filters.has_verified_evidence = true;
    if (document.getElementById('filterHasSafeVerifiedEvidence').checked) filters.has_safe_verified_evidence = true;
    
    // Construct payload
    const payload = {
        text: textVal,
        max_candidates: parseInt(document.getElementById('maxCandidates').value, 10) || 10,
        filters: filters,
        include_unverified_suggestions: document.getElementById('includeUnverifiedSuggestions').checked,
        include_pending_review: document.getElementById('includePendingReview').checked
    };
    
    try {
        const response = await fetch('/api/writing/citation-candidates', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const errText = await response.text();
            let parsedErr;
            try {
                parsedErr = JSON.parse(errText);
            } catch(e) {}
            
            const detailMsg = (parsedErr && parsedErr.detail) ? parsedErr.detail : errText;
            throw new Error(`API Error: ${detailMsg}`);
        }
        
        const data = await response.json();
        renderResults(data);
        showToast('Candidates retrieved successfully!', 'success');
        
    } catch(err) {
        console.error('Retrieval error:', err);
        alertDiv.innerText = err.message || 'Error: Failed to fetch citation candidates from backend API.';
        alertDiv.style.display = 'block';
        showToast('Retrieval failed. See error panel.', 'error');
        
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">❌</div>
                <h3>Search Failed</h3>
                <p>${err.message}</p>
            </div>
        `;
    } finally {
        loading.style.display = 'none';
    }
}

/**
 * Render API response into DOM components.
 */
function renderResults(data) {
    const container = document.getElementById('candidatesContainer');
    const resultsCount = document.getElementById('resultsCount');
    const excludedCount = document.getElementById('excludedCount');
    const excludedCollapsible = document.getElementById('excludedCollapsible');
    const excludedList = document.getElementById('excludedList');
    
    const candidates = data.candidates || [];
    const excludedReasons = data.excluded_reasons || [];
    
    resultsCount.innerText = candidates.length;
    
    if (candidates.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🔍</div>
                <h3>No candidates found</h3>
                <p>No matching references satisfy your writing context and active filters. Try refining the text or disabling filters.</p>
            </div>
        `;
    } else {
        container.innerHTML = '';
        candidates.forEach(cand => {
            const card = document.createElement('div');
            card.className = 'candidate-card';
            
            // Map safety labels and border classes
            let badgeText = '';
            let badgeClass = '';
            let borderClass = '';
            
            if (cand.can_be_used_as_confirmed_citation === true) {
                badgeText = 'Confirmed citation candidate';
                badgeClass = 'badge-confirmed';
                borderClass = 'border-confirmed';
            } else if (cand.requires_human_verification === true && cand.evidence_status !== 'metadata_only') {
                badgeText = 'Needs human verification';
                badgeClass = 'badge-needs-verification';
                borderClass = 'border-needs-verification';
            } else {
                badgeText = 'Metadata-only suggestion — cannot be used as evidence yet';
                badgeClass = 'badge-metadata-only';
                borderClass = 'border-metadata-only';
            }
            
            card.classList.add(borderClass);
            
            // Build snippets HTML
            let snippetsHtml = '';
            if (cand.supporting_snippets && cand.supporting_snippets.length > 0) {
                const listItems = cand.supporting_snippets.map(snip => {
                    const textWithHighlights = highlightQueryTerms(snip.text, data.query_text);
                    const sourceLabel = snip.source ? `Source: ${snip.source}` : '';
                    const pageLabel = snip.page ? `Page: ${snip.page}` : '';
                    const statusLabel = snip.locator_status ? `Locator: ${snip.locator_status}` : '';
                    const metaLabel = [sourceLabel, pageLabel, statusLabel].filter(Boolean).join(' | ');
                    
                    return `
                        <div class="snippet-card">
                            <div>"${textWithHighlights}"</div>
                            <div class="snippet-source">${metaLabel}</div>
                        </div>
                    `;
                }).join('');
                
                snippetsHtml = `
                    <div class="snippets-section">
                        <div class="snippets-title">Supporting Snippets</div>
                        <div class="snippets-list">${listItems}</div>
                    </div>
                `;
            }
            
            // Build warnings HTML
            let warningsHtml = '';
            if (cand.warnings && cand.warnings.length > 0) {
                const warningItems = cand.warnings.map(w => {
                    // Make warning readable
                    let text = w;
                    if (w === 'suggestion_only_needs_human_verification') {
                        text = 'Suggestion only — requires manual extraction review.';
                    } else if (w === 'impact_factor_needs_metadata') {
                        text = 'Impact Factor is missing (requires metadata lookup).';
                    }
                    return `<div class="card-warning-message">${text}</div>`;
                }).join('');
                
                warningsHtml = `
                    <div class="card-warning-box">
                        <div class="card-warning-icon">⚠️</div>
                        <div class="card-warning-list">${warningItems}</div>
                    </div>
                `;
            }
            
            // IF Display
            const ifVal = cand.impact_factor !== null ? cand.impact_factor : '-';
            const ifStatus = cand.impact_factor_status || 'needs_metadata';
            
            // Year & Journal details
            const yearStr = cand.year || '-';
            const journalStr = cand.journal || '-';
            
            // Recommendation Tier
            const recTier = cand.recommendation_tier || 'weak';
            const scoreVal = cand.recommendation_score !== undefined ? cand.recommendation_score : 0;
            
            card.innerHTML = `
                <div class="card-header">
                    <div class="card-title-area">
                        <h4 class="card-title">${cand.title || 'Untitled Paper'}</h4>
                        <div class="card-metadata">
                            <span class="metadata-separator">${journalStr}</span>
                            <span class="metadata-separator">Year: ${yearStr}</span>
                            <span>IF: ${ifVal} (${ifStatus})</span>
                        </div>
                    </div>
                    <div class="safety-badge ${badgeClass}">${badgeText}</div>
                </div>
                
                ${warningsHtml}
                
                <div class="card-details-grid">
                    <div class="detail-item">
                        <span class="detail-label">Rec Score</span>
                        <span class="detail-value">${scoreVal}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">Rec Tier</span>
                        <span class="detail-value">
                            <span class="tier-badge tier-${recTier}">${recTier.toUpperCase()}</span>
                        </span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">Evidence Status</span>
                        <span class="detail-value">${cand.evidence_status || '-'}</span>
                    </div>
                    <div class="detail-item">
                        <span class="detail-label">Citation Priority</span>
                        <span class="detail-value">${cand.citation_priority || '-'}</span>
                    </div>
                </div>
                
                ${snippetsHtml}
                
                <div class="card-reason-box">
                    <strong>Reason:</strong> ${cand.reason || '-'}
                </div>
                
                <div id="proposalContainer-${cand.paper_id}" class="proposal-container" style="display: none;"></div>
                
                <div class="card-actions">
                    <button class="btn btn-sm btn-primary" onclick="generateDraftProposal(${JSON.stringify(cand).replace(/"/g, '&quot;')})">Generate Draft Citation Proposal</button>
                    <button class="btn btn-sm btn-ghost" onclick="copyCardTitle('${escapeJsString(cand.title)}')">Copy Title</button>
                    <button class="btn btn-sm btn-outline" onclick="copyCardInfo(${JSON.stringify(cand).replace(/"/g, '&quot;')})">Copy Candidate Info</button>
                </div>
            `;
            container.appendChild(card);
        });
    }
    
    // Render excluded reasons list
    excludedCount.innerText = excludedReasons.length;
    if (excludedReasons.length > 0) {
        excludedCollapsible.style.display = 'block';
        excludedList.innerHTML = excludedReasons.map(item => {
            let label = item.reason || 'excluded';
            // Make reason readable
            if (label === 'exclude_from_citation=true') {
                label = 'Marked as "Do Not Cite"';
            } else if (label === 'citation_priority=exclude') {
                label = 'Excluded priority';
            } else if (label === 'year_below_min' || label === 'year_above_max') {
                label = 'Year bounds mismatch';
            } else if (label === 'journal_include_filter_mismatch') {
                label = 'Journal include filter mismatch';
            } else if (label === 'journal_exclude_filter_match') {
                label = 'Journal exclude filter match';
            } else if (label === 'impact_factor_below_min' || label === 'impact_factor_above_max') {
                label = 'Impact Factor filter mismatch';
            } else if (label === 'needs_metadata_excluded_by_impact_factor_min' || label === 'needs_metadata_excluded_by_impact_factor_max') {
                label = 'Needs impact metadata';
            } else if (label.endsWith('filter_mismatch')) {
                label = 'Filter requirement mismatch';
            }
            
            return `
                <div class="excluded-item">
                    <div class="excluded-item-info">
                        <div class="excluded-item-title">Paper ID: ${item.paper_id}</div>
                    </div>
                    <span class="excluded-item-reason">${label}</span>
                </div>
            `;
        }).join('');
    } else {
        excludedCollapsible.style.display = 'none';
    }
}

/**
 * Reset all input filters.
 */
function clearFilters() {
    document.getElementById('filterYearMin').value = '';
    document.getElementById('filterYearMax').value = '';
    document.getElementById('filterIFMin').value = '';
    document.getElementById('filterIFMax').value = '';
    document.getElementById('filterJournalInc').value = '';
    document.getElementById('filterJournalExc').value = '';
    document.getElementById('filterCitationPriority').value = '';
    document.getElementById('filterExcludeFromCitation').value = '';
    document.getElementById('filterNeedsMetadata').checked = false;
    document.getElementById('filterHasPdf').checked = false;
    document.getElementById('filterHasParsedText').checked = false;
    document.getElementById('filterHasExtractionOutput').checked = false;
    document.getElementById('filterHasVerifiedEvidence').checked = false;
    document.getElementById('filterHasSafeVerifiedEvidence').checked = false;
    
    showToast('Filters cleared', 'success');
}

/**
 * Copy title to clipboard.
 */
function copyCardTitle(title) {
    writeClipboardText(title).then(() => {
        showToast('Title copied to clipboard!', 'success');
    }).catch(() => {
        showToast('Failed to copy text', 'error');
    });
}

/**
 * Copy candidate detailed info including security tags and warnings.
 */
function copyCardInfo(cand) {
    let warningText = 'None';
    if (cand.warnings && cand.warnings.length > 0) {
        warningText = cand.warnings.join(', ');
    }
    
    let safetyLabel = 'Metadata-only suggestion';
    if (cand.can_be_used_as_confirmed_citation === true) {
        safetyLabel = 'Confirmed citation candidate';
    } else if (cand.requires_human_verification === true) {
        safetyLabel = 'Needs human verification';
    }

    const infoString = [
        `Title: ${cand.title || 'Untitled'}`,
        `Journal: ${cand.journal || 'Unknown'}`,
        `Year: ${cand.year || 'Unknown'}`,
        `Impact Factor: ${cand.impact_factor !== null ? cand.impact_factor : 'N/A'}`,
        `Recommendation Score: ${cand.recommendation_score || 0} (${cand.recommendation_tier || 'weak'})`,
        `Safety Classification: ${safetyLabel}`,
        `Evidence Status: ${cand.evidence_status || 'unknown'}`,
        `Verification Warning: ${warningText}`
    ].join('\n');
    
    writeClipboardText(infoString).then(() => {
        showToast('Candidate metadata copied to clipboard!', 'success');
    }).catch(() => {
        showToast('Failed to copy metadata', 'error');
    });
}

/**
 * Simple client-side tokenize implementation matching backend tokenize logic.
 */
function tokenizeText(text) {
    const stopwords = new Set([
        "and", "are", "can", "for", "from", "has", "have", "into", "that", "the", "their", "this", "with", "within"
    ]);
    const regex = /[A-Za-z0-9][A-Za-z0-9\-]+/g;
    const tokens = [];
    let match;
    while ((match = regex.exec(text)) !== null) {
        const val = match[0].toLowerCase();
        if (val.length > 2 && !stopwords.has(val)) {
            tokens.push(val);
        }
    }
    return [...new Set(tokens)];
}

/**
 * Highlight terms in text based on query context.
 */
function highlightQueryTerms(snippet, queryText) {
    const queryTokens = tokenizeText(queryText);
    if (queryTokens.length === 0) return snippet;
    
    // Sort tokens by length descending to match larger tokens first
    queryTokens.sort((a, b) => b.length - a.length);
    
    let highlighted = snippet;
    queryTokens.forEach(tok => {
        // Simple word boundary highlight regex
        const escaped = tok.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&');
        const reg = new RegExp(`\\b(${escaped})\\b`, 'gi');
        highlighted = highlighted.replace(reg, '<mark>$1</mark>');
    });
    return highlighted;
}

/**
 * Shows visual toast notification.
 */
function showToast(message, type = 'success') {
    const toastContainer = document.getElementById('toastContainer');
    if (!toastContainer) return;
    
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerText = message;
    
    toastContainer.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

/**
 * Helper to escape single quotes in JS strings.
 */
function escapeJsString(str) {
    return (str || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

/**
 * Generate a draft citation proposal for a candidate.
 */
async function generateDraftProposal(cand) {
    const textVal = document.getElementById('writingText').value.trim();
    if (!textVal) {
        showToast('Error: Please enter text context before generating a draft.', 'error');
        return;
    }
    
    if (!cand.paper_id) {
        showToast('Error: Candidate missing paper_id. Safety violation.', 'error');
        return;
    }

    const container = document.getElementById(`proposalContainer-${cand.paper_id}`);
    if (!container) return;
    
    container.style.display = 'block';
    container.innerHTML = '<div class="proposal-loading">Generating draft proposal...</div>';
    
    const payload = {
        text: textVal,
        selected_paper_id: cand.paper_id,
        citation_marker: cand.citation_marker || `[Draft_${cand.paper_id.substring(0,6)}]`,
        insertion_mode: "parenthetical",
        citation_style: "draft_author_year",
        candidate_evidence_status: cand.evidence_status || "unknown",
        candidate_can_be_used_as_confirmed_citation: cand.can_be_used_as_confirmed_citation || false,
        candidate_requires_human_verification: cand.requires_human_verification || false,
        supporting_snippet: (cand.supporting_snippets && cand.supporting_snippets.length > 0) ? cand.supporting_snippets[0].text : "",
        user_note: ""
    };
    
    try {
        const response = await fetch('/api/writing/citation-insertion-draft', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`API Error: ${errText}`);
        }
        
        const data = await response.json();
        
        // Store in global state for copying
        if (!window.currentDraftProposals) window.currentDraftProposals = {};
        window.currentDraftProposals[cand.paper_id] = data;
        
        renderDraftProposal(cand.paper_id, data);
        
    } catch(err) {
        console.error('Draft generation error:', err);
        container.innerHTML = `<div class="proposal-error">Failed to generate draft: ${err.message}</div>`;
        showToast('Draft generation failed.', 'error');
    }
}

/**
 * Render the draft proposal result into the container.
 */
function renderDraftProposal(paperId, data) {
    const container = document.getElementById(`proposalContainer-${paperId}`);
    if (!container) return;
    
    if (data.proposal_status === 'blocked_excluded_from_citation') {
        container.innerHTML = `
            <div class="proposal-blocked">
                <div class="blocked-icon">⛔</div>
                <div class="blocked-text">
                    <strong>Blocked</strong><br/>
                    No draft citation generated because this paper is excluded from citation.
                </div>
            </div>
            ${renderBlockedActions(data.blocked_actions)}
        `;
        return;
    }
    
    let safetyBanner = '';
    if (data.can_insert_as_confirmed_citation === true) {
        safetyBanner = `<div class="proposal-banner banner-confirmed">Confirmed candidate draft — still review before final manuscript use</div>`;
    } else if (data.requires_human_verification === true) {
        safetyBanner = `<div class="proposal-banner banner-warning">Needs human verification before citation use</div>`;
    } else if (data.evidence_status === 'metadata_only') {
        safetyBanner = `<div class="proposal-banner banner-metadata">Metadata-only suggestion — cannot be used as evidence yet</div>`;
    }
    
    let warningsHtml = '';
    if (data.warnings && data.warnings.length > 0) {
        warningsHtml = `
            <div class="proposal-warnings">
                <strong>Warnings:</strong>
                <ul>
                    ${data.warnings.map(w => `<li>${escapeJsString(w)}</li>`).join('')}
                </ul>
            </div>
        `;
    }
    
    let checklistHtml = '';
    if (data.human_review_checklist && data.human_review_checklist.length > 0) {
        checklistHtml = `
            <div class="proposal-checklist">
                <strong>Human Review Checklist:</strong>
                <ul>
                    ${data.human_review_checklist.map(c => `<li><input type="checkbox" disabled> ${escapeJsString(c)}</li>`).join('')}
                </ul>
            </div>
        `;
    }
    
    const draftText = data.draft_text || 'No draft text provided.';
    
    container.innerHTML = `
        <div class="proposal-content">
            ${safetyBanner}
            ${warningsHtml}
            
            <div class="draft-text-box">
                <div class="draft-label">Draft Citation Proposal:</div>
                <div class="draft-text">${escapeJsString(draftText)}</div>
                <div class="draft-marker">Marker: ${escapeJsString(data.citation_marker || '')}</div>
            </div>
            
            ${checklistHtml}
            ${renderBlockedActions(data.blocked_actions)}
            
            <div class="proposal-actions">
                <button class="btn btn-sm btn-outline" onclick="copyDraftProposal('${paperId}')">Copy Draft Proposal</button>
            </div>
        </div>
    `;
}

function renderBlockedActions(blockedActions) {
    if (!blockedActions || blockedActions.length === 0) return '';
    return `
        <div class="blocked-actions-audit">
            <strong>Safety Audit Actions Blocked:</strong>
            <ul>
                ${blockedActions.map(a => `<li>${escapeJsString(a)}</li>`).join('')}
            </ul>
        </div>
    `;
}

/**
 * Copy the draft proposal text securely without offering final bibliographies.
 */
function copyDraftProposal(paperId) {
    if (!window.currentDraftProposals) return;
    const data = window.currentDraftProposals[paperId];
    if (!data) return;
    
    const parts = [
        `Proposal Status: ${data.proposal_status}`,
        `Evidence Status: ${data.evidence_status}`,
        `Requires Human Verification: ${data.requires_human_verification}`
    ];
    
    if (data.warnings && data.warnings.length > 0) {
        parts.push(`Warnings: ${data.warnings.join(' | ')}`);
    }
    
    parts.push(`Draft Text: ${data.draft_text || ''}`);
    
    if (data.human_review_checklist && data.human_review_checklist.length > 0) {
        parts.push(`Review Checklist: \n- ${data.human_review_checklist.join('\n- ')}`);
    }
    
    writeClipboardText(parts.join('\n\n')).then(() => {
        showToast('Draft Proposal copied!', 'success');
    }).catch(() => {
        showToast('Failed to copy', 'error');
    });
}

function writeClipboardText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        return navigator.clipboard.writeText(text).catch(() => fallbackCopyText(text));
    }
    return fallbackCopyText(text);
}

function fallbackCopyText(text) {
    return new Promise((resolve, reject) => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();

        try {
            if (document.execCommand('copy')) {
                resolve();
            } else {
                reject(new Error('Copy command failed'));
            }
        } catch (err) {
            reject(err);
        } finally {
            textarea.remove();
        }
    });
}
