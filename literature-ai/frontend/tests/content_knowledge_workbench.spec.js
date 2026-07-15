const { test, expect } = require('@playwright/test');
const BASE_URL = 'http://127.0.0.1:4174';
const PAPER_ID = '11111111-1111-4111-8111-111111111111';

const firstItem = {
  item_id: 'claim:1', reviewable: true, paper_id: PAPER_ID, paper_code: 'B0078', paper_title: 'Evidence <b>claim</b>', category: 'mechanism_evidence', category_label: '机理证据卡', content: '<img src=x onerror="window.xssHit=1">Catalyst improves conversion', evidence_text: 'Page evidence', page_start: 4, section_title: 'Results', review_status: 'needs_review', citation_policy: 'needs_review', risk_flags: ['missing_locator'], source_identity_verified: false, match_reason: '关键词命中', updated_at: '2026-07-16T00:00:00Z', metadata: { internal: '<script>bad()</script>' },
};
const secondItem = { ...firstItem, item_id: 'card:2', paper_code: 'B0079', paper_title: 'Second paper', content: 'Second content', risk_flags: [] };
const thirdItem = { ...firstItem, item_id: 'card:3', paper_code: 'B0080', paper_title: 'Third paper', content: 'Third content' };

async function mockKnowledge(page, { conflict = false, posts = null } = {}) {
  await page.route('**/api/content-knowledge**', async (route) => {
    const request = route.request(); const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname.endsWith('/review')) {
      if (posts) posts.push(JSON.parse(request.postData() || '{}'));
      if (conflict) return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'stale' }) });
      const body = JSON.parse(request.postData() || '{}');
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...firstItem, citation_policy: body.decision === 'approve_citable' ? 'citable' : body.decision, review_status: body.decision, updated_at: '2026-07-16T00:01:00Z' }) });
    }
    if (url.pathname.endsWith('/claim%3A1') || url.pathname.endsWith('/claim:1')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(firstItem) });
    if (url.pathname.endsWith('/card%3A2') || url.pathname.endsWith('/card:2')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(secondItem) });
    if (url.pathname.endsWith('/card%3A3') || url.pathname.endsWith('/card:3')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(thirdItem) });
    const offset = Number(url.searchParams.get('offset') || 0); const query = url.searchParams.get('query');
    const all = query === 'second' ? [secondItem] : [firstItem, secondItem, thirdItem]; const items = all.slice(offset, offset + 2);
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schema_version: 'content_knowledge.v1', items, total: all.length, offset, limit: 2, has_more: offset + items.length < all.length }) });
  });
}

test('content knowledge workbench searches, restores URL state, paginates, and keeps dynamic text safe', async ({ page }) => {
  await mockKnowledge(page);
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html?query=second&selected=card%3A2`);
  await expect(page.getByRole('heading', { name: 'Second paper' })).toBeVisible();
  await expect(page).toHaveURL(/query=second/);
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await expect(page.getByText('B0078')).toBeVisible();
  await expect(page.locator('img')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('bad()');
  await page.locator('#queryInput').fill('second');
  await page.waitForTimeout(350);
  await expect(page.getByText('Second content')).toBeVisible();
  await expect(page).toHaveURL(/query=second/);
  await page.locator('#queryInput').fill('');
  await page.waitForTimeout(350);
  await page.getByRole('button', { name: '加载更多' }).click();
  await expect(page.getByText('Third paper')).toBeVisible();
  await expect(page.getByText('显示 1–3，共 3 条')).toBeVisible();
});

test('content detail has collapsed technical JSON and all four evidence decisions use the fixed contract', async ({ page }) => {
  const posted = [];
  await mockKnowledge(page, { posts: posted });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await expect(page.locator('blockquote')).toHaveText('Page evidence');
  await expect(page.locator('.technical-details')).not.toHaveAttribute('open', '');
  await expect(page.getByRole('link', { name: '去审核中心修正源内容' })).toHaveAttribute('href', new RegExp(`paper_id=${PAPER_ID}`));
  for (const decision of ['approve_citable', 'writing_only', 'needs_human', 'reject']) {
    await page.locator(`input[value="${decision}"]`).check();
    if (decision === 'needs_human' || decision === 'reject') {
      await page.locator('#reviewReason').fill('evidence is insufficient');
    }
    await page.getByRole('button', { name: '提交审核决定' }).click();
    await expect(page.getByText('审核决定已保存，并已局部刷新该内容。')).toBeVisible();
  }
  await expect.poll(() => posted.length).toBe(4);
  const reviewBodies = posted;
  expect(reviewBodies.map((body) => body.decision)).toEqual(['approve_citable', 'writing_only', 'needs_human', 'reject']);
  expect(reviewBodies.every((body) => body.reviewer === 'human-ui' && 'expected_updated_at' in body)).toBeTruthy();
});

test('reason is required for reject or needs human and a 409 prompts reload', async ({ page }) => {
  await mockKnowledge(page, { conflict: true });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await page.locator('input[value="reject"]').check();
  await page.getByRole('button', { name: '提交审核决定' }).click();
  await expect(page.getByText('拒绝或转需人工时必须填写原因。')).toBeVisible();
  await page.locator('#reviewReason').fill('conflicting source');
  await page.getByRole('button', { name: '提交审核决定' }).click();
  await expect(page.getByText('审核状态已被更新，请重载后再决定。')).toBeVisible();
});

test('content workbench stacks its three panels on a narrow screen', async ({ page }) => {
  await mockKnowledge(page);
  await page.setViewportSize({ width: 600, height: 900 });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  const columns = await page.locator('.workbench').evaluate((el) => getComputedStyle(el).gridTemplateColumns);
  expect(columns.split(' ').length).toBe(1);
  await expect(page.locator('.filters')).toBeVisible();
});

test('legacy projection requires index sync and never sends its source-shaped ID to review', async ({ page }) => {
  const legacy = { ...firstItem, item_id: 'mechanism_claim:88', reviewable: false, requires_sync: true };
  let detailRequested = false; let reviewRequested = false;
  await page.route('**/api/content-knowledge**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.includes('/items/')) detailRequested = true;
    if (url.pathname.endsWith('/review')) reviewRequested = true;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [legacy], total: 1, offset: 0, limit: 25, has_more: false }) });
  });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await expect(page.getByText('先同步索引后审核')).toBeVisible();
  await expect(page.getByRole('radio', { name: '批准可引用' })).toBeDisabled();
  expect(detailRequested).toBeFalsy();
  expect(reviewRequested).toBeFalsy();
});

test('advanced actions keep run scope and call the real scoped workflow endpoints', async ({ page }) => {
  const calls = [];
  page.on('dialog', (dialog) => dialog.accept());
  await page.route('**/api/content-knowledge**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    calls.push({ method: request.method(), path: url.pathname, search: url.search, body });

    if (request.method() === 'GET' && url.pathname === '/api/content-knowledge') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [firstItem], total: 1, offset: 0, limit: 25, has_more: false }),
      });
    }
    if (url.pathname.endsWith('/claim%3A1') || url.pathname.endsWith('/claim:1')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ item: firstItem }) });
    }
    if (url.pathname === '/api/content-knowledge/sync') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ synced: true }) });
    }
    if (url.pathname === '/api/content-knowledge/review-bundles') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          bundle_id: 'bundle-1',
          manifest: { scope_type: 'external_analysis_run', item_count: 1, instructions: 'Review B0078' },
          return_template: { schema_version: 'content_evidence_review_result_v1' },
        }),
      });
    }
    if (url.pathname.endsWith('/validate')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ valid: true }) });
    }
    if (url.pathname.endsWith('/apply')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ applied: 1, needs_human: 0 }) });
    }
    if (url.pathname.endsWith('/finalize')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ finalized: true }) });
    }
    if (url.pathname === '/api/content-knowledge/writing-plan') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ query: body.query, citations: [] }) });
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });

  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html?paper_id=B0078&run_id=run-1`);
  await expect(page.getByText('当前审核范围：AI 批次 run-1')).toBeVisible();
  await page.locator('#categorySelect').selectOption('mechanism_evidence');
  await expect(page).toHaveURL(/run_id=run-1/);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await page.getByText('批量与高级操作').click();

  await page.getByRole('button', { name: '同步内容索引' }).click();
  await expect.poll(() => calls.some((call) => call.path.endsWith('/sync'))).toBeTruthy();
  const syncCall = calls.find((call) => call.path.endsWith('/sync'));
  expect(syncCall.search).toContain(`paper_id=${PAPER_ID}`);
  expect(syncCall.body).toBeNull();

  await page.getByRole('button', { name: '生成 AI 审核包' }).click();
  await expect(page.getByText(/审核包已生成：bundle-1/)).toBeVisible();
  const bundleCall = calls.find((call) => call.path.endsWith('/review-bundles'));
  expect(bundleCall.body).toEqual({ paper_id: PAPER_ID, run_id: 'run-1' });

  await page.locator('#bundleResultInput').fill('{}');
  await page.getByRole('button', { name: '校验回传' }).click();
  await expect(page.getByText(/回传校验通过/)).toBeVisible();
  await page.getByRole('button', { name: '应用审核回传' }).click();
  await expect(page.getByText(/已应用 1 项/)).toBeVisible();
  await page.getByRole('button', { name: '完成审核' }).click();
  await expect(page.getByText('审核已完成。')).toBeVisible();

  await page.locator('#writingPlanQuery').fill('Li2S conversion');
  await page.getByRole('button', { name: '生成写作证据计划' }).click();
  await expect(page.locator('#writingPlanResult')).toContainText('Li2S conversion');
  const writingCall = calls.find((call) => call.path.endsWith('/writing-plan'));
  expect(writingCall.body).toEqual({ query: 'Li2S conversion', paper_ids: [PAPER_ID] });
  expect(calls.some((call) => call.path.endsWith('/validate'))).toBeTruthy();
  expect(calls.some((call) => call.path.endsWith('/apply'))).toBeTruthy();
  expect(calls.some((call) => call.path.endsWith('/finalize'))).toBeTruthy();
});

test('a paper-code deep link resolves the UUID before creating a review bundle', async ({ page }) => {
  let bundleBody = null;
  await mockKnowledge(page);
  await page.route('**/api/content-knowledge/review-bundles', async (route) => {
    bundleBody = JSON.parse(route.request().postData() || '{}');
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ bundle_id: 'bundle-paper-code', manifest: { item_count: 1 } }),
    });
  });

  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html?paper_id=B0078`);
  await page.getByText('批量与高级操作').click();
  await page.getByRole('button', { name: '生成 AI 审核包' }).click();

  await expect(page.getByText(/审核包已生成：bundle-paper-code/)).toBeVisible();
  expect(bundleBody).toEqual({ paper_id: PAPER_ID });
});

test('structured evidence shows its quote and locator instead of a JSON wall', async ({ page }) => {
  const structuredItem = {
    ...firstItem,
    item_id: '22222222-2222-4222-8222-222222222222',
    evidence_text: JSON.stringify({
      raw_payload: {
        evidence_location: {
          quoted_text: 'The defect formation energy is 3.711 eV.',
          page: 3,
          section: 'Geometry optimization',
        },
      },
    }),
    page_start: null,
    section_title: null,
  };
  await page.route('**/api/content-knowledge**', async (route) => {
    const url = new URL(route.request().url());
    const body = url.pathname.includes('/items/')
      ? { item: structuredItem }
      : { items: [structuredItem], total: 1, offset: 0, limit: 25, has_more: false };
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });

  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();

  await expect(page.locator('blockquote')).toHaveText('The defect formation energy is 3.711 eV.');
  await expect(page.locator('.detail-content')).toContainText('Geometry optimization · 第 3 页');
  await expect(page.locator('blockquote')).not.toContainText('raw_payload');
});
