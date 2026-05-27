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
    doi: '10.1000/primary-doi 10.2000/reference-doi',
    year: 2025,
    journal: 'Journal of Testing',
    paper_type: 'research',
    pdf_path: 'test.pdf',
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
  doi: '10.1000/primary-doi 10.2000/reference-doi',
  year: 2025,
  journal: 'Journal of Testing',
  pdf_path: 'test.pdf',
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

const PILOT_PAPER_ID = '3978dc79f94f4457863fd68449ae293d';

const PILOT_PAPER = {
  id: PILOT_PAPER_ID,
  title: '锂硫电池非均相电催化剂',
  doi: '',
  year: 2026,
  journal: 'Pilot active library',
  paper_type: 'research',
  pdf_path: 'pilot.pdf',
  counts: {
    sections: 1,
    figures: 0,
    dft_results: 0,
    writing_cards: 1,
    field_reviews: 5,
  },
  abstract: 'D4-3C pilot paper for pending review UX verification.',
  sections: [
    {
      id: 'pilot-section-1',
      section_title: 'Results',
      section_type: 'results',
      text: 'The Li-S heterogeneous electrocatalyst evidence text is available but lacks an exact PDF page locator.',
      page_start: null,
      page_end: null,
    },
  ],
};

const PILOT_PENDING_REVIEW_IDS = [
  'e2c75b7f-2d9c-41ff-a6e1-e95e5d491896',
  '09f83676-8f13-4e82-a576-ab359b264933',
  '280f2d9e-3ebb-4107-9702-f6ea6d645465',
  '4ba0e490-5934-439c-8136-33a8ddf4e201',
  '56f72584-45b3-465b-9a40-97ec60a2fabf',
];

const PILOT_PENDING_REVIEWS = [
  {
    id: PILOT_PENDING_REVIEW_IDS[0],
    paper_id: PILOT_PAPER_ID,
    target_type: 'catalyst_samples',
    target_id: '11111111-1111-4111-8111-111111111111',
    target_fingerprint: 'pilot-catalyst-name',
    target_label: 'heterogeneous Li-S electrocatalyst',
    field_path: 'CatalystSample.name',
    field_name: 'name',
    original_value: 'heterogeneous Li-S electrocatalyst',
    reviewed_value: null,
    unit: null,
    evidence_text: 'Evidence text for the heterogeneous catalyst is present.',
    reviewer_status: 'pending',
    reviewer: null,
    reviewer_note: 'prepared_from_extraction',
    target_resolution_status: 'active',
    remapped_from_target_id: null,
    last_resolved_target_id: '11111111-1111-4111-8111-111111111111',
    created_at: '2026-05-27T10:00:00',
    updated_at: '2026-05-27T10:00:00',
  },
  {
    id: PILOT_PENDING_REVIEW_IDS[1],
    paper_id: PILOT_PAPER_ID,
    target_type: 'catalyst_samples',
    target_id: '11111111-1111-4111-8111-111111111111',
    target_fingerprint: 'pilot-catalyst-type',
    target_label: 'single atom catalyst',
    field_path: 'CatalystSample.catalyst_type',
    field_name: 'catalyst_type',
    original_value: 'single atom catalyst',
    reviewed_value: null,
    unit: null,
    evidence_text: 'Catalyst type evidence text is visible to the reviewer.',
    reviewer_status: 'pending',
    reviewer: null,
    reviewer_note: 'prepared_from_extraction',
    target_resolution_status: 'active',
    remapped_from_target_id: null,
    last_resolved_target_id: '11111111-1111-4111-8111-111111111111',
    created_at: '2026-05-27T10:00:00',
    updated_at: '2026-05-27T10:00:00',
  },
  {
    id: PILOT_PENDING_REVIEW_IDS[2],
    paper_id: PILOT_PAPER_ID,
    target_type: 'catalyst_samples',
    target_id: '11111111-1111-4111-8111-111111111111',
    target_fingerprint: 'pilot-metal-centers',
    target_label: 'Co-N4',
    field_path: 'CatalystSample.metal_centers',
    field_name: 'metal_centers',
    original_value: ['Co-N4'],
    reviewed_value: null,
    unit: null,
    evidence_text: 'Metal-center evidence text is visible.',
    reviewer_status: 'pending',
    reviewer: null,
    reviewer_note: 'prepared_from_extraction',
    target_resolution_status: 'active',
    remapped_from_target_id: null,
    last_resolved_target_id: '11111111-1111-4111-8111-111111111111',
    created_at: '2026-05-27T10:00:00',
    updated_at: '2026-05-27T10:00:00',
  },
  {
    id: PILOT_PENDING_REVIEW_IDS[3],
    paper_id: PILOT_PAPER_ID,
    target_type: 'dft_settings',
    target_id: '22222222-2222-4222-8222-222222222222',
    target_fingerprint: 'pilot-convergence',
    target_label: 'convergence settings',
    field_path: 'DFTSetting.convergence_settings',
    field_name: 'convergence_settings',
    original_value: { force: '0.02 eV/A' },
    reviewed_value: null,
    unit: null,
    evidence_text: 'DFT convergence evidence text is visible.',
    reviewer_status: 'pending',
    reviewer: null,
    reviewer_note: 'prepared_from_extraction',
    target_resolution_status: 'active',
    remapped_from_target_id: null,
    last_resolved_target_id: '22222222-2222-4222-8222-222222222222',
    created_at: '2026-05-27T10:00:00',
    updated_at: '2026-05-27T10:00:00',
  },
  {
    id: PILOT_PENDING_REVIEW_IDS[4],
    paper_id: PILOT_PAPER_ID,
    target_type: 'electrochemical_performance',
    target_id: '33333333-3333-4333-8333-333333333333',
    target_fingerprint: 'pilot-rate',
    target_label: '0.5 C',
    field_path: 'ElectrochemicalPerformance.rate',
    field_name: 'rate',
    original_value: '0.5 C',
    reviewed_value: null,
    unit: null,
    evidence_text: 'Rate-performance evidence text is visible.',
    reviewer_status: 'pending',
    reviewer: null,
    reviewer_note: 'prepared_from_extraction',
    target_resolution_status: 'active',
    remapped_from_target_id: null,
    last_resolved_target_id: '33333333-3333-4333-8333-333333333333',
    created_at: '2026-05-27T10:00:00',
    updated_at: '2026-05-27T10:00:00',
  },
];

function reviewToField(review, valueOverride) {
  return {
    value: valueOverride === undefined ? review.original_value : valueOverride,
    unit: review.unit,
    evidence_text: review.evidence_text,
    source_section: 'Results',
    page_span: {},
    confidence: 0.74,
    verified: false,
    review,
    evidence_locator: {
      locator_status: 'missing_page',
      page: null,
      bbox: null,
      evidence_text: review.evidence_text,
      paper_id: PILOT_PAPER_ID,
      can_jump_to_pdf_page: false,
      can_highlight_in_pdf: false,
      warning_reason: 'unsafe_locator',
    },
  };
}

const PILOT_EXTRACTION_RESULTS = {
  paper_id: PILOT_PAPER_ID,
  field_reviews: PILOT_PENDING_REVIEWS,
  schemas: { CatalystSample: {}, DFTSetting: {}, ElectrochemicalPerformance: {} },
  validation_status: 'needs_review',
  validation_warnings: PILOT_PENDING_REVIEWS.map(review => ({
    severity: 'warning',
    code: 'evidence_locator_missing_page',
    message: 'Exact PDF locator missing; unsafe_locator / no exact locator.',
    target_type: review.target_type,
    target_id: review.target_id,
    field: review.field_name,
  })),
  results: {
    CatalystSample: [
      {
        target_id: '11111111-1111-4111-8111-111111111111',
        target_type: 'CatalystSample',
        name: reviewToField(PILOT_PENDING_REVIEWS[0]),
        catalyst_type: reviewToField(PILOT_PENDING_REVIEWS[1]),
        metal_centers: reviewToField(PILOT_PENDING_REVIEWS[2], 'Co-N4'),
      },
    ],
    DFTSetting: [
      {
        target_id: '22222222-2222-4222-8222-222222222222',
        target_type: 'DFTSetting',
        convergence_settings: reviewToField(PILOT_PENDING_REVIEWS[3], 'force 0.02 eV/A'),
      },
    ],
    ElectrochemicalPerformance: [
      {
        target_id: '33333333-3333-4333-8333-333333333333',
        target_type: 'ElectrochemicalPerformance',
        rate: reviewToField(PILOT_PENDING_REVIEWS[4]),
      },
    ],
    DFTResult: [],
    MechanismClaim: [],
  },
};

const PILOT_AUDIT = {
  paper_id: PILOT_PAPER_ID,
  total_reviews: 5,
  active: 5,
  remapped: 0,
  stale: 0,
  ambiguous: 0,
  unresolved: 0,
  items: PILOT_PENDING_REVIEWS,
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

  // Debug logging for G3B tests (disabled)
  // if (pathname.includes('extraction') || pathname.includes('evidence') || pathname.includes('papers')) {
  //   console.log(`[mockApi] ${method} ${pathname}`);
  // }

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

  if (pathname === '/api/papers/paper-1' && method === 'DELETE') {
    return jsonResponse(route, { status: 'deleted', paper_id: 'paper-1' });
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
      headers: {
        'X-D3-Export-Safety-Gate': 'safe_verified_with_required_evidence',
        'X-D3-Export-Count': '1',
        'X-D3-Block-Count': '2',
      },
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

  // G3B Evidence Locator APIs
  if (pathname === '/api/papers/paper-1/evidence/locators' && method === 'GET') {
    return jsonResponse(route, []);
  }

  if (pathname === '/api/extraction/results/paper-1/evidence-locators' && method === 'GET') {
    return jsonResponse(route, []);
  }

  if (pathname.startsWith('/api/evidence/claims/') && pathname.endsWith('/locator') && method === 'GET') {
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }) });
  }

  if (pathname.match(/\/api\/papers\/[^/]+\/pdf$/) && method === 'GET') {
    return route.fulfill({ status: 204, body: '' });
  }

  if (pathname.match(/\/api\/papers\/[^/]+\/pdf$/) && method === 'HEAD') {
    return route.fulfill({ status: 200, headers: { 'content-type': 'application/pdf' } });
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
    await expect(page.locator('.step')).toContainText(['1 Topic', '2 Evidence search', '3 Evidence pack', '4 Draft', '5 Citation audit']);
    await expect(page.locator('button:has-text("Search evidence")')).toBeVisible();
    await expect(page.locator('button:has-text("Run Citation Audit")')).toBeVisible();
    await expect(page.locator('#evidencePanel')).toBeVisible();
    await expect(page.locator('body')).not.toContainText(/Export final|Final conclusion|Direct export/i);
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

    await page.route(/\/api\/extraction\/results\/paper-1\/validate$/, route => {
      return jsonResponse(route, {
        paper_id: 'paper-1',
        status: 'validated',
        validation_warnings: mockResults.validation_warnings
      });
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
    await expect(page.locator('#schemaForm')).toContainText('Pending human review / Not verified');

    const evidenceBtn = page.locator('button:has-text("原文证据 ▾")').first();
    await evidenceBtn.click();
    const evidenceTextarea = page.locator('textarea[data-field="catalyst"]').first();
    await expect(evidenceTextarea).toBeVisible();

    const valueInput = page.locator('input[data-field="value"][data-part="value"]').first();
    await valueInput.fill('-1.25');
    
    const saveBtn = page.locator('button:text-is("保存")').first();
    await saveBtn.click();
    await expect(page.locator('#toast')).toContainText('保存成功');
    expect(saveCalled).toBe(true);

    const verifyBtn = page.locator('button:text-is("人工确认")').first();
    await verifyBtn.click();
    await expect(page.locator('#toast')).toContainText('人工确认通过');
    expect(verifyCalled).toBe(true);

    const filterSelect = page.locator('#filterSelect');
    await filterSelect.selectOption('warnings');
    await page.waitForTimeout(200);

    await expect(page.locator('#schemaForm')).toContainText('Energy value seems unusually high');
    await expect(page.locator('input[data-field="catalyst"]')).toHaveCount(0);

    const scopeSummary = page.locator('#actionScopeSummary');
    await expect(scopeSummary).toContainText('当前 schema');
    await expect(scopeSummary).toContainText('DFTResult');
    await expect(scopeSummary).toContainText('当前过滤');
    await expect(scopeSummary).toContainText('当前可见记录');
    await expect(scopeSummary).toContainText('即将处理字段');
    await expect(scopeSummary).toContainText(/警告|warnings/);

    verifyCalled = false;
    await page.click('.footer-actions button:has-text("人工确认校验")');
    await expect(page.locator('#toast')).toContainText('批量人工确认通过成功');
    expect(verifyCalled).toBe(true);
  });

  test('D4-3C: pilot pending reviews stay visible, unverified, and blocked on workbench open', async ({ page }) => {
    const apiRequests = [];
    page.on('request', request => {
      const url = request.url();
      if (url.includes('/api/')) {
        apiRequests.push({
          method: request.method(),
          url,
          body: request.postData() || '',
        });
      }
    });

    await page.route(/\/api\/papers\?limit=200$/, route => jsonResponse(route, [PILOT_PAPER]));
    await page.route(new RegExp(`/api/papers/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_PAPER));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_EXTRACTION_RESULTS));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews$`), route => jsonResponse(route, PILOT_PENDING_REVIEWS));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews/audit$`), route => jsonResponse(route, PILOT_AUDIT));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/evidence-locators$`), route => jsonResponse(route, []));

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${PILOT_PAPER_ID}`);
    await page.waitForTimeout(800);

    await expect(page.locator('#paperMeta')).toContainText(PILOT_PAPER_ID);
    await expect(page.locator('#stabilitySummaryBox')).toContainText('5');

    await page.locator('#schemaSelect').selectOption('CatalystSample');
    await expect(page.locator('#schemaForm')).toContainText('name');
    await expect(page.locator('#schemaForm')).toContainText('catalyst_type');
    await expect(page.locator('#schemaForm')).toContainText('metal_centers');
    await expect(page.locator('#schemaForm .status-chip')).toHaveCount(3);
    await expect(page.locator('#schemaForm')).toContainText('Pending human review / Not verified');
    await expect(page.locator('#schemaForm')).toContainText('Evidence text for the heterogeneous catalyst is present.');
    await expect(page.locator('#schemaForm')).toContainText('missing_page');
    await expect(page.locator('#schemaForm')).toContainText('Exact PDF locator missing');
    await expect(page.locator('#schemaForm')).toContainText('Blocked from export/writing until exact locator + human verification');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    await page.locator('#schemaSelect').selectOption('DFTSetting');
    await expect(page.locator('#schemaForm')).toContainText('convergence_settings');
    await expect(page.locator('#schemaForm')).toContainText('DFT convergence evidence text is visible.');
    await expect(page.locator('#schemaForm')).toContainText('Pending human review / Not verified');
    await expect(page.locator('#schemaForm')).toContainText('unsafe_locator / no exact locator');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    await page.locator('#schemaSelect').selectOption('ElectrochemicalPerformance');
    await expect(page.locator('#schemaForm')).toContainText('rate');
    await expect(page.locator('#schemaForm')).toContainText('Rate-performance evidence text is visible.');
    await expect(page.locator('#schemaForm')).toContainText('Pending human review / Not verified');
    await expect(page.locator('#schemaForm')).toContainText('Blocked from export/writing until exact locator + human verification');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    const schemaText = await page.locator('#schemaForm').innerText();
    expect(schemaText).not.toMatch(/Human verified|Ready for export|Ready for writing|export-ready|writing-ready|AI approved|auto verified/i);

    const openingRequests = apiRequests.filter(request => request.url.includes(PILOT_PAPER_ID));
    expect(openingRequests.some(request => request.url.includes('/reviews/prepare'))).toBe(false);
    expect(openingRequests.some(request => request.url.includes('/reviews/mark-verified'))).toBe(false);
    expect(openingRequests.some(request => /reviewer_status"\s*:\s*"verified"|verified"\s*:\s*true/i.test(request.body))).toBe(false);
    expect(openingRequests.some(request => request.url.includes('/export') || request.url.includes('/writer/'))).toBe(false);
  });

  test('business flow: view DFT extraction results and evidence link', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('#dftTable')).toContainText('Li2S4');
    await page.click('button:has-text("evidence link")');
    await expect(page.locator('#evidenceDetail')).toContainText('evidence_text');
  });

  test('business flow: DFT export displays safety headers', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('.export-note')).toContainText('Human verified + required evidence');
    await expect(page.locator('.export-note')).toContainText('blocked rows 不会导出');
    await expect(page.locator('#dftTable')).toContainText('Li2S4');

    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportCSV()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('safe_verified_with_required_evidence');
    await expect(page.locator('#exportSafetyStatus')).toContainText('exported: 1');
    await expect(page.locator('#exportSafetyStatus')).toContainText('blocked: 2');
  });

  test('business flow: DFT export empty state shows correct wording and status', async ({ page }) => {
    await page.route(/\/api\/papers\/export\/csv/, route => {
      return route.fulfill({
        status: 200,
        contentType: 'text/csv',
        headers: {
          'X-D3-Export-Safety-Gate': 'enforced',
          'X-D3-Export-Count': '0',
          'X-D3-Block-Count': '0',
        },
        body: 'paper_id,title,value\n',
      });
    });

    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('.export-note')).toContainText('Human verified + required evidence');
    
    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportCSV()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('enforced');
    await expect(page.locator('#exportSafetyStatus')).toContainText('exported: 0');
    await expect(page.locator('#exportSafetyStatus')).toContainText('blocked: 0');
    await expect(page.locator('#exportSafetyStatus')).toContainText('没有可导出的 Human verified + required evidence 记录');
    
    // assert toast
    await expect(page.locator('#toast')).toContainText('0 rows exported / 没有记录被导出');
    
    // check no misleading wording
    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toMatch(/Export successful|导出成功|已导出|全部完成|Final export|Final conclusion|Direct export/i);
  });

  test('business flow: DFT export blocked-only state shows correct wording and status', async ({ page }) => {
    await page.route(/\/api\/papers\/export\/csv/, route => {
      return route.fulfill({
        status: 200,
        contentType: 'text/csv',
        headers: {
          'X-D3-Export-Safety-Gate': 'enforced',
          'X-D3-Export-Count': '0',
          'X-D3-Block-Count': '3',
        },
        body: 'paper_id,title,value\n',
      });
    });

    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('.export-note')).toContainText('Human verified + required evidence');
    
    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportCSV()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('enforced');
    await expect(page.locator('#exportSafetyStatus')).toContainText('exported: 0');
    await expect(page.locator('#exportSafetyStatus')).toContainText('blocked: 3');
    await expect(page.locator('#exportSafetyStatus')).toContainText('没有 safe rows 被导出');
    await expect(page.locator('#exportSafetyStatus')).toContainText('缺少 Human verified 或 required evidence');

    // assert toast
    await expect(page.locator('#toast')).toContainText('没有 safe rows 被导出 (3 blocked)');
    
    // check no misleading wording
    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toMatch(/Export successful|导出成功|已导出|全部完成|Final export|Final conclusion|Direct export/i);
  });

  test('business flow: AI candidate materialize uses explicit scope contracts', async ({ page }) => {
    const runPayload = {
      id: 'run-1',
      source: 'manual',
      source_label: '外部 AI 候选建议',
      created_at: '2026-05-26T12:00:00',
      mapping_status: 'normalized',
      candidates: [
        {
          id: 'candidate-1',
          candidate_type: 'correction',
          status: 'pending',
          confidence: 0.86,
          materialized_target_type: null,
          normalized_payload: { field_name: 'dft_results', target_path: 'dft_results:target-1:value' },
        },
      ],
    };
    const materializePayloads = [];
    const materializeDialogs = [];

    await page.route(/\/api\/external-analysis\/runs(\?|$)/, route => jsonResponse(route, [runPayload]));
    await page.route(/\/api\/external-analysis\/runs\/run-1\/materialize$/, async route => {
      materializePayloads.push(JSON.parse(route.request().postData() || '{}'));
      return jsonResponse(route, { ok: true });
    });
    page.on('dialog', async dialog => {
      expect(dialog.message()).toContain('这不是人工 verified');
      materializeDialogs.push(dialog.message());
      await dialog.accept();
    });

    async function openReviewCandidates() {
      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.click('button[data-tab="review"]');
      await expect(page.locator('#externalRuns')).toContainText('外部 AI 候选建议');
      await page.click('button:has-text("展开候选项")');
      await expect(page.locator('.candidate-card').first()).toBeVisible();
    }

    await openReviewCandidates();
    const reviewAreaText = await page.locator('#tab-review').innerText();
    expect(reviewAreaText).toMatch(/AI 建议候选/);
    expect(reviewAreaText).toMatch(/生成待确认记录/);
    expect(reviewAreaText).not.toMatch(/写回数据库|一键全部写回数据库|AI 审核|AI 分析结果|审核结果|已验证|已校验/);

    await page.click('.candidate-card button:has-text("生成待确认记录")');
    await expect.poll(() => materializePayloads.length).toBe(1);
    expect(materializePayloads[0]).toEqual({ candidate_ids: ['candidate-1'], created_by: 'web_user' });
    expect(materializePayloads[0].candidate_ids).not.toEqual([]);

    await openReviewCandidates();
    await page.click('button:has-text("选中生成待确认记录")');
    await expect.poll(() => materializePayloads.length).toBe(1);

    await openReviewCandidates();
    await page.check('.candidate-select[value="candidate-1"]');
    await page.click('button:has-text("选中生成待确认记录")');
    await expect.poll(() => materializePayloads.length).toBe(2);
    expect(materializePayloads[1]).toEqual({ candidate_ids: ['candidate-1'], created_by: 'web_user' });
    expect(materializePayloads[1].candidate_ids).not.toEqual([]);

    await openReviewCandidates();
    await page.click('button:has-text("批量生成待确认记录")');
    await expect.poll(() => materializePayloads.length).toBe(3);
    expect(materializePayloads[2]).toEqual({ explicit_all: true, created_by: 'web_user' });
    expect(materializePayloads[2]).not.toHaveProperty('candidate_ids');
    expect(materializeDialogs).toHaveLength(3);
  });

  test('business flow: literature library opens extraction job center', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('button:has-text("解析任务")');
    await expect(page.locator('#acquisitionResult')).toContainText('解析任务中心');
  });

  test('business flow: literature library UX is Chinese, clamps DOI, and exposes key entries', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-card');
    await page.waitForTimeout(500);

    await expect(page.locator('#paperMeta')).toContainText('10.1000/primary-doi');
    await expect(page.locator('#paperMeta')).not.toContainText('10.2000/reference-doi');
    await expect(page.locator('#paperMeta')).toContainText('检测到多个 DOI，可能需要重新解析元数据');
    await expect(page.locator('#summaryContent')).toContainText('PDF 证据定位');
    await expect(page.locator('#summaryContent')).toContainText('只在有精确页码时跳转到 PDF 页');

    await page.click('button:has-text("更多操作")');
    await expect(page.locator('#paperMoreMenu')).toContainText('删除当前文献');

    const visibleText = await page.locator('body').innerText();
    expect(visibleText).not.toMatch(/Extraction Jobs|Extraction Job Center|source label|manual|unknown/);
  });

  test('business flow: unconfigured internal AI shows Chinese settings guide', async ({ page }) => {
    await page.route(/\/api\/external-analysis\/papers\/paper-1\/internal-parse$/, route => {
      return route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Internal AI is not configured' }),
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-card');
    await page.click('button[data-tab="review"]');
    await page.click('button:has-text("生成 AI 候选项")');

    await expect(page.locator('#internalAIConfigGuide')).toContainText('网页内 AI 尚未配置，请到 设置 -> API 配置 中填写 Writer API Key / Base URL / Model。');
    await expect(page.locator('#internalAIConfigGuide button:has-text("打开设置页")')).toBeVisible();
  });

  test('business flow: delete current paper opens confirmation and clears selection', async ({ page }) => {
    let deleted = false;

    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, deleted ? [] : PAPERS);
      }
      return route.fallback();
    });
    await page.route(/\/api\/papers\/paper-1$/, route => {
      if (route.request().method() === 'DELETE') {
        deleted = true;
        return jsonResponse(route, { status: 'deleted', paper_id: 'paper-1' });
      }
      return jsonResponse(route, PAPER_DETAIL);
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-card');
    await page.click('button:has-text("更多操作")');
    await page.click('#paperMoreMenu button:has-text("删除当前文献")');

    await expect(page.locator('#deletePaperDialog')).toBeVisible();
    await expect(page.locator('#deletePaperDialog')).toContainText('Test Paper for Smoke Validation');
    await expect(page.locator('#deletePaperDialog')).toContainText('默认只删除数据库记录，不删除原 PDF 文件');
    await expect(page.locator('#deletePaperPdfFiles')).not.toBeChecked();
    await expect(page.locator('#deletePaperDerivedFiles')).not.toBeChecked();

    await page.click('#deletePaperDialog button:has-text("确认删除")');
    await expect(page.locator('#deletePaperDialog')).not.toBeVisible();
    await expect(page.locator('#paperList')).toContainText('当前条件下没有文献');
    expect(deleted).toBe(true);
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

    test('9. Orphan stale/unknown review renders properly in dedicated section without safe verified state and respects filters', async ({ page }) => {
      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, EXTRACTION_RESULTS);
      });

      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/audit$/, route => {
        return jsonResponse(route, {
          paper_id: 'paper-1',
          total_reviews: 2,
          active: 0,
          remapped: 0,
          stale: 1,
          ambiguous: 0,
          unresolved: 0,
          unknown: 1,
          items: [
            {
              target_id: 'old-target-1',
              target_type: 'DFTResult',
              field_name: 'value',
              target_resolution_status: 'stale',
              reviewer_status: 'verified',
              target_label: 'Pt(111)',
              field_path: 'DFTResult.value',
              reviewed_value: -9.99,
              unit: 'eV',
              evidence_text: 'old evidence showing -9.99 eV',
              remapped_from_target_id: 'original-id-1',
              target_fingerprint: 'fingerprint-123'
            },
            {
              target_id: 'old-target-2',
              target_type: 'DFTResult',
              field_name: 'value',
              target_resolution_status: 'unknown',
              reviewer_status: 'corrected',
              target_label: 'Pd(100)',
              field_path: 'DFTResult.value',
              reviewed_value: -8.88,
              unit: 'eV',
              evidence_text: 'unknown status evidence showing -8.88 eV',
              remapped_from_target_id: 'original-id-2',
              target_fingerprint: 'fingerprint-456'
            }
          ]
        });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const orphanTitle = page.locator('h3:has-text("需要重新确认的旧人工校验记录")');
      await expect(orphanTitle).toBeVisible();

      const staleCard = page.locator('.field-container:has-text("old-target-1")');
      await expect(staleCard).toBeVisible();
      await expect(staleCard).toContainText('已失效');
      await expect(staleCard).toContainText('需重新确认');
      await expect(staleCard).not.toContainText('已校验');
      await expect(staleCard).toContainText('Pt(111)');
      await expect(staleCard).toContainText('DFTResult.value');
      await expect(staleCard).toContainText('-9.99 eV');
      await expect(staleCard).toContainText('old evidence showing -9.99 eV');
      await expect(staleCard).toContainText('这是一条旧目标人工校验记录，当前抽取结果中未能安全匹配。请在当前字段中重新确认后保存为新的人工确认。');

      const unknownCard = page.locator('.field-container:has-text("old-target-2")');
      await expect(unknownCard).toBeVisible();
      await expect(unknownCard).toContainText('未知');
      await expect(unknownCard).toContainText('需重新确认');
      await expect(unknownCard).not.toContainText('已校验');
      await expect(unknownCard).toContainText('Pd(100)');
      await expect(unknownCard).toContainText('-8.88 eV');
      await expect(unknownCard).toContainText('unknown status evidence showing -8.88 eV');

      const filterSelect = page.locator('#filterSelect');

      await filterSelect.selectOption('active_remapped');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container:has-text("old-target-1")')).toHaveCount(0);
      await expect(page.locator('.field-container:has-text("old-target-2")')).toHaveCount(0);

      await filterSelect.selectOption('stale_ambiguous_unresolved');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container:has-text("old-target-1")')).toHaveCount(1);
      await expect(page.locator('.field-container:has-text("old-target-2")')).toHaveCount(0);

      await filterSelect.selectOption('needs_reconfirmation');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container:has-text("old-target-1")')).toHaveCount(1);
      await expect(page.locator('.field-container:has-text("old-target-2")')).toHaveCount(1);
    });

    test('10. Save success triggers strict sequential refresh sequence', async ({ page }) => {
      const apiCalls = [];

      await page.route('**/api/extraction/results/paper-1**', async (route, request) => {
        const url = request.url();
        const method = request.method();
        
        if (url.endsWith('/reviews/save') && method === 'POST') {
          apiCalls.push('SAVE');
          return jsonResponse(route, { status: 'success' });
        } else if (url.endsWith('/validate') && method === 'POST') {
          apiCalls.push('VALIDATE');
          return jsonResponse(route, EXTRACTION_RESULTS);
        } else if (url.endsWith('/reviews/audit') && method === 'GET') {
          apiCalls.push('AUDIT');
          return jsonResponse(route, {
            paper_id: 'paper-1',
            total_reviews: 0,
            active: 0,
            remapped: 0,
            stale: 0,
            ambiguous: 0,
            unresolved: 0,
            items: []
          });
        } else if (url.endsWith('/paper-1') && method === 'GET') {
          apiCalls.push('RESULTS');
          return jsonResponse(route, EXTRACTION_RESULTS);
        }
        
        return route.continue();
      });

      await page.route(/\/api\/papers\/paper-1$/, route => {
        return jsonResponse(route, { id: 'paper-1', title: 'Paper 1' });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      apiCalls.length = 0;

      await page.click('button:has-text("保存")');
      await page.waitForTimeout(600);

      expect(apiCalls).toContain('RESULTS');
      expect(apiCalls).toContain('VALIDATE');
      expect(apiCalls).toContain('AUDIT');

      const resultsIndex = apiCalls.indexOf('RESULTS');
      const validateIndex = apiCalls.indexOf('VALIDATE');
      const auditIndex = apiCalls.indexOf('AUDIT');

      expect(resultsIndex).toBeLessThan(validateIndex);
      expect(validateIndex).toBeLessThan(auditIndex);
    });

    test('11. Orphan review with empty/non-standard target_resolution_status is normalized to unknown', async ({ page }) => {
      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, EXTRACTION_RESULTS);
      });

      await page.route(/\/api\/extraction\/results\/paper-1\/reviews\/audit$/, route => {
        return jsonResponse(route, {
          paper_id: 'paper-1',
          total_reviews: 1,
          active: 0,
          remapped: 0,
          stale: 0,
          ambiguous: 0,
          unresolved: 0,
          unknown: 0,
          items: [
            {
              target_id: "old-target-empty-status",
              target_type: "DFTResult",
              field_name: "value",
              target_resolution_status: "",
              reviewer_status: "verified",
              target_label: "Ni-N4",
              field_path: "DFTResult.value",
              reviewed_value: -7.77,
              unit: "eV",
              evidence_text: "empty status should be treated as unknown"
            }
          ]
        });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(500);

      const orphanTitle = page.locator('h3:has-text("需要重新确认的旧人工校验记录")');
      await expect(orphanTitle).toBeVisible();

      const card = page.locator('.field-container:has-text("old-target-empty-status")');
      await expect(card).toBeVisible();

      await expect(card).toContainText('未知');
      await expect(card).toContainText('需重新确认');
      await expect(card).not.toContainText('已校验');

      const summaryBox = page.locator('#stabilitySummaryBox');
      await expect(summaryBox).toContainText('未知: 1');

      const filterSelect = page.locator('#filterSelect');
      await filterSelect.selectOption('needs_reconfirmation');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container:has-text("old-target-empty-status")')).toHaveCount(1);

      await filterSelect.selectOption('active_remapped');
      await page.waitForTimeout(200);
      await expect(page.locator('.field-container:has-text("old-target-empty-status")')).toHaveCount(0);
    });
  });

  // ── G3B Evidence Locator / PDF Evidence Jump UI Tests ──

  test.describe('G3B Evidence Locator & PDF Evidence Jump UI', () => {
    const LOCATOR_EXACT = {
      id: 'loc-1',
      paper_id: 'paper-1',
      claim_id: null,
      chunk_id: 'chunk-1',
      target_type: 'DFTResult',
      target_id: 'target-1',
      field_name: 'value',
      evidence_text: 'The adsorption energy is -1.23 eV on Fe-N4.',
      page: 5,
      bbox: { x0: 72, y0: 144, x1: 360, y1: 180, coordinate_system: 'pdf_points' },
      section: 'Results',
      source_type: 'section',
      locator_status: 'exact_page',
      provenance_level: 'exact_pdf_page',
      can_jump_to_pdf_page: true,
      can_highlight_in_pdf: false,
      locator_confidence: 0.95,
      parser_source: 'docling',
      warning_reason: null,
    };

    const LOCATOR_PAGE_ONLY = {
      ...LOCATOR_EXACT,
      id: 'loc-2',
      field_name: 'catalyst',
      evidence_text: 'Fe-N4 catalyst used.',
      page: 5,
      bbox: null,
      locator_status: 'exact_page',
      provenance_level: 'exact_pdf_page',
      can_jump_to_pdf_page: true,
      can_highlight_in_pdf: false,
      locator_confidence: 0.7,
    };

    const LOCATOR_MISSING = {
      ...LOCATOR_EXACT,
      id: 'loc-3',
      field_name: 'adsorbate',
      evidence_text: 'Li2S4 adsorbate.',
      page: null,
      bbox: null,
      locator_status: 'missing_locator',
      provenance_level: 'unavailable',
      can_jump_to_pdf_page: false,
      can_highlight_in_pdf: false,
      locator_confidence: 0.0,
    };

    const LOCATOR_TEXT_ONLY = {
      ...LOCATOR_EXACT,
      id: 'loc-4',
      field_name: 'energy_type',
      evidence_text: 'adsorption energy type.',
      page: null,
      bbox: null,
      locator_status: 'text_only',
      provenance_level: 'text_evidence_only',
      can_jump_to_pdf_page: false,
      can_highlight_in_pdf: false,
      locator_confidence: 0.3,
    };

    const LOCATOR_NEEDS_REPARSE = {
      ...LOCATOR_EXACT,
      id: 'loc-5',
      field_name: 'reaction_step',
      evidence_text: 'Li2S4 adsorption step.',
      page: null,
      bbox: null,
      locator_status: 'missing_page',
      provenance_level: 'text_evidence_only',
      can_jump_to_pdf_page: false,
      can_highlight_in_pdf: false,
      locator_confidence: 0.1,
    };

    test('A. Paper detail locator panel: exact_page, missing locator statuses', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1\/evidence\/locators$/, route => {
        return jsonResponse(route, [LOCATOR_EXACT, LOCATOR_PAGE_ONLY, LOCATOR_MISSING]);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.waitForTimeout(800);

      const panel = page.locator('#evidenceLocatorsPanel');
      await expect(panel).toBeVisible();

      // exact_page: safe page jump text, no page-internal box claim
      await expect(panel).toContainText('跳转到第 5 页');

      // second exact_page locator also jumps to page 5
      await expect(panel.locator('button:has-text("跳转到第 5 页")')).toHaveCount(2);

      // missing: degradation hint
      await expect(panel).toContainText('暂无可用 PDF 定位');
    });

    test('B. Workbench field-level locator badge: exact_page, missing_page', async ({ page }) => {
      const mockWithLocators = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [
            {
              target_id: 'target-1',
              target_type: 'DFTResult',
              catalyst: {
                value: 'Fe-N4',
                unit: null,
                evidence_text: 'Fe-N4 catalyst.',
                source_section: 'Results',
                page_span: {},
                confidence: 0.8,
                evidence_locator: {
                  locator_status: 'exact_page',
                  page: 5,
                  bbox: null,
                  evidence_text: 'Fe-N4 catalyst used.',
                  paper_id: 'paper-1',
                  can_jump_to_pdf_page: true,
                }
              },
              adsorbate: {
                value: 'Li2S4',
                unit: null,
                evidence_text: 'Li2S4 adsorption.',
                source_section: 'Results',
                page_span: {},
                confidence: 0.9,
                evidence_locator: {
                  locator_status: 'missing_page',
                  page: null,
                  bbox: null,
                  evidence_text: 'Li2S4 adsorbate.',
                  paper_id: 'paper-1',
                  can_jump_to_pdf_page: false,
                }
              },
              energy_type: { value: 'adsorption_energy', unit: null, evidence_text: 'adsorption energy.', source_section: 'Results', page_span: {}, confidence: 0.9 },
              value: {
                value: -1.23,
                unit: 'eV',
                evidence_text: 'The adsorption energy is -1.23 eV.',
                source_section: 'Results',
                page_span: {},
                confidence: 0.91,
                evidence_locator: {
                  locator_status: 'exact_page',
                  page: 5,
                  bbox: { x0: 72, y0: 144, x1: 360, y1: 180, coordinate_system: 'pdf_points' },
                  evidence_text: 'The adsorption energy is -1.23 eV on Fe-N4.',
                  paper_id: 'paper-1',
                  can_jump_to_pdf_page: true,
                }
              },
              reaction_step: { value: 'Li2S4 adsorption', unit: null, evidence_text: 'Li2S4 adsorption step.', source_section: 'Results', page_span: {}, confidence: 0.85 },
            },
          ],
        },
      };

      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, mockWithLocators);
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(1500);

      // Check locator status badges exist
      const exactBadge = page.locator('.locator-status-badge[data-locator-status="exact_page"]');
      await expect(exactBadge.first()).toBeVisible();
      await expect(exactBadge.first()).toContainText('exact_page');

      const pageOnlyBadge = page.locator('.locator-status-badge[data-locator-status="exact_page"]');
      await expect(pageOnlyBadge.first()).toBeVisible();
      await expect(pageOnlyBadge.first()).toContainText('exact_page');

      const needsReparseBadge = page.locator('.locator-status-badge[data-locator-status="missing_page"]');
      await expect(needsReparseBadge).toBeVisible();
      await expect(needsReparseBadge).toContainText('missing_page');

      // exact_page: has page jump button
      const viewOriginalBtn = page.locator('button:has-text("跳转到第 5 页")');
      await expect(viewOriginalBtn.first()).toBeAttached();

      // missing_page: no precise jump
      await expect(page.locator('#schemaForm')).toContainText('仅有证据文本，暂无 PDF 页码定位');
    });

    test('C. Exact bbox click opens PDF viewer with evidence panel and page indicator', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1\/evidence\/locators$/, route => {
        return jsonResponse(route, [LOCATOR_EXACT]);
      });

      await page.route(/\/api\/papers\/paper-1\/pdf$/, route => {
        const method = route.request().method();
        if (method === 'HEAD') {
          return route.fulfill({ status: 200, headers: { 'content-type': 'application/pdf' } });
        }
        return route.fulfill({
          status: 200,
          contentType: 'application/pdf',
          body: Buffer.from('%PDF-1.4 mock'),
        });
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.waitForTimeout(800);

      const panel = page.locator('#evidenceLocatorsPanel');
      await expect(panel).toContainText('跳转到第 5 页');

      // Click the button
      await page.locator('#evidenceLocatorsPanel button:has-text("跳转到第 5 页")').click();
      await page.waitForTimeout(800);

      // PDF viewer overlay should be visible
      const overlay = page.locator('#pdfViewerOverlay');
      await expect(overlay).toBeVisible();

      // iframe src must be set and point to PDF endpoint
      const iframe = page.locator('#pdfViewerIframe');
      const src = await iframe.getAttribute('src');
      expect(src).toContain('/api/papers/paper-1/pdf');
      expect(src).toContain('page=5');

      // Page indicator must show target page
      const pageIndicator = page.locator('#pdfViewerPageIndicator');
      await expect(pageIndicator).toContainText('5');

      // Evidence panel must show page locator info
      const evidencePanel = page.locator('#pdfViewerEvidencePanel');
      await expect(evidencePanel).toContainText('PDF 页码定位');
      await expect(evidencePanel).toContainText('当前版本不提供 PDF 页面内框选');

      // PDF unavailable message must be hidden
      const unavailable = page.locator('#pdfViewerUnavailable');
      await expect(unavailable).not.toBeVisible();

      // No fake highlight overlay content
      const highlight = page.locator('#pdfHighlightOverlay');
      await expect(highlight.locator('div')).toHaveCount(0);

      // Close the viewer
      await page.locator('#pdfViewerOverlay button:has-text("关闭")').click();
      await page.waitForTimeout(300);
      await expect(overlay).not.toBeVisible();
    });

    test('D. Exact_page without bbox opens PDF viewer without fake box', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1\/evidence\/locators$/, route => {
        return jsonResponse(route, [LOCATOR_PAGE_ONLY]);
      });

      await page.route(/\/api\/papers\/paper-1\/pdf$/, route => {
        const method = route.request().method();
        if (method === 'HEAD') {
          return route.fulfill({ status: 200, headers: { 'content-type': 'application/pdf' } });
        }
        return route.fulfill({
          status: 200,
          contentType: 'application/pdf',
          body: Buffer.from('%PDF-1.4 mock'),
        });
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.waitForTimeout(800);

      const panel = page.locator('#evidenceLocatorsPanel');
      await expect(panel).toContainText('跳转到第 5 页');

      await page.locator('#evidenceLocatorsPanel button:has-text("跳转到第 5 页")').click();
      await page.waitForTimeout(800);

      const overlay = page.locator('#pdfViewerOverlay');
      await expect(overlay).toBeVisible();

      // iframe src must be set
      const iframe = page.locator('#pdfViewerIframe');
      const src = await iframe.getAttribute('src');
      expect(src).toContain('/api/papers/paper-1/pdf');

      // Page indicator must show target page
      const pageIndicator = page.locator('#pdfViewerPageIndicator');
      await expect(pageIndicator).toContainText('5');

      // Evidence panel must show page locator status
      const evidencePanel = page.locator('#pdfViewerEvidencePanel');
      await expect(evidencePanel).toContainText('PDF 页码定位');

      // No bbox highlight overlay
      const highlight = page.locator('#pdfHighlightOverlay .pdf-bbox-highlight');
      await expect(highlight).toHaveCount(0);

      await page.locator('#pdfViewerOverlay button:has-text("关闭")').click();
    });

    test('E. Missing/text_only/missing_page do not show page jump or fake bbox overlay', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1\/evidence\/locators$/, route => {
        return jsonResponse(route, [LOCATOR_MISSING, LOCATOR_TEXT_ONLY, LOCATOR_NEEDS_REPARSE]);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.waitForTimeout(800);

      const panel = page.locator('#evidenceLocatorsPanel');
      await expect(panel).toBeVisible();

      // No "高亮" text should appear
      const highlightButtons = page.locator('#evidenceLocatorsPanel button:has-text("高亮")');
      await expect(highlightButtons).toHaveCount(0);

      // No fake bbox overlay
      const fakeOverlay = page.locator('.pdf-bbox-highlight');
      await expect(fakeOverlay).toHaveCount(0);

      // Degradation messages
      await expect(panel).toContainText('仅有证据文本，暂无 PDF 页码定位');
      await expect(panel).toContainText('暂无可用 PDF 定位');
    });

    test('E2. PDF not available shows unavailable message and no fake highlight', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1\/evidence\/locators$/, route => {
        return jsonResponse(route, [LOCATOR_EXACT]);
      });

      // PDF endpoint returns 404
      await page.route(/\/api\/papers\/paper-1\/pdf$/, route => {
        const method = route.request().method();
        if (method === 'HEAD') {
          return route.fulfill({ status: 404, headers: {} });
        }
        return route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'PDF not uploaded or unavailable' }),
        });
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.waitForTimeout(800);

      const panel = page.locator('#evidenceLocatorsPanel');
      await expect(panel).toContainText('跳转到第 5 页');

      // Click the button
      await page.locator('#evidenceLocatorsPanel button:has-text("跳转到第 5 页")').click();
      await page.waitForTimeout(800);

      // Overlay should be visible
      const overlay = page.locator('#pdfViewerOverlay');
      await expect(overlay).toBeVisible();

      // PDF unavailable message should be visible
      const unavailable = page.locator('#pdfViewerUnavailable');
      await expect(unavailable).toBeVisible();
      await expect(unavailable).toContainText('PDF 尚未上传或不可预览');

      // PDF content area should be hidden
      const pdfContent = page.locator('#pdfViewerContent');
      await expect(pdfContent).not.toBeVisible();

      // No fake highlight
      const fakeOverlay = page.locator('.pdf-bbox-highlight');
      await expect(fakeOverlay).toHaveCount(0);

      // No iframe loading
      const iframe = page.locator('#pdfViewerIframe');
      const src = await iframe.getAttribute('src');
      expect(src).toBeFalsy();

      await page.locator('#pdfViewerOverlay button:has-text("关闭")').click();
    });

    test('F. API failure graceful degradation - 404/500', async ({ page }) => {
      await page.route(/\/api\/papers\/paper-1\/evidence\/locators$/, route => {
        return route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Not Found' }),
        });
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(500);
      await page.click('.paper-card');
      await page.waitForTimeout(800);

      // Page should not crash
      const panel = page.locator('#evidenceLocatorsPanel');
      await expect(panel).toBeVisible();
      await expect(panel).toContainText('证据定位暂不可用');

      // Original paper detail still works
      await expect(page.locator('#summaryContent')).toBeVisible();
    });

    test('F2. Workbench locator API 500 graceful degradation', async ({ page }) => {
      await page.route(/\/api\/extraction\/results\/paper-1\/evidence-locators$/, route => {
        return route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Internal Server Error' }),
        });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(600);

      // Page should not crash
      await expect(page.locator('#schemaForm')).toBeVisible();
      await expect(page.locator('#schemaForm')).toContainText('value');
    });

    test('G. G2B regression: review target, review stability, audit tests still pass', async ({ page }) => {
      // This is a basic regression check - make sure G2B features are still intact
      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(600);

      // Stability summary box exists
      const summaryBox = page.locator('#stabilitySummaryBox');
      await expect(summaryBox).toBeVisible();

      // Review status badges still exist
      const statusChips = page.locator('.status-chip');
      await expect(statusChips.first()).toBeVisible();

      // Filter still works
      const filterSelect = page.locator('#filterSelect');
      await expect(filterSelect).toBeVisible();

      // Save and verify buttons still exist
      await expect(page.locator('.footer-actions button:has-text("Save")')).toBeVisible();
      await expect(page.locator('.footer-actions button:has-text("人工确认校验")')).toBeVisible();
    });

    test('G2. Locator warnings do not override G2B review_target warnings', async ({ page }) => {
      const mockWithWarnings = {
        ...EXTRACTION_RESULTS,
        results: {
          ...EXTRACTION_RESULTS.results,
          DFTResult: [{
            ...EXTRACTION_RESULTS.results.DFTResult[0],
            value: {
              ...EXTRACTION_RESULTS.results.DFTResult[0].value,
              review: {
                target_resolution_status: 'stale',
                reviewer_status: 'verified',
                target_label: 'Fe-N4',
                field_path: 'DFTResult.value',
                reviewed_value: -1.23,
                unit: 'eV',
              }
            }
          }]
        },
        validation_warnings: [
          {
            severity: 'warning',
            code: 'review_target_stale',
            message: 'Review target is stale and needs reconfirmation',
            target_type: 'DFTResult',
            target_id: 'target-1',
            field: 'value'
          },
          {
            severity: 'info',
            code: 'evidence_locator_missing_page',
            message: 'Evidence locator is missing page information',
            target_type: 'DFTResult',
            target_id: 'target-1',
            field: 'value'
          }
        ],
      };

      await page.route(/\/api\/extraction\/results\/paper-1$/, route => {
        return jsonResponse(route, mockWithWarnings);
      });

      await page.route(/\/api\/extraction\/results\/paper-1\/validate$/, route => {
        return jsonResponse(route, {
          paper_id: 'paper-1',
          status: 'validated',
          validation_warnings: mockWithWarnings.validation_warnings
        });
      });

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
      await page.waitForTimeout(600);

      // Review target stale warning must take priority
      const warningBox = page.locator('#warningsBox');
      await expect(warningBox).toContainText('review_target_stale');

      // Locator warning also visible
      await expect(warningBox).toContainText('evidence_locator_missing_page');

      // G2B status badge must show "需重新确认" not "已校验"
      const fieldContainer = page.locator('.field-container:has-text("value")');
      await expect(fieldContainer).toContainText('需重新确认');
    });
  });
});
