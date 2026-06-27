-- Optional physical tables for Li-S SAC/DAC project-library v4.
-- The current v4 export path remains service-layer/read-only over DFTResult.
-- Apply this migration only after the user-submit/backfill workflow is accepted.

CREATE TABLE IF NOT EXISTS project_library_active_site_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    catalyst_sample_id UUID NOT NULL REFERENCES catalyst_samples(id) ON DELETE CASCADE,
    active_site_instance_key TEXT NOT NULL,
    active_site_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    dft_setting_id UUID REFERENCES dft_settings(id) ON DELETE SET NULL,
    structure_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    dft_setting_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    binding_source VARCHAR(64) NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (catalyst_sample_id, active_site_instance_key)
);

CREATE INDEX IF NOT EXISTS ix_project_library_active_site_instances_paper
    ON project_library_active_site_instances (paper_id);

CREATE INDEX IF NOT EXISTS ix_project_library_active_site_instances_key
    ON project_library_active_site_instances (active_site_instance_key);

CREATE TABLE IF NOT EXISTS project_library_adsorbate_properties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    active_site_instance_id UUID NOT NULL REFERENCES project_library_active_site_instances(id) ON DELETE CASCADE,
    dft_result_id UUID REFERENCES dft_results(id) ON DELETE SET NULL,
    catalyst_sample_id UUID NOT NULL REFERENCES catalyst_samples(id) ON DELETE CASCADE,
    adsorbate VARCHAR(128) NOT NULL,
    canonical_adsorbate VARCHAR(128),
    adsorption_energy_eV DOUBLE PRECISION,
    unit VARCHAR(32) DEFAULT 'eV',
    source_text TEXT,
    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_level VARCHAR(32),
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_project_library_adsorbate_properties_instance
    ON project_library_adsorbate_properties (active_site_instance_id);

CREATE INDEX IF NOT EXISTS ix_project_library_adsorbate_properties_adsorbate
    ON project_library_adsorbate_properties (canonical_adsorbate);

CREATE TABLE IF NOT EXISTS project_library_reaction_step_properties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    active_site_instance_id UUID NOT NULL REFERENCES project_library_active_site_instances(id) ON DELETE CASCADE,
    dft_result_id UUID REFERENCES dft_results(id) ON DELETE SET NULL,
    catalyst_sample_id UUID NOT NULL REFERENCES catalyst_samples(id) ON DELETE CASCADE,
    reaction_type VARCHAR(128),
    reaction_step TEXT,
    reaction_species VARCHAR(128),
    property_subtype VARCHAR(128),
    energy_kind VARCHAR(64),
    value_eV DOUBLE PRECISION,
    unit VARCHAR(32) DEFAULT 'eV',
    source_text TEXT,
    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_level VARCHAR(32),
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_project_library_reaction_step_properties_instance
    ON project_library_reaction_step_properties (active_site_instance_id);

CREATE INDEX IF NOT EXISTS ix_project_library_reaction_step_properties_subtype
    ON project_library_reaction_step_properties (property_subtype);

CREATE TABLE IF NOT EXISTS project_library_electronic_properties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    active_site_instance_id UUID NOT NULL REFERENCES project_library_active_site_instances(id) ON DELETE CASCADE,
    dft_result_id UUID REFERENCES dft_results(id) ON DELETE SET NULL,
    catalyst_sample_id UUID NOT NULL REFERENCES catalyst_samples(id) ON DELETE CASCADE,
    bader_charge_M1 DOUBLE PRECISION,
    bader_charge_M2 DOUBLE PRECISION,
    charge_transfer_e DOUBLE PRECISION,
    charge_transfer_direction VARCHAR(128),
    state_context TEXT,
    site_label VARCHAR(128),
    source_text TEXT,
    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_level VARCHAR(32),
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_project_library_electronic_properties_instance
    ON project_library_electronic_properties (active_site_instance_id);

CREATE TABLE IF NOT EXISTS project_library_structure_properties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    active_site_instance_id UUID NOT NULL REFERENCES project_library_active_site_instances(id) ON DELETE CASCADE,
    dft_result_id UUID REFERENCES dft_results(id) ON DELETE SET NULL,
    catalyst_sample_id UUID NOT NULL REFERENCES catalyst_samples(id) ON DELETE CASCADE,
    metal_metal_distance_A DOUBLE PRECISION,
    coordination_environment TEXT,
    adsorption_site TEXT,
    adsorption_mode TEXT,
    metal_ligand_distance_A DOUBLE PRECISION,
    source_text TEXT,
    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_level VARCHAR(32),
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_project_library_structure_properties_instance
    ON project_library_structure_properties (active_site_instance_id);

CREATE TABLE IF NOT EXISTS project_library_ambiguous_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    catalyst_sample_id UUID REFERENCES catalyst_samples(id) ON DELETE SET NULL,
    dft_result_id UUID REFERENCES dft_results(id) ON DELETE SET NULL,
    source_candidate_id UUID REFERENCES external_analysis_candidates(id) ON DELETE SET NULL,
    ambiguity_type VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_text TEXT,
    source_location JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolution_status VARCHAR(64) NOT NULL DEFAULT 'needs_user_decision',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_project_library_ambiguous_records_paper
    ON project_library_ambiguous_records (paper_id);

CREATE INDEX IF NOT EXISTS ix_project_library_ambiguous_records_status
    ON project_library_ambiguous_records (resolution_status);
