const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const { readPageSource } = require('./helpers/read-page-source');

const REPO_ROOT = path.resolve(__dirname, '..');

function readFrontendFile(relativePath) {
  return fs.readFileSync(path.join(REPO_ROOT, relativePath), 'utf8');
}

test('review center is the formal AI prompt entrypoint surface', () => {
  const reviewCenter = readPageSource('pages/review_center/index.html');
  const dftReviewerScope = reviewCenter.slice(
    reviewCenter.indexOf('      dft: {'),
    reviewCenter.indexOf('    const conflictState')
  );

  expect(reviewCenter).toContain('主文图片审核提示词');
  expect(reviewCenter).toContain('支撑文献图片审核提示词');
  expect(reviewCenter).toContain('表格审核提示词');
  expect(reviewCenter).toContain('DFT 数据审核与入库提示词');
  expect(reviewCenter).not.toContain('value="dft_primary"');
  expect(dftReviewerScope).toContain('一份证据合格的 AI 意见即可通过受控入口直接确认、修正、拒绝或新增');
  expect(dftReviewerScope).toContain('不需要第二 AI、主 AI 或按 AI 身份计票');
  expect(reviewCenter).toContain('一次只能选择一个目标');
  expect(reviewCenter).toContain('return actionConfig.scopeNote + "\\n\\n" + rendered;');
  expect(reviewCenter).toContain('const template = profileTemplates[kind] || templates[kind] || compositeTemplates[kind];');
  expect(reviewCenter).not.toContain('|| templates.overall');
  expect(reviewCenter).not.toContain('<option value="figure">图表指令</option>');
  expect(reviewCenter).not.toContain('resolveVisualPromptKind()');
});

test('detail pages no longer expose formal prompt copy entrypoints', () => {
  const reviewJs = readFrontendFile('pages/literature_library/review.js');
  const dftWorkflow = readFrontendFile('pages/literature_library/dft-workflow.js');
  const reviewCards = readFrontendFile('pages/literature_library/review-card-renderers.js');
  const combined = [reviewJs, dftWorkflow, reviewCards].join('\n');

  expect(combined).not.toContain('复制总体解析指令');
  expect(combined).not.toContain('总体解析指令</summary>');
  expect(combined).not.toContain('生成下一轮 AI 审核任务');
  expect(combined).toContain('请回审核中心按单篇文献发起 AI 审核任务');
});

test('DFT audit center is not a daily primary prompt entrypoint', () => {
  const topnav = readFrontendFile('shared/topnav.js');
  const auditCenter = readFrontendFile('pages/dft_audit_center/index.html');

  expect(topnav).not.toContain('label: "DFT 核验"');
  expect(auditCenter).not.toContain('copyQueueHintBtn');
  expect(auditCenter).not.toContain('复制主 AI 处理提示');
  expect(auditCenter).toContain('日常 DFT 审核与入库提示词必须回审核中心选择一篇主文献后复制');
});

test('AI task center main log stays batch-oriented', () => {
  const detailActions = readFrontendFile('pages/literature_library/detail-actions.js');
  const jobsCenter = readFrontendFile('pages/literature_library/jobs-center.js');
  const libraryIndex = readFrontendFile('pages/literature_library/index.html');
  const collectScope = detailActions.slice(
    detailActions.indexOf('function collectTaskLogEntries'),
    detailActions.indexOf('function renderTaskLogPanel')
  );

  expect(libraryIndex).toContain('AI任务中心');
  expect(libraryIndex).toContain('刷新AI任务中心');
  expect(collectScope).toContain('import_analysis 批次导入');
  expect(collectScope).toContain('候选总数');
  expect(collectScope).toContain('candidates.length');
  expect(collectScope).not.toContain('object_review_audits');
  expect(collectScope).not.toContain('对象审核');
  expect(jobsCenter).toContain('["agent_activity", "AI任务记录"]');
  expect(jobsCenter).toContain('summary.problem_items');
  expect(jobsCenter).toContain('批次摘要');
});

test('content knowledge page exposes unified safe retrieval surface', () => {
  const topnav = readFrontendFile('shared/topnav.js');
  const page = [
    'pages/content_knowledge/index.html',
    'pages/content_knowledge/api.js',
    'pages/content_knowledge/page.js',
    'pages/content_knowledge/review-actions.js',
  ].map(readFrontendFile).join('\n');

  expect(topnav).toContain('id: "content-knowledge"');
  expect(topnav).toContain('label: "内容知识"');
  expect(page).toContain('/api/content-knowledge?');
  expect(page).toContain('/api/content-knowledge/sync?');
  expect(page).toContain('/validate');
  expect(page).toContain('/apply');
  expect(page).toContain('/finalize');
  expect(page).toContain('先同步索引后审核');
  expect(page).toContain('mechanism_evidence');
  expect(page).toContain('writing_material');
  expect(page).toContain('citation_policy');
  expect(page).toContain('risk_flags');
  expect(page).toContain('include_candidates');
  expect(page).toContain('include_blocked');
  expect(page).toContain('currentRunId');
  expect(page).toContain('run_id:runId');
  expect(page).toContain('scopeBanner');
  expect(page).toContain('当前审核范围：AI 批次');
  expect(page).toContain('external_analysis_run');
  expect(page).toContain('item_count');
  expect(page).toContain('finalizeReview');
  expect(page).toContain('完成审核');
  expect(page).toContain('仍有未解决项');
  expect(page).toContain('声明来源；未认证');
});
