let currentPapers = [];
let pendingAction = null;
let pendingData = null;

document.addEventListener('DOMContentLoaded', () => {
    applyFilters();
});

async function applyFilters() {
    try {
        const queryParams = new URLSearchParams();

        const yearMin = document.getElementById('filterYearMin').value;
        if (yearMin) queryParams.append('year_min', yearMin);
        const yearMax = document.getElementById('filterYearMax').value;
        if (yearMax) queryParams.append('year_max', yearMax);
        const journalInc = document.getElementById('filterJournalInc').value;
        if (journalInc) queryParams.append('journal_include', journalInc);
        const journalExc = document.getElementById('filterJournalExc').value;
        if (journalExc) queryParams.append('journal_exclude', journalExc);
        const ifMin = document.getElementById('filterIFMin').value;
        if (ifMin) queryParams.append('impact_factor_min', ifMin);
        const ifMax = document.getElementById('filterIFMax').value;
        if (ifMax) queryParams.append('impact_factor_max', ifMax);

        if (document.getElementById('filterNeedsMetadata').checked) queryParams.append('needs_metadata', 'true');
        if (document.getElementById('filterHasPdf').checked) queryParams.append('has_pdf', 'true');
        if (document.getElementById('filterHasParsedText').checked) queryParams.append('has_parsed_text', 'true');
        if (document.getElementById('filterHasExtractionOutput').checked) queryParams.append('has_extraction_output', 'true');
        if (document.getElementById('filterHasVerifiedEvidence').checked) queryParams.append('has_verified_evidence', 'true');
        if (document.getElementById('filterHasSafeVerifiedEvidence').checked) queryParams.append('has_safe_verified_evidence', 'true');

        const exclCitation = document.getElementById('filterExcludeFromCitation').value;
        if (exclCitation) queryParams.append('exclude_from_citation', exclCitation);

        const citPriority = document.getElementById('filterCitationPriority').value;
        if (citPriority) queryParams.append('citation_priority', citPriority);

        const url = `/api/library/papers/filter?${queryParams.toString()}`;
        const resp = await fetch(url);
        if (!resp.ok) {
            console.error('Filter API failed', await resp.text());
            return;
        }
        
        const data = await resp.json();
        currentPapers = data.papers || data; // depending on API response format
        renderTable();
    } catch (err) {
        console.error('Error applying filters', err);
    }
}

function clearFilters() {
    document.getElementById('filterYearMin').value = '';
    document.getElementById('filterYearMax').value = '';
    document.getElementById('filterJournalInc').value = '';
    document.getElementById('filterJournalExc').value = '';
    document.getElementById('filterIFMin').value = '';
    document.getElementById('filterIFMax').value = '';
    document.getElementById('filterNeedsMetadata').checked = false;
    document.getElementById('filterHasPdf').checked = false;
    document.getElementById('filterHasParsedText').checked = false;
    document.getElementById('filterHasExtractionOutput').checked = false;
    document.getElementById('filterHasVerifiedEvidence').checked = false;
    document.getElementById('filterHasSafeVerifiedEvidence').checked = false;
    document.getElementById('filterExcludeFromCitation').value = '';
    document.getElementById('filterCitationPriority').value = '';
    applyFilters();
}

function renderTable() {
    const tbody = document.getElementById('resultsTableBody');
    tbody.innerHTML = '';
    
    currentPapers.forEach(paper => {
        const tr = document.createElement('tr');
        
        const safeVerifiedCount = paper.safe_verified_evidence_count !== undefined ? paper.safe_verified_evidence_count : '-';
        const verifiedCount = paper.verified_evidence_count !== undefined ? paper.verified_evidence_count : '-';

        let ifStatusDisplay = paper.impact_factor_status || '';
        if (!paper.impact_factor) {
            ifStatusDisplay = 'needs_metadata';
        }

        tr.innerHTML = `
            <td><input type="checkbox" class="row-checkbox" value="${paper.id}"></td>
            <td class="title-col" title="${paper.title || ''}">${paper.title || '-'}</td>
            <td>${paper.year || '-'}</td>
            <td>${paper.journal || '-'}</td>
            <td>${paper.impact_factor || '-'}</td>
            <td>${paper.impact_factor_year || '-'}</td>
            <td>${paper.impact_factor_source || '-'}</td>
            <td>${ifStatusDisplay}</td>
            <td>${paper.has_pdf ? 'Yes' : 'No'}</td>
            <td>${paper.has_parsed_text ? 'Yes' : 'No'}</td>
            <td>${paper.has_extraction_output ? 'Yes' : 'No'}</td>
            <td>${verifiedCount}</td>
            <td>${safeVerifiedCount}</td>
            <td>${paper.exclude_from_citation ? 'Yes' : 'No'}</td>
            <td>${paper.citation_priority || '-'}</td>
            <td>${paper.user_note || ''}</td>
        `;
        tbody.appendChild(tr);
    });
}

function toggleSelectAll() {
    const checked = document.getElementById('selectAllCheckbox').checked;
    const checkboxes = document.querySelectorAll('.row-checkbox');
    checkboxes.forEach(cb => cb.checked = checked);
}

function getSelectedIds() {
    const checkboxes = document.querySelectorAll('.row-checkbox:checked');
    return Array.from(checkboxes).map(cb => cb.value);
}

function markSelectedDoNotCite() {
    const ids = getSelectedIds();
    if (ids.length === 0) return alert('No papers selected.');
    
    pendingAction = 'bulkEligibility';
    pendingData = {
        paper_ids: ids,
        updates: {
            exclude_from_citation: true
        }
    };
    
    showConfirmModal('Mark as Do Not Cite', `Are you sure you want to mark ${ids.length} selected paper(s) as "Do Not Cite"?`);
}

function setPriorityForSelected() {
    const ids = getSelectedIds();
    if (ids.length === 0) return alert('No papers selected.');
    const priority = document.getElementById('bulkPrioritySelect').value;
    
    pendingAction = 'bulkEligibility';
    pendingData = {
        paper_ids: ids,
        updates: {
            citation_priority: priority
        }
    };
    
    showConfirmModal('Set Citation Priority', `Are you sure you want to set citation priority to "${priority}" for ${ids.length} selected paper(s)?`);
}

function showConfirmModal(title, message) {
    document.getElementById('confirmModalTitle').innerText = title;
    document.getElementById('confirmModalMessage').innerText = message;
    document.getElementById('confirmModalOverlay').style.display = 'flex';
}

function closeConfirmModal() {
    document.getElementById('confirmModalOverlay').style.display = 'none';
    pendingAction = null;
    pendingData = null;
}

async function executeConfirmedAction() {
    if (pendingAction === 'bulkEligibility' && pendingData) {
        try {
            const resp = await fetch('/api/library/papers/citation-eligibility/bulk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(pendingData)
            });
            if (resp.ok) {
                applyFilters();
            } else {
                alert('Bulk update failed: ' + await resp.text());
            }
        } catch (err) {
            alert('Error updating: ' + err);
        }
    }
    closeConfirmModal();
}

function openImportPanel() {
    document.getElementById('importPanelOverlay').style.display = 'flex';
    document.getElementById('importTextarea').value = '';
    document.getElementById('importDryRun').checked = true;
    document.getElementById('importResults').style.display = 'none';
}

function closeImportPanel() {
    document.getElementById('importPanelOverlay').style.display = 'none';
}

async function executeImport() {
    const text = document.getElementById('importTextarea').value;
    const isDryRun = document.getElementById('importDryRun').checked;
    
    if (!text.trim()) {
        alert('Please paste CSV or JSON content first.');
        return;
    }
    
    if (!isDryRun) {
        const confirmReal = confirm('You are running a real import (dry_run = false). This will update the database. Are you sure?');
        if (!confirmReal) return;
    }

    try {
        const resp = await fetch(`/api/library/impact-metadata/import?dry_run=${isDryRun}`, {
            method: 'POST',
            headers: { 'Content-Type': 'text/plain' }, // Using text/plain as it can be JSON or CSV text
            body: text
        });
        
        if (resp.ok) {
            const data = await resp.json();
            document.getElementById('importResImported').innerText = data.imported_count || 0;
            document.getElementById('importResUpdated').innerText = data.updated_count || 0;
            document.getElementById('importResMatched').innerText = data.matched_paper_count || 0;
            document.getElementById('importResUnmatched').innerText = data.unmatched_items || 0;
            document.getElementById('importResInvalid').innerText = data.invalid_items || 0;
            document.getElementById('importResNeedsMetadata').innerText = data.needs_metadata_remaining || 0;
            document.getElementById('importResults').style.display = 'block';
            
            if (!isDryRun) {
                applyFilters();
            }
        } else {
            alert('Import failed: ' + await resp.text());
        }
    } catch (err) {
        alert('Error during import: ' + err);
    }
}
