const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';

function makeReport(overrides = {}) {
  return {
    schema_version: 'dft_audit_report_v1',
    filters: { paper_id: 'paper-1', days: 30, include_closed: false, start_at: '2026-06-01T00:00:00' },
    issue_status_counts: { needs_primary_ai: 1, needs_user_decision: 1, fixed_by_primary_ai: 1 },
    issue_type_counts: { missing_dft_result: 1, wrong_value: 2 },
    open_needs_primary_ai_count: 1,
    open_needs_user_decision_count: 1,
    fixed_by_primary_ai_pending_review_count: 1,
    repair_action_counts: { create_missing_dft: 1 },
    repair_actor_counts: [
      { source_prefix: 'dft_primary_repair', actor_role: 'primary_ai_repair', capability_used: 'repair_dft_issues', count: 1 },
    ],
    repair_issue_type_counts: { missing_dft_result: 1 },
    repair_writes_final_truth_count: 0,
    suspect_repair_actor_warnings: [],
    mcp_capability_warnings: [],
    ...overrides,
  };
}

function makeIssue(overrides = {}) {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    issue_type: 'missing_dft_result',
    status: 'needs_primary_ai',
    severity: 'high',
    target_type: 'dft_results',
    target_id: 'new',
    current_snapshot: null,
    suggested_value: null,
    suggested_dft: {
      material_identity: 'Fe-GDY',
      property_type: 'adsorption_energy',
      adsorbate: 'Li2S4',
      value: -1.1,
      unit: 'eV',
    },
    evidence_payload: {
      source_document_type: 'main_text',
      page: 5,
      table: 'Table 1',
      quoted_text: 'Fe-GDY Li2S4 adsorption energy is -1.10 eV.',
    },
    source_count: 2,
    source_candidate_ids: ['candidate-1', 'candidate-2'],
    created_at: '2026-06-28T10:00:00',
    updated_at: '2026-06-28T11:00:00',
    ...overrides,
  };
}

async function mockApis(page, { issues = [makeIssue()], report = makeReport() } = {}) {
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route(/\/api\/dft\/audit-issues.*/, route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ count: issues.length, items: issues, filters: {} }),
    });
  });
  await page.route(/\/api\/dft\/audit-report.*/, route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(report),
    });
  });
}

test('renders DFT audit report summary and issue list', async ({ page }) => {
  await mockApis(page);
  await page.goto(`${BASE_URL}/pages/dft_audit_center/index.html?paper_id=paper-1`);

  await expect(page.getByRole('heading', { name: 'DFT 核验中心' })).toBeVisible();
  await expect(page.locator('#reportSummary')).toContainText('待主 AI');
  await expect(page.locator('#reportSummary')).toContainText('主 AI 修复待复核');
  await expect(page.locator('#reportSummary')).toContainText('create_missing_dft');
  await expect(page.locator('#issueList')).toContainText('missing_dft_result');
  await expect(page.locator('#issueList')).toContainText('Fe-GDY');
  await expect(page.locator('#issueList')).toContainText('Table 1');
  await expect(page.locator('#issueList')).toContainText('sources 2');
});

test('copies serializer ids with real newlines and keeps API access readonly', async ({ page }) => {
  const requests = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) {
      requests.push({ method: request.method(), url: request.url() });
    }
  });
  const issues = [
    makeIssue(),
    makeIssue({
      id: '22222222-2222-4222-8222-222222222222',
      issue_type: 'wrong_value',
      target_id: 'result-2',
    }),
  ];
  await page.addInitScript(() => {
    window.__clipboardText = '';
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async text => {
          window.__clipboardText = text;
        },
      },
    });
  });
  await mockApis(page, { issues });
  await page.goto(`${BASE_URL}/pages/dft_audit_center/index.html`);

  await page.getByRole('button', { name: '复制 issue_id' }).first().click();
  await expect.poll(() => page.evaluate(() => window.__clipboardText))
    .toBe('11111111-1111-4111-8111-111111111111');

  await page.getByRole('button', { name: '复制主 AI 处理提示' }).click();
  const queueText = await page.evaluate(() => window.__clipboardText);
  expect(queueText).toContain('11111111-1111-4111-8111-111111111111');
  expect(queueText).toContain('22222222-2222-4222-8222-222222222222');
  expect(queueText).toContain('\n');
  expect(queueText).not.toContain('\\n');
  expect(queueText.split('\n')).toContain('11111111-1111-4111-8111-111111111111');
  expect(queueText.split('\n')).toContain('22222222-2222-4222-8222-222222222222');

  expect(requests).toHaveLength(2);
  expect(requests.every(request => request.method === 'GET')).toBe(true);
  expect(requests.map(request => new URL(request.url).pathname).sort()).toEqual([
    '/api/dft/audit-issues',
    '/api/dft/audit-report',
  ]);
  expect(requests.some(request => request.url.includes('repair_dft_audit_issue'))).toBe(false);
});

test('issue type filter covers the complete backend issue type contract', async ({ page }) => {
  await mockApis(page);
  await page.goto(`${BASE_URL}/pages/dft_audit_center/index.html`);

  const optionValues = await page.locator('#issueTypeFilter option').evaluateAll(
    options => options.map(option => option.value).filter(Boolean),
  );
  expect(optionValues.sort()).toEqual([
    'consensus_ready',
    'duplicate_suspected',
    'missing_dft_result',
    'missing_evidence',
    'negative_consensus',
    'source_scope_error',
    'uncertain',
    'wrong_adsorbate',
    'wrong_material',
    'wrong_property_type',
    'wrong_reaction_step',
    'wrong_unit',
    'wrong_value',
  ].sort());
});

test('renders suspect and MCP warnings without write-action buttons', async ({ page }) => {
  await mockApis(page, {
    report: makeReport({
      suspect_repair_actor_warnings: [
        {
          code: 'unexpected_repair_capability',
          message: 'repair_dft_audit_issue should use repair_dft_issues capability',
          source_prefix: 'assigned_dft_audit',
          actor_role: 'dft_auditor',
          capability_used: 'review_dft',
        },
      ],
      mcp_capability_warnings: [
        {
          code: 'repair_dft_issues_non_primary_repair_key',
          message: 'repair_dft_issues should only be assigned to a DFT primary repair AI key',
          source_prefix: 'admin',
          display_name: 'Admin',
          capability: 'repair_dft_issues',
        },
      ],
    }),
  });
  await page.goto(`${BASE_URL}/pages/dft_audit_center/index.html`);

  await expect(page.locator('#warningList')).toContainText('unexpected_repair_capability');
  await expect(page.locator('#warningList')).toContainText('repair_dft_issues_non_primary_repair_key');
  await expect(page.locator('body')).not.toContainText('接受 AI 裁定');
  await expect(page.locator('body')).not.toContainText('批量修复');
  await expect(page.locator('body')).not.toContainText('都不采用');
});

test('renders empty issue state and applies readonly filters', async ({ page }) => {
  let issueUrl = '';
  let reportUrl = '';
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route(/\/api\/dft\/audit-issues.*/, route => {
    issueUrl = route.request().url();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ count: 0, items: [], filters: {} }),
    });
  });
  await page.route(/\/api\/dft\/audit-report.*/, route => {
    reportUrl = route.request().url();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(makeReport({
        issue_status_counts: {},
        issue_type_counts: {},
        open_needs_primary_ai_count: 0,
        open_needs_user_decision_count: 0,
        fixed_by_primary_ai_pending_review_count: 0,
        repair_action_counts: {},
        repair_actor_counts: [],
        repair_issue_type_counts: {},
      })),
    });
  });

  await page.goto(`${BASE_URL}/pages/dft_audit_center/index.html?paper_id=paper-1&status=needs_user_decision&issue_type=wrong_value&days=7&include_closed=true`);

  await expect(page.locator('#issueList')).toContainText('当前没有待处理 DFT audit issue');
  await expect.poll(() => issueUrl).toContain('paper_id=paper-1');
  await expect.poll(() => issueUrl).toContain('status=needs_user_decision');
  await expect.poll(() => issueUrl).toContain('include_closed=true');
  await expect.poll(() => reportUrl).toContain('days=7');
  await expect.poll(() => reportUrl).toContain('include_closed=true');
});
