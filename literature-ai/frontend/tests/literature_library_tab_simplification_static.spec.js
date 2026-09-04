const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('literature detail exposes figures, mechanism, and DFT tabs by default', () => {
  const index = read('pages/literature_library/index.html');
  const state = read('pages/literature_library/state.js');
  const controls = read('pages/literature_library/page-list-controls.js');
  const detail = read('pages/literature_library/render-detail.js');
  const loader = read('pages/literature_library/detail-loader.js');

  expect(index).toContain('data-tab="figures" onclick="switchTab(\'figures\')">图表');
  expect(index).toContain('data-tab="mechanism" onclick="switchTab(\'mechanism\')">机理');
  expect(index).toContain('data-tab="dft" onclick="switchTab(\'dft\')">DFT 数据');
  for (const tab of ['summary', 'writing', 'translation', 'review']) {
    expect(index).toContain(`data-tab="${tab}"`);
    expect(index).toMatch(new RegExp(`<button[^>]*data-tab="${tab}"[^>]*hidden`));
    expect(index).toContain(`id="tab-${tab}"`);
  }

  expect(state).toContain('currentTab: "figures"');
  expect(controls).toContain('const DAILY_DETAIL_TABS = ["figures", "mechanism", "dft"]');
  expect(controls).toContain('mechanism: "mechanism"');
  expect(detail).toContain('renderJSONCards("机理声明", mechanismItems)');
  expect(loader).toContain('机理数据加载失败。');
  expect(controls).toContain('return DAILY_DETAIL_TABS.includes(tab) ? tab : "figures";');
  expect(detail).toContain('const activeTab = state.currentTab || "figures";');
});
