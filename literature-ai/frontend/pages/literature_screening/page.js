let currentPapers = [];
/* ---- URL 状态同步（shared/view-state.js）：筛选写进地址栏，
   从详情页按「返回」时原样还原；带参数的 URL 粘到新标签也能还原同样的视图。 ---- */
const VIEW_PARAMS = {
  year_min: { id: 'filterYearMin', default: '' },
  year_max: { id: 'filterYearMax', default: '' },
  journal: { id: 'filterJournalInc', default: '' },
  journal_x: { id: 'filterJournalExc', default: '' },
  if_min: { id: 'filterIFMin', default: '' },
  if_max: { id: 'filterIFMax', default: '' },
  needs_meta: { id: 'filterNeedsMetadata', default: '' },
  has_pdf: { id: 'filterHasPdf', default: '' },
  has_parsed: { id: 'filterHasParsedText', default: '' },
  has_ext: { id: 'filterHasExtractionOutput', default: '' },
  has_ver: { id: 'filterHasVerifiedEvidence', default: '' },
  has_sver: { id: 'filterHasSafeVerifiedEvidence', default: '' },
  excl_cite: { id: 'filterExcludeFromCitation', default: '' },
  prio: { id: 'filterCitationPriority', default: '' }
};
let viewState = null;
/* 把表单当前值快照写回 URL（Clear Filters / Apply Filters 之后修正 URL） */
function syncUrl() {
  if (!viewState) return;
  Object.keys(VIEW_PARAMS).forEach(key => {
    const el = document.getElementById(VIEW_PARAMS[key].id);
    if (el) viewState[key] = el.type === 'checkbox' ? (el.checked ? '1' : '') : String(el.value);
  });
  ViewState.write(viewState, VIEW_PARAMS);
}
let pendingAction = null;
let pendingData = null;

// 手机（<=700px）默认折叠「筛选」区块；桌面/平板（>=701px）保持展开（原布局不变）。
const filtersPanelMq = window.matchMedia('(max-width: 700px)');

function syncFiltersPanel() {
    const panel = document.getElementById('filtersPanel');
    if (panel) panel.open = !filtersPanelMq.matches;
}

document.addEventListener('DOMContentLoaded', () => {
    // 这个页面有 #topnav-mount 却从来没有初始化导航，导致页面上完全没有顶栏、无法跳回其它页面。
    // 注意：shared/topnav.js 里是 `class TopNav`（词法作用域全局），window.TopNav 永远是 undefined，
    // 用 window.TopNav 做判断会让顶栏永远挂载不上；这里改成 typeof 判断，调用方式不变。
    if (typeof TopNav !== 'undefined' && TopNav && typeof TopNav.init === 'function') {
        TopNav.init({ currentPage: "screening", mountId: "topnav-mount" });
    }
    syncFiltersPanel();
    if (filtersPanelMq.addEventListener) {
        filtersPanelMq.addEventListener('change', syncFiltersPanel);
    }
    /* 先读 URL 填控件，再用同样的值发第一次查询（只发一次）。
       本页是「显式应用」语义（要点 Apply Filters 才查询），所以不接 ViewState.bind 的
       控件联动：否则地址栏会先于列表更新，出现「URL 说筛了 79 条、列表还是 99 条」，
       进详情再返回还会拿到用户当时没见过的筛选视图。URL 统一由 applyFilters() 末尾的
       syncUrl() 写，保证地址栏永远等于当前看到的列表。 */
    viewState = ViewState.apply(ViewState.read(VIEW_PARAMS), VIEW_PARAMS);
    ViewState.bindDetailLinks('literature_screening');
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
        // /api/library/papers/filter 返回 {total, items, safety}；
        // 旧代码只认 data.papers，于是 currentPapers 变成整个对象，
        // renderTable() 里 forEach 抛错 -> 表格永远空白（手机上的那个 console error 就是它）。
        if (Array.isArray(data)) {
            currentPapers = data;
        } else if (Array.isArray(data.papers)) {
            currentPapers = data.papers; // 兼容旧字段名
        } else if (Array.isArray(data.items)) {
            currentPapers = data.items;
        } else {
            currentPapers = [];
        }
        updateResultCount(typeof data.total === 'number' ? data.total : currentPapers.length);
        renderTable();
    } catch (err) {
        console.error('Error applying filters', err);
    }
    syncUrl();
}

function updateResultCount(count) {
    const el = document.getElementById('resultCount');
    if (!el) return;
    el.textContent = count === 0 ? 'No papers' : `${count} paper${count === 1 ? '' : 's'}`;
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

    if (currentPapers.length === 0) {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan="16" class="empty-row">No papers match the current filters.</td>';
        tbody.appendChild(tr);
        return;
    }

    currentPapers.forEach(paper => {
        const tr = document.createElement('tr');

        const safeVerifiedCount = paper.safe_verified_evidence_count !== undefined ? paper.safe_verified_evidence_count : '-';
        const verifiedCount = paper.verified_evidence_count !== undefined ? paper.verified_evidence_count : '-';

        let ifStatusDisplay = paper.impact_factor_status || '';
        if (!paper.impact_factor) {
            ifStatusDisplay = 'needs_metadata';
        }

        // data-label 带上列名：<=700px 时 shared/responsive.css 的 table.litai-stack
        // 规则会把每个 td 渲染成「列名 + 值」的卡片行；标题和复选框不带列名。
        // 注意：title 内层 HTML 保持原样 —— 库里有 11 条标题带 <sub> 下标标记，
        // 必须继续按 HTML 渲染（转义会把 <sub> 显示成字面标签，桌面行高也会变）；
        // 只有 title 属性做转义，避免标题里的引号截断属性。
        tr.innerHTML = `
            <td class="cell-select"><input type="checkbox" class="row-checkbox" value="${esc(paper.id)}"></td>
            <td class="title-col" title="${esc(paper.title || '')}"><a class="screening-title-link" href="../paper_detail/index.html?paper_id=${esc(paper.id)}&from=literature_screening" title="${esc(paper.title || '')}">${paper.title || '-'}</a></td>
            <td data-label="Year">${esc(paper.year || '-')}</td>
            <td data-label="Journal">${esc(paper.journal || '-')}</td>
            <td data-label="Impact Factor">${esc(paper.impact_factor || '-')}</td>
            <td data-label="IF Year">${esc(paper.impact_factor_year || '-')}</td>
            <td data-label="IF Source">${esc(paper.impact_factor_source || '-')}</td>
            <td data-label="IF Status">${esc(ifStatusDisplay)}</td>
            <td data-label="PDF">${paper.has_pdf ? 'Yes' : 'No'}</td>
            <td data-label="Parsed Text">${paper.has_parsed_text ? 'Yes' : 'No'}</td>
            <td data-label="Ext. Output">${paper.has_extraction_output ? 'Yes' : 'No'}</td>
            <td data-label="Verified Ev. Count">${esc(verifiedCount)}</td>
            <td data-label="Safe Ver. Ev. Count">${esc(safeVerifiedCount)}</td>
            <td data-label="Exclude Citation">${paper.exclude_from_citation ? 'Yes' : 'No'}</td>
            <td data-label="Priority">${esc(paper.citation_priority || '-')}</td>
            <td data-label="User Note">${esc(paper.user_note || '')}</td>
        `;
        tbody.appendChild(tr);
    });
}

function esc(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
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
