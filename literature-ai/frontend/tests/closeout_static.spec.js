const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

/* 2026-09-23 收尾静态契约：数据表列收窄 + 窄屏不出现整页横向滚动。
   只读源码，不需要后端；真实渲染另由浏览器截图验收。 */

test('data table shows only common fields plus a reaction column when no reaction is selected', () => {
  const page = read('pages/data_table/page.js');

  const currentFields = page.match(/function currentFields\(\)\s*\{[\s\S]*?\n\}/);
  expect(currentFields).not.toBeNull();
  const body = currentFields[0];

  // 未选反应时不得再合并各反应专属字段
  expect(body).not.toContain('Object.values(state.templates.reactions');
  expect(body).not.toContain('merged.push');
  // 未选反应 -> 只返回公共字段；选了反应 -> 公共字段 + 该反应字段
  expect(body).toContain('if (!reaction) return common.slice();');
  expect(body).toContain('return common.concat(reactionFields);');

  // 非全部反应模式插入「反应」列，列数随之变化
  expect(page).toContain('function showReactionColumn()');
  expect(page).toContain('function reactionLabel(row)');
  expect(page).toContain('${showReaction ?');
  expect(page).toContain('全部反应（仅公共字段）');
  expect(read('pages/data_table/index.html')).toContain('id="reactionHint"');
});

test('shared rebuild layout lets grid children shrink so narrow screens do not scroll sideways', () => {
  const css = read('pages/rebuild-shared.css');
  expect(css).toContain('.rebuild-layout > *');
  const block = css.match(/\.rebuild-layout > \*\s*\{[^}]*\}/);
  expect(block).not.toBeNull();
  expect(block[0]).toContain('min-width: 0');
  // 宽表由自身容器横向滚动
  expect(css).toMatch(/\.rebuild-table-wrap\s*\{[^}]*overflow-x:\s*auto/);
});

test('rebuild pages load the closeout asset versions', () => {
  for (const file of [
    'pages/data_table/index.html',
    'pages/summary_analysis/index.html',
    'pages/figure_assets/index.html',
    'pages/literature_library/rebuild.html',
  ]) {
    expect(read(file)).toContain('rebuild-shared.css?v=20260923-closeout');
  }
  expect(read('pages/data_table/index.html')).toContain('page.js?v=20260923-closeout');
});
