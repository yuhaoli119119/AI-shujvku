const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';
const PAPER_ID = 'paper-dft-uuid-reject';
const SAMPLE_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const OTHER_SAMPLE_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const RESULT_A = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const RESULT_B = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const PREPENDED_RESULT = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';

function jsonResponse(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

function dftItem(id, value, status = 'ai_verified_ml_ready', sampleId = SAMPLE_ID, catalyst = 'UUID-bound catalyst') {
  return {
    id,
    catalyst_sample_id: sampleId,
    catalyst,
    active_site_instance_key: 'site-a',
    adsorbate: 'Li2S',
    property_type: 'adsorption_energy',
    value,
    unit: 'eV',
    candidate_status: status,
    export_safety: { is_exportable: true, eligible: true, blocked_reasons: [] },
  };
}

function paperSummary() {
  return {
    id: PAPER_ID,
    paper_id: PAPER_ID,
    title: 'DFT UUID Reject Fixture',
    year: 2026,
    journal: 'Journal of Stable Identifiers',
    paper_type: 'research',
    library_name: 'Default Library',
    pdf_path: 'paper.pdf',
    workflow_status: 'Initial_Parsed',
    pdf_quality_status: 'A_text_readable',
    counts: { sections: 0, figures: 0, dft_results: 3, writing_cards: 0 },
  };
}

function paperDetail({ shifted, rejected }) {
  const items = [
    dftItem(RESULT_A, -1.0),
    dftItem(RESULT_B, -1.1, rejected ? 'rejected' : 'ai_verified_ml_ready'),
  ];
  const otherSampleItem = dftItem(
    PREPENDED_RESULT,
    -0.9,
    'ai_verified_ml_ready',
    OTHER_SAMPLE_ID,
    'Earlier display-only catalyst',
  );
  if (shifted) items.unshift(otherSampleItem);
  else items.push(otherSampleItem);
  return {
    ...paperSummary(),
    abstract: 'Fixture for UUID-targeted DFT card actions.',
    sections: [],
    tables: [],
    figures: [],
    paper_notes: [],
    dft_settings_items: [],
    catalyst_samples_items: [
      { id: SAMPLE_ID, name: 'UUID-bound catalyst', catalyst_type: 'unknown', metal_centers: [] },
      { id: OTHER_SAMPLE_ID, name: 'Earlier display-only catalyst', catalyst_type: 'unknown', metal_centers: [] },
    ],
    dft_results_items: items,
    dft_results_page: { offset: 0, limit: 100, returned: items.length, total: items.length, has_more: false },
    electrochemical_performance_items: [],
    mechanism_claims_items: [],
    writing_cards_items: [],
    outgoing_relationships: [],
    incoming_relationships: [],
    references: [],
  };
}

test('catalyst group and terminal DFT keep their UUIDs when display ordinals change', async ({ page }) => {
  let shifted = false;
  let rejected = false;
  let rejectRequest = null;
  let detailRequests = 0;
  await page.addInitScript(() => {
    window.copiedCatalystSampleId = null;
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async value => { window.copiedCatalystSampleId = value; },
      },
    });
  });
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === `/api/papers/${PAPER_ID}/dft-results/${RESULT_B}/reject`) {
      rejectRequest = {
        method: request.method(),
        body: JSON.parse(request.postData() || '{}'),
      };
      rejected = true;
      return jsonResponse(route, { status: 'rejected' });
    }
    if (pathname === '/api/libraries') {
      return jsonResponse(route, [{ name: 'Default Library', is_active: true, root_path: '/libraries/default', paper_count: 1 }]);
    }
    if (pathname === '/api/papers/libraries') {
      return jsonResponse(route, [{ name: 'Default Library', paper_count: 1 }]);
    }
    if ((pathname === '/api/papers' || pathname === '/api/papers/') && request.method() === 'GET') {
      return jsonResponse(route, [paperSummary()]);
    }
    if (pathname === `/api/papers/${PAPER_ID}`) {
      detailRequests += 1;
      return jsonResponse(route, paperDetail({ shifted, rejected }));
    }
    if (pathname === `/api/papers/${PAPER_ID}/knowledge-context`) {
      return jsonResponse(route, { candidates: [], metadata: {} });
    }
    if (pathname.endsWith('/reviews/audit') || pathname.endsWith('/evidence/locators')) {
      return jsonResponse(route, { items: [] });
    }
    return jsonResponse(route, {});
  });

  const pageUrl = `${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=${PAPER_ID}&tab=dft`;
  await page.goto(pageUrl);
  const sampleGroup = page.locator(`[data-role="dft-sample-group"][data-catalyst-sample-id="${SAMPLE_ID}"]`);
  await expect(sampleGroup).toHaveCount(1);
  await expect(sampleGroup.locator(':scope > summary h3')).toContainText('催化剂样本 1');
  const sampleLocator = sampleGroup.locator(`[data-role="catalyst-sample-id"][data-catalyst-sample-id="${SAMPLE_ID}"]`);
  await expect(sampleLocator).toHaveCount(1);
  await expect(sampleLocator).not.toContainText('系统绑定 ID');
  await expect(sampleLocator).toContainText(SAMPLE_ID.slice(0, 8));
  const headingRow = sampleGroup.locator(':scope > summary .dft-sample-heading-row');
  const identityLabel = headingRow.locator('.dft-sample-identity-label');
  await expect(headingRow).toHaveCount(1);
  expect(await headingRow.evaluate(element => getComputedStyle(element).display)).toBe('flex');
  expect(await headingRow.evaluate(element => getComputedStyle(element).flexWrap)).toBe('nowrap');
  const inlineOffsets = await headingRow.evaluate(element => {
    const heading = element.querySelector('h3');
    const locator = element.querySelector('[data-role="catalyst-sample-id"]');
    const identity = element.querySelector('.dft-sample-identity-label');
    const headingBox = heading.getBoundingClientRect();
    const locatorBox = locator.getBoundingClientRect();
    const identityBox = identity.getBoundingClientRect();
    const center = box => box.top + box.height / 2;
    return [
      Math.abs(center(headingBox) - center(locatorBox)),
      Math.abs(center(headingBox) - center(identityBox)),
    ];
  });
  expect(Math.max(...inlineOffsets)).toBeLessThanOrEqual(1);
  await expect(identityLabel).toContainText('UUID-bound catalyst');
  await expect(sampleGroup.locator(':scope > summary [data-role="copy-catalyst-sample-id"]')).toHaveCount(0);
  await sampleGroup.locator(':scope > summary').click();
  const copySampleIdButton = sampleGroup.locator('.dft-catalyst-base-info [data-role="copy-catalyst-sample-id"]');
  await expect(copySampleIdButton).toHaveText('复制 ID');
  const baseActionButtons = sampleGroup.locator('.dft-catalyst-base-info .dft-catalyst-base-actions .btn');
  const baseActionHeights = await baseActionButtons.evaluateAll(elements => elements.map(element => getComputedStyle(element).height));
  const baseActionWidths = await baseActionButtons.evaluateAll(elements => elements.map(element => getComputedStyle(element).width));
  expect(new Set(baseActionHeights).size).toBe(1);
  expect(new Set(baseActionWidths).size).toBe(1);
  await copySampleIdButton.click();
  await expect.poll(() => page.evaluate(() => window.copiedCatalystSampleId)).toBe(SAMPLE_ID);

  const initialTargetCard = sampleGroup.locator(`.dft-compact-card[data-dft-result-id="${RESULT_B}"]`);
  await expect(initialTargetCard).toHaveCount(1);
  await expect(initialTargetCard.locator('[data-role="dft-record-number"]')).toHaveText('DFT #2');

  shifted = true;
  await page.goto(pageUrl);
  const shiftedSampleGroup = page.locator(`[data-role="dft-sample-group"][data-catalyst-sample-id="${SAMPLE_ID}"]`);
  await expect(shiftedSampleGroup.locator(':scope > summary h3')).toContainText('催化剂样本 2');
  await shiftedSampleGroup.locator(':scope > summary').click();
  const shiftedTargetCard = shiftedSampleGroup.locator(`.dft-compact-card[data-dft-result-id="${RESULT_B}"]`);
  await expect(shiftedTargetCard).toHaveCount(1);
  await expect(shiftedTargetCard.locator('[data-role="dft-record-number"]')).toHaveText('DFT #3');
  const dftActions = shiftedTargetCard.locator('.dft-compact-actions');
  await expect(dftActions.locator('.btn')).toHaveCount(4);
  expect(await dftActions.evaluate(element => getComputedStyle(element).flexDirection)).toBe('row');
  expect(await dftActions.evaluate(element => getComputedStyle(element).flexWrap)).toBe('nowrap');
  const dftActionCenterOffsets = await dftActions.locator('.btn').evaluateAll(elements => {
    const centers = elements.map(element => {
      const box = element.getBoundingClientRect();
      return box.top + box.height / 2;
    });
    return centers.map(center => Math.abs(center - centers[0]));
  });
  expect(Math.max(...dftActionCenterOffsets)).toBeLessThanOrEqual(1);

  const detailRequestsBeforeReject = detailRequests;
  page.once('dialog', async dialog => {
    expect(dialog.message()).toContain(`目标 DFT UUID：${RESULT_B}`);
    expect(dialog.message()).toContain('当前状态：ai_verified_ml_ready');
    expect(dialog.message()).toContain('不会物理删除这条 DFT 数据，只会阻止导出');
    await dialog.accept();
  });
  await shiftedTargetCard.locator(`[data-role="reject-dft-result"][data-dft-result-id="${RESULT_B}"]`).click();

  await expect.poll(() => rejectRequest).not.toBeNull();
  expect(rejectRequest).toEqual({
    method: 'POST',
    body: {
      confirm_reject_candidate: true,
      reviewer: 'literature_library_dft',
      reviewer_note: 'Rejected from the Literature Library DFT panel.',
    },
  });
  await expect.poll(() => detailRequests).toBeGreaterThan(detailRequestsBeforeReject);
  await expect(page.locator(`[data-role="reject-dft-result"][data-dft-result-id="${RESULT_B}"]`)).toHaveCount(0);
});
