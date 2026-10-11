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
            throw new Error('HTTP ' + resp.status);
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
        const body = document.getElementById('resultsTableBody');
        body.innerHTML = '';
        const row = body.insertRow(), cell = row.insertCell();
        cell.colSpan = 6;
        cell.className = 'empty-state';
        updateSelection();
        cell.textContent = '文献筛选加载失败：' + err.message + '。';
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'btn ghost'; retry.textContent = '重试';
        retry.addEventListener('click', applyFilters); cell.appendChild(retry);
        document.getElementById('resultCount').textContent = '未能读取结果';
    }
    syncUrl();
}

function updateResultCount(count) {
    const el = document.getElementById('resultCount');
    if (!el) return;
    el.textContent = `${count} 篇正文`;
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

const PRIORITY_LABELS = { high: '高优先级', medium: '中优先级', low: '低优先级', exclude: '排除' };
const IF_STATUS_LABELS = { needs_metadata: '待补信息', matched: '已匹配', verified: '已核对', available: '已登记', imported: '已导入', missing: '待补信息', unknown: '来源未登记', manual: '人工登记', ok: '已登记' };
function renderTitle(title) {
    // Preserve scientific subscripts without inserting other stored HTML.
    const doc = new DOMParser().parseFromString(String(title || '未命名论文'), 'text/html');
    function safe(node) {
        if (node.nodeType === Node.TEXT_NODE) return esc(node.textContent);
        const content = Array.from(node.childNodes).map(safe).join('');
        const tag = node.nodeName.toLowerCase();
        return ['sub', 'sup', 'i', 'b', 'em'].includes(tag) ? `<${tag}>${content}</${tag}>` : content;
    }
    return safe(doc.body);
}
function renderTable() {
    const tbody = document.getElementById('resultsTableBody');
    tbody.innerHTML = '';
    document.getElementById('selectAllCheckbox').checked = false;
    document.getElementById('selectAllCheckbox').indeterminate = false;
    if (!currentPapers.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-row">没有符合条件的文献。可放宽筛选条件后重试。</td></tr>';
        updateSelection();
        return;
    }
    currentPapers.forEach(paper => {
        const tr = document.createElement('tr');
        const hasIF = paper.impact_factor !== null && paper.impact_factor !== undefined && paper.impact_factor !== '';
        const status = hasIF ? (IF_STATUS_LABELS[paper.impact_factor_status] || '已登记') : '待补信息';
        const tags = [['PDF',paper.has_pdf],['全文',paper.has_parsed_text],['提取结果',paper.has_extraction_output]].map(([name,yes]) => `<span class="status-badge ${yes ? 'available' : ''}">${yes ? '有' : '无'}${name}</span>`).join('');
        tr.innerHTML = `
            <td class="cell-select"><input type="checkbox" class="row-checkbox" aria-label="选择文献" value="${esc(paper.id)}"></td>
            <td class="title-col">${paper.paper_code ? `<span class="paper-code">${esc(paper.paper_code)}</span>` : ''}<a class="screening-title-link" href="../paper_detail/index.html?paper_id=${esc(paper.id)}&from=literature_screening">${renderTitle(paper.title)}</a></td>
            <td data-label="发表信息"><div>${esc(paper.journal || '期刊未登记')}</div><div class="cell-subtle">${esc(paper.year ?? '年份未登记')}</div></td>
            <td data-label="影响因子"><strong>${esc(hasIF ? paper.impact_factor : '—')}</strong><div class="cell-subtle">${esc(status)}</div><details class="record-details"><summary>来源详情</summary><dl><dt>指标年份</dt><dd>${esc(paper.impact_factor_year ?? '未登记')}</dd><dt>来源</dt><dd>${esc(paper.impact_factor_source === 'unknown' ? '未登记' : paper.impact_factor_source || '未登记')}</dd></dl></details></td>
            <td data-label="资料与证据"><div class="status-tags">${tags}</div><details class="record-details"><summary>证据详情</summary><dl><dt>核验证据数</dt><dd>${esc(paper.verified_evidence_count ?? '未登记')}</dd><dt>安全核验证据数</dt><dd>${esc(paper.safe_verified_evidence_count ?? '未登记')}</dd></dl></details></td>
            <td data-label="引用设置"><span class="status-badge ${paper.exclude_from_citation ? 'warning' : 'priority'}">${paper.exclude_from_citation ? '不引用' : '未排除'}</span><div class="cell-subtle">${esc(PRIORITY_LABELS[paper.citation_priority] || '未设优先级')}</div>${paper.user_note ? `<details class="record-details"><summary>备注</summary><p class="user-note">${esc(paper.user_note)}</p></details>` : ''}</td>`;
        tr.querySelector('.row-checkbox').addEventListener('change', updateSelection);
        tbody.appendChild(tr);
    });
    updateSelection();
}
function updateSelection() {
    const boxes = Array.from(document.querySelectorAll('.row-checkbox'));
    const count = boxes.filter(cb => cb.checked).length;
    document.getElementById('selectionCount').textContent = `已选 ${count} 篇`;
    document.getElementById('bulkPriorityBtn').disabled = count === 0;
    document.getElementById('bulkExcludeBtn').disabled = count === 0;
    const all = document.getElementById('selectAllCheckbox');
    all.checked = boxes.length > 0 && count === boxes.length;
    all.indeterminate = count > 0 && count < boxes.length;
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
    updateSelection();
}

function getSelectedIds() {
    const checkboxes = document.querySelectorAll('.row-checkbox:checked');
    return Array.from(checkboxes).map(cb => cb.value);
}

function markSelectedDoNotCite() {
    const ids = getSelectedIds();
    if (ids.length === 0) return alert('请先选择文献。');
    
    pendingAction = 'bulkEligibility';
    pendingData = {
        paper_ids: ids,
        updates: {
            exclude_from_citation: true
        }
    };
    
    showConfirmModal('设为不引用', `确认将选中的 ${ids.length} 篇文献设为不引用？`);
}

function setPriorityForSelected() {
    const ids = getSelectedIds();
    if (ids.length === 0) return alert('请先选择文献。');
    const priority = document.getElementById('bulkPrioritySelect').value;
    
    pendingAction = 'bulkEligibility';
    pendingData = {
        paper_ids: ids,
        updates: {
            citation_priority: priority
        }
    };
    
    showConfirmModal('设置引用优先级', `确认将选中的 ${ids.length} 篇文献设为${PRIORITY_LABELS[priority]}？`);
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
                alert('批量更新失败：' + await resp.text());
            }
        } catch (err) {
            alert('更新失败：' + err);
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
        alert('请先粘贴 CSV 或 JSON 内容。');
        return;
    }
    
    if (!isDryRun) {
        const confirmReal = confirm('确认导入并保存影响因子信息？此次操作会更新文献的发表信息。');
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
            alert('导入失败：' + await resp.text());
        }
    } catch (err) {
        alert('导入失败：' + err);
    }
}
