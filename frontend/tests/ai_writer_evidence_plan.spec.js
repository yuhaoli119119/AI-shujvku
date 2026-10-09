const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const FRONTEND_ROOT = path.resolve(__dirname, '..');

function read(relativePath) {
  return fs.readFileSync(path.join(FRONTEND_ROOT, relativePath), 'utf8');
}

test('AI writer source is a bounded read-only evidence plan surface', () => {
  const html = read('pages/ai_writer/index.html');
  const page = read('pages/ai_writer/page.js');
  const combined = `${html}\n${page}`;

  expect(html).toContain('本地 AI 写作证据计划');
  expect(html).toContain('网页不生成论文草稿');
  expect(html).toContain('<option value="narrative" selected>');
  expect(html).toContain('id="evidenceBudget" type="number" value="24"');
  expect(html).toContain('id="batchSize" type="number" value="10"');
  expect(html).toContain('id="maxPerPaper" type="number" value="3"');
  expect(combined).toContain('/api/content-knowledge/writing-plan');
  expect(combined).not.toContain('generateAcademicDraft');
  expect(combined).not.toContain('生成草稿');
  expect(page).toContain('requested_sections: includeDft ? ["dft_results"] : []');
  expect(page).toContain('覆盖不完整：不得声称系统性、全面或穷尽性覆盖');
  expect(page).toContain('无证据支持');
  expect(page).toContain('不要补写事实或数字');
  expect(page).toContain('可引用');
  expect(page).toContain('仅用于写作，不可直接引用');
  expect(combined).not.toContain('clipboard.writeText');
});

test('AI writer posts safe defaults and displays bounded evidence batches', async ({ page }) => {
  const paperA = '00000000-0000-0000-0000-000000000101';
  const paperB = '00000000-0000-0000-0000-000000000102';
  let requestPayload = null;

  await page.route('**/api/papers?limit=200', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        { id: paperA, paper_code: 'B0101', title: 'Paper A' },
        { id: paperB, paper_code: 'B0102', title: 'Paper B' },
      ]),
    });
  });
  await page.route('**/api/content-knowledge/writing-plan', async (route) => {
    requestPayload = route.request().postDataJSON();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        plan_fingerprint: 'stable',
        query: requestPayload.query,
        retrieval_mode: 'narrative',
        selected_evidence_types: ['writing_cards'],
        dft_included: false,
        dft_included_reason: 'not_requested',
        requested_paper_count: 2,
        valid_paper_count: 2,
        represented_paper_count: 1,
        budgets: { evidence_budget: 24, used: 2, remaining: 22 },
        coverage: {
          coverage_complete: false,
          by_paper: [
            { paper_id: paperA, paper_code: 'B0101', status: 'represented' },
            { paper_id: paperB, paper_code: 'B0102', status: 'budget_exhausted' },
          ],
        },
        warnings: [],
        batches: [
          {
            batch_id: 'batch-001',
            paper_ids: [paperA],
            paper_codes: ['B0101'],
            selected_evidence_ids: ['evidence-a'],
            budget: { used: 1 },
          },
          {
            batch_id: 'batch-002',
            paper_ids: [paperB],
            paper_codes: ['B0102'],
            selected_evidence_ids: ['evidence-b'],
            budget: { used: 1 },
          },
        ],
        batch_prompt_contexts: [
          {
            batch_id: 'batch-001',
            paper_ids: [paperA],
            evidence_cards: [{ evidence_id: 'evidence-a', excerpt: 'ONLY-A' }],
            full_text_included: false,
          },
          {
            batch_id: 'batch-002',
            paper_ids: [paperB],
            evidence_cards: [{ evidence_id: 'evidence-b', excerpt: 'ONLY-B' }],
            full_text_included: false,
          },
        ],
        selected_evidence: [],
        database_writes: false,
      }),
    });
  });

  await page.goto('http://127.0.0.1:4173/pages/ai_writer/index.html');
  await page.locator('input[data-paper-id]').first().check();
  await page.locator('input[data-paper-id]').nth(1).check();
  await page.locator('#writingTopic').fill('ordinary narrative');
  await page.locator('#buildPlanInlineBtn').click();

  await expect(page.locator('#coverageBadge')).toHaveText('覆盖不完整');
  await expect(page.locator('#dftStatus')).toContainText('DFT 未启用 / 未检索');
  await expect(page.locator('#warnings')).toContainText('不得声称系统性、全面或穷尽性覆盖');
  expect(requestPayload).toMatchObject({
    query: 'ordinary narrative',
    mode: 'narrative',
    requested_sections: [],
    evidence_budget: 24,
    batch_size: 10,
    max_evidence_per_paper: 3,
    max_sources_per_claim: 5,
  });

  await expect(page.locator('.batch-card')).toHaveCount(2);
  await expect(page.locator('.batch-card').first()).toContainText('evidence-a');
  await expect(page.locator('.batch-card').nth(1)).toContainText('evidence-b');
});
