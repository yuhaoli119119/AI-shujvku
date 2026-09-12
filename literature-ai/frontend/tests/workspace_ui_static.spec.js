const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('workspace dashboard surface matches the research workbench spec', () => {
  const dashboard = read('pages/dashboard/index.html');
  const topnav = read('shared/topnav.js');

  // 标题改为「科研工作台」
  expect(dashboard).toContain('科研工作台');
  expect(dashboard).toContain('高效管理科研文献，跟踪解析进度，沉淀科学数据。');

  // PDF 上传是第一主操作
  expect(dashboard).toContain('id="pdfDropzone"');
  expect(dashboard).toContain('id="pdfInput"');
  expect(dashboard).toContain('拖入 PDF，或点击添加文献');
  expect(dashboard).toContain('bindDropzone()');
  expect(dashboard).toContain('handlePDFUpload(input)');

  // 当前文献库 selector
  expect(dashboard).toContain('id="librarySelect"');

  // 最近文献区
  expect(dashboard).toContain('id="recentPapers"');
  expect(dashboard).toContain('最近文献');

  // 解析任务面板
  expect(dashboard).toContain('id="jobsPanel"');
  expect(dashboard).toContain('解析任务');

  // 文献库概况
  expect(dashboard).toContain('id="libraryOverview"');
  expect(dashboard).toContain('文献库概况');

  // 复用真实 API（不新增 backend）— dashboard 用 API_BASE 变量拼接
  expect(dashboard).toContain('type-stats');
  expect(dashboard).toContain('ingest/upload/jobs');
  expect(dashboard).toContain('INGESTION_JOB_TYPES = ["local_pdf_path_ingest"]');

  // 不再显示服务器路径 / 内部目录
  expect(dashboard).not.toContain('root_path');
  expect(dashboard).not.toContain('/data/libraries/');

  // 旧模块已删除
  expect(dashboard).not.toContain('快速入口');
  expect(dashboard).not.toContain('仅保留元数据');
  expect(dashboard).not.toContain('通过DOI/检索导入');
  expect(dashboard).not.toContain('无本地 PDF');

  // TopNav 一级导航不再有入库 / Add Literature
  expect(topnav).not.toContain('入库 / Add Literature');
  expect(topnav).not.toContain('id: "ingestion"');
  expect(topnav).toContain('label: "工作台"');
  expect(topnav).toContain('label: "数据分析"');
});

test('workspace metrics avoid fake aggregate numbers', () => {
  const dashboard = read('pages/dashboard/index.html');

  // 不再把 limit=200 的长度当全库总数
  expect(dashboard).not.toContain('const total = papers.length');
  // 文献总数来自 type-stats
  expect(dashboard).toContain('loadTypeStats');
  // 已解析 / DFT 候选副指标诚实标注「基于近期样本」
  expect(dashboard).toContain('基于近期');
});
