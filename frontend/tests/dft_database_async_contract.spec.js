const { test, expect } = require('@playwright/test');

const BASE_URL = 'http://127.0.0.1:4173';

function summaryPayload() {
  return {
    total_records: 2,
    exploratory_numeric_records: 2,
    catalyst_groups: { 'Fe-N4': 1 },
    excluded_reasons: {},
    export_policy: { effective_exports_enabled: true },
  };
}

async function installApi(page, options = {}) {
  const state = { summaryCalls: [], exportStarted: false };
  if (options.holdSummary) state.summaryGate = new Promise(resolve => { state.releaseSummary = resolve; });
  if (options.holdExport) state.exportGate = new Promise(resolve => { state.releaseExport = resolve; });
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === '/api/libraries') {
      return route.fulfill({ json: [
        { name: '锂硫双原子', is_active: true, paper_count: 2 },
        { name: '硫中毒', is_active: false, paper_count: 1 },
      ] });
    }
    if (pathname === '/api/papers/analysis-summary') {
      state.summaryCalls.push(Object.fromEntries(url.searchParams.entries()));
      if (state.summaryGate) await state.summaryGate;
      return route.fulfill({ json: summaryPayload() });
    }
    if (pathname === '/api/papers/compare') {
      return route.fulfill({ json: {
        items: [{
          record_id: 'record-1',
          paper_id: 'paper-1',
          title: 'Evidence paper',
          catalyst_name: 'Fe-N4',
          property_type: 'li2s4_binding_energy_ev',
          value: -1.98,
          unit: 'eV',
          evidence_text: 'Li2S4 binding energy is -1.98 eV.',
          source_section: 'Results',
          sources: options.sources || [{ page_number: 7, page_identity_status: 'verified' }],
          evidence_page: 7,
        }],
        total: 1,
        papers: [],
      } });
    }
    if (pathname === '/api/papers/dft-catalyst-options') return route.fulfill({ json: [] });
    if (pathname === '/api/papers/export/dft-quality' || pathname === '/api/papers/export/dft-review-queue') {
      return route.fulfill({ json: { gate_summary: { eligible: 1, blocked: 0 } } });
    }
    if (pathname === '/api/papers/export/csv') {
      state.exportStarted = true;
      if (state.exportGate) await state.exportGate;
      return route.fulfill({
        contentType: 'text/csv; charset=utf-8',
        headers: {
          'X-D3-Export-Safety-Gate': 'exploratory',
          'X-Exploratory-Row-Count': '1',
          'X-D3-Export-Count': '1',
          'X-D3-Block-Count': '0',
          'X-D3-Base-Eligible-Count': '1',
        },
        body: 'a,b\n1,2\n',
      });
    }
    if (pathname === '/api/papers/export/dft-dataset') {
      return route.fulfill({ json: {
        metadata: {
          safety_gate: 'exploratory',
          dataset_profile: url.searchParams.get('dataset_profile') || '',
          base_eligible_count: 1,
          profile_included_count_before_limit: 1,
          profile_excluded_count: 0,
          exported_count_after_limit: 1,
          blocked_count: 0,
          review_status_counts: {},
        },
        records: [{ record_id: 'record-1' }],
      } });
    }
    return route.fulfill({ status: 404, json: { detail: 'unexpected ' + pathname } });
  });
  return state;
}

async function openPage(page, options = {}) {
  const state = await installApi(page, options);
  await page.goto(BASE_URL + '/pages/dft_database/index.html?library_name=' + encodeURIComponent('锂硫双原子'));
  await expect(page.locator('#dftTable')).toContainText('Evidence paper');
  return state;
}

test('quality overview keeps its 60-second budget after summary coalescing', async ({ page }) => {
  const state = await installApi(page, { holdSummary: true });
  await page.clock.install();
  await page.goto(BASE_URL + '/pages/dft_database/index.html?library_name=' + encodeURIComponent('锂硫双原子'));
  await expect(page.locator('#dftTable')).toContainText('Evidence paper');
  await page.clock.fastForward(21500);
  const text = await page.locator('#qualityDonuts').textContent();
  state.releaseSummary();
  expect(text).not.toContain('读取超时');
  await expect(page.locator('#qualityDonuts')).toContainText('2');
});

test('superseded CSV response cannot overwrite changed-filter status', async ({ page }) => {
  const state = await openPage(page, { holdExport: true });
  await page.locator('button[onclick="exportCSV()"]').click();
  await expect.poll(() => state.exportStarted).toBe(true);
  await page.selectOption('#propertyType', 'binding_energy');
  await expect(page.locator('#exportSafetyStatus')).toContainText('筛选条件已变化');
  state.releaseExport();
  await page.waitForTimeout(100);
  await expect(page.locator('#exportSafetyStatus')).toContainText('筛选条件已变化');
});

test('unverified source page is not presented as an exact PDF page', async ({ page }) => {
  await openPage(page, { sources: [{ page_number: 9, page_identity_status: 'unverified' }] });
  await page.locator('.evidence-link').click();
  const text = await page.locator('#evidenceDetail').textContent();
  expect(text).toContain('候选页码 9（来源身份未核验）');
  expect(text).not.toContain('PDF 第 9 页');
});

test('profile and exploratory JSON stay scoped and desktop/mobile layouts remain contained', async ({ page }) => {
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await openPage(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.selectOption('#libraryFilter', '硫中毒');
    await expect(page.locator('#datasetProfile')).toHaveValue('');
    await expect(page.locator('#datasetProfile option[value="dac_lis_ml"]')).toBeDisabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
});
