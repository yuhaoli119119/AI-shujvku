const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { readPageSource } = require('./helpers/read-page-source');

const REPO_ROOT = path.resolve(__dirname, '..');

function readFrontendFile(relativePath) {
  return fs.readFileSync(path.join(REPO_ROOT, relativePath), 'utf8');
}

test('review center exposes local AI chart review copy instruction and status fields', () => {
  const reviewCenter = readPageSource('pages/review_center/index.html');

  expect(reviewCenter).toContain('复制本地 AI 全量图片复核指令');
  expect(reviewCenter).toContain('get_chart_review_task(paper_id)');
  expect(reviewCenter).toContain('resolve_chart_review_actions(paper_id, review_result)');
  expect(reviewCenter).toContain('finalize_chart_review(paper_id, review_result)');
  expect(reviewCenter).toContain('unresolved_actions');
  expect(reviewCenter).toContain('stage_status');
  expect(reviewCenter).toContain('completed_snapshot_fingerprint');
  expect(reviewCenter).toContain("used_tools:['get_codex_item','read_paper_page']");
  expect(reviewCenter).toContain('verification_note');
  expect(reviewCenter).toContain('每一张范围内图片');
  expect(reviewCenter).toContain('duplicate_or_conflicting_figure_action');
  expect(reviewCenter).toContain('missing_evidence_ids_for_modification');
  expect(reviewCenter).toContain('WEB_AI_FILL_THIS.json');
  expect(reviewCenter).toContain('OUTPUT_RULES.json');
  expect(reviewCenter).toContain('CREATE figure 必须有 source_paper_id、page、bbox_norm、evidence_checked=true 和真实 evidence_ids');
  expect(reviewCenter).toContain('run_id=" + encodeURIComponent(runId)');
});

test('content knowledge routes run-scoped figure field reminders to chart review', () => {
  const contentKnowledge = readFrontendFile('pages/content_knowledge/index.html');
  expect(contentKnowledge).toContain('转到图表审核');
  expect(contentKnowledge).toContain('category === "figure_table_evidence"');
  expect(contentKnowledge).toContain('不要生成内容审核包');
});

test('run-scoped chart review keeps and verifies its fixed manual scope', () => {
  const reviewCenter = readPageSource('pages/review_center/index.html');
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
  const reviewCenter = readPageSource('pages/review_center/index.html');
  expect(reviewCenter).toContain('function dedupeValidationIssues(issues)');
  expect(reviewCenter).toContain('code + "\\u0000" + message');
  expect(reviewCenter).toContain('action_ref');
  expect(reviewCenter).toContain('target_id');
});

test('review center defaults to an unconfirmed scope and exposes explicit scope choices', () => {
  const reviewCenter = readPageSource('pages/review_center/index.html');
  expect(reviewCenter).toContain('请先选择 AI 批次或明确选择整篇论文审核');
  expect(reviewCenter).toContain('图表审核范围（高级）');
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
  expect(reviewCenter).toContain('高级：处理待补充图表或查看审核历史');
  expect(reviewCenter).not.toContain(' | run_id=');
  expect(reviewCenter).not.toContain(' | paper_id：');
});

test('main-paper DFT entry restores the completed recommended chart run', () => {
  const detailLoader = readFrontendFile('pages/literature_library/detail-loader.js');
  const pageActions = readFrontendFile('pages/literature_library/page-actions.js');
  expect(detailLoader).toContain('chartStatus.primary_completed_run && chartStatus.primary_completed_run.chart_run_id');
  expect(pageActions).toContain('chartStatus.primary_completed_run && chartStatus.primary_completed_run.chart_run_id');
});

test('DFT export stays paper-scoped and completed chart runs are not shown as pending work', () => {
  const reviewCenter = readPageSource('pages/review_center/index.html');

  expect(reviewCenter).toContain('DFT 数据查漏不需要选择 AI 批次。');
  expect(reviewCenter).toContain('待补充图表（');
  expect(reviewCenter).toContain('已完成历史（');
  expect(reviewCenter).toContain('return !["completed", "not_required"].includes');
  expect(reviewCenter).toContain('/dft-review-bundle?include_figure_files=true&chart_scope=paper');
  expect(reviewCenter).toContain('4 导出 " + modeLabel + "包（仅已完成两级审核图片）');
  expect(reviewCenter).toContain('待完成两级审核：主文 ');
  expect(reviewCenter).toContain('再由本地 AI 逐图对照 PDF 核验');
  expect(reviewCenter).toContain('已完成两级审核的证据为主文 ');
  expect(reviewCenter).toContain('暂不可导出：图表阶段 ');
});

test('review center shares the live chart stage across a main and supplementary group only', () => {
  const reviewCenter = readPageSource('pages/review_center/index.html');

  expect(reviewCenter).toContain('function applyLiveChartStageToSupplementaryGroup(paperId, stage)');
  expect(reviewCenter).toContain('const mainPaperId = String(targetGroup && targetGroup.main_paper_id || targetRow.paper_id || "")');
  expect(reviewCenter).toContain('const rowMainPaperId = String(group && group.main_paper_id || row && row.paper_id || "")');
  expect(reviewCenter).toContain('row._live_chart_stage = String(stage || "unknown")');
  expect(reviewCenter).toContain('applyLiveChartStageToSupplementaryGroup(target.paper_id, gateStage)');
  expect(reviewCenter).toContain('const mainRow = state.rows.find(function (candidate)');
  expect(reviewCenter).toContain('figures: liveChartStage ? ["completed", "not_required"].includes(liveChartStage) : normalize(mainProgress, "figures")');
  expect(reviewCenter).toContain('dft: normalize(source, "dft")');
  expect(reviewCenter).toContain('content: normalize(source, "content")');
});

test('literature detail renders direct review state and loads full chart scope only on demand', () => {
  const reviewStatus = readFrontendFile('pages/literature_library/review-status.js');
  const renderDetail = readFrontendFile('pages/literature_library/render-detail.js');
  const dftWorkflow = readFrontendFile('pages/literature_library/dft-workflow.js');

  expect(reviewStatus).toContain('function chartReviewCoverage(detail)');
  expect(reviewStatus).toContain('chartStatus.excluded_duplicate_figures');
  expect(reviewStatus).toContain('excluded_figure_id');
  expect(reviewStatus).toContain('function chartReviewExcludedFigure(detail, item)');
  expect(reviewStatus).toContain('重复候选，已从审核范围排除');
  expect(reviewStatus).toContain('same_page_same_normalized_caption');
  expect(reviewStatus).toContain('item.review_status');
  expect(reviewStatus).toContain('item.object_review_audit_count');
  expect(reviewStatus).toContain('item.table_review_status');
  expect(reviewStatus).toContain('主文图片审核：已审核 ');
  expect(reviewStatus).toContain('待补充图表审核');
  expect(reviewStatus).toContain('复制图表审核提示');
  expect(reviewStatus).toContain('表格');
  expect(reviewStatus).toContain('chartReviewCompleted');
  expect(reviewStatus).toContain('已闭环');
  expect(renderDetail).toContain('figureChartReviewStatusHtml(detail, item, reviewCoverage)');
  expect(renderDetail).toContain('const excludedDuplicate = chartReviewExcludedFigure(detail, item)');
  expect(renderDetail).toContain('const reviewCardStatus = excludedDuplicate ? "excluded"');
  expect(renderDetail).toContain('重复候选，映射到 ');
  expect(renderDetail).toContain('正规图映射：');
  expect(renderDetail).toContain('高级：修正图片说明');
  expect(renderDetail).toContain('仅用于修正标题、摘要、分图说明或 OCR；不会完成图表审核。');
  expect(dftWorkflow).toContain('async function ensureSelectedChartReviewScopes()');
  expect(dftWorkflow).toContain('/chart-review-scopes');
  expect(dftWorkflow).toContain('async function copyFigureChartReviewPrompt(figureId)');
  expect(dftWorkflow).toContain('这些写入不改变图表审核状态。');
  expect(dftWorkflow).toContain('先执行 validate（只校验）；校验成功后才 resolve/apply；没有未解决项后才调用 finalize_chart_review。');
});

test('excluded duplicate figures are outside effective coverage and have their own card state', () => {
  const source = readFrontendFile('pages/literature_library/review-status.js');
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(`${source}\nglobalThis.__chartReviewTest = { chartReviewCoverage, chartReviewExcludedFigure, figureChartReviewStatusHtml };`, sandbox);

  const regularFigures = Array.from({ length: 20 }, (_, index) => ({
    id: `regular-${index + 1}`,
    figure_label: `Figure S${index + 1}`,
    review_status: 'reviewed',
  }));
  const duplicateS1 = {
    id: '96d660c1-6597-435b-bdbd-6e48b10fff3f',
    figure_label: 'fig_candidate_1',
  };
  const duplicateS5 = {
    id: '3498d14b-ec9c-451c-9950-efcd2fa9e3d5',
    figure_label: 'fig_candidate_5',
  };
  const detail = {
    figures: [...regularFigures, duplicateS1, duplicateS5],
    chart_review_status: {
      excluded_duplicate_figures: [
        {
          excluded_figure_id: duplicateS1.id,
          canonical_figure_id: 'ae47178f-723a-4555-ab4b-6ae1515655e9',
          canonical_figure_label: 'Figure S1',
          reason: 'same_page_same_normalized_caption',
        },
        {
          excluded_figure_id: duplicateS5.id,
          canonical_figure_id: 'a919edc3-00ba-4234-b0de-76b42d6a4456',
          canonical_figure_label: 'Figure S5',
          reason: 'same_page_same_normalized_caption',
        },
      ],
    },
  };

  const api = sandbox.__chartReviewTest;
  const coverage = api.chartReviewCoverage(detail);
  expect(coverage.mainFigureTotal).toBe(20);
  expect(coverage.reviewedMainFigureCount).toBe(20);
  expect(coverage.pendingMainFigureCount).toBe(0);
  expect(coverage.mainFigureIds.has(duplicateS1.id)).toBe(false);
  expect(coverage.reviewedMainFigureIds.has(duplicateS1.id)).toBe(false);
  expect(api.figureChartReviewStatusHtml(detail, duplicateS1, coverage)).toContain('重复候选，已从审核范围排除');
  expect(api.figureChartReviewStatusHtml(detail, regularFigures[0], coverage)).toContain('图表审核已完成');
  expect(api.chartReviewExcludedFigure(detail, duplicateS5)).toMatchObject({
    canonical_figure_label: 'Figure S5',
    canonical_figure_id: 'a919edc3-00ba-4234-b0de-76b42d6a4456',
  });
});
