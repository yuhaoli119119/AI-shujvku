const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';

const ACTIVE_LIBRARY = {
  name: 'Active Library',
  is_active: true,
  root_path: '/libraries/active',
  paper_count: 3,
};

const ALT_LIBRARY = {
  name: 'Archive Library',
  is_active: false,
  root_path: '/libraries/archive',
  paper_count: 1,
};

function makeSetting(id, functional, software) {
  return {
    dft_setting_id: id,
    functional,
    software,
    dispersion_correction: null,
    pseudopotential: null,
    cutoff_energy_ev: null,
    k_points: null,
    convergence_settings: null,
    vacuum_thickness_a: null,
    raw_json: null,
    match_score: null,
    match_reasons: null,
  };
}

const PBE_SETTING = makeSetting('setting-pbe', 'PBE', 'VASP');
const RPBE_SETTING = makeSetting('setting-rpbe', 'RPBE', 'Quantum ESPRESSO');
const PBE0_SETTING = makeSetting('setting-pbe0', 'PBE0', 'CP2K');

const DATASET_PAYLOAD = {
  metadata: {
    dataset_version: 'dft-ml-dataset-v0.2',
    schema_version: 'dft_results_ml_v2',
    created_at: '2026-06-18T12:34:56Z',
    filters: {
      property_type: null,
      adsorbate: null,
      year_min: null,
      year_max: null,
      library_name: 'Active Library',
      paper_id: null,
    },
    safety_gate: 'safe_verified_with_required_evidence',
    eligible_count: 5,
    blocked_count: 1,
    blocked_reasons: {
      missing_review: 1,
    },
    total_candidates: 6,
    numeric_record_count: 4,
    numeric_ml_ready_count: 2,
    numeric_blocked_count: 2,
    lm_record_count: 1,
    history_backfill_mode: 'export_time_enrichment',
    ml_setting_field: 'linked_dft_setting',
  },
  records: [
    {
      record_id: 'record-ready-1',
      paper: {
        paper_id: 'paper-ready',
        title: 'DFT Ready Paper',
        doi: '10.1000/ready',
        journal: 'Journal of Ready Catalysis',
        year: 2024,
        authors: ['A. Ready'],
      },
      target: {
        property_type: 'adsorption_energy',
        normalized_property_type: 'adsorption_energy',
        canonical_property_type: 'adsorption_energy',
        property_family: 'energetics',
        property_subtype: 'adsorption_energy',
        physical_dimension: 'energy',
        ml_role: 'target',
        adsorbate: 'Li2S4',
        canonical_adsorbate: 'Li2S4',
        value: -1.23,
        unit: 'eV',
        reaction_step: 'Li2S4 adsorption',
        normalized_value: -1.23,
        normalized_unit: 'eV',
        normalization_status: 'normalized',
        normalization_blockers: [],
        normalization_basis: null,
      },
      catalyst: {
        catalyst_sample_id: 'cat-ready',
        name: 'Fe-N4/C',
        catalyst_type: 'single_atom',
        metal_centers: ['Fe'],
        coordination: 'N4',
        support: 'carbon',
        synthesis_method: null,
        evidence_strength: 'verified',
      },
      catalyst_candidates: [],
      dft_settings: [PBE_SETTING],
      paper_level_dft_settings: [PBE_SETTING],
      linked_dft_setting: PBE_SETTING,
      setting_link_status: 'clear_primary',
      setting_link_reason: 'single_result_level_match',
      setting_link_candidates: [PBE_SETTING],
      recommended_ml_setting_field: 'linked_dft_setting',
      provenance: {
        source_section: 'Results',
        source_figure: 'Fig. 2a',
        evidence_text: 'The adsorption energy of Li2S4 on Fe-N4/C is -1.23 eV.',
        confidence: 0.95,
        review_status: 'verified',
        review_gate_status: 'safe_verified',
        provenance_level: 'exact_pdf_page',
        locator_status: 'exact_page',
        gate_reasons: ['verified_review', 'exact_page_locator'],
        safety_gate: 'safe_verified_with_required_evidence',
        evidence_payload: {
          material_identity: 'Fe-N4/C',
          surface_facet: '(111)',
          adsorption_site: 'top',
          coverage: '0.25 ML',
        },
      },
      descriptor_fields: {
        d_band_center: {
          record_id: 'record-descriptor-1',
          canonical_property_type: 'd_band_center',
          property_subtype: 'd_band_center',
          value: -1.75,
          unit: 'eV',
          raw_value: -1.75,
          raw_unit: 'eV',
          adsorbate: 'Li2S4',
          setting_link_status: 'clear_primary',
          instance_key: 'instance:ready',
        },
      },
      sample_context: {
        sample_key: 'instance:ready',
        instance_key: 'instance:ready',
        instance_anchor_key: 'anchor:ready',
        material_scope_key: 'material:ready',
        target_context_key: 'adsorption_energy',
        instance_scope_level: 'target_context',
        instance_components: {
          material_identity: 'Fe-N4/C',
          surface_facet: '(111)',
          adsorption_site: 'top',
          coverage: '0.25 ML',
        },
        history_backfill_applied: true,
        numeric_record_count: 2,
        target_record_count: 1,
        descriptor_record_count: 1,
        material_scope_count: 2,
        descriptor_instance_ambiguous: false,
      },
      ml_blockers: [],
      ml_readiness_score: 100,
      is_ml_ready: true,
    },
    {
      record_id: 'record-barrier-ambiguous',
      paper: {
        paper_id: 'paper-barrier',
        title: 'Li2S Barrier Study',
        doi: '10.1000/barrier',
        journal: 'Journal of Barrier Chemistry',
        year: 2023,
        authors: ['B. Barrier'],
      },
      target: {
        property_type: 'li2s_decomposition_barrier',
        normalized_property_type: 'reaction_barrier',
        canonical_property_type: 'reaction_barrier',
        property_family: 'kinetics',
        property_subtype: 'li2s_decomposition_barrier',
        physical_dimension: 'energy',
        ml_role: 'target',
        adsorbate: 'Li2S',
        canonical_adsorbate: 'Li2S',
        value: 420,
        unit: 'meV',
        reaction_step: 'Li2S decomposition',
        normalized_value: 0.42,
        normalized_unit: 'eV',
        normalization_status: 'normalized',
        normalization_blockers: [],
        normalization_basis: null,
      },
      catalyst: {
        catalyst_sample_id: 'cat-barrier',
        name: 'Ni3S2',
        catalyst_type: 'heterogeneous',
        metal_centers: ['Ni'],
        coordination: null,
        support: null,
        synthesis_method: null,
        evidence_strength: 'verified',
      },
      catalyst_candidates: [],
      dft_settings: [PBE_SETTING, RPBE_SETTING],
      paper_level_dft_settings: [PBE_SETTING, RPBE_SETTING],
      linked_dft_setting: null,
      setting_link_status: 'ambiguous',
      setting_link_reason: 'multiple_candidate_settings',
      setting_link_candidates: [PBE_SETTING, RPBE_SETTING],
      recommended_ml_setting_field: 'linked_dft_setting',
      provenance: {
        source_section: 'Discussion',
        source_figure: 'Scheme 1',
        evidence_text: 'The Li2S decomposition barrier is 420 meV on the Ni3S2 surface.',
        confidence: 0.9,
        review_status: 'verified',
        review_gate_status: 'safe_verified',
        provenance_level: 'exact_pdf_page',
        locator_status: 'exact_page',
        gate_reasons: ['verified_review', 'exact_page_locator'],
        safety_gate: 'safe_verified_with_required_evidence',
        evidence_payload: {
          material_identity: 'Ni3S2',
          surface_facet: '(104)',
          adsorption_site: 'bridge',
          coverage: '0.50 ML',
          slab: '4-layer',
          termination: 'S-terminated',
        },
      },
      descriptor_fields: {},
      sample_context: {
        sample_key: 'instance:barrier',
        instance_key: 'instance:barrier',
        instance_anchor_key: 'anchor:barrier',
        material_scope_key: 'material:barrier',
        target_context_key: 'reaction_barrier',
        instance_scope_level: 'target_context',
        instance_components: {
          material_identity: 'Ni3S2',
          surface_facet: '(104)',
          adsorption_site: 'bridge',
          coverage: '0.50 ML',
          slab: '4-layer',
          termination: 'S-terminated',
        },
        history_backfill_applied: true,
        numeric_record_count: 1,
        target_record_count: 1,
        descriptor_record_count: 0,
        material_scope_count: 1,
        descriptor_instance_ambiguous: false,
      },
      ml_blockers: ['ambiguous_result_setting_link'],
      ml_readiness_score: 65,
      is_ml_ready: false,
    },
    {
      record_id: 'record-basis-blocked',
      paper: {
        paper_id: 'paper-basis',
        title: 'Basis Qualified Energy',
        doi: '10.1000/basis',
        journal: 'Units & Models',
        year: 2022,
        authors: ['C. Basis'],
      },
      target: {
        property_type: 'adsorption_energy',
        normalized_property_type: 'adsorption_energy',
        canonical_property_type: 'adsorption_energy',
        property_family: 'energetics',
        property_subtype: 'adsorption_energy',
        physical_dimension: 'energy',
        ml_role: 'target',
        adsorbate: 'H*',
        canonical_adsorbate: 'H*',
        value: -0.38,
        unit: 'eV/atom',
        reaction_step: 'H adsorption',
        normalized_value: null,
        normalized_unit: null,
        normalization_status: 'basis_qualified',
        normalization_blockers: ['energy_basis_requires_explicit_modeling'],
        normalization_basis: 'per_atom',
      },
      catalyst: {
        catalyst_sample_id: 'cat-basis',
        name: 'MoS2 edge',
        catalyst_type: 'heterogeneous',
        metal_centers: ['Mo'],
        coordination: null,
        support: null,
        synthesis_method: null,
        evidence_strength: 'verified',
      },
      catalyst_candidates: [],
      dft_settings: [PBE0_SETTING],
      paper_level_dft_settings: [PBE0_SETTING],
      linked_dft_setting: PBE0_SETTING,
      setting_link_status: 'clear_primary',
      setting_link_reason: 'single_result_level_match',
      setting_link_candidates: [PBE0_SETTING],
      recommended_ml_setting_field: 'linked_dft_setting',
      provenance: {
        source_section: 'Supplementary Results',
        source_figure: null,
        evidence_text: 'The H adsorption energy is reported as -0.38 eV/atom.',
        confidence: 0.88,
        review_status: 'verified',
        review_gate_status: 'safe_verified',
        provenance_level: 'exact_pdf_page',
        locator_status: 'exact_page',
        gate_reasons: ['verified_review', 'exact_page_locator'],
        safety_gate: 'safe_verified_with_required_evidence',
        evidence_payload: {
          material_identity: 'MoS2 edge',
          structure_name: 'edge model',
        },
      },
      descriptor_fields: {},
      sample_context: {
        sample_key: 'instance:basis',
        instance_key: 'instance:basis',
        instance_anchor_key: 'anchor:basis',
        material_scope_key: 'material:basis',
        target_context_key: 'adsorption_energy',
        instance_scope_level: 'target_context',
        instance_components: {
          material_identity: 'MoS2 edge',
          structure_name: 'edge model',
        },
        history_backfill_applied: true,
        numeric_record_count: 1,
        target_record_count: 1,
        descriptor_record_count: 0,
        material_scope_count: 1,
        descriptor_instance_ambiguous: false,
      },
      ml_blockers: ['energy_basis_requires_explicit_modeling'],
      ml_readiness_score: 70,
      is_ml_ready: false,
    },
    {
      record_id: 'record-descriptor-1',
      paper: {
        paper_id: 'paper-ready',
        title: 'DFT Ready Paper',
        doi: '10.1000/ready',
        journal: 'Journal of Ready Catalysis',
        year: 2024,
        authors: ['A. Ready'],
      },
      target: {
        property_type: 'd_band_center',
        normalized_property_type: 'd_band_center',
        canonical_property_type: 'd_band_center',
        property_family: 'electronic_descriptor',
        property_subtype: 'd_band_center',
        physical_dimension: 'energy',
        ml_role: 'descriptor',
        adsorbate: null,
        canonical_adsorbate: null,
        value: -1.75,
        unit: 'eV',
        reaction_step: null,
        normalized_value: -1.75,
        normalized_unit: 'eV',
        normalization_status: 'normalized',
        normalization_blockers: [],
        normalization_basis: null,
      },
      catalyst: {
        catalyst_sample_id: 'cat-ready',
        name: 'Fe-N4/C',
        catalyst_type: 'single_atom',
        metal_centers: ['Fe'],
        coordination: 'N4',
        support: 'carbon',
        synthesis_method: null,
        evidence_strength: 'verified',
      },
      catalyst_candidates: [],
      dft_settings: [PBE_SETTING],
      paper_level_dft_settings: [PBE_SETTING],
      linked_dft_setting: PBE_SETTING,
      setting_link_status: 'clear_primary',
      setting_link_reason: 'single_result_level_match',
      setting_link_candidates: [PBE_SETTING],
      recommended_ml_setting_field: 'linked_dft_setting',
      provenance: {
        source_section: 'Results',
        source_figure: 'Fig. 2b',
        evidence_text: 'The d-band center is -1.75 eV for Fe-N4/C.',
        confidence: 0.93,
        review_status: 'verified',
        review_gate_status: 'safe_verified',
        provenance_level: 'exact_pdf_page',
        locator_status: 'exact_page',
        gate_reasons: ['verified_review', 'exact_page_locator'],
        safety_gate: 'safe_verified_with_required_evidence',
        evidence_payload: {
          material_identity: 'Fe-N4/C',
        },
      },
      descriptor_fields: {},
      sample_context: {
        sample_key: 'instance:ready',
        instance_key: 'instance:ready',
        instance_anchor_key: 'anchor:ready',
        material_scope_key: 'material:ready',
        target_context_key: 'descriptor_adsorption_energy',
        instance_scope_level: 'target_context',
        instance_components: {
          material_identity: 'Fe-N4/C',
        },
        history_backfill_applied: true,
        numeric_record_count: 2,
        target_record_count: 1,
        descriptor_record_count: 1,
        material_scope_count: 2,
        descriptor_instance_ambiguous: false,
      },
      ml_blockers: [],
      ml_readiness_score: 95,
      is_ml_ready: true,
    },
  ],
  lm_records: [
    {
      record_id: 'lm-claim-1',
      paper: {
        paper_id: 'paper-lm',
        title: 'LM Support Paper',
        doi: '10.1000/lm',
        journal: 'LM Notes',
        year: 2021,
        authors: ['L. Model'],
      },
      catalyst: {
        catalyst_sample_id: 'cat-lm',
        name: 'Co-N-C',
        catalyst_type: 'single_atom',
        metal_centers: ['Co'],
        coordination: null,
        support: 'carbon',
        synthesis_method: null,
        evidence_strength: 'verified',
      },
      catalyst_candidates: [],
      dft_settings: [],
      paper_level_dft_settings: [],
      linked_dft_setting: null,
      setting_link_status: 'missing',
      setting_link_reason: 'text_only_claim',
      setting_link_candidates: [],
      recommended_ml_setting_field: 'linked_dft_setting',
      provenance: {
        source_section: 'Discussion',
        source_figure: null,
        evidence_text: 'Charge redistribution accelerates sulfur reduction.',
        confidence: 0.78,
        review_status: 'verified',
        review_gate_status: 'safe_verified',
        provenance_level: 'exact_pdf_page',
        locator_status: 'exact_page',
        gate_reasons: ['verified_review', 'exact_page_locator'],
        safety_gate: 'safe_verified_with_required_evidence',
        evidence_payload: null,
      },
      sample_context: {
        sample_key: 'lm-sample',
        instance_key: 'lm-instance',
        instance_anchor_key: 'lm-anchor',
        material_scope_key: 'lm-material',
        target_context_key: 'lm_text',
        instance_scope_level: 'target_context',
        instance_components: {
          material_identity: 'Co-N-C',
        },
        history_backfill_applied: true,
      },
      claim: {
        property_type: 'mechanistic_claim',
        normalized_property_type: 'mechanistic_claim',
        canonical_property_type: 'mechanistic_claim',
        property_family: 'textual_claim',
        property_subtype: 'charge_redistribution',
        physical_dimension: 'text',
        ml_role: 'lm_auxiliary',
        adsorbate: null,
        canonical_adsorbate: null,
        value: null,
        unit: null,
        reaction_step: null,
        normalized_value: null,
        normalized_unit: null,
        normalization_status: 'text_only',
        normalization_blockers: [],
        normalization_basis: null,
        evidence_text: 'Charge redistribution accelerates sulfur reduction.',
      },
    },
  ],
};

async function installMockApi(page) {
  let lastDatasetUrl = '';
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route(/\/api\/libraries$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([ACTIVE_LIBRARY, ALT_LIBRARY]),
  }));
  await page.route(/\/api\/papers\/export\/dft-dataset.*/, route => {
    lastDatasetUrl = route.request().url();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(DATASET_PAYLOAD),
    });
  });
  return {
    getLastDatasetUrl: () => lastDatasetUrl,
  };
}

test.describe('DFT ML-ready dataset page', () => {
  test('renders v2 summary, descriptor column, and detail context', async ({ page }) => {
    await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await expect(page.locator('h1')).toContainText('DFT 机器学习数据集');
    await expect(page.locator('#schemaVersionBadge')).toContainText('dft_results_ml_v2');
    await expect(page.locator('#statTotalCandidates')).toContainText('6');
    await expect(page.locator('#statNumericReadyCount')).toContainText('2');
    await expect(page.locator('#resultsMeta')).toContainText('LM 辅助记录 1 条');
    await expect(page.locator('#recordsTableBody')).toContainText('反应势垒（reaction_barrier）');
    await expect(page.locator('#recordsTableBody')).toContainText('li2s_decomposition_barrier');
    await expect(page.locator('#recordsTableBody')).toContainText('-1.75 eV');
    await expect(page.locator('#recordsTableBody')).toContainText('精确页码（exact_page） / 安全通过（safe_verified）');

    await page.locator('button[data-record-id="record-barrier-ambiguous"]').click();
    await expect(page.locator('.detail-row')).toContainText('surface_facet');
    await expect(page.locator('.detail-row')).toContainText('(104)');
    await expect(page.locator('.detail-row')).toContainText('paper_level_dft_settings / dft_settings 仅供审计与兼容');
  });

  test('supports blocker filtering and server-side year/library refresh params', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await page.selectOption('#readinessFilter', 'blocked');
    await expect(page.locator('#recordsTableBody')).not.toContainText('DFT Ready Paper');
    await expect(page.locator('#recordsTableBody')).toContainText('Li2S Barrier Study');
    await expect(page.locator('#recordsTableBody')).toContainText('Basis Qualified Energy');

    await page.selectOption('#blockerFilter', 'ambiguous_result_setting_link');
    await expect(page.locator('#recordsTableBody')).toContainText('Li2S Barrier Study');
    await expect(page.locator('#recordsTableBody')).not.toContainText('Basis Qualified Energy');
    await expect(page.locator('#recordsTableBody')).toContainText('存在歧义（ambiguous）');

    await page.fill('#yearMinFilter', '2024');
    await page.click('#applyServerFiltersButton');
    await expect.poll(() => mockState.getLastDatasetUrl()).toContain('library_name=Active+Library');
    await expect.poll(() => mockState.getLastDatasetUrl()).toContain('year_min=2024');
  });

  test('reuses the shared stored library selection before falling back to the active library', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.addInitScript(() => {
      window.localStorage.setItem('litai_current_library', 'Archive Library');
    });

    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await expect(page.locator('#libraryFilter')).toHaveValue('Archive Library');
    await expect(page.locator('#libraryBadge')).toContainText('Archive Library');
    await expect.poll(() => mockState.getLastDatasetUrl()).toContain('library_name=Archive+Library');
  });

  test('exports ready-only CSV and prefers linked_dft_setting over paper-level settings', async ({ page }) => {
    await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    const result = await page.evaluate(payload => {
      const utils = window.DFTMLDatasetUtils;
      return {
        csv: utils.buildMlReadyCsv(payload.records),
        readySetting: utils.getPreferredMlSetting(payload.records[0]),
        ambiguousSetting: utils.getPreferredMlSetting(payload.records[1]),
      };
    }, DATASET_PAYLOAD);

    expect(result.readySetting.functional).toBe('PBE');
    expect(result.readySetting.software).toBe('VASP');
    expect(result.ambiguousSetting).toBeNull();
    expect(result.csv).toContain('record-ready-1');
    expect(result.csv).toContain('record-descriptor-1');
    expect(result.csv).toContain('-1.75');
    expect(result.csv).toContain('PBE,VASP');
    expect(result.csv).not.toContain('record-barrier-ambiguous');
    expect(result.csv).not.toContain('record-basis-blocked');
    expect(result.csv).not.toContain('PBE0,CP2K');
  });
});
