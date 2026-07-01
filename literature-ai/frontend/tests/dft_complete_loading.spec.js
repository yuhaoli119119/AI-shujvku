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
    catalyst: 'Sc-BP',
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
  const fullDetail = {
    ...commonDetail,
    dft_results_items: allItems.slice(0, 28),
    dft_results_page: { offset: 0, limit: 28, returned: 28, total: 30, has_more: true },
  };
  let remainingPageRequests = 0;

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
      await new Promise(resolve => setTimeout(resolve, 1500));
      return jsonResponse(route, {
        paper_id: 'paper-1',
        items: allItems.slice(28),
        offset: 28,
        limit: 50,
        returned: 2,
        total: 30,
        has_more: false,
      });
    }
    if (pathname === '/api/papers/paper-1') {
      return jsonResponse(route, url.searchParams.get('mode') === 'light' ? lightDetail : fullDetail);
    }
    if (pathname === '/api/papers/paper-1/codex-context') {
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
    '正在加载完整 DFT 数据 28 / 30 条'
  );
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toHaveCount(0);
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toHaveCount(1);
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toContainText('DFT 30 条');
  await expect(page.locator('#dftContent [data-role="dft-sample-group"]')).toContainText('Sc-BP');
  await expect(page.locator('[data-role="dft-pagination"]')).toHaveCount(0);
  await expect(page.locator('[data-role="load-more-dft"]')).toHaveCount(0);
  expect(remainingPageRequests).toBe(1);
});
