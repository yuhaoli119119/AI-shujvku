# D4-3F Controlled Locator Recovery Helper / Repair Proposal Gate

Date: 2026-05-27

Scope: backend-only read-only helper for generating locator repair proposals. This round did not write active DB rows, review rows, locators, materialized facts, registry entries, exports, or artifacts.

## 1. Baseline / Sync

Required preflight commands:

- `git status --short`: clean
- `git log -1 --oneline`: `be5ab9c docs d4 exact locator repair planning`
- `git rev-parse HEAD`: `be5ab9c49029e652196ca4ed9448e0d4caed625c`
- `git branch -vv`: `* master be5ab9c [origin/master] docs d4 exact locator repair planning`
- `git fetch origin`: first attempt hit a transient Windows schannel TLS handshake failure; immediate retry succeeded.
- `git ls-remote origin refs/heads/master`: `be5ab9c49029e652196ca4ed9448e0d4caed625c refs/heads/master`
- `git rev-parse origin/master`: `be5ab9c49029e652196ca4ed9448e0d4caed625c`

Conclusion: after successful retry fetch, local `HEAD`, local `origin/master`, and remote `refs/heads/master` were identical at `be5ab9c49029e652196ca4ed9448e0d4caed625c`.

## 2. Scope / Modified Files

Modified files:

- `backend/app/services/locator_recovery_helper.py`
- `backend/tests/test_d4_locator_recovery_helper.py`
- `docs/audits/D4-3F_controlled_locator_recovery_helper.md`

No frontend files were modified.

## 3. Helper Behavior

`ControlledLocatorRecoveryHelper` accepts an explicit `LocatorRecoveryRequest` containing:

- `paper_id`
- `review_id`
- `field_name`
- `target_value`
- `evidence_text`
- `evidence_reference`
- generic artifact candidates
- evidence span candidates
- Docling block candidates with optional `prov.page_no` and `prov.bbox`

It returns `LocatorRepairProposal` only. The proposal includes:

- `paper_id`, `review_id`, `field_name`, `target_value`
- `status`: `green`, `yellow`, or `red`
- `proposed_locator_status`
- `source_artifact`
- `page`
- `bbox`
- `matched_text`
- `match_method`
- `confidence`
- `warnings`
- `blockers`
- `should_write_locator=false`
- `requires_human_confirmation=true`
- `mark_verified=false`
- `safe_verified=false`
- `export_eligible=false`
- `writing_eligible=false`

Supported matching:

- exact match
- normalized whitespace match
- substring match
- target token match
- evidence span candidates
- Docling `prov.page_no` / `bbox` extraction
- ambiguous multiple match detection
- no-match RED fallback

Safety behavior:

- ambiguous same-rank matches remain `yellow`
- no match returns `red`
- missing page is not fabricated and remains text-only/yellow
- missing bbox is not fabricated
- proposal status never implies safe human verification
- text-only match never unlocks export or writing
- `convergence_settings` is hard-gated RED for D4-3F

## 4. Test Coverage

New test file: `backend/tests/test_d4_locator_recovery_helper.py`.

Coverage:

1. exact text match returns proposal with page when source has page
2. normalized whitespace match works
3. Docling `prov.page_no` and `bbox` produce a candidate proposal
4. ambiguous match does not become green or safe
5. no match returns RED
6. current RED `convergence_settings` empty dict evidence remains RED
7. helper does not mark verified
8. helper does not set safe_verified
9. helper does not claim export/writing eligible
10. helper does not fabricate page when source lacks page
11. helper does not fabricate bbox when source lacks bbox
12. proposal defaults to `should_write_locator=false` and `requires_human_confirmation=true`
13. temp-DB regression confirms proposal generation does not write review rows or locator rows

## 5. D4-3E Feasibility Carried Forward

D4-3E classified four rows as YELLOW:

- `name / Fe-Co-V`
- `catalyst_type / single_atom`
- `metal_centers / Fe,Co,V`
- `rate / 0.2C`

D4-3F implements the helper needed before any later controlled write gate. It can produce human-reviewable locator proposals from target evidence spans, artifact text, and Docling provenance, but it intentionally does not persist them.

The D4-3E warning still applies: current pending-row evidence such as `HAADF-STEM` can be ambiguous and must not be used blindly as a safe locator source.

## 6. Why `convergence_settings` Remains RED

The D4-3E RED row is not locator-repairable from the current evidence because the evidence is an extracted empty-settings/synthetic payload rather than a source quote. D4-3F therefore hard-gates `field_name=convergence_settings` to RED with blockers:

- `d4_3e_red_field_not_repairable`
- `convergence_settings_requires_new_source_evidence`

This prevents generic DFT text, functional mentions, or empty dict evidence from being promoted into a locator proposal.

## 7. Verification

Backend focused test:

- `py -m pytest tests/test_d4_locator_recovery_helper.py`
- result: `13 passed, 3 warnings`

Required backend verification:

- `py -m compileall app findpapers tests`
- result: passed, exit code 0
- `py -m pytest`
- result: `346 passed, 626 warnings in 207.08s`

Frontend Playwright was not run because no frontend files were modified in D4-3F.

## 8. Safety Confirmation

- active DB write: no
- locator write: no
- verified review write: no
- `mark_verified`: no
- `save_reviews`: no
- extraction/reprocessing apply: no
- materialize: no
- migration apply: no
- export/writing unlock: no
- artifact cleanup: no
- DB copy/move/delete: no
- registry write: no

## 9. D4-3G Recommendation

Next step should be a controlled proposal manifest runner for the four D4-3E YELLOW rows only. It should read the active DB and artifacts in read-only mode, generate per-review proposals with this helper, and emit a reviewable manifest without writing locators.

Suggested D4-3G constraints:

- include the three catalyst rows and the electrochemical rate row
- keep `convergence_settings` RED and excluded from repair candidates
- require human confirmation before any D4-3H write gate
- continue blocking export/writing until exact locator writes and verified review gates are independently satisfied
