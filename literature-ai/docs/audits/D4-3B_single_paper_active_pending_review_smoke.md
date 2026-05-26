# D4-3B Single-Paper Active Pending Review Smoke

Date: 2026-05-27

Scope: explicitly authorized minimal active DB write for one pilot paper only. The only allowed write was to call the D4-3A prepare endpoint and create pending / unverified review rows for `3978dc79f94f4457863fd68449ae293d`. No verified review, extraction/reprocessing, materialization, migration, registry write, DB file operation, export, writing, or artifact cleanup was performed.

## 1. Baseline / Sync

Commands requested and executed before DB work:

- `git status --short`: clean
- `git log -1 --oneline`: `125697b feat d4 single paper review preparation`
- `git rev-parse HEAD`: `125697b78184aa1a576adc14515ee78bcaf3dca3`
- `git branch -vv`: `* master 125697b [origin/master] feat d4 single paper review preparation`
- `git fetch origin`: succeeded
- `git ls-remote origin refs/heads/master`: `125697b78184aa1a576adc14515ee78bcaf3dca3 refs/heads/master`

Baseline conclusion:

- Starting HEAD, local `origin/master`, and remote `refs/heads/master` were identical at `125697b78184aa1a576adc14515ee78bcaf3dca3`.
- Worktree before smoke: clean.

## 2. Active-write Authorization Scope

Allowed write:

- Paper only: `3978dc79f94f4457863fd68449ae293d`
- API path only: `POST /api/extraction/results/3978dc79f94f4457863fd68449ae293d/reviews/prepare`
- Row type only: `extraction_field_reviews`
- Status only: `reviewer_status=pending`
- Verified state only: `verified=False`

Everything else was prohibited and remained untouched.

## 3. Active DB Confirmation

- Registry path used: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- Resolved DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `papers_total`: 15
- Pilot paper exists: yes
- Pilot title: `锂硫电池非均相电催化剂`
- Pilot extraction state: `catalyst_samples=1`, `dft_settings=1`, `electrochemical_performance=1`, `evidence_spans=11`, `evidence_locators=0`, `writing_cards=1`

## 4. Before Snapshot

- Pilot review rows before: 0
- Pilot pending rows before: 0
- Pilot verified rows before: 0
- Pilot safe verified rows before: 0
- Non-pilot review rows before: 0
- DFT result rows total: 0
- Export eligible rows before: 0
- Writing eligible evidence before: 0

## 5. Prepare Endpoint Call

Call method:

- A temporary FastAPI app mounted the real `extraction_router` at `/api/extraction`.
- `get_db_session` was dependency-overridden to a direct SQLAlchemy session on the canonical active SQLite DB.
- This avoided the main app lifespan path and therefore avoided registry activation/writes while still using the real API route.

Call:

- `POST /api/extraction/results/3978dc79f94f4457863fd68449ae293d/reviews/prepare`
- Status: 200
- Returned rows: 5
- Returned statuses: `pending`
- Returned verified flags: all `false`

## 6. After Snapshot

- Pilot review rows after: 5
- Pilot pending rows after: 5
- Pilot verified rows after: 0
- Pilot safe verified rows after: 0
- Non-pilot review rows after: 0
- Export eligible rows after: 0
- Writing eligible evidence after: 0
- Writing cards total after: 6
- Paper metadata after: unchanged

New pending review rows:

| Review ID | Target type | Field | Evidence reference | Evidence text | Locator status | Exact locator | Unsafe locator |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `e2c75b7f-2d9c-41ff-a6e1-e95e5d491896` | `catalyst_samples` | `name` | yes | yes | `missing_page` | no | yes |
| `09f83676-8f13-4e82-a576-ab359b264933` | `catalyst_samples` | `catalyst_type` | yes | yes | `missing_page` | no | yes |
| `280f2d9e-3ebb-4107-9702-f6ea6d645465` | `catalyst_samples` | `metal_centers` | yes | yes | `missing_page` | no | yes |
| `4ba0e490-5934-439c-8136-33a8ddf4e201` | `dft_settings` | `convergence_settings` | no | yes | `missing_page` | no | yes |
| `56f72584-45b3-465b-9a40-97ec60a2fabf` | `electrochemical_performance` | `rate` | yes | yes | `missing_page` | no | yes |

All new rows:

- `reviewer_status=pending`
- `verified=False`
- `reviewer=None`
- `reviewer_note=prepared_from_extraction`
- `target_resolution_status=active`
- no exact locator
- no PDF jump / highlight eligibility

## 7. API / Human Workbench Visibility

Read-only API checks after prepare:

- `GET /api/extraction/results/3978dc79f94f4457863fd68449ae293d/reviews`: 200, returned 5 rows, all pending.
- `GET /api/extraction/results/3978dc79f94f4457863fd68449ae293d`: 200, `field_reviews` count 5.
- Extraction results payload has pending review objects attached to fields.
- `POST /api/extraction/results/3978dc79f94f4457863fd68449ae293d/validate`: 200, warning codes include `evidence_locator_missing_page`.

Frontend visibility:

- Human Workbench code reads `field.review.reviewer_status` and `field_reviews`, so these pending rows are displayable through the existing extraction results/review data path.
- No frontend prepare button was added in this gate. A dedicated UI trigger for the prepare endpoint should be a D4-3C product task.

## 8. Safety Check

- Verified rows after: 0
- Safe verified rows after: 0
- DFT export eligible before/after: 0 / 0
- Writing eligible before/after: 0 / 0
- Missing exact locator remains blocking: yes
- New rows are unsafe locator state: yes, all `missing_page`
- `save_reviews` verified bypass: not invoked; D4-3A regression tests still cover rejection
- `mark_verified`: not called

## 9. Rollback Plan

If rollback is needed, delete only the five rows created by this gate:

- `e2c75b7f-2d9c-41ff-a6e1-e95e5d491896`
- `09f83676-8f13-4e82-a576-ab359b264933`
- `280f2d9e-3ebb-4107-9702-f6ea6d645465`
- `4ba0e490-5934-439c-8136-33a8ddf4e201`
- `56f72584-45b3-465b-9a40-97ec60a2fabf`

Rollback SQL shape, if explicitly authorized later:

```sql
DELETE FROM extraction_field_reviews
WHERE paper_id = '3978dc79f94f4457863fd68449ae293d'
  AND reviewer_status = 'pending'
  AND id IN (
    'e2c75b7f2d9c41ffa6e1e95e5d491896',
    '09f836768f134e82a576ab359b264933',
    '280f2d9e3ebb41079702f6ea6d645465',
    '4ba0e4905934439c813633a8ddf4e201',
    '56f7258445b3465b9a4097ec60a2fabf'
  );
```

Rollback executed: no.

Reason not rolled back:

- The smoke matched expectations exactly.
- Only pilot paper pending/unverified review rows were created.
- Verified/export/writing eligibility stayed at 0.
- Keeping the pending queue supports D4-3C Human Workbench/front-end review validation.

## 10. Next Recommended Gate

D4-3C should validate the Human Workbench UX against these five pending rows and add a clear UI action for controlled review preparation if needed. Do not call `mark_verified` until exact locator coverage exists and a human reviewer explicitly performs verification.

## 11. Prohibited Actions Check

- Active DB write: yes, limited to the five pending/unverified pilot review rows listed above.
- DB copy/move/delete: no
- Registry write: no
- Migration apply: no
- Verified review write: no
- Extraction/reprocessing apply: no
- Full materialize: no
- Artifact cleanup: no
- Non-pilot review write: no
- Paper metadata modified: no
- Export/writing eligible changed from 0: no

