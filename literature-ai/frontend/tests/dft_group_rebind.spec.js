const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';
const SOURCE_ID = '11111111-1111-4111-8111-111111111111';
const TARGET_ID = '22222222-2222-4222-8222-222222222222';
const UNNAMED_TARGET_ID = '33333333-3333-4333-8333-333333333333';

function jsonResponse(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

function dftItem(id, sampleId, activeSite, value) {
  return {
    id,
    catalyst_sample_id: sampleId,
    catalyst: sampleId === SOURCE_ID ? 'Wrong catalyst 30' : 'Correct catalyst 15',
    active_site_instance_key: activeSite,
    adsorbate: 'Li2S',
    property_type: 'adsorption_energy',
    value,
    unit: 'eV',
    candidate_status: 'ML_Ready',
    export_safety: { is_exportable: true, eligible: true, blocked_reasons: [] },
  };
}

function paperSummary() {
  return {
    id: 'paper-rebind',
    paper_id: 'paper-rebind',
    title: 'DFT Group Rebind Paper',
    year: 2026,
    journal: 'Journal of Atomic Rebinds',
    paper_type: 'research',
    library_name: 'Default Library',
    pdf_path: 'paper.pdf',
    workflow_status: 'Initial_Parsed',
    pdf_quality_status: 'A_text_readable',
    counts: { sections: 0, figures: 0, dft_results: 4, writing_cards: 0 },
  };
}

function paperDetail(rebound = false) {
  const summary = paperSummary();
  const items = [
    dftItem('dft-a1', rebound ? TARGET_ID : SOURCE_ID, 'site-a', -1.0),
    dftItem('dft-b1', rebound ? TARGET_ID : SOURCE_ID, 'site-b', -1.1),
    dftItem('dft-a2', rebound ? TARGET_ID : SOURCE_ID, 'site-a', -1.2),
    dftItem('dft-target-existing', TARGET_ID, 'site-c', -1.3),
  ];
  return {
    ...summary,
    abstract: 'Atomic group rebind frontend fixture.',
    sections: [],
    tables: [],
    figures: [],
    paper_notes: [],
    dft_settings_items: [],
    catalyst_samples_items: [
      { id: SOURCE_ID, name: 'Wrong catalyst 30', catalyst_type: 'unknown', metal_centers: [] },
      { id: TARGET_ID, name: 'Correct catalyst 15', catalyst_type: 'unknown', metal_centers: [] },
      { id: UNNAMED_TARGET_ID, name: null, catalyst_type: 'unknown', metal_centers: [] },
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

async function installRoutes(page, onRebind) {
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (pathname === `/api/papers/paper-rebind/catalyst-samples/${SOURCE_ID}/rebind-dft-results`) {
      return onRebind(route);
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
    if (pathname === '/api/papers/paper-rebind') {
      return jsonResponse(route, paperDetail(Boolean(page.__rebound)));
    }
    if (pathname === '/api/papers/paper-rebind/knowledge-context') {
      return jsonResponse(route, { candidates: [], metadata: {} });
    }
    if (pathname.endsWith('/reviews/audit') || pathname.endsWith('/evidence/locators')) {
      return jsonResponse(route, { items: [] });
    }
    return jsonResponse(route, {});
  });
}

async function openFirstSourceRebindForm(page) {
  const sourceGroups = page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${SOURCE_ID}"]`);
  await expect(sourceGroups).toHaveCount(2);
  const group = sourceGroups.first();
  await group.locator(':scope > summary').click();
  const basicInfoCard = group.locator('.dft-catalyst-base-info');
  const basicInfoSummary = basicInfoCard.locator(':scope > summary');
  await expect(basicInfoCard).not.toHaveAttribute('open', '');
  await expect(basicInfoSummary.locator('button')).toHaveText(['整组重新关联', '编辑基础信息']);
  await expect(page.locator('.dft-group-rebind-panel')).toHaveCount(0);
  await expect(group.locator(':scope > .dft-sample-group-body > .dft-group-rebind-form')).toHaveCount(0);
  await basicInfoSummary.getByRole('button', { name: '整组重新关联' }).click();
  await expect(basicInfoCard).toHaveAttribute('open', '');
  const form = basicInfoCard.locator(':scope > .dft-group-rebind-form');
  await expect(form).toBeVisible();
  return form;
}

test('group rebind collects every active-site subgroup and removes the empty source group after refresh', async ({ page }) => {
  let submitted = null;
  await installRoutes(page, async route => {
    submitted = JSON.parse(route.request().postData() || '{}');
    page.__rebound = true;
    return jsonResponse(route, {
      status: 'rebound',
      source_sample_id: SOURCE_ID,
      target_sample_id: TARGET_ID,
      rebound_result_ids: ['dft-a1', 'dft-b1', 'dft-a2'],
      rebound_result_count: 3,
      requires_reverification: true,
      remaining_dft_result_count: 0,
    });
  });

  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-rebind&tab=dft`);
  const form = await openFirstSourceRebindForm(page);
  await expect(form).toContainText('3 条 DFT 数据');
  await expect(form.locator('option')).toHaveCount(2);
  await expect(form.locator('option')).toContainText(['请选择目标样本', 'Correct catalyst 15 · 22222222']);
  await expect(form.locator(`option[value="${UNNAMED_TARGET_ID}"]`)).toHaveCount(0);
  await form.locator('[data-field="target_sample_id"]').selectOption(TARGET_ID);
  await expect(form.locator('[data-role="selected-target-id"]')).toContainText(TARGET_ID);
  await form.locator('[data-field="reason"]').fill('来源样本命名错误，应整组改绑到样本 15。');
  await form.getByRole('button', { name: '确认整组重新关联' }).click();

  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted).toEqual({
    target_sample_id: TARGET_ID,
    dft_result_ids: ['dft-a1', 'dft-b1', 'dft-a2'],
    expected_result_count: 3,
    confirm_rebind: true,
    reason: '来源样本命名错误，应整组改绑到样本 15。',
    reviewer: 'literature_library_user',
  });
  await expect(page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${SOURCE_ID}"]`)).toHaveCount(0);
  await expect(page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${TARGET_ID}"]`)).toHaveCount(3);
});

test('group rebind preserves target and reason while showing 409 and ordinary backend details', async ({ page }) => {
  let failureStatus = 409;
  let requestCount = 0;
  await installRoutes(page, route => {
    requestCount += 1;
    const detail = failureStatus === 409
      ? 'Identity v2 observation_key conflict with an existing DFT result.'
      : 'The submitted DFT result count is stale.';
    return jsonResponse(route, { detail }, failureStatus);
  });

  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-rebind&tab=dft`);
  const form = await openFirstSourceRebindForm(page);
  await form.locator('[data-field="target_sample_id"]').selectOption(TARGET_ID);
  await form.locator('[data-field="reason"]').fill('保留这段失败后继续修正的原因。');
  await form.getByRole('button', { name: '确认整组重新关联' }).click();
  await expect(form.locator('[data-role="rebind-error"]')).toContainText('observation_key conflict');
  await expect(form.locator('[data-field="target_sample_id"]')).toHaveValue(TARGET_ID);
  await expect(form.locator('[data-field="reason"]')).toHaveValue('保留这段失败后继续修正的原因。');

  failureStatus = 400;
  await form.getByRole('button', { name: '确认整组重新关联' }).click();
  await expect(form.locator('[data-role="rebind-error"]')).toContainText('result count is stale');
  await expect(form.locator('[data-field="target_sample_id"]')).toHaveValue(TARGET_ID);
  await expect(form.locator('[data-field="reason"]')).toHaveValue('保留这段失败后继续修正的原因。');
  const basicInfoCard = form.locator('xpath=..');
  await form.getByRole('button', { name: '取消' }).click();
  await expect(form).toBeHidden();
  await expect(basicInfoCard).toHaveAttribute('open', '');
  await expect.poll(() => form.evaluate(node => node.offsetHeight)).toBe(0);
  await basicInfoCard.locator(':scope > summary').getByRole('button', { name: '整组重新关联' }).click();
  await expect(form).toBeVisible();
  await expect(form.locator('[data-field="target_sample_id"]')).toHaveValue(TARGET_ID);
  await expect(form.locator('[data-field="reason"]')).toHaveValue('保留这段失败后继续修正的原因。');
  expect(requestCount).toBe(2);
});
