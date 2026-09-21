// Static contract for the authoritative chart stage in the review-center list.
//
// The list must obtain the whole-paper chart stage on first paint.  The
// compatibility field ``manual_review_progress`` is not allowed to be the only
// source: it is written by one legacy path only, so a paper completed through
// the V2 pipeline keeps it empty and the list would still show "not finished".
//
// Equally important is the opposite direction: a stale snapshot, an unresolved
// action or an incomplete scope must never render as "figures finished", even
// when the task still carries a ``completed`` stage token.
const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');
const reviewCenterPage = fs.readFileSync(path.join(REPO_ROOT, 'pages/review_center/page.js'), 'utf8');

test('review center list reads the authoritative chart stage on first paint', () => {
  expect(reviewCenterPage).toContain('/api/workbench/review-center/chart-stages');
  expect(reviewCenterPage).toContain('const authoritativeChartStages = {}');
  expect(reviewCenterPage).toContain('function visibleRowsMissingChartStage(');
  expect(reviewCenterPage).toContain('function refreshAuthoritativeChartStages(');
  expect(reviewCenterPage).toContain('function applyAuthoritativeChartStages(');
  // renderRows must trigger the authoritative read for whatever it renders,
  // so paging, filtering and sorting can never leave a stale chip behind.
  const renderRowsStart = reviewCenterPage.indexOf('function renderRows(');
  expect(renderRowsStart).toBeGreaterThan(-1);
  expect(reviewCenterPage.slice(renderRowsStart)).toContain('refreshAuthoritativeChartStages(rows)');
});

test('only the whole-paper authoritative payload may drive the chart chip', () => {
  expect(reviewCenterPage).toContain('if (!entry || typeof entry !== "object" || !entry.authoritative) return;');
  expect(reviewCenterPage).toContain('applyLiveChartStageToSupplementaryGroup(paperId, authoritativeDisplayStage(entry))');
  // a user-visible reload re-reads the stage; the background refresh keeps it
  expect(reviewCenterPage).toContain('if (!silent) clearAuthoritativeChartStages();');
  // selecting a paper still refreshes, but the preview gate is a secondary opinion
  expect(reviewCenterPage).toContain('void queueAuthoritativeChartStages([target.paper_id]);');
  expect(reviewCenterPage).toContain('const CHART_STAGE_BATCH_LIMIT = 50;');
});

test('a completed stage token alone cannot render as finished', () => {
  // the chip stage is derived from the conjunctive verdict, not from the token
  expect(reviewCenterPage).toContain('function authoritativeDisplayStage(entry)');
  expect(reviewCenterPage).toContain('if (entry && entry.figures_completed) return stage;');
  expect(reviewCenterPage).toContain('if (entry && entry.snapshot_fingerprint_matches === false) return "stale";');
  expect(reviewCenterPage).toContain('if (Number(entry && entry.unresolved_count || 0) > 0) return "completed_with_issues";');
  expect(reviewCenterPage).toContain('return "incomplete";');
  // the compatibility field is a fallback only, and the live stage always wins
  expect(reviewCenterPage).toContain('figures: liveChartStage ? ["completed", "not_required"].includes(liveChartStage) : normalize(mainProgress, "figures")');
});
