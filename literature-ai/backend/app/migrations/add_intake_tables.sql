-- Literature Intake MVP: 两张候选/会话表
-- 作用：存储 AI 检索候选，不写入 papers 表，等待用户确认后才触发入库。
-- 适用：PostgreSQL (生产环境)

-- ============================================================
-- 1. 检索会话表
-- ============================================================
CREATE TABLE IF NOT EXISTS literature_intake_sessions (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    library_name   VARCHAR(255) NOT NULL DEFAULT '默认文献库',
    user_need      TEXT,
    original_query TEXT         NOT NULL,
    rewritten_query TEXT,
    providers      JSONB        NOT NULL DEFAULT '[]',
    target_types   JSONB,
    max_results    INTEGER      NOT NULL DEFAULT 20,
    status         VARCHAR(32)  NOT NULL DEFAULT 'searching',
    created_at     TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_lit_intake_sessions_library ON literature_intake_sessions (library_name);
CREATE INDEX IF NOT EXISTS ix_lit_intake_sessions_status  ON literature_intake_sessions (status);

-- ============================================================
-- 2. 候选文献表
-- ============================================================
CREATE TABLE IF NOT EXISTS literature_intake_candidates (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID         NOT NULL REFERENCES literature_intake_sessions(id) ON DELETE CASCADE,
    -- 元数据
    title               TEXT,
    doi                 VARCHAR(512),
    year                INTEGER,
    journal             VARCHAR(512),
    authors             JSONB        NOT NULL DEFAULT '[]',
    abstract            TEXT,
    identifier          VARCHAR(512),
    url                 TEXT,
    pdf_url             TEXT,
    providers           JSONB        NOT NULL DEFAULT '[]',
    -- AI 筛选
    relevance_score     FLOAT,
    screening_tier      VARCHAR(16),     -- recommended / maybe / weak
    screening_reason    TEXT,
    risk_flags          JSONB        NOT NULL DEFAULT '[]',
    -- 状态
    status              VARCHAR(32)  NOT NULL DEFAULT 'pending_review',
    reject_reason       TEXT,
    -- 关联
    duplicate_paper_id  UUID REFERENCES papers(id) ON DELETE SET NULL,
    ingest_job_id       VARCHAR(64),
    ingested_paper_id   UUID REFERENCES papers(id) ON DELETE SET NULL,
    created_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_session   ON literature_intake_candidates (session_id);
CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_doi       ON literature_intake_candidates (doi);
CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_status    ON literature_intake_candidates (status);
CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_tier      ON literature_intake_candidates (screening_tier);
CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_dup       ON literature_intake_candidates (duplicate_paper_id);
CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_job       ON literature_intake_candidates (ingest_job_id);
CREATE INDEX IF NOT EXISTS ix_lit_intake_cand_ingested  ON literature_intake_candidates (ingested_paper_id);
