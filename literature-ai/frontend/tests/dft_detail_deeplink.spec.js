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
  await expect(targetCard.locator('details.raw-data-toggle')).toHaveAttribute('open', '');
  await expect.poll(() => targetCard.evaluate(node => {
    const rect = node.getBoundingClientRect();
    return rect.top >= 0 && rect.top < window.innerHeight;
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
