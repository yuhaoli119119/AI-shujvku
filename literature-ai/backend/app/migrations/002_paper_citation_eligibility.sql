CREATE TABLE IF NOT EXISTS paper_citation_eligibility (
    paper_id UUID PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    included_for_writing BOOLEAN NOT NULL DEFAULT TRUE,
    exclude_from_citation BOOLEAN NOT NULL DEFAULT FALSE,
    exclude_reason TEXT,
    citation_priority VARCHAR(16) NOT NULL DEFAULT 'medium',
    user_note TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_paper_citation_eligibility_exclude
    ON paper_citation_eligibility (exclude_from_citation);

CREATE INDEX IF NOT EXISTS ix_paper_citation_eligibility_priority
    ON paper_citation_eligibility (citation_priority);

CREATE TABLE IF NOT EXISTS paper_impact_metadata (
    paper_id UUID PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    impact_factor DOUBLE PRECISION,
    impact_factor_source VARCHAR(64) NOT NULL DEFAULT 'unknown',
    impact_factor_year INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_paper_impact_metadata_impact_factor
    ON paper_impact_metadata (impact_factor);
