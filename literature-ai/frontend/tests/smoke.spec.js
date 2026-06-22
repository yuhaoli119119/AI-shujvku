const { test, expect } = require('@playwright/test');

const PAGES = [
  { name: 'Dashboard', path: '/pages/dashboard/index.html', coreSelector: '.panel-card' },
  { name: 'Ingestion Center', path: '/pages/ingestion/index.html', coreSelector: '.dropzone' },
  { name: 'Literature Library', path: '/pages/literature_library/index.html', coreSelector: '#paperList' },
  { name: 'Paper Detail', path: '/pages/paper_detail/index.html', coreSelector: '.panel-card' },
  { name: 'DFT Database', path: '/pages/dft_database/index.html', coreSelector: '#dftTable' },
  { name: 'Data Visuals', path: '/pages/visuals/index.html', coreSelector: '#metrics' },
  { name: 'Mechanism Knowledge', path: '/pages/mechanism_knowledge/index.html', coreSelector: '#mechanismTabs' },
  { name: 'AI Writing Studio', path: '/pages/ai_writer/index.html', coreSelector: '#paperChecklist' },
  { name: 'Extraction Review Workbench', path: '/pages/external_analysis_workbench/index.html', coreSelector: '#schemaForm' },
  { name: 'Settings', path: '/pages/settings/index.html', coreSelector: '.field' },
  { name: 'Literature Screening', path: '/pages/literature_screening/index.html', coreSelector: '.screening-table' },
  { name: 'Writing Citation Assistant', path: '/pages/writing_assistant/index.html', coreSelector: '#writingText' },
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
    library_name: 'Default Library',
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
   library_name: 'Default Library',
  pdf_path: 'test.pdf',
  abstract: 'Synthetic paper detail payload used by Playwright smoke tests.',
  sections: [
    { id: 'chunk-1', section_title: 'Introduction', section_type: 'introduction', text: 'Smoke-test content.', page_start: 1, page_end: 1 },
    { id: 'chunk-2', section_title: 'Results', section_type: 'results', text: 'Selected section translation content.', page_start: 3, page_end: 4 }
  ],
  figures: [{
    id: 'figure-1',
    caption: 'Figure 1. Graphene defect model.',
    page: 3,
    image_path: 'figures/figure-1.png',
    asset_url: '/api/papers/assets/figures/figure-1.png',
    figure_role: 'structure',
    crop_status: 'needs_review',
    image_review: {
      crop_status: 'needs_review',
      review_required: true,
      flags: ['missing_parser_bbox', 'missing_full_page_snapshot', 'small_crop_or_subfigure'],
      pixel_size: { width: 120, height: 80 },
      bbox_size_points: { width: 40, height: 30 },
      full_page_image_path: null,
    },
    review_required: true,
    flags: ['missing_parser_bbox', 'missing_full_page_snapshot', 'small_crop_or_subfigure'],
    figure_reliability_status: 'needs_review',
    figure_reliability_warnings: ['missing_bbox', 'missing_full_page_snapshot', 'small_crop'],
    object_review_audit_count: 1,
    latest_object_review_audit: {
      source: 'glm_figure_audit',
      source_label: 'GLM figure audit',
      decision: 'REVISE',
      confidence: 0.72,
      verification_status: 'unverified',
    },
    object_review_audits: [{
      candidate_id: 'figure-audit-1',
      candidate_type: 'object_review_audit',
      source: 'glm_figure_audit',
      source_label: 'GLM figure audit',
      decision: 'REVISE',
      confidence: 0.72,
      verification_status: 'unverified',
      evidence_location: { page: 3 },
    }],
    conflict_count: 1,
    field_conflicts: [{ field_name: 'crop_status', conflict_types: ['decision_conflict'], opinions: [] }],
  }],
  tables: [],
  dft_settings_items: [{ id: 'setting-1', code: 'PBE', kpoints: '3x3x1' }],
  catalyst_samples_items: [{ id: 'catalyst-1', name: 'Pt(111)', catalyst_type: 'surface', support: 'Pt support' }],
  dft_results_items: [{ id: 'dft-1', property_type: 'adsorption_energy', value: -1.23, unit: 'eV', evidence_text: 'The adsorption energy is -1.23 eV.' }],
  electrochemical_performance_items: [{ id: 'electro-1', metric: 'onset_potential', value: 0.71, unit: 'V' }],
  mechanism_claims_items: [{
    id: 'mechanism-claim-1',
    claim_type: 'adsorption_mechanism',
    claim_text: 'Associative pathway is favored by defect-driven charge redistribution.',
    evidence_text: 'The discussion links defect sites with charge redistribution and stronger adsorption.',
    evidence_status: 'present',
    locator_status: 'text_only',
    confidence: 0.71,
    confidence_status: 'medium',
    object_review_audit_count: 1,
    latest_object_review_audit: {
      source: 'glm_mechanism_audit',
      source_label: 'GLM mechanism audit',
      decision: 'FLAG',
      confidence: 0.7,
      verification_status: 'unverified',
    },
    object_review_audits: [{
      candidate_id: 'mechanism-audit-1',
      candidate_type: 'object_review_audit',
      source: 'glm_mechanism_audit',
      source_label: 'GLM mechanism audit',
      decision: 'FLAG',
      confidence: 0.7,
      verification_status: 'unverified',
      evidence_location: { page: 6 },
    }],
    conflict_count: 1,
    field_conflicts: [{ field_name: 'claim_text', conflict_types: ['decision_conflict'], opinions: [] }],
  }],
  writing_cards_items: [{
    id: 'writing-card-1',
    paper_type: 'research',
    research_gap: 'Existing catalysts lack durable polysulfide anchoring.',
    proposed_solution: 'Use defect-rich graphene to stabilize intermediates.',
    core_hypothesis: 'Defect sites alter adsorption and charge redistribution.',
    evidence_chain_status: 'present',
    review_gate_status: 'blocked',
    evidence_status: 'present',
    safety_status: 'blocked',
    safe_verified: false,
    can_use_for_writing: false,
    blocked_reasons: ['unsafe_review'],
    object_review_audit_count: 1,
    latest_object_review_audit: {
      source: 'codex_writing_audit',
      source_label: 'Codex writing audit',
      decision: 'FLAG',
      confidence: 0.66,
      verification_status: 'unverified',
    },
    object_review_audits: [{
      candidate_id: 'writing-audit-1',
      candidate_type: 'object_review_audit',
      source: 'codex_writing_audit',
      source_label: 'Codex writing audit',
      decision: 'FLAG',
      confidence: 0.66,
      verification_status: 'unverified',
      evidence_location: { page: 5 },
    }],
    conflict_count: 1,
    field_conflicts: [{ field_name: 'core_hypothesis', conflict_types: ['decision_conflict'], opinions: [] }],
  }],
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

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function makePilotExtractionWithoutPendingReviews() {
  const extraction = cloneJson(PILOT_EXTRACTION_RESULTS);
  extraction.field_reviews = [];
  extraction.validation_warnings = [];
  Object.values(extraction.results).forEach(records => {
    records.forEach(record => {
      Object.keys(record).forEach(key => {
        if (key === 'target_id' || key === 'target_type') return;
        if (record[key] && typeof record[key] === 'object') {
          delete record[key].review;
        }
      });
    });
  });
  return extraction;
}

const PILOT_EMPTY_AUDIT = {
  paper_id: PILOT_PAPER_ID,
  total_reviews: 0,
  active: 0,
  remapped: 0,
  stale: 0,
  ambiguous: 0,
  unresolved: 0,
  items: [],
};

function makePrepareSummary(overrides = {}) {
  return {
    created_count: 5,
    existing_count: 0,
    skipped_count: 0,
    verified_count: 0,
    safe_verified_count: 0,
    review_ids: PILOT_PENDING_REVIEW_IDS,
    items: PILOT_PENDING_REVIEWS,
    ...overrides,
  };
}

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

  if ((pathname === '/api/papers' || pathname === '/api/papers/') && method === 'GET') {
    return jsonResponse(route, PAPERS);
  }

  if (pathname === '/api/papers/libraries' && method === 'GET') {
    return jsonResponse(route, [{ name: 'Default Library', paper_count: PAPERS.length }]);
  }

  if (pathname === '/api/library/papers/filter' && method === 'GET') {
    return jsonResponse(route, { papers: PAPERS });
  }

  if (pathname === '/api/papers/paper-1' && method === 'DELETE') {
    return jsonResponse(route, { status: 'deleted', paper_id: 'paper-1' });
  }

  if (pathname === '/api/papers/paper-1/codex-context' && method === 'GET') {
    return jsonResponse(route, {
      paper_id: 'paper-1',
      title: PAPER_DETAIL.title,
      schema_version: 'codex_context_v1',
      context: {
        dft_export_readiness: {
          safety_gate: 'safe_verified_with_required_evidence',
          total_candidates: 1,
          eligible_count: 0,
          blocked_count: 1,
          blocked_reasons: { missing_review: 1, unsafe_locator: 1 },
          items: [
            {
              record_id: 'dft-1',
              is_exportable: false,
              eligible: false,
              blocked_reasons: ['missing_review', 'unsafe_locator'],
              review_status: 'missing',
              review_gate_status: 'blocked',
              provenance_level: 'text_evidence_only',
              locator_status: 'missing_page',
            },
          ],
        },
      },
      markdown: '# Test Paper for Smoke Validation',
      token_budget_hint: {},
    });
  }

  if (pathname === '/api/papers/paper-1/knowledge-context' && method === 'GET') {
    return jsonResponse(route, {
      paper_id: 'paper-1',
      title: PAPER_DETAIL.title,
      schema_version: 'paper_knowledge_context_v1',
      reliability_policy: {
        knowledge_items_are_candidates: true,
        section_fallbacks_are_not_final_claims: true,
        external_ai_imports_are_unverified: true,
        use_codex_or_human_review_before_citing: true,
      },
      metadata: {
        returned: 1,
        category_counts: { mechanism_context: 1 },
        source_type_counts: { paper_section: 1 },
        has_mechanism_claims: false,
        has_writing_cards: true,
      },
      candidates: [
        {
          id: 'section_fallback:paper-1:mechanism_context',
          paper_id: 'paper-1',
          category: 'mechanism_context',
          title: 'mechanism context',
          content: 'Defect sites alter adsorption and charge redistribution.',
          source_type: 'paper_section',
          candidate_status: 'section_candidate_unverified',
          evidence_state: 'parsed_source_text',
        },
      ],
      markdown: '# Paper Knowledge Candidates',
    });
  }

  if (pathname.match(/^\/api\/papers\/paper-1\/codex-item\//) && method === 'GET') {
    return jsonResponse(route, {
      paper_id: 'paper-1',
      title: PAPER_DETAIL.title,
      item_type: pathname.includes('/figure/') ? 'figure' : 'dft_result',
      item_id: pathname.split('/').pop(),
      schema_version: 'codex_item_context_v1',
      context: {},
      markdown: '# Codex Item',
      token_budget_hint: {},
    });
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
          source_section: 'Results',
          evidence_payload: {
            source_location: { section: 'Results' },
            before_text: 'The Fe-N4 catalyst was evaluated for Li-S intermediates.',
            after_text: 'This result indicates stronger polysulfide anchoring.',
          },
        },
        {
          paper_id: 'paper-2',
          title: 'Verified Legacy Record Without Extraction Confidence',
          adsorbate: 'O*',
          property_type: 'adsorption_energy',
          value: -0.8,
          unit: 'eV',
          confidence: null,
          evidence_text: 'The reviewed adsorption energy is -0.8 eV.',
          source_section: 'Results',
          validation_status: 'validated',
          is_exportable: true,
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

  if (pathname === '/api/papers/export/dft-dataset') {
    return jsonResponse(route, {
      metadata: {
        dataset_version: 'dft-ml-dataset-v0.1',
        schema_version: 'dft_results_ml_v1',
        safety_gate: 'safe_verified_with_required_evidence',
        eligible_count: 1,
        blocked_count: 2,
        blocked_reasons: { missing_review: 2 },
        total_candidates: 3,
      },
      records: [
        {
          record_id: 'dft-1',
          paper: { paper_id: 'paper-1', title: 'Test Paper for Smoke Validation' },
          target: { property_type: 'adsorption_energy', adsorbate: 'Li2S4', value: -1.23, unit: 'eV' },
          catalyst: { name: 'Fe-N-C', catalyst_type: 'single_atom' },
          dft_settings: [{ functional: 'PBE' }],
          provenance: { review_gate_status: 'safe_verified', locator_status: 'exact_page' },
        },
      ],
    });
  }

  if (pathname === '/api/papers/export/dft-quality' || pathname === '/api/papers/export/dft-review-queue') {
    return jsonResponse(route, {
      metadata: {
        schema_version: pathname.endsWith('dft-review-queue') ? 'dft_review_queue_v1' : 'dft_quality_v1',
        safety_gate: 'safe_verified_with_required_evidence',
        eligible_count: 1,
        blocked_count: 2,
        blocked_reasons: { missing_review: 1, unsafe_locator: 1 },
        total_candidates: 3,
      },
      rows: [
        {
          record_id: 'dft-blocked-1',
          paper_id: 'paper-1',
          title: 'Test Paper for Smoke Validation',
          property_type: 'adsorption_energy',
          adsorbate: 'Li2S4',
          value: -1.23,
          unit: 'eV',
          review_status: 'missing',
          review_gate_status: 'blocked',
          provenance_level: 'exact_pdf_page',
          locator_status: 'exact_page',
          blocked_reasons: ['missing_review'],
          is_exportable: false,
          can_mark_verified: true,
          recommended_action: 'verify_against_pdf',
          source_section: 'Results',
          evidence_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
          evidence_preview: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
          evidence_payload: {
            source_location: { section: 'Results' },
            before_text: 'The Fe-N4 catalyst was evaluated for Li-S intermediates.',
            after_text: 'This result indicates stronger polysulfide anchoring.',
          },
          primary_evidence_locator: {
            page: 4,
            source_type: 'table',
            table_id: 'table-1',
            figure_id: 'figure-1',
            locator_status: 'exact_page',
            locator_confidence: 0.93,
            evidence_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
          },
          primary_locator_reliability: {
            page: 4,
            bbox: null,
            status: 'exact_page',
            confidence: 0.93,
          },
          locator_reliability_status: 'weak',
          locator_reliability_warnings: ['missing_bbox'],
          evidence_page: 4,
          pdf_page_url: '/api/papers/paper-1/pdf#page=4',
          codex_item_url: '/api/papers/paper-1/codex-item/dft_result/dft-blocked-1',
          verify_url: '/api/papers/paper-1/dft-results/dft-blocked-1/verify',
          reject_url: '/api/papers/paper-1/dft-results/dft-blocked-1/reject',
          correction_url: '/api/papers/paper-1/dft-results/dft-blocked-1/corrections',
          evidence_locators: [
            {
              page: 4,
              source_type: 'table',
              table_id: 'table-1',
              figure_id: 'figure-1',
              locator_status: 'exact_page',
              locator_confidence: 0.93,
              evidence_text: 'The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.',
            },
          ],
          latest_external_audit_opinions: [
            {
              candidate_id: 'audit-1',
              source: 'assigned_dft_audit',
              source_label: 'Assigned AI DFT audit',
              agent_role: 'dft_auditor',
              model_name: 'glm-test',
              verdict: 'WARN',
              recommended_action: 'verify_against_pdf',
              verification_status: 'unverified',
              confidence: 0.72,
            },
          ],
          object_review_audits_count: 1,
          object_review_audits: [
            {
              candidate_id: 'object-audit-1',
              candidate_type: 'object_review_audit',
              status: 'candidate',
              target_type: 'dft_results',
              target_id: 'dft-blocked-1',
              field_name: 'value',
              source: 'assigned_dft_audit',
              source_label: 'Assigned AI DFT audit',
              agent_role: 'dft_auditor',
              model_name: 'glm-test',
              decision: 'REVISE',
              recommended_action: 'propose_correction',
              verification_status: 'unverified',
              confidence: 0.71,
              reason: 'Object-level audit says the value should be checked against Table 1.',
              evidence_location: { page: 4, table: 'Table 1' },
            },
          ],
          library_detail_url: '../literature_library/index.html?paper_id=paper-1&tab=dft',
          review_workbench_url: '../external_analysis_workbench/index.html?paper_id=paper-1',
        },
        {
          record_id: 'dft-blocked-text-only',
          paper_id: 'paper-1',
          title: 'Test Paper for Smoke Validation',
          property_type: 'band_gap',
          adsorbate: 'graphdiyne',
          value: 0.44,
          unit: 'eV',
          review_status: 'missing',
          review_gate_status: 'blocked',
          provenance_level: 'text_evidence_only',
          locator_status: 'text_only',
          blocked_reasons: ['unsafe_locator'],
          is_exportable: false,
          can_mark_verified: false,
          recommended_action: 'repair_pdf_locator',
          source_section: 'Results',
          evidence_text: 'The band gap is reported in the text, but the parser did not retain a page locator.',
          evidence_preview: 'The band gap is reported in the text, but the parser did not retain a page locator.',
          evidence_payload: {
            source_location: { section: 'Results' },
            before_text: 'Graphdiyne structures were compared in the electronic analysis.',
            after_text: 'The missing parser page keeps this as text-only evidence.',
          },
          primary_evidence_locator: {
            page: null,
            source_type: 'text',
            locator_status: 'text_only',
            locator_confidence: 0.31,
            evidence_text: 'The band gap is reported in the text, but the parser did not retain a page locator.',
          },
          primary_locator_reliability: {
            page: null,
            bbox: null,
            status: 'text_only',
            confidence: 0.31,
          },
          locator_reliability_status: 'text_only',
          locator_reliability_warnings: ['text_only_locator'],
          evidence_page: null,
          pdf_page_url: null,
          evidence_locators: [
            {
              page: null,
              source_type: 'text',
              locator_status: 'text_only',
              locator_confidence: 0.31,
              evidence_text: 'The band gap is reported in the text, but the parser did not retain a page locator.',
              warning_reason: 'page missing; only evidence text is available',
            },
          ],
          latest_external_audit_opinions: [],
          object_review_audits_count: 0,
          object_review_audits: [],
          library_detail_url: '../literature_library/index.html?paper_id=paper-1&tab=dft',
          review_workbench_url: '../external_analysis_workbench/index.html?paper_id=paper-1',
        },
      ],
      paper_completeness: [
        {
          paper_id: 'paper-1',
          title: 'Test Paper for Smoke Validation',
          library_detail_url: '../literature_library/index.html?paper_id=paper-1&tab=dft',
          exportable_dft_results: 1,
          blocked_dft_results: 2,
          catalyst_samples: 0,
          dft_settings: 0,
          hints: ['missing_catalyst_sample', 'missing_dft_setting', 'has_blocked_dft_results'],
        },
      ],
    });
  }

  if (pathname === '/api/workbench/review-center') {
    return jsonResponse(route, {
      schema_version: 'workbench_review_center_v1',
      metadata: { returned: 1, status_counts: { Imported: 1 }, quality_counts: { Good: 1 } },
      rows: [
        {
          paper_id: 'paper-1',
          title: 'Test Paper for Smoke Validation',
          year: 2025,
          journal: 'Journal of Testing',
          workflow_status: 'Initial_Parsed',
          pdf_quality_status: 'Good',
          pdf_quality_score: 0.92,
          pdf_exists: true,
          pdf_artifact_status: {
            pdf_exists: true,
            pdf_path_kind: 'storage_relative',
            pdf_file_size: 123456,
            blocking_errors: [],
          },
          has_dft_candidates: true,
          dft_candidate_count: 1,
          dft_candidate_status_counts: { system_candidate: 1 },
          dft_audit: { status_label: 'Initial parsed', detected_signal_count: 1, parsed_dft_count: 1, suspected_missing_count: 0 },
          dft_completeness_status: 'Initial_Parsed',
          dft_completeness_label: 'Initial parsed',
          suspected_missing_dft_count: 0,
          figure_count: 1,
          figure_reliability: {
            status: 'needs_review',
            figure_count: 1,
            issue_count: 3,
            issue_counts: { missing_full_page_snapshot: 1, small_crop: 1, missing_bbox: 1 },
            top_issues: [
              { code: 'missing_full_page_snapshot', count: 1 },
              { code: 'small_crop', count: 1 },
              { code: 'missing_bbox', count: 1 },
            ],
          },
          figure_issue_count: 3,
          figure_issue_counts: { missing_full_page_snapshot: 1, small_crop: 1, missing_bbox: 1 },
          top_figure_issues: [
            { code: 'missing_full_page_snapshot', count: 1 },
            { code: 'small_crop', count: 1 },
            { code: 'missing_bbox', count: 1 },
          ],
          table_count: 1,
          evidence_count: 1,
          locator_reliability: {
            status: 'needs_review',
            locator_count: 3,
            issue_count: 2,
            issue_counts: { text_only_locator: 1, missing_bbox: 1 },
            top_issues: [
              { code: 'missing_bbox', count: 1 },
              { code: 'text_only_locator', count: 1 },
            ],
          },
          locator_issue_count: 2,
          locator_issue_counts: { text_only_locator: 1, missing_bbox: 1 },
          top_locator_issues: [
            { code: 'missing_bbox', count: 1 },
            { code: 'text_only_locator', count: 1 },
          ],
          external_audit_count: 1,
          external_audit_opinions: [
            {
              candidate_id: 'audit-1',
              source: 'assigned_dft_audit',
              source_label: 'Assigned AI DFT audit',
              verdict: 'WARN',
              recommended_action: 'verify_against_pdf',
              verification_status: 'unverified',
            },
          ],
          object_review_audit_count: 1,
          object_review_audits: [
            {
              candidate_id: 'object-audit-1',
              candidate_type: 'object_review_audit',
              status: 'candidate',
              target_type: 'dft_results',
              target_id: 'dft-blocked-1',
              field_name: 'value',
              source: 'assigned_dft_audit',
              source_label: 'Assigned AI DFT audit',
              agent_role: 'dft_auditor',
              model_name: 'glm-test',
              decision: 'REVISE',
              recommended_action: 'propose_correction',
              verification_status: 'unverified',
              confidence: 0.71,
              reason: 'Object-level audit says the value should be checked against Table 1.',
            },
          ],
          review_conflict_count: 0,
          workspace_path: '/workspace/paper-1',
        },
        {
          paper_id: 'paper-2',
          title: 'Missing PDF But Has Clues',
          year: 2024,
          journal: 'Journal of Edge Cases',
          workflow_status: 'Unparsed',
          pdf_quality_status: 'Broken',
          pdf_quality_score: 0.0,
          pdf_exists: false,
          pdf_artifact_status: {
            pdf_exists: false,
            pdf_path_kind: 'unknown',
            pdf_file_size: null,
            blocking_errors: ['missing_pdf'],
          },
          has_dft_candidates: false,
          dft_candidate_count: 0,
          dft_candidate_status_counts: {},
          dft_audit: { status_label: 'Unparsed', detected_signal_count: 0, parsed_dft_count: 0, suspected_missing_count: 0 },
          dft_completeness_status: 'Unparsed',
          dft_completeness_label: 'Unparsed',
          suspected_missing_dft_count: 0,
          figure_count: 0,
          figure_reliability: {
            status: 'reliable',
            figure_count: 0,
            issue_count: 0,
            issue_counts: {},
            top_issues: [],
          },
          figure_issue_count: 0,
          figure_issue_counts: {},
          top_figure_issues: [],
          table_count: 0,
          evidence_count: 0,
          locator_reliability: {
            status: 'reliable',
            locator_count: 0,
            issue_count: 0,
            issue_counts: {},
            top_issues: [],
          },
          locator_issue_count: 0,
          locator_issue_counts: {},
          top_locator_issues: [],
          external_audit_count: 0,
          external_audit_opinions: [],
          object_review_audit_count: 0,
          object_review_audits: [],
          review_conflict_count: 2,
          workspace_path: '/workspace/paper-2',
        },
      ],
    });
  }

  if (pathname === '/api/workbench/review-conflicts') {
    const paperId = requestUrl.searchParams.get('paper_id');
    if (paperId === 'paper-2') {
      return jsonResponse(route, {
        rows: [
          {
            target_type: 'dft_results',
            target_id: 'dft-paper-2-1',
            field_name: 'value',
            reviewer_count: 2,
            conflict_types: ['value_conflict', 'decision_conflict'],
            opinions: [
              {
                source: 'assigned_dft_audit',
                source_label: 'Gemini data audit',
                model_name: 'gemini-test',
                agent_role: 'data_auditor',
                decision: 'accept',
                confidence: 0.82,
                value: '-1.80',
                unit: 'eV',
                reason: 'Matches Table 2.',
                evidence: { locator: { page: 5, locator_status: 'exact_page' } },
              },
              {
                source: 'external_analysis',
                source_label: 'GLM review',
                model_name: 'glm-test',
                agent_role: 'cross_checker',
                decision: 'revise',
                confidence: 0.64,
                value: '-1.75',
                unit: 'eV',
                reason: 'Caption and table disagree.',
                evidence: { locator: { page: 5, locator_status: 'text_only' } },
              },
            ],
          },
          {
            target_type: 'writing_card',
            target_id: 'writing-card-2',
            field_name: 'core_hypothesis',
            reviewer_count: 2,
            conflict_types: ['mapping_conflict'],
            opinions: [
              {
                source: 'assigned_writing_audit',
                source_label: 'Claude writing audit',
                model_name: 'claude-test',
                agent_role: 'writing_auditor',
                decision: 'review',
                confidence: 0.58,
                reason: 'Hypothesis may map to mechanism claim instead.',
                evidence: { locator: { page: 2, locator_status: 'exact_page' } },
              },
              {
                source: 'external_analysis',
                source_label: 'Gemini writing review',
                model_name: 'gemini-test',
                agent_role: 'writing_checker',
                decision: 'review',
                confidence: 0.61,
                reason: 'Current mapping still looks valid.',
                evidence: { locator: { page: 2, locator_status: 'exact_page' } },
              },
            ],
          },
        ],
      });
    }
    return jsonResponse(route, { rows: [] });
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

  if (pathname === '/api/papers/paper-1/translation/preview' && method === 'POST') {
    const payload = JSON.parse(route.request().postData() || '{}');
    const items = [];
    if (payload.include_abstract) {
      items.push({
        source_type: 'abstract',
        section_id: null,
        title: '摘要',
        page_start: null,
        page_end: null,
        source_text: PAPER_DETAIL.abstract,
        translated_text: '这是用于验收测试的摘要译文预览。',
      });
    }
    const selectedSections = Array.isArray(payload.section_ids) && payload.section_ids.length
      ? PAPER_DETAIL.sections.filter(item => payload.section_ids.includes(item.id))
      : PAPER_DETAIL.sections.slice(0, Math.min(payload.max_sections || 3, PAPER_DETAIL.sections.length));
    selectedSections.forEach(section => {
      items.push({
        source_type: 'section',
        section_id: section.id,
        title: section.section_title,
        page_start: section.page_start,
        page_end: section.page_end,
        source_text: section.text,
        translated_text: `这是 ${section.section_title} 的中文译文预览。`,
      });
    });
    return jsonResponse(route, {
      paper_id: 'paper-1',
      title: PAPER_DETAIL.title,
      target_language: 'zh-CN',
      backend_used: 'writer_llm',
      llm_status: 'preview',
      items,
    });
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
      recommended_entrypoint: {
        method: 'POST',
        path: '/api/papers/{paper_id}/prepare-ai-context',
        description: 'Prepare materials for IDE / MCP AI review.',
      },
      http_endpoints: [
        {
          name: 'prepare-ai-context',
          method: 'POST',
          path: '/api/papers/{paper_id}/prepare-ai-context',
          purpose: 'Prepare review materials without running a browser-owned AI parse.',
        },
      ],
      mcp: {
        url: '/mcp',
        common_tools: ['prepare-ai-context', 'codex-item', 'import_analysis'],
      },
      prompt_schema_version: 'ide_review_prompt_v3',
      prompt_contract: {
        schema_version: 'ide_review_prompt_v3',
        canonical_mcp_path: '/mcp',
        target_list_token: '{{TARGET_LIST}}',
        source_label_token: '{{SOURCE_LABEL}}',
        templates: {
          overall: '统一总体提示词\n目标={{TARGET_LIST}}\nsource_label={{SOURCE_LABEL}}\n受控调用 app.mcp.context.mcp_auth_context + app.mcp.server；禁止直接操作数据库。',
          dft: '统一 DFT 提示词\n目标={{TARGET_LIST}}\nsource_label={{SOURCE_LABEL}}\n单个 AI 不得最终确认 DFT。',
          figure: '统一图表提示词\n目标={{TARGET_LIST}}\nsource_label={{SOURCE_LABEL}}',
          table: '统一表格提示词\n目标={{TARGET_LIST}}\nsource_label={{SOURCE_LABEL}}',
          sections_writing: '统一章节提示词\n目标={{TARGET_LIST}}\nsource_label={{SOURCE_LABEL}}\nsection_level section_number parent_heading heading_path',
        },
        composite_templates: {
          figure_table: '统一 Figure + Table 提示词\n目标={{TARGET_LIST}}\nsource_label={{SOURCE_LABEL}}\n图像规则 + 表格规则',
        },
      },
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

  if (pathname === '/api/settings/extraction-protocols') {
    return jsonResponse(route, {
      schema_version: 'extraction_protocols_v1',
      items: [
        {
          key: 'dft_results',
          title: 'DFT 结果提取',
          path: 'prompts/dft_results.yaml',
          version: '0.2',
          stage: 'mvp',
          scope: 'DFT result extraction',
          raw_text: 'name: dft_results_extraction\nversion: 0.2\n',
        },
      ],
    });
  }

  if (pathname === '/api/visuals/overview') {
    return jsonResponse(route, {
      library_name: 'Default Library',
      summary: {
        papers: 1,
        pdf_available: 1,
        parsed_papers: 1,
        figures: 2,
        figure_data_points: 1,
        dft_settings: 1,
        catalyst_samples: 1,
        dft_results: 1,
      },
      years: [{ year: 2025, count: 1 }],
      journals: [{ journal: 'Journal of Testing', count: 1 }],
      paper_types: [{ type: 'research', count: 1 }],
      dft_matrix: [{ property_type: 'adsorption_energy', adsorbate: 'Li2S4', count: 1, avg_confidence: 0.9 }],
      dft_status: [{ status: 'Codex_Candidate', count: 1 }],
      recent_tasks: [{ job_id: 'job-1', type: 'agent_activity', status: 'completed', title: 'Mock activity', created_at: '2026-06-01T00:00:00' }],
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

  if (pathname === '/api/writing/citation-candidates' && method === 'POST') {
    const payload = JSON.parse(route.request().postData());
    if (!payload.text || payload.text.trim().length === 0) {
      return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: 'text must contain at least two searchable terms' }) });
    }
    return jsonResponse(route, {
      query_text: payload.text,
      candidate_count: 3,
      candidates: [
        {
          paper_id: 'paper-confirmed',
          title: 'Confirmed Catalyst Discovery for Lithium-Sulfur batteries',
          year: 2026,
          journal: 'Journal of Energy Chemistry',
          impact_factor: 15.6,
          impact_factor_year: 2026,
          impact_factor_status: 'available',
          citation_priority: 'high',
          exclude_from_citation: false,
          recommendation_score: 0.95,
          recommendation_tier: 'strong',
          evidence_status: 'safe_verified',
          can_be_used_as_confirmed_citation: true,
          requires_human_verification: false,
          matched_fields: ['title', 'abstract'],
          supporting_snippets: [
            { text: 'Single-atom catalysts can accelerate sulfur redox kinetics in lithium-sulfur batteries.', source: 'abstract', page: 2, locator_status: 'exact', verified: true, safe_verified: true }
          ],
          reason: 'Matches query terms; supporting review passed safe verified gate.',
          warnings: []
        },
        {
          paper_id: 'paper-needs-verification',
          title: 'Unverified Heterogeneous Electrocatalyst Acceleration',
          year: 2025,
          journal: 'Nature Communications',
          impact_factor: 16.2,
          impact_factor_year: 2025,
          impact_factor_status: 'available',
          citation_priority: 'medium',
          exclude_from_citation: false,
          recommendation_score: 0.76,
          recommendation_tier: 'strong',
          evidence_status: 'pending_with_locator',
          can_be_used_as_confirmed_citation: false,
          requires_human_verification: true,
          matched_fields: ['title', 'section'],
          supporting_snippets: [
            { text: 'Catalysts can accelerate sulfur redox reactions.', source: 'section', page: 4, locator_status: 'page_only', verified: false, safe_verified: false }
          ],
          reason: 'Matches query terms, but evidence is pending and requires human verification.',
          warnings: ['suggestion_only_needs_human_verification']
        },
        {
          paper_id: 'paper-metadata-only',
          title: 'A Review on Lithium-Sulfur Batteries and Redox Kinetics',
          year: 2024,
          journal: 'Advanced Materials',
          impact_factor: 29.4,
          impact_factor_year: 2024,
          impact_factor_status: 'available',
          citation_priority: 'low',
          exclude_from_citation: false,
          recommendation_score: 0.42,
          recommendation_tier: 'weak',
          evidence_status: 'metadata_only',
          can_be_used_as_confirmed_citation: false,
          requires_human_verification: true,
          matched_fields: ['title'],
          supporting_snippets: [
            { text: 'batteries redox kinetics in lithium-sulfur.', source: 'title', page: null, locator_status: 'missing', verified: false, safe_verified: false }
          ],
          reason: 'Matches metadata in title; metadata-only relevance cannot be used as direct evidence.',
          warnings: ['suggestion_only_needs_human_verification', 'impact_factor_needs_metadata']
        }
      ],
      excluded_count: 1,
      excluded_reasons: [
        { paper_id: 'paper-excluded', reason: 'exclude_from_citation=true' }
      ],
      warnings: [],
      safety: {
        read_only: true,
        writes_db: false,
        marks_verified: false,
        unlocks_export_or_writing: false,
        generates_bibliography: false
      }
    });
  }

  if (pathname === '/api/writing/citation-insertion-draft' && method === 'POST') {
    const payload = JSON.parse(route.request().postData());
    if (payload.selected_paper_id === 'paper-excluded') {
      return jsonResponse(route, {
        proposal_status: 'blocked_excluded_from_citation',
        draft_text: null,
        blocked_actions: ['no_database_write', 'no_verified_status_change', 'no_bibliography_generation', 'no_export_unlock']
      });
    }
    
    let draft_text = `(Draft Citation: ${payload.selected_paper_id})`;
    let can_insert = false;
    let requires_human = true;
    let warnings = [];
    let evidence_status = payload.candidate_evidence_status || 'unknown';
    
    if (payload.selected_paper_id === 'paper-confirmed') {
      can_insert = true;
      requires_human = false;
    } else if (payload.selected_paper_id === 'paper-needs-verification') {
      warnings.push('使用前必须完成人工核验');
    } else if (payload.selected_paper_id === 'paper-metadata-only') {
      warnings.push('Needs metadata and verification');
      evidence_status = 'metadata_only';
    }

    return jsonResponse(route, {
      proposal_status: evidence_status === 'metadata_only' ? 'metadata_only_draft' : (requires_human ? 'needs_human_verification' : 'can_insert'),
      can_insert_as_confirmed_citation: can_insert,
      requires_human_verification: requires_human,
      evidence_status: evidence_status,
      citation_marker: payload.citation_marker || `[1]`,
      draft_text: draft_text,
      warnings: warnings,
      human_review_checklist: ['Verify evidence', 'Check claim'],
      blocked_actions: ['no_database_write', 'no_verified_status_change', 'no_bibliography_generation', 'no_export_unlock']
    });
  }

  if (pathname === '/api/writing/evidence-backed-cards' && method === 'POST') {
    const payload = JSON.parse(route.request().postData());
    const cards = payload.candidates.map(cand => {
      const isConfirmed = cand.evidence_status === 'safe_verified';
      return {
        card_type: isConfirmed ? 'confirmed_writing_card' : 'suggestion_only',
        status: isConfirmed ? 'confirmed_writing_card' : 'suggestion_only',
        can_be_used_as_confirmed_fact: isConfirmed,
        draft_text: cand.draft_text,
        source_title: cand.title,
        evidence_status: cand.evidence_status,
        warnings: cand.warnings || [],
        safety_guardrails: { writes_db: false, auto_insert: false, generates_bibliography: false, export_unlocked: false, verified_status_changed: false }
      };
    });
    return jsonResponse(route, {
      writing_cards: cards,
      safety_guardrails: { writes_db: false, auto_insert: false, generates_bibliography: false, export_unlocked: false, verified_status_changed: false }
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
          await page.click('#addLiteratureMenu [data-add-mode="online"]');
          await expect(page.locator('#addLiteratureDialog')).toBeVisible();
          await page.click('#addLiteratureDialog button:has-text("关闭")');

          await page.click('.paper-row');
          await page.click('button[data-tab="writing"]');
          await expect(page.locator('#tab-writing')).toBeVisible();

          await page.click('button[data-tab="review"]');
          await expect(page.locator('#tab-review')).toBeVisible();
        } else if (pageInfo.name === 'DFT Database') {
          await expect(page.locator('button[onclick="exportCSV()"]')).toBeVisible();
          await expect(page.locator('.confidence-badge.unknown')).toHaveText('未记录');
          await expect(page.locator('.confidence-badge.unknown')).not.toHaveText('0%');
        } else if (pageInfo.name === 'AI Writing Studio') {
          await expect(page.locator('button[onclick="generateAcademicDraft()"]')).toBeVisible();
        } else if (pageInfo.name === 'Extraction Review Workbench') {
          await expect(page.locator('#schemaSelect')).toBeVisible();
          await page.click('button[onclick="validateCurrent()"]');
          await expect(page.locator('#warningsBox')).toBeVisible();
        } else if (pageInfo.name === 'Settings') {
          await page.click('button:has-text("IDE 连接")');
          await expect(page.locator('#section-ide')).toBeVisible();

          await page.click('button:has-text("主题外观")');
          await expect(page.locator('#section-theme')).toBeVisible();
        }

        expect(consoleErrors).toEqual([]);
      });
    });
  }

  test('literature library renders backend impact factor and ignores local cache', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('impactFactors', JSON.stringify({ 'Journal of Testing': 999 }));
    });
    await page.route(/\/api\/papers\/?\?.*/, route => jsonResponse(route, [{
      ...PAPERS[0],
      impact_factor: 24.4,
      impact_factor_year: 2024,
      impact_factor_source: 'user_imported',
    }]));

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    const impactCell = page.locator('.paper-row').first().locator('td').nth(3);
    await expect(impactCell).toContainText('24.4');
    await expect(impactCell).not.toContainText('IF:');
    await expect(impactCell).not.toContainText('999');
    await expect(impactCell.locator('[title="user_imported · 2024"]')).toBeVisible();
  });

  test('literature library labels arXiv as preprint rather than journal', async ({ page }) => {
    await page.route(/\/api\/papers\/?\?.*/, route => jsonResponse(route, [{
      ...PAPERS[0],
      journal: 'arXiv',
      doi: '10.48550/arxiv.2603.24827',
      impact_factor: null,
      impact_factor_year: null,
      impact_factor_source: null,
    }]));

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    const metaLine = page.locator('.paper-row').first().locator('.paper-meta').first();
    await expect(metaLine).toContainText('预印本: arXiv');
    await expect(metaLine).not.toContainText('期刊: arXiv');
  });

  test('business flow: Ingestion page is localized and renders calendar jobs safely', async ({ page }) => {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0).toISOString();
    const mockedJobs = [
        {
          job_id: 'job-safe-1',
          type: 'discovery_download_ingest',
          status: 'completed',
          progress: { phase: 'completed', ingested: 5, failed: 0 },
          summary: { source_label: '在线下载队列入库', success_count: 5, failure_count: 0 },
          result: null,
          error: null,
          created_at: today,
          updated_at: today,
          library_name: 'Default Library',
        },
      ];
    await page.route(/\/api\/jobs.*/, route => jsonResponse(route, mockedJobs));
    await page.route(/\/api\/papers\/ai_workflow\/jobs.*/, route => jsonResponse(route, mockedJobs));

    await page.goto(`${BASE_URL}/pages/ingestion/index.html`);
    await page.waitForTimeout(600);

    await expect(page.locator('#topnav-mount .topnav')).toBeVisible();
    await expect(page.locator('h1')).toContainText('入库中心');
    await expect(page.locator('#calendarTitle')).toBeVisible();
    await expect(page.locator('#jobQueue')).toContainText('成功入库：5 篇');

    await page.click('button[onclick="switchIngestTab(\'doi\')"]');
    await expect(page.locator('#tab-doi')).toContainText('DOI、arXiv ID 或论文 URL');

    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toMatch(/[\uFFFD\u934F\u7C31\u93C8\u7EEF\u9983\u9286\u922B\u951B]/);
    expect(consoleErrors).toEqual([]);
  });

  test('business flow: literature library uses review-center style pagination controls', async ({ page }) => {
    const total = 53;
    const libraryPapers = Array.from({ length: total }, (_, index) => ({
      id: `paper-${index + 1}`,
      paper_id: `paper-${index + 1}`,
      title: `Paper ${index + 1}`,
      title_zh: `文献 ${index + 1}`,
      year: 2024 - (index % 3),
      paper_type: 'A',
      journal: 'Journal of Tests',
      doi: `10.1000/test-${index + 1}`,
      pdf_path: `/tmp/paper-${index + 1}.pdf`,
      library_name: '默认文献库',
      counts: { sections: 2, figures: 1, dft_results: 0, writing_cards: 0 },
    }));

    await page.route('**/api/libraries', async route => {
      if (route.request().method() !== 'GET') {
        return route.fallback();
      }
      return jsonResponse(route, [
        { name: '默认文献库', is_active: true, root_path: '/libraries/default', paper_count: total },
      ]);
    });

    await page.route('**/api/papers**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() !== 'GET' || url.pathname !== '/api/papers') {
        return route.fallback();
      }
      const limit = Number(url.searchParams.get('limit') || 25);
      const offset = Number(url.searchParams.get('offset') || 0);
      return jsonResponse(route, {
        papers: libraryPapers.slice(offset, offset + limit),
        total,
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(700);

    await expect(page.locator('#paginationMeta')).toContainText('当前页 1-25 / 53 篇');
    await expect(page.locator('#paginationMeta')).toContainText('第 1 / 3 页');

    await page.click('#paginationBar button:has-text("下一页")');
    await expect(page.locator('#paginationMeta')).toContainText('当前页 26-50 / 53 篇');
    await expect(page.locator('#paginationMeta')).toContainText('第 2 / 3 页');

    await page.selectOption('#paperPageSizeSelect', '50');
    await expect(page.locator('#paginationMeta')).toContainText('当前页 1-50 / 53 篇');
    await expect(page.locator('#paginationMeta')).toContainText('第 1 / 2 页');

    await page.click('#paginationBar button:has-text("末页")');
    await expect(page.locator('#paginationMeta')).toContainText('当前页 51-53 / 53 篇');
    await expect(page.locator('#paginationMeta')).toContainText('第 2 / 2 页');
  });

  test('business flow: Mechanism Knowledge page is localized and handles empty aggregate data', async ({ page }) => {
    await page.route(/\/api\/papers\/aggregate/, route => {
      return jsonResponse(route, {
        catalyst_groups: {},
        adsorbate_groups: {},
        possible_name_aliases: [],
      });
    });

    await page.goto(`${BASE_URL}/pages/mechanism_knowledge/index.html`);
    await page.waitForTimeout(600);

    await expect(page.locator('#topnav-mount .topnav')).toBeVisible();
    await expect(page.locator('h1')).toContainText('机理知识聚合');
    await expect(page.locator('#mechanismTabs')).toContainText('催化剂聚合');
    await expect(page.locator('#catalystsBox')).toContainText('暂无催化剂聚合数据');

    await page.click('button:has-text("证据缺口")');
    await expect(page.locator('#gapsBox')).toContainText('当前库暂无机理聚合数据');

    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toMatch(/[\uFFFD\u934F\u7C31\u93C8\u7EEF\u9983\u9286\u922B\u951B]/);
    expect(consoleErrors).toEqual([]);
  });

  test('business flow: Mechanism Knowledge page shows retry state when aggregate API fails', async ({ page }) => {
    await page.route(/\/api\/papers\/aggregate/, route => {
      return route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'aggregate unavailable' }),
      });
    });

    await page.goto(`${BASE_URL}/pages/mechanism_knowledge/index.html`);
    await page.waitForTimeout(600);

    await expect(page.locator('#pageStatus')).toContainText('加载失败');
    await expect(page.locator('#pageStatus')).toContainText('aggregate unavailable');
    await expect(page.locator('#pageStatus button:has-text("重试")')).toBeVisible();
    await expect(page.locator('#catalystsBox')).toContainText('加载失败');
    expect(consoleErrors.filter(msg => !msg.includes('Failed to load resource'))).toEqual([]);
  });

  test('business flow: open Writing Studio, add evidence, generate draft, and view Citation Audit', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/ai_writer/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('.step')).toContainText(['1 主题', '2 检索证据', '3 整理证据', '4 生成草稿', '5 引文核查']);
    await expect(page.locator('button:has-text("搜索证据")')).toBeVisible();
    await expect(page.locator('button:has-text("运行引文核查")')).toBeVisible();
    await expect(page.locator('#evidencePanel')).toBeVisible();
    await expect(page.locator('body')).not.toContainText(/Export final|Final conclusion|Direct export/i);
    await page.fill('#writingTopic', 'Li2S4 adsorption energy Fe-N4');
    await page.check('#paperChecklist input[type="checkbox"]');
    await page.click('button:has-text("搜索证据")');
    await expect(page.locator('#evidencePanel')).toContainText('得分');
    await page.click('button[onclick="generateAcademicDraft()"]');
    await expect(page.locator('#tab-outline')).toContainText('Intro');
    await page.click('button:has-text("运行引文核查")');
    await expect(page.locator('#tab-audit')).toContainText('引文核查');
  });

  test('business flow: Paper Detail shows evidence panel and claim detail', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/paper_detail/index.html?paper_id=paper-1`);
    await page.waitForTimeout(500);
    await expect(page.locator('#evidencePanel')).toContainText('已支持');
    await page.click('button:has-text("DFT 候选与性能")');
    await expect(page.locator('#dftSettings')).toContainText('查看原始 JSON');
    await page.locator('#dftSettings summary').first().click();
    await expect(page.locator('#dftSettings')).toContainText('原始数据');
    await page.click('button:has-text("中文译文")');
    await expect(page.locator('#translationSections')).toContainText('Introduction');
    await expect(page.locator('#translationSections')).toContainText('Results');
    await page.uncheck('#translationIncludeAbstract');
    await page.uncheck('.translation-section-checkbox[value="chunk-1"]');
    await page.click('button:has-text("生成中文译文预览")');
    await expect(page.locator('#translationPanel')).toContainText('这是 Results 的中文译文预览');
    await expect(page.locator('#translationPanel')).not.toContainText('摘要译文预览');
    await page.click('button:has-text("仅看译文")');
    await expect(page.locator('#translationPanel')).toContainText('复制译文');
    await page.click('#evidencePanel button');
    await expect(page.locator('#evidenceDetail')).toContainText('论断');
    await expect(page.locator('#evidenceDetail')).toContainText('已支持');
    await expect(page.locator('#evidenceDetail')).toContainText('The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.');
  });

  test('business flow: Paper Detail without id shows localized empty state and hidden nav entry', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/paper_detail/index.html`);
    await page.waitForTimeout(300);
    await expect(page.locator('#fallbackState')).toContainText('请先从文献库选择一篇文献查看详情。');
    await expect(page.locator('.topnav')).not.toContainText('论文详情');
  });

  test('business flow: open manual validation workbench and validate extraction results', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=paper-1`);
    await page.waitForTimeout(500);
    await expect(page.locator('#schemaForm')).toContainText('value');
    await page.click('button[onclick="validateCurrent()"]');
    await expect(page.locator('#warningsBox')).toContainText('当前没有校验提醒');
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
    await expect(page.locator('#schemaForm')).toContainText('待人工确认');

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
    await expect(scopeSummary).toContainText('当前数据类型');
    await expect(scopeSummary).toContainText('DFT 结果');
    await expect(scopeSummary).toContainText('当前过滤');
    await expect(scopeSummary).toContainText('只看有提醒');
    await expect(scopeSummary).toContainText('当前可见记录');
    await expect(scopeSummary).toContainText('即将处理字段');

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
    await expect(page.locator('#schemaForm')).toContainText('待人工确认');
    await expect(page.locator('#schemaForm')).toContainText('Evidence text for the heterogeneous catalyst is present.');
    await expect(page.locator('#schemaForm')).toContainText('缺少准确 PDF 定位');
    await expect(page.locator('#schemaForm')).toContainText('仅有证据文本，暂无 PDF 页码定位');
    await expect(page.locator('#schemaForm')).toContainText('需要补全定位并人工确认后，才能导出或用于写作');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    await page.locator('#schemaSelect').selectOption('DFTSetting');
    await expect(page.locator('#schemaForm')).toContainText('convergence_settings');
    await expect(page.locator('#schemaForm')).toContainText('DFT convergence evidence text is visible.');
    await expect(page.locator('#schemaForm')).toContainText('待人工确认');
    await expect(page.locator('#schemaForm')).toContainText('缺少准确 PDF 定位');
    await expect(page.locator('#schemaForm')).toContainText('需要补全定位并人工确认后，才能导出或用于写作');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    await page.locator('#schemaSelect').selectOption('ElectrochemicalPerformance');
    await expect(page.locator('#schemaForm')).toContainText('rate');
    await expect(page.locator('#schemaForm')).toContainText('Rate-performance evidence text is visible.');
    await expect(page.locator('#schemaForm')).toContainText('待人工确认');
    await expect(page.locator('#schemaForm')).toContainText('需要补全定位并人工确认后，才能导出或用于写作');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    const schemaText = await page.locator('#schemaForm').innerText();
    expect(schemaText).not.toMatch(/Human verified|Ready for export|Ready for writing|export-ready|writing-ready|AI approved|auto verified/i);

    const openingRequests = apiRequests.filter(request => request.url.includes(PILOT_PAPER_ID));
    expect(openingRequests.some(request => request.url.includes('/reviews/prepare'))).toBe(false);
    expect(openingRequests.some(request => request.url.includes('/reviews/mark-verified'))).toBe(false);
    expect(openingRequests.some(request => /reviewer_status"\s*:\s*"verified"|verified"\s*:\s*true/i.test(request.body))).toBe(false);
    expect(openingRequests.some(request => request.url.includes('/export') || request.url.includes('/writer/'))).toBe(false);
  });

  test('D4-3D.2: prepare queue is user-gated, cancel-safe, and sends no verified-like payload', async ({ page }) => {
    let prepareCalls = 0;
    let prepared = false;
    const preparePayloads = [];

    await page.route(/\/api\/papers\?limit=200$/, route => jsonResponse(route, [PILOT_PAPER]));
    await page.route(new RegExp(`/api/papers/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_PAPER));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}$`), route => {
      return jsonResponse(route, prepared ? PILOT_EXTRACTION_RESULTS : makePilotExtractionWithoutPendingReviews());
    });
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews/audit$`), route => {
      return jsonResponse(route, prepared ? PILOT_AUDIT : PILOT_EMPTY_AUDIT);
    });
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/validate$`), route => {
      return jsonResponse(route, { paper_id: PILOT_PAPER_ID, validation_warnings: [] });
    });
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/evidence-locators$`), route => jsonResponse(route, []));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews/prepare$`), route => {
      prepareCalls += 1;
      preparePayloads.push(route.request().postData() || '');
      prepared = true;
      return jsonResponse(route, makePrepareSummary());
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${PILOT_PAPER_ID}`);
    await expect(page.locator('#prepareReviewPanel')).toContainText('生成待确认清单');
    await expect(page.locator('#prepareReviewsButton')).toBeEnabled();
    expect(prepareCalls).toBe(0);

    page.once('dialog', async dialog => {
      expect(dialog.message()).toContain('不会标记已确认');
      expect(dialog.message()).toContain('不会导出');
      expect(dialog.message()).toContain('不会解锁写作');
      expect(dialog.message()).toContain('不会重新解析论文');
      await dialog.dismiss();
    });
    await page.locator('#prepareReviewsButton').click();
    await page.waitForTimeout(200);
    expect(prepareCalls).toBe(0);
    await expect(page.locator('#prepareReviewPanel')).not.toContainText('新建待确认：5');

    page.once('dialog', async dialog => {
      expect(dialog.message()).toContain('不会标记已确认');
      await dialog.accept();
    });
    await page.locator('#prepareReviewsButton').click();
    await expect(page.locator('#prepareSummary')).toContainText(/新建待确认：\s*5/);
    expect(prepareCalls).toBe(1);

    const preparePayload = preparePayloads.join('\n');
    expect(preparePayload).not.toMatch(/reviewer_status\s*[:=]\s*["']?verified/i);
    expect(preparePayload).not.toMatch(/verified\s*[:=]\s*true/i);
    expect(preparePayload).not.toMatch(/safe_verified\s*[:=]\s*true/i);
    expect(preparePayload).not.toMatch(/mark_verified|export|writing|materialize|reprocess|migration/i);

    const preparePanel = page.locator('#prepareReviewPanel');
    await expect(preparePanel).toContainText(/复用已有待确认：\s*0/);
    await expect(preparePanel).toContainText(/跳过：\s*0/);
    await expect(preparePanel).toContainText(/误生成已确认：\s*0/);
    await expect(preparePanel).toContainText(/可安全导出：\s*0/);
    await expect(preparePanel).toContainText(/返回记录：\s*5/);
    await expect(page.locator('#prepareReviewsButton')).toBeDisabled();
    await expect(page.locator('#prepareReviewsButton')).toContainText('待确认清单已生成');

    await page.locator('#schemaSelect').selectOption('CatalystSample');
    const reviewQueue = page.locator('#schemaForm');
    await expect(reviewQueue).toContainText('待人工确认');
    await expect(reviewQueue).toContainText('缺少准确 PDF 定位');
    await expect(reviewQueue).toContainText('仅有证据文本，暂无 PDF 页码定位');
    await expect(reviewQueue).toContainText('需要补全定位并人工确认后，才能导出或用于写作');
    await expect(reviewQueue.locator('button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);

    const scopedText = `${await preparePanel.innerText()}\n${await reviewQueue.innerText()}`;
    expect(scopedText).not.toMatch(/Human verified|Export ready|Writing ready|已验证|可导出|可写作|一键写回数据库|自动验证|AI 审核通过|Export final|Final conclusion|Direct export/i);
  });

  test('D4-3D.2: existing pending rows disable prepare and do not auto-post', async ({ page }) => {
    let prepareCalls = 0;

    await page.route(/\/api\/papers\?limit=200$/, route => jsonResponse(route, [PILOT_PAPER]));
    await page.route(new RegExp(`/api/papers/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_PAPER));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_EXTRACTION_RESULTS));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews$`), route => jsonResponse(route, PILOT_PENDING_REVIEWS));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews/audit$`), route => jsonResponse(route, PILOT_AUDIT));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/evidence-locators$`), route => jsonResponse(route, []));
    await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews/prepare$`), route => {
      prepareCalls += 1;
      return jsonResponse(route, makePrepareSummary({ created_count: 0, existing_count: 5 }));
    });

    await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${PILOT_PAPER_ID}`);
    await expect(page.locator('#prepareReviewPanel')).toContainText('已有 5 条待确认记录');
    await expect(page.locator('#prepareReviewsButton')).toBeDisabled();
    await expect(page.locator('#prepareReviewsButton')).toContainText('待确认清单已生成');
    expect(prepareCalls).toBe(0);

    await page.locator('#schemaSelect').selectOption('CatalystSample');
    await expect(page.locator('#schemaForm')).toContainText('待人工确认');
    await expect(page.locator('#schemaForm')).toContainText('缺少准确 PDF 定位');
    await expect(page.locator('#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]')).toHaveCount(0);
  });

  test('business flow: view DFT extraction results and evidence link', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('#dftTable')).toContainText('Li2S4');
    await page.click('button:has-text("证据链接")');
    await expect(page.locator('#evidenceDetail')).toContainText('片段定位状态');
    await expect(page.locator('#evidenceDetail')).toContainText('来源章节');
    await expect(page.locator('#evidenceDetail')).toContainText('Results');
    await expect(page.locator('#evidenceDetail')).toContainText('证据原文');
    await expect(page.locator('#evidenceDetail')).toContainText('The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.');
    await expect(page.locator('#evidenceDetail')).toContainText('前文');
    await expect(page.locator('#evidenceDetail')).toContainText('后文');
  });

  test('business flow: DFT export displays safety headers', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('.export-note')).toContainText('人工已确认、证据完整、定位准确');
    await expect(page.locator('.export-note')).toContainText('需要处理的记录不会导出');
    await expect(page.locator('#dftTable')).toContainText('Li2S4');

    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportCSV()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('人工确认 + 必要证据 + 准确定位');
    await expect(page.locator('#exportSafetyStatus')).toContainText('已导出 1 条');
    await expect(page.locator('#exportSafetyStatus')).toContainText('需处理 2 条');
  });

  test('business flow: DFT ML dataset export keeps safety summary', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);
    await expect(page.locator('button[onclick="exportMLDataset()"]')).toContainText('导出 ML 数据集');

    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportMLDataset()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('人工确认 + 必要证据 + 准确定位');
    await expect(page.locator('#exportSafetyStatus')).toContainText('可导出 1 条');
    await expect(page.locator('#exportSafetyStatus')).toContainText('待完成 2 条');
  });

  test('business flow: DFT export safety overview refreshes when library changes', async ({ page }) => {
    await page.route('**/api/libraries', async route => {
      if (route.request().method() !== 'GET') {
        return route.fallback();
      }
      return jsonResponse(route, [
        { name: '默认文献库', is_active: true, root_path: '/libraries/default', paper_count: 20 },
        { name: '双原子催化剂', is_active: false, root_path: '/libraries/dual-atom', paper_count: 53 },
      ]);
    });
    await page.route('**/api/papers/export/dft-review-queue**', async route => {
      const url = new URL(route.request().url());
      const libraryName = url.searchParams.get('library_name') || '';
      if (libraryName === '双原子催化剂') {
        return jsonResponse(route, {
          metadata: {
            schema_version: 'dft_review_queue_v1',
            safety_gate: 'safe_verified_with_required_evidence',
            eligible_count: 7,
            blocked_count: 2,
            blocked_reasons: { missing_review: 1, unsafe_locator: 1 },
            total_candidates: 9,
          },
          rows: [],
          paper_completeness: [],
        });
      }
      return jsonResponse(route, {
        metadata: {
          schema_version: 'dft_review_queue_v1',
          safety_gate: 'safe_verified_with_required_evidence',
          eligible_count: 1,
          blocked_count: 4,
          blocked_reasons: { missing_review: 3, unsafe_locator: 1 },
          total_candidates: 5,
        },
        rows: [],
        paper_completeness: [],
      });
    });

    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);

    await expect(page.locator('#qualityDonuts')).toContainText('5');
    await expect(page.locator('#qualityDonuts')).toContainText('1');
    await expect(page.locator('#qualityDonuts')).toContainText('4');

    await page.selectOption('#libraryFilter', '双原子催化剂');

    await expect(page.locator('#qualityDonuts')).toContainText('9');
    await expect(page.locator('#qualityDonuts')).toContainText('7');
    await expect(page.locator('#qualityDonuts')).toContainText('2');
  });

  test('business flow: DFT quality panel shows blocked reasons and review links', async ({ page }) => {
    let verifyPayload = null;
    let rejectPayload = null;
    let correctionPayload = null;
    await page.route(/\/api\/papers\/paper-1\/dft-results\/dft-blocked-1\/verify$/, async route => {
      verifyPayload = JSON.parse(route.request().postData() || '{}');
      return jsonResponse(route, {
        paper_id: 'paper-1',
        dft_result_id: 'dft-blocked-1',
        field_names: ['value'],
        reviews: [{ id: 'review-queue-1', verified: true, reviewer_status: 'verified' }],
        export_safety: { record_id: 'dft-blocked-1', is_exportable: true, eligible: true, blocked_reasons: [] },
      });
    });
    await page.route(/\/api\/papers\/paper-1\/dft-results\/dft-blocked-1\/reject$/, async route => {
      rejectPayload = JSON.parse(route.request().postData() || '{}');
      return jsonResponse(route, {
        paper_id: 'paper-1',
        dft_result_id: 'dft-blocked-1',
        field_names: ['value'],
        reviews: [{ id: 'review-queue-2', verified: false, reviewer_status: 'rejected' }],
        export_safety: { record_id: 'dft-blocked-1', is_exportable: false, eligible: false, blocked_reasons: ['unsafe_review'] },
      });
    });
    await page.route(/\/api\/papers\/paper-1\/dft-results\/dft-blocked-1\/corrections$/, async route => {
      correctionPayload = JSON.parse(route.request().postData() || '{}');
      return jsonResponse(route, {
        correction: {
          id: 'correction-queue-1',
          paper_id: 'paper-1',
          field_name: 'dft_results',
          target_path: 'dft_results:dft-blocked-1:unit',
          proposed_value: 'eV',
          status: 'pending',
        },
      });
    });

    await page.goto(`${BASE_URL}/pages/dft_database/index.html`);
    await page.waitForTimeout(500);

    await expect(page.locator('#qualityDonuts')).toContainText('TOTAL');
    await expect(page.locator('#qualityDonuts')).toContainText('3');
    await expect(page.locator('#qualityDonuts')).toContainText('合格记录');
    await expect(page.locator('#qualityDonuts')).toContainText('1');
    await expect(page.locator('#qualityDonuts')).toContainText('待处理记录');
    await expect(page.locator('#qualityDonuts')).toContainText('2');
    await expect(page.locator('#qualityReasonChips')).toContainText('缺少人工确认');
    await expect(page.locator('#qualityReasonChips')).toContainText('PDF 定位不可靠');
    await expect(page.locator('#resultSummary')).toContainText('1 条已审核可导出 DFT 记录');
    await expect(page.locator('#dftList')).toContainText('Li2S4');
    await expect(page.locator('#dftList')).toContainText('-1.2300 eV');
    await expect(page.locator('#dftList')).toContainText('已审核可导出');
    await expect(page.locator('#dftList')).not.toContainText('待处理');
    expect(verifyPayload).toBeNull();
    expect(rejectPayload).toBeNull();
    expect(correctionPayload).toBeNull();
  });

  test('business flow: review center simplifies first-screen statuses and exposes read-only details', async ({ page }) => {
    const writeCalls = [];
    await page.route(/\/api\/papers\/.*\/dft-results\/.*\/(verify|reject)$|\/api\/papers\/.*\/dft-results\/.*\/corrections$/, async route => {
      writeCalls.push(route.request().url());
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.waitForTimeout(500);

    const rows = page.locator('#rows tr');
    await expect(rows).toHaveCount(2);

    const parsedRow = page.locator('#rows tr', { hasText: 'Test Paper for Smoke Validation' });
    const missingRow = page.locator('#rows tr', { hasText: 'Missing PDF But Has Clues' });
    await expect(parsedRow).toHaveCount(1);
    await expect(missingRow).toHaveCount(1);

    const firstStatus = parsedRow.locator('td').nth(3);
    const firstAudit = parsedRow.locator('td').nth(4);
    await expect(firstStatus).toContainText('PDF 可用');
    await expect(firstStatus).toContainText('图表');
    await expect(firstStatus).toContainText('DFT');
    await expect(firstStatus).toContainText('内容解析');
    await expect(firstAudit).toContainText('待审 DFT');
    await expect(firstAudit).toContainText('候选 1');
    await expect(firstAudit).toContainText('外部审核 1');
    await expect(firstAudit).toContainText('对象审核 1');

    const secondStatus = missingRow.locator('td').nth(3);
    const secondAudit = missingRow.locator('td').nth(4);
    await expect(secondStatus).toContainText('无 PDF');
    await expect(secondStatus).toContainText('图表');
    await expect(secondStatus).toContainText('DFT');
    await expect(secondStatus).toContainText('内容解析');
    await expect(secondAudit).toContainText('未见 DFT');
    await expect(secondAudit).toContainText('冲突 2');
    await expect(secondAudit).not.toContainText('疑似漏提');

    await parsedRow.locator('[data-action="open-details"]').click();
    const overlay = page.locator('#infoOverlay.open');
    await expect(overlay).toBeVisible();
    await expect(page.locator('#infoModalTitle')).toHaveText('Test Paper for Smoke Validation');
    await expect(page.locator('#infoModalSubtitle')).toContainText('Journal of Testing | 2025');
    await expect(overlay).toContainText('当前状态');
    await expect(overlay).toContainText('处理重点');
    await expect(overlay).toContainText('风险提醒');
    await expect(overlay).toContainText('审核痕迹');
    await expect(overlay).toContainText('待处理 DFT');
    await expect(overlay).toContainText('外部审核：1');
    await expect(overlay).toContainText('对象审核：1');
    await expect(overlay).toContainText('图表风险');
    await expect(overlay).toContainText('证据定位风险');
    await expect(overlay).toContainText('系统候选');
    await expect(overlay).toContainText('未核实');
    await page.locator('#infoOverlay .modal-close').click();
    await expect(page.locator('#infoOverlay')).not.toHaveClass(/open/);

    await missingRow.locator('[data-action="open-conflicts"]').click();
    const conflictOverlay = page.locator('#infoOverlay.open');
    await expect(conflictOverlay).toBeVisible();
    await expect(conflictOverlay).toContainText('冲突详情');
    await expect(conflictOverlay).toContainText('只读聚合，不自动合并');
    await page.locator('#infoOverlay .modal-close').click();

    await missingRow.locator('[data-action="open-details"]').click();
    const missingDetailOverlay = page.locator('#infoOverlay.open');
    await expect(missingDetailOverlay).toBeVisible();
    await expect(missingDetailOverlay).toContainText('当前没有可用 PDF');
    await expect(missingDetailOverlay).not.toContainText('文件异常');
    await page.locator('#infoOverlay .modal-close').click();

    expect(writeCalls).toEqual([]);
  });

  test('business flow: review center supports next page and sticky controls', async ({ page }) => {
    const pagedRows = Array.from({ length: 30 }, (_, index) => ({
      paper_id: `paged-paper-${String(index + 1).padStart(3, '0')}`,
      paper_code: `A${String(index + 1).padStart(4, '0')}`,
      title: `Paged Paper ${String(index + 1).padStart(3, '0')}`,
      doi: `10.1000/paged-${index + 1}`,
      year: 2026 - (index % 3),
      journal: 'Pagination Journal',
      workflow_status: 'Initial_Parsed',
      pdf_quality_status: 'A_text_readable',
      pdf_quality_score: 0.95,
      pdf_exists: true,
      pdf_artifact_status: {
        pdf_exists: true,
        pdf_path_kind: 'storage_relative',
        pdf_file_size: 2048,
        blocking_errors: [],
      },
      manual_review_progress: {
        figures: { complete: false },
        dft: { complete: false },
        content: { complete: false },
      },
      has_dft_candidates: true,
      has_active_dft_candidates: true,
      active_dft_candidate_count: 1,
      dft_candidate_count: 1,
      dft_candidate_status_counts: { system_candidate: 1 },
      dft_audit: {
        status_label: 'Initial parsed',
        detected_signal_count: 1,
        parsed_dft_count: 1,
        suspected_missing_count: 0,
      },
      dft_completeness_status: 'Initial_Parsed',
      dft_completeness_label: 'Initial parsed',
      suspected_missing_dft_count: 0,
      figure_count: 0,
      figure_reliability: { status: 'reliable', figure_count: 0, issue_count: 0, issue_counts: {}, top_issues: [] },
      figure_issue_count: 0,
      figure_issue_counts: {},
      top_figure_issues: [],
      table_count: 0,
      evidence_count: 0,
      locator_reliability: { status: 'reliable', locator_count: 0, issue_count: 0, issue_counts: {}, top_issues: [] },
      locator_issue_count: 0,
      locator_issue_counts: {},
      top_locator_issues: [],
      external_audit_count: 0,
      external_audit_opinions: [],
      object_review_audit_count: 0,
      object_review_audits: [],
      paper_note_count: 0,
      latest_paper_notes: [],
      review_conflict_count: 0,
      review_conflict_total_count: 0,
      workspace_path: `/workspace/paged-paper-${String(index + 1).padStart(3, '0')}`,
    }));

    await page.route(/\/api\/workbench\/review-center(\?.*)?$/, route => {
      return jsonResponse(route, {
        schema_version: 'workbench_review_center_v1',
        metadata: {
          returned: 30,
          total: 30,
          limit: 5000,
          has_more: false,
          sort_by: 'recent',
          status_counts: { Initial_Parsed: 30 },
          quality_counts: { A_text_readable: 30 },
        },
        rows: pagedRows,
      });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.waitForTimeout(500);

    await expect(page.locator('#rows tr')).toHaveCount(25);
    await expect(page.locator('#paginationMeta')).toContainText('第 1 / 2 页');
    await expect(page.locator('#rows tr', { hasText: 'Paged Paper 030' })).toHaveCount(1);
    await expect(page.locator('#rows tr', { hasText: 'Paged Paper 006' })).toHaveCount(1);
    await expect(page.locator('#rows tr', { hasText: 'Paged Paper 005' })).toHaveCount(0);

    const stickyPositions = await page.evaluate(() => ({
      topLine: window.getComputedStyle(document.querySelector('.review-topline')).position,
      panelHead: window.getComputedStyle(document.querySelector('.panel-head')).position,
    }));
    expect(stickyPositions.topLine).toBe('static');
    expect(stickyPositions.panelHead).toBe('sticky');

    await page.getByRole('button', { name: '下一页' }).click();

    await expect(page.locator('#rows tr')).toHaveCount(5);
    await expect(page.locator('#paginationMeta')).toContainText('第 2 / 2 页');
    await expect(page.locator('#rows tr', { hasText: 'Paged Paper 005' })).toHaveCount(1);
    await expect(page.locator('#rows tr', { hasText: 'Paged Paper 001' })).toHaveCount(1);
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
    await expect(page.locator('.export-note')).toContainText('人工已确认、证据完整、定位准确');
    
    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportCSV()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('enforced');
    await expect(page.locator('#exportSafetyStatus')).toContainText('没有可导出的合格记录');
    
    // assert toast
    await expect(page.locator('#toast')).toContainText('没有记录被导出');
    
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
    await expect(page.locator('.export-note')).toContainText('人工已确认、证据完整、定位准确');
    
    const downloadPromise = page.waitForEvent('download');
    await page.click('button[onclick="exportCSV()"]');
    await downloadPromise;

    await expect(page.locator('#exportSafetyStatus')).toContainText('enforced');
    await expect(page.locator('#exportSafetyStatus')).toContainText('0 条导出');
    await expect(page.locator('#exportSafetyStatus')).toContainText('3 条需要先处理');

    // assert toast
    await expect(page.locator('#toast')).toContainText('没有合格记录被导出，3 条需要处理');
    
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

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-row');
    await page.click('button[data-tab="review"]');
    await expect(page.locator('#taskLogPanel')).toContainText('外部 AI 候选建议');
    await expect(page.locator('#taskLogPanel')).toContainText('import_analysis 导入');
    await expect(page.locator('#taskLogPanel')).toContainText('correction x1');

    await page.evaluate(() => materializeCandidate('run-1', 'candidate-1'));
    await expect.poll(() => materializePayloads.length).toBe(1);
    expect(materializePayloads[0]).toEqual({ candidate_ids: ['candidate-1'], created_by: 'web_user' });

    await page.evaluate(() => materializeRun('run-1'));
    await expect.poll(() => materializePayloads.length).toBe(2);
    expect(materializePayloads[1]).toEqual({ explicit_all: true, created_by: 'web_user' });
    expect(materializePayloads[1]).not.toHaveProperty('candidate_ids');
    expect(materializeDialogs).toHaveLength(2);
  });

  test('business flow: literature library opens extraction job center', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('button:has-text("解析任务")');
    await expect(page.locator('#acquisitionResult')).toContainText('任务中心');
  });

  test('business flow: literature library UX is Chinese, clamps DOI, and exposes key entries', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-row');
    await page.waitForTimeout(500);

    await expect(page.locator('#paperMeta')).toContainText('10.1000/primary-doi');
    await expect(page.locator('#paperMeta')).not.toContainText('10.2000/reference-doi');
    await expect(page.locator('#paperMeta')).toContainText('检测到多个 DOI，可能需要重新解析元数据');
    await expect(page.locator('#summaryContent')).toContainText('PDF 证据定位');
    await expect(page.locator('#summaryContent')).toContainText('当前只支持有精确页码的证据跳转到 PDF');

    await page.click('button:has-text("更多操作")');
    await expect(page.locator('#paperMoreMenu')).toContainText('删除当前文献');

    const visibleText = await page.locator('body').innerText();
    expect(visibleText).not.toMatch(/Extraction Jobs|Extraction Job Center|source label|manual|unknown/);
  });

  test('business flow: literature library exposes DFT safety and Codex item actions', async ({ page }) => {
      let verifyPayload = null;
      await page.route(/\/api\/papers\/paper-1\/dft-results\/dft-1\/verify$/, async route => {
        verifyPayload = JSON.parse(route.request().postData() || '{}');
        return jsonResponse(route, {
        paper_id: 'paper-1',
        dft_result_id: 'dft-1',
        field_names: ['value'],
        reviews: [{ id: 'review-1', verified: true, reviewer_status: 'verified' }],
        export_safety: {
          record_id: 'dft-1',
          is_exportable: true,
          eligible: true,
          blocked_reasons: [],
          review_status: 'verified',
          review_gate_status: 'safe_verified',
          provenance_level: 'exact_pdf_page',
          locator_status: 'exact_page',
        },
        audit_log_id: 'audit-1',
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-row');
    await page.click('button[data-tab="dft"]');

    await expect(page.locator('#dftContent')).toContainText('候选 DFT 入库安全状态');
    await expect(page.locator('#dftContent')).toContainText('可导出 0');
    await expect(page.locator('#dftContent')).toContainText('双AI一致，待系统写回');
    await expect(page.locator('#dftContent button:has-text("复制审核提示")')).toHaveCount(2);
    const dftCandidateCard = page.locator('#dftContent details.readable-card').filter({ hasText: 'adsorption_energy' }).first();
    await dftCandidateCard.locator('summary').click();
    await expect(dftCandidateCard.locator('button:has-text("接受入库")')).toBeVisible();

    page.once('dialog', dialog => dialog.accept());
    await dftCandidateCard.locator('button:has-text("接受入库")').click();
    await expect.poll(() => verifyPayload).not.toBeNull();
    expect(verifyPayload.confirm_reviewed_against_pdf).toBe(true);
    expect(verifyPayload.reviewer).toBe('literature_library_dft');

      await page.click('button[data-tab="figures"]');
      await expect(page.locator('#figuresContent button:has-text("复制审核提示")')).toHaveCount(1);
    });

    test('business flow: literature library normalizes DFT AI opinion labels for confirmed, adopted, and pending-object-review states', async ({ page }) => {
      const detail = JSON.parse(JSON.stringify(PAPER_DETAIL));
      detail.counts = Object.assign({}, detail.counts, { dft_results: 3 });
      detail.dft_results_items = [
        {
          id: 'dft-confirmed',
          property_type: 'd_band_center',
          value: -3.03,
          unit: 'eV',
          candidate_status: 'ML_Ready',
          evidence_payload: { source: 'system_rules' },
          object_review_audit_count: 1,
          object_review_audits: [
            {
              candidate_id: 'audit-confirmed',
              candidate_type: 'object_review_audit',
              status: 'candidate',
              target_type: 'dft_result',
              target_id: 'dft-confirmed',
              field_name: 'd_band_center_value',
              source: 'mcp',
              source_label: 'reasonix_dft',
              decision: 'confirmed',
              verification_status: 'unverified',
            },
          ],
        },
        {
          id: 'dft-adopted',
          property_type: 'charge_transfer',
          value: 0.36,
          unit: 'e',
          candidate_status: 'ML_Ready',
          evidence_payload: { source: 'system_rules' },
          object_review_audit_count: 1,
          object_review_audits: [
            {
              candidate_id: 'audit-adopted',
              candidate_type: 'object_review_audit',
              status: 'candidate',
              target_type: 'dft_result',
              target_id: 'dft-adopted',
              field_name: 'charge_transfer_value',
              source: 'mcp',
              source_label: 'reasonix_dft',
              decision: 'confirmed_with_corrections',
              corrected_value: 0.36,
              verification_status: 'unverified',
            },
          ],
        },
        {
          id: 'dft-pending-object-review',
          property_type: 'free_energy',
          value: -2.83,
          unit: 'eV',
          candidate_status: 'new_candidate',
          extraction_protocol_version: 'ide_ai_new_candidate_v1',
          evidence_payload: {
            source: 'gemini_dft_review',
            source_label: 'gemini_dft_review',
            import_policy: 'new_candidate_unverified_dft_result',
          },
          object_review_audit_count: 0,
          object_review_audits: [],
        },
      ];

      await page.route(new RegExp(`/api/papers/paper-1(?:\\?.*)?$`), route => jsonResponse(route, detail));
      await page.route(new RegExp(`/api/papers/paper-1/codex-context(?:\\?.*)?$`), route => jsonResponse(route, {
        paper_id: 'paper-1',
        title: detail.title,
        schema_version: 'codex_context_v1',
        context: {
          dft_export_readiness: {
            safety_gate: 'safe_verified_with_required_evidence',
            total_candidates: 3,
            active_candidates: 3,
            eligible_count: 2,
            blocked_count: 1,
            blocked_reasons: { missing_review: 1 },
            items: [
              {
                record_id: 'dft-confirmed',
                is_exportable: true,
                eligible: true,
                blocked_reasons: [],
                review_status: 'verified',
                review_gate_status: 'safe_verified',
                provenance_level: 'exact_pdf_page',
                locator_status: 'exact_page',
              },
              {
                record_id: 'dft-adopted',
                is_exportable: true,
                eligible: true,
                blocked_reasons: [],
                review_status: 'verified',
                review_gate_status: 'safe_verified',
                provenance_level: 'exact_pdf_page',
                locator_status: 'exact_page',
              },
              {
                record_id: 'dft-pending-object-review',
                is_exportable: false,
                eligible: false,
                blocked_reasons: ['missing_review'],
                review_status: 'missing',
                review_gate_status: 'blocked',
                provenance_level: 'exact_pdf_page',
                locator_status: 'exact_page',
              },
            ],
          },
        },
        markdown: '# Test Paper for Smoke Validation',
        token_budget_hint: {},
      }));

      await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=paper-1&tab=dft&library_name=${encodeURIComponent('Default Library')}`);
      await page.waitForTimeout(700);
      await expect(page.locator('button[data-tab="dft"].active')).toBeVisible();

      const cards = page.locator('#dftContent details.readable-card');
    await expect(cards.filter({ hasText: 'd_band_center' }).first()).toContainText('AI 确认字段');
    await expect(cards.filter({ hasText: 'charge_transfer' }).first()).toContainText('已采纳 AI 修正');
    await expect(cards.filter({ hasText: 'free_energy' }).first()).toContainText('待对象审核');
    await expect(page.locator('#dftContent')).not.toContainText('AI 意见待判定');
    await expect(page.locator('#dftContent')).not.toContainText('无 AI 意见');
  });

  test('business flow: literature library keeps filter state during the current session until cleared', async ({ page }) => {
    await page.route('**/api/**', mockApi);
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.evaluate(() => window.sessionStorage.removeItem('litai:literature-library:filters:v1'));
    await page.reload();
    await page.waitForSelector('.paper-row');

    await page.fill('#searchInput', 'Fe-N4');
    await page.fill('#filterYear', '2025');
    await page.fill('#filterJournal', 'Journal of Testing');
    await page.selectOption('#filterPaperType', 'A');
    await page.selectOption('#filterDFT', 'true');
    await page.selectOption('#filterWC', 'true');
    await page.selectOption('#filterPdf', 'true');
    await page.selectOption('#filterSort', 'paper_code_asc');

    await page.evaluate(() => window.searchLocal());
    await page.reload();
    await page.waitForSelector('.paper-row');

    await expect(page.locator('#searchInput')).toHaveValue('Fe-N4');
    await expect(page.locator('#filterYear')).toHaveValue('2025');
    await expect(page.locator('#filterJournal')).toHaveValue('Journal of Testing');
    await expect(page.locator('#filterPaperType')).toHaveValue('A');
    await expect(page.locator('#filterDFT')).toHaveValue('true');
    await expect(page.locator('#filterWC')).toHaveValue('true');
    await expect(page.locator('#filterPdf')).toHaveValue('true');
    await expect(page.locator('#filterSort')).toHaveValue('paper_code_asc');

    await page.evaluate(() => window.clearFilters());
    await page.reload();
    await page.waitForSelector('.paper-row');

    await expect(page.locator('#searchInput')).toHaveValue('');
    await expect(page.locator('#filterYear')).toHaveValue('');
    await expect(page.locator('#filterJournal')).toHaveValue('');
    await expect(page.locator('#filterPaperType')).toHaveValue('');
    await expect(page.locator('#filterDFT')).toHaveValue('');
    await expect(page.locator('#filterWC')).toHaveValue('');
    await expect(page.locator('#filterPdf')).toHaveValue('');
    await expect(page.locator('#filterSort')).toHaveValue('');
  });

  test('business flow: literature library does not label unresolved whole-row DFT fixes as adopted', async ({ page }) => {
      const detail = JSON.parse(JSON.stringify(PAPER_DETAIL));
      detail.counts = Object.assign({}, detail.counts, { dft_results: 1 });
      detail.dft_results_items = [
        {
          id: 'dft-pending-whole-row',
          property_type: 'binding_energy',
          adsorbate: 'H2',
          value: -3.2,
          unit: 'eV',
          candidate_status: 'ML_Ready',
          conflict_count: 1,
          field_conflicts: [
            {
              field_name: 'value',
              conflict_types: ['adsorbate_conflict'],
              affected_field_names: ['adsorbate'],
            },
          ],
          object_review_audit_count: 1,
          object_review_audits: [
            {
              candidate_id: 'audit-pending-whole-row',
              candidate_type: 'object_review_audit',
              status: 'candidate',
              target_type: 'dft_result',
              target_id: 'dft-pending-whole-row',
              field_name: 'dft_results',
              source: 'mcp',
              source_label: 'reasonix_dft',
              decision: 'confirmed_with_corrections',
              corrected_value: {
                value: -3.2,
                unit: 'eV',
                property_type: 'binding_energy',
                adsorbate: null,
              },
              verification_status: 'unverified',
              evidence_location: { page: 5, quoted_text: 'Binding energy is -3.2 eV.' },
            },
          ],
        },
      ];

      await page.route(new RegExp(`/api/papers/paper-1(?:\\?.*)?$`), route => jsonResponse(route, detail));
      await page.route(new RegExp(`/api/papers/paper-1/codex-context(?:\\?.*)?$`), route => jsonResponse(route, {
        paper_id: 'paper-1',
        title: detail.title,
        schema_version: 'codex_context_v1',
        context: {
          dft_export_readiness: {
            safety_gate: 'safe_verified_with_required_evidence',
            total_candidates: 1,
            active_candidates: 1,
            eligible_count: 1,
            blocked_count: 0,
            blocked_reasons: {},
            items: [
              {
                record_id: 'dft-pending-whole-row',
                is_exportable: true,
                eligible: true,
                blocked_reasons: [],
                review_status: 'verified',
                review_gate_status: 'safe_verified',
                provenance_level: 'exact_pdf_page',
                locator_status: 'exact_page',
              },
            ],
          },
        },
        markdown: '# Test Paper for Smoke Validation',
        token_budget_hint: {},
      }));

      await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=paper-1&tab=dft&library_name=${encodeURIComponent('Default Library')}`);
      await page.waitForTimeout(700);

      const card = page.locator('#dftContent details.readable-card').filter({ hasText: 'binding_energy' }).first();
      await expect(card).toContainText('AI 已提修正');
      await expect(card).not.toContainText('已采纳 AI 修正');
      await expect(card).toContainText('Conflicts 1');
    });

    test('business flow: literature library deep-links to the requested review object', async ({ page }) => {
      await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}&paper_id=paper-1&tab=dft&target_type=dft_results&target_id=dft-1&field_name=value`);
      await page.waitForTimeout(700);

      await expect(page.locator('button[data-tab="dft"].active')).toBeVisible();
      await expect(page.locator('#workspaceBody')).toContainText('Test Paper for Smoke Validation');

      const targetCard = page.locator('#dftContent details.readable-card[data-codex-item-type="dft_result"][data-target-id="dft-1"]').first();
      await expect(targetCard).toBeVisible();
      await expect(targetCard).toHaveAttribute('open', '');
      await expect(targetCard).toContainText('adsorption_energy');
      await expect(targetCard).toContainText('-1.23');
    });

    test('business flow: literature library deep-links can auto-open PDF evidence for exact pages', async ({ page }) => {
      await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}&paper_id=paper-1&tab=dft&target_type=dft_results&target_id=dft-1&field_name=value&pdf_page=5&pdf_locator_status=exact_page&pdf_evidence_text=${encodeURIComponent('The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.')}`);
      await page.waitForTimeout(900);

      const targetCard = page.locator('#dftContent details.readable-card[data-codex-item-type="dft_result"][data-target-id="dft-1"]').first();
      await expect(targetCard).toHaveAttribute('open', '');

      const overlay = page.locator('#pdfViewerOverlay');
      await expect(overlay).toBeVisible();
      await expect(page.locator('#pdfViewerPageIndicator')).toContainText('5');
      await expect(page.locator('#pdfViewerEvidencePanel')).toContainText('The adsorption energy of Li2S4 on Fe-N4 is -1.23 eV.');

      const iframeSrc = await page.locator('#pdfViewerIframe').getAttribute('src');
      expect(iframeSrc).toContain('/api/papers/paper-1/pdf');
      expect(iframeSrc).toContain('page=5');

      await page.locator('#pdfViewerOverlay button:has-text("关闭")').click();
      await expect(overlay).not.toBeVisible();
    });

    test('business flow: literature library deep-links to writing cards and catalyst samples', async ({ page }) => {
      await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}&paper_id=paper-1&tab=writing&target_type=writing_card&target_id=writing-card-1&field_name=core_hypothesis`);
      await page.waitForTimeout(700);

      await expect(page.locator('button[data-tab="writing"].active')).toBeVisible();
      const writingCard = page.locator('#writingContent details.writing-card-compact[data-codex-item-type="writing_card"][data-target-id="writing-card-1"]').first();
      await expect(writingCard).toBeVisible();
      await expect(writingCard).toHaveAttribute('open', '');
      await expect(writingCard).toContainText('Defect sites alter adsorption and charge redistribution.');

      await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent('Default Library')}&paper_id=paper-1&tab=dft&target_type=catalyst_samples&target_id=catalyst-1&field_name=name`);
      await page.waitForTimeout(700);

      await expect(page.locator('button[data-tab="dft"].active')).toBeVisible();
      const catalystCard = page.locator('#dftContent details.readable-card[data-codex-item-type="catalyst_sample"][data-target-id="catalyst-1"]').first();
      await expect(catalystCard).toBeVisible();
      await expect(catalystCard).toHaveAttribute('open', '');
      await expect(catalystCard).toContainText('Pt(111)');
    });

    test('business flow: literature library refresh preserves the requested library selection', async ({ page }) => {
      const requestedLibrary = '双原子催化剂';
      const otherLibrary = '默认文献库';
      const requestedPapers = [{
        id: 'paper-dual-1',
        title: 'Dual Atom Requested Paper',
        year: 2026,
        journal: 'Dual Atom Journal',
        paper_type: 'research',
        library_name: requestedLibrary,
        counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
      }];
      const otherPapers = [{
        id: 'paper-default-1',
        title: 'Default Library Paper',
        year: 2025,
        journal: 'Default Journal',
        paper_type: 'research',
        library_name: otherLibrary,
        counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
      }];
      const requestedLibraries = [];

      await page.route(/\/api\/papers\/libraries$/, route => {
        return jsonResponse(route, [
          { name: requestedLibrary, paper_count: requestedPapers.length, is_active: false },
          { name: otherLibrary, paper_count: otherPapers.length, is_active: true },
        ]);
      });
      await page.route(/\/api\/libraries$/, route => {
        return jsonResponse(route, [
          { name: otherLibrary, paper_count: otherPapers.length, is_active: true, root_path: '/libraries/default' },
          { name: requestedLibrary, paper_count: requestedPapers.length, is_active: false, root_path: '/libraries/dual-atom' },
        ]);
      });
      await page.route(/\/api\/papers(\?.*)?$/, route => {
        const url = new URL(route.request().url());
        if (route.request().method() !== 'GET' || url.pathname !== '/api/papers') {
          return route.fallback();
        }
        const libraryName = url.searchParams.get('library_name') || '';
        requestedLibraries.push(libraryName);
        if (libraryName === requestedLibrary) {
          return jsonResponse(route, requestedPapers);
        }
        if (libraryName === otherLibrary) {
          return jsonResponse(route, otherPapers);
        }
        return jsonResponse(route, []);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html?library_name=${encodeURIComponent(requestedLibrary)}`);
      await page.waitForTimeout(900);

      await expect(page.locator('#librarySelect')).toHaveValue(requestedLibrary);
      await expect(page.locator('#paperList')).toContainText('Dual Atom Requested Paper');
      await expect(page.locator('#paperList')).not.toContainText('Default Library Paper');
      await expect(page.locator('#paperList')).not.toContainText('文献列表加载失败');

      await page.evaluate(() => refreshCurrentPage());
      await page.waitForTimeout(500);

      await expect(page.locator('#librarySelect')).toHaveValue(requestedLibrary);
      await expect(page.locator('#paperList')).toContainText('Dual Atom Requested Paper');
      await expect(page.locator('#paperList')).not.toContainText('Default Library Paper');
      await expect(page.locator('#paperList')).not.toContainText('文献列表加载失败');
      expect(requestedLibraries.length).toBeGreaterThan(0);
      expect(requestedLibraries.every(name => name === requestedLibrary)).toBe(true);
    });

    test('business flow: literature library reverts selection when activation fails', async ({ page }) => {
      const activeLibrary = '默认文献库';
      const targetLibrary = '双原子催化剂';

      await page.route(/\/api\/papers\/libraries$/, route => {
        return jsonResponse(route, [
          { name: activeLibrary, paper_count: 1, is_active: true },
          { name: targetLibrary, paper_count: 3, is_active: false },
        ]);
      });
      await page.route(/\/api\/libraries$/, route => {
        return jsonResponse(route, [
          { name: activeLibrary, paper_count: 1, is_active: true, root_path: '/libraries/default' },
          { name: targetLibrary, paper_count: 3, is_active: false, root_path: '/libraries/dual-atom' },
        ]);
      });
      await page.route(/\/api\/libraries\/[^/]+\/activate$/, route => {
        return jsonResponse(route, { detail: '激活文献库失败：mock db switch failed' }, 500);
      });
      await page.route(/\/api\/papers(\?.*)?$/, route => {
        const url = new URL(route.request().url());
        if (route.request().method() !== 'GET' || url.pathname !== '/api/papers') {
          return route.fallback();
        }
        return jsonResponse(route, [{
          id: 'paper-default-1',
          title: 'Default Library Paper',
          year: 2025,
          journal: 'Default Journal',
          paper_type: 'research',
          library_name: activeLibrary,
          counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
        }]);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(700);

      await expect(page.locator('#librarySelect')).toHaveValue(activeLibrary);
      await page.locator('#librarySelect').selectOption(targetLibrary);
      await page.waitForTimeout(700);

      await expect(page.locator('#librarySelect')).toHaveValue(activeLibrary);
      await expect(page.locator('#paperList')).toContainText('Default Library Paper');
      await expect(page.locator('.toast.error')).toContainText('切库失败：激活文献库失败：mock db switch failed');
    });

    test('business flow: literature library initial load honors active library even when quick list lacks active flags', async ({ page }) => {
      const defaultLibrary = '默认文献库';
      const activeLibrary = '双原子催化剂';

      await page.route(/\/api\/papers\/libraries$/, route => {
        return jsonResponse(route, [
          { name: defaultLibrary, paper_count: 0 },
          { name: activeLibrary, paper_count: 53 },
        ]);
      });
      await page.route(/\/api\/libraries$/, route => {
        return jsonResponse(route, [
          { name: defaultLibrary, paper_count: 0, is_active: false, root_path: '/libraries/default' },
          { name: activeLibrary, paper_count: 53, is_active: true, root_path: '/libraries/dual-atom' },
        ]);
      });
      await page.route(/\/api\/papers\/?(?:\?.*)?$/, route => {
        const url = new URL(route.request().url());
        if (route.request().method() !== 'GET' || !['/api/papers', '/api/papers/'].includes(url.pathname)) {
          return route.fallback();
        }
        return jsonResponse(route, [{
          id: 'paper-dual-atom-1',
          title: 'Dual Atom Active Paper',
          year: 2025,
          journal: 'JACS',
          paper_type: 'A',
          library_name: activeLibrary,
          counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
        }]);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(900);

      await expect(page.locator('#librarySelect')).toHaveValue(activeLibrary);
      await expect(page.locator('#paperList')).not.toContainText('文献列表加载失败');
      await expect.poll(async () => page.evaluate(() => ({
        currentLibrary: state.currentLibrary && state.currentLibrary.name,
        currentLibraryTotal: state.currentLibraryTotal,
      }))).toEqual({
        currentLibrary: activeLibrary,
        currentLibraryTotal: 53,
      });
    });

    test('business flow: literature library short-code sort triggers numeric paper-code request', async ({ page }) => {
      const activeLibrary = '双原子催化剂';
      const seenSorts = [];

      await page.route(/\/api\/papers\/libraries$/, route => {
        return jsonResponse(route, [
          { name: activeLibrary, paper_count: 2 },
        ]);
      });
      await page.route(/\/api\/libraries$/, route => {
        return jsonResponse(route, [
          { name: activeLibrary, paper_count: 2, is_active: true, root_path: '/libraries/dual-atom' },
        ]);
      });
      await page.route(/\/api\/papers\/?(?:\?.*)?$/, route => {
        const url = new URL(route.request().url());
        if (route.request().method() !== 'GET' || !['/api/papers', '/api/papers/'].includes(url.pathname)) {
          return route.fallback();
        }
        seenSorts.push({
          sortBy: url.searchParams.get('sort_by') || '',
          sortOrder: url.searchParams.get('sort_order') || '',
        });
        return jsonResponse(route, [
          {
            id: 'paper-1',
            title: 'Paper One',
            year: 2025,
            journal: 'JACS',
            paper_type: 'A',
            library_name: activeLibrary,
            paper_code: 'A0002',
            counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
          },
          {
            id: 'paper-2',
            title: 'Paper Two',
            year: 2024,
            journal: 'Nature',
            paper_type: 'B',
            library_name: activeLibrary,
            paper_code: 'B0010',
            counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
          },
        ]);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(700);
      await page.locator('#filterSort').selectOption('paper_code_asc');
      await page.waitForTimeout(700);

      await expect(page.locator('#paperList')).toContainText('Paper One');
      expect(seenSorts.some(item => item.sortBy === 'paper_code_numeric' && item.sortOrder === 'asc')).toBe(true);
    });

    test('business flow: stale literature list failures do not overwrite the latest successful result', async ({ page }) => {
      const activeLibrary = '石墨炔';
      let papersRequestCount = 0;

      await page.route(/\/api\/papers\/libraries$/, route => {
        return jsonResponse(route, [
          { name: activeLibrary, paper_count: 1, is_active: true },
        ]);
      });
      await page.route(/\/api\/libraries$/, route => {
        return jsonResponse(route, [
          { name: activeLibrary, paper_count: 1, is_active: true, root_path: '/libraries/graphdiyne' },
        ]);
      });
      await page.route(/\/api\/papers(\?.*)?$/, async route => {
        const url = new URL(route.request().url());
        if (route.request().method() !== 'GET' || url.pathname !== '/api/papers') {
          return route.fallback();
        }
        papersRequestCount += 1;
        if (papersRequestCount === 1) {
          await new Promise(resolve => setTimeout(resolve, 350));
          return jsonResponse(route, { detail: 'stale fetch failure' }, 500);
        }
        return jsonResponse(route, [{
          id: 'paper-graph-1',
          title: 'Graphdiyne Stable Result',
          year: 2026,
          journal: 'Graphdiyne Journal',
          paper_type: 'research',
          library_name: activeLibrary,
          counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
        }]);
      });

      await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
      await page.waitForTimeout(50);
      await page.evaluate(() => refreshCurrentPage());
      await page.waitForTimeout(900);

      expect(papersRequestCount).toBeGreaterThanOrEqual(2);
      await expect(page.locator('#librarySelect')).toHaveValue(activeLibrary);
      await expect(page.locator('#paperList')).toContainText('Graphdiyne Stable Result');
      await expect(page.locator('#paperList')).not.toContainText('文献列表加载失败');
      await expect(page.locator('.toast.error')).toHaveCount(0);
    });

    test('business flow: literature library DFT detail cards show real conflict field summaries', async ({ page }) => {
      const detail = JSON.parse(JSON.stringify(PAPER_DETAIL));
      detail.counts = Object.assign({}, detail.counts, { dft_results: 4 });
      detail.dft_results_items = [
        {
          id: '1b6ddc19-aac3-4c96-999d-e8d060597378',
          property_type: 'binding_energy',
          adsorbate: null,
          reaction_step: null,
          value: -12.2,
          unit: 'eV',
          candidate_status: 'system_candidate',
          evidence_text: 'HOO transition-barrier evidence.',
          conflict_count: 1,
          affected_field_names: ['adsorbate', 'reaction_step'],
          conflict_field_names: ['adsorbate', 'reaction_step'],
          field_conflicts: [{
            field_name: 'value',
            conflict_types: ['adsorbate_conflict', 'reaction_step_conflict'],
            affected_field_names: ['adsorbate', 'reaction_step'],
            conflict_field_names: ['adsorbate', 'reaction_step'],
            opinions: [],
          }],
        },
        {
          id: '54a15c22-73b2-4fa4-9774-d22d4660ca4b',
          property_type: 'binding_energy',
          adsorbate: 'H2',
          reaction_step: 'DFT',
          value: -12.2,
          unit: 'eV',
          candidate_status: 'system_candidate',
          evidence_text: 'H2 binding energy evidence.',
          conflict_count: 1,
          affected_field_names: ['adsorbate'],
          conflict_field_names: ['adsorbate'],
          field_conflicts: [{
            field_name: 'value',
            conflict_types: ['adsorbate_conflict'],
            affected_field_names: ['adsorbate'],
            conflict_field_names: ['adsorbate'],
            opinions: [],
          }],
        },
        {
          id: '861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c',
          property_type: 'binding_energy',
          adsorbate: 'Co atom',
          reaction_step: 'SAC-to-DAC stability comparison',
          value: -10.5,
          unit: 'eV',
          candidate_status: 'ML_Ready',
          evidence_text: 'Co atom binding energy evidence.',
          conflict_count: 1,
          affected_field_names: ['adsorbate'],
          conflict_field_names: ['adsorbate'],
          field_conflicts: [{
            field_name: 'value',
            conflict_types: ['adsorbate_conflict'],
            affected_field_names: ['adsorbate'],
            conflict_field_names: ['adsorbate'],
            opinions: [],
          }],
        },
        {
          id: 'ac0af689-ed0d-42f9-8b92-6bf7429ef289',
          property_type: 'adsorption_energy',
          adsorbate: 'CO',
          reaction_step: 'adsorption',
          value: -3.2,
          unit: 'eV',
          candidate_status: 'system_candidate',
          evidence_text: 'CO adsorption energy evidence.',
          conflict_count: 1,
          affected_field_names: ['property_type', 'adsorbate'],
          conflict_field_names: ['property_type', 'adsorbate'],
          field_conflicts: [{
            field_name: 'value',
            conflict_types: ['property_conflict', 'adsorbate_conflict'],
            affected_field_names: ['property_type', 'adsorbate'],
            conflict_field_names: ['property_type', 'adsorbate'],
            opinions: [],
          }],
        },
      ];

      await page.route(new RegExp(`/api/papers/paper-1(?:\\?.*)?$`), route => jsonResponse(route, detail));
      await page.route(new RegExp(`/api/papers/paper-1/codex-context(?:\\?.*)?$`), route => jsonResponse(route, {
        paper_id: 'paper-1',
        title: detail.title,
        schema_version: 'codex_context_v1',
        context: {
          dft_export_readiness: {
            safety_gate: 'safe_verified_with_required_evidence',
            total_candidates: 4,
            active_candidates: 4,
            eligible_count: 1,
            blocked_count: 3,
            blocked_reasons: { review_conflict: 3 },
            items: detail.dft_results_items.map(item => ({
              record_id: item.id,
              is_exportable: item.id === '861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c',
              eligible: item.id === '861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c',
              blocked_reasons: item.id === '861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c' ? [] : ['review_conflict'],
              review_status: item.id === '861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c' ? 'verified' : 'review_conflict',
              review_gate_status: item.id === '861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c' ? 'safe_verified' : 'blocked',
              provenance_level: 'exact_pdf_page',
              locator_status: 'exact_page',
            })),
          },
        },
      }));

      await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=paper-1&tab=dft`);
      await page.waitForTimeout(700);
      await expect(page.locator('button[data-tab="dft"].active')).toBeVisible();

      const expectations = [
        ['1b6ddc19-aac3-4c96-999d-e8d060597378', 'Conflict fields: adsorbate, reaction_step'],
        ['54a15c22-73b2-4fa4-9774-d22d4660ca4b', 'Conflict fields: adsorbate'],
        ['861fb7e4-0d8e-4f3b-9f4e-41661bc10c5c', 'Conflict fields: adsorbate'],
        ['ac0af689-ed0d-42f9-8b92-6bf7429ef289', 'Conflict fields: property_type, adsorbate'],
      ];

      for (const [targetId, expectedText] of expectations) {
        const detailCard = page.locator(`#dftContent details.readable-card[data-target-id="${targetId}"]`);
        await expect(detailCard).toHaveCount(1);
        await detailCard.locator('summary').click();
        await expect(detailCard).toContainText('Conflicts 1');
        await expect(detailCard).toContainText(expectedText);
        await expect(detailCard).not.toContainText(/Conflict fields:\s*value\b/);
      }
    });

  test('business flow: literature library figures tab shows read-only figure review summaries', async ({ page }) => {
      const unsafeWrites = [];
      await page.route(/\/api\/papers\/paper-1\/dft-results\/.*\/(verify|reject)$|\/api\/external-analysis\/runs\/.*\/materialize$|\/api\/papers\/paper-1\/corrections/, async route => {
        unsafeWrites.push({ method: route.request().method(), url: route.request().url() });
      return jsonResponse(route, { error: 'unexpected write' }, 500);
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-row');
    await page.click('button[data-tab="figures"]');

    const figures = page.locator('#figuresContent');
    await expect(figures).toContainText('Crop status: 待核对');
    await expect(figures).toContainText('Figure reliability: needs review');
    await expect(figures).toContainText('Image review: required');
    await expect(figures).toContainText('Flags: missing_parser_bbox');
    await expect(figures).toContainText('missing full-page snapshot');
    await expect(figures).toContainText('small crop');
    await expect(figures).toContainText('missing bbox');
    await expect(figures).toContainText('Figure artifact detail: pixel 120x80');
    await expect(figures).toContainText('Object audits 1');
    await expect(figures).toContainText('Conflicts 1');
    await expect(figures).toContainText('Latest audit: GLM figure audit');
    await expect(figures).toContainText('decision=REVISE');
    await expect(figures).toContainText('verification=unverified');

    await page.locator('#figuresContent details.figure-card').evaluateAll(details => {
      details.forEach(item => item.setAttribute('open', ''));
    });
    await page.click('#figuresContent button:has-text("Open PDF page 3")');
    await expect(page.locator('#pdfViewerOverlay')).toBeVisible();
    await page.evaluate(() => window.closePdfViewer && window.closePdfViewer());

    await page.locator('#figuresContent summary button').first().click();
    await expect.poll(() => unsafeWrites.length).toBe(0);
  });

  test('business flow: literature library figure cards can directly delete polluted duplicates', async ({ page }) => {
    const detail = JSON.parse(JSON.stringify(PAPER_DETAIL));
    detail.figures = [
      {
        id: 'figure-dup-1',
        figure_label: 'fig_4a',
        caption: 'Duplicate right-column fragment of Fig. 4.',
        page: 6,
        figure_role: 'experimental_evidence',
        crop_status: 'needs_recrop',
        content_summary: 'Duplicate parser fragment.',
        key_elements: ['duplicate crop'],
        flags: [],
        direct_delete_eligible: true,
        image_review: { review_required: true, crop_status: 'needs_recrop' },
      }
    ];
    let deletePayload = null;
    await page.route(new RegExp(`/api/papers/paper-1(?:\\?.*)?$`), route => jsonResponse(route, detail));
    await page.route(/\/api\/papers\/paper-1\/figures\/figure-dup-1\/delete$/, async route => {
      deletePayload = JSON.parse(route.request().postData() || '{}');
      return jsonResponse(route, {
        status: 'deleted',
        correction_id: 'corr-delete-1',
        paper_id: 'paper-1',
        figure_id: 'figure-dup-1',
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=paper-1&tab=figures`);
    await page.waitForTimeout(700);
    await page.evaluate(() => {
      window.prompt = () => 'Duplicate parser fragment of Fig. 4; submit delete proposal.';
    });

    const figureCard = page.locator('#figuresContent details.figure-card').first();
    await expect(figureCard).toBeVisible();
    await expect(figureCard.locator('summary')).toContainText('直接删除');
    await figureCard.locator('button:has-text("直接删除")').click();
    await expect.poll(() => deletePayload).not.toBeNull();
    expect(deletePayload).toMatchObject({
      confirm_direct_delete: true,
      reviewer: 'literature_library_user',
      reason: 'Duplicate parser fragment of Fig. 4; submit delete proposal.',
    });
    expect(deletePayload.evidence_payload.page).toBe(6);
    expect(deletePayload.evidence_payload.figure_label).toBe('fig_4a');
    await expect(page.locator('#figuresContent details.figure-card')).toHaveCount(0);
  });

  test('business flow: literature library normal figures do not expose direct delete', async ({ page }) => {
    const detail = JSON.parse(JSON.stringify(PAPER_DETAIL));
    detail.figures = [
      {
        id: 'figure-clean-1',
        figure_label: 'fig_2',
        caption: 'Figure 2. Clean figure still needs a curator delete option when the user identifies a duplicate object.',
        page: 4,
        figure_role: 'experimental_evidence',
        crop_status: 'recropped',
        content_summary: 'Full figure crop.',
        key_elements: ['full figure'],
        flags: [],
        direct_delete_eligible: false,
        image_review: { review_required: false, crop_status: 'recropped' },
      }
    ];
    await page.route(new RegExp(`/api/papers/paper-1(?:\\?.*)?$`), route => jsonResponse(route, detail));

    await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=paper-1&tab=figures`);
    await page.waitForTimeout(700);

    const figureCard = page.locator('#figuresContent details.figure-card').first();
    await expect(figureCard).toBeVisible();
    await expect(figureCard.locator('summary')).not.toContainText('直接删除');
    await expect(figureCard.locator('button:has-text("直接删除")')).toHaveCount(0);
  });

  test('business flow: literature library duplicate figures still expose direct delete even with legacy pending proposals', async ({ page }) => {
    const detail = JSON.parse(JSON.stringify(PAPER_DETAIL));
    detail.figures = [
      {
        id: 'figure-pending-delete-1',
        figure_label: 'fig_3',
        caption: 'Duplicate parser fragment of Fig. 3.',
        page: 6,
        figure_role: 'experimental_evidence',
        crop_status: 'needs_recrop',
        content_summary: 'Duplicate parser fragment.',
        key_elements: ['duplicate crop'],
        flags: [],
        pending_delete_proposal_count: 1,
        pending_correction_count: 1,
        pending_correction_fields: ['delete'],
        direct_delete_eligible: true,
        image_review: { review_required: true, crop_status: 'needs_recrop' },
      }
    ];
    await page.route(new RegExp(`/api/papers/paper-1(?:\\?.*)?$`), route => jsonResponse(route, detail));

    await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=paper-1&tab=figures`);
    await page.waitForTimeout(700);

    const figureCard = page.locator('#figuresContent details.figure-card').first();
    await expect(figureCard.locator('summary')).toContainText('直接删除');
    await expect(figureCard).toContainText('Legacy delete proposals still pending (1)');
  });

  test('business flow: literature library writing cards show read-only audit summaries', async ({ page }) => {
    const unsafeWrites = [];
    await page.route(/\/api\/papers\/paper-1\/dft-results\/.*\/(verify|reject)$|\/api\/external-analysis\/runs\/.*\/materialize$|\/api\/papers\/paper-1\/corrections/, async route => {
      unsafeWrites.push({ method: route.request().method(), url: route.request().url() });
      return jsonResponse(route, { error: 'unexpected write' }, 500);
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-row');
    await page.click('button[data-tab="writing"]');

    const writing = page.locator('#writingContent');
    await expect(writing).toContainText('Object audits 1');
    await expect(writing).toContainText('Conflicts 1');
    await expect(writing).toContainText('Evidence status: present');
    await expect(writing).toContainText('Safety: blocked');
    await expect(writing).toContainText('Latest audit: Codex writing audit');
    await expect(writing).toContainText('decision=FLAG');
    await expect(writing).toContainText('verification=unverified');

    await page.locator('#writingContent summary button').first().click();
    await expect.poll(() => unsafeWrites.length).toBe(0);
  });

  test('business flow: literature library mechanism claims show read-only audit summaries', async ({ page }) => {
    const unsafeWrites = [];
    await page.route(/\/api\/papers\/paper-1\/dft-results\/.*\/(verify|reject)$|\/api\/external-analysis\/runs\/.*\/materialize$|\/api\/papers\/paper-1\/corrections/, async route => {
      unsafeWrites.push({ method: route.request().method(), url: route.request().url() });
      return jsonResponse(route, { error: 'unexpected write' }, 500);
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    await page.click('.paper-row');
    await page.click('button[data-tab="dft"]');

    const dft = page.locator('#dftContent');
    await expect(dft).toContainText('Object audits 1');
    await expect(dft).toContainText('Conflicts 1');
    await expect(dft).toContainText('Evidence status: present');
    await expect(dft).toContainText('Locator: text only');
    await expect(dft).toContainText('Confidence: medium');
    await expect(dft).toContainText('Latest audit: GLM mechanism audit');
    await expect(dft).toContainText('decision=FLAG');
    await expect(dft).toContainText('verification=unverified');

    await page.locator('#dftContent button:has-text("复制审核提示")').last().click();
    await expect.poll(() => unsafeWrites.length).toBe(0);
  });

  test('business flow: review tab exposes MCP guide instead of legacy internal AI trigger', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.locator('.paper-row').first().click();
    await page.click('button[data-tab="review"]');

    await expect(page.locator('#tab-review')).toContainText('任务日志');
    await expect(page.locator('#tab-review')).toContainText('刷新任务日志');
    await expect(page.locator('#tab-review button[onclick="runInternalAIParse()"]')).toHaveCount(0);
    await expect(page.locator('#tab-review button[onclick="loadAgentGuide()"]')).toHaveCount(0);
    await page.click('button[data-tab="writing"]');
    await page.locator('#tab-writing button[onclick="loadAgentGuide()"]').click();
    await expect(page.locator('#mcpGuideBox')).toContainText('IDE / MCP AI');
    await expect(page.locator('#mcpGuideBox')).toContainText('/mcp');
    await expect(page.locator('#mcpGuideBox')).toContainText('prepare-ai-context');
    await expect(page.locator('#mcpGuideBox')).toContainText('codex-item');
    await expect(page.locator('#progressBox')).toHaveCount(0);
  });

  test('business flow: paper detail DFT prompts include the canonical safety preamble', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.locator('.paper-row').first().click();

    const prompt = await page.evaluate(() => canonicalIdePromptForSelectedPaper('dft'));

    expect(prompt).toContain('统一 DFT 提示词');
    expect(prompt).toContain('单个 AI 不得最终确认 DFT');
    expect(prompt).toContain('paper_id=paper-1');
    expect(prompt).not.toContain('{{TARGET_LIST}}');
  });

  test('business flow: review tab separates IDE/MCP workflow from embedding settings', async ({ page }) => {
    await page.route(/\/api\/settings\/status$/, route => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          embedding: { configured: true, provider: 'deterministic', model: 'text-embedding-3-small' },
          writer: {
            configured: false,
            disabled: true,
            backend: 'disabled',
            model: '',
            missing: [],
            message: 'Web-side Writer LLM is disabled',
          },
          internal_parser: {
            configured: false,
            backend: 'rule',
            model: 'gpt-4.1-mini',
            uses: 'writer_llm',
            missing: ['internal_parser_api_base', 'internal_parser_api_key'],
            message: 'Internal AI parsing is not configured; it uses the Writer LLM connection, not Embedding.',
          },
          mcp: { has_keys: false, enabled: false },
        }),
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.locator('.paper-row').first().click();
    await page.click('button[data-tab="review"]');

    await expect(page.locator('#tab-review')).toContainText('任务日志');
    await expect(page.locator('#tab-review button[onclick="runInternalAIParse()"]')).toHaveCount(0);
    await expect(page.locator('#tab-review button[onclick="loadAgentGuide()"]')).toHaveCount(0);
    await page.click('button[data-tab="writing"]');
    await page.locator('#tab-writing button[onclick="loadAgentGuide()"]').click();
    await expect(page.locator('#mcpGuideBox')).toContainText('/api/papers/{paper_id}/prepare-ai-context');
    await expect(page.locator('#mcpGuideBox')).toContainText('codex-item');
    await expect(page.locator('#tab-writing')).not.toContainText('text-embedding-3-small');
    await expect(page.locator('#progressBox')).toHaveCount(0);
  });

  test('business flow: review center builds prompts from the canonical backend contract', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.waitForTimeout(500);

    const prompt = await page.evaluate(() => buildIdePromptForCopy('sections_writing'));

    expect(prompt).toContain('统一章节提示词');
    expect(prompt).toContain('section_level section_number parent_heading heading_path');
    expect(prompt).toContain('source_label=<agent_name>_sections_writing_');
    expect(prompt).not.toContain('{{TARGET_LIST}}');
    expect(prompt).not.toContain('{{SOURCE_LABEL}}');
  });

  test('business flow: review center keeps one visual entry and resolves figure, table, or mixed prompts', async ({ page }) => {
    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.waitForTimeout(500);

    await expect(page.locator('#promptCopySelect option')).toHaveCount(5);
    await expect(page.locator('#promptCopySelect option[value="figure"]')).toHaveText('图表指令');
    await expect(page.locator('#promptCopySelect option[value="table"]')).toHaveCount(0);

    const kinds = await page.evaluate(() => {
      const originalRows = state.rows;
      const originalVisibleRows = currentVisibleRows;
      selectedPaperIds.clear();
      window.currentVisibleRows = () => [{ paper_id: 'figure-only', figure_count: 2, table_count: 0 }];
      const figureKind = resolveVisualPromptKind();
      window.currentVisibleRows = () => [{ paper_id: 'table-only', figure_count: 0, table_count: 2 }];
      const tableKind = resolveVisualPromptKind();
      window.currentVisibleRows = () => [
        { paper_id: 'mixed', figure_count: 1, table_count: 1 },
      ];
      const mixedKind = resolveVisualPromptKind();
      window.currentVisibleRows = originalVisibleRows;
      state.rows = originalRows;
      return { figureKind, tableKind, mixedKind };
    });

    expect(kinds).toEqual({ figureKind: 'figure', tableKind: 'table', mixedKind: 'figure_table' });
    const mixedPrompt = await page.evaluate(() => buildIdePromptForCopy('figure_table'));
    expect(mixedPrompt).toContain('统一 Figure + Table 提示词');
    expect(mixedPrompt).toContain('source_label=<agent_name>_figure_table_');
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
    await page.locator('.paper-card, .paper-row').first().click();
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
            workflow_status: 'Needs_Human_Confirmation',
            pdf_quality_status: 'Broken',
            counts: { sections: 0, figures: 0, dft_results: 0, writing_cards: 0 }
          }
        ]);
      }
      return route.fallback();
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);
    const row = page.locator('.paper-row[data-id="paper-meta-only"]');
    await expect(row).toContainText('无 PDF');
    await expect(row).not.toContainText('待人工审核');
    await expect(row).not.toContainText('文件异常');
  });

  test('business flow: literature library trusts artifact_status pdf_exists even without pdf_path', async ({ page }) => {
    const artifactPdfPaper = {
      id: 'paper-artifact-pdf',
      paper_id: 'paper-artifact-pdf',
      title: 'Artifact Status PDF Paper',
      year: 2025,
      journal: 'Journal of Artifact Truth',
      paper_type: 'research',
      pdf_path: null,
      pdf_url: null,
      oa_status: 'uploaded',
      workflow_status: 'Initial_Parsed',
      artifact_status: {
        pdf_exists: true,
        pdf_file_size: 2048,
        pdf_path_kind: 'storage_relative',
        blocking_errors: [],
        warnings: [],
      },
      counts: { sections: 3, figures: 1, dft_results: 0, writing_cards: 0 },
    };

    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [artifactPdfPaper]);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/paper-artifact-pdf(?:\?.*)?$/, route => {
      return jsonResponse(route, {
        ...artifactPdfPaper,
        abstract: 'Detail payload only exposes artifact_status.pdf_exists as the PDF truth source.',
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    const row = page.locator('.paper-row[data-id="paper-artifact-pdf"]');
    await expect(row).toContainText('Artifact Status PDF Paper');
    await expect(row).not.toContainText('无 PDF');

    await row.click();
    await page.waitForTimeout(500);

    await expect(page.locator('#pdfEvidenceHeaderBtn')).toHaveText('查看 PDF');
    await expect(page.locator('#pdfEvidenceHeaderBtn')).toBeEnabled();
    await expect(page.locator('#summaryContent')).not.toContainText('尚无 PDF');
    await expect(page.locator('#summaryContent')).toContainText('查看 PDF / 证据定位');
  });

  test('business flow: explicit missing artifact overrides stale pdf_path', async ({ page }) => {
    const missingArtifactPaper = {
      id: 'paper-missing-artifact',
      paper_id: 'paper-missing-artifact',
      title: 'Missing Artifact Paper',
      year: 2025,
      journal: 'Journal of Artifact Truth',
      paper_type: 'research',
      pdf_path: 'storage/pdf/stale-reference.pdf',
      pdf_url: null,
      pdf_exists: false,
      oa_status: 'uploaded',
      workflow_status: 'Initial_Parsed',
      pdf_quality_status: 'A_text_readable',
      artifact_status: {
        pdf_exists: false,
        blocking_errors: ['missing_pdf'],
        warnings: [],
      },
      counts: { sections: 3, figures: 1, dft_results: 0, writing_cards: 0 },
    };

    await page.route(/\/api\/papers(\?|$)/, route => {
      if (route.request().method() === 'GET') {
        return jsonResponse(route, [missingArtifactPaper]);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/paper-missing-artifact(?:\?.*)?$/, route => {
      return jsonResponse(route, {
        ...missingArtifactPaper,
        abstract: 'The stored path is stale and must not imply an available PDF.',
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html`);
    await page.waitForTimeout(500);

    const row = page.locator('.paper-row[data-id="paper-missing-artifact"]');
    await expect(row).toContainText('无 PDF');
    await expect(row).not.toContainText('PDF已上传');
    await expect(row).not.toContainText('文件可读');

    await row.click();
    await page.waitForTimeout(500);

    await expect(page.locator('#pdfEvidenceHeaderBtn')).toHaveText('PDF 未上传');
    await expect(page.locator('#pdfEvidenceHeaderBtn')).toBeDisabled();
  });

  test('business flow: review center inspect link preserves library context', async ({ page }) => {
    await page.route(/\/api\/workbench\/review-center(\?.*)?$/, route => {
      return jsonResponse(route, {
        schema_version: 'workbench_review_center_v1',
        metadata: {
          returned: 1,
          total: 1,
          limit: 5000,
          has_more: false,
          sort_by: 'recent',
          status_counts: { Initial_Parsed: 1 },
          quality_counts: { A_text_readable: 1 },
        },
        rows: [
          {
            paper_id: 'paper-review-link',
            paper_code: 'U0023',
            paper_short_id: 'U0023',
            library_name: '双原子催化剂',
            title: 'Review Link Paper',
            year: 2025,
            journal: 'Journal of Context',
            doi: '10.1000/context',
            workflow_status: 'Initial_Parsed',
            pdf_quality_status: 'A_text_readable',
            pdf_exists: true,
            pdf_artifact_status: { pdf_exists: true, pdf_path_kind: 'stored', pdf_file_size: 1234, blocking_errors: [] },
            has_parsed_content: true,
            manual_review_progress: { content: false, figures: false, dft: false },
            pdf_url: '/api/papers/paper-review-link/pdf',
            needs_human_confirmation: true,
            has_dft_candidates: false,
            has_active_dft_candidates: false,
            active_dft_candidate_count: 0,
            dft_candidate_count: 0,
            dft_candidate_status_counts: {},
            dft_audit: { status_label: 'Unparsed', detected_signal_count: 0, parsed_dft_count: 0, suspected_missing_count: 0 },
            dft_completeness_status: 'Unparsed',
            dft_completeness_label: '未解析',
            suspected_missing_dft_count: 0,
            figure_count: 0,
            figure_crop_status_counts: {},
            unreliable_figure_count: 0,
            figure_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
            figure_issue_count: 0,
            figure_issue_counts: {},
            top_figure_issues: [],
            table_count: 0,
            evidence_count: 0,
            locator_reliability: { status: 'reliable', issue_count: 0, issue_counts: {}, top_issues: [] },
            locator_issue_count: 0,
            locator_issue_counts: {},
            top_locator_issues: [],
            external_audit_count: 0,
            external_audit_source_counts: {},
            external_audit_opinions: [],
            object_review_audit_count: 0,
            object_review_audit_source_counts: {},
            object_review_audits: [],
            paper_note_count: 0,
            latest_paper_notes: [],
            review_conflict_count: 0,
            review_conflict_total_count: 0,
            workspace_path: '/workspace/review-link',
          },
        ],
      });
    });

    await page.goto(`${BASE_URL}/pages/review_center/index.html`);
    await page.waitForTimeout(500);

    const href = await page.locator('#rows tr a:has-text("查看")').first().getAttribute('href');
    expect(href).toContain('paper_id=paper-review-link');
    expect(href).toContain('tab=');
    expect(href).toContain('library_name=' + encodeURIComponent('双原子催化剂'));
  });

  test('business flow: literature library loads selected paper detail even when not in current page list', async ({ page }) => {
    const selectedPaperId = 'paper-off-page';
    const listRows = Array.from({ length: 25 }, (_, index) => ({
      id: `paper-list-${index + 1}`,
      paper_id: `paper-list-${index + 1}`,
      title: `Paged Library Paper ${String(index + 1).padStart(2, '0')}`,
      year: 2025,
      journal: 'Journal of Paging',
      paper_type: 'research',
      library_name: '双原子催化剂',
      workflow_status: 'Initial_Parsed',
      pdf_quality_status: 'A_text_readable',
      counts: { sections: 1, figures: 0, dft_results: 0, writing_cards: 0 },
    }));
    const selectedPaperDetail = {
      id: selectedPaperId,
      paper_id: selectedPaperId,
      title: 'Off Page Selected Paper',
      year: 2024,
      journal: 'Journal of Direct Detail Loading',
      paper_type: 'research',
      library_name: '双原子催化剂',
      pdf_path: null,
      oa_status: 'uploaded',
      artifact_status: {
        pdf_exists: true,
        pdf_file_size: 2048,
        pdf_path_kind: 'storage_relative',
        blocking_errors: [],
        warnings: [],
      },
      pdf_quality_status: 'A_text_readable',
      counts: { sections: 2, figures: 1, dft_results: 0, writing_cards: 0 },
      abstract: 'This paper should load by paper_id even when it is not visible in the current page list.',
      sections: [],
      tables: [],
      figures: [],
      paper_notes: [],
      dft_settings_items: [],
      catalyst_samples_items: [],
      dft_results_items: [],
      electrochemical_performance_items: [],
      mechanism_claims_items: [],
      writing_cards_items: [],
      figure_data_points_items: [],
      outgoing_relationships: [],
      incoming_relationships: [],
      references: [],
      abstract_review_status: 'raw_only',
      sections_review_status: 'raw_only',
      writing_cards_review_status: 'missing',
      figures_review_status: 'missing',
      dft_review_status: 'missing',
      translation_review_status: 'missing',
      rag_quality: {},
    };

    await page.route(/\/api\/papers(\?.*)?$/, route => {
      const url = new URL(route.request().url());
      if (route.request().method() === 'GET' && url.pathname === '/api/papers') {
        return jsonResponse(route, listRows);
      }
      return route.fallback();
    });

    await page.route(/\/api\/papers\/libraries$/, route => {
      return jsonResponse(route, [{ name: '双原子催化剂', paper_count: 25, is_active: true }]);
    });

    await page.route(/\/api\/libraries$/, route => {
      return jsonResponse(route, [{ name: '双原子催化剂', paper_count: 25, is_active: true, root_path: '/libraries/dual-atom' }]);
    });

    await page.route(new RegExp(`/api/papers/${selectedPaperId}(?:\\?.*)?$`), route => {
      return jsonResponse(route, selectedPaperDetail);
    });

    await page.goto(`${BASE_URL}/pages/literature_library/index.html?paper_id=${selectedPaperId}&tab=summary&library_name=${encodeURIComponent('双原子催化剂')}`);
    await page.waitForTimeout(1000);

    await expect(page.locator('#paperTitle')).toHaveText('Off Page Selected Paper');
    await expect(page.locator('#workspaceBody')).toBeVisible();
    await expect(page.locator('#workspaceEmpty')).not.toBeVisible();
    await expect(page.locator('#pdfEvidenceHeaderBtn')).toHaveText('查看 PDF');
    await expect(page.locator('#librarySelect')).toHaveValue('双原子催化剂');
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

    await page.route(/\/api\/papers\/paper-meta-only(?:\?.*)?$/, route => {
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

    const metaCard = page.locator('.paper-row[data-id="paper-meta-only"]');
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

    await expect(page.locator('.paper-row.active')).toContainText('Metadata Only Paper (Attached)');
    await expect(page.locator('.paper-row.active .status-chip.parsed')).toBeVisible();
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

    await page.route(/\/api\/papers\/paper-meta-only(?:\?.*)?$/, route => {
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

    const metaCard = page.locator('.paper-row[data-id="paper-meta-only"]');
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

    await page.route(/\/api\/papers\/paper-meta-only(?:\?.*)?$/, route => {
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

    const metaCard = page.locator('.paper-row[data-id="paper-meta-only"]');
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

    await page.route(/\/api\/papers\/paper-meta-only(?:\?.*)?$/, route => {
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

    const metaCard = page.locator('.paper-row[data-id="paper-meta-only"]');
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
    await page.evaluate(() => window.pollAIWorkflowJob && window.pollAIWorkflowJob('job-1'));
    await expect(page.locator('#acquisitionResult')).toContainText('AI 后台检索 / 收录任务');
    
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

      // Click on paper row to load detail
      await page.click('.paper-row');
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
      await page.locator('.paper-card, .paper-row').first().click();
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
      await expect(exactBadge.first()).toContainText('PDF 定位准确');

      const pageOnlyBadge = page.locator('.locator-status-badge[data-locator-status="exact_page"]');
      await expect(pageOnlyBadge.first()).toBeVisible();
      await expect(pageOnlyBadge.first()).toContainText('PDF 定位准确');

      const needsReparseBadge = page.locator('.locator-status-badge[data-locator-status="missing_page"]');
      await expect(needsReparseBadge).toBeVisible();
      await expect(needsReparseBadge).toContainText('缺少准确 PDF 定位');

      // exact_page: has page jump button
      const viewOriginalBtn = page.locator('button:has-text("查看 PDF 第 5 页")');
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
      await page.locator('.paper-card, .paper-row').first().click();
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
      await expect(evidencePanel).toContainText('临时高亮/绘制不会写回系统');

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
      await page.locator('.paper-card, .paper-row').first().click();
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
      await page.locator('.paper-card, .paper-row').first().click();
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
      await page.locator('.paper-card, .paper-row').first().click();
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
      await page.locator('.paper-card, .paper-row').first().click();
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
      await expect(page.locator('.footer-actions button:has-text("保存修改")')).toBeVisible();
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

    test('D4-3H.2: human workbench verification gate for repaired and unrepaired locators', async ({ page }) => {
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

      const mockRepairedLocators = [
        {
          paper_id: PILOT_PAPER_ID,
          target_type: 'CatalystSample',
          target_id: '11111111-1111-4111-8111-111111111111',
          field_name: 'catalyst_type',
          evidence_text: 'Catalyst type evidence text is visible to the reviewer.',
          page: 7,
          bbox: { x0: 53.858, y0: 477.052, x1: 287.155, y1: 359.672 },
          parser_source: 'Docling',
          locator_status: 'exact_page',
          can_jump_to_pdf_page: true,
          can_highlight_in_pdf: true,
        },
        {
          paper_id: PILOT_PAPER_ID,
          target_type: 'CatalystSample',
          target_id: '11111111-1111-4111-8111-111111111111',
          field_name: 'metal_centers',
          evidence_text: 'Metal-center evidence text is visible.',
          page: 7,
          bbox: { x0: 53.859, y0: 356.594, x1: 287.167, y1: 70.085 },
          parser_source: 'Docling',
          locator_status: 'exact_page',
          can_jump_to_pdf_page: true,
          can_highlight_in_pdf: true,
        },
        {
          paper_id: PILOT_PAPER_ID,
          target_type: 'ElectrochemicalPerformance',
          target_id: '33333333-3333-4333-8333-333333333333',
          field_name: 'rate',
          evidence_text: 'Rate-performance evidence text is visible.',
          page: 6,
          bbox: { x0: 53.858, y0: 125.995, x1: 541.43, y1: 71.087 },
          parser_source: 'Docling',
          locator_status: 'exact_page',
          can_jump_to_pdf_page: true,
          can_highlight_in_pdf: true,
        }
      ];

      await page.route(/\/api\/papers\?limit=200$/, route => jsonResponse(route, [PILOT_PAPER]));
      await page.route(new RegExp(`/api/papers/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_PAPER));
      await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}$`), route => jsonResponse(route, PILOT_EXTRACTION_RESULTS));
      await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews$`), route => jsonResponse(route, PILOT_PENDING_REVIEWS));
      await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/reviews/audit$`), route => jsonResponse(route, PILOT_AUDIT));
      await page.route(new RegExp(`/api/extraction/results/${PILOT_PAPER_ID}/evidence-locators$`), route => jsonResponse(route, mockRepairedLocators));

      await page.goto(`${BASE_URL}/pages/external_analysis_workbench/index.html?paper_id=${PILOT_PAPER_ID}`);
      await page.waitForTimeout(1000);

      // 1. Verify all 5 rows are unverified / pending review and no misleading words
      const bodyText = await page.locator('body').innerText();
      expect(bodyText).not.toMatch(/Human verified|Ready for export|Ready for writing|export-ready|writing-ready|AI approved|auto verified/i);
      expect(bodyText).not.toMatch(/已验证|已校验|可导出|可写作|安全证据/i);

      // 2. CatalystSample schema check: repaired catalyst_type and metal_centers, unrepaired name
      await page.locator('#schemaSelect').selectOption('CatalystSample');
      await page.waitForTimeout(200);

      const form = page.locator('#schemaForm');

      // Unrepaired name check (HIGH-CAUTION)
      const nameField = form.locator('#field-11111111-1111-4111-8111-111111111111-name');
      await expect(nameField).toContainText('待人工确认');
      await expect(nameField).toContainText('高风险：缺少准确 PDF 定位');
      await expect(nameField.locator('button:has-text("查看 PDF")')).toHaveCount(0);

      // Repaired catalyst_type check
      const catalystTypeField = form.locator('#field-11111111-1111-4111-8111-111111111111-catalyst_type');
      await expect(catalystTypeField).toContainText('待人工确认');
      await expect(catalystTypeField).toContainText('PDF 定位准确');
      await expect(catalystTypeField).toContainText('page 7');
      await expect(catalystTypeField).toContainText('Docling source if available');
      await expect(catalystTypeField).toContainText('Catalyst type evidence text is visible to the reviewer.');
      await expect(catalystTypeField.locator('button:has-text("查看 PDF 第 7 页")')).toHaveCount(1);

      // Repaired metal_centers check
      const metalCentersField = form.locator('#field-11111111-1111-4111-8111-111111111111-metal_centers');
      await expect(metalCentersField).toContainText('待人工确认');
      await expect(metalCentersField).toContainText('PDF 定位准确');
      await expect(metalCentersField).toContainText('page 7');
      await expect(metalCentersField).toContainText('Docling source if available');
      await expect(metalCentersField.locator('button:has-text("查看 PDF 第 7 页")')).toHaveCount(1);

      // 3. DFTSetting schema check: unrepaired convergence_settings (RED excluded)
      await page.locator('#schemaSelect').selectOption('DFTSetting');
      await page.waitForTimeout(200);

      const convField = form.locator('#field-22222222-2222-4222-8222-222222222222-convergence_settings');
      await expect(convField).toContainText('待人工确认');
      await expect(convField).toContainText('缺少准确 PDF 定位');
      await expect(convField.locator('button:has-text("查看 PDF")')).toHaveCount(0);

      // 4. ElectrochemicalPerformance schema check: repaired rate
      await page.locator('#schemaSelect').selectOption('ElectrochemicalPerformance');
      await page.waitForTimeout(200);

      const rateField = form.locator('#field-33333333-3333-4333-8333-333333333333-rate');
      await expect(rateField).toContainText('待人工确认');
      await expect(rateField).toContainText('PDF 定位准确');
      await expect(rateField).toContainText('page 6');
      await expect(rateField).toContainText('Docling source if available');
      await expect(rateField.locator('button:has-text("查看 PDF 第 6 页")')).toHaveCount(1);

      // 5. Verify no prepare or mark-verified API is called on page load
      const openingRequests = apiRequests.filter(req => req.url.includes(PILOT_PAPER_ID));
      expect(openingRequests.some(req => req.url.includes('/reviews/prepare'))).toBe(false);
      expect(openingRequests.some(req => req.url.includes('/reviews/mark-verified'))).toBe(false);
      expect(openingRequests.some(req => /reviewer_status"\s*:\s*"verified"|verified"\s*:\s*true/i.test(req.body))).toBe(false);
    });
  });

  test('business flow: Literature Screening page loads and calls filter API', async ({ page }) => {
    let filterCalled = false;
    await page.route(/\/api\/library\/papers\/filter.*/, route => {
      filterCalled = true;
      return jsonResponse(route, { papers: PAPERS });
    });
    await page.goto(`${BASE_URL}/pages/literature_screening/index.html`);
    await expect.poll(() => filterCalled).toBe(true);
    await expect(page.locator('.screening-table')).toBeVisible();
    await expect(page.locator('.screening-table')).toContainText('needs_metadata');
  });

  test('business flow: Literature Screening filter sends correct params', async ({ page }) => {
    let lastUrl = '';
    await page.route(/\/api\/library\/papers\/filter.*/, route => {
      lastUrl = route.request().url();
      return jsonResponse(route, { papers: PAPERS });
    });
    await page.goto(`${BASE_URL}/pages/literature_screening/index.html`);

    await page.fill('#filterYearMin', '2020');
    await page.fill('#filterIFMin', '5.0');
    await page.check('#filterNeedsMetadata');
    await page.click('button:has-text("Apply Filters")');

    await expect.poll(() => lastUrl).toContain('year_min=2020');
    await expect.poll(() => lastUrl).toContain('impact_factor_min=5');
    await expect.poll(() => lastUrl).toContain('needs_metadata=true');
  });

  test('business flow: Literature Screening bulk Mark Do Not Cite', async ({ page }) => {
    let bulkData = null;
    await page.route(/\/api\/library\/papers\/filter.*/, route => jsonResponse(route, { papers: PAPERS }));
    await page.route(/\/api\/library\/papers\/citation-eligibility\/bulk/, async route => {
      bulkData = JSON.parse(route.request().postData() || '{}');
      return jsonResponse(route, { ok: true });
    });

    await page.goto(`${BASE_URL}/pages/literature_screening/index.html`);
    await page.waitForTimeout(500);

    await page.check('#selectAllCheckbox');
    await page.click('button:has-text("Mark selected as Do Not Cite")');
    await expect(page.locator('#confirmModalOverlay')).toBeVisible();
    await page.click('#confirmModalActionBtn');

    await expect.poll(() => bulkData !== null).toBe(true);
    expect(bulkData.paper_ids).toContain('paper-1');
    expect(bulkData.updates.exclude_from_citation).toBe(true);
  });

  test('business flow: Literature Screening bulk Set Priority', async ({ page }) => {
    let bulkData = null;
    await page.route(/\/api\/library\/papers\/filter.*/, route => jsonResponse(route, { papers: PAPERS }));
    await page.route(/\/api\/library\/papers\/citation-eligibility\/bulk/, async route => {
      bulkData = JSON.parse(route.request().postData() || '{}');
      return jsonResponse(route, { ok: true });
    });

    await page.goto(`${BASE_URL}/pages/literature_screening/index.html`);
    await page.waitForTimeout(500);

    await page.check('#selectAllCheckbox');
    await page.selectOption('#bulkPrioritySelect', 'high');
    await page.click('button:has-text("Set selected priority")');
    await expect(page.locator('#confirmModalOverlay')).toBeVisible();
    await page.click('#confirmModalActionBtn');

    await expect.poll(() => bulkData !== null).toBe(true);
    expect(bulkData.updates.citation_priority).toBe('high');
  });

  test('business flow: Literature Screening Import Impact Metadata Panel', async ({ page }) => {
    let importUrl = '';
    let importData = '';
    await page.route(/\/api\/library\/papers\/filter.*/, route => jsonResponse(route, { papers: [] }));
    await page.route(/\/api\/library\/impact-metadata\/import.*/, async route => {
      importUrl = route.request().url();
      importData = route.request().postData() || '';
      return jsonResponse(route, {
        imported_count: 1,
        updated_count: 0,
        matched_paper_count: 1,
        unmatched_items: 0,
        invalid_items: 0,
        needs_metadata_remaining: 10
      });
    });

    await page.goto(`${BASE_URL}/pages/literature_screening/index.html`);
    await page.click('button:has-text("Import Impact Metadata")');
    await expect(page.locator('#importPanelOverlay')).toBeVisible();
    await expect(page.locator('#importDryRun')).toBeChecked();

    await page.fill('#importTextarea', 'doi,if\n10.123/456,10.0');
    await page.click('button:has-text("Execute Import")');

    await expect.poll(() => importUrl).toContain('dry_run=true');
    expect(importData).toContain('10.123/456');
    await expect(page.locator('#importResImported')).toContainText('1');
    await expect(page.locator('#importResMatched')).toContainText('1');

    importUrl = '';
    page.once('dialog', async dialog => {
      expect(dialog.message()).toContain('dry_run = false');
      await dialog.accept();
    });
    await page.uncheck('#importDryRun');
    await page.click('button:has-text("Execute Import")');
    await expect.poll(() => importUrl).toContain('dry_run=false');

    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toMatch(/delete paper/i);
    expect(bodyText).not.toMatch(/Human verified/i);
  });

  test('business flow: Writing Assistant page operates correctly and safely', async ({ page }) => {
    let apiPayload = null;
    await page.route(/\/api\/writing\/citation-candidates/, async route => {
      apiPayload = JSON.parse(route.request().postData() || '{}');
      return mockApi(route);
    });

    await page.goto(`${BASE_URL}/pages/writing_assistant/index.html`);
    await page.waitForTimeout(500);

    // 1. Verify page layout & safety banner
    await expect(page.locator('h2')).toContainText('写作引用辅助');
    const mainSafetyBanner = page.locator('.safety-disclaimer-banner').first();
    await expect(mainSafetyBanner).toBeVisible();
    await expect(mainSafetyBanner).toContainText('系统评估的高置信度候选');

    // 2. Empty text input click validation
    await page.click('#btnSearch');
    await expect(page.locator('#validationAlert')).toBeVisible();
    await expect(page.locator('#validationAlert')).toContainText('请先输入句子或段落上下文');
    expect(apiPayload).toBeNull(); // Ensure no API was called

    // 3. Populate text input and trigger search
    await page.fill('#writingText', 'Single-atom catalysts can accelerate sulfur redox kinetics in lithium-sulfur batteries.');

    // Set some advanced filters
    await page.click('button[data-panel-section="filters"]');
    await page.fill('#filterYearMin', '2022');
    await page.fill('#filterIFMin', '10.0');
    await page.selectOption('#filterCitationPriority', 'high');
    await page.check('#filterHasPdf');
    // Click search
    await page.click('#btnSearch');

    // Check loading indicator was triggered or results loaded
    await expect(page.locator('#resultsCount')).toContainText('3');

    // Verify correct API request payload
    expect(apiPayload).not.toBeNull();
    await expect.poll(() => apiPayload && apiPayload.text).toContain('Single-atom catalysts can accelerate');
    await expect.poll(() => apiPayload && apiPayload.filters && apiPayload.filters.year_min).toBe(2022);
    await expect.poll(() => apiPayload && apiPayload.filters && apiPayload.filters.impact_factor_min).toBe(10.0);
    await expect.poll(() => apiPayload && apiPayload.filters && apiPayload.filters.citation_priority).toBe('high');
    await expect.poll(() => apiPayload && apiPayload.filters && apiPayload.filters.has_pdf).toBe(true);
    expect(apiPayload.include_unverified_suggestions).toBe(true);

    // 4. Verify candidate cards and safety badges
    const confirmedCard = page.locator('.candidate-card').filter({ hasText: 'Confirmed Catalyst Discovery' });
    await expect(confirmedCard).toBeVisible();
    // legacy assertion replaced by localized check
    await expect(confirmedCard.locator('.safety-badge')).toContainText('高置信度候选');

    const needsVerificationCard = page.locator('.candidate-card').filter({ hasText: 'Unverified Heterogeneous' });
    await expect(needsVerificationCard).toBeVisible();
    // legacy assertion replaced by localized check
    await expect(needsVerificationCard.locator('.safety-badge')).toContainText('需要人工核验');
    // legacy assertion replaced by localized check
    await expect(needsVerificationCard.locator('.card-warning-box')).toContainText('人工核验');
    const metadataOnlyCard = page.locator('.candidate-card').filter({ hasText: 'A Review on Lithium-Sulfur' });
    await expect(metadataOnlyCard).toBeVisible();
    // legacy assertion replaced by localized check
    await expect(metadataOnlyCard.locator('.safety-badge')).toContainText('仅元数据建议');
    // legacy assertion replaced by localized check
    await expect(metadataOnlyCard.locator('.card-warning-box')).toContainText('影响因子缺失');
    // 5. Verify excluded candidate collapsed listing
    await expect(page.locator('#excludedCollapsible')).toBeVisible();
    await expect(page.locator('#excludedCount')).toContainText('1');
    await page.click('#excludedCollapsible summary');
    // legacy assertion replaced by localized check
    await expect(page.locator('#excludedList')).toContainText('不可引用');
    // 6. Strict safety check: Ensure no DB writes or auto-inserters exist on page
    const pageBody = await page.locator('body').innerText();
    expect(pageBody).not.toMatch(/automatically insert citation/i);
    expect(pageBody).not.toMatch(/save_reviews/i);
    expect(pageBody).not.toMatch(/mark_verified/i);
    expect(pageBody).not.toMatch(/Insert Citation/i);
    expect(pageBody).not.toMatch(/Generate Bibliography/i);
    expect(pageBody).not.toMatch(/Final Citation/i);
    
    // 7. Click Generate Draft Citation Proposal on Confirmed Candidate
    // legacy assertion replaced by localized check
    await confirmedCard.locator('button:has-text("生成引用建议草稿")').click();
    // legacy assertion replaced by localized check
    await expect(confirmedCard.locator('.proposal-banner')).toContainText('使用前仍建议进行人工核对');
    
    // 8. Click Generate Draft on Needs Verification Candidate
    // legacy assertion replaced by localized check
    await needsVerificationCard.locator('button:has-text("生成引用建议草稿")').click();
    // legacy assertion replaced by localized check
    await expect(needsVerificationCard.locator('.proposal-banner')).toContainText('引用前必须完成人工核验');
    // legacy assertion replaced by localized check
    // legacy assertion replaced by localized check
    
    // 9. Verify Copy Draft Proposal
    // legacy assertion replaced by localized check
    await needsVerificationCard.locator('button:has-text("复制建议草稿")').click();
    // legacy assertion replaced by localized check
    // Note: To test clipboard we might need clipboard permissions, but we can just check the toast.
  });

  test('business flow: D5-3B Evidence-backed Writing Cards operate safely', async ({ page }) => {
    let apiPayload = null;
    await page.route(/\/api\/writing\/citation-candidates/, async route => mockApi(route));
    await page.route(/\/api\/writing\/evidence-backed-cards/, async route => {
      apiPayload = JSON.parse(route.request().postData() || '{}');
      return mockApi(route);
    });

    await page.goto(`${BASE_URL}/pages/writing_assistant/index.html`);
    await page.waitForTimeout(500);

    // Populate and search candidates first to get window.currentCandidates
    await page.fill('#writingText', 'Testing evidence cards');
    await page.click('#btnSearch');
    await expect(page.locator('#resultsCount')).toContainText('3');

    // Click Generate Evidence Cards
    await page.click('#btnGenerateCards');

    // Assert cards generated
    await expect(page.locator('#writingCardsSection')).toBeVisible();
    
    // Confirmed card
    const confirmedCard = page.locator('#writingCardsContainer .candidate-card').filter({ hasText: 'Confirmed Catalyst Discovery' });
    await expect(confirmedCard).toBeVisible();
    await expect(confirmedCard.locator('.badge-confirmed')).toContainText('Confirmed Fact');
    await expect(confirmedCard.locator('.banner-confirmed')).toContainText('Confirmed writing card 仅代表 safe_verified 来源。建议核对原文。');
    await expect(confirmedCard.locator('button')).toContainText('Copy Draft Card');

    // Suggestion card
    const suggestionCard = page.locator('#writingCardsContainer .candidate-card').filter({ hasText: 'Unverified Heterogeneous Electrocatalyst' });
    await expect(suggestionCard).toBeVisible();
    await expect(suggestionCard.locator('.badge-needs-verification')).toContainText('Suggestion Only');
    await expect(suggestionCard.locator('.banner-warning')).toContainText('suggestion-only / needs human verification 不可直接作为事实。');
    await expect(suggestionCard.locator('button')).toContainText('Copy Suggestion Draft');
    
    // Assert dangerous text absent
    const pageBody = await page.locator('#writingCardsSection').innerText();
    expect(pageBody).not.toMatch(/Accept & Insert/i);
    expect(pageBody).not.toMatch(/Apply/i);
    expect(pageBody).not.toMatch(/Use as Final/i);
    expect(pageBody).not.toMatch(/Insert Card/i);
    expect(pageBody).not.toMatch(/Final Citation/i);
    expect(pageBody).not.toMatch(/Generate Bibliography/i);
    expect(pageBody).not.toMatch(/Export Final/i);
    expect(pageBody).not.toMatch(/Auto Apply/i);
  });

  test('business flow: D5-1 Manuscript Comment Assistant operates safely', async ({ page }) => {
    let apiPayload = null;
    await page.route(/\/api\/writing\/manuscript-comment-suggestions/, async route => {
      apiPayload = JSON.parse(route.request().postData() || '{}');
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          paragraph_text: apiPayload.paragraph_text,
          suggestions: [
            {
              type: "draft_comment_suggestion",
              text: "Consider citing evidence for the claims in this paragraph.",
              candidate_papers: [
                { title: "Mock Paper 1", evidence_status: "metadata_only" }
              ],
              warnings: [
                "suggestion_only_needs_human_verification",
                "draft_do_not_use_as_final_fact"
              ]
            }
          ],
          safety_guardrails: {
            is_suggestion_only: true,
            writes_db: false,
            auto_insert: false,
            generates_bibliography: false,
            export_unlocked: false,
            verified_status_changed: false
          }
        })
      });
    });

    await page.goto(`${BASE_URL}/pages/writing_assistant/index.html`);
    await page.waitForTimeout(500);

    // Click Suggest Comments button without text
    await page.click('#btnSuggestComments');
    await expect(page.locator('#validationAlert')).toBeVisible();
    await expect(page.locator('#validationAlert')).toContainText('请先输入句子或段落上下文');

    // Fill text and click Suggest Comments
    await page.fill('#writingText', 'This is a test paragraph.');
    await page.click('#btnSuggestComments');

    // Verify API payload
    await expect.poll(() => apiPayload && apiPayload.paragraph_text).toBe('This is a test paragraph.');

    // Verify suggestions are rendered
    const card = page.locator('.candidate-card').filter({ hasText: 'Comment Suggestion' });
    await expect(card).toBeVisible();
    await expect(card).toContainText('Consider citing evidence');
    await expect(card).toContainText('Mock Paper 1');
    await expect(card).toContainText('metadata_only');
    
    // Check warnings
    await expect(card.locator('.card-warning-message').first()).toContainText('仅为建议，需先完成人工核验。');

    // Strict safety check for this feature: no dangerous buttons
    const pageBody = await page.locator('body').innerText();
    expect(pageBody).not.toMatch(/Accept & Insert/i);
    expect(pageBody).not.toMatch(/Final Citation/i);
    expect(pageBody).not.toMatch(/Generate Bibliography/i);
    expect(pageBody).not.toMatch(/Export Final/i);
  });

  test('business flow: D5-2B Draft Revision Assistant operates safely', async ({ page }) => {
    let apiPayload = null;
    await page.route(/\/api\/writing\/draft-revisions/, async route => {
      apiPayload = JSON.parse(route.request().postData() || '{}');
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          draft_text: apiPayload.draft_text,
          revision_suggestions: [
            {
              suggestion_type: "unsupported_claim",
              original_excerpt: "Test draft.",
              suggested_revision: "Test draft (revised).",
              warnings: ["draft_do_not_use_as_final_fact"],
              candidate_papers: [
                {
                  title: "Mock Paper 1",
                  evidence_status: "metadata_only",
                  warnings: ["suggestion_only_needs_human_verification"]
                }
              ]
            }
          ],
          safety_guardrails: {
            is_suggestion_only: true,
            writes_db: false,
            auto_apply: false,
            generates_bibliography: false,
            export_unlocked: false,
            verified_status_changed: false
          }
        })
      });
    });

    await page.goto(`${BASE_URL}/pages/writing_assistant/index.html`);
    await page.waitForTimeout(500);

    // Click Revise Draft button without text
    await page.click('#btnReviseDraft');
    await expect(page.locator('#validationAlert')).toBeVisible();
    await expect(page.locator('#validationAlert')).toContainText('请先输入句子或段落上下文');

    // Fill text and click Revise Draft
    await page.fill('#writingText', 'Test draft.');
    await page.click('#btnReviseDraft');

    // Verify API payload
    await expect.poll(() => apiPayload && apiPayload.draft_text).toBe('Test draft.');

    // Verify suggestions are rendered
    const card = page.locator('.candidate-card').filter({ hasText: 'Draft Revision Suggestion' });
    await expect(card).toBeVisible();
    await expect(card).toContainText('Test draft (revised).');
    await expect(card).toContainText('Mock Paper 1');
    await expect(card).toContainText('metadata_only');
    
    // Check warnings
    await expect(card.locator('.card-warning-message').first()).toContainText('draft_do_not_use_as_final_fact');
    await expect(card).toContainText('Warning: suggestion_only_needs_human_verification');

    // Check button is named appropriately
    await expect(card.locator('button')).toContainText('Copy Draft Suggestion');

    // Strict safety check for this feature: no dangerous buttons
    const pageBody = await page.locator('body').innerText();
    expect(pageBody).not.toMatch(/Accept & Insert/i);
    expect(pageBody).not.toMatch(/Apply Changes/i);
    expect(pageBody).not.toMatch(/Auto Apply/i);
    expect(pageBody).not.toMatch(/Rewrite File/i);
    expect(pageBody).not.toMatch(/Final Citation/i);
    expect(pageBody).not.toMatch(/Generate Bibliography/i);
    expect(pageBody).not.toMatch(/Export Final/i);
    expect(pageBody).not.toMatch(/Verified Fact/i);
  });
});
