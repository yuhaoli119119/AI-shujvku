const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('literature library keeps the PDF-first daily action surface', () => {
  const index = read('pages/literature_library/index.html');
  const controls = read('pages/literature_library/page-list-controls.js');

  expect(index).toContain('onclick="openAddLiteraturePanel(\'pdf\')">添加 PDF');
  expect(index).toContain('onclick="openSelectedPdfEvidence()">查看 PDF');
  expect(index).toContain('onclick="refreshSelectedPaperDetailFromHeader()">刷新详情');
  expect(index).toContain('onclick="reparseSelectedPaper()">重新解析');
  expect(index).not.toContain('onclick="openCreateLibraryDialog()"');
  expect(index).not.toContain('onclick="openImportLibraryDialog()"');
  expect(index).not.toContain('onclick="removeCurrentLibrary()"');
  expect(index).not.toContain('onclick="openExtractionJobCenter()"');
  expect(index).not.toContain('onclick="openMetadataDiagnostics()"');
  expect(index).not.toContain('onclick="rerunExtraction()"');
  expect(index).not.toContain('onclick="addToEvidencePack()"');
  expect(index).not.toContain('onclick="openAggregateView()"');
  expect(index).not.toContain('onclick="promptAddRelationship(state.selectedPaperId)"');
  expect(index).not.toContain('data-add-mode="doi"');
  expect(index).not.toContain('data-add-mode="online"');
  expect(index).not.toContain('data-add-mode="folder"');
  expect(index).toContain('onclick="copyPaperIdentity()"');
  expect(index).toContain('onclick="triggerSupplementaryUpload()"');
  expect(index).toContain('onclick="resetCurrentPaperUpload(event)"');
  expect(index).toContain('onclick="openDeletePaperDialog(event)"');
  expect(controls).toContain('添加 PDF');
  expect(controls).not.toContain('在线检索');
});

test('literature library hides dynamic review and maintenance shortcuts without removing their implementations', () => {
  const css = read('pages/literature_library/page.css');
  const workflow = read('pages/literature_library/dft-workflow.js');
  const actions = read('pages/literature_library/page-actions.js');

  expect(css).toContain('button[data-role="classify-unknown-btn"]');
  expect(css).toContain('button[onclick="openSelectedReviewCenter()"]');
  expect(workflow).toContain('onclick="openSelectedReviewCenter()"');
  expect(actions).toContain('function ensureClassificationToolbarButton()');
});
