const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '../pages/paper_detail/index.html'), 'utf8');
const readerCode = html.slice(html.indexOf('function renderSectionEvidence('), html.indexOf('function renderPaperDetails('));
const escapeCode = html.slice(html.indexOf('function esc('), html.indexOf('/* Safe title HTML formatter'));
const highlightCode = html.slice(html.indexOf('function highlightDeepLinkTarget('), html.indexOf('/* 非 DFT 对象'));

function fixture() {
  const relation = {kind: 'extraction_boundary', file_id: 'file-main',
    old_section: {section_id: 'old', content_sha256: 'old-hash'},
    new_sections: [{section_id: 'new', content_sha256: 'new-hash'}],
    reason: '<script>synthetic untrusted reason</script>', audit_id: 'audit-correction',
    actor: 'DS declarer', request_id: 'stable-request', created_at: '2026-10-12',
    source_sha256: 'file-hash', declaration_sha256: 'declaration-hash',
    sources: [{page_number: 2, locator: 'printed heading', quote: 'Original heading'}]};
  const old = {id: 'old', section_title: 'Old extraction', text: '  Old full bytes\r\nSI & refs.  ',
    page_start: 2, page_end: 2, display_status: 'extraction_boundary_history', correction_history: [relation],
    explanation_zh: '旧中文解读', provenance: {file_id: 'file-main', role: 'main',
      original_filename: 'synthetic.pdf', page_basis: 'source_pdf', sources: relation.sources}};
  const current = {...old, id: 'new', text: 'Original heading', section_title: 'Corrected extraction',
    display_status: 'current', conflicts_with_section_ids: ['old']};
  const conflict = {id: 'conflict', section_title: 'Scientific candidate', text: 'Another scientific candidate',
    conflicts_with_section_ids: ['new'], explanation_zh: 'AI提取章节边界纠正历史；文字不能决定类型。'};
  const legacy = {id: 'legacy', section_title: 'Legacy raw', text: 'Unknown-source original bytes'};
  return [old, current, conflict, legacy];
}

function reader(sections) {
  const nodes = new Map();
  const context = {state: {paper: {sections}}, document: {body: {}, getElementById: id => nodes.get(id)},
    navigator: {clipboard: {writeText: async value => { context.copied = value; }}}};
  vm.createContext(context);
  vm.runInContext(escapeCode + highlightCode + readerCode, context);
  return {context, nodes};
}

test('only explicit projected history moves to an expandable history group', () => {
  const sections = fixture(); const {context} = reader(sections);
  const rendered = context.renderSections(sections);
  const group = rendered.indexOf('section-history-group');
  expect(rendered.indexOf('data-record-id="new"')).toBeLessThan(group);
  expect(rendered.indexOf('data-record-id="conflict"')).toBeLessThan(group);
  expect(rendered.indexOf('data-record-id="legacy"')).toBeLessThan(group);
  expect(rendered.indexOf('data-record-id="old"')).toBeGreaterThan(group);
  expect(rendered).toContain('AI提取章节边界纠正历史（1）');
  expect(rendered).toContain('双方内容均保留');
  expect(rendered).toContain('Old full bytes\r\nSI &amp; refs.  ');
  expect(rendered).toContain('&lt;script&gt;synthetic untrusted reason&lt;/script&gt;');
  expect(rendered).not.toContain('<script>synthetic');
  expect(rendered).toContain('audit-correction');
  expect(rendered).toContain('/preview?correction=audit-correction&page=2#page=2&toolbar=1');
  expect(rendered).toContain('中文解读（待独立核验）');
});

test('unmarked conflicts and raw chapters stay current and source page links differ per page', () => {
  const sections = fixture().map(s => ({...s, display_status: 'current', correction_history: []}));
  const {context} = reader(sections);
  expect(context.renderSections(sections)).not.toContain('section-history-group');
  const s = sections[0]; s.provenance.sources.push({page_number: 3, locator: 'next page', quote: 'More original'});
  const evidence = context.renderSectionEvidence(s);
  expect(evidence).toContain('page=2#page=2');
  expect(evidence).toContain('page=3#page=3');
  expect(context.renderSections([])).toContain('暂无解析章节');
});

test('copy uses exact original bytes and navigation expands a historical chapter', async () => {
  const sections = fixture(); const {context, nodes} = reader(sections);
  const button = {textContent: '复制原文'};
  await context.copySectionOriginal({currentTarget: button}, 0);
  expect(context.copied).toBe(sections[0].text);
  expect(button.textContent).toBe('已复制原文');
  const node = {tagName: 'DETAILS', open: false, parentElement: context.document.body,
    classList: {add() {}}, scrollIntoView() {node.scrolled = true;}};
  nodes.set('sec-item-0', node);
  context.jumpToSection(0);
  expect(node.open).toBe(true); expect(node.scrolled).toBe(true);
});
