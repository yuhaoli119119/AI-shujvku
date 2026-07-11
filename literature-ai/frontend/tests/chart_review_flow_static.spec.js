const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');

function readFrontendFile(relativePath) {
  return fs.readFileSync(path.join(REPO_ROOT, relativePath), 'utf8');
}

test('review center exposes local AI chart review copy instruction and status fields', () => {
  const reviewCenter = readFrontendFile('pages/review_center/index.html');

  expect(reviewCenter).toContain('复制本地 AI 图表复核指令');
  expect(reviewCenter).toContain('get_chart_review_task(paper_id)');
  expect(reviewCenter).toContain('resolve_chart_review_actions(paper_id, review_result)');
  expect(reviewCenter).toContain('finalize_chart_review(paper_id, review_result)');
  expect(reviewCenter).toContain('unresolved_actions');
  expect(reviewCenter).toContain('stage_status');
  expect(reviewCenter).toContain('completed_snapshot_fingerprint');
  expect(reviewCenter).toContain("used_tools:['get_codex_item','read_paper_page']");
  expect(reviewCenter).toContain('verification_note');
  expect(reviewCenter).toContain('duplicate_or_conflicting_figure_action');
  expect(reviewCenter).toContain('missing_evidence_ids_for_modification');
  expect(reviewCenter).toContain('run_id=" + encodeURIComponent(runId)');
});

test('content knowledge routes run-scoped figure field reminders to chart review', () => {
  const contentKnowledge = readFrontendFile('pages/content_knowledge/index.html');
  expect(contentKnowledge).toContain('转到图表审核');
  expect(contentKnowledge).toContain('category === "figure_table_evidence"');
  expect(contentKnowledge).toContain('不要生成内容审核包');
});

test('run-scoped chart review keeps and verifies its fixed manual scope', () => {
  const reviewCenter = readFrontendFile('pages/review_center/index.html');
  expect(reviewCenter).toContain('REVIEW_CENTER_MANUAL_CONTEXT_SESSION_KEY');
  expect(reviewCenter).toContain('restoreManualReviewContext()');
  expect(reviewCenter).toContain('updateManualReviewUrl()');
  expect(reviewCenter).toContain('当前 AI 批次');
  expect(reviewCenter).toContain('当前图表审核范围');
  expect(reviewCenter).toContain('X-LitAI-Review-Scope');
  expect(reviewCenter).toContain('X-LitAI-Review-Run-Id');
  expect(reviewCenter).toContain('后端返回的图表审核范围与当前固定范围不一致，已阻止下载');
  expect(reviewCenter).toContain('evidenceScopeMismatchIssues(parsed)');
  expect(reviewCenter).toContain('evidenceScopeMismatchIssues(data)');
  expect(reviewCenter).toContain('完成图表审核');
  expect(reviewCenter).toContain('/chart-review-result/finalize');
});

test('chart review validation errors are merged by code and message', () => {
  const reviewCenter = readFrontendFile('pages/review_center/index.html');
  expect(reviewCenter).toContain('function dedupeValidationIssues(issues)');
  expect(reviewCenter).toContain('code + "\\u0000" + message');
  expect(reviewCenter).toContain('action_ref');
  expect(reviewCenter).toContain('target_id');
});

test('review center defaults to an unconfirmed scope and exposes explicit scope choices', () => {
  const reviewCenter = readFrontendFile('pages/review_center/index.html');
  expect(reviewCenter).toContain('请先选择 AI 批次或明确选择整篇论文审核');
  expect(reviewCenter).toContain('选择审核范围');
  expect(reviewCenter).toContain('整篇论文审核');
  expect(reviewCenter).toContain('function selectRunScope(runId)');
  expect(reviewCenter).toContain('function selectWholePaperScope()');
  expect(reviewCenter).toContain('scopeType: "external_analysis_run"');
  expect(reviewCenter).toContain('scopeType: "paper"');
  expect(reviewCenter).toContain('if (!urlPaperId && stored && stored.paperId && stored.runId && stored.mode === "evidence")');
  expect(reviewCenter).toContain('if (urlPaperId && urlScope === "paper")');
  expect(reviewCenter).toContain('loadReviewScopeCandidates()');
  expect(reviewCenter).toContain('/api/papers/" + encodeURIComponent(paperId) + "/chart-review-scopes');
  expect(reviewCenter).toContain('scopeTargetCounts(run)');
  expect(reviewCenter).toContain('clearMismatchedManualReviewContext(target)');
  expect(reviewCenter).toContain('当前范围属于 ');
  expect(reviewCenter).toContain('重新选择批次');
  expect(reviewCenter).toContain('requireSelectedMainEvidenceScope(target)');
  expect(reviewCenter).toContain('历史重复执行（');
  expect(reviewCenter).toContain('当前主文推荐批次');
  expect(reviewCenter).not.toContain(' | run_id=');
  expect(reviewCenter).not.toContain(' | paper_id：');
});

test('main-paper DFT entry restores the completed recommended chart run', () => {
  const detailLoader = readFrontendFile('pages/literature_library/detail-loader.js');
  const pageActions = readFrontendFile('pages/literature_library/page-actions.js');
  expect(detailLoader).toContain('chartStatus.primary_completed_run && chartStatus.primary_completed_run.chart_run_id');
  expect(pageActions).toContain('chartStatus.primary_completed_run && chartStatus.primary_completed_run.chart_run_id');
});

test('literature detail treats chart review as combined figure and table completion', () => {
  const reviewStatus = readFrontendFile('pages/literature_library/review-status.js');

  expect(reviewStatus).toContain('chart_review_status');
  expect(reviewStatus).toContain('unresolved_count');
  expect(reviewStatus).toContain('表格');
  expect(reviewStatus).toContain('图片和表格都完成后才可标记图表完成');
  expect(reviewStatus).toContain('chartReviewCompleted');
  expect(reviewStatus).toContain('已闭环');
});
