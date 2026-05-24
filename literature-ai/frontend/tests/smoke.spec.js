const { test, expect } = require('@playwright/test');

const PAGES = [
  { name: 'Dashboard', path: '/pages/dashboard/index.html', coreSelector: '.panel-card' },
  { name: 'Ingestion Center', path: '/pages/ingestion/index.html', coreSelector: '.dropzone' },
  { name: 'Literature Library', path: '/pages/literature_library/index.html', coreSelector: '#paperList' },
  { name: 'Paper Detail', path: '/pages/paper_detail/index.html', coreSelector: '.panel-card' },
  { name: 'DFT Database', path: '/pages/dft_database/index.html', coreSelector: '#dftTable' },
  { name: 'AI Writing Studio', path: '/pages/ai_writer/index.html', coreSelector: '#paperChecklist' },
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
  sections: [{ heading: 'Introduction', content: 'Smoke-test content.' }],
  figures: [],
  tables: [],
  dft_settings_items: [{ code: 'PBE', kpoints: '3x3x1' }],
  catalyst_samples_items: [{ name: 'Pt(111)' }],
  dft_results_items: [{ property: 'adsorption_energy', value: -1.23, unit: 'eV' }],
  electrochemical_performance_items: [{ metric: 'onset_potential', value: 0.71, unit: 'V' }],
  mechanism_claims_items: [{ claim: 'Associative pathway is favored.' }],
  writing_cards_items: [{ title: 'Key insight', summary: 'A concise validation card.' }],
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
          adsorbate: 'H*',
          value: -1.23,
          unit: 'eV',
          confidence: 0.9,
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
          const response = await page.goto(url);

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
});
