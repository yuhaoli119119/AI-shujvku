-- Explicit migration: apply only to a reviewed, backed-up database.
-- This migration never rewrites or deletes pre-existing scientific results.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '30s';
CREATE TABLE IF NOT EXISTS dataset_sources (
    id uuid PRIMARY KEY,
    paper_id uuid NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    dataset_key varchar(255) NOT NULL,
    version varchar(255) NOT NULL,
    title text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (paper_id, dataset_key, version),
    UNIQUE (id, paper_id)
);
CREATE TABLE IF NOT EXISTS dataset_files (
    id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL,
    paper_id uuid NOT NULL,
    filename text NOT NULL,
    mime_type text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256 varchar(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    object_key text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (dataset_id, paper_id) REFERENCES dataset_sources (id, paper_id) ON DELETE RESTRICT,
    UNIQUE (dataset_id, sha256),
    UNIQUE (id, paper_id)
);
-- The original source table and its JSON fingerprints remain unchanged.
-- This index permits same-paper composite foreign keys without rewriting rows.
CREATE INDEX IF NOT EXISTS ix_dataset_files_paper ON dataset_files (paper_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rebuild_source_id_paper ON rebuild_value_sources (id, paper_id);
CREATE TABLE IF NOT EXISTS dataset_value_sources (
    source_id uuid PRIMARY KEY,
    paper_id uuid NOT NULL,
    dataset_file_id uuid NOT NULL,
    dataset_locator jsonb NOT NULL,
    FOREIGN KEY (source_id, paper_id) REFERENCES rebuild_value_sources (id, paper_id) ON DELETE RESTRICT,
    FOREIGN KEY (dataset_file_id, paper_id) REFERENCES dataset_files (id, paper_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_dataset_value_sources_paper ON dataset_value_sources (paper_id, source_id);
COMMIT;
