// 验收 P1/P2 缺陷的专项回归测试
// 1) ingestion 切库并发：新库刷新不得被旧库进行中请求吞掉
// 2) workbench 失败原子性：部分请求失败时不得提交新论文的 locatorCache
// 3) workbench 代际守卫：过期失败不得覆盖新论文成功结果
// 4) jobs-acquisition 恢复即查：visibilitychange/online 应立即轮询而非等待旧定时器
const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:4173';

function jsonResponse(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

test.describe('P1: ingestion 切库并发刷新', () => {
  test('A 库请求进行中切换到 B，B 的刷新在旧请求结束后立即执行', async ({ page }) => {
    const jobsRequests = [];
    let activeLib = 'LibA';  // activate 后切换，模拟真实后端行为
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === '/api/libraries' && route.request().method() === 'GET') {
        return jsonResponse(route, [
          { name: 'LibA', is_active: activeLib === 'LibA' },
          { name: 'LibB', is_active: activeLib === 'LibB' },
        ]);
      }
      if (path.startsWith('/api/libraries/') && path.endsWith('/activate')) {
        activeLib = decodeURIComponent(path.split('/')[3]);
        return jsonResponse(route, { ok: true });
      }
      if (path === '/api/jobs') {
        const lib = url.searchParams.get('library_name') || '';
        jobsRequests.push({ lib, at: Date.now() });
        if (lib === 'LibA') {
          // A 库响应延迟 1.5s，模拟慢请求
          await new Promise(resolve => setTimeout(resolve, 1500));
          return jsonResponse(route, []);
        }
        return jsonResponse(route, [{
          job_id: 'job-b-1',
          type: 'local_pdf_path_ingest',
          status: 'completed',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          summary: { title: 'B库专属任务标题' },
        }]);
      }
      if (path === '/api/papers/type-stats') return jsonResponse(route, {});
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/ingestion/index.html`);
    // 等 A 库首个 jobs 请求发出（处于 1.5s 延迟中）
    await expect.poll(() => jobsRequests.some(r => r.lib === 'LibA'), { timeout: 10000 }).toBe(true);
    const switchAt = Date.now();
    await page.evaluate(() => switchLibrary('LibB'));

    // 修复后：B 的请求应在旧请求结束后立即执行（远小于 12s 轮询周期）
    await expect.poll(() => jobsRequests.some(r => r.lib === 'LibB'), { timeout: 8000 }).toBe(true);
    const bReq = jobsRequests.find(r => r.lib === 'LibB');
    expect(bReq.at - switchAt).toBeLessThan(8000);
    // B 的任务应真实渲染出来（旧结果会被序列号门禁丢弃，不能停留在空列表）
    await expect(page.locator('text=B库专属任务标题').first()).toBeVisible({ timeout: 8000 });
  });
});

test.describe('P1: workbench 失败原子性', () => {
  test('paper detail 失败时不得提交新论文状态或泄漏 locatorCache', async ({ page }) => {
    const P1 = 'paper-ok';
    const P2 = 'paper-fail';
    const paperPayload = id => ({
      id, paper_id: id, title: `论文 ${id}`, year: 2026, journal: 'J', doi: null,
      sections: [{ id: `${id}-s1`, section_title: 'Intro', section_type: 'intro', page_start: 1, page_end: 2, text: 'text' }],
    });
    const extractionPayload = {
      results: { DFTResult: [{ target_id: 'r1', target_type: 'DFTResult', value: { value: -1.0, unit: 'eV', verified: false, confidence: 0.9, evidence_text: 'ev', evidence_locator: { locator_status: 'exact_page', page: 3, evidence_text: 'ev', parser_source: 'Docling', can_jump_to_pdf_page: true } } }] },
      validation_warnings: [],
      field_reviews: [],
    };
    let locatorCalls = 0;
    let auditCalls = 0;
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === '/api/papers' || path === '/api/papers/') {
        return jsonResponse(route, [{ id: P1, title: '论文 paper-ok' }, { id: P2, title: '论文 paper-fail' }]);
      }
      if (path === `/api/papers/${P1}`) return jsonResponse(route, paperPayload(P1));
      if (path === `/api/papers/${P2}`) return jsonResponse(route, { detail: 'forced detail failure' }, 500);
      if (path.includes('/evidence-locators')) {
        locatorCalls += 1;
        if (path.includes(P2)) {
          return jsonResponse(route, [{ target_id: 'new-target', target_type: 'DFTResult', field_name: 'new-field', page: 9 }]);
        }
        return jsonResponse(route, [{ target_id: 't1', target_type: 'DFTResult', field_name: 'value', page: 3 }]);
      }
      if (path.includes('/reviews/audit')) {
        auditCalls += 1;
        return jsonResponse(route, { items: [] });
      }
      if (path.includes('/extraction/results/')) return jsonResponse(route, extractionPayload);
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html`);
    // 等 P1 完整加载并渲染
    await expect(page.locator('#paperMeta')).toContainText('论文 paper-ok', { timeout: 15000 });
    await expect(page.locator('#schemaForm')).toContainText('PDF 定位准确');

    // 切换到 P2：detail 500、locators 成功（污染场景）
    await page.evaluate((p2) => {
      const select = document.getElementById('paperSelect');
      select.value = p2;
      return loadSelectedPaper();
    }, P2);
    await expect(page.locator('#toast')).toContainText('forced detail failure', { timeout: 10000 });

    // 原子性断言：state 仍停留在 P1；首屏使用内嵌 locator，独立缓存保持为空。
    const snapshot = await page.evaluate(() => ({
      paperId: state.paper && state.paper.id,
      locatorKeys: Object.keys(state.locatorCache),
    }));
    expect(snapshot.paperId).toBe(P1);
    expect(snapshot.locatorKeys).toEqual([]);
    expect(locatorCalls).toBe(0);
    expect(auditCalls).toBe(0);
  });

  test('A 的延迟失败不得在 B 成功后显示过期错误', async ({ page }) => {
    const PAPER_A = 'paper-slow-fail';
    const PAPER_B = 'paper-fast-success';
    const STALE_ERROR = 'forced stale A detail failure';
    let aDetailStarted = false;
    let aDetailFinished = false;
    const paperPayload = (id, title, sectionText) => ({
      id,
      paper_id: id,
      title,
      year: 2026,
      journal: 'J',
      doi: null,
      sections: [{
        id: `${id}-s1`,
        section_title: `${title} 章节`,
        section_type: 'intro',
        page_start: 1,
        page_end: 2,
        text: sectionText,
      }],
    });
    const extractionPayload = (targetId, value) => ({
      results: {
        DFTResult: [{
          target_id: targetId,
          target_type: 'DFTResult',
          value: { value, unit: 'eV', verified: false, confidence: 0.9, evidence_text: `${value} evidence` },
        }],
      },
      validation_warnings: [],
      field_reviews: [],
    });

    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === '/api/papers' || path === '/api/papers/') {
        return jsonResponse(route, [
          { id: PAPER_A, title: '慢失败论文 A' },
          { id: PAPER_B, title: '成功论文 B' },
        ]);
      }
      if (path === `/api/papers/${PAPER_A}`) {
        aDetailStarted = true;
        await new Promise(resolve => setTimeout(resolve, 1200));
        aDetailFinished = true;
        return jsonResponse(route, { detail: STALE_ERROR }, 500);
      }
      if (path === `/api/papers/${PAPER_B}`) {
        return jsonResponse(route, paperPayload(PAPER_B, '成功论文 B', 'B 正常渲染内容'));
      }
      if (path === `/api/extraction/results/${PAPER_A}`) return jsonResponse(route, extractionPayload('a-result', -1.1));
      if (path === `/api/extraction/results/${PAPER_B}`) return jsonResponse(route, extractionPayload('b-result', -2.2));
      if (path.includes('/reviews/audit')) return jsonResponse(route, { items: [] });
      if (path.includes('/evidence-locators')) return jsonResponse(route, []);
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html`);
    await expect.poll(() => aDetailStarted, { timeout: 10000 }).toBe(true);

    await page.evaluate((paperB) => {
      const select = document.getElementById('paperSelect');
      select.value = paperB;
      return loadSelectedPaper();
    }, PAPER_B);

    await expect(page.locator('#paperMeta')).toContainText('成功论文 B', { timeout: 10000 });
    await expect(page.locator('#sectionList')).toContainText('成功论文 B 章节');
    await expect(page.locator('#schemaForm')).toContainText('-2.2');
    await expect.poll(() => aDetailFinished, { timeout: 10000 }).toBe(true);

    const finalPaperId = await page.evaluate(() => state.paper && state.paper.id);
    expect(finalPaperId).toBe(PAPER_B);
    await expect(page.locator('#toast')).not.toContainText(STALE_ERROR);
    await expect(page.locator('#paperMeta')).toContainText('成功论文 B');
  });
});

function workbenchPaper(id, title = `论文 ${id}`) {
  return {
    id,
    paper_id: id,
    title,
    year: 2026,
    journal: 'Journal',
    doi: null,
    sections: [{ id: `${id}-section`, section_title: `${title} 章节`, section_type: 'results', page_start: 1, page_end: 2, text: `${title} section text` }],
  };
}

function evidenceField(value, locator, review = null) {
  return {
    value,
    unit: 'eV',
    confidence: 0.9,
    evidence_text: `${value} evidence`,
    verified: review ? review.verified === true : false,
    review,
    evidence_locator: locator,
  };
}

function workbenchExtraction(id, options = {}) {
  return {
    paper_id: id,
    schemas: { DFTResult: {} },
    validation_status: 'needs_review',
    validation_warnings: options.validationWarnings || [],
    field_reviews: options.fieldReviews || [],
    results: {
      DFTResult: [{
        target_id: `${id}-result`,
        target_type: 'DFTResult',
        value: evidenceField(options.value || `${id}-value`, options.locator || null, options.review || null),
        ...(options.extraFields || {}),
      }],
    },
  };
}

test.describe('方案 A: workbench 首屏关键路径与重复 GET 门禁', () => {
  test('URL paper_id 直载不等待延迟 4 秒的列表，且同一详情只请求一次', async ({ page }) => {
    const paperId = 'paper-direct';
    const calls = { list: 0, paper: 0, extraction: 0, audit: 0, locators: 0 };
    const started = {};
    let listFinishedAt = 0;
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/papers' || path === '/api/papers/') {
        calls.list += 1;
        started.list = Date.now();
        await new Promise(resolve => setTimeout(resolve, 4000));
        listFinishedAt = Date.now();
        return jsonResponse(route, [{ id: paperId, title: '直载论文' }]);
      }
      if (path === `/api/papers/${paperId}`) {
        calls.paper += 1;
        started.paper = Date.now();
        return jsonResponse(route, workbenchPaper(paperId, '直载论文'));
      }
      if (path === `/api/extraction/results/${paperId}`) {
        calls.extraction += 1;
        started.extraction = Date.now();
        return jsonResponse(route, workbenchExtraction(paperId, { value: 'direct-value' }));
      }
      if (path.endsWith('/reviews/audit')) {
        calls.audit += 1;
        return jsonResponse(route, { items: [] });
      }
      if (path.endsWith('/evidence-locators')) {
        calls.locators += 1;
        return jsonResponse(route, []);
      }
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${paperId}`);
    await expect(page.locator('#schemaForm')).toContainText('direct-value', { timeout: 3000 });
    expect(listFinishedAt).toBe(0);
    expect(started.paper).toBeTruthy();
    expect(started.extraction).toBeTruthy();

    await expect.poll(() => listFinishedAt, { timeout: 7000 }).toBeGreaterThan(0);
    await expect(page.locator('#paperSelect')).toHaveValue(paperId);
    expect(started.paper).toBeLessThan(listFinishedAt);
    expect(started.extraction).toBeLessThan(listFinishedAt);
    expect(calls).toEqual({ list: 1, paper: 1, extraction: 1, audit: 0, locators: 0 });
  });

  test('URL 论文不在前 200 条列表中时保留临时 option 和已加载详情', async ({ page }) => {
    const requestedId = 'paper-outside-list';
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/papers' || path === '/api/papers/') return jsonResponse(route, [{ id: 'paper-other', title: '其它论文' }]);
      if (path === `/api/papers/${requestedId}`) return jsonResponse(route, workbenchPaper(requestedId, '列表外论文'));
      if (path === `/api/extraction/results/${requestedId}`) return jsonResponse(route, workbenchExtraction(requestedId, { value: 'outside-value' }));
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${requestedId}`);
    await expect(page.locator('#paperMeta')).toContainText('列表外论文');
    await expect(page.locator('#schemaForm')).toContainText('outside-value');
    await expect(page.locator('#paperSelect')).toHaveValue(requestedId);
    await expect(page.locator(`#paperSelect option[value="${requestedId}"]`)).toHaveCount(1);
  });

  test('无 URL paper_id 时列表完成后只加载首篇一次且不产生空 extraction 请求', async ({ page }) => {
    const firstId = 'paper-first';
    let paperCalls = 0;
    let extractionCalls = 0;
    const apiPaths = [];
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      apiPaths.push(path);
      if (path === '/api/papers' || path === '/api/papers/') {
        await new Promise(resolve => setTimeout(resolve, 250));
        return jsonResponse(route, [{ id: firstId, title: '首篇论文' }, { id: 'paper-second', title: '第二篇' }]);
      }
      if (path === `/api/papers/${firstId}`) {
        paperCalls += 1;
        return jsonResponse(route, workbenchPaper(firstId, '首篇论文'));
      }
      if (path === `/api/extraction/results/${firstId}`) {
        extractionCalls += 1;
        return jsonResponse(route, workbenchExtraction(firstId, { value: 'first-value' }));
      }
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html`);
    await expect(page.locator('#schemaForm')).toContainText('first-value');
    expect(paperCalls).toBe(1);
    expect(extractionCalls).toBe(1);
    expect(apiPaths.some(path => path === '/api/extraction/results/')).toBe(false);
  });

  test('field_reviews 派生 audit 保留计数、orphan、write_version 与嵌入 locator，并在切换后清空旧缓存', async ({ page }) => {
    const paperA = 'paper-audit-a';
    const paperB = 'paper-audit-b';
    const activeReview = { id: 'review-active', target_type: 'DFTResult', target_id: `${paperA}-result`, field_name: 'value', target_resolution_status: 'active', reviewer_status: 'pending', verified: false, write_version: 7 };
    const fieldReviews = [
      activeReview,
      { id: 'review-remapped', target_type: 'DFTResult', target_id: 'remapped-target', field_name: 'value', target_resolution_status: 'remapped', reviewer_status: 'pending', verified: false, write_version: 2 },
      { id: 'review-stale', target_type: 'DFTResult', target_id: 'orphan-stale', field_name: 'value', target_resolution_status: 'stale', reviewer_status: 'verified', verified: true, target_label: '旧目标', field_path: 'DFTResult.value', reviewed_value: -9.9, unit: 'eV', evidence_text: 'orphan evidence', target_fingerprint: 'fp-stale', write_version: 3 },
      { id: 'review-ambiguous', target_type: 'DFTResult', target_id: 'ambiguous-target', field_name: 'value', target_resolution_status: 'ambiguous', reviewer_status: 'pending', verified: false, write_version: 4 },
      { id: 'review-unresolved', target_type: 'DFTResult', target_id: 'unresolved-target', field_name: 'value', target_resolution_status: 'unresolved', reviewer_status: 'pending', verified: false, write_version: 5 },
      { id: 'review-empty', target_type: 'OtherType', target_id: 'empty-target', field_name: 'value', target_resolution_status: '', reviewer_status: 'pending', verified: false, write_version: 6 },
      { id: 'review-nonstandard', target_type: 'OtherType', target_id: 'other-target', field_name: 'value', target_resolution_status: 'future_status', reviewer_status: 'pending', verified: false, write_version: 8 },
    ];
    const exact = { locator_status: 'exact_page', page: 7, bbox: null, evidence_text: 'exact evidence', parser_source: 'Docling', can_jump_to_pdf_page: true };
    const approximate = { locator_status: 'approximate', page: 5, bbox: null, evidence_text: 'approx evidence', parser_source: 'Docling', can_jump_to_pdf_page: false };
    const missing = { locator_status: 'missing_locator', page: null, bbox: null, evidence_text: 'missing evidence', parser_source: 'unknown', can_jump_to_pdf_page: false };
    const extractionA = workbenchExtraction(paperA, {
      value: 'audit-value',
      locator: exact,
      review: activeReview,
      fieldReviews,
      extraFields: {
        reaction_step: evidenceField('approx-value', approximate),
        adsorbate: evidenceField('missing-value', missing),
      },
    });
    const extractionB = workbenchExtraction(paperB, { value: 'clean-value' });
    let auditCalls = 0;
    let locatorCalls = 0;
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/papers' || path === '/api/papers/') return jsonResponse(route, [{ id: paperA, title: '审计论文 A' }, { id: paperB, title: '审计论文 B' }]);
      if (path === `/api/papers/${paperA}`) return jsonResponse(route, workbenchPaper(paperA, '审计论文 A'));
      if (path === `/api/papers/${paperB}`) return jsonResponse(route, workbenchPaper(paperB, '审计论文 B'));
      if (path === `/api/extraction/results/${paperA}`) return jsonResponse(route, extractionA);
      if (path === `/api/extraction/results/${paperB}`) return jsonResponse(route, extractionB);
      if (path.endsWith('/reviews/audit')) {
        auditCalls += 1;
        return jsonResponse(route, { items: [] });
      }
      if (path.endsWith('/evidence-locators')) {
        locatorCalls += 1;
        return jsonResponse(route, []);
      }
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${paperA}`);
    await expect(page.locator('#schemaForm')).toContainText('audit-value');
    await expect(page.locator('#stabilitySummaryBox')).toContainText('有效: 1');
    await expect(page.locator('#stabilitySummaryBox')).toContainText('已重映射: 1');
    await expect(page.locator('#stabilitySummaryBox')).toContainText('已失效: 1');
    await expect(page.locator('#stabilitySummaryBox')).toContainText('有歧义: 1');
    await expect(page.locator('#stabilitySummaryBox')).toContainText('未解析: 1');
    await expect(page.locator('#stabilitySummaryBox')).toContainText('未知: 2');
    await expect(page.locator('.field-container:has-text("orphan-stale")')).toContainText('orphan evidence');
    await expect(page.locator('button:has-text("查看 PDF 第 7 页")')).toHaveCount(1);
    await expect(page.locator('#schemaForm')).toContainText('可能相关页码，需要确认');
    await expect(page.locator('#schemaForm')).toContainText('暂无可用 PDF 定位');

    const auditState = await page.evaluate(() => ({
      audit: state.audit,
      writeVersion: state.reviewVersions['DFTResult:paper-audit-a-result:value'],
      pendingCount: getPendingReviewRows().length,
      locatorKeys: Object.keys(state.locatorCache),
    }));
    expect(auditState.audit.total_reviews).toBe(7);
    expect(auditState.audit.unknown).toBe(2);
    expect(auditState.writeVersion).toBe(7);
    expect(auditState.pendingCount).toBeGreaterThan(0);
    expect(auditState.locatorKeys).toEqual([]);
    expect(auditCalls).toBe(0);
    expect(locatorCalls).toBe(0);

    await page.selectOption('#paperSelect', paperB);
    await page.evaluate(() => loadSelectedPaper());
    await expect(page.locator('#schemaForm')).toContainText('clean-value');
    await expect(page.locator('#schemaForm')).not.toContainText('audit-value');
    await expect(page.locator('button:has-text("查看 PDF 第 7 页")')).toHaveCount(0);
    expect(await page.evaluate(() => Object.keys(state.locatorCache))).toEqual([]);
  });

  test('写后显式刷新仍请求 audit 和 evidence-locators', async ({ page }) => {
    const paperId = 'paper-refresh';
    const extraction = workbenchExtraction(paperId, { value: 'refresh-value' });
    let auditCalls = 0;
    let locatorCalls = 0;
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === '/api/papers' || path === '/api/papers/') return jsonResponse(route, [{ id: paperId, title: '刷新论文' }]);
      if (path === `/api/papers/${paperId}`) return jsonResponse(route, workbenchPaper(paperId, '刷新论文'));
      if (path === `/api/extraction/results/${paperId}`) return jsonResponse(route, extraction);
      if (path === `/api/extraction/results/${paperId}/validate`) return jsonResponse(route, { ...extraction, status: 'validated' });
      if (path === `/api/extraction/results/${paperId}/reviews/audit`) {
        auditCalls += 1;
        return jsonResponse(route, { paper_id: paperId, total_reviews: 0, active: 0, remapped: 0, stale: 0, ambiguous: 0, unresolved: 0, unknown: 0, items: [] });
      }
      if (path === `/api/extraction/results/${paperId}/evidence-locators`) {
        locatorCalls += 1;
        return jsonResponse(route, []);
      }
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${paperId}`);
    await expect(page.locator('#schemaForm')).toContainText('refresh-value');
    expect(auditCalls).toBe(0);
    expect(locatorCalls).toBe(0);
    await page.evaluate(() => refreshReviewStateAfterMutation());
    expect(auditCalls).toBe(1);
    expect(locatorCalls).toBe(1);
  });
});

test.describe('P2: jobs-acquisition 恢复即查', () => {
  test('visibilitychange 恢复时立即轮询，不等待未触发的定时器', async ({ page }) => {
    let jobRequests = 0;
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === '/api/jobs/job-resume-1') {
        jobRequests += 1;
        return jsonResponse(route, { job_id: 'job-resume-1', status: 'running', result: {}, summary: {} });
      }
      if (path === '/api/libraries') return jsonResponse(route, [{ name: 'Default Library', is_active: true }]);
      if (path === '/api/papers' || path === '/api/papers/') return jsonResponse(route, { items: [], total: 0 });
      return jsonResponse(route, {});
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}`);
    await page.waitForFunction(() => typeof pollWorkflowIngestJob === 'function');

    // 启动轮询链（t=0 第一次请求），等到第一次 3s 定时调度完成、下一定时器挂起
    await page.evaluate(() => pollWorkflowIngestJob('job-resume-1'));
    await expect.poll(() => jobRequests, { timeout: 10000 }).toBe(1);
    await expect.poll(() => jobRequests, { timeout: 10000 }).toBe(2);  // 3s 后第二次
    const before = jobRequests;

    // 下一次定时器（3s 周期）挂起中触发恢复事件：应立即补一次请求
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect.poll(() => jobRequests, { timeout: 2000, intervals: [100, 200, 300, 500] }).toBe(before + 1);
  });
});
