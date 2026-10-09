const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';
const SOURCE_ID = '11111111-1111-4111-8111-111111111111';
const TARGET_ID = '22222222-2222-4222-8222-222222222222';
const UNNAMED_TARGET_ID = '33333333-3333-4333-8333-333333333333';
const EMPTY_SOURCE_ID = '44444444-4444-4444-8444-444444444444';
const LEGACY_EVIDENCE_NAME = 'Historical catalyst label no longer in system';
const HOSTILE_SITE_KEY = `safe');window.PWNED=1;//"\\site`;

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
    evidence_payload: { reported_material_identity: LEGACY_EVIDENCE_NAME },
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

function paperDetail(rebound = false, merged = false) {
  const summary = paperSummary();
  const movedToTarget = rebound || merged;
  const items = [
    dftItem('dft-a1', movedToTarget ? TARGET_ID : SOURCE_ID, HOSTILE_SITE_KEY, -1.0),
    dftItem('dft-b1', movedToTarget ? TARGET_ID : SOURCE_ID, 'site-b', -1.1),
    dftItem('dft-a2', movedToTarget ? TARGET_ID : SOURCE_ID, HOSTILE_SITE_KEY, -1.2),
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
      ...(!merged ? [
        { id: SOURCE_ID, name: 'Wrong catalyst 30', catalyst_type: 'unknown', metal_centers: [] },
        { id: EMPTY_SOURCE_ID, name: 'Retired renamed catalyst', catalyst_type: 'unknown', metal_centers: [] },
      ] : []),
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

async function installRoutes(page, onRebind, onMerge) {
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (pathname === `/api/papers/paper-rebind/catalyst-samples/${SOURCE_ID}/rebind-dft-results`) {
      return onRebind(route);
    }
    if (pathname.startsWith('/api/papers/paper-rebind/catalyst-samples/') && pathname.endsWith('/merge-duplicates')) {
      return onMerge ? onMerge(route) : jsonResponse(route, { detail: 'Unexpected merge request.' }, 500);
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
      return jsonResponse(route, paperDetail(Boolean(page.__rebound), Boolean(page.__merged)));
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
  await expect(basicInfoSummary.locator('button')).toHaveText(['复制 ID', '整组重新关联', '合并重复样本', '编辑基础信息']);
  await expect(page.locator('.dft-group-rebind-panel')).toHaveCount(0);
  await expect(group.locator(':scope > .dft-sample-group-body > .dft-group-rebind-form')).toHaveCount(0);
  await basicInfoSummary.getByRole('button', { name: '整组重新关联' }).click();
  await expect(basicInfoCard).toHaveAttribute('open', '');
  const form = basicInfoCard.locator(':scope > .dft-group-rebind-form:not(.dft-duplicate-merge-form)');
  await expect(form).toBeVisible();
  return form;
}

async function openTargetMergeForm(page) {
  const targetGroups = page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${TARGET_ID}"]`);
  await expect(targetGroups).toHaveCount(1);
  const group = targetGroups.first();
  await group.locator(':scope > summary').click();
  const basicInfoCard = group.locator('.dft-catalyst-base-info');
  const basicInfoSummary = basicInfoCard.locator(':scope > summary');
  await basicInfoSummary.getByRole('button', { name: '合并重复样本' }).click();
  await expect(basicInfoCard).toHaveAttribute('open', '');
  const form = basicInfoCard.locator(':scope > .dft-duplicate-merge-form');
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
  await expect(form).toContainText('恢复为 system_candidate');
  await expect(form).toContainText('重新进入待复核');
  await expect(form.locator('option')).toHaveCount(2);
  await expect(form.locator('option')).toContainText([
    '请选择目标样本',
    'Correct catalyst 15 · 22222222',
  ]);
  await expect(form.locator(`option[value="${EMPTY_SOURCE_ID}"]`)).toHaveCount(0);
  await expect(form.locator(`option[value="${UNNAMED_TARGET_ID}"]`)).toHaveCount(0);
  await expect(form.locator('option')).not.toContainText([LEGACY_EVIDENCE_NAME]);
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

test('duplicate merge submits every selected source including an empty sample and removes deleted candidates after refresh', async ({ page }) => {
  let submitted = null;
  await installRoutes(page, route => jsonResponse(route, { detail: 'Unexpected rebind request.' }, 500), async route => {
    submitted = JSON.parse(route.request().postData() || '{}');
    page.__merged = true;
    return jsonResponse(route, {
      status: 'merged',
      target_sample_id: TARGET_ID,
      merged_source_sample_ids: [SOURCE_ID, EMPTY_SOURCE_ID],
      moved_dft_result_ids: ['dft-a1', 'dft-b1', 'dft-a2'],
      moved_dft_result_count: 3,
      deleted_source_sample_ids: [SOURCE_ID, EMPTY_SOURCE_ID],
      requires_reverification: false,
      review_state_preserved: true,
      invalidated_review_ids: [],
      reverification_task_ids: [],
    });
  });

  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-rebind&tab=dft`);
  const form = await openTargetMergeForm(page);
  const options = form.locator('[data-role="merge-source-option"]');
  await expect(options).toHaveCount(2);
  await expect(options).toContainText([
    'Retired renamed catalyst · 44444444',
    'Wrong catalyst 30 · 11111111',
  ]);
  await expect(options).toContainText(['0 条 DFT', '3 条 DFT']);
  await expect(form).not.toContainText(LEGACY_EVIDENCE_NAME);
  await expect(form).not.toContainText(UNNAMED_TARGET_ID);
  await form.locator(`[data-source-sample-id="${EMPTY_SOURCE_ID}"]`).check();
  await form.locator(`[data-source-sample-id="${SOURCE_ID}"]`).check();
  await expect(form.locator('[data-role="merge-selection-summary"]')).toHaveText('已选择 2 个重复样本，共 3 条 DFT。');
  await form.locator('[data-field="reason"]').fill('两个记录均为吸附物差异造成的同一物理催化剂误拆。');
  page.once('dialog', async dialog => {
    expect(dialog.message()).toContain('确实是同一个物理催化剂');
    await dialog.accept();
  });
  await form.getByRole('button', { name: '确认合并重复样本' }).click();

  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted).toEqual({
    expected_target_name: 'Correct catalyst 15',
    sources: [
      {
        source_sample_id: EMPTY_SOURCE_ID,
        expected_current_name: 'Retired renamed catalyst',
        dft_result_ids: [],
        expected_dft_result_count: 0,
      },
      {
        source_sample_id: SOURCE_ID,
        expected_current_name: 'Wrong catalyst 30',
        dft_result_ids: ['dft-a1', 'dft-b1', 'dft-a2'],
        expected_dft_result_count: 3,
      },
    ],
    confirm_same_physical_catalyst: true,
    reason: '两个记录均为吸附物差异造成的同一物理催化剂误拆。',
    reviewer: 'literature_library_user',
  });
  await expect(page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${SOURCE_ID}"]`)).toHaveCount(0);
  await expect(page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${TARGET_ID}"]`)).toHaveCount(3);

  const refreshedMergeForms = page.locator('.dft-duplicate-merge-form');
  await expect(refreshedMergeForms).toHaveCount(3);
  await expect(refreshedMergeForms.first().locator('[data-role="merge-source-option"]')).toHaveCount(0);
  await expect(refreshedMergeForms.first()).not.toContainText('Wrong catalyst 30');
  await expect(refreshedMergeForms.first()).not.toContainText(SOURCE_ID);
  await expect(refreshedMergeForms.first()).not.toContainText('Retired renamed catalyst');
  await expect(refreshedMergeForms.first()).not.toContainText(EMPTY_SOURCE_ID);
  await expect(refreshedMergeForms.first()).not.toContainText(LEGACY_EVIDENCE_NAME);
  const refreshedRebindOptions = page.locator('.dft-group-rebind-form:not(.dft-duplicate-merge-form) option');
  const refreshedRebindText = (await refreshedRebindOptions.allTextContents()).join('\n');
  expect(refreshedRebindText).not.toContain('Wrong catalyst 30');
  expect(refreshedRebindText).not.toContain('Retired renamed catalyst');
  expect(refreshedRebindText).not.toContain(LEGACY_EVIDENCE_NAME);
});

test('duplicate merge preserves selection and reason after 409 and ordinary errors', async ({ page }) => {
  let failureStatus = 409;
  let requestCount = 0;
  await installRoutes(page, route => jsonResponse(route, { detail: 'Unexpected rebind request.' }, 500), route => {
    requestCount += 1;
    const detail = failureStatus === 409
      ? 'Catalyst sample descriptors conflict and cannot be merged.'
      : 'The submitted source sample snapshot is stale.';
    return jsonResponse(route, { detail }, failureStatus);
  });
  page.on('dialog', dialog => dialog.accept());

  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-rebind&tab=dft`);
  const form = await openTargetMergeForm(page);
  await form.locator(`[data-source-sample-id="${SOURCE_ID}"]`).check();
  await form.locator('[data-field="reason"]').fill('失败后必须保留这段原因和源样本选择。');
  await form.getByRole('button', { name: '确认合并重复样本' }).click();
  await expect(form.locator('[data-role="merge-error"]')).toContainText('descriptors conflict');
  await expect(form.locator(`[data-source-sample-id="${SOURCE_ID}"]`)).toBeChecked();
  await expect(form.locator('[data-field="reason"]')).toHaveValue('失败后必须保留这段原因和源样本选择。');

  failureStatus = 400;
  await form.getByRole('button', { name: '确认合并重复样本' }).click();
  await expect(form.locator('[data-role="merge-error"]')).toContainText('snapshot is stale');
  await expect(form.locator(`[data-source-sample-id="${SOURCE_ID}"]`)).toBeChecked();
  await expect(form.locator('[data-field="reason"]')).toHaveValue('失败后必须保留这段原因和源样本选择。');
  const basicInfoCard = form.locator('xpath=..');
  await form.getByRole('button', { name: '取消' }).click();
  await expect(form).toBeHidden();
  await basicInfoCard.locator(':scope > summary').getByRole('button', { name: '合并重复样本' }).click();
  await expect(form).toBeVisible();
  await expect(form.locator(`[data-source-sample-id="${SOURCE_ID}"]`)).toBeChecked();
  await expect(form.locator('[data-field="reason"]')).toHaveValue('失败后必须保留这段原因和源样本选择。');
  expect(requestCount).toBe(2);
});

test('hostile active-site keys stay inert across rebind merge and basic-info inline handlers', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.addInitScript(() => {
    window.PWNED = 0;
  });
  await installRoutes(
    page,
    route => jsonResponse(route, { detail: 'Expected rebind rejection for handler safety test.' }, 409),
    route => jsonResponse(route, { detail: 'Expected merge rejection for handler safety test.' }, 409),
  );
  page.on('dialog', dialog => dialog.accept());

  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-rebind&tab=dft`);
  const rebindForm = await openFirstSourceRebindForm(page);
  const hostileGroup = rebindForm.locator('xpath=ancestor::*[@data-role="dft-sample-group"]');
  await expect(hostileGroup).toHaveAttribute('data-dft-sample-key', `${SOURCE_ID}|${HOSTILE_SITE_KEY}`);
  await expect.poll(() => page.evaluate(() => window.PWNED)).toBe(0);

  await rebindForm.locator('[data-field="target_sample_id"]').selectOption(TARGET_ID);
  await rebindForm.locator('[data-field="reason"]').fill('验证恶意活性位点键不会执行。');
  await rebindForm.getByRole('button', { name: '确认整组重新关联' }).click();
  await expect(rebindForm.locator('[data-role="rebind-error"]')).toContainText('handler safety test');
  await expect.poll(() => page.evaluate(() => window.PWNED)).toBe(0);
  await rebindForm.getByRole('button', { name: '取消' }).click();

  const basicInfoCard = rebindForm.locator('xpath=..');
  const basicInfoSummary = basicInfoCard.locator(':scope > summary');
  await basicInfoSummary.getByRole('button', { name: '合并重复样本' }).click();
  const mergeForm = basicInfoCard.locator(':scope > .dft-duplicate-merge-form');
  await expect(mergeForm).toBeVisible();
  await mergeForm.locator(`[data-source-sample-id="${TARGET_ID}"]`).check();
  await mergeForm.locator('[data-field="reason"]').fill('验证合并事件参数安全。');
  await mergeForm.getByRole('button', { name: '确认合并重复样本' }).click();
  await expect(mergeForm.locator('[data-role="merge-error"]')).toContainText('handler safety test');
  await expect.poll(() => page.evaluate(() => window.PWNED)).toBe(0);
  await mergeForm.getByRole('button', { name: '取消' }).click();

  await basicInfoSummary.getByRole('button', { name: '编辑基础信息' }).click();
  const basicInfoForm = basicInfoCard.locator(':scope > .dft-basic-info-form');
  await expect(basicInfoForm).toBeVisible();
  await basicInfoForm.locator('[data-field="name"]').fill('Wrong catalyst renamed safely');
  await expect(basicInfoForm.locator('[data-role="name-change-confirmation"]')).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.PWNED)).toBe(0);
  await basicInfoForm.getByRole('button', { name: '取消' }).click();

  expect(pageErrors).toEqual([]);
  expect(await page.evaluate(() => window.PWNED)).toBe(0);
});
