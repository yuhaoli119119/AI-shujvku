const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '../pages/paper_detail/index.html'), 'utf8');
const between = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));
const inventoryCode = between('function visualLabelKey(', 'function renderStoredPaperTable(');
const readingCode = between('function buildFigureReadingData(', 'function renderFigureReadingHtml(');
const paperId = 'paper-main';
const main = {id: 'main', paper_id: paperId, role: 'main', sha256: 'same-pdf'};
const si = {id: 'si', paper_id: paperId, role: 'si', sha256: 'si-pdf'};

function inventoryFor(originals, assets, files = [main, si]) {
  const state = {
    paperId, paper: {source_pdf_sha256: main.sha256, figures: originals, tables: []},
    rebuildAssets: assets, rebuildFiles: files,
  };
  const context = {
    state,
    currentSourceFile: id => files.find(file => file.id === id && file.paper_id === paperId) || null,
    currentAssetFigure: asset => ({_currentAsset: asset, figure_label: asset.figure_label, page: asset.page_numbers?.[0]}),
    supersededCurrentAsset: asset => asset.provenance?.lifecycle === 'superseded',
    hasEstablishedReading: figure => Boolean(figure.reading_explanation?.detailed_explanation_zh),
  };
  vm.createContext(context);
  vm.runInContext(`${inventoryCode}\nresult = visualInventory();`, context);
  return context.result;
}

function asset(id, label, page, fileId = main.id, extra = {}) {
  return {id, paper_id: paperId, file_id: fileId, asset_type: 'figure',
    figure_label: label, page_numbers: [page], ...extra};
}

test('same-source current whole figures replace legacy crops while panels and SI remain distinct', () => {
  const reading = {detailed_explanation_zh: '旧版有来源解读', evidence_locators: [{paper_id: paperId, page: 7}]};
  const originals = [
    {id: 'old-fig2', paper_id: paperId, figure_label: 'fig_2', page: 7, reading_explanation: reading},
    {id: 'old-ga', paper_id: paperId, figure_label: 'graphical_abstract', page: 3},
    {id: 'old-fig3', paper_id: paperId, figure_label: 'fig_3', page: 9},
  ];
  const assets = [
    asset('new-fig2', 'Figure 2', 7),
    asset('new-panel-a', 'Figure 2', 7, main.id, {subfigure_label: '(a)'}),
    asset('new-ga', 'Graphical abstract', 3),
    asset('si-s2', 'Figure S2', 7, si.id),
  ];
  const result = inventoryFor(originals, assets);
  expect(result.figures.map(figure => figure._currentAsset?.id || figure.id)).toEqual([
    'new-ga', 'new-fig2', 'new-panel-a', 'si-s2', 'old-fig3',
  ]);
  expect(result.figures.find(figure => figure._currentAsset?.id === 'new-fig2')._fallbackReadingFigure.id).toBe('old-fig2');
  expect(result.figures.filter(figure => figure._currentAsset?.id === 'new-fig2')).toHaveLength(1);
});

test('different source, page and figure number cannot replace a legacy figure', () => {
  const originals = [{id: 'old', paper_id: paperId, figure_label: 'fig_2', page: 7}];
  const otherMain = {id: 'other-main', paper_id: paperId, role: 'main', sha256: 'different-pdf'};
  const result = inventoryFor(originals, [
    asset('other-page', 'Figure 2', 8),
    asset('other-number', 'Figure 3', 7),
    asset('si-number', 'Figure S2', 7, si.id),
    asset('other-edition', 'Figure 2', 7, otherMain.id),
  ], [main, si, otherMain]);
  expect(result.figures.map(figure => figure._currentAsset?.id || figure.id)).toContain('old');
  expect(result.figures).toHaveLength(5);
});

test('legacy reading is used only when the current reading is incomplete and provenance matches', () => {
  const fallback = {reading_explanation: {summary_zh: '旧摘要', detailed_explanation_zh: '旧版来源解读'}};
  const context = {
    fallback,
    currentScientificReading: () => ({type: 'current_incomplete'}),
    isPlaceholderReading: () => false,
  };
  vm.createContext(context);
  vm.runInContext(`${readingCode}\nresult = buildFigureReadingData({_currentAsset: {}, _fallbackReadingFigure: fallback});`, context);
  expect(context.result.summary).toBe('旧摘要');
  expect(context.result.core).toBe('旧版来源解读');
  context.currentScientificReading = () => ({type: 'current_scientific', summary: '新版解读'});
  vm.runInContext('result = buildFigureReadingData({_currentAsset: {}, _fallbackReadingFigure: fallback});', context);
  expect(context.result.summary).toBe('新版解读');

  const invalid = {id: 'old', paper_id: paperId, figure_label: 'fig_2', page: 7,
    reading_explanation: {detailed_explanation_zh: '错误来源', evidence_locators: [{paper_id: 'other-paper', page: 7}]}};
  const result = inventoryFor([invalid], [asset('new', 'Figure 2', 7)]);
  expect(result.figures[0]._fallbackReadingFigure).toBeUndefined();
});
