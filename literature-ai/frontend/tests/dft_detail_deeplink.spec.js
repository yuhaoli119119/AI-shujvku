const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';

function jsonResponse(route, payload) {
  return route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

function makePaper() {
  return {
    id: 'paper-1',
    title: 'DFT Deep Link Paper',
    year: 2026,
    journal: 'Journal of Review UX',
    doi: '10.1000/dft-link',
    pdf_path: 'paper.pdf',
    abstract: 'Synthetic DFT detail payload.',
    counts: { sections: 1, figures: 0, dft_results: 2, mechanism_claims: 0 },
    sections: [{ id: 'section-1', section_title: 'Results', text: 'DFT values are discussed.', page_start: 3, page_end: 4 }],
    figures: [],
    tables: [],
    dft_settings_items: [],
    dft_results_items: [
      { id: 'dft-other', catalyst: 'Fe-N4', property_type: 'adsorption_energy', value: -1.1, unit: 'eV' },
      { id: 'dft-target', catalyst: 'Co-N4', property_type: 'adsorption_energy', adsorbate: 'Li2S4', value: -1.8, unit: 'eV' },
    ],
    electrochemical_performance_items: [],
    mechanism_claims_items: [],
    writing_cards_items: [],
  };
}

test('DFT deep link opens tab, expands, scrolls and highlights target card with issue context', async ({ page }) => {
  const requests = [];
  await page.addInitScript(() => {
    window.__copiedDftLocator = '';
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async text => {
          window.__copiedDftLocator = String(text || '');
        },
      },
    });
  });
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', route => {
    const request = route.request();
    requests.push({ method: request.method(), pathname: new URL(request.url()).pathname });
    const url = new URL(request.url());
    if (url.pathname === '/api/papers/paper-1') return jsonResponse(route, makePaper());
    if (url.pathname === '/api/evidence/claims') return jsonResponse(route, []);
    return jsonResponse(route, {});
  });

  await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1&tab=dft&target_type=dft_results&target_id=dft-target&issue_id=issue-77`);

  await expect(page.locator('#tab-dft')).toHaveClass(/active/);
  await expect(page.locator('#dftIssueContext')).toContainText('来自 DFT 核验 issue: issue-77');
  const returnLink = page.getByRole('link', { name: '返回 DFT 核验中心' });
  const href = await returnLink.getAttribute('href');
  const url = new URL(href, `${BASE_URL}/pages/paper_detail/index.html`);
  expect(url.pathname).toBe('/pages/dft_audit_center/index.html');
  expect(url.searchParams.get('paper_id')).toBe('paper-1');

  const targetCard = page.locator('#dftResults [data-record-id="dft-target"]');
  await expect(targetCard).toHaveClass(/deep-link-target/);
  await expect(page.locator('#dftResults [data-role="dft-record-number"]')).toHaveText(['DFT #1', 'DFT #2']);
  await expect(targetCard.locator('[data-role="dft-record-locator"]')).toHaveText('DFT #2');
  await expect(targetCard.locator('[data-role="dft-core-value"]')).toContainText('-1.8');
  await expect(targetCard.locator('[data-role="dft-core-value"]')).toContainText('eV');
  await targetCard.locator('[data-role="copy-dft-locator"]').click();
  await expect.poll(() => page.evaluate(() => window.__copiedDftLocator)).toBe('DFT #2; dft_result_id=dft-target');
  await expect(targetCard.locator('details.dft-detail-toggle')).toHaveAttribute('open', '');
  await expect(targetCard.locator('details.raw-data-toggle')).toHaveAttribute('open', '');
  await expect.poll(() => targetCard.evaluate(node => {
    const rect = node.getBoundingClientRect();
    return rect.top >= 0 && rect.top < window.innerHeight;
  })).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(targetCard.locator('[data-role="copy-dft-locator"]')).toBeVisible();
  await expect.poll(() => targetCard.locator('[data-role="dft-record-locator"]').evaluate(node => {
    return node.scrollWidth <= node.clientWidth + 1;
  })).toBe(true);
  await expect.poll(() => targetCard.evaluate(node => {
    return node.scrollWidth <= node.clientWidth + 1;
  })).toBe(true);
  expect(requests.every(request => request.method === 'GET')).toBe(true);
});

test('DFT deep link shows a non-blocking warning when target card is missing', async ({ page }) => {
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/papers/paper-1') return jsonResponse(route, makePaper());
    if (url.pathname === '/api/evidence/claims') return jsonResponse(route, []);
    return jsonResponse(route, {});
  });

  await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1&tab=dft&target_type=dft_results&target_id=missing-dft`);

  await expect(page.locator('#tab-dft')).toHaveClass(/active/);
  await expect(page.locator('#dftDeepLinkWarning')).toContainText('未找到目标 DFT 条目，可能已被删除或筛选隐藏');
});

test('paper detail edits one DFT row through the audited manual update API', async ({ page }) => {
  let updatePayload = null;
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'PATCH' && url.pathname === '/api/papers/paper-1/dft-results/dft-target') {
      updatePayload = JSON.parse(request.postData() || '{}');
      return jsonResponse(route, {
        paper_id: 'paper-1',
        dft_result_id: 'dft-target',
        changed_fields: ['value', 'reaction_step'],
        corrections: [],
        invalidated_review_ids: [],
        export_safety: { is_exportable: false, blocked_reasons: ['unsafe_review'] },
      });
    }
    if (url.pathname === '/api/papers/paper-1') return jsonResponse(route, makePaper());
    if (url.pathname === '/api/evidence/claims') return jsonResponse(route, []);
    return jsonResponse(route, {});
  });

  await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1&tab=dft`);
  const card = page.locator('#dftResults [data-record-id="dft-target"]');
  await card.getByRole('button', { name: '修改数据' }).click();
  await expect(page.locator('#dftEditOverlay')).toBeVisible();
  await expect(page.locator('#dftEditLocator')).toContainText('dft_result_id=dft-target');
  await page.locator('#dftEditValue').fill('-1.95');
  await page.locator('#dftEditReactionStep').fill('Li2S4 adsorption');
  await page.locator('#dftEditReason').fill('对照原 PDF 表格修正。');
  await page.locator('#dftEditSubmit').click();

  await expect.poll(() => updatePayload).not.toBeNull();
  expect(updatePayload).toMatchObject({
    confirm_manual_update: true,
    reviewer: 'paper_detail_user',
    reason: '对照原 PDF 表格修正。',
    updates: {
      value: -1.95,
      reaction_step: 'Li2S4 adsorption',
    },
  });
  expect(Object.keys(updatePayload.updates).sort()).toEqual(['reaction_step', 'value']);
  await expect(page.locator('#dftEditOverlay')).toBeHidden();
});

test('paper detail starts light and paginates DFT results in batches of 50', async ({ page }) => {
  const allItems = Array.from({ length: 60 }, (_, index) => ({
    id: `dft-${index + 1}`,
    property_type: 'adsorption_energy',
    value: -index / 10,
    unit: 'eV',
  }));
  const lightPaper = {
    ...makePaper(),
    counts: { sections: 1, figures: 0, dft_results: 60, mechanism_claims: 0 },
    sections: [],
    dft_results_items: [],
    dft_results_page: { offset: 0, limit: 50, returned: 0, total: 60, has_more: true },
  };
  const fullPaper = {
    ...makePaper(),
    counts: { sections: 1, figures: 0, dft_results: 60, mechanism_claims: 0 },
    dft_results_items: allItems.slice(0, 50),
    dft_results_page: { offset: 0, limit: 50, returned: 50, total: 60, has_more: true },
  };
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/papers/paper-1/dft-results') {
      return jsonResponse(route, {
        paper_id: 'paper-1',
        items: allItems.slice(50),
        offset: 50,
        limit: 50,
        returned: 10,
        total: 60,
        has_more: false,
      });
    }
    if (url.pathname === '/api/papers/paper-1') {
      return jsonResponse(route, url.searchParams.get('mode') === 'light' ? lightPaper : fullPaper);
    }
    if (url.pathname === '/api/evidence/claims') return jsonResponse(route, []);
    return jsonResponse(route, {});
  });

  await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1`);
  await expect(page.locator('#dftResults [data-collection="dft_results"]')).toHaveCount(0);
  await page.getByRole('button', { name: 'DFT 候选与性能' }).click();
  await expect(page.locator('#dftResults [data-collection="dft_results"]')).toHaveCount(50);
  await expect(page.locator('[data-role="dft-pagination"]')).toContainText('已加载 50 / 60 条');
  await page.locator('[data-role="load-more-dft"]').click();
  await expect(page.locator('#dftResults [data-collection="dft_results"]')).toHaveCount(60);
  await expect(page.locator('[data-role="load-more-dft"]')).toHaveCount(0);
});

test('DFT deep link fetches a target outside the first server page', async ({ page }) => {
  const firstPage = Array.from({ length: 50 }, (_, index) => ({
    id: `dft-${index + 1}`,
    property_type: 'adsorption_energy',
    value: -index / 10,
    unit: 'eV',
  }));
  const lightPaper = {
    ...makePaper(),
    counts: { sections: 0, figures: 0, dft_results: 60, mechanism_claims: 0 },
    sections: [],
    dft_results_items: [],
  };
  const fullPaper = {
    ...lightPaper,
    dft_results_items: firstPage,
    dft_results_page: { offset: 0, limit: 50, returned: 50, total: 60, has_more: true },
  };
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/papers/paper-1/dft-results' && url.searchParams.get('result_id') === 'dft-55') {
      return jsonResponse(route, {
        items: [{ id: 'dft-55', property_type: 'adsorption_energy', value: -5.5, unit: 'eV' }],
        total: 60,
        has_more: false,
      });
    }
    if (url.pathname === '/api/papers/paper-1') {
      return jsonResponse(route, url.searchParams.get('mode') === 'light' ? lightPaper : fullPaper);
    }
    if (url.pathname === '/api/evidence/claims') return jsonResponse(route, []);
    return jsonResponse(route, {});
  });

  await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1&tab=dft&target_type=dft_results&target_id=dft-55`);
  await expect(page.locator('#dftResults [data-record-id="dft-55"]')).toHaveClass(/deep-link-target/);
  await expect(page.locator('#dftResults [data-collection="dft_results"]')).toHaveCount(51);
});
