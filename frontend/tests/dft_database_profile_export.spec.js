const { test, expect } = require('@playwright/test');
const path = require('path');

test.describe('DFT Database Profile Export Interaction', () => {
  test('datasetProfile selection does not reload table and carries correct param in CSV and ML exports', async ({ page }) => {
    let compareCount = 0;
    let lastCsvParams = null;
    let lastMlParams = null;

    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;

      if (pathname === '/api/libraries') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([{ name: 'test_lib', is_active: true }]),
        });
      }
      if (pathname === '/api/papers/aggregate') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            adsorbate_groups: {},
            catalyst_groups: {},
            possible_name_aliases: [],
          }),
        });
      }
      if (pathname === '/api/papers/compare') {
        compareCount++;
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], total: 0, papers: [] }),
        });
      }
      if (pathname === '/api/papers/dft-catalyst-options') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([]),
        });
      }
      if (pathname === '/api/papers/export/dft-quality' || pathname === '/api/papers/export/dft-review-queue') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ gate_summary: { eligible: 5, blocked: 372 } }),
        });
      }
      if (pathname === '/api/papers/export/csv') {
        lastCsvParams = Object.fromEntries(url.searchParams.entries());
        return route.fulfill({
          status: 200,
          contentType: 'text/csv; charset=utf-8',
          headers: {
            'X-D3-Export-Safety-Gate': 'safe_verified_with_required_evidence',
            'X-D3-Export-Count': '0',
            'X-D3-Block-Count': '372',
            'X-D3-Base-Eligible-Count': '5',
            'X-D3-Profile-Included-Count': '0',
            'X-D3-Profile-Excluded-Count': '5',
            'X-D3-Dataset-Profile': url.searchParams.get('dataset_profile') || '',
          },
          body: 'header1,header2\n',
        });
      }
      if (pathname === '/api/papers/export/dft-dataset') {
        lastMlParams = Object.fromEntries(url.searchParams.entries());
        return route.fulfill({
          status: 200,
          contentType: 'application/json; charset=utf-8',
          body: JSON.stringify({
            metadata: {
              safety_gate: 'safe_verified_with_required_evidence',
              dataset_profile: url.searchParams.get('dataset_profile') || '',
              eligible_count: 5,
              base_eligible_count: 5,
              profile_candidate_count: 5,
              profile_included_count_before_limit: 0,
              profile_excluded_count: 5,
              profile_excluded_reasons: { excluded_insufficient_catalyst_scope: 5 },
              exported_count_after_limit: 0,
              limit: 100,
              blocked_count: 372,
              review_status_counts: { rejected: 5 },
            },
            records: [],
          }),
        });
      }
      throw new Error(`Unexpected/unmocked API call intercepted: ${pathname}`);
    });

    const baseUrl = process.env.TEST_BASE_URL || 'http://127.0.0.1:4173';
    await page.goto(`${baseUrl}/pages/dft_database/index.html`);

    // Wait for initial load
    await expect(page.locator('#datasetProfile')).toBeVisible();
    const initialCompareCount = compareCount;
    expect(initialCompareCount).toBeGreaterThan(0);

    // 1. Check label and title
    const label = page.locator('label[for="datasetProfile"]');
    await expect(label).toHaveText('导出数据范围');
    await expect(label).toHaveAttribute('title', '仅影响导出文件，不影响当前表格查询');

    // 2. Select dac_lis_ml: verify no /api/papers/compare or table reload occurs
    await page.selectOption('#datasetProfile', 'dac_lis_ml');
    await page.waitForTimeout(300);
    expect(compareCount).toBe(initialCompareCount);

    // 3. Test CSV Export with dac_lis_ml
    const csvButton = page.locator('button:has-text("导出 CSV")');
    await csvButton.click();
    await page.waitForTimeout(300);

    expect(lastCsvParams).not.toBeNull();
    expect(lastCsvParams.dataset_profile).toBe('dac_lis_ml');

    // CSV status verifies actual exported count (0) and profile exclusion (5)
    const exportStatus = page.locator('#exportSafetyStatus');
    await expect(exportStatus).toContainText('实际导出 0 条');
    await expect(exportStatus).toContainText('范围排除 5 条');
    await expect(exportStatus).toContainText('dac_lis_ml');

    // 4. Test ML Dataset Export with dac_lis_ml
    const mlButton = page.locator('button:has-text("导出 ML 数据集")');
    await mlButton.click();
    await page.waitForTimeout(300);

    expect(lastMlParams).not.toBeNull();
    expect(lastMlParams.dataset_profile).toBe('dac_lis_ml');

    // ML status verifies exported_count_after_limit (0), NOT eligible_count (5)
    const mlStatusText = await exportStatus.textContent();
    expect(mlStatusText).toContain('实际导出 0 条');
    expect(mlStatusText).toContain('范围排除 5 条');
    expect(mlStatusText).toContain('基础合格 5 条');
    expect(mlStatusText).not.toContain('可导出 5 条');

    // 5. Select sac_lis_ml: verify parameter changes to sac_lis_ml and no compare triggered
    await page.selectOption('#datasetProfile', 'sac_lis_ml');
    expect(compareCount).toBe(initialCompareCount);

    await csvButton.click();
    await page.waitForTimeout(300);
    expect(lastCsvParams.dataset_profile).toBe('sac_lis_ml');
  });
});
