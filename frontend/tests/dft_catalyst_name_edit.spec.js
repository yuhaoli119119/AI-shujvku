const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';
const SAMPLE_ID = '11111111-1111-4111-8111-111111111111';

function jsonResponse(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  });
}

function dftItem(id, activeSite, value) {
  return {
    id,
    catalyst_sample_id: SAMPLE_ID,
    catalyst: 'Wrong catalyst name',
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
    id: 'paper-name-edit',
    paper_id: 'paper-name-edit',
    title: 'Catalyst Name Identity Rekey Paper',
    year: 2026,
    journal: 'Journal of Atomic Renames',
    paper_type: 'research',
    library_name: 'Default Library',
    pdf_path: 'paper.pdf',
    workflow_status: 'Initial_Parsed',
    pdf_quality_status: 'A_text_readable',
    counts: { sections: 0, figures: 0, dft_results: 3, writing_cards: 0 },
  };
}

function paperDetail(renamed = false) {
  const items = [
    dftItem('dft-a1', 'site-a', -1.0),
    dftItem('dft-b1', 'site-b', -1.1),
    dftItem('dft-a2', 'site-a', -1.2),
  ];
  if (renamed) {
    items.forEach(item => {
      item.catalyst = 'Correct catalyst name';
    });
  }
  return {
    ...paperSummary(),
    abstract: 'Protected catalyst name correction frontend fixture.',
    sections: [],
    tables: [],
    figures: [],
    paper_notes: [],
    dft_settings_items: [],
    catalyst_samples_items: [{
      id: SAMPLE_ID,
      name: renamed ? 'Correct catalyst name' : 'Wrong catalyst name',
      catalyst_type: 'single_atom',
      metal_centers: ['Fe'],
      support: 'graphene',
      coordination: 'Fe-N4',
      synthesis_method: 'Original synthesis note',
    }],
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

async function installRoutes(page, onUpdate) {
  page.__renamed = false;
  page.__detailRequests = 0;
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (pathname === `/api/papers/paper-name-edit/catalyst-samples/${SAMPLE_ID}/basic-info`) {
      return onUpdate(route);
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
    if (pathname === '/api/papers/paper-name-edit') {
      page.__detailRequests += 1;
      return jsonResponse(route, paperDetail(Boolean(page.__renamed)));
    }
    if (pathname === '/api/papers/paper-name-edit/knowledge-context') {
      return jsonResponse(route, { candidates: [], metadata: {} });
    }
    if (pathname.endsWith('/reviews/audit') || pathname.endsWith('/evidence/locators')) {
      return jsonResponse(route, { items: [] });
    }
    return jsonResponse(route, {});
  });
}

async function openNameEditForm(page, groupIndex = 0) {
  const groups = page.locator(`#dftContent [data-role="dft-sample-group"][data-target-id="${SAMPLE_ID}"]`);
  await expect(groups).toHaveCount(2);
  const group = groups.nth(groupIndex);
  await group.locator(':scope > summary').click();
  const card = group.locator('.dft-catalyst-base-info');
  await card.locator(':scope > summary').getByRole('button', { name: '编辑基础信息' }).click();
  const form = card.locator('.dft-basic-info-form');
  await expect(form).toBeVisible();
  return { form, group, groups };
}

test('second active-site subgroup opens its own form and submits the complete protected Identity v2 request', async ({ page }) => {
  let submitted = null;
  await installRoutes(page, async route => {
    submitted = JSON.parse(route.request().postData() || '{}');
    page.__renamed = true;
    return jsonResponse(route, {
      catalyst_sample: { id: SAMPLE_ID, name: 'Correct catalyst name' },
      active_site_refresh: { active_site_status: 'refreshed' },
      name_change: {
        status: 'renamed',
        previous_name: 'Wrong catalyst name',
        current_name: 'Correct catalyst name',
        affected_dft_result_ids: ['dft-a1', 'dft-b1', 'dft-a2'],
        affected_dft_result_count: 3,
        requires_reverification: false,
        review_state_preserved: true,
        invalidated_review_ids: [],
        reverification_task_ids: [],
      },
    });
  });
  let confirmationText = '';
  page.on('dialog', dialog => {
    confirmationText = dialog.message();
    dialog.accept();
  });

  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-name-edit&tab=dft`);
  const initialDetailRequests = page.__detailRequests;
  const { form, group, groups } = await openNameEditForm(page, 1);
  const firstForm = groups.nth(0).locator('.dft-catalyst-base-info .dft-basic-info-form');
  await expect(group).toHaveAttribute('open', '');
  await expect(form).toBeVisible();
  await expect(firstForm).toBeHidden();
  await expect(form).toHaveAttribute('data-original-name', 'Wrong catalyst name');
  await expect(form).toHaveAttribute('data-catalyst-sample-id', SAMPLE_ID);
  await expect(form).toHaveAttribute('data-dft-result-ids', 'dft-a1,dft-b1,dft-a2');
  const confirmation = form.locator('[data-role="name-change-confirmation"]');
  await expect(confirmation).toBeHidden();

  await form.locator('[data-field="name"]').fill('Correct catalyst name');
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText('同步更新 3 条 DFT 数据的 Identity v2');
  await expect(confirmation).toContainText('已有核验状态和可导出状态保持不变');
  await expect(confirmation).toContainText('实际属于另一个催化剂');
  await expect(confirmation).toContainText('整组重新关联');
  await form.locator('[data-field="name_change_reason"]').fill('名称录入错误，现按原文修正。');
  await form.getByRole('button', { name: '保存基础信息' }).click();

  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted).toMatchObject({
    name: 'Correct catalyst name',
    confirm_name_change_with_dft: true,
    name_change_reason: '名称录入错误，现按原文修正。',
    expected_current_name: 'Wrong catalyst name',
    affected_dft_result_ids: ['dft-a1', 'dft-b1', 'dft-a2'],
    expected_dft_result_count: 3,
  });
  expect(confirmationText).toContain('仍是同一个催化剂样本');
  expect(confirmationText).toContain('仅修正名称');
  expect(confirmationText).toContain('Identity v2');
  expect(confirmationText).toContain('原核验状态和可导出状态保持不变');
  expect(confirmationText).toContain('整组重新关联');
  expect(confirmationText).not.toContain('重新进入待复核');
  await expect.poll(() => page.__detailRequests).toBeGreaterThan(initialDetailRequests);
  await expect(page.locator('#dftContent')).toContainText('Correct catalyst name');
});

test('empty reason and cancelled second confirmation do not submit or clear the form', async ({ page }) => {
  let requestCount = 0;
  await installRoutes(page, route => {
    requestCount += 1;
    return jsonResponse(route, {});
  });
  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-name-edit&tab=dft`);
  const { form } = await openNameEditForm(page);
  await form.locator('[data-field="name"]').fill('Correct catalyst name');
  await form.locator('[data-field="coordination"]').fill('Updated Fe-N4 note');
  await form.getByRole('button', { name: '保存基础信息' }).click();
  await expect(form.locator('[data-role="basic-info-error"]')).toContainText('请填写催化剂名称修改原因');
  expect(requestCount).toBe(0);

  await form.locator('[data-field="name_change_reason"]').fill('保留的名称修改原因。');
  page.once('dialog', dialog => dialog.dismiss());
  await form.getByRole('button', { name: '保存基础信息' }).click();
  expect(requestCount).toBe(0);
  await expect(form.locator('[data-field="name"]')).toHaveValue('Correct catalyst name');
  await expect(form.locator('[data-field="name_change_reason"]')).toHaveValue('保留的名称修改原因。');
  await expect(form.locator('[data-field="coordination"]')).toHaveValue('Updated Fe-N4 note');
});

test('409 and ordinary failures show backend detail and preserve every input', async ({ page }) => {
  let failureStatus = 409;
  await installRoutes(page, route => jsonResponse(route, {
    detail: failureStatus === 409
      ? 'write_conflict:catalyst_sample_name_already_exists:other-sample'
      : 'Injected ordinary backend failure',
  }, failureStatus));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-name-edit&tab=dft`);
  const { form } = await openNameEditForm(page);
  await form.locator('[data-field="name"]').fill('Correct catalyst name');
  await form.locator('[data-field="name_change_reason"]').fill('失败后仍需保留的原因。');
  await form.locator('[data-field="coordination"]').fill('Preserved coordination');
  await form.getByRole('button', { name: '保存基础信息' }).click();
  await expect(form.locator('[data-role="basic-info-error"]')).toContainText('catalyst_sample_name_already_exists');

  failureStatus = 500;
  await form.getByRole('button', { name: '保存基础信息' }).click();
  await expect(form.locator('[data-role="basic-info-error"]')).toContainText('Injected ordinary backend failure');
  await expect(form).toBeVisible();
  await expect(form.locator('[data-field="name"]')).toHaveValue('Correct catalyst name');
  await expect(form.locator('[data-field="name_change_reason"]')).toHaveValue('失败后仍需保留的原因。');
  await expect(form.locator('[data-field="coordination"]')).toHaveValue('Preserved coordination');
});

test('non-name edits and zero-DFT name edits keep the legacy request shape', async ({ page }) => {
  const submissions = [];
  await installRoutes(page, route => {
    submissions.push(JSON.parse(route.request().postData() || '{}'));
    return jsonResponse(route, { catalyst_sample: { id: SAMPLE_ID } });
  });
  await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=Default%20Library&paper_id=paper-name-edit&tab=dft`);
  let { form } = await openNameEditForm(page);
  await form.locator('[data-field="coordination"]').fill('Changed coordination only');
  await form.getByRole('button', { name: '保存基础信息' }).click();
  await expect.poll(() => submissions.length).toBe(1);
  expect(submissions[0].confirm_name_change_with_dft).toBeUndefined();
  expect(submissions[0].affected_dft_result_ids).toBeUndefined();

  ({ form } = await openNameEditForm(page));
  await form.evaluate(node => { node.dataset.dftResultIds = ''; });
  await form.locator('[data-field="name"]').fill('No DFT new name');
  await expect(form.locator('[data-role="name-change-confirmation"]')).toBeHidden();
  await form.getByRole('button', { name: '保存基础信息' }).click();
  await expect.poll(() => submissions.length).toBe(2);
  expect(submissions[1].name).toBe('No DFT new name');
  expect(submissions[1].confirm_name_change_with_dft).toBeUndefined();
  expect(submissions[1].name_change_reason).toBeUndefined();
});
