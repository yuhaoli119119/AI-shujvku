const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('paper detail active nav context is literature (所属文献库)', () => {
  const detail = read('pages/paper_detail/index.html');
  expect(detail).toContain('currentPage: "literature"');
  expect(detail).not.toContain('currentPage: "paper-detail"');
});

test('paper detail keeps four metric regions as compact cards', () => {
  const detail = read('pages/paper_detail/index.html');
  expect(detail).toContain('id="cntSections"');
  expect(detail).toContain('id="cntFigures"');
  expect(detail).toContain('id="cntDFT"');
  expect(detail).toContain('id="cntMechanism"');
  expect(detail).toContain('pd-metric');
  // 真实数据描述
  expect(detail).toContain('含引言、方法、结论');
  expect(detail).toContain('吸附能、能垒');
});

test('paper detail keeps all research tabs', () => {
  const detail = read('pages/paper_detail/index.html');
  expect(detail).toContain('switchDetailTab(\'summary\'');
  expect(detail).toContain('switchDetailTab(\'sections\'');
  expect(detail).toContain('switchDetailTab(\'figures\'');
  expect(detail).toContain('switchDetailTab(\'dft\'');
  expect(detail).toContain('switchDetailTab(\'cards\'');
  expect(detail).toContain('switchDetailTab(\'translation\'');
});

test('paper detail keeps DFT edit and translation UI', () => {
  const detail = read('pages/paper_detail/index.html');
  // DFT edit overlay 与函数保留
  expect(detail).toContain('id="dftEditOverlay"');
  expect(detail).toContain('function openDftEditDialog');
  expect(detail).toContain('function submitDftEdit');
  // translation UI 保留
  expect(detail).toContain('id="translationBtn"');
  expect(detail).toContain('function generateTranslationPreview');
  expect(detail).toContain('function setTranslationViewMode');
});

test('paper detail evidence sidebar exists and is no longer mono block', () => {
  const detail = read('pages/paper_detail/index.html');
  expect(detail).toContain('id="evidencePanel"');
  expect(detail).toContain('id="evidenceDetail"');
  // evidenceDetail 不再默认 mono class（工程信息降级）
  expect(detail).not.toContain('id="evidenceDetail" class="mono"');
  // 新 evidence card 结构
  expect(detail).toContain('pd-evidence-card');
  expect(detail).toContain('pd-evidence-status');
  expect(detail).toContain('pd-evidence-claim');
  // 技术信息折叠
  expect(detail).toContain('技术信息');
});

test('paper detail breadcrumb and header actions present', () => {
  const detail = read('pages/paper_detail/index.html');
  // breadcrumb
  expect(detail).toContain('文献库');
  expect(detail).toContain('文献详情');
  expect(detail).toContain('返回列表');
  // header actions
  expect(detail).toContain('viewPaperPdf()');
  expect(detail).toContain('reparsePaperDetail()');
  expect(detail).toContain('copyPaperMeta()');
  // 重新解析复用现有 reparse endpoint
  expect(detail).toContain('/reparse');
});

test('paper detail figures hide engineering fields by default', () => {
  const detail = read('pages/paper_detail/index.html');
  // figure card 默认精简，技术信息进 details
  expect(detail).toContain('pd-figure-card');
  expect(detail).toContain('pd-figure-tech');
  expect(detail).toContain('技术信息');
  // 不再默认暴露 Crop status / Crop source 在一级界面
  expect(detail).not.toContain('Crop status');
  expect(detail).not.toContain('No figure image path available');
});

test('paper detail overview uses normal font and analysis grid', () => {
  const detail = read('pages/paper_detail/index.html');
  // 摘要不再用 mono
  expect(detail).toContain('class="prewrap"');
  // 综合分析改 2x2 cards
  expect(detail).toContain('pd-analysis-grid');
  expect(detail).toContain('pd-analysis-item');
  // 引用预览降级到「更多信息 / 引用信息」
  expect(detail).toContain('更多信息 / 引用信息');
  // 关键词 pills 容器
  expect(detail).toContain('id="paperKeywordsPanel"');
});
