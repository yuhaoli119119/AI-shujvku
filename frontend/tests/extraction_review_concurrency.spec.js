const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://localhost:8000';
const PAPER_ID = 'paper-1';
const TARGET_ID = 'target-1';

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function review(version = 7) {
  return {
    id: 'review-1',
    paper_id: PAPER_ID,
    target_type: 'dft_results',
    target_id: TARGET_ID,
    field_name: 'value',
    reviewed_value: -1.2,
    reviewer_status: 'corrected',
    target_resolution_status: 'active',
    write_version: version,
    verified: false,
    created_at: '2026-06-19T00:00:00',
    updated_at: '2026-06-19T00:00:00',
  };
}

function extractionPayload(version = 7) {
  const currentReview = review(version);
  return {
    paper_id: PAPER_ID,
    schemas: {},
    results: {
      DFTResult: [{
        target_type: 'dft_results',
        target_id: TARGET_ID,
        value: {
          value: -1.2,
          unit: 'eV',
          evidence_text: 'The adsorption energy is -1.2 eV.',
          confidence: 0.9,
          review: currentReview,
          verified: false,
        },
      }],
    },
    field_reviews: [currentReview],
    validation_warnings: [],
    validation_status: 'validated',
  };
}

async function installWorkbenchRoutes(page, onWrite) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === '/api/papers' && method === 'GET') {
      return json(route, [{ id: PAPER_ID, title: 'Concurrency paper', pdf_path: 'paper.pdf' }]);
    }
    if (path === `/api/papers/${PAPER_ID}` && method === 'GET') {
      return json(route, { id: PAPER_ID, title: 'Concurrency paper', pdf_path: 'paper.pdf' });
    }
    if (path === `/api/extraction/results/${PAPER_ID}` && method === 'GET') {
      return json(route, extractionPayload());
    }
    if (path === `/api/extraction/results/${PAPER_ID}/validate`) {
      return json(route, extractionPayload());
    }
    if (path === `/api/extraction/results/${PAPER_ID}/reviews/audit`) {
      return json(route, { paper_id: PAPER_ID, total_reviews: 1, active: 1, remapped: 0, stale: 0, ambiguous: 0, unresolved: 0, items: [review()] });
    }
    if (path === `/api/extraction/results/${PAPER_ID}/evidence-locators`) {
      return json(route, []);
    }
    if (path.endsWith('/reviews/save') || path.endsWith('/reviews/mark-verified')) {
      return onWrite(route, path, JSON.parse(route.request().postData() || '{}'));
    }
    return json(route, {});
  });
}

async function openWorkbench(page) {
  await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${PAPER_ID}`);
  await page.selectOption('#schemaSelect', 'DFTResult');
  await expect(page.locator('input[data-field="value"][data-part="value"]')).toBeVisible();
}

test('all extraction review write paths send the current per-field write version', async ({ page }) => {
  const writes = [];
  await installWorkbenchRoutes(page, (route, path, payload) => {
    writes.push({ path, payload });
    return json(route, [review(8)]);
  });
  await openWorkbench(page);

  await page.evaluate(() => saveSingleField('dft_results', 'target-1', 'value'));
  await page.evaluate(() => verifySingleField('dft_results', 'target-1', 'value'));
  await page.evaluate(() => verifyRecord('dft_results', 'target-1'));
  await page.evaluate(() => saveDraft());
  await page.evaluate(() => markVerified());

  expect(writes).toHaveLength(5);
  expect(writes[0].payload.reviews[0].expected_write_version).toBe(7);
  expect(writes[1].payload.expected_write_versions).toEqual({ value: 7 });
  expect(writes[2].payload.expected_write_versions).toEqual({ value: 7 });
  expect(writes[3].payload.reviews[0].expected_write_version).toBe(7);
  expect(writes[4].payload.expected_write_versions).toEqual({ value: 7 });
});

test('HTTP 409 refreshes review state and reports conflict without retrying', async ({ page }) => {
  let resultReads = 0;
  let writeCalls = 0;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/papers') return json(route, [{ id: PAPER_ID, title: 'Concurrency paper' }]);
    if (path === `/api/papers/${PAPER_ID}`) return json(route, { id: PAPER_ID, title: 'Concurrency paper' });
    if (path === `/api/extraction/results/${PAPER_ID}` && route.request().method() === 'GET') {
      resultReads += 1;
      return json(route, extractionPayload(resultReads === 1 ? 7 : 8));
    }
    if (path === `/api/extraction/results/${PAPER_ID}/reviews/save`) {
      writeCalls += 1;
      return json(route, { detail: 'write_conflict:extraction_review_version_stale' }, 409);
    }
    if (path === `/api/extraction/results/${PAPER_ID}/validate`) return json(route, extractionPayload(8));
    if (path.endsWith('/reviews/audit')) return json(route, { items: [review(8)] });
    if (path.endsWith('/evidence-locators')) return json(route, []);
    return json(route, {});
  });
  await openWorkbench(page);
  const readsBeforeWrite = resultReads;

  await page.evaluate(() => saveSingleField('dft_results', 'target-1', 'value'));

  expect(writeCalls).toBe(1);
  expect(resultReads).toBeGreaterThan(readsBeforeWrite);
  await expect(page.locator('#toast')).toContainText('并发冲突');
  await expect(page.locator('#toast')).toContainText('write_conflict:extraction_review_version_stale');
});
