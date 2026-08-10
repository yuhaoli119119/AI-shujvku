import { state, readUrlState, writeUrlState } from './state.js';

const CATEGORY_LABELS = {
  mechanism_evidence: '机理内容',
  performance_evidence: '性能证据',
  dft_evidence: 'DFT 证据',
  figure_table_evidence: '图表证据',
  material_evidence: '材料信息',
  method_evidence: '方法信息',
  writing_material: '论文重点内容',
  review_viewpoint: '综述观点',
  uncertainty_note: '争议 / 风险',
  draft_evidence_check: '草稿证据核验',
};

const PRESERVED_SCOPE_KEYS = ['run_id'];

export function initFilters(onChange) {
  const form = document.querySelector('#knowledgeFilters');
  const category = document.querySelector('#categorySelect');
  category.innerHTML = '<option value="">全部类别</option>' + Object.entries(CATEGORY_LABELS).map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
  const saved = readUrlState();
  const savedView = ['content', 'audit'].includes(saved.result_view) ? saved.result_view : 'content';
  form.elements.result_view.value = savedView;
  ['query', 'paper_id', 'library_name', 'category', 'review_status', 'citation_status'].forEach((name) => { if (saved[name] != null && form.elements[name]) form.elements[name].value = saved[name]; });
  form.elements.include_candidates.checked = savedView === 'audit' && saved.include_candidates !== 'false';
  form.elements.include_blocked.checked = saved.include_blocked === 'true';
  updateCandidateControl(form);
  state.filters = {
    ...saved,
    result_view: savedView,
    include_candidates: form.elements.include_candidates.checked ? 'true' : 'false',
  };
  const toggle = document.querySelector('#moreFiltersToggle');
  toggle.addEventListener('click', () => { const box = document.querySelector('#moreFilters'); box.hidden = !box.hidden; toggle.setAttribute('aria-expanded', String(!box.hidden)); });
  let timer;
  form.query.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => submit(form, onChange), 300); });
  form.addEventListener('submit', (event) => { event.preventDefault(); submit(form, onChange); });
  form.querySelectorAll('select,input[type="checkbox"]').forEach((element) => element.addEventListener('change', () => submit(form, onChange)));
}

function submit(form, onChange) {
  const previousView = state.filters.result_view || 'content';
  const nextView = form.elements.result_view.value;
  const data = new FormData(form);
  const preservedScope = Object.fromEntries(
    PRESERVED_SCOPE_KEYS
      .filter((key) => state.filters[key])
      .map((key) => [key, state.filters[key]]),
  );
  state.filters = preservedScope;
  for (const [key, value] of data.entries()) {
    if (String(value).trim()) state.filters[key] = String(value).trim();
  }
  if (!['content', 'audit'].includes(state.filters.result_view)) state.filters.result_view = 'content';
  if (previousView !== 'audit' && nextView === 'audit') form.elements.include_candidates.checked = true;
  if (state.filters.result_view === 'content') form.elements.include_candidates.checked = false;
  state.filters.include_candidates = form.elements.include_candidates.checked ? 'true' : 'false';
  state.filters.include_blocked = form.elements.include_blocked.checked ? 'true' : 'false';
  state.selectedId = null;
  updateCandidateControl(form);
  writeUrlState({ resetOffset: true });
  onChange();
}

function updateCandidateControl(form) {
  const auditView = form.elements.result_view.value === 'audit';
  const toggle = form.elements.include_candidates;
  toggle.disabled = !auditView;
  if (!auditView) toggle.checked = false;
  document.querySelector('#candidateToggleLabel').textContent = auditView
    ? '显示该视图中的候选审计记录'
    : '外部候选已与论文内容分流';
}
