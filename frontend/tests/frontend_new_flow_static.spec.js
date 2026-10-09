const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

/* 前端「新流程适配」静态契约（2026-09-22 计划书 A~F）。
   这些断言只看源码，不需要后端；真实渲染另由浏览器截图验收。 */

test('literature library sidebar only keeps new-flow stats and quick filters', () => {
  const index = read('pages/literature_library/index.html');
  const page = read('pages/literature_library/page.js');

  // 统计卡片只剩「文献总数」「有 PDF」
  expect(index).toContain('id="statTotal"');
  expect(index).toContain('id="statPdf"');
  expect(index).not.toContain('id="statParsed"');
  expect(index).not.toContain('id="statReview"');
  expect(index).not.toContain('已解析');
  expect(index).not.toContain('待审核');

  // 快捷筛选只剩「全部文献」「有 PDF」
  expect(index).toContain('data-quick="all"');
  expect(index).toContain('data-quick="pdf"');
  expect(index).not.toContain('data-quick="parsed"');
  expect(index).not.toContain('data-quick="review"');
  expect(index).not.toContain('id="quickParsed"');
  expect(index).not.toContain('id="quickReview"');

  // 页面脚本不再写入已删除的统计节点，也不再用旧解析/审核做快捷筛选
  expect(page).not.toContain('statParsed');
  expect(page).not.toContain('statReview');
  expect(page).not.toContain('quickParsed');
  expect(page).not.toContain('quickReview');
  expect(page).not.toContain('state.quick==="parsed"');
  expect(page).not.toContain('state.quick==="review"');
});

test('literature library list rows drop old parse and review status chips', () => {
  const page = read('pages/literature_library/page.js');
  const controls = read('pages/literature_library/page-list-controls.js');

  const renderStatus = page.match(/function renderStatus\(p\)\{[^\n]*\}/);
  expect(renderStatus).not.toBeNull();
  expect(renderStatus[0]).not.toContain('"已解析"');
  expect(renderStatus[0]).not.toContain('"未解析"');
  expect(renderStatus[0]).not.toContain('"待审核"');
  expect(renderStatus[0]).toContain('"DFT"');

  expect(controls).not.toContain('status-chip parsed');
  expect(controls).not.toContain('已解析');
  expect(controls).toContain('PDF已上传');
});

test('top navigation has no review center entry and no legacy extraction protocol', () => {
  const topnav = read('shared/topnav.js');
  const navItems = topnav.slice(topnav.indexOf('static NAV_ITEMS'), topnav.indexOf('static NAV_ALIASES'));
  const moreItems = topnav.slice(topnav.indexOf('static MORE_ITEMS'), topnav.indexOf('static syncScrollHints'));

  expect(navItems).not.toContain('审核中心');
  expect(navItems).not.toContain('review_center');
  expect(moreItems).not.toContain('高级提取协议');
  expect(moreItems).not.toContain('extraction_workflow');

  // 一级导航保留工作台 / 文献库 / DFT 数据库 / 数据分析 / 设置
  for (const label of ['label: "工作台"', 'label: "文献库"', 'label: "DFT 数据库"', 'label: "数据分析"', 'label: "设置"']) {
    expect(navItems).toContain(label);
  }

  // 「更多」菜单里新流程四页按序可见，写作排在四页之后
  const order = ['AI 提取', 'AI 图表整理', 'AI 数据表', 'AI 汇总分析', '本地 AI 写作'];
  const positions = order.map(label => moreItems.indexOf(`label: "${label}"`));
  positions.forEach(position => expect(position).toBeGreaterThan(-1));
  for (let i = 1; i < positions.length; i += 1) {
    expect(positions[i]).toBeGreaterThan(positions[i - 1]);
  }
});

test('dashboard metrics and recent list avoid old parse/review semantics', () => {
  const dashboard = read('pages/dashboard/index.html');

  expect(dashboard).not.toContain('quick=parsed');
  expect(dashboard).not.toContain('quick=review');
  expect(dashboard).not.toContain('metricParsed');
  expect(dashboard).not.toContain('metricPending');
  expect(dashboard).not.toContain('>已解析<');
  expect(dashboard).not.toContain('解析后可进入审核中心查看结果');

  // 保留新流程相关指标，且副指标诚实标注样本范围
  expect(dashboard).toContain('id="metricTotal"');
  expect(dashboard).toContain('id="metricPdf"');
  expect(dashboard).toContain('基于近期样本');

  // 不能出现编造的全库外推
  expect(dashboard).not.toContain('Math.round(total * 1.5)');

  // 上传入口文案不得承诺自动解析
  expect(dashboard).not.toContain('自动完成解析、保存和结构化抽取');
  expect(dashboard).not.toContain('后台解析任务');
  expect(dashboard).not.toContain('上传或解析任务创建失败');
  expect(dashboard).toContain('不会自动启动解析或抽取');
  expect(dashboard).toContain('未启动自动解析');
});

test('data table resolves the paper reaction before applying the default filter', () => {
  const page = read('pages/data_table/page.js');

  expect(page).toContain('async function resolvePaperReaction()');
  expect(page).toContain('paper_id: state.paperId');
  expect(page).toContain('await resolvePaperReaction()');
  expect(page).not.toContain('if (state.paperId) $("reactionFilter").value = "";');
});

test('upload surfaces report completion instead of an old parse chain', () => {
  const ingestion = read('pages/ingestion/index.html');

  expect(ingestion).toContain('上传完成');
  expect(ingestion).not.toContain('上传或解析失败');
  expect(ingestion).not.toContain('正在上传并创建后台解析任务');
});
