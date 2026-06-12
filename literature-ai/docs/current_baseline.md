# Current Baseline

This file is the current architecture baseline for future IDE AI, MCP, and workbench changes. Historical audit and plan files under `docs/audits/` and `docs/plans/` are records of past gates; they do not override this baseline.

## AI Collaboration Model

- AI roles are assigned per task by the user.
- Codex, Gemini, GLM, Claude, or another IDE AI may parse papers, inspect images, audit DFT rows, check evidence, summarize knowledge, or perform second-pass review.
- A model name does not imply a fixed job, higher authority, or final verification right.
- All AI outputs are candidates unless they pass the required evidence, review, and confirmation gates.
- `source`, `source_label`, `agent_role`, and `model_name` should record what the AI did in that run, for example `glm_figure_audit`, `gemini_data_audit`, `codex_parse_review`, or `manual_second_pass`.

## Compatibility Names

Some public APIs, services, prompts, and workflow statuses still contain historical names:

- `get_codex_context`
- `codex-context`
- `extract`
- `batch-stage2`
- `GeminiAuditService`
- `gemini_audit_protocol`
- `Gemini_Verified`, `Gemini_Revised`, `Gemini_Flagged`

These are compatibility names and should not be interpreted as fixed ownership by Codex or Gemini, or as proof that the backend must run its own LLM deep parse. New docs and UI copy should describe the general role, such as "AI paper context", "prepare AI-readable materials", "external AI audit", "second AI review", or "assigned AI reviewer".

## Database

- PostgreSQL with pgvector is the only default active business database.
- SQLite is legacy/import/test infrastructure only.
- Runtime code must not auto-discover, repair, or switch to `database.sqlite`.
- `LITAI_DATABASE_URL` is the database source of truth.
- `LITAI_FORCE_CONFIGURED_DATABASE` defaults to `true`.

## Runtime Artifacts

- Parser artifacts live under `LITAI_STORAGE_ROOT`.
- Docker Compose uses `LITAI_STORAGE_ROOT=/data/storage` and mounts `./data:/data`.
- If running locally from `literature-ai/backend`, use `LITAI_STORAGE_ROOT=../data/storage`.
- A wrong storage root can make PostgreSQL paper rows visible while artifact checks report `missing_pdf`, `missing_markdown_and_docling_json`, or `missing_ai_reading_package`.

## DFT Evidence Locator Boundary

- DFT rows may have paper-level provenance, source sections, and evidence text while still lacking a precise PDF page.
- Missing DFT locator pages must remain visible as `text_only` / missing-page evidence. The UI may explain the limitation, but it must not display fake page links or imply that a PDF jump is available.
- Do not add a web UI button that claims to run AI page lookup unless the backend actually provides a reviewed, auditable AI workflow for that action. In the current workflow, the assigned IDE AI, not the web page itself, performs any ad hoc PDF page investigation requested by the user.
- Do not rebuild PDF page text, OCR the PDF, or reconstruct chunk-page mappings merely to repair DFT locator pages unless the user explicitly approves that broader parser work.
- Conservative locator repair may only write pages when existing parsed artifacts already provide a unique, exact evidence-text-to-page match. It must not write approximate guesses, mark reviews verified, approve DFT rows, or unlock CSV/ML export.
- When no safe page recovery exists, keep the DFT row reviewable through paper title, DOI, source section, evidence text, and review-center links; exact-page export gates should continue to block it.

## Schema

- `backend/app/db/models.py` is the ORM source.
- `backend/app/migrations/001_init.sql` is the PostgreSQL baseline generated from the current model set.
- Embedding columns use `LITAI_EMBEDDING_DIMENSION`, currently `1024`.
- Schema changes must update both models and the PostgreSQL baseline, and must keep `backend/tests/test_schema_baseline.py` passing.

## Settings And MCP Security

- GET endpoints must not return real API keys, MCP keys, admin tokens, or secrets.
- `/api/settings/ide-prompts` may return placeholders only.
- Settings writes from non-loopback clients require `LITAI_SETTINGS_ADMIN_TOKEN`.
- MCP keys remain bearer credentials and are never sample data.
- Non-admin IDE AI keys should normally have `read_papers,append_notes,propose_corrections,request_parse`.
- `review_corrections` should remain reserved for trusted admin or human-review keys.

## Writing Safety

- Writing Assistant is a draft/suggestion workflow.
- Citation insertion output is a suggestion unless backed by `safe_verified` evidence and reviewed by a human.
- Bibliography output is a draft reference preview for `safe_verified` sources only; unverified cards must not generate formal references.
- UI copy should distinguish draft exports, pending reference previews, and citation insertion suggestions from final scholarly output.

## Legacy Docs

Docs that mention SQLite as the active DB, fixed Codex/Gemini role ownership, no bibliography generation, or old D2/D3 acceptance constraints are historical unless explicitly reaffirmed by this baseline.
