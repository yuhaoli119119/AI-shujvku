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

test('v2 bundle flow selects a module, validates proposal, and shows readonly local plan', async ({ page }) => {
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
    if (url.pathname === '/api/content-knowledge/review-bundles/v2') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          bundle_id: 'bundle-1',
          status: 'created', bundle_fingerprint: 'fp-1', download_url: '/api/content-knowledge/review-bundles/bundle-1/download', proposal_only: true, writes_final_truth: false, source_identity_verified: true,
          manifest: { targets: [{ id: 'a' }, { id: 'b' }], allowed_pages: [4, 7], instructions: 'Review B0078 safely' },
          return_template: { schema_version: 'content_evidence_review_result_v1' },
        }),
      });
    }
    if (url.pathname.endsWith('/web-proposal/validate')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ valid: true }) });
    }
    if (url.pathname.endsWith('/local-verification-plan')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ web_reviewed_target_count: 1, local_required_target_count: 1, local_skipped_target_count: 0, unique_page_count: 2, page_batches: [{ page: 4, checks: ['page_text'], plan_item_ids: ['a'] }, { page: '<img src=x onerror=alert(1)>', target_count: 2, page_asset_ref: 'asset-7' }], metrics: { logical_page_read_count: 3, unresolved_page_target_count: 1 }, local_ai_instruction: 'Do not read the whole bundle.' }) });
    }
    if (url.pathname.endsWith('/local-verification-status')) {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        bundle_id: 'bundle-1', status: 'finalized',
        object_counts: { required: 2, applied: 2, pending: 0, stale: 0, failed: 0, awaiting_human: 0 },
        formal_eligibility_before: { writing: 1, citation: 0, rag: 1 }, formal_eligibility_after: { writing: 2, citation: 1, rag: 2 }, formal_eligibility_delta: { writing: 1, citation: 1, rag: 1 },
        metrics: { logical_page_read_count: 4, physical_page_read_attempt_count: 3, page_read_retry_count: 1, page_cache_hit_count: 1 }, results: [],
      }) });
    }
    if (url.pathname.endsWith('/download')) {
      return route.fulfill({ contentType: 'application/zip', body: 'PK\u0003\u0004' });
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

  await page.locator('#bundleModule').selectOption('writing_cards');
  await page.getByRole('button', { name: '生成 v2 审核包' }).click();
  await expect(page.getByText(/审核包已生成：bundle-1/)).toBeVisible();
  await expect(page.getByText(/对象 2；唯一证据页 2/)).toBeVisible();
  const bundleCall = calls.find((call) => call.path.endsWith('/review-bundles/v2'));
  expect(bundleCall.body).toEqual({ paper_id: PAPER_ID, module: 'writing_cards' });

  const proposalFile = { name: 'proposal.json', mimeType: 'application/json', buffer: Buffer.from('{"targets":[]}') };
  await page.locator('#bundleFile').setInputFiles(proposalFile);
  await expect(page.getByText('已载入网页 AI JSON：proposal.json')).toBeVisible();
  await page.locator('#bundleResultInput').fill('{not-json');
  await page.getByRole('button', { name: '校验网页 AI 建议' }).click();
  await expect(page.getByText('JSON 解析失败：请上传严格有效的 JSON。')).toBeVisible();
  await page.locator('#bundleResultInput').fill('{}');
  await page.getByRole('button', { name: '校验网页 AI 建议' }).click();
  await expect(page.getByText(/网页 AI 回传校验完成：通过/)).toBeVisible();
  await expect(page.getByText(/本地核验计划：网页已核验 1/)).toBeVisible();
  await expect(page.getByText('总状态：已完成（finalized）')).toBeVisible();
  await expect(page.getByText(/对象计数：必需 2；已应用 2；待处理 0/)).toBeVisible();
  await expect(page.getByText(/可写作 1 → 2（1）；可引用 0 → 1（1）；RAG 1 → 2（1）/)).toBeVisible();
  await expect(page.getByText(/逻辑读取 4；物理读取 3；重试 1；缓存命中 1/)).toBeVisible();
  await expect(page.getByText(/逻辑页读取 3；未解决页目标 1/)).toBeVisible();
  await expect(page.getByText('第 4 页批次：1 个对象')).toBeVisible();
  await expect(page.getByText(/第 <img src=x onerror=alert\(1\)> 页批次/)).toBeVisible();
  await expect(page.getByRole('button', { name: '复制精简本地 AI 核验指令' })).toBeVisible();
  await page.getByRole('button', { name: '复制精简本地 AI 核验指令' }).click();
  await expect(page.getByText(/精简本地 AI 核验指令已复制|无法使用剪贴板/)).toBeVisible();
  const lastModified = await page.locator('#bundleFile').evaluate((input) => input.files[0].lastModified);
  await page.locator('#bundleDropZone').evaluate((element, modified) => {
    const file = new File(['{"targets":[]}'], 'proposal.json', { type: 'application/json', lastModified: modified });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    element.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer }));
  }, lastModified);
  await expect(page.getByText('该 JSON 已上传过，请选择新的文件。')).toBeVisible();
  await page.locator('#bundleFile').setInputFiles({ name: 'too-large.json', mimeType: 'application/json', buffer: Buffer.alloc(5 * 1024 * 1024 + 1) });
  await expect(page.getByText('网页 AI JSON 超过 5 MB 大小限制。')).toBeVisible();
  await page.locator('#bundleFile').setInputFiles({ name: 'new-proposal.json', mimeType: 'application/json', buffer: Buffer.from('{}') });
  await expect(page.locator('#localPlan')).toBeHidden();
  await page.getByRole('button', { name: '下载审核包 ZIP' }).click();
  await expect(page.getByRole('button', { name: '复制网页 AI 指令' })).toBeVisible();
  await page.getByRole('button', { name: '复制网页 AI 指令' }).click();
  await expect(page.getByText(/网页 AI 指令已复制|网页 AI 指令已显示/)).toBeVisible();

  await page.locator('#writingPlanQuery').fill('Li2S conversion');
  await page.getByRole('button', { name: '生成写作证据计划' }).click();
  await expect(page.locator('#writingPlanResult')).toContainText('Li2S conversion');
  const writingCall = calls.find((call) => call.path.endsWith('/writing-plan'));
  expect(writingCall.body).toEqual({ query: 'Li2S conversion', paper_ids: [PAPER_ID] });
  expect(calls.some((call) => call.path.endsWith('/web-proposal/validate'))).toBeTruthy();
  expect(calls.some((call) => call.path.endsWith('/local-verification-plan'))).toBeTruthy();
  expect(calls.some((call) => call.path.endsWith('/apply'))).toBeFalsy();
  expect(calls.some((call) => call.path.endsWith('/finalize'))).toBeFalsy();
});

test('local verification status refresh maps lifecycle states and remains read-only', async ({ page }) => {
  const calls = [];
  let statusCall = 0;
  const statuses = ['partial', 'stale', 'awaiting_human', 'finalized'];
  await page.route('**/api/content-knowledge**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    calls.push({ method: request.method(), path: url.pathname });
    if (url.pathname === '/api/content-knowledge') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [firstItem], total: 1, offset: 0, limit: 25, has_more: false }) });
    if (url.pathname.includes('/items/')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ item: firstItem }) });
    if (url.pathname.endsWith('/v2')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ bundle_id: 'bundle-status', status: 'created', manifest: { targets: [], allowed_pages: [] } }) });
    if (url.pathname.endsWith('/web-proposal/validate')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ valid: true }) });
    if (url.pathname.endsWith('/local-verification-plan')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ summary: {}, page_batches: [] }) });
    if (url.pathname.endsWith('/local-verification-status')) {
      const status = statuses[Math.min(statusCall++, statuses.length - 1)];
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ bundle_id: 'bundle-status', status, object_counts: { required: 4, applied: status === 'finalized' ? 4 : 2, pending: status === 'partial' ? 2 : 0, stale: status === 'stale' ? 1 : 0, failed: 0, awaiting_human: status === 'awaiting_human' ? 1 : 0 }, formal_eligibility_before: { writing: 0, citation: 0, rag: 0 }, formal_eligibility_after: { writing: 1, citation: 1, rag: 1 }, formal_eligibility_delta: { writing: 1, citation: 1, rag: 1 }, metrics: {}, results: status === 'stale' ? [{ plan_item_id: 'plan-1', status: 'stale', target_type: 'section', field_name: 'text', stale_reasons: ['source_changed'] }] : [] }) });
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await page.getByText('批量与高级操作').click();
  await page.getByRole('button', { name: '生成 v2 审核包' }).click();
  await page.locator('#bundleResultInput').fill('{}');
  await page.getByRole('button', { name: '校验网页 AI 建议' }).click();
  await expect(page.getByText('总状态：部分完成（partial）')).toBeVisible();
  await page.getByRole('button', { name: '刷新本地核验状态' }).click();
  await expect(page.getByText('总状态：已失效（stale）')).toBeVisible();
  await expect(page.getByText(/目标类型 section；字段 text；状态 已失效；原因 source_changed/)).toBeVisible();
  await page.getByRole('button', { name: '刷新本地核验状态' }).click();
  await expect(page.getByText('总状态：待人工（awaiting_human）')).toBeVisible();
  await page.getByRole('button', { name: '刷新本地核验状态' }).click();
  await expect(page.getByText('总状态：已完成（finalized）')).toBeVisible();
  expect(calls.filter((call) => call.path.endsWith('/local-verification-status')).length).toBe(4);
  expect(calls.some((call) => call.path.endsWith('/local-verification/apply'))).toBeFalsy();
  expect(calls.some((call) => call.path.endsWith('/finalize'))).toBeFalsy();
  await expect(page.getByRole('button', { name: /应用|批准|写入正式状态/ })).toHaveCount(0);
});

test('local verification status failure does not break proposal flow', async ({ page }) => {
  await page.route('**/api/content-knowledge**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/content-knowledge') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [firstItem], total: 1, offset: 0, limit: 25, has_more: false }) });
    if (url.pathname.includes('/items/')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ item: firstItem }) });
    if (url.pathname.endsWith('/v2')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ bundle_id: 'bundle-status-error', manifest: { targets: [], allowed_pages: [] } }) });
    if (url.pathname.endsWith('/web-proposal/validate')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ valid: true }) });
    if (url.pathname.endsWith('/local-verification-plan')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ summary: {}, page_batches: [], local_ai_instruction: 'read PDF' }) });
    if (url.pathname.endsWith('/local-verification-status')) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'temporarily unavailable' }) });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await page.getByText('批量与高级操作').click();
  await page.getByRole('button', { name: '生成 v2 审核包' }).click();
  await page.locator('#bundleResultInput').fill('{}');
  await page.getByRole('button', { name: '校验网页 AI 建议' }).click();
  await expect(page.getByText(/网页 AI 回传校验完成：通过/)).toBeVisible();
  await expect(page.getByText(/本地核验状态读取失败：temporarily unavailable/)).toBeVisible();
  await page.getByRole('button', { name: '复制精简本地 AI 核验指令' }).click();
  await expect(page.getByText(/精简本地 AI 核验指令已复制|无法使用剪贴板/)).toBeVisible();
});

test('a paper-code deep link resolves the UUID before creating a v2 review bundle', async ({ page }) => {
  let bundleBody = null;
  await mockKnowledge(page);
  await page.route('**/api/content-knowledge/review-bundles/v2', async (route) => {
    bundleBody = JSON.parse(route.request().postData() || '{}');
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ bundle_id: 'bundle-paper-code', object_count: 1, unique_evidence_page_count: 1 }),
    });
  });

  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html?paper_id=B0078`);
  await page.getByText('批量与高级操作').click();
  await page.getByRole('button', { name: '生成 v2 审核包' }).click();

  await expect(page.getByText(/审核包已生成：bundle-paper-code/)).toBeVisible();
  expect(bundleBody).toEqual({ paper_id: PAPER_ID, module: 'abstract' });
});

test('v2 validation errors are rendered as safe text and do not request a local plan', async ({ page }) => {
  let planRequested = false;
  await page.route('**/api/content-knowledge**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/content-knowledge') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [firstItem], total: 1, offset: 0, limit: 25, has_more: false }) });
    if (url.pathname.includes('/items/')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ item: firstItem }) });
    if (url.pathname.endsWith('/v2')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ bundle_id: 'bundle-errors', status: 'created', manifest: { targets: [], allowed_pages: [] } }) });
    if (url.pathname.endsWith('/web-proposal/validate')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ valid: false, status: 'rejected', errors: ['<img src=x onerror=window.xssHit=1>', { code: 'bad_target', detail: '<script>alert(1)</script>' }] }) });
    if (url.pathname.endsWith('/local-verification-plan')) planRequested = true;
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });
  await page.goto(`${BASE_URL}/pages/content_knowledge/index.html`);
  await page.getByRole('button', { name: /Evidence.*claim/ }).click();
  await page.getByText('批量与高级操作').click();
  await page.getByRole('button', { name: '生成 v2 审核包' }).click();
  await page.locator('#bundleResultInput').fill('{}');
  await page.getByRole('button', { name: '校验网页 AI 建议' }).click();
  await expect(page.getByText('<img src=x onerror=window.xssHit=1>')).toBeVisible();
  await expect(page.getByText('<script>alert(1)</script>')).toBeVisible();
  await expect(page.locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => window.xssHit)).toBeUndefined();
  expect(planRequested).toBeFalsy();
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
