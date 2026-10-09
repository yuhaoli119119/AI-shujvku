const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';

function jsonResponse(route, payload) {
  return route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

function dftItem(index) {
  return {
    id: `dft-${index}`,
    catalyst_sample_id: 'sample-sc',
    bound_catalyst_sample: {
      id: 'sample-sc',
      name: 'Sc-BP',
      catalyst_type: 'unknown',
      metal_centers: [],
    },
    catalyst: 'Parsed Sc-BP label',
    active_site_instance_key: 'Sc-BP|Sc-BP',
    adsorbate: index % 2 ? 'Li2S' : 'S8/LiPSs',
    property_type: 'adsorption_energy',
    value: -index / 10,
    unit: 'eV',
    candidate_status: 'ML_Ready',
    export_safety: { is_exportable: true, eligible: true, blocked_reasons: [] },
  };
}

test('literature library waits for all DFT pages before rendering material groups', async ({ page }) => {
  const allItems = Array.from({ length: 30 }, (_, index) => dftItem(index + 1));
  const paper = {
    id: 'paper-1',
    paper_id: 'paper-1',
    title: 'Complete DFT Group Paper',
    year: 2026,
    journal: 'Journal of Complete DFT',
    paper_type: 'research',
    library_name: 'Default Library',
    pdf_path: 'paper.pdf',
    workflow_status: 'Initial_Parsed',
    pdf_quality_status: 'A_text_readable',
    counts: { sections: 0, figures: 0, dft_results: 30, writing_cards: 0 },
  };
  const commonDetail = {
    ...paper,
    abstract: 'DFT pagination regression fixture.',
    sections: [],
    tables: [],
    figures: [],
    paper_notes: [],
    dft_settings_items: [],
    catalyst_samples_items: [{
      id: 'sample-sc',
      name: 'Sc-BP',
      catalyst_type: 'unknown',
      metal_centers: [],
    }],
    electrochemical_performance_items: [],
    mechanism_claims_items: [],
    writing_cards_items: [],
    outgoing_relationships: [],
    incoming_relationships: [],
    references: [],
  };
  const lightDetail = {
    ...commonDetail,
    dft_results_items: [],
    dft_results_page: { offset: 0, limit: 28, returned: 0, total: 30, has_more: true },
  };
  const dftDetail = {
    ...commonDetail,
    dft_results_items: [],
    dft_results_page: { offset: 0, limit: 28, returned: 0, total: 30, has_more: true },
  };
  let remainingPageRequests = 0;
  let requestedDftLimit = null;
  const detailModes = [];
  let codexContextRequests = 0;

  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (pathname === '/api/libraries') {
      return jsonResponse(route, [{
        name: 'Default Library',
        is_active: true,
        root_path: '/libraries/default',
        paper_count: 1,
      }]);
    }
    if (pathname === '/api/papers/libraries') {
      return jsonResponse(route, [{ name: 'Default Library', paper_count: 1 }]);
    }
    if ((pathname === '/api/papers' || pathname === '/api/papers/') && request.method() === 'GET') {
      return jsonResponse(route, [paper]);
    }
    if (pathname === '/api/papers/paper-1/dft-results') {
      remainingPageRequests += 1;
      requestedDftLimit = url.searchParams.get('limit');
      await new Promise(resolve => setTimeout(resolve, 1500));
      return jsonResponse(route, {
        paper_id: 'paper-1',
        items: allItems,
        offset: 0,
        limit: 100,
        returned: 30,
        total: 30,
        has_more: false,
      });
    }
    if (pathname === '/api/papers/paper-1') {
      const mode = url.searchParams.get('mode');
      detailModes.push(mode);
      return jsonResponse(route, mode === 'dft' ? dftDetail : lightDetail);
    }
    if (pathname === '/api/papers/paper-1/codex-context') {
      codexContextRequests += 1;
      return jsonResponse(route, {
        context: {
          dft_export_readiness: {
            total_candidates: 30,
            eligible_count: 30,
            blocked_count: 0,
            blocked_reasons: {},
            items: [],
          },
        },
      });
    }
    if (pathname === '/api/papers/paper-1/knowledge-context') {
      return jsonResponse(route, { candidates: [], metadata: {} });
    }
    if (pathname.endsWith('/reviews/audit')) {
      return jsonResponse(route, { items: [] });
    }
    if (pathname.endsWith('/evidence/locators')) {
      return jsonResponse(route, { items: [] });
    }
    return jsonResponse(route, {});
  });

  await page.goto(
    `${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}&paper_id=paper-1&tab=dft`
  );

  await expect(page.locator('[data-role="dft-pagination"]')).toContainText(
    '正在加载完整 DFT 数据 0 / 30 条'
  );
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toHaveCount(0);
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toHaveCount(1);
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toContainText('DFT 30 条');
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toContainText('Sc-BP');
  const groupHeader = page.locator('#dftContent [data-role="dft-sample-group"] > summary');
  await expect(groupHeader).toContainText('Sc-BP');
  await expect(groupHeader).not.toContainText('Parsed Sc-BP label');
  await expect(groupHeader).not.toContainText('|');
  await expect(page.locator('[data-role="dft-pagination"]')).toHaveCount(0);
  await expect(page.locator('[data-role="load-more-dft"]')).toHaveCount(0);
  await expect(page.locator('[data-role="dft-status-panel"]')).toContainText('已审核 30');
  await expect(page.locator('text=RAG 可用状态')).toHaveCount(0);
  expect(remainingPageRequests).toBe(1);
  expect(requestedDftLimit).toBe('100');
  expect(detailModes).toEqual(['dft']);
  expect(detailModes).not.toContain('full');
  expect(codexContextRequests).toBe(0);
});

test('literature library falls back to 50 item DFT pages when backend rejects larger pages', async ({ page }) => {
  const allItems = Array.from({ length: 60 }, (_, index) => dftItem(index + 1));
  allItems.forEach(item => {
    item.catalyst = null;
    item.bound_catalyst_sample = null;
  });
  const paper = {
    id: 'paper-compat',
    paper_id: 'paper-compat',
    title: 'Backward Compatible DFT Paging Paper',
    year: 2026,
    journal: 'Journal of Compatible DFT',
    paper_type: 'research',
    library_name: 'Default Library',
    pdf_path: 'paper.pdf',
    counts: { sections: 0, figures: 0, dft_results: 60, writing_cards: 0 },
  };
  const commonDetail = {
    ...paper,
    abstract: 'DFT fallback fixture.',
    sections: [],
    tables: [],
    figures: [],
    paper_notes: [],
    dft_settings_items: [],
    catalyst_samples_items: [{
      id: 'sample-sc',
      name: '',
      catalyst_type: 'unknown',
      metal_centers: [],
    }],
    electrochemical_performance_items: [],
    mechanism_claims_items: [],
    writing_cards_items: [],
    outgoing_relationships: [],
    incoming_relationships: [],
    references: [],
  };
  const lightDetail = {
    ...commonDetail,
    dft_results_items: [],
    dft_results_page: { offset: 0, limit: 28, returned: 0, total: 60, has_more: true },
  };
  const dftDetail = {
    ...commonDetail,
    dft_results_items: [],
    dft_results_page: { offset: 0, limit: 28, returned: 0, total: 60, has_more: true },
  };
  const requestedLimits = [];

  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (pathname === '/api/libraries') {
      return jsonResponse(route, [{
        name: 'Default Library',
        is_active: true,
        root_path: '/libraries/default',
        paper_count: 1,
      }]);
    }
    if (pathname === '/api/papers/libraries') {
      return jsonResponse(route, [{ name: 'Default Library', paper_count: 1 }]);
    }
    if ((pathname === '/api/papers' || pathname === '/api/papers/') && request.method() === 'GET') {
      return jsonResponse(route, [paper]);
    }
    if (pathname === '/api/papers/paper-compat/dft-results') {
      const limit = url.searchParams.get('limit');
      requestedLimits.push(limit);
      if (limit === '100') {
        return route.fulfill({
          status: 422,
          contentType: 'application/json; charset=utf-8',
          body: JSON.stringify({ detail: [{ loc: ['query', 'limit'], msg: 'Input should be less than or equal to 50' }] }),
        });
      }
      return jsonResponse(route, {
        paper_id: 'paper-compat',
        items: limit === '50' && requestedLimits.length === 2 ? allItems.slice(0, 50) : allItems.slice(50),
        offset: limit === '50' && requestedLimits.length === 2 ? 0 : 50,
        limit: 50,
        returned: limit === '50' && requestedLimits.length === 2 ? 50 : 10,
        total: 60,
        has_more: limit === '50' && requestedLimits.length === 2,
      });
    }
    if (pathname === '/api/papers/paper-compat') {
      return jsonResponse(route, url.searchParams.get('mode') === 'dft' ? dftDetail : lightDetail);
    }
    if (pathname === '/api/papers/paper-compat/codex-context') {
      return jsonResponse(route, {
        context: {
          dft_export_readiness: {
            total_candidates: 60,
            eligible_count: 60,
            blocked_count: 0,
            blocked_reasons: {},
            items: [],
          },
        },
      });
    }
    if (pathname.endsWith('/reviews/audit')) return jsonResponse(route, { items: [] });
    if (pathname.endsWith('/evidence/locators')) return jsonResponse(route, { items: [] });
    return jsonResponse(route, {});
  });

  await page.goto(
    `${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}&paper_id=paper-compat&tab=dft`
  );

  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toHaveCount(1);
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toContainText('DFT 60 条');
  const groupHeader = page.locator('#dftContent [data-role="dft-sample-group"] > summary');
  await expect(groupHeader).toContainText('已关联催化剂（名称待补）');
  await expect(groupHeader).not.toContainText('CatalystSample sample-sc');
  expect(requestedLimits).toEqual(['100', '50', '50']);
});
