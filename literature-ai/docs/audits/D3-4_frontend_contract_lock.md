# D3-4 Frontend Contract Lock / UX Regression Gate

Baseline commit: `052ca76390181a6e008a34d2ad0fc9ca5ec3a03d`

Scope: Playwright smoke inventory and minimal long-term regression locks for the D3 frontend safety contract. This audit did not change backend APIs, migrations, active database state, verified review data, extraction apply flows, or production materialization.

## Coverage Inventory

### A. Materialize Payload Contract

- Single candidate materialize is covered in `frontend/tests/smoke.spec.js`: sends `candidate_ids: ["candidate-1"]` with `created_by`.
- Selected candidate materialize is covered: no-selection path does not POST, and selected path sends explicit non-empty `candidate_ids`.
- Full-run materialize is covered: sends `explicit_all: true` with `created_by`.
- Full-run materialize is covered against the unsafe legacy shape: it must not include `candidate_ids`, so it cannot send `candidate_ids: []`.
- Full-run and candidate materialize are covered as confirm-gated operations by asserting every POST-triggering flow passes through the confirmation dialog.

### B. Dangerous Wording Regression

- Review / AI candidate / materialize area is locked against the old dangerous wording:
  - `写回数据库`
  - `一键全部写回数据库`
  - `AI 审核`
  - `AI 分析结果`
  - `审核结果`
  - `已验证`
  - `已校验`
- The check is scoped to the review candidate area instead of globally banning wording that may be valid in unrelated system status or non-D3 contexts.

### C. Human Workbench Scope Contract

- Save / verify action scope is covered through `#actionScopeSummary`.
- The smoke test confirms the action area displays current schema, current filter, current visible records, and the fields that will be processed.
- The workbench state labels remain distinguishable in smoke coverage:
  - AI suggestions are labeled in the library review candidate area.
  - Generated pending records are labeled `已生成待确认记录`.
  - Manual corrections are labeled by the workbench status chip implementation.
  - Human-confirmed state is labeled `人工确认通过`.

### D. Export Safe Gate Contract

- DFT export UI is covered near the export action: it shows `Human verified + required evidence` and `blocked rows 不会导出`.
- Export response headers are surfaced in UI and locked by smoke coverage:
  - `X-D3-Export-Safety-Gate`
  - `X-D3-Export-Count`
  - `X-D3-Block-Count`
- Current smoke coverage verifies populated export state. Empty and blocked-only states remain a lower-risk residual gap because the UI copy already states blocked rows are excluded, and no backend or real export path was touched in this pass.

### E. Writing Evidence-First Contract

- AI Writing Studio is covered as an evidence-first flow: `Evidence search`, `Evidence pack`, `Evidence Panel`, and `Citation Audit` are visible before and during draft generation.
- Citation Audit remains directly visible and executable.
- Smoke coverage asserts there is no misleading `Export final`, `Final conclusion`, or `Direct export` entry point in the writing page.

## Residual Risk

- The DFT export empty / blocked-only UX is documented but not deeply scenario-tested in this pass.
- The workbench manual-correction label is covered by implementation inventory and existing edit flow, but the new assertion focuses on pending and scoped action visibility to keep this pass minimal and stable.
