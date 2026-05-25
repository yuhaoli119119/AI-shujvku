const { test, expect } = require('@playwright/test');

const PAGES = [
  { name: 'Dashboard', path: '/pages/dashboard/index.html', coreSelector: '.panel-card' },
  { name: 'Ingestion Center', path: '/pages/ingestion/index.html', coreSelector: '.dropzone' },
  { name: 'Literature Library', path: '/pages/literature_library/index.html', coreSelector: '#paperList' },
  { name: 'Paper Detail', path: '/pages/paper_detail/index.html', coreSelector: '.panel-card' },
  { name: 'DFT Database', path: '/pages/dft_database/index.html', coreSelector: '#dftTable' },
  { name: 'AI Writing Studio', path: '/pages/ai_writer/index.html', coreSelector: '#paperChecklist' },
  { name: 'Extraction Review Workbench', path: '/pages/external_analysis_workbench/index.html', coreSelector: '#schemaForm' },
  { name: 'Settings', path: '/pages/settings/index.html', coreSelector: '.field' },
];

const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1280, height: 800 },
  { width: 1024, height: 768 },
];

const BASE_URL = process.env.TEST_BASE_URL || 'http://localhost:8000';

const LIBRARIES = [
  {
    name: 'Default Library',
    is_active: true,
    root_path: '/libraries/default',
    paper_count: 1,
  },
];

const PAPERS = [
  {
    id: 'paper-1',
    title: 'Test Paper for Smoke Validation',
    year: 2025,
    journal: 'Journal of Testing',
    paper_type: 'research',
    serial_number: 1,
    counts: {
      sections: 8,
      figures: 2,
      dft_results: 1,
      writing_cards: 1,
    },
  },
];

const PAPER_DETAIL = {
  id: 'paper-1',
  title: 'Test Paper for Smoke Validation',
  year: 2025,
  journal: 'Journal of Testing',
  abstract: 'Synthetic paper detail payload used by Playwright smoke tests.',
  sections: [{ id: 'chunk-1', section_title: 'Introduction', section_type: 'introduction', text: 'Smoke-test content.', page_start: 1, page_end: 1 }],
  figures: [],
  tables: [],
  dft_settings_items: [{ code: 'PBE', kpoints: '3x3x1' }],
  catalyst_samples_items: [{ name: 'Pt(111)' }],
  dft_results_items: [{ property: 'adsorption_energy', value: -1.23, unit: 'eV' }],
  electrochemical_performance_items: [{ metric: 'onset_potential', value: 0.71, unit: 'V' }],
  mechanism_claims_items: [{ claim: 'Associative pathway is favored.' }],
  writing_cards_items: [{ title: 'Key insight', summary: 'A concise validation card.' }],
};

const EVIDENCE_ITEMS = [
  {
    score: 0.91,
    source: 'sections',
    paper_id: 'paper-1',
    chunk_id: 'chunk-1',
    section_id: 'chunk-1',
    section_title: 'Results',
    text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
    page_start: 5,
    page_end: 5,
    score_breakdown: { bm25: 0.8, vector: 0.6, hybrid: 0.73 },
    evidence: {
      paper_id: 'paper-1',
      chunk_id: 'chunk-1',
      section_id: 'chunk-1',
      page_span: { page_start: 5, page_end: 5, span_start: null, span_end: null },
      evidence_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
      confidence: 0.91,
      source: 'section',
      section_title: 'Results',
      target_type: 'section',
      target_id: 'chunk-1',
    },
  },
];

const CLAIMS = [
  {
    claim_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
    source_type: 'writer',
    target_type: 'dft_results',
    target_id: null,
    evidence: [EVIDENCE_ITEMS[0].evidence],
    confidence: 0.91,
    validation_status: 'supported',
    metadata: {},
  },
];

const AUDIT = {
  ok: true,
  total_claims: 1,
  supported_claims: 1,
  unsupported_claims: 0,
  claims: [{ claim_text: CLAIMS[0].claim_text, status: 'supported', evidence: [EVIDENCE_ITEMS[0].evidence] }],
};

const EXTRACTION_RESULTS = {
  paper_id: 'paper-1',
  schemas: { DFTResult: { title: 'DFTResult' } },
  validation_status: 'validated',
  validation_warnings: [],
  results: {
    CatalystSample: [],
    DFTSetting: [],
    DFTResult: [
      {
        target_id: 'target-1',
        target_type: 'DFTResult',
        catalyst: { value: 'Fe-N4', unit: null, evidence_text: 'Fe-N4 catalyst.', source_section: 'Results', page_span: {}, confidence: 0.8 },
        adsorbate: { value: 'Li2S4', unit: null, evidence_text: 'Li2S4 adsorption.', source_section: 'Results', page_span: {}, confidence: 0.9 },
        energy_type: { value: 'adsorption_energy', unit: null, evidence_text: 'adsorption energy.', source_section: 'Results', page_span: {}, confidence: 0.9 },
        value: { value: -1.23, unit: 'eV', evidence_text: 'The adsorption energy is -1.23 eV.', source_section: 'Results', page_span: {}, confidence: 0.91 },
        reaction_step: { value: 'Li2S4 adsorption', unit: null, evidence_text: 'Li2S4 adsorption step.', source_section: 'Results', page_span: {}, confidence: 0.85 },
      },
    ],
    MechanismClaim: [],
    ElectrochemicalPerformance: [],
  },
};

function jsonResponse(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  });
}

async function mockApi(route) {
  const requestUrl = new URL(route.request().url());
  const pathname = requestUrl.pathname;
  const method = route.request().method();

  if (pathname === '/favicon.ico') {
    return route.fulfill({ status: 204, body: '' });
  }

  if (pathname === '/api/libraries' && method === 'GET') {
    return jsonResponse(route, LIBRARIES);
  }

  if (pathname === '/api/libraries' && method === 'POST') {
    return jsonResponse(route, { ok: true });
  }

  if (pathname === '/api/libraries/import') {
    return jsonResponse(route, { ok: true });
  }

  if (pathname === '/api/libraries/browse-roots') {
    return jsonResponse(route, [{ path: '/libraries/default' }]);
  }

  if (pathname === '/api/libraries/browse') {
    return jsonResponse(route, {
      current_path: '/libraries/default',
      parent_path: '/libraries',
      subdirs: [{ name: 'default', path: '/libraries/default' }],
    });
  }

  if (pathname.startsWith('/api/libraries/')) {
    return jsonResponse(route, { ok: true });
  }

  if (pathname === '/api/papers' && method === 'GET') {
    return jsonResponse(route, PAPERS);
  }

  if (pathname === '/api/papers/aggregate') {
    return jsonResponse(route, {
      adsorbate_groups: { 'H*': ['paper-1'] },
      catalyst_groups: { Pt: ['paper-1'] },
      possible_name_aliases: ['Pt(111)'],
    });
  }

  if (pathname === '/api/papers/compare') {
    return jsonResponse(route, {
      items: [
        {
          paper_id: 'paper-1',
          title: 'Test Paper for Smoke Validation',
          catalyst_type: 'Pt',
          adsorbate: 'Li2S4',
          property_type: 'adsorption_energy',
          value: -1.23,
          unit: 'eV',
          confidence: 0.9,
          evidence_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
        },
      ],
      stats: {
        count: 1,
        min: -1.23,
        max: -1.23,
        mean: -1.23,
        unit: 'eV',
      },
    });
  }

  if (pathname === '/api/papers/export/csv') {
    return route.fulfill({
      status: 200,
      contentType: 'text/csv',
      body: 'paper_id,title,value\npaper-1,Test Paper for Smoke Validation,-1.23\n',
    });
  }

  if (pathname === '/api/papers/stream') {
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'event: papers_update\ndata: []\n\n',
    });
  }

  if (pathname === '/api/papers/ai_workflow/jobs' && method === 'POST') {
    return jsonResponse(route, {
      job_id: 'job-1',
      type: 'ai_workflow',
      status: 'queued',
      progress: { message: 'Queued' },
      result: null,
      error: null,
      library_name: 'Default Library',
    });
  }

  if (pathname === '/api/papers/ai_workflow/jobs/job-1') {
    return jsonResponse(route, {
      job_id: 'job-1',
      type: 'ai_workflow',
      status: 'completed',
      progress: { message: 'Done' },
      result: { papers: PAPERS },
      error: null,
      library_name: 'Default Library',
    });
  }

  if (pathname === '/api/extraction/jobs' && method === 'GET') {
    return jsonResponse(route, [
      {
        job_id: 'extract-job-1',
        type: 'extraction',
        status: 'completed',
        progress: { phase: 'completed', paper_id: 'paper-1' },
        result: { paper_id: 'paper-1' },
        error: null,
        library_name: 'Default Library',
      },
    ]);
  }

  if (pathname === '/api/extraction/jobs' && method === 'POST') {
    return jsonResponse(route, {
      job_id: 'extract-job-1',
      type: 'extraction',
      status: 'queued',
      progress: { phase: 'queued', paper_id: 'paper-1' },
      result: null,
      error: null,
      library_name: 'Default Library',
    });
  }

  if (pathname.startsWith('/api/extraction/jobs/') && pathname.endsWith('/retry')) {
    return jsonResponse(route, {
      job_id: 'extract-job-retry',
      type: 'extraction',
      status: 'queued',
      progress: { phase: 'queued' },
      result: null,
      error: null,
      library_name: 'Default Library',
    });
  }

  if (pathname === '/api/extraction/schemas') {
    return jsonResponse(route, EXTRACTION_RESULTS.schemas);
  }

  if (pathname === '/api/extraction/results/paper-1' && method === 'GET') {
    return jsonResponse(route, EXTRACTION_RESULTS);
  }

  if (pathname === '/api/extraction/results/paper-1/reviews/audit' && method === 'GET') {
    return jsonResponse(route, {
      paper_id: 'paper-1',
      total_reviews: 1,
      active: 1,
      remapped: 0,
      stale: 0,
      ambiguous: 0,
      unresolved: 0,
      items: [
        {
          id: 'review-1',
          paper_id: 'paper-1',
          target_type: 'DFTResult',
          target_id: 'target-1',
          target_fingerprint: 'fingerprint-1',
          target_label: 'Pt(111)',
          field_path: 'DFTResult.value',
          target_resolution_status: 'active',
          field_name: 'value',
          reviewer_status: 'verified',
          reviewer: 'manual_reviewer',
          verified: true,
          created_at: '2026-05-25T12:00:00',
          updated_at: '2026-05-25T12:00:00'
        }
      ]
    });
  }

  if (pathname === '/api/extraction/results/paper-1/validate') {
    return jsonResponse(route, {
      paper_id: 'paper-1',
      status: 'validated',
      validation_warnings: [],
    });
  }

  if (pathname === '/api/retrieval/search') {
    return jsonResponse(route, {
      query: 'Li2S4',
      mode: 'focused',
      recall: { bm25: 'enabled', vector: 'enabled' },
      reranker: { enabled: true, name: 'noop_score_sort' },
      total: EVIDENCE_ITEMS.length,
      items: EVIDENCE_ITEMS,
    });
  }

  if (pathname === '/api/evidence/claims') {
    return jsonResponse(route, CLAIMS);
  }

  if (pathname === '/api/evidence/audit') {
    return jsonResponse(route, AUDIT);
  }

  if (pathname === '/api/papers/discovery/search') {
    return jsonResponse(route, { items: [] });
  }

  if (pathname === '/api/papers/ai_search') {
    return jsonResponse(route, { papers: [] });
  }

  if (pathname === '/api/papers/discovery/download' || pathname === '/api/papers/ingest/upload') {
    return jsonResponse(route, { papers: PAPERS });
  }

  if (pathname === '/api/papers/assets/test-figure.png') {
    return route.fulfill({ status: 204, body: '' });
  }

  if (pathname.startsWith('/api/papers/') && method === 'GET') {
    return jsonResponse(route, PAPER_DETAIL);
  }

  if (pathname.startsWith('/api/papers/') && method === 'POST') {
    return jsonResponse(route, { ok: true });
  }

  if (pathname === '/api/writer/status') {
    return jsonResponse(route, {
      backend_used: 'mock-backend',
      llm_status: 'ready',
      llm_error: null,
    });
  }

  if (pathname === '/api/writer/draft') {
    return jsonResponse(route, {
      backend_used: 'mock-backend',
      llm_status: 'ready',
      llm_error: null,
      guard_actions: [],
      citation_guard: { verified: true },
      outline: ['1. Intro', '2. Results'],
      introduction: 'Mock introduction.',
      dft_results: 'Mock DFT results.',
      discussion: 'Mock discussion.',
      figure_storyline: ['Figure 1 supports the main claim.'],
      prompt_preview: 'Mock prompt preview',
      evidence_claims: CLAIMS,
      citation_audit: AUDIT,
    });
  }

  if (pathname === '/api/system/agent-guide') {
    return jsonResponse(route, {
      base_url: 'http://localhost:8000',
      mcp_url: 'http://localhost:8000/mcp',
      notes: ['Mock guide'],
    });
  }

  if (pathname === '/api/external-analysis/import') {
    return jsonResponse(route, { ok: true });
  }

  if (pathname.startsWith('/api/external-analysis/papers/')) {
    return jsonResponse(route, { ok: true, run_id: 'run-1' });
  }

  if (pathname === '/api/external-analysis/runs') {
    return jsonResponse(route, []);
  }

  if (pathname.startsWith('/api/external-analysis/runs/')) {
    return jsonResponse(route, { ok: true });
  }

  if (pathname === '/api/settings') {
    if (method === 'GET') {
      return jsonResponse(route, {
        embedding_provider: 'deterministic',
        embedding_api_base: '',
        embedding_api_key: '',
        embedding_model: 'text-embedding-3-small',
        embedding_dimension: '1536',
        writer_backend: 'openai_compatible',
        writer_api_base: '',
        writer_api_key: '',
        writer_model: 'deepseek-chat',
        mcp_api_keys: '',
      });
    }
    return jsonResponse(route, { updated: 1 });
  }

  if (pathname === '/api/settings/status') {
    return jsonResponse(route, {
      embedding: { configured: true, provider: 'deterministic', model: 'text-embedding-3-small' },
      writer: { configured: true, backend: 'openai_compatible', model: 'deepseek-chat' },
      mcp: { has_keys: false, enabled: false },
    });
  }

  if (pathname === '/api/settings/ide-prompts') {
    return jsonResponse(route, {
      suggested_prompt: 'Mock IDE prompt',
      cursor_config_json: '{ "prompt": "mock" }',
      base_url: 'http://localhost:8000',
      mcp_url: 'http://localhost:8000/mcp',
      local_ip: '127.0.0.1',
      hostname: 'localhost',
    });
  }

  return route.fulfill({ status: 204, body: '' });
}

test.describe('Literature AI Front-end Smoke Tests', () => {
  let consoleErrors = [];

  test.beforeEach(async ({ page }) => {
    consoleErrors = [];

    await page.addInitScript(() => {
      class MockEventSource {
        constructor(url) {
          this.url = url;
          this.readyState = 1;
        }
        addEventListener() {}
        close() {
          this.readyState = 2;
        }
      }
      window.EventSource = MockEventSource;
    });

    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route('**/api/**', mockApi);
    // Intercept Google Fonts requests to prevent external network flakiness/timeouts
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({ status: 200, contentType: 'text/css', body: '' }));
    await page.route('https://fonts.gstatic.com/**', route => route.fulfill({ status: 404, body: '' }));

    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });
    page.on('pageerror', err => {
      consoleErrors.push(err.message);
    });
  });

  for (const pageInfo of PAGES) {
    test.describe(`Page: ${pageInfo.name}`, () => {
      for (const viewport of VIEWPORTS) {
        test(`renders correctly at ${viewport.width}x${viewport.height}`, async ({ page }) => {
          await page.setViewportSize(viewport);

          const url = `${BASE_URL}${pageInfo.path}`;
          const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });

          expect(response.status()).toBe(200);
          await page.waitForTimeout(1000);
          await expect(page.locator(pageInfo.coreSelector).first()).toBeVisible();
          expect(consoleErrors).toEqual([]);
        });
      }

      test('core interactions and buttons work', async ({ page }) => {
        await page.setViewportSize({ width: 1280, height: 800 });
        const url = `${BASE_URL}${pageInfo.path}`;
        await page.goto(url);
        await page.waitForTimeout(1000);

        if (pageInfo.name === 'Dashboard') {
          await expect(page.locator('a.action-btn').first()).toBeVisible();
        } else if (pageInfo.name === 'Ingestion Center') {
          await page.click('button[onclick="switchIngestTab(\'doi\')"]');
          await expect(page.locator('#tab-doi')).toBeVisible();

          await page.click('button[onclick="switchIngestTab(\'online\')"]');
          await expect(page.locator('#tab-online')).toBeVisible();
        } else if (pageInfo.name === 'Literature Library') {
          await page.click('#addLiteratureBtn');
          await expect(page.locator('#addLiteratureMenu')).toBeVisible();
          await page.click('#addLiteratureMenu [data-add-mode="ai"]');
          await expect(page.locator('#addLiteratureDialog')).toBeVisible();
          await page.click('#addLiteratureDialog button:has-text("关闭")');

          await page.click('.paper-card');
          await page.click('button[data-tab="writing"]');
          await expect(page.locator('#tab-writing')).toBeVisible();

          await page.click('button[data-tab="review"]');
          await expect(page.locator('#tab-review')).toBeVisible();
        } else if (pageInfo.name === 'DFT Database') {
          await expect(page.locator('button[onclick="exportCSV()"]')).toBeVisible();
        } else if (pageInfo.name === 'AI Writing Studio') {
          await expect(page.locator('button[onclick="generateAcademicDraft()"]')).toBeVisible();
        } else if (pageInfo.name === 'Extraction Review Workbench') {
          await expect(page.locator('#schemaSelect')).toBeVisible();
          await page.click('button:has-text("Validate")');
          await expect(page.locator('#warningsBox')).toBeVisible();
        } else if (pageInfo.name === 'Settings') {
          await page.click('button[onclick="showSection(\'ide\')"]');
          await expect(page.locator('#section-ide')).toBeVisible();

          await page.click('button[onclick="showSection(\'theme\')"]');
          await expect(page.locator('#section-theme')).toBeVisible();
        }

        expect(consoleErrors).toEqual([]);
      });
    });
  }

  test('business flow: open Writing Studio, add evidence, generate draft, and view Citation Audit', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/ai_writer/index.html`);
    await page.waitForTimeout(500);
    await page.fill('#writingTopic', 'Li2S4 adsorption energy Fe-N4');
    await page.check('#paperChecklist input[type="checkbox"]');
    await page.click('button:has-text("Search evidence")');
    await expect(page.locator('#evidencePanel')).toContainText('score');
    await page.click('button[onclick="generateAcademicDraft()"]');
    await expect(page.locator('#tab-outline')).toContainText('Intro');
    await page.click('button:has-text("Run Citation Audit")');
    await expect(page.locator('#tab-audit')).toContainText('Citation Audit');
  });

  test('business flow: Paper Detail shows evidence panel and claim detail', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1`);
    await page.waitForTimeout(500);
    await expect(page.locator('#evidencePanel')).toContainText('supported');
    await page.click('#evidencePanel button');
    await expect(page.locator('#evidenceDetail')).toContainText('paper_id');
    await expect(page.locator('#evidenceDetail')).toContainText('chunk_id');
  });

  test('business flow: open manual validation workbench and validate extraction results', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
    await page.waitForTimeout(500);
    await expect(page.locator('#schemaForm')).toContainText('value');
    await page.click('button:has-text("Validate")');
    await expect(page.locator('#warningsBox')).toContainText('No validation warnings');
  });

  test('business flow: validation workbench editing, verifying, and warning filtering', async ({ page }) => {
    const mockResults = {
      ...EXTRACTION_RESULTS,
      validation_warnings: [
        {
          severity: 'warning',
          code: 'INVALID_VALUE',
          message: 'Energy value seems unusually high',
          target_type: 'DFTResult',
          target_id: 'target-1',
          field: 'value'
        }
      ],
      results: {
        ...EXTRACTION_RESULTS.results,
        DFTResult: [
          {
            target_id: 'target-1',
            target_type: 'DFTResult',
            catalyst: { value: 'Fe-N4', unit: null, evidence_text: 'Fe-N4 catalyst.', source_section: 'Results', page_span: {}, confidence: 0.8 },
            adsorbate: { value: 'Li2S4', unit: null, evidence_text: 'Li2S4 adsorption.', source_section: 'Results', page_span: {}, confidence: 0.9 },
            energy_type: { value: 'adsorption_energy', unit: null, evidence_text: 'adsorption energy.', source_section: 'Results', page_span: {}, confidence: 0.9 },
            value: { 
              value: -1.23, 
              unit: 'eV', 
              evidence_text: 'The adsorption energy is -1.23 eV.', 
              source_section: 'Results', 
              page_span: {}, 
              confidence: 0.91,
              review: { reviewer_status: 'pending' },
              verified: false
            },
            reaction_step: { value: 'Li2S4 adsorption', unit: null, evidence_text: 'Li2S4 adsorption step.', source_section: 'Results', page_span: {}, confidence: 0.85 },
          }
        ]
      }
    };

    let saveCalled = false;
    let verifyCalled = false;

    await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
      return jsonResponse(route, mockResults);
    });

    await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/save$/, route => {
      saveCalled = true;
      return jsonResponse(route, { status: 'success' });
    });

    await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/mark-verified$/, route => {
      verifyCalled = true;
      return jsonResponse(route, { status: 'success' });
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
    await page.waitForTimeout(500);

    await expect(page.locator('#schemaForm')).toContainText('Fe-N4');
    await expect(page.locator('#schemaForm')).toContainText('Energy value seems unusually high');

    const evidenceBtn = page.locator('button:has-text("原文证据 ▾")').first();
    await evidenceBtn.click();
    const evidenceTextarea = page.locator('textarea[data-field="catalyst"]').first();
    await expect(evidenceTextarea).toBeVisible();

    const valueInput = page.locator('input[data-field="value"][data-part="value"]').first();
    await valueInput.fill('-1.25');
    
    const saveBtn = page.locator('button:has-text("保存")').first();
    await saveBtn.click();
    await page.waitForTimeout(200);
    expect(saveCalled).toBe(true);

    const verifyBtn = page.locator('button:has-text("校验")').first();
    await verifyBtn.click();
    await page.waitForTimeout(200);
    expect(verifyCalled).toBe(true);

    const filterSelect = page.locator('#filterSelect');
    await filterSelect.selectOption('warnings');
    await page.waitForTimeout(200);

    await expect(page.locator('#schemaForm')).toContainText('Energy value seems unusually high');
    await expect(page.locator('input[data-field="catalyst"]')).toHaveCount(0);

    verifyCalled = false;
    await page.click('.footer-actions button:has-text("Mark verified")');
    await page.waitForTimeout(200);
    expect(verifyCalled).toBe(true);
  });

  test('business flow: view DFT extraction results and evidence link', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('#dftTable')).toContainText('Li2S4');
    await page.click('button:has-text("evidence link")');
    await expect(page.locator('#evidenceDetail')).toContainText('evidence_text');
  });

  test('business flow: literature library opens extraction job center', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('button:has-text("Extraction Jobs")');
    await expect(page.locator('#acquisitionResult')).toContainText('Extraction Job Center');
  });

  test('business flow: empty literature library does not crash', async ({ page }) => {
    await page.route(/\/api\/papers/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, []);
      }
      return route.fallback();
    });
    await page.route(/\/api\/libraries/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [
          { name: 'Empty Library', is_active: true, root_path: '/libraries/empty', paper_count: 0 }
        ]);
      }
      return route.fallback();
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('#paperList')).toBeVisible();
    await expect(page.locator('.list-empty')).toBeVisible();
    await expect(page.locator('#workspaceEmpty')).toContainText('当前库还没有文献');
  });

  test('business flow: literature library displays metadata-only state', async ({ page }) => {
    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [
          {
            id: 'paper-meta-only',
            title: 'Metadata Only Paper',
            year: 2025,
            journal: 'Journal of Metadata',
            paper_type: 'research',
            oa_status: 'metadata_only',
            counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
          }
        ]);
      }
      return route.fallback();
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('.status-chip.meta')).toContainText('仅元数据');
  });

  test('business flow: metadata-only attach pdf and workflow status checks', async ({ page }) => {
    let attachCalled = false;
    
    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        if (attachCalled) {
          return jsonResponse(route, [
            {
              id: 'paper-meta-only',
              title: 'Metadata Only Paper (Attached)',
              year: 2025,
              journal: 'Journal of Metadata',
              paper_type: 'research',
              pdf_path: '/path/to/pdf',
              oa_status: 'local_pdf',
              counts: { sections: 5, figures: 1, dft_results: 1, writing_cards: 1 }
            }
          ]);
        }
        return jsonResponse(route, [
          {
            id: 'paper-meta-only',
            title: 'Metadata Only Paper',
            year: 2025,
            journal: 'Journal of Metadata',
            paper_type: 'research',
            oa_status: 'metadata_only',
            counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
          }
        ]);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/paper-meta-only$/, route => {
      if (attachCalled) {
        return jsonResponse(route, {
          id: 'paper-meta-only',
          title: 'Metadata Only Paper (Attached)',
          year: 2025,
          journal: 'Journal of Metadata',
          pdf_path: '/path/to/pdf',
          oa_status: 'local_pdf',
          abstract: 'This paper now has a PDF attached.',
          counts: { sections: 5, figures: 1, dft_results: 1, writing_cards: 1 }
        });
      }
      return jsonResponse(route, {
        id: 'paper-meta-only',
        title: 'Metadata Only Paper',
        year: 2025,
        journal: 'Journal of Metadata',
        oa_status: 'metadata_only',
        abstract: 'This is a metadata-only paper without PDF.',
        counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
      });
    });

    await page.route(/\/api\/papers\/paper-meta-only\/attach-pdf$/, route => {
      attachCalled = true;
      return jsonResponse(route, {
        paper_id: 'paper-meta-only',
        title: 'Metadata Only Paper (Attached)',
        status: 'completed'
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    const metaCard = page.locator('.paper-card').first();
    await expect(metaCard).toContainText('Metadata Only Paper');
    await expect(metaCard.locator('.status-chip.meta')).toBeVisible();

    await metaCard.click();
    await page.waitForTimeout(500);

    await expect(page.locator('#summaryContent')).toContainText('尚无 PDF');
    const uploadBtn = page.locator('#summaryContent button:has-text("上传 PDF 并自动合并")');
    await expect(uploadBtn).toBeVisible();

    const fileChooserPromise = page.waitForEvent('filechooser');
    await uploadBtn.click();
    const fileChooser = await fileChooserPromise;
    
    await fileChooser.setFiles({
      name: 'test.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4...'),
    });

    await page.waitForTimeout(500);

    await expect(page.locator('.paper-card.active')).toContainText('Metadata Only Paper (Attached)');
    await expect(page.locator('.paper-card.active .status-chip.parsed')).toBeVisible();
  });

  test('business flow: attach-pdf identity verification - needs_confirmation and confirm', async ({ page }) => {
    let firstCall = true;
    let secondCallPayload = null;

    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [
          {
            id: 'paper-meta-only',
            title: 'Metadata Only Paper',
            year: 2025,
            journal: 'Journal of Metadata',
            paper_type: 'research',
            oa_status: 'metadata_only',
            counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
          }
        ]);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/paper-meta-only$/, route => {
      return jsonResponse(route, {
        id: 'paper-meta-only',
        title: 'Metadata Only Paper',
        year: 2025,
        journal: 'Journal of Metadata',
        oa_status: 'metadata_only',
        abstract: 'This is a metadata-only paper without PDF.',
        counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
      });
    });

    await page.route(/\/api\/papers\/paper-meta-only\/attach-pdf$/, route => {
      const postData = route.request().postData() || '';
      if (postData.includes('confirm_identity_mismatch') && postData.includes('true')) {
        secondCallPayload = postData;
        return jsonResponse(route, {
          paper_id: 'paper-meta-only',
          title: 'Metadata Only Paper (Attached Confirmed)',
          status: 'merged_confirmed'
        });
      } else {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: {
              status: 'needs_confirmation',
              target_paper_id: 'paper-meta-only',
              target: { title: 'Metadata Only Paper', doi: '10.1000/xyz', year: 2025 },
              incoming: { title: 'Different Ingested Title', doi: '10.1000/xyz', year: 2026 },
              match_score: 0.65,
              match_reason: 'Title similarity is slightly low but DOI matches.'
            }
          })
        });
      }
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    const metaCard = page.locator('.paper-card').first();
    await metaCard.click();
    await page.waitForTimeout(500);

    const uploadBtn = page.locator('#summaryContent button:has-text("上传 PDF 并自动合并")');
    const fileChooserPromise = page.waitForEvent('filechooser');
    await uploadBtn.click();
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: 'test.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4...'),
    });

    await page.waitForTimeout(500);

    // Confirmation dialog should be visible
    const confirmModal = page.locator('#identityConfirmModal');
    await expect(confirmModal).toBeVisible();
    await expect(confirmModal).toContainText('需要确认文献身份');
    await expect(confirmModal).toContainText('65%');
    await expect(confirmModal).toContainText('Title similarity is slightly low');

    // Click cancel in confirm dialog
    await confirmModal.locator('#confirmCancelBtn').click();
    await expect(confirmModal).not.toBeVisible();
    expect(secondCallPayload).toBeNull();

    // Trigger upload again to confirm
    const fileChooserPromise2 = page.waitForEvent('filechooser');
    await uploadBtn.click();
    const fileChooser2 = await fileChooserPromise2;
    await fileChooser2.setFiles({
      name: 'test.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4...'),
    });

    await page.waitForTimeout(500);
    await expect(confirmModal).toBeVisible();
    await confirmModal.locator('#confirmAttachBtn').click();

    await page.waitForTimeout(500);
    await expect(confirmModal).not.toBeVisible();
    expect(secondCallPayload).toContain('confirm_identity_mismatch');
    expect(secondCallPayload).toContain('true');
  });

  test('business flow: attach-pdf identity verification - identity_mismatch block', async ({ page }) => {
    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [
          {
            id: 'paper-meta-only',
            title: 'Metadata Only Paper',
            year: 2025,
            journal: 'Journal of Metadata',
            paper_type: 'research',
            oa_status: 'metadata_only',
            counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
          }
        ]);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/paper-meta-only$/, route => {
      return jsonResponse(route, {
        id: 'paper-meta-only',
        title: 'Metadata Only Paper',
        year: 2025,
        journal: 'Journal of Metadata',
        oa_status: 'metadata_only',
        abstract: 'This is a metadata-only paper without PDF.',
        counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
      });
    });

    await page.route(/\/api\/papers\/paper-meta-only\/attach-pdf$/, route => {
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            status: 'identity_mismatch',
            target_paper_id: 'paper-meta-only',
            target: { title: 'Metadata Only Paper', doi: '10.1000/xyz', year: 2025 },
            incoming: { title: 'Entirely Different Paper', doi: '10.1000/abc', year: 2026 },
            match_score: 0.12,
            match_reason: 'DOIs are incompatible.'
          }
        })
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    const metaCard = page.locator('.paper-card').first();
    await metaCard.click();
    await page.waitForTimeout(500);

    const uploadBtn = page.locator('#summaryContent button:has-text("上传 PDF 并自动合并")');
    const fileChooserPromise = page.waitForEvent('filechooser');
    await uploadBtn.click();
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: 'test.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4...'),
    });

    await page.waitForTimeout(500);

    // Mismatch block dialog should be visible
    const mismatchModal = page.locator('#identityMismatchModal');
    await expect(mismatchModal).toBeVisible();
    await expect(mismatchModal).toContainText('文献身份冲突');
    await expect(mismatchModal).toContainText('DOI 冲突');
    await expect(mismatchModal.locator('#confirmAttachBtn')).toHaveCount(0); // Should NOT have forced confirm button

    // Click cancel in mismatch block dialog
    await mismatchModal.locator('#mismatchCancelBtn').click();
    await expect(mismatchModal).not.toBeVisible();
  });

  test('business flow: attach-pdf identity verification - already_exists', async ({ page }) => {
    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [
          {
            id: 'paper-meta-only',
            title: 'Metadata Only Paper',
            year: 2025,
            journal: 'Journal of Metadata',
            paper_type: 'research',
            oa_status: 'metadata_only',
            counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
          }
        ]);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/paper-meta-only$/, route => {
      return jsonResponse(route, {
        id: 'paper-meta-only',
        title: 'Metadata Only Paper',
        year: 2025,
        journal: 'Journal of Metadata',
        oa_status: 'metadata_only',
        abstract: 'This is a metadata-only paper without PDF.',
        counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
      });
    });

    await page.route(/\/api\/papers\/paper-meta-only\/attach-pdf$/, route => {
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            status: 'already_exists',
            target_paper_id: 'paper-existing',
            title: 'Existing Full Paper'
          }
        })
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    const metaCard = page.locator('.paper-card').first();
    await metaCard.click();
    await page.waitForTimeout(500);

    const uploadBtn = page.locator('#summaryContent button:has-text("上传 PDF 并自动合并")');
    const fileChooserPromise = page.waitForEvent('filechooser');
    await uploadBtn.click();
    const fileChooser = await fileChooserPromise;
    await fileChooser.setFiles({
      name: 'test.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4...'),
    });

    await page.waitForTimeout(500);

    // Jump toast should be visible
    const jumpToast = page.locator('.already-exists-toast');
    await expect(jumpToast).toBeVisible();
    await expect(jumpToast).toContainText('文献已存在');
  });

  test('business flow: AI workflow job results show metadata_only and already_exists', async ({ page }) => {
    await page.route(/\/api\/papers\/ai_workflow\/jobs\/job-1/, route => {
      return jsonResponse(route, {
        job_id: 'job-1',
        type: 'ai_workflow',
        status: 'completed',
        progress: { message: 'Done' },
        result: {
          prompt_used: 'AI Search Prompt',
          ingested: [
            {
              paper_id: 'paper-1',
              title: 'Ingested Paper 1',
              status: 'completed',
              identifier: 'doi:1',
              doi: '10.1000/1'
            },
            {
              paper_id: 'paper-2',
              title: 'Metadata Only Ingested',
              status: 'metadata_only',
              identifier: 'doi:2',
              doi: '10.1000/2'
            },
            {
              paper_id: 'paper-3',
              title: 'Already Existing Paper',
              status: 'already_exists',
              identifier: 'doi:3',
              doi: '10.1000/3'
            }
          ],
          failed: [
            {
              identifier: 'doi:4',
              title: 'Failed Paper',
              code: 'DOWNLOAD_FAILED',
              reason: 'Server Timeout'
            }
          ]
        },
        error: null,
        library_name: 'Default Library'
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    await page.click('#addLiteratureBtn');
    await page.click('#addLiteratureMenu [data-add-mode="ai"]');
    await page.fill('#aiSearchQuery', 'test query');
    await page.click('#addLiteratureDialog button:has-text("搜索并收录")');

    await page.waitForTimeout(1000);
    
    const resultBox = page.locator('#acquisitionResult');
    await expect(resultBox.locator('.status-chip.parsed').first()).toContainText('已收录');
    await expect(resultBox.locator('.status-chip.meta').first()).toContainText('元数据');
    await expect(resultBox.locator('.status-chip.duplicate').first()).toContainText('已存在');
    await expect(resultBox.locator('.status-chip.failed').first()).toContainText('DOWNLOAD_FAILED');
  });

  test.describe('G2B Review Stability & Audit Tests', () => {
    test('1. Audit all green shows matching normal banner', async ({ page }) => {
      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/audit$/, route => {
        return jsonResponse(route, {
          paper_id: 'paper-1',
          total_reviews: 2,
          active: 1,
          remapped: 1,
          stale: 0,
          ambiguous: 0,
          unresolved: 0,
          items: []
        });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const summaryBox = page.locator('#stabilitySummaryBox');
      await expect(summaryBox).toBeVisible();
      await expect(summaryBox).toContainText('人工校验记录与当前抽取结果匹配正常');
      await expect(summaryBox).toContainText('有效: 1');
      await expect(summaryBox).toContainText('已重映射: 1');
    });

    test('2. Audit with stale reviews shows needs confirmation banner and overrides verified status', async ({ page }) => {
      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/audit$/, route => {
        return jsonResponse(route, {
          paper_id: 'paper-1',
          total_reviews: 1,
          active: 0,
          remapped: 0,
          stale: 1,
          ambiguous: 0,
          unresolved: 0,
          items: []
        });
      });

      const mockStaleResults = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [
            {
              target_id: 'target-1',
              target_type: 'DFTResult',
              value: {
                value: -1.23,
                unit: 'eV',
                review: {
                  reviewer_status: 'verified',
                  target_resolution_status: 'stale',
                  target_label: 'Pt(111)',
                  field_path: 'DFTResult.value',
                  target_fingerprint: 'fp-stale'
                },
                verified: true
              }
            }
          ]
        }
      };

      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, mockStaleResults);
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const summaryBox = page.locator('#stabilitySummaryBox');
      await expect(summaryBox).toBeVisible();
      await expect(summaryBox).toContainText('部分人工校验记录已无法安全匹配到当前抽取结果');
      await expect(summaryBox).toContainText('已失效: 1');

      // The field verified status must override to "需重新确认"
      const statusChip = page.locator('.status-chip').first();
      await expect(statusChip).toContainText('需重新确认');

      // The resolution status badge must show "已失效"
      const resChip = page.locator('.res-status-chip').first();
      await expect(resChip).toContainText('已失效');
      
      // Target label and field path must be visible
      await expect(page.locator('#schemaForm')).toContainText('目标: Pt(111)');
      await expect(page.locator('#schemaForm')).toContainText('路径: DFTResult.value');
    });

    test('3. Ambiguous review displays ambiguous badge', async ({ page }) => {
      const mockAmbiguousResults = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [
            {
              target_id: 'target-1',
              target_type: 'DFTResult',
              value: {
                value: -1.23,
                unit: 'eV',
                review: {
                  reviewer_status: 'verified',
                  target_resolution_status: 'ambiguous',
                  target_label: 'Pt(111)'
                },
                verified: true
              }
            }
          ]
        }
      };

      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, mockAmbiguousResults);
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const resChip = page.locator('.res-status-chip').first();
      await expect(resChip).toContainText('有歧义');
      
      const statusChip = page.locator('.status-chip').first();
      await expect(statusChip).toContainText('需重新确认');
    });

    test('4. Unresolved review displays unresolved badge', async ({ page }) => {
      const mockUnresolvedResults = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [
            {
              target_id: 'target-1',
              target_type: 'DFTResult',
              value: {
                value: -1.23,
                unit: 'eV',
                review: {
                  reviewer_status: 'verified',
                  target_resolution_status: 'unresolved'
                },
                verified: true
              }
            }
          ]
        }
      };

      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, mockUnresolvedResults);
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const resChip = page.locator('.res-status-chip').first();
      await expect(resChip).toContainText('未解析');
      
      const statusChip = page.locator('.status-chip').first();
      await expect(statusChip).toContainText('需重新确认');
    });

    test('5. Workbench filtering conditions work correctly', async ({ page }) => {
      const mockMixedResults = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [
            {
              target_id: 'target-1',
              target_type: 'DFTResult',
              catalyst: {
                value: 'Pt',
                review: { reviewer_status: 'verified', target_resolution_status: 'active' },
                verified: true
              },
              adsorbate: {
                value: 'H',
                review: { reviewer_status: 'verified', target_resolution_status: 'stale' },
                verified: true
              }
            }
          ]
        }
      };

      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, mockMixedResults);
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      // Verify both are present initially
      await expect(page.locator('.field-container')).toHaveCount(2);

      const filterSelect = page.locator('#filterSelect');
      
      // Filter: active_remapped
      await filterSelect.selectOption('active_remapped');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container')).toHaveCount(1);
      await expect(page.locator('.field-container')).toContainText('catalyst');

      // Filter: stale_ambiguous_unresolved
      await filterSelect.selectOption('stale_ambiguous_unresolved');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container')).toHaveCount(1);
      await expect(page.locator('.field-container')).toContainText('adsorbate');

      // Filter: needs_reconfirmation
      await filterSelect.selectOption('needs_reconfirmation');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container')).toHaveCount(1);
      await expect(page.locator('.field-container')).toContainText('adsorbate');
    });

    test('6. Save triggers confirm alert and triggers refetching on stale reviews', async ({ page }) => {
      let confirmTriggered = false;
      let refetchCalled = false;

      page.on('dialog', dialog => {
        confirmTriggered = true;
        dialog.accept();
      });

      const mockStaleResults = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [
            {
              target_id: 'target-1',
              target_type: 'DFTResult',
              value: {
                value: -1.23,
                unit: 'eV',
                review: {
                  reviewer_status: 'verified',
                  target_resolution_status: 'stale'
                },
                verified: true
              }
            }
          ]
        }
      };

      let getResultsCallCount = 0;
      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        getResultsCallCount++;
        if (getResultsCallCount > 1) {
          refetchCalled = true;
        }
        return jsonResponse(route, mockStaleResults);
      });

      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/save$/, route => {
        return jsonResponse(route, { status: 'success' });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      // Trigger Save
      await page.click('button:has-text("保存")');
      await page.waitForTimeout(200);

      expect(confirmTriggered).toBe(true);
      expect(refetchCalled).toBe(true);
    });

    test('7. Audit API 404 gracefully handles degradation', async ({ page }) => {
      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/audit$/, route => {
        return route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Not Found' })
        });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const summaryBox = page.locator('#stabilitySummaryBox');
      await expect(summaryBox).toBeVisible();
      await expect(summaryBox).toContainText('review audit 暂不可用');
    });

    test('8. Literature Library detail page surfaces warning banner', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1$/, route => {
        return jsonResponse(route, {
          id: 'paper-1',
          title: 'Test Paper with Stale Reviews',
          oa_status: 'local_pdf',
          counts: { sections: 1, figures: 0, dft_results: 1 }
        });
      });

      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/audit$/, route => {
        return jsonResponse(route, {
          paper_id: 'paper-1',
          total_reviews: 3,
          active: 1,
          remapped: 0,
          stale: 2,
          ambiguous: 0,
          unresolved: 0,
          items: []
        });
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);

      // Click on paper card to load detail
      await page.click('.paper-card');
      await page.waitForTimeout(500);

      // Verify the warning banner inside summaryContent is visible
      const warningBanner = page.locator('#summaryContent .section-card:has-text("人工校验需要重新确认")');
      await expect(warningBanner).toBeVisible();
      await expect(warningBanner).toContainText('该文献有 2 条人工校验记录需要重新确认');
      await expect(warningBanner).toContainText('已失效 2');

      // Go to Review Tab
      await page.click('button[data-tab="review"]');
      await page.waitForTimeout(500);

      // Verify the tab review warning banner is visible
      const tabWarningBanner = page.locator('#tab-review #reviewTabAuditWarning');
      await expect(tabWarningBanner).toBeVisible();
      await expect(tabWarningBanner).toContainText('该文献有 2 条人工校验记录需要重新确认');
    });
  });
});
