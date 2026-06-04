# PostgreSQL + pgvector + Shared Storage Deployment

This project now treats PostgreSQL on the B computer as the production source of truth.

## B Computer PostgreSQL

Assumed production values:

- B computer IP: `192.168.1.20`
- PostgreSQL port: `5432`
- Database: `literature_ai`
- App role: `litai_app`

Recommended Docker bootstrap on the B computer:

```powershell
docker run --name litai-postgres `
  -d `
  -p 5432:5432 `
  -e POSTGRES_DB=literature_ai `
  -e POSTGRES_USER=litai_admin `
  -e POSTGRES_PASSWORD="replace-with-strong-admin-password" `
  -v D:\LitAI\postgres-data:/var/lib/postgresql/data `
  pgvector/pgvector:pg16
```

Initialization SQL:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE ROLE litai_app LOGIN PASSWORD 'replace-with-strong-app-password';

GRANT CONNECT ON DATABASE literature_ai TO litai_app;
GRANT USAGE, CREATE ON SCHEMA public TO litai_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO litai_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO litai_app;
```

Production security:

- Allow port `5432` only from the A computer IP `192.168.1.10`.
- Use `scram-sha-256` password auth.
- Use `sslmode=prefer` for initial testing, then move to `sslmode=require` after TLS is configured.

## Shared Storage

B computer storage root:

```text
D:\LitAI\storage
├─ pdf
├─ markdown
├─ tei
├─ docling_json
├─ figures
└─ tables
```

Share `D:\LitAI\storage` as:

```text
\\B-PC\LitAIStorage$
```

Map it on the A computer as:

```text
Z:\LitAIStorage
```

Docker backend/worker mount:

```text
Z:\LitAIStorage -> /data/storage
```

Application setting:

```env
LITAI_STORAGE_ROOT=/data/storage
LITAI_HOST_STORAGE_ROOT=Z:/LitAIStorage
```

Production ingestion requires the configured storage root to already exist. If the share is disconnected, ingestion fails instead of creating a local fallback directory.

## Verification SQL

```sql
SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pgcrypto', 'pg_trgm');

SELECT COUNT(*) FROM papers;
SELECT COUNT(*) FROM paper_sections;
SELECT COUNT(*) FROM paper_chunks;

SELECT vector_dims(embedding) FROM paper_chunks WHERE embedding IS NOT NULL LIMIT 5;
```

Expected `vector_dims` value: `1536`.

## Test Data Cleanup Gate

Do not clean local test data until a new empty PostgreSQL database is online and 5-10 formal papers ingest successfully into B-computer storage.

Before cleanup:

```powershell
# Stop backend, worker, and compose services first.
# Confirm B-computer PostgreSQL backup exists.
# Confirm B-computer storage contains the formal files.
```

Resolve paths before deleting anything:

```powershell
Resolve-Path D:\Desktop\代码开发\AI-shujvku\data\libraries\
Resolve-Path D:\Desktop\代码开发\AI-shujvku\data\library_registry.json
Resolve-Path D:\Desktop\代码开发\AI-shujvku\literature-ai\data\libraries\
Resolve-Path D:\Desktop\代码开发\AI-shujvku\literature-ai\data\library_registry.json
Resolve-Path D:\Desktop\代码开发\AI-shujvku\test-artifacts\pdf-regression\
```

After cleanup verification:

```powershell
Get-ChildItem -Path . -Recurse -Filter database.sqlite
```

Runtime `database.sqlite` files should not appear outside tests.
