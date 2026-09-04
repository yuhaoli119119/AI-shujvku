const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('literature detail unifies paper content while preserving DFT and legacy deep links', () => {
  const index = read('pages/literature_library/index.html');
  const controls = read('pages/literature_library/page-list-controls.js');
  const detail = read('pages/literature_library/render-detail.js');
  const content = read('pages/literature_library/detail-content.js');
  const list = read('pages/literature_library/render-list.js');

  expect(index).toContain('data-tab="mechanism" onclick="switchTab(\'mechanism\')">机理');
  expect(index).not.toContain('data-tab="sections"');
  expect(index).toContain('data-tab="writing" onclick="switchTab(\'writing\')" hidden>论文内容');
  expect(controls).toContain('mechanism_claim: { itemType: "mechanism_claim", tab: "mechanism" }');
  expect(controls).toContain('mechanism: "mechanism"');
  expect(detail).toContain('renderJSONCards("机理内容", mechanismItems)');
  expect(detail).toContain('renderJSONCards("论文重点", writingItems)');
  expect(detail).toContain('renderJSONCards("电化学性能", detail.electrochemical_performance_items || [])');
  expect(detail).not.toContain('renderJSONCards("机理声明", detail.mechanism_claims_items || [])');
  expect(content).toContain('params.set("paper_id", paperCode)');
  expect(content).toContain('writing_card: { label: "论文重点"');
  expect(content).not.toContain('label: "写作卡片"');
  expect(index).toContain('有可用论文重点');
  expect(index).toContain('无可用论文重点');
  expect(index).not.toContain('已提取论文重点');
  expect(list).toContain('论文重点记录数');
  expect(list).not.toContain('关联写作卡片');
});

test('detail review entrypoints use paper_code and keep manual progress separate from approval', () => {
  const content = read('pages/literature_library/detail-content.js');
  const status = read('pages/literature_library/review-status.js');
  const detail = read('pages/literature_library/render-detail.js');

  expect(content).toContain('params.set("paper_id", paperCode)');
  expect(content).toContain('/pages/review_center/index.html?');
  expect(detail).toContain('renderContentKnowledgeLinkCard(detail, "论文内容审核"');
  expect(detail).not.toContain('"mechanism_evidence"');
  expect(detail).not.toContain('"writing_material"');
  expect(detail).not.toContain('"sections_writing"');
  expect(status).toContain('人工浏览标记不等于审核通过');
  expect(status).toContain('人工浏览标记为已完成');
  expect(status).not.toContain("esc(status ? '取消已完成' : '标记已完成')");
});

test('primary navigation and legacy review URLs converge on review center', () => {
  const topnav = read('shared/topnav.js');
  const legacyPages = [
    'pages/content_knowledge/index.html',
    'pages/dft_audit_center/index.html',
    'pages/external_analysis_workbench/index.html',
  ].map(read);

  expect(topnav).toContain('id: "ingestion"');
  expect(topnav).toContain('id: "literature"');
  expect(topnav).toContain('id: "review-center"');
  expect(topnav).toContain('id: "dft-database"');
  expect(topnav).not.toContain('id: "content-knowledge"');
  expect(topnav).not.toContain('id: "dft-audit-center"');
  expect(topnav).not.toContain('id: "external"');

  for (const page of legacyPages) {
    expect(page).toContain('window.location.replace("../review_center/index.html" + window.location.search + window.location.hash)');
  }
});

test('review center exposes the canonical text_review prompt action', () => {
  const html = read('pages/review_center/index.html');
  const page = read('pages/review_center/page.js');

  expect(html).toContain('<option value="text_review">论文内容审核提示词</option>');
  expect(page).toContain('text_review: {');
  expect(page).toContain('kind: "text_review"');
  expect(page).toContain('buildIdePromptForCopy(actionConfig)');
  expect(page).toContain('const template = profileTemplates[kind] || templates[kind] || compositeTemplates[kind];');
  expect(page).toContain('每个要用于正式写作的 section、writing_card、mechanism_claim');
  expect(page).toContain('[AI_REVIEWED] review note 只能作为说明，不能授予 RAG、写作或引用资格');
  expect(page).toContain('get_codex_item、get_paper 或 retrieve_evidence 回读');
  expect(page).toContain('安全门未通过时必须报告仍为候选');
  expect(page).toContain('正文、机理内容和论文重点');
  expect(page).not.toContain('正文、机理知识和写作卡');
  expect(page).not.toContain('也必须写 [AI_REVIEWED] review_notes');
});
