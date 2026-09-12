const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('literature library implements bidirectional filter sync and total calibration', () => {
  const controls = read('pages/literature_library/page-list-controls.js');
  const pageList = read('pages/literature_library/page-list.js');
  const jobsUpload = read('pages/literature_library/jobs-upload.js');

  expect(controls).toContain('function hasActiveFilters()');
  expect(controls).toContain('state.activeTypeFilter');
  expect(controls).toContain('state.activeStatusFilter');
  expect(controls).toContain('state.currentLibraryTotal = requestedOffset + state.papers.length');

  expect(pageList).toContain('state.activeStatusFilter = value || ""');
  expect(pageList).toContain('$("filterPdf").value = value || ""');
  expect(pageList).toContain('state.activeTypeFilter = value || ""');
  expect(pageList).toContain('$("filterPaperType").value = value || ""');

  expect(jobsUpload).not.toContain('loadPaperDetail(paperId);');
  expect(jobsUpload).toContain('openPaperDetailById(paperId);');
});

test('literature library render-list renders title links, short code, DOI, badges, and select-all', () => {
  const renderList = read('pages/literature_library/render-list.js');
  const api = read('pages/literature_library/api.js');

  expect(renderList).toContain('lib-paper-title-link');
  expect(renderList).toContain('lib-code-pill');
  expect(renderList).toContain('lib-mini-badge dft');
  expect(renderList).toContain('lib-paper-doi');
  expect(renderList).toContain('bindSelectAll()');
  expect(renderList).toContain('state.typeStats');

  expect(api).toContain('async function loadPaperTypeStats()');
  expect(api).toContain('loadPaperTypeStats()');
});

test('literature library index.html is slimmed of dead modals and has responsive styles', () => {
  const index = read('pages/literature_library/index.html');
  const css = read('pages/literature_library/page.css');

  expect(index).not.toContain('id="dftDetailDialog"');
  expect(index).not.toContain('id="metadataDiagnosticsDialog"');
  expect(index).not.toContain('id="pdfViewerOverlay"');
  expect(index).not.toContain('id="figureLightboxOverlay"');

  expect(css).toContain('@media (max-width: 1280px)');
  expect(css).toContain('@media (max-width: 960px)');
  expect(css).toContain('.lib-paper-title-link');
  expect(css).toContain('.lib-code-pill');
  expect(css).toContain('.lib-paper-doi');
});
