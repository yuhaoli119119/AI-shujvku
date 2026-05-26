# D4-0 Next Phase Readiness / Backlog Triage Gate

Date: 2026-05-27

Scope: readiness and backlog triage only. This pass did not implement a new feature, change business logic, change frontend UI behavior, change API safety contracts, modify tests, touch the active database, run migrations, write verified reviews, apply extraction results, or materialize real data.

## 1. Baseline / Sync Gate

Commands requested and executed before any edit:

- `git status --short`: clean
- `git log -1 --oneline`: `234a1b8 docs frontend playwright fallback repro`
- `git rev-parse HEAD`: `234a1b8af395a0a23f015b19559d97dfb8948844`
- `git branch -vv`: `master 234a1b8 [origin/master] docs frontend playwright fallback repro`
- `git fetch origin`: first attempt failed with a transient GitHub TLS `SSL_ERROR_SYSCALL`; retry succeeded.
- `git ls-remote origin refs/heads/master`: `234a1b8af395a0a23f015b19559d97dfb8948844 refs/heads/master`

Baseline conclusion after successful fetch retry:

- Starting HEAD: `234a1b8af395a0a23f015b19559d97dfb8948844`
- Local `origin/master`: `234a1b8af395a0a23f015b19559d97dfb8948844`
- Remote `refs/heads/master`: `234a1b8af395a0a23f015b19559d97dfb8948844`
- Worktree before this manifest: clean.
- HEAD, local `origin/master`, and remote `refs/heads/master` were identical before this docs-only edit.

## 2. Reviewed Scope

### 2.1 Docs / README / Audit Manifests

Reviewed:

- Root `README.md`
- `literature-ai/README.md`
- `literature-ai/AGENTS.md`
- `literature-ai/使用说明.md`
- `literature-ai/docs/README.md`
- D2 closure/readiness manifests in `literature-ai/docs/audits/`
- D3 closure manifests:
  - `D3-3_review_workflow_regression_lock.md`
  - `D3-4_frontend_contract_lock.md`
  - `D3-4.1_export_empty_blocked_only_lock.md`
  - `D3-5_d3_safety_line_final_closure.md`
  - `D3-5.1_frontend_playwright_fallback_repro.md`

Readiness notes:

- The top-level and project README documents still describe the project as being in `D2 migration readiness`. That is stale relative to the latest D2/D3 closure chain and should be refreshed in a later docs task, but this D4-0 pass intentionally did not modify the current effective README set.
- `AGENTS.md` still contains the important operational guardrails: active SQLite as source of truth, no default migration apply, no active SQLite moves, no verified review writes by AI, and explicit test reporting.
- D2 audit history indicates the migration line is closed and the active database target stabilized at 15 papers. D4 should not reopen or mutate the closed D2 migration line unless a separate, explicit migration task is approved.
- D3 audit history indicates the safety line is closed: backend safety contract passed, frontend contract passed, export empty/blocked-only passed, writing/export safe gate passed, final closure passed, and D3-5.1 documented the frontend Playwright fallback environment debt.
- Some older audit manifests contain mojibake in Chinese text, but the closure facts and safety boundaries remain identifiable. Rewriting old manifests is not a D4 product priority.

### 2.2 Backend Core Capability

Reviewed areas:

- Paper management and ingestion:
  - `backend/app/api/papers/`
  - `backend/app/services/paper_ingestion.py`
  - `backend/app/services/paper_query.py`
  - `backend/app/services/paper_identity.py`
  - `backend/app/services/library_manager.py`
- Extraction and reprocessing:
  - `backend/app/api/extraction.py`
  - `backend/app/services/extraction_pipeline.py`
  - `backend/app/services/paper_reprocessing.py`
  - `backend/app/services/workflow_jobs.py`
- Review workflow and safety gates:
  - `backend/app/services/extraction_review_service.py`
  - `backend/app/services/review_target_resolver.py`
  - `backend/app/utils/review_safety.py`
- DFT database and export:
  - `backend/app/api/papers/aggregation.py`
  - DFT/catalyst/electrochemical/mechanism/writing extractors and normalizers
- Writing / RAG / evidence chain:
  - `backend/app/api/writer.py`
  - `backend/app/rag/`
  - `backend/app/api/evidence.py`
  - `backend/app/services/evidence_service.py`
  - `backend/app/services/evidence_locator_service.py`
- Active database / library health:
  - `backend/app/utils/active_database.py`
  - `backend/app/api/health.py`
  - `backend/app/api/system.py`

Readiness notes:

- The backend already has a broad working surface: paper CRUD/list/detail, ingestion, AI workflow jobs, extraction jobs/retry/cancel/results, review save/mark-verified/audit, DFT compare/aggregate/export, evidence locator APIs, retrieval, and writer draft/audit endpoints.
- D3 safety boundaries are implemented as explicit backend gates, not only frontend wording.
- Reprocessing can rebuild a paper document from persisted sections/tables/figures/artifacts and replace Stage 2 extraction results. This is a strong candidate for D4 real extraction hardening, but it touches extraction apply if executed against real papers.
- DFT CSV export is already safety-gated to safe verified rows with required evidence and exposes export/block headers. The compare/aggregate endpoints appear more product-oriented but are not themselves the same safe export path.
- Writing/RAG has an evidence-first path and tests for safe verified evidence gates. It is a promising product direction, but productizing it depends on enough verified evidence being available.
- Active database safety helpers remain central. D4 should keep active DB mutation out of readiness work and only touch real data in an explicitly approved execution task.

### 2.3 Frontend Core Pages

Reviewed areas:

- `frontend/pages/literature_library/`
- `frontend/pages/paper_detail/index.html`
- `frontend/pages/external_analysis_workbench/index.html`
- `frontend/pages/dft_database/index.html`
- `frontend/pages/ai_writer/index.html`
- `frontend/tests/smoke.spec.js`

Readiness notes:

- Literature Library is the broadest operational workspace: browse/filter papers, inspect detail tabs, open review flows, rerun extraction, view PDF/evidence locator status, and add writing evidence.
- Paper Detail exposes metadata, sections, DFT/performance content, and evidence-backed claim inspection.
- Human Workbench is the strongest safety-critical page: manual correction/verification, review audit states, stale/ambiguous/unresolved handling, locator badges, and exact-page/text-only/missing-page degradation behavior.
- DFT Database already provides a product-shaped surface: filters, table summary, evidence detail, safe CSV export status, and empty/blocked-only messaging. It likely needs productization around explainability, traceability, and verified-data workflow rather than another safety rewrite.
- AI Writing Studio has the skeleton of an evidence-first reporting workflow: paper selection, evidence search, evidence pack, draft generation, and citation audit. It is currently more of a workflow shell than a polished reporting product.

### 2.4 Test Coverage

Reviewed areas:

- `backend/pytest.ini`
- `backend/tests/`
- `frontend/tests/smoke.spec.js`
- D3 audit manifests summarizing recent validation results

Current locks:

- Backend tests cover paper APIs/query/reprocessing, extraction pipeline/reviews, DFT extractors/reproducibility, evidence APIs/page recovery/quality audit, review boundary enforcement, D3 review workflow regression, export safety gate, writing safety gate, RAG workflow, library manager/CRUD, active database recovery, D2 migration/readiness guards, MCP, settings, and workflow job schemas.
- Frontend smoke covers all main pages, core page interactions, writing evidence/audit flow, paper detail evidence panel, manual validation workbench, review target states, DFT evidence/export headers, DFT empty/blocked-only export UX, AI candidate materialize payload scope, extraction job center, library metadata-only flows, attach-PDF identity cases, and locator degradation/PDF behavior.

Coverage gaps for next phase:

- Real extraction quality over the 15 active papers is not locked as a broad, repeatable product acceptance gate. Existing tests use temporary databases and stable samples, which is appropriate for safety but not enough to prove real library usefulness.
- DFT Database product workflows are covered mostly as smoke and safe export scenarios, not as end-to-end verified-data analysis workflows with richer filtering, explanation, traceability, and evidence drilldown.
- Writing/report workflow is protected for safety gates, but not yet deeply tested as an end-to-end report artifact experience using real verified evidence packs.
- Frontend toolchain fallback is documented, but the standard `npm test` path remains broken in this shell because `npm` is not in `PATH`.

### 2.5 Known Residual Risks

- `npm` is not in `PATH`; frontend verification still depends on the bundled Node / Playwright fallback and temporary non-repo `@playwright/test` shim documented in D3-5/D3-5.1.
- Backend pytest passes in the latest D3 final closure, but third-party SWIG deprecation warnings remain.
- Real extraction stability is currently proven only by limited stable samples and audit history, not by a broad D4 product acceptance run across the 15 active papers.
- When page/bbox is missing, evidence locator behavior correctly degrades to text-only / missing-page / missing-locator states, but the user value depends on making that degradation visible and useful rather than pretending there is precise PDF positioning.
- The closed D2 migration line should remain closed. Do not touch active DB, canonical registry, migration apply scripts, historical mirror cleanup, or real artifacts as part of D4 planning.

## 3. D4 Candidate Routes

### A. Real Extraction Workflow Hardening

- Route name: Real Extraction Workflow Hardening
- Product value: Improve confidence that the 15 active papers produce usable structured results, evidence chains, and reviewable outputs. This is the most direct path from the current safety foundation to a trustworthy literature database.
- Technical scope:
  - Audit real extraction/reprocessing readiness without changing D2 migration state.
  - Define a small, repeatable acceptance set over active papers or controlled snapshots.
  - Harden reprocessing summaries, extraction job observability, evidence locator degradation display, and evidence_chain completeness.
  - Improve handling for missing page/bbox so text-only and missing-page states are clear and actionable.
  - Add focused tests only after PM chooses this route.
- Risk level: High if executed against active DB or real extraction apply; Medium if first step is audit/snapshot-only.
- Recommended executor: Codex + Antigravity collaboration.
  - Codex is well-suited for backend gates, tests, and manifest discipline.
  - Antigravity is useful for multi-paper manual UX walkthrough and real-world extraction quality review.
- Why now:
  - D2 and D3 are closed, so the next bottleneck is actual usable extraction quality rather than safety contract closure.
  - Reprocessing, locator degradation, review audit, and evidence safety primitives already exist.
  - This route can turn the 15-paper active library into a credible product demo.
- Why not now:
  - It may require touching real extraction apply or active DB if the route moves beyond audit.
  - Real-paper variability can expand scope quickly.
  - PM needs to decide whether product value should start at data quality or at a visible user-facing workflow.
- Touches real data / active DB / migration / verified review / extraction apply:
  - Real data: likely yes in later execution, but first step can be read-only.
  - Active DB: only with explicit approval; not in readiness.
  - Migration: no.
  - Verified review: no direct AI verified writes; human confirmation remains required.
  - Extraction apply: likely yes only after explicit PM approval.
- Suggested first task name: `D4-1 Real Extraction Readiness Snapshot`

### B. DFT Database Productization

- Route name: DFT Database Productization
- Product value: Make extracted DFT results usable as an actual database product: filter, compare, explain, inspect evidence, understand blocked vs safe rows, and export only safe verified data.
- Technical scope:
  - Improve DFT result browsing around verified/safe status, blocked reasons, locator status, paper metadata, and evidence drilldown.
  - Clarify differences between raw extracted rows, safe verified rows, and exportable rows.
  - Strengthen compare/aggregate/read-only analysis flows.
  - Preserve safe CSV export semantics and D3 headers.
  - Add route-specific smoke and backend tests after PM selection.
- Risk level: Medium.
- Recommended executor: Codex primary, Antigravity optional for UX review.
- Why now:
  - DFT Database already has a page, backend compare/aggregate/export endpoints, evidence detail, filters, and D3 export safety locks.
  - It provides visible product value without necessarily applying new extraction results.
  - It can leverage the completed D3 safe verified gate.
- Why not now:
  - If very few rows are human verified, the safe export/product view may look sparse.
  - Without better real extraction hardening first, the page may productize imperfect underlying data.
  - PM may prefer to improve source data quality before polishing analysis workflows.
- Touches real data / active DB / migration / verified review / extraction apply:
  - Real data: can be read-only.
  - Active DB: should be read-only unless a separate data task is approved.
  - Migration: no.
  - Verified review: no AI writes; may display verified state.
  - Extraction apply: no.
- Suggested first task name: `D4-1 DFT Database Safe Product View`

### C. Writing / Evidence-first Report Workflow

- Route name: Writing / Evidence-first Report Workflow
- Product value: Help users move from verified evidence to report-ready material: evidence packs, citation audit, DFT summaries, and draft sections that remain grounded in safe evidence.
- Technical scope:
  - Productize evidence pack creation and inspection.
  - Make citation audit results more actionable.
  - Connect DFT/writing cards/retrieval outputs into report sections without bypassing safe verified gates.
  - Add exportable report artifacts only if PM approves the format and safety language.
  - Add tests around end-to-end report generation from safe evidence after route selection.
- Risk level: Medium to High.
- Recommended executor: Codex + Antigravity collaboration.
  - Codex for safe evidence constraints, backend tests, and writer contract.
  - Antigravity for report workflow UX and content-quality review.
- Why now:
  - AI Writing Studio already has evidence search, evidence pack, draft, and citation audit flow.
  - D3 writing/export safe gate is closed, so the safety baseline is in place.
  - This route aligns with high-value user output: a report draft or evidence pack.
- Why not now:
  - The route depends heavily on verified evidence availability.
  - Generated writing quality can distract from database/extraction correctness.
  - Report export may introduce new safety wording and artifact expectations that need careful product decisions.
- Touches real data / active DB / migration / verified review / extraction apply:
  - Real data: read-only if limited to existing safe verified evidence.
  - Active DB: no writes by default.
  - Migration: no.
  - Verified review: no AI verified writes.
  - Extraction apply: no.
- Suggested first task name: `D4-1 Evidence Pack Report Workflow Design Lock`

### D. Frontend Toolchain Cleanup

- Route name: Frontend Toolchain Cleanup
- Product value: Reduce verification friction by restoring standard `npm test` / Playwright execution, removing reliance on bundled runtime paths and temporary shims.
- Technical scope:
  - Fix local/frontend Node dependency setup and PATH expectations.
  - Document or script a stable test command.
  - Remove the need for temporary non-repo `@playwright/test` shim in normal verification.
  - Keep business UI unchanged.
- Risk level: Low to Medium.
- Recommended executor: Codex primary.
- Why now:
  - D3-5/D3-5.1 repeatedly hit the same environment debt.
  - A stable frontend toolchain would make all future D4 UI verification cheaper.
- Why not now:
  - It is engineering hygiene, not a product mainline.
  - It does not improve real extraction quality, DFT product usefulness, or report workflow by itself.
  - It may be host-environment-specific rather than repository code.
- Touches real data / active DB / migration / verified review / extraction apply:
  - Real data: no.
  - Active DB: no.
  - Migration: no.
  - Verified review: no.
  - Extraction apply: no.
- Suggested first task name: `D4-Tooling Frontend Playwright Standard Path`

## 4. Recommended Priority for PM Choice

1. Recommended first: Real Extraction Workflow Hardening
2. Second: DFT Database Productization
3. Later: Writing / Evidence-first Report Workflow
4. Not now: Frontend Toolchain Cleanup as the D4 product mainline

Rationale: Real extraction quality is the highest-leverage dependency for the other product surfaces. DFT Database is the best visible product route once the data quality/readiness picture is clear. Writing/report workflow is valuable, but it should rely on enough verified evidence to avoid turning safety scaffolding into thin output. Toolchain cleanup is worthwhile as a parallel or short maintenance task, but it should not become the D4 product route unless verification friction blocks the chosen mainline.

## 5. Explicit Non-Actions

This D4-0 pass did not touch:

- Active DB
- Migration apply
- Canonical registry
- Historical mirror cleanup
- Verified review write
- Extraction apply
- Real full materialize
- Real artifacts cleanup
- Backend business logic
- Frontend business UI
- API safety contract
- Tests

## 6. Validation Plan for This Pass

Required docs-only validation:

- `git diff --check`

Backend pytest and frontend Playwright are not required for this pass because the only intended change is this docs-only manifest.
