const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

/* AI 提取工作台静态契约（2026-09-23）。
   pages/literature_library/rebuild.html 从「检索表 + 上传表单」改成
   任务导向的四步工作台；这些断言只看源码，真实渲染另由浏览器截图验收。 */

test('rebuild page is a task oriented workbench with the four extraction steps', () => {
  const html = read('pages/literature_library/rebuild.html');

  expect(html).toContain('AI 提取工作台');
  expect(html).toContain('上传只保存文件，不启动旧解析链');

  // 四步流程卡片，各自带状态徽标
  for (const [title, badge] of [
    ['文件关联', 'id="step1Status"'],
    ['AI 图表整理', 'id="step2Status"'],
    ['AI 按反应模板填表', 'id="step3Status"'],
    ['汇总分析', 'id="step4Status"'],
  ]) {
    expect(html).toContain(`>${title}<`);
    expect(html).toContain(badge);
  }
  expect(html).toContain('SRR/HER/OER/ORR/CO2RR');
  expect(html).toContain('从真实 PDF 裁图');
  expect(html).toContain('条件不一致不混合');

  // 三步跳转 + AI 工作包复制
  expect(html).toContain('href="../figure_assets/index.html"');
  expect(html).toContain('href="../data_table/index.html"');
  expect(html).toContain('href="../summary_analysis/index.html"');
  expect(html).toContain('id="copyPackage"');

  // 上传表单默认收起，由折叠按钮控制
  expect(html).toMatch(/<button class="rebuild-btn" id="uploadToggle" type="button" aria-expanded="false"[^>]*>上传文件<\/button>/);
  expect(html).toMatch(/<form id="uploadForm"[^>]*hidden>/);
  expect(html).toContain('不启动自动解析');

  // 保持既有 rebuild 资源版本约定与窄屏 grid 约定
  expect(html).toContain('rebuild-shared.css?v=20260923-closeout');
  expect(html).toContain('class="rebuild-layout"');
});

test('rebuild page drops the full library table and keeps a compact selectable result list', () => {
  const html = read('pages/literature_library/rebuild.html');

  expect(html).not.toContain('id="paperRows"');
  expect(html).not.toContain('rebuild-table');
  expect(html).not.toContain('id="pager"');
  expect(html).toContain('id="searchResults"');
  expect(html).toContain('id="resultSummary"');
  expect(html).toContain('id="selectedPaper"');
});

test('workbench script drives selection, step counts and the disabled empty state', () => {
  const js = read('pages/literature_library/rebuild.js');

  // 搜索 + 详情两个只读接口
  expect(js).toContain('/api/rebuild/papers?');
  expect(js).toContain('/api/rebuild/papers/${id}');
  // URL 带 paper_id 时自动选中
  expect(js).toContain('get("paper_id")');

  // 状态徽标文案与计数
  expect(js).toContain('未关联文件');
  expect(js).toContain('已关联 ${files} 个文件');
  expect(js).toContain('暂无图表对象');
  expect(js).toContain('已有 ${assets} 个图表对象');
  expect(js).toContain('暂无数据行');
  expect(js).toContain('已有 ${rows} 行数据');
  expect(js).toContain('先选择论文');

  // 未选论文时四张卡片灰化 + 主按钮不可点
  expect(js).toContain('classList.toggle("is-disabled"');
  expect(js).toContain('setAttribute("aria-disabled", "true")');
  expect(js).toContain('setAttribute("aria-disabled", "false")');

  // 跳转链接带 paper_id
  expect(js).toContain('?paper_id=${encodeURIComponent(paper.id)}');
  expect(js).toContain('ai-work-package');

  // 源 PDF 预览只在有 PDF 时出现
  expect(js).toContain('paper.has_pdf');
  expect(js).toContain('source-pdf/preview');

  // 上传成功刷新计数
  expect(js).toContain('await selectPaper(state.selected.id)');
});

