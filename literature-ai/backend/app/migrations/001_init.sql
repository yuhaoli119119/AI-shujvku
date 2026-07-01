-- PostgreSQL + pgvector baseline schema generated from app.db.models.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;


CREATE TABLE literature_intake_sessions (
	id UUID NOT NULL, 
	library_name VARCHAR(255) DEFAULT '默认文献库' NOT NULL, 
	user_need TEXT, 
	original_query TEXT NOT NULL, 
	rewritten_query TEXT, 
	providers JSONB NOT NULL, 
	target_types JSONB, 
	max_results INTEGER NOT NULL, 
	status VARCHAR(32) DEFAULT 'searching' NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);


CREATE TABLE papers (
	id UUID NOT NULL, 
	library_name VARCHAR(255) DEFAULT '默认文献库' NOT NULL, 
	doi VARCHAR(512), 
	title TEXT, 
	year INTEGER, 
	journal VARCHAR(512), 
	authors JSONB NOT NULL, 
	abstract TEXT, 
	pdf_path TEXT NOT NULL, 
	source_path TEXT, 
	oa_status VARCHAR(64), 
	license VARCHAR(128), 
	tei_path TEXT, 
	docling_json_path TEXT, 
	markdown_path TEXT, 
	serial_number INTEGER, 
	paper_code VARCHAR(16),
	comprehensive_analysis JSONB, 
	paper_type VARCHAR(20), 
	type_confidence FLOAT, 
	classification_source VARCHAR(20), 
	workflow_status VARCHAR(64) DEFAULT 'Imported' NOT NULL, 
	pdf_quality_status VARCHAR(32), 
	pdf_quality_score FLOAT, 
	pdf_quality_report JSONB, 
	workspace_path TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_papers_library_doi UNIQUE (library_name, doi)
);


CREATE TABLE share_tokens (
	id UUID NOT NULL, 
	token VARCHAR(64) NOT NULL, 
	scope TEXT DEFAULT 'all' NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE, 
	created_by VARCHAR(64), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);


CREATE TABLE workflow_jobs (
	job_id VARCHAR(64) NOT NULL, 
	type VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	progress JSONB, 
	result JSONB, 
	error TEXT, 
	library_name VARCHAR(255) DEFAULT '默认文献库' NOT NULL, 
	payload JSONB, 
	runtime_context JSONB, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (job_id)
);


CREATE TABLE verification_session_paper_claims (
	id UUID NOT NULL,
	session_id VARCHAR(64) NOT NULL,
	paper_id UUID NOT NULL,
	status VARCHAR(32) DEFAULT 'active' NOT NULL,
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	released_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES workflow_jobs (job_id) ON DELETE CASCADE,
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE audit_logs (
	id UUID NOT NULL, 
	paper_id UUID, 
	action VARCHAR(64) NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	target_type VARCHAR(64), 
	target_id VARCHAR(64), 
	payload JSONB, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE SET NULL
);


CREATE TABLE module_write_locks (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	module_name VARCHAR(64) NOT NULL, 
	locked_by VARCHAR(128) NOT NULL, 
	lock_token VARCHAR(64) NOT NULL, 
	status VARCHAR(32) DEFAULT 'active' NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	released_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	metadata JSONB, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE catalyst_samples (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	name VARCHAR(255), 
	catalyst_type VARCHAR(64), 
	metal_centers JSONB NOT NULL, 
	coordination VARCHAR(255), 
	support VARCHAR(255), 
	synthesis_method TEXT, 
	evidence_strength TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE dft_settings (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	software VARCHAR(255), 
	functional VARCHAR(255), 
	dispersion_correction VARCHAR(255), 
	pseudopotential VARCHAR(255), 
	cutoff_energy_ev FLOAT, 
	k_points VARCHAR(128), 
	convergence_settings JSONB, 
	vacuum_thickness_a FLOAT, 
	raw_json JSONB, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE evidence_spans (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	object_type VARCHAR(64) NOT NULL, 
	object_id VARCHAR(64) NOT NULL, 
	text TEXT NOT NULL, 
	page INTEGER, 
	section VARCHAR(255), 
	figure TEXT, 
	"table" TEXT, 
	confidence FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE external_analysis_runs (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	source_label VARCHAR(128), 
	source_identity VARCHAR(160) DEFAULT 'untrusted:external_analysis',
	source_identity_verified BOOLEAN DEFAULT FALSE NOT NULL,
	raw_text TEXT, 
	raw_payload JSONB, 
	normalized_payload JSONB, 
	mapping_status VARCHAR(32) NOT NULL, 
	mapping_error TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE extraction_field_reviews (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	target_type VARCHAR(64) NOT NULL, 
	target_id VARCHAR(64) NOT NULL, 
	target_fingerprint VARCHAR(128), 
	target_label VARCHAR(255), 
	field_path VARCHAR(255), 
	target_resolution_status VARCHAR(32) NOT NULL, 
	remapped_from_target_id VARCHAR(64), 
	last_resolved_target_id VARCHAR(64), 
	field_name VARCHAR(128) NOT NULL, 
	original_value JSONB, 
	reviewed_value JSONB, 
	unit VARCHAR(64), 
	evidence_text TEXT, 
	reviewer_status VARCHAR(32) NOT NULL, 
	reviewer VARCHAR(128), 
	reviewer_note TEXT, 
	review_payload JSONB, 
	write_version INTEGER DEFAULT 1 NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_extraction_field_review UNIQUE (paper_id, target_type, target_id, field_name), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE literature_intake_candidates (
	id UUID NOT NULL, 
	session_id UUID NOT NULL, 
	title TEXT, 
	doi VARCHAR(512), 
	year INTEGER, 
	journal VARCHAR(512), 
	authors JSONB NOT NULL, 
	abstract TEXT, 
	identifier VARCHAR(512), 
	url TEXT, 
	pdf_url TEXT, 
	providers JSONB NOT NULL, 
	relevance_score FLOAT, 
	screening_tier VARCHAR(16), 
	screening_reason TEXT, 
	risk_flags JSONB NOT NULL, 
	status VARCHAR(32) DEFAULT 'pending_review' NOT NULL, 
	reject_reason TEXT, 
	duplicate_paper_id UUID, 
	ingest_job_id VARCHAR(64), 
	ingested_paper_id UUID, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES literature_intake_sessions (id) ON DELETE CASCADE, 
	FOREIGN KEY(duplicate_paper_id) REFERENCES papers (id) ON DELETE SET NULL, 
	FOREIGN KEY(ingested_paper_id) REFERENCES papers (id) ON DELETE SET NULL
);


CREATE TABLE paper_citation_eligibility (
	paper_id UUID NOT NULL, 
	included_for_writing BOOLEAN NOT NULL, 
	exclude_from_citation BOOLEAN NOT NULL, 
	exclude_reason TEXT, 
	citation_priority VARCHAR(16) NOT NULL, 
	user_note TEXT, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (paper_id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_corrections (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	field_name VARCHAR(128) NOT NULL, 
	target_path VARCHAR(255) NOT NULL, 
	operation VARCHAR(32) NOT NULL, 
	proposed_value JSONB, 
	reason TEXT NOT NULL, 
	evidence_payload JSONB, 
	status VARCHAR(32) NOT NULL, 
	reviewed_at TIMESTAMP WITHOUT TIME ZONE, 
	reviewed_by VARCHAR(64), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_figures (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	caption TEXT, 
	image_path TEXT, 
	page INTEGER, 
	figure_role VARCHAR(128), 
	role_confidence FLOAT, 
	content_summary TEXT, 
	key_elements JSONB, 
	prov JSONB, 
	figure_label VARCHAR(64), 
	crop_status VARCHAR(32) DEFAULT 'candidate_crop' NOT NULL, 
	crop_confidence FLOAT, 
	crop_source VARCHAR(64), 
	write_version INTEGER DEFAULT 1 NOT NULL,
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_impact_metadata (
	paper_id UUID NOT NULL, 
	impact_factor FLOAT, 
	impact_factor_source VARCHAR(64) NOT NULL, 
	impact_factor_year INTEGER, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (paper_id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_notes (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	content TEXT NOT NULL, 
	field_name VARCHAR(128), 
	page INTEGER, 
	section_title TEXT, 
	quoted_text TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_relationships (
	id UUID NOT NULL, 
	source_paper_id UUID NOT NULL, 
	target_paper_id UUID NOT NULL, 
	relationship_type VARCHAR(64) NOT NULL, 
	note TEXT, 
	created_by VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(target_paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_sections (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	section_title TEXT, 
	section_type VARCHAR(64), 
	text TEXT NOT NULL, 
	page_start INTEGER, 
	page_end INTEGER, 
	embedding vector(1024), 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE paper_tables (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	caption TEXT, 
	markdown_content TEXT, 
	page INTEGER, 
	extraction_source VARCHAR(64), 
	prov JSONB, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE parse_jobs (
	id UUID NOT NULL, 
	identifier VARCHAR(512) NOT NULL, 
	providers JSONB NOT NULL, 
	requested_by VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	paper_id UUID, 
	error_message TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE SET NULL
);


CREATE TABLE reference_entries (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	title TEXT NOT NULL, 
	authors TEXT, 
	journal VARCHAR(512), 
	year INTEGER, 
	doi VARCHAR(512), 
	volume VARCHAR(64), 
	pages VARCHAR(64), 
	reference_number INTEGER, 
	citation_context TEXT, 
	linked_paper_id UUID, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(linked_paper_id) REFERENCES papers (id) ON DELETE SET NULL
);


CREATE TABLE writing_cards (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	paper_type VARCHAR(128), 
	research_gap TEXT, 
	proposed_solution TEXT, 
	core_hypothesis TEXT, 
	evidence_chain JSONB, 
	section_strategy JSONB, 
	figure_logic TEXT, 
	abstract_logic TEXT, 
	introduction_logic TEXT, 
	discussion_logic TEXT, 
	embedding vector(1024), 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE dft_results (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	catalyst_sample_id UUID, 
	adsorbate VARCHAR(64), 
	property_type VARCHAR(128), 
	value FLOAT, 
	unit VARCHAR(64), 
	reaction_step TEXT, 
	reaction_type VARCHAR(32),
	reaction_type_source VARCHAR(32),
	reaction_type_confidence FLOAT,
	reaction_profile_version VARCHAR(64),
	reaction_validation_status VARCHAR(32),
	source_section VARCHAR(255), 
	source_figure VARCHAR(255), 
	evidence_text TEXT, 
	confidence FLOAT, 
	candidate_status VARCHAR(64) DEFAULT 'system_candidate' NOT NULL, 
	evidence_payload JSONB, 
	extraction_protocol_version VARCHAR(64), 
	candidate_identity VARCHAR(64),
	support_lifecycle_status VARCHAR(32),
	support_writeback_paper_id UUID,
	support_writeback_dft_result_id UUID,
	support_lifecycle_reason TEXT,
	support_lifecycle_actor VARCHAR(160),
	support_lifecycle_updated_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id), 
	CONSTRAINT uq_dft_result_candidate_identity UNIQUE (paper_id, candidate_identity),
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(catalyst_sample_id) REFERENCES catalyst_samples (id) ON DELETE SET NULL,
	FOREIGN KEY(support_writeback_paper_id) REFERENCES papers (id) ON DELETE SET NULL,
	FOREIGN KEY(support_writeback_dft_result_id) REFERENCES dft_results (id) ON DELETE SET NULL
);


CREATE TABLE electrochemical_performance (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	catalyst_sample_id UUID, 
	sulfur_loading_mg_cm2 FLOAT, 
	sulfur_content_wt_percent FLOAT, 
	electrolyte_sulfur_ratio VARCHAR(64), 
	capacity_value FLOAT, 
	cycle_number INTEGER, 
	rate VARCHAR(64), 
	decay_per_cycle FLOAT, 
	evidence_text TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(catalyst_sample_id) REFERENCES catalyst_samples (id) ON DELETE SET NULL
);


CREATE TABLE evidence_claims (
	id UUID NOT NULL, 
	claim_text TEXT NOT NULL, 
	source_type VARCHAR(64) NOT NULL, 
	target_type VARCHAR(64), 
	target_id VARCHAR(64), 
	paper_id UUID, 
	chunk_id VARCHAR(64), 
	section_id UUID, 
	page_start INTEGER, 
	page_end INTEGER, 
	span_start INTEGER, 
	span_end INTEGER, 
	evidence_text TEXT NOT NULL, 
	confidence FLOAT, 
	validation_status VARCHAR(32) NOT NULL, 
	metadata JSONB, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(section_id) REFERENCES paper_sections (id) ON DELETE SET NULL
);


CREATE TABLE external_analysis_candidates (
	id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	candidate_type VARCHAR(32) NOT NULL, 
	normalized_payload JSONB, 
	confidence FLOAT, 
	mapping_reason TEXT, 
	evidence_payload JSONB, 
	status VARCHAR(32) NOT NULL, 
	materialized_target_type VARCHAR(64), 
	materialized_target_id VARCHAR(64), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES external_analysis_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE figure_data_points (
	id UUID NOT NULL, 
	figure_id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	metric_name VARCHAR(255) NOT NULL, 
	metric_value FLOAT, 
	unit VARCHAR(64), 
	conditions JSONB, 
	sample_label VARCHAR(128), 
	confidence FLOAT NOT NULL, 
	raw_text TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(figure_id) REFERENCES paper_figures (id) ON DELETE CASCADE, 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE
);


CREATE TABLE mechanism_claims (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	catalyst_sample_id UUID, 
	claim_type VARCHAR(128), 
	claim_text TEXT NOT NULL, 
	evidence_types JSONB NOT NULL, 
	confidence FLOAT, 
	evidence_text TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(catalyst_sample_id) REFERENCES catalyst_samples (id) ON DELETE SET NULL
);


CREATE TABLE paper_chunks (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	section_id UUID, 
	chunk_index INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	page_start INTEGER, 
	page_end INTEGER, 
	token_count INTEGER, 
	embedding vector(1024), 
	embedding_model VARCHAR(128), 
	embedding_dimension INTEGER, 
	content_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_paper_chunk_section_index UNIQUE (paper_id, section_id, chunk_index), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(section_id) REFERENCES paper_sections (id) ON DELETE CASCADE
);


CREATE TABLE evidence_locators (
	id UUID NOT NULL, 
	paper_id UUID NOT NULL, 
	claim_id UUID, 
	chunk_id VARCHAR(64), 
	source_type VARCHAR(32) NOT NULL, 
	page INTEGER, 
	bbox JSONB, 
	section VARCHAR(255), 
	figure_id UUID, 
	table_id UUID, 
	equation_id VARCHAR(128), 
	target_type VARCHAR(64), 
	target_id VARCHAR(64), 
	field_name VARCHAR(128), 
	evidence_text TEXT NOT NULL, 
	char_start INTEGER, 
	char_end INTEGER, 
	locator_status VARCHAR(32) NOT NULL, 
	locator_confidence FLOAT NOT NULL, 
	parser_source VARCHAR(32) NOT NULL, 
	warning_reason TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(paper_id) REFERENCES papers (id) ON DELETE CASCADE, 
	FOREIGN KEY(claim_id) REFERENCES evidence_claims (id) ON DELETE CASCADE, 
	FOREIGN KEY(figure_id) REFERENCES paper_figures (id) ON DELETE SET NULL, 
	FOREIGN KEY(table_id) REFERENCES paper_tables (id) ON DELETE SET NULL
);

CREATE INDEX ix_literature_intake_sessions_status ON literature_intake_sessions (status);
CREATE INDEX ix_literature_intake_sessions_library_name ON literature_intake_sessions (library_name);

CREATE INDEX ix_papers_workflow_status ON papers (workflow_status);
CREATE INDEX ix_papers_library_name ON papers (library_name);
CREATE INDEX ix_papers_type_confidence ON papers (type_confidence);
CREATE INDEX ix_papers_paper_type ON papers (paper_type);
CREATE INDEX ix_papers_pdf_quality_status ON papers (pdf_quality_status);
CREATE INDEX ix_papers_serial_number ON papers (serial_number);
CREATE UNIQUE INDEX uq_papers_paper_code ON papers (paper_code) WHERE paper_code IS NOT NULL AND paper_code <> '';

CREATE UNIQUE INDEX ix_share_tokens_token ON share_tokens (token);

CREATE INDEX ix_workflow_jobs_type ON workflow_jobs (type);
CREATE INDEX ix_workflow_jobs_status ON workflow_jobs (status);
CREATE INDEX ix_workflow_jobs_library_name ON workflow_jobs (library_name);
CREATE INDEX ix_verification_session_paper_claims_session_id ON verification_session_paper_claims (session_id);
CREATE INDEX ix_verification_session_paper_claims_paper_id ON verification_session_paper_claims (paper_id);
CREATE INDEX ix_verification_session_paper_claims_status ON verification_session_paper_claims (status);
CREATE INDEX ix_verification_session_paper_claims_expires_at ON verification_session_paper_claims (expires_at);
CREATE UNIQUE INDEX uq_verification_session_active_paper ON verification_session_paper_claims (paper_id) WHERE status = 'active';

CREATE INDEX ix_audit_logs_paper_id ON audit_logs (paper_id);

CREATE INDEX ix_module_write_locks_paper_id ON module_write_locks (paper_id);
CREATE INDEX ix_module_write_locks_module_name ON module_write_locks (module_name);
CREATE INDEX ix_module_write_locks_locked_by ON module_write_locks (locked_by);
CREATE UNIQUE INDEX ix_module_write_locks_lock_token ON module_write_locks (lock_token);
CREATE INDEX ix_module_write_locks_status ON module_write_locks (status);
CREATE INDEX ix_module_write_locks_expires_at ON module_write_locks (expires_at);
CREATE UNIQUE INDEX uq_module_write_locks_active_scope ON module_write_locks (paper_id, module_name) WHERE status = 'active';

CREATE INDEX ix_catalyst_samples_paper_id ON catalyst_samples (paper_id);

CREATE INDEX ix_dft_settings_paper_id ON dft_settings (paper_id);

CREATE INDEX ix_evidence_spans_paper_id ON evidence_spans (paper_id);

CREATE INDEX ix_external_analysis_runs_paper_id ON external_analysis_runs (paper_id);

CREATE INDEX ix_extraction_field_reviews_target_fingerprint ON extraction_field_reviews (target_fingerprint);
CREATE INDEX ix_extraction_field_reviews_paper_id ON extraction_field_reviews (paper_id);
CREATE INDEX ix_extraction_field_reviews_field_name ON extraction_field_reviews (field_name);
CREATE INDEX ix_extraction_field_reviews_target_id ON extraction_field_reviews (target_id);
CREATE INDEX ix_extraction_field_reviews_target_resolution_status ON extraction_field_reviews (target_resolution_status);
CREATE INDEX ix_extraction_field_reviews_target_type ON extraction_field_reviews (target_type);
CREATE INDEX ix_extraction_field_reviews_reviewer_status ON extraction_field_reviews (reviewer_status);

CREATE INDEX ix_literature_intake_candidates_screening_tier ON literature_intake_candidates (screening_tier);
CREATE INDEX ix_literature_intake_candidates_session_id ON literature_intake_candidates (session_id);
CREATE INDEX ix_literature_intake_candidates_ingest_job_id ON literature_intake_candidates (ingest_job_id);
CREATE INDEX ix_literature_intake_candidates_duplicate_paper_id ON literature_intake_candidates (duplicate_paper_id);
CREATE INDEX ix_literature_intake_candidates_identifier ON literature_intake_candidates (identifier);
CREATE INDEX ix_literature_intake_candidates_doi ON literature_intake_candidates (doi);
CREATE INDEX ix_literature_intake_candidates_status ON literature_intake_candidates (status);
CREATE INDEX ix_literature_intake_candidates_ingested_paper_id ON literature_intake_candidates (ingested_paper_id);

CREATE INDEX ix_paper_citation_eligibility_exclude_from_citation ON paper_citation_eligibility (exclude_from_citation);
CREATE INDEX ix_paper_citation_eligibility_citation_priority ON paper_citation_eligibility (citation_priority);

CREATE INDEX ix_paper_corrections_paper_id ON paper_corrections (paper_id);

CREATE INDEX ix_paper_figures_figure_label ON paper_figures (figure_label);
CREATE INDEX ix_paper_figures_crop_status ON paper_figures (crop_status);
CREATE INDEX ix_paper_figures_paper_id ON paper_figures (paper_id);

CREATE INDEX ix_paper_impact_metadata_impact_factor ON paper_impact_metadata (impact_factor);

CREATE INDEX ix_paper_notes_paper_id ON paper_notes (paper_id);

CREATE INDEX ix_paper_relationships_target_paper_id ON paper_relationships (target_paper_id);
CREATE INDEX ix_paper_relationships_source_paper_id ON paper_relationships (source_paper_id);

CREATE INDEX ix_paper_sections_paper_id ON paper_sections (paper_id);

CREATE INDEX ix_paper_tables_paper_id ON paper_tables (paper_id);

CREATE INDEX ix_parse_jobs_paper_id ON parse_jobs (paper_id);
CREATE INDEX ix_parse_jobs_identifier ON parse_jobs (identifier);

CREATE INDEX ix_reference_entries_paper_id ON reference_entries (paper_id);
CREATE INDEX ix_reference_entries_linked_paper_id ON reference_entries (linked_paper_id);

CREATE INDEX ix_writing_cards_paper_id ON writing_cards (paper_id);

CREATE INDEX ix_dft_results_paper_id ON dft_results (paper_id);
CREATE INDEX ix_dft_results_candidate_status ON dft_results (candidate_status);
CREATE INDEX ix_dft_results_reaction_type ON dft_results (reaction_type);

CREATE INDEX ix_electrochemical_performance_paper_id ON electrochemical_performance (paper_id);

CREATE INDEX ix_evidence_claims_target_type ON evidence_claims (target_type);
CREATE INDEX ix_evidence_claims_source_type ON evidence_claims (source_type);
CREATE INDEX ix_evidence_claims_chunk_id ON evidence_claims (chunk_id);
CREATE INDEX ix_evidence_claims_paper_id ON evidence_claims (paper_id);
CREATE INDEX ix_evidence_claims_target_id ON evidence_claims (target_id);
CREATE INDEX ix_evidence_claims_validation_status ON evidence_claims (validation_status);
CREATE INDEX ix_evidence_claims_section_id ON evidence_claims (section_id);

CREATE INDEX ix_external_analysis_candidates_run_id ON external_analysis_candidates (run_id);
CREATE INDEX ix_external_analysis_candidates_paper_id ON external_analysis_candidates (paper_id);

CREATE INDEX ix_figure_data_points_paper_id ON figure_data_points (paper_id);
CREATE INDEX ix_figure_data_points_figure_id ON figure_data_points (figure_id);

CREATE INDEX ix_mechanism_claims_paper_id ON mechanism_claims (paper_id);

CREATE INDEX ix_paper_chunks_content_hash ON paper_chunks (content_hash);
CREATE INDEX ix_paper_chunks_embedding_model ON paper_chunks (embedding_model);
CREATE INDEX ix_paper_chunks_section_id ON paper_chunks (section_id);
CREATE INDEX ix_paper_chunks_paper_id ON paper_chunks (paper_id);
CREATE INDEX ix_paper_chunks_embedding_dimension ON paper_chunks (embedding_dimension);

CREATE INDEX ix_evidence_locators_paper_id ON evidence_locators (paper_id);
CREATE INDEX ix_evidence_locators_parser_source ON evidence_locators (parser_source);
CREATE INDEX ix_evidence_locators_source_type ON evidence_locators (source_type);
CREATE INDEX ix_evidence_locators_target_id ON evidence_locators (target_id);
CREATE INDEX ix_evidence_locators_field_name ON evidence_locators (field_name);
CREATE INDEX ix_evidence_locators_table_id ON evidence_locators (table_id);
CREATE INDEX ix_evidence_locators_figure_id ON evidence_locators (figure_id);
CREATE INDEX ix_evidence_locators_equation_id ON evidence_locators (equation_id);
CREATE INDEX ix_evidence_locators_locator_status ON evidence_locators (locator_status);
CREATE INDEX ix_evidence_locators_target_type ON evidence_locators (target_type);
CREATE INDEX ix_evidence_locators_claim_id ON evidence_locators (claim_id);
CREATE INDEX ix_evidence_locators_page ON evidence_locators (page);
CREATE INDEX ix_evidence_locators_chunk_id ON evidence_locators (chunk_id);

