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
});
