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
    numeric_record_count: 5,
    numeric_ml_ready_count: 3,
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
        normalized_property_type: 'li2s_decomposition_barrier',
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
        property_type: 'gibbs_free_energy_change',
        normalized_property_type: 'gibbs_free_energy_change',
        canonical_property_type: 'gibbs_free_energy_change',
        property_family: 'thermodynamics',
        property_subtype: 'gibbs_free_energy_change',
        physical_dimension: 'energy',
        ml_role: 'target',
        adsorbate: 'Li2S4',
        canonical_adsorbate: 'Li2S4',
        value: -0.38,
        unit: 'eV/atom',
        reaction_step: 'RDS',
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
        evidence_text: 'The RDS Gibbs free energy is reported as -0.38 eV/atom.',
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
    {
      record_id: 'record-migration-ready',
      paper: {
        paper_id: 'paper-migration',
        title: 'Migration Barrier Paper',
        doi: '10.1000/migration',
        journal: 'Transport Chemistry',
        year: 2024,
        authors: ['M. Migration'],
      },
      target: {
        property_type: 'migration_barrier',
        normalized_property_type: 'migration_barrier',
        canonical_property_type: 'reaction_barrier',
        property_family: 'kinetics',
        property_subtype: 'migration_barrier',
        physical_dimension: 'energy',
        ml_role: 'target',
        adsorbate: 'Li+',
        canonical_adsorbate: 'Li+',
        value: 0.18,
        unit: 'eV',
        reaction_step: 'Li+ migration',
        normalized_value: 0.18,
        normalized_unit: 'eV',
        normalization_status: 'normalized',
        normalization_blockers: [],
        normalization_basis: null,
      },
      catalyst: {
        catalyst_sample_id: 'cat-migration',
        name: 'Co-N-C',
        catalyst_type: 'single_atom',
        metal_centers: ['Co'],
        coordination: 'Co-N4',
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
        source_section: 'Kinetics',
        source_figure: 'Fig. 3',
        evidence_text: 'The Li+ migration barrier is 0.18 eV on Co-N-C.',
        confidence: 0.91,
        review_status: 'verified',
        review_gate_status: 'safe_verified',
        provenance_level: 'exact_pdf_page',
        locator_status: 'exact_page',
        gate_reasons: ['verified_review', 'exact_page_locator'],
        safety_gate: 'safe_verified_with_required_evidence',
        evidence_payload: {
          material_identity: 'Co-N-C',
          surface_facet: '(100)',
        },
      },
      descriptor_fields: {},
      sample_context: {
        sample_key: 'instance:migration',
        instance_key: 'instance:migration',
        instance_anchor_key: 'anchor:migration',
        material_scope_key: 'material:migration',
        target_context_key: 'reaction_barrier',
        instance_scope_level: 'target_context',
        instance_components: {
          material_identity: 'Co-N-C',
          surface_facet: '(100)',
        },
        history_backfill_applied: true,
        numeric_record_count: 1,
        target_record_count: 1,
        descriptor_record_count: 0,
        material_scope_count: 1,
        descriptor_instance_ambiguous: false,
      },
      ml_blockers: [],
      ml_readiness_score: 92,
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

function makeV3Manifest(task) {
  if (task === 'reaction_barrier') {
    return {
      schema: 'dft_results_ml_v3',
      version: 'dft-ml-dataset-v0.3',
      task: 'reaction_barrier',
      profile: 'SRR_LiS',
      source_candidate_count: 8,
      candidate_count: 3,
      task_candidate_count: 2,
      returned_count: 1,
      label_ready_count: 1,
      tabular_ready_count: 1,
      excluded_counts: {
        unknown_reaction_type: 4,
        feature_blocked: 1,
      },
    };
  }
  if (task === 'rds_gibbs_free_energy') {
    return {
      schema: 'dft_results_ml_v3',
      version: 'dft-ml-dataset-v0.3',
      task: 'SRR_LiS:rds_gibbs_free_energy',
      profile: 'SRR_LiS',
      source_candidate_count: 8,
      candidate_count: 2,
      task_candidate_count: 1,
      returned_count: 1,
      label_ready_count: 1,
      tabular_ready_count: 1,
      excluded_counts: {
        missing_rds_semantics: 1,
      },
    };
  }
  return {
    schema: 'dft_results_ml_v3',
    version: 'dft-ml-dataset-v0.3',
    task: 'adsorption_energy',
    profile: 'SRR_LiS',
    source_candidate_count: 8,
    candidate_count: 3,
    task_candidate_count: 1,
    returned_count: 0,
    label_ready_count: 0,
    tabular_ready_count: 0,
    excluded_counts: {
      unknown_reaction_type: 4,
      missing_safe_review: 1,
    },
  };
}

function makeV4Payload(task, readyOnly = true) {
  if (task === 'li2s_barrier') {
    return {
      schema_version: 'project_library_ml_export_v4',
      read_only: true,
      auto_verification_applied: false,
      status: 'ready',
      manifest: {
        schema_version: 'project_library_ml_export_v4',
        dataset_version: 'project-library-ml-export-v4.0',
        context_key: 'li_s_sac_dac',
        library_name: 'Active Library',
        task: 'li2s_barrier',
        ready_only: readyOnly,
        candidate_count: 3,
        candidate_sample_count: 2,
        returned_count: readyOnly ? 1 : 3,
        returned_sample_count: readyOnly ? 1 : 2,
        ml_ready_count: 1,
        sample_ml_ready_count: 1,
        blocked_count: readyOnly ? 0 : 2,
        sample_blocked_count: readyOnly ? 0 : 1,
        blocker_counts: readyOnly ? {} : {
          missing_reaction_step: 1,
          energy_kind_task_mismatch: 1,
        },
        sample_blocker_counts: readyOnly ? {} : {
          missing_reaction_step: 1,
          energy_kind_task_mismatch: 1,
        },
        database_write_authority: 'user_submit_only',
        ai_consensus_auto_adopt_allowed: false,
      },
      records: [],
      sample_records: [
        {
          sample_id: 'paper:p1|catalyst:c1|site:li2s',
          sample_unit: 'active_site_instance',
          paper_id: 'p1',
          title: 'Li-S Barrier Paper',
          task: 'li2s_barrier',
          catalyst_sample_id: 'c1',
          catalyst_name: 'FeCo-NC',
          catalyst_type: 'DAC',
          metal_centers: ['Fe', 'Co'],
          active_site_instance_key: 'paper:p1|catalyst:c1|site:li2s',
          task_labels: [
            {
              record_id: 'v4-record-1',
              label_name: 'li2s_barrier_eV',
              label_value: 0.65,
              label_unit: 'eV',
              adsorbate: 'Li2S',
              reaction_step: 'Li2S decomposition',
              ml_ready: true,
              blockers: [],
            },
          ],
          wide_properties: {
            adsorption_energy_li2s_ev: -1.1,
            li2s_decomposition_barrier_ev: 0.65,
            bader_charge_li2s_e: 0.21,
          },
          property_group_counts: {
            adsorbate_properties: 1,
            reaction_step_properties: 1,
            electronic_properties: 1,
          },
          blockers: [],
          ml_ready: true,
        },
        ...(readyOnly ? [] : [{
          sample_id: 'paper:p1|catalyst:c2|site:unknown',
          sample_unit: 'active_site_instance',
          paper_id: 'p1',
          title: 'Li-S Barrier Paper',
          task: 'li2s_barrier',
          catalyst_sample_id: 'c2',
          catalyst_name: 'Fe-NC',
          catalyst_type: 'SAC',
          metal_centers: ['Fe'],
          active_site_instance_key: 'paper:p1|catalyst:c2|site:unknown',
          task_labels: [
            {
              record_id: 'v4-record-2',
              label_name: 'li2s_barrier_eV',
              label_value: 0.72,
              label_unit: 'eV',
              adsorbate: 'Li2S',
              reaction_step: '',
              ml_ready: false,
              blockers: ['missing_reaction_step'],
            },
          ],
          wide_properties: {
            li2s_decomposition_barrier_ev: 0.72,
          },
          property_group_counts: {
            reaction_step_properties: 1,
          },
          blockers: ['missing_reaction_step', 'energy_kind_task_mismatch'],
          ml_ready: false,
        }]),
      ],
    };
  }
  return {
    schema_version: 'project_library_ml_export_v4',
    read_only: true,
    auto_verification_applied: false,
    status: 'not_ready',
    manifest: {
      schema_version: 'project_library_ml_export_v4',
      dataset_version: 'project-library-ml-export-v4.0',
      context_key: 'li_s_sac_dac',
      library_name: 'Active Library',
      task: task || 'adsorption_energy',
      ready_only: readyOnly,
      candidate_count: 2,
      candidate_sample_count: 2,
      returned_count: readyOnly ? 0 : 2,
      returned_sample_count: readyOnly ? 0 : 2,
      ml_ready_count: 0,
      sample_ml_ready_count: 0,
      blocked_count: readyOnly ? 0 : 2,
      sample_blocked_count: readyOnly ? 0 : 2,
      blocker_counts: readyOnly ? {
        missing_source_text: 2,
      } : {
        missing_source_text: 2,
        generated_active_site_instance_key: 1,
      },
      sample_blocker_counts: readyOnly ? {
        missing_source_text: 2,
      } : {
        missing_source_text: 2,
        generated_active_site_instance_key: 1,
      },
      database_write_authority: 'user_submit_only',
      ai_consensus_auto_adopt_allowed: false,
    },
    records: [],
    sample_records: readyOnly ? [] : [
      {
        sample_id: 'paper:p2|catalyst:c3|site:default',
        sample_unit: 'active_site_instance',
        catalyst_name: 'Co-NC',
        catalyst_type: 'SAC',
        metal_centers: ['Co'],
        active_site_instance_key: 'paper:p2|catalyst:c3|site:default',
        task_labels: [],
        wide_properties: {},
        property_group_counts: {},
        blockers: ['missing_source_text'],
        ml_ready: false,
      },
    ],
  };
}

function makeProjectLibraryQualityPayload() {
  return {
    schema_version: 'project_library_quality_v1',
    context_key: 'li_s_sac_dac',
    context_version: 'project_library_contexts_v1',
    context_display_name_zh: '锂硫双原子',
    library_name: 'Active Library',
    read_only: true,
    auto_verification_applied: false,
    counts: {
      paper_count: 2,
      parsed_count: 2,
      with_dft_count: 2,
      needs_fields_count: 1,
      srr_lis_task_candidate_count: 3,
      label_ready_count: 1,
      training_ready_count: 1,
      feature_candidate_blocked_paper_count: 1,
      catalyst_sample_count: 2,
      active_site_instance_count: 2,
      ambiguous_records_count: 0,
      manual_verification_required_count: 0,
    },
    blocker_counts: {},
    feature_candidate_blocker_counts: {},
    sample_quality: {
      sample_unit: 'active_site_instance',
      counts: {
        total_sample_count: 2,
        missing_li2s_adsorption_sample_count: 1,
        missing_li2s_barrier_sample_count: 1,
        missing_rds_sample_count: 2,
        missing_bader_or_charge_transfer_sample_count: 1,
        dac_missing_metal_metal_distance_sample_count: 1,
        unknown_metal_descriptor_sample_count: 1,
      },
      gap_examples: {
        missing_bader_or_charge_transfer_sample_count: [
          {
            paper_id: 'paper-1',
            title: 'Li-S Barrier Paper',
            catalyst_sample_id: 'c2',
            catalyst_name: 'Fe-NC',
            active_site_instance_key: 'paper:p1|catalyst:c2|site:unknown',
          },
        ],
      },
      notes: [],
    },
    tasks: [],
    needs_fields_papers: [],
  };
}

async function installMockApi(page) {
  let lastDatasetUrl = '';
  let lastV3ManifestUrl = '';
  let lastV3JsonUrl = '';
  let lastV3CsvUrl = '';
  let lastV4JsonUrl = '';
  let lastV4CsvUrl = '';
  let lastV4PreviewPayload = null;
  let lastV4SubmitPayload = null;
  await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
  await page.route(/\/api\/libraries$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([ACTIVE_LIBRARY, ALT_LIBRARY]),
  }));
  await page.route(/\/api\/dft\/ml-dataset-v3\/manifest.*/, route => {
    lastV3ManifestUrl = route.request().url();
    const task = new URL(lastV3ManifestUrl).searchParams.get('task') || 'adsorption_energy';
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(makeV3Manifest(task)),
    });
  });
  await page.route(/\/api\/dft\/ml-dataset-v3\.csv.*/, route => {
    lastV3CsvUrl = route.request().url();
    return route.fulfill({
      status: 200,
      contentType: 'text/csv',
      body: 'record_id,task\nv3-record-1,reaction_barrier\n',
    });
  });
  await page.route(/\/api\/dft\/ml-dataset-v3\?.*/, route => {
    lastV3JsonUrl = route.request().url();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ metadata: makeV3Manifest(new URL(lastV3JsonUrl).searchParams.get('task')) || {}, records: [] }),
    });
  });
  await page.route(/\/api\/dft\/project-library-ml-export-v4\.csv.*/, route => {
    lastV4CsvUrl = route.request().url();
    return route.fulfill({
      status: 200,
      contentType: 'text/csv',
      body: 'sample_id,task,active_site_instance_key,li2s_barrier_eV\nsite-1,li2s_barrier,site-1,0.65\n',
    });
  });
  await page.route(/\/api\/dft\/project-library-ml-export-v4\?.*/, route => {
    lastV4JsonUrl = route.request().url();
    const url = new URL(lastV4JsonUrl);
    const task = url.searchParams.get('task') || 'adsorption_energy';
    const readyOnly = url.searchParams.get('ready_only') !== 'false';
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(makeV4Payload(task, readyOnly)),
    });
  });
  await page.route(/\/api\/dft\/project-library-quality.*/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(makeProjectLibraryQualityPayload()),
  }));
  await page.route(/\/api\/dft\/project-library-v4\/user-submit\/preview$/, async route => {
    lastV4PreviewPayload = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'project_library_v4_user_submit_preview_v1',
        context_key: 'li_s_sac_dac',
        paper_id: lastV4PreviewPayload.paper_id,
        record_id: lastV4PreviewPayload.record_id || null,
        action: lastV4PreviewPayload.record_id ? 'update_existing_dft_result' : 'create_new_dft_result',
        can_submit: true,
        writes_to_database: false,
        database_write_authority: 'user_submit_only',
        visible_in_v4_export: true,
        ready_only_export_eligible: false,
        hard_blockers: [],
        ml_blockers: [],
        warnings: [],
        resolved_source_candidate_ids: [],
        persisted_field_targets: ['value', 'unit'],
        evidence_payload_fields: ['bader_charge_M1', 'charge_transfer_e'],
        normalized_submission: lastV4PreviewPayload,
      }),
    });
  });
  await page.route(/\/api\/dft\/project-library-v4\/user-submit$/, async route => {
    lastV4SubmitPayload = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'project_library_v4_user_submit_result_v1',
        context_key: 'li_s_sac_dac',
        paper_id: lastV4SubmitPayload.paper_id,
        record_id: lastV4SubmitPayload.record_id || 'new-record-1',
        action: lastV4SubmitPayload.record_id ? 'update_existing_dft_result' : 'create_new_dft_result',
        writes_to_database: true,
        database_write_authority: 'user_submit_only',
        visible_in_v4_export: true,
        ready_only_export_eligible: true,
        candidate_status: 'final_user_submitted',
        audit_log_id: 'audit-1',
        consumed_source_candidate_ids: [],
        persisted_field_targets: ['value', 'unit'],
        evidence_payload_fields: ['bader_charge_M1', 'charge_transfer_e'],
        export_record: { record_id: lastV4SubmitPayload.record_id || 'new-record-1' },
      }),
    });
  });
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
    getLastV3ManifestUrl: () => lastV3ManifestUrl,
    getLastV3JsonUrl: () => lastV3JsonUrl,
    getLastV3CsvUrl: () => lastV3CsvUrl,
    getLastV4JsonUrl: () => lastV4JsonUrl,
    getLastV4CsvUrl: () => lastV4CsvUrl,
    getLastV4PreviewPayload: () => lastV4PreviewPayload,
    getLastV4SubmitPayload: () => lastV4SubmitPayload,
  };
}

test.describe('DFT ML-ready dataset page', () => {
  test('renders v2 summary, descriptor column, and detail context', async ({ page }) => {
    await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await expect(page.locator('h1')).toContainText('DFT 机器学习数据集');
    await expect(page.locator('#schemaVersionBadge')).toContainText('dft_results_ml_v2');
    await expect(page.locator('#statTotalCandidates')).toContainText('6');
    await expect(page.locator('#statNumericReadyCount')).toContainText('3');
    await expect(page.locator('#resultsMeta')).toContainText('LM 辅助记录 1 条');
    await expect(page.locator('#recordsTableBody')).toContainText('反应能垒（reaction_barrier）');
    await expect(page.locator('#recordsTableBody')).toContainText('Li2S 分解能垒（li2s_decomposition_barrier）');
    await expect(page.locator('#recordsTableBody')).toContainText('迁移能垒（migration_barrier）');
    await expect(page.locator('#recordsTableBody')).toContainText('自由能变化（gibbs_free_energy_change）');
    await expect(page.locator('#recordsTableBody')).toContainText('-1.75 eV');
    await expect(page.locator('#recordsTableBody')).toContainText('精确页码（exact_page） / 安全通过（safe_verified）');

    await page.locator('button[data-record-id="record-barrier-ambiguous"]').click();
    await expect(page.locator('.detail-row')).toContainText('surface_facet');
    await expect(page.locator('.detail-row')).toContainText('(104)');
    await expect(page.locator('.detail-row')).toContainText('paper_level_dft_settings / dft_settings 仅供审计与兼容');
  });

  test('renders v3 SRR_LiS manifest, empty state, and task switching', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await expect(page.locator('#v3PanelTitle')).toContainText('SRR_LiS 任务级导出入口');
    await expect(page.locator('#v3SchemaValue')).toContainText('dft_results_ml_v3');
    await expect(page.locator('#v3TaskValue')).toContainText('吸附能任务');
    await expect(page.locator('#v3TaskValue')).toContainText('adsorption_energy');
    await expect(page.locator('#v3ReturnedCount')).toContainText('0');
    await expect(page.locator('#v3TabularReadyCount')).toContainText('0');
    await expect(page.locator('#v3ExcludedCounts')).toContainText('unknown_reaction_type');
    await expect(page.locator('#v3StatusPanel')).toContainText('当前没有可直接训练的 SRR_LiS 记录');
    await expect(page.locator('#v3StatusPanel')).toContainText('不是导出接口失败');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('task=adsorption_energy');

    await page.selectOption('#v3TaskSelect', 'reaction_barrier');
    await expect(page.locator('#v3TaskValue')).toContainText('反应能垒任务');
    await expect(page.locator('#v3TaskValue')).toContainText('reaction_barrier');
    await expect(page.locator('#v3ReturnedCount')).toContainText('1');
    await expect(page.locator('#v3TabularReadyCount')).toContainText('1');
    await expect(page.locator('#v3StatusPanel')).toContainText('v3 manifest 已加载');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('task=reaction_barrier');

    await page.selectOption('#v3TaskSelect', 'rds_gibbs_free_energy');
    await expect(page.locator('#v3TaskValue')).toContainText('RDS 自由能 / 决速步骤自由能任务');
    await expect(page.locator('#v3TaskValue')).toContainText('rds_gibbs_free_energy');
    await expect(page.locator('#v3ExcludedCounts')).toContainText('missing_rds_semantics');
    await expect(page.locator('#v3TaskValue')).not.toContainText('反应能垒');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('task=rds_gibbs_free_energy');
  });

  test('renders v4 project-library manifest and blocked diagnostics scope', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await expect(page.locator('#v4PanelTitle')).toContainText('催化剂实例级 v4 导出入口');
    await expect(page.locator('#v4SchemaValue')).toContainText('project_library_ml_export_v4');
    await expect(page.locator('#v4TaskValue')).toContainText('吸附能任务');
    await expect(page.locator('#v4CandidateCount')).toContainText('2');
    await expect(page.locator('#v4ReadyCount')).toContainText('0');
    await expect(page.locator('#v4StatusPanel')).toContainText('当前 v4 任务没有 ML-ready 样本');
    await expect(page.locator('#v4SampleQualityCounts')).toContainText('missing_li2s_adsorption_sample_count');
    await expect(page.locator('#v4SampleQualityCounts')).toContainText('dac_missing_metal_metal_distance_sample_count');
    await expect(page.locator('#v4SampleQualityExamples')).toContainText('paper:p1|catalyst:c2|site:unknown');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('context_key=li_s_sac_dac');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('task=adsorption_energy');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('ready_only=false');

    await page.selectOption('#v4TaskSelect', 'li2s_barrier');
    await expect(page.locator('#v4TaskValue')).toContainText('Li2S 能垒任务');
    await expect(page.locator('#v4ReadyCount')).toContainText('1');
    await expect(page.locator('#v4ReturnedCount')).toContainText('2');
    await expect(page.locator('#v4StatusPanel')).toContainText('v4 manifest 已加载');
    await expect(page.locator('#v4SampleRecordsTableBody')).toContainText('paper:p1|catalyst:c1|site:li2s');
    await expect(page.locator('#v4SampleRecordsTableBody')).toContainText('FeCo-NC');
    await expect(page.locator('#v4SampleRecordsTableBody')).toContainText('li2s_decomposition_barrier_ev');
    await expect(page.locator('#v4SampleRecordsTableBody')).toContainText('bader_charge_li2s_e');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('task=li2s_barrier');

    await expect(page.locator('#v4ReturnedCount')).toContainText('2');
    await expect(page.locator('#v4BlockedCount')).toContainText('1');
    await expect(page.locator('#v4BlockerCounts')).toContainText('missing_reaction_step');
    await expect(page.locator('#v4BlockerCounts')).toContainText('energy_kind_task_mismatch');
    await expect(page.locator('#v4SampleRecordsTableBody')).toContainText('Fe-NC');
    await expect(page.locator('#v4SampleRecordsTableBody')).toContainText('blocked');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('ready_only=false');
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
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('library_name=Active+Library');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('year_min=2024');
  });

  test('passes shared filters to v3 JSON and CSV downloads', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.addInitScript(() => {
      window.URL.createObjectURL = () => 'blob:mock-download';
      window.URL.revokeObjectURL = () => {};
      HTMLAnchorElement.prototype.click = function noopClick() {};
    });

    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);
    await page.selectOption('#v3TaskSelect', 'reaction_barrier');
    await page.fill('#yearMinFilter', '2021');
    await page.fill('#yearMaxFilter', '2025');
    await page.selectOption('#libraryFilter', 'Archive Library');
    await page.click('#applyServerFiltersButton');

    await page.click('#v3JsonButton');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('/api/dft/ml-dataset-v3?');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('task=reaction_barrier');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('ready_only=true');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('library_name=Archive+Library');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('year_min=2021');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('year_max=2025');

    await page.click('#v3CsvButton');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('/api/dft/ml-dataset-v3.csv?');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('task=reaction_barrier');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('library_name=Archive+Library');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('year_min=2021');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('year_max=2025');
  });

  test('passes library and ready-only scope to v4 JSON and CSV downloads', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.addInitScript(() => {
      window.URL.createObjectURL = () => 'blob:mock-download';
      window.URL.revokeObjectURL = () => {};
      HTMLAnchorElement.prototype.click = function noopClick() {};
    });

    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);
    await page.selectOption('#v4TaskSelect', 'li2s_barrier');
    await page.selectOption('#v4ReadyOnlySelect', 'false');
    await page.selectOption('#libraryFilter', 'Archive Library');

    await page.click('#v4JsonButton');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('/api/dft/project-library-ml-export-v4?');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('context_key=li_s_sac_dac');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('task=li2s_barrier');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('ready_only=false');
    await expect.poll(() => mockState.getLastV4JsonUrl()).toContain('library_name=Archive+Library');

    await page.selectOption('#v4ReadyOnlySelect', 'true');
    await page.click('#v4CsvButton');
    await expect.poll(() => mockState.getLastV4CsvUrl()).toContain('/api/dft/project-library-ml-export-v4.csv?');
    await expect.poll(() => mockState.getLastV4CsvUrl()).toContain('context_key=li_s_sac_dac');
    await expect.poll(() => mockState.getLastV4CsvUrl()).toContain('task=li2s_barrier');
    await expect.poll(() => mockState.getLastV4CsvUrl()).toContain('ready_only=true');
    await expect.poll(() => mockState.getLastV4CsvUrl()).toContain('unit=sample');
    await expect.poll(() => mockState.getLastV4CsvUrl()).toContain('library_name=Archive+Library');
  });

  test('previews and submits project-library v4 sample-level user fields', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await page.selectOption('#v4TaskSelect', 'li2s_barrier');
    await page.locator('.select-v4-sample-btn').first().click();
    await expect(page.locator('#v4SubmitSelectedSample')).toContainText('paper:p1|catalyst:c1|site:li2s');
    await expect(page.locator('#v4SubmitRecordId')).toHaveValue('v4-record-1');
    await expect(page.locator('#v4SubmitValue')).toHaveValue('0.65');

    await page.fill('#v4SubmitSourceText', 'Bader charge and charge transfer were reported after Li2S adsorption.');
    await page.fill('#v4SubmitBaderM1', '0.12');
    await page.fill('#v4SubmitBaderM2', '-0.08');
    await page.fill('#v4SubmitChargeTransfer', '-1.11');
    await page.fill('#v4SubmitChargeDirection', 'adsorbate_to_catalyst');
    await page.fill('#v4SubmitMetalDistance', '2.41');
    await page.fill('#v4SubmitCoordinationEnv', 'Fe-Co-N6');
    await page.fill('#v4SubmitSourcePage', '7');

    await page.click('#v4SubmitPreviewButton');
    await expect.poll(() => mockState.getLastV4PreviewPayload()).toMatchObject({
      schema_version: 'project_library_ml_export_v4',
      context_key: 'li_s_sac_dac',
      paper_id: 'p1',
      record_id: 'v4-record-1',
      database_write_authority: 'user_submit_only',
      ai_consensus_auto_adopt_allowed: false,
      active_site_instance_key: 'paper:p1|catalyst:c1|site:li2s',
      catalyst_sample_id: 'c1',
      bader_charge_M1: 0.12,
      bader_charge_M2: -0.08,
      charge_transfer_e: -1.11,
      charge_transfer_direction: 'adsorbate_to_catalyst',
      metal_metal_distance_A: 2.41,
      coordination_environment: 'Fe-Co-N6',
    });
    await expect(page.locator('#v4SubmitResult')).toContainText('project_library_v4_user_submit_preview_v1');

    await page.click('#v4SubmitButton');
    await expect.poll(() => mockState.getLastV4SubmitPayload()).toMatchObject({
      paper_id: 'p1',
      record_id: 'v4-record-1',
      source_location: { page: '7' },
      source_text: 'Bader charge and charge transfer were reported after Li2S adsorption.',
    });
    await expect(page.locator('#v4SubmitResult')).toContainText('project_library_v4_user_submit_result_v1');
    await expect(page.locator('#toastContainer')).toContainText('已提交样本级字段');
  });

  test('uses current DOM year and library filters for direct v3 refresh and downloads', async ({ page }) => {
    const mockState = await installMockApi(page);
    await page.addInitScript(() => {
      window.URL.createObjectURL = () => 'blob:mock-download';
      window.URL.revokeObjectURL = () => {};
      HTMLAnchorElement.prototype.click = function noopClick() {};
    });

    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);
    await page.selectOption('#v3TaskSelect', 'reaction_barrier');
    await page.fill('#yearMinFilter', '2020');
    await page.fill('#yearMaxFilter', '2024');
    await page.selectOption('#libraryFilter', 'Archive Library');

    await page.click('#v3RefreshButton');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('task=reaction_barrier');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('library_name=Archive+Library');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('year_min=2020');
    await expect.poll(() => mockState.getLastV3ManifestUrl()).toContain('year_max=2024');

    await page.fill('#yearMinFilter', '2022');
    await page.fill('#yearMaxFilter', '2026');
    await page.click('#v3JsonButton');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('task=reaction_barrier');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('ready_only=true');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('library_name=Archive+Library');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('year_min=2022');
    await expect.poll(() => mockState.getLastV3JsonUrl()).toContain('year_max=2026');

    await page.selectOption('#libraryFilter', 'Active Library');
    await page.click('#v3CsvButton');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('task=reaction_barrier');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('library_name=Active+Library');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('year_min=2022');
    await expect.poll(() => mockState.getLastV3CsvUrl()).toContain('year_max=2026');
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

  test('shows policy-disabled state instead of a generic load failure when exports are disabled', async ({ page }) => {
    await page.route('**/favicon.ico', route => route.fulfill({ status: 204, body: '' }));
    await page.route(/\/api\/libraries$/, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([ACTIVE_LIBRARY, ALT_LIBRARY]),
    }));
    await page.route(/\/api\/papers\/export\/dft-dataset.*/, route => route.fulfill({
      status: 403,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Exports are disabled by server policy' }),
    }));

    await page.goto(`${BASE_URL}/pages/dft_ml_dataset/index.html`);

    await expect(page.locator('#statusPanel')).toContainText('当前服务器策略已关闭导出接口');
    await expect(page.locator('#statusPanel')).toHaveClass(/policy/);
    await expect(page.locator('#resultsMeta')).toContainText('导出策略关闭中');
    await expect(page.locator('#refreshButton')).toBeDisabled();
    await expect(page.locator('#applyServerFiltersButton')).toBeDisabled();
    await expect(page.locator('#exportCsvButton')).toBeDisabled();
    await expect(page.locator('#exportJsonButton')).toBeDisabled();
    await expect(page.locator('#v3RefreshButton')).toBeDisabled();
    await expect(page.locator('#v3JsonButton')).toBeDisabled();
    await expect(page.locator('#v3CsvButton')).toBeDisabled();
    await expect(page.locator('#v4RefreshButton')).toBeDisabled();
    await expect(page.locator('#v4JsonButton')).toBeDisabled();
    await expect(page.locator('#v4CsvButton')).toBeDisabled();
    await expect(page.locator('#toastContainer')).not.toContainText('读取失败：Exports are disabled by server policy');
  });
});
