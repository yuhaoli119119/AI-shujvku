const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('literature detail separates mechanism knowledge from DFT and preserves deep links', () => {
  const index = read('pages/literature_library/index.html');
  const controls = read('pages/literature_library/page-list-controls.js');
  const detail = read('pages/literature_library/render-detail.js');
  const content = read('pages/literature_library/detail-content.js');

  expect(index).toContain('data-tab="mechanism"');
  expect(index).toContain('正文审核');
  expect(controls).toContain('mechanism_claim: { itemType: "mechanism_claim", tab: "mechanism" }');
  expect(controls).toContain('mechanism: "mechanism"');
  expect(detail).toContain('renderJSONCards("机理知识", mechanismItems)');
  expect(detail).toContain('renderJSONCards("电化学性能", detail.electrochemical_performance_items || [])');
  expect(detail).not.toContain('renderJSONCards("机理声明", detail.mechanism_claims_items || [])');
  expect(content).toContain('params.set("paper_id", paperCode)');
});

test('detail review entrypoints use paper_code and keep manual progress separate from approval', () => {
  const content = read('pages/literature_library/detail-content.js');
  const status = read('pages/literature_library/review-status.js');
  const detail = read('pages/literature_library/render-detail.js');

  expect(content).toContain('params.set("paper_id", paperCode)');
  expect(content).toContain('/pages/content_knowledge/index.html?');
  expect(detail).toContain('"mechanism_evidence"');
  expect(detail).toContain('"writing_material"');
  expect(detail).toContain('renderContentKnowledgeLinkCard(detail, "正文审核入口"');
  expect(detail).not.toContain('"sections_writing"');
  expect(status).toContain('人工浏览标记不等于审核通过');
  expect(status).toContain('人工浏览标记为已完成');
  expect(status).not.toContain("esc(status ? '取消已完成' : '标记已完成')");
});

test('review center exposes the canonical text_review prompt action', () => {
  const html = read('pages/review_center/index.html');
  const page = read('pages/review_center/page.js');

  expect(html).toContain('<option value="text_review">正文、机理与写作审核提示词</option>');
  expect(page).toContain('text_review: {');
  expect(page).toContain('kind: "text_review"');
  expect(page).toContain('buildIdePromptForCopy(actionConfig)');
  expect(page).toContain('const template = profileTemplates[kind] || templates[kind] || compositeTemplates[kind];');
});
