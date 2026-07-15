import { state, readUrlState, writeUrlState } from './state.js';

const CATEGORY_LABELS = {
  mechanism_evidence: '机理证据卡', performance_evidence: '性能证据卡', dft_evidence: 'DFT 证据卡',
  figure_table_evidence: '图表证据卡', material_evidence: '材料信息卡', method_evidence: '方法信息卡',
  writing_material: '写作素材卡', review_viewpoint: '综述观点卡', uncertainty_note: '争议 / 风险卡',
  draft_evidence_check: '草稿证据核验',
};

export function initFilters(onChange) {
  const form = document.querySelector('#knowledgeFilters');
  const category = document.querySelector('#categorySelect');
  category.innerHTML = '<option value="">全部类别</option>' + Object.entries(CATEGORY_LABELS).map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
  const saved = readUrlState();
  ['query', 'paper_id', 'library_name', 'category', 'review_status', 'citation_status'].forEach((name) => { if (saved[name] != null && form.elements[name]) form.elements[name].value = saved[name]; });
  form.elements.include_candidates.checked = saved.include_candidates !== 'false';
  form.elements.include_blocked.checked = saved.include_blocked === 'true';
  const toggle = document.querySelector('#moreFiltersToggle');
  toggle.addEventListener('click', () => { const box = document.querySelector('#moreFilters'); box.hidden = !box.hidden; toggle.setAttribute('aria-expanded', String(!box.hidden)); });
  let timer;
  form.query.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => submit(form, onChange), 300); });
  form.addEventListener('submit', (event) => { event.preventDefault(); submit(form, onChange); });
  form.querySelectorAll('select,input[type="checkbox"]').forEach((element) => element.addEventListener('change', () => submit(form, onChange)));
}

function submit(form, onChange) {
  const data = new FormData(form);
  state.filters = {};
  for (const [key, value] of data.entries()) if (String(value).trim()) state.filters[key] = String(value).trim();
  state.filters.include_candidates = form.elements.include_candidates.checked ? 'true' : 'false';
  state.filters.include_blocked = form.elements.include_blocked.checked ? 'true' : 'false';
  state.selectedId = null;
  writeUrlState({ resetOffset: true });
  onChange();
}
