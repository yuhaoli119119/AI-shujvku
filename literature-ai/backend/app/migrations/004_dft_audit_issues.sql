CREATE TABLE IF NOT EXISTS dft_audit_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    target_type VARCHAR(64) NOT NULL DEFAULT 'dft_results',
    target_id VARCHAR(64),
    issue_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'medium',
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    current_snapshot JSONB,
    suggested_value JSONB,
    suggested_dft JSONB,
    evidence_payload JSONB,
    source_identities JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    fingerprint VARCHAR(128) NOT NULL,
    resolution_note TEXT,
    resolved_by VARCHAR(128),
    resolved_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_dft_audit_issue_identity UNIQUE (
        paper_id,
        target_type,
        target_id,
        issue_type,
        fingerprint
    )
);

CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_paper_id ON dft_audit_issues (paper_id);
CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_status ON dft_audit_issues (status);
CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_issue_type ON dft_audit_issues (issue_type);
CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_target_id ON dft_audit_issues (target_id);
