CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS papers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    library_name TEXT NOT NULL DEFAULT '默认文献库',
    doi TEXT UNIQUE,
    title TEXT,
    year INTEGER,
    journal TEXT,
    authors JSONB DEFAULT '[]'::jsonb,
    abstract TEXT,
    pdf_path TEXT NOT NULL,
    source_path TEXT,
    oa_status TEXT,
    license TEXT,
    tei_path TEXT,
    docling_json_path TEXT,
    markdown_path TEXT,
    comprehensive_analysis JSONB,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS paper_sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    section_title TEXT,
    section_type TEXT,
    text TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    embedding vector(64)
);

CREATE TABLE IF NOT EXISTS paper_tables (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    caption TEXT,
    markdown_content TEXT,
    page INTEGER,
    extraction_source TEXT
);

CREATE TABLE IF NOT EXISTS paper_figures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    caption TEXT,
    image_path TEXT,
    page INTEGER,
    figure_role TEXT
);

CREATE TABLE IF NOT EXISTS catalyst_samples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    name TEXT,
    catalyst_type TEXT,
    metal_centers JSONB DEFAULT '[]'::jsonb,
    coordination TEXT,
    support TEXT,
    synthesis_method TEXT,
    evidence_strength TEXT
);

CREATE TABLE IF NOT EXISTS dft_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    software TEXT,
    functional TEXT,
    dispersion_correction TEXT,
    pseudopotential TEXT,
    cutoff_energy_ev DOUBLE PRECISION,
    k_points TEXT,
    convergence_settings JSONB,
    vacuum_thickness_a DOUBLE PRECISION,
    raw_json JSONB
);

CREATE TABLE IF NOT EXISTS dft_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    catalyst_sample_id UUID REFERENCES catalyst_samples(id) ON DELETE SET NULL,
    adsorbate TEXT,
    property_type TEXT,
    value DOUBLE PRECISION,
    unit TEXT,
    reaction_step TEXT,
    source_section TEXT,
    source_figure TEXT,
    evidence_text TEXT,
    confidence DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS mechanism_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    catalyst_sample_id UUID REFERENCES catalyst_samples(id) ON DELETE SET NULL,
    claim_type TEXT,
    claim_text TEXT NOT NULL,
    evidence_types JSONB DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION,
    evidence_text TEXT
);

CREATE TABLE IF NOT EXISTS electrochemical_performance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    catalyst_sample_id UUID REFERENCES catalyst_samples(id) ON DELETE SET NULL,
    sulfur_loading_mg_cm2 DOUBLE PRECISION,
    sulfur_content_wt_percent DOUBLE PRECISION,
    electrolyte_sulfur_ratio TEXT,
    capacity_value DOUBLE PRECISION,
    cycle_number INTEGER,
    rate TEXT,
    decay_per_cycle DOUBLE PRECISION,
    evidence_text TEXT
);

CREATE TABLE IF NOT EXISTS writing_cards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    paper_type TEXT,
    research_gap TEXT,
    proposed_solution TEXT,
    core_hypothesis TEXT,
    evidence_chain JSONB,
    section_strategy JSONB,
    figure_logic TEXT,
    abstract_logic TEXT,
    introduction_logic TEXT,
    discussion_logic TEXT,
    embedding vector(64)
);

CREATE TABLE IF NOT EXISTS evidence_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    text TEXT NOT NULL,
    page INTEGER,
    section TEXT,
    figure TEXT,
    "table" TEXT,
    confidence DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS paper_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    field_name TEXT,
    page INTEGER,
    section_title TEXT,
    quoted_text TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS paper_corrections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    field_name TEXT NOT NULL,
    target_path TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'replace',
    proposed_value JSONB,
    reason TEXT NOT NULL,
    evidence_payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_at TIMESTAMP WITHOUT TIME ZONE,
    reviewed_by TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS parse_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identifier TEXT NOT NULL,
    providers JSONB DEFAULT '[]'::jsonb,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    paper_id UUID REFERENCES papers(id) ON DELETE SET NULL,
    error_message TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID REFERENCES papers(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    payload JSONB,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS paper_relationships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    target_paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    note TEXT,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS external_analysis_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_label TEXT,
    raw_text TEXT,
    raw_payload JSONB,
    normalized_payload JSONB,
    mapping_status TEXT NOT NULL DEFAULT 'pending',
    mapping_error TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS external_analysis_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES external_analysis_runs(id) ON DELETE CASCADE,
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL,
    normalized_payload JSONB,
    confidence DOUBLE PRECISION,
    mapping_reason TEXT,
    evidence_payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    materialized_target_type TEXT,
    materialized_target_id TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_sections_paper_id ON paper_sections(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_tables_paper_id ON paper_tables(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_figures_paper_id ON paper_figures(paper_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_samples_paper_id ON catalyst_samples(paper_id);
CREATE INDEX IF NOT EXISTS idx_dft_settings_paper_id ON dft_settings(paper_id);
CREATE INDEX IF NOT EXISTS idx_dft_results_paper_id ON dft_results(paper_id);
CREATE INDEX IF NOT EXISTS idx_mechanism_claims_paper_id ON mechanism_claims(paper_id);
CREATE INDEX IF NOT EXISTS idx_electrochemical_performance_paper_id ON electrochemical_performance(paper_id);
CREATE INDEX IF NOT EXISTS idx_writing_cards_paper_id ON writing_cards(paper_id);
CREATE INDEX IF NOT EXISTS idx_evidence_spans_paper_id ON evidence_spans(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_notes_paper_id ON paper_notes(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_corrections_paper_id ON paper_corrections(paper_id);
CREATE INDEX IF NOT EXISTS idx_parse_jobs_identifier ON parse_jobs(identifier);
CREATE INDEX IF NOT EXISTS idx_parse_jobs_paper_id ON parse_jobs(paper_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_paper_id ON audit_logs(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_relationships_source ON paper_relationships(source_paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_relationships_target ON paper_relationships(target_paper_id);
CREATE INDEX IF NOT EXISTS idx_external_analysis_runs_paper_id ON external_analysis_runs(paper_id);
CREATE INDEX IF NOT EXISTS idx_external_analysis_candidates_run_id ON external_analysis_candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_external_analysis_candidates_paper_id ON external_analysis_candidates(paper_id);
