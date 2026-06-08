# Codex Acceptance Gate

ACCEPTANCE_GATE=PASS

- Created at: 2026-06-08T16:30:41.296168+00:00
- Library: `chain_realpaper_smoke_20260608`
- API base: `http://localhost:8000`
- Paper IDs: `2d977b15-7715-4a27-87e3-985dc77c4da1, d5d5c467-8a91-4f9a-9c93-4e4c84a30bab, e636ff33-55fc-436d-b4ec-1b4f064f4050`

## Runtime Guard

- Active database PostgreSQL: `True`
- Legacy SQLite ignored: `True`
- API runtime-debug status: `200`
- API storage root exists: `True`

## Live Artifact Parity

- Live diagnosis root causes: `{'no_live_mismatch_detected': 3}`
- All real-paper artifacts ready: `True`

### 2d977b15-7715-4a27-87e3-985dc77c4da1

- local_ready: `True`
- api_get_paper_ready: `True`
- api_get_codex_context_ready: `True`
- api_review_center_ready: `True`
- artifact_ready_for_external_audit: `True`
- blocking_errors: `[]`

### d5d5c467-8a91-4f9a-9c93-4e4c84a30bab

- local_ready: `True`
- api_get_paper_ready: `True`
- api_get_codex_context_ready: `True`
- api_review_center_ready: `True`
- artifact_ready_for_external_audit: `True`
- blocking_errors: `[]`

### e636ff33-55fc-436d-b4ec-1b4f064f4050

- local_ready: `True`
- api_get_paper_ready: `True`
- api_get_codex_context_ready: `True`
- api_review_center_ready: `True`
- artifact_ready_for_external_audit: `True`
- blocking_errors: `[]`

## External Audit Visibility

- All visible: `True`
- No Windows absolute paths exposed: `True`
- External AI not required to resolve storage paths: `True`

### 2d977b15-7715-4a27-87e3-985dc77c4da1

- MCP get_review_coverage visible: `True`
- Review-center visible: `True`
- PostgreSQL candidate visible: `True`
- Does not auto-write verified/safe_verified: `True`

### d5d5c467-8a91-4f9a-9c93-4e4c84a30bab

- MCP get_review_coverage visible: `True`
- Review-center visible: `True`
- PostgreSQL candidate visible: `True`
- Does not auto-write verified/safe_verified: `True`

### e636ff33-55fc-436d-b4ec-1b4f064f4050

- MCP get_review_coverage visible: `True`
- Review-center visible: `True`
- PostgreSQL candidate visible: `True`
- Does not auto-write verified/safe_verified: `True`

## Smoke Report

- Path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\reports\realpaper_chain_smoke.json`
- Selected IDs match live IDs: `True`
- Selected IDs: `['2d977b15-7715-4a27-87e3-985dc77c4da1', 'd5d5c467-8a91-4f9a-9c93-4e4c84a30bab', 'e636ff33-55fc-436d-b4ec-1b4f064f4050']`
