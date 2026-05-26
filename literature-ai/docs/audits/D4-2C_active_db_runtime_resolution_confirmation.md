# D4-2C Active DB Runtime Resolution Confirmation

Date: 2026-05-27

Scope: confirm whether runtime active DB resolution consistently points to the canonical 15-paper DB found in D4-2B. This pass did not modify resolver code, registry files, database files, papers, reviews, artifacts, migrations, extraction state, or reprocessing state.

## 1. Baseline / Sync

Commands requested and executed before any edit:

- `git status --short`: clean
- `git log -1 --oneline`: `34970b0 test d4 real extraction baseline locks`
- `git rev-parse HEAD`: `34970b0d691e21d37212de2876bf6aaefff55d9b`
- `git branch -vv`: `master 34970b0 [origin/master] test d4 real extraction baseline locks`
- `git fetch origin`: succeeded
- `git rev-parse origin/master`: `34970b0d691e21d37212de2876bf6aaefff55d9b`
- `git ls-remote origin refs/heads/master`: `34970b0d691e21d37212de2876bf6aaefff55d9b refs/heads/master`

Baseline conclusion:

- Starting HEAD: `34970b0d691e21d37212de2876bf6aaefff55d9b`
- Local `origin/master`: `34970b0d691e21d37212de2876bf6aaefff55d9b`
- Remote `refs/heads/master`: `34970b0d691e21d37212de2876bf6aaefff55d9b`
- Worktree before edits: clean.
- HEAD, local `origin/master`, and remote `refs/heads/master` were identical before this docs-only manifest was added.

## 2. Code Audit

Reviewed:

- `literature-ai/backend/app/utils/active_database.py`
- `literature-ai/backend/app/utils/artifact_paths.py`
- `literature-ai/backend/app/utils/project_paths.py`
- `literature-ai/backend/app/api/health.py`
- `literature-ai/backend/app/api/system.py`
- `literature-ai/backend/app/main.py`
- `literature-ai/backend/app/workers/celery_app.py`
- `literature-ai/backend/app/services/library_manager.py`
- `git grep` for registry and active database resolution references under `literature-ai/backend/app`, `literature-ai/backend/tests`, and `literature-ai`

Runtime precedence confirmed:

1. `project_paths.canonical_registry_path()` resolves to `literature-ai/data/library_registry.json` based on `__file__`, not process current working directory.
2. `active_database.get_registered_active_library_info()` reads only that canonical registry for the active library root.
3. `LibraryManager.REGISTRY_PATH` also uses `canonical_registry_path()`.
4. `project_paths.shadow_registry_paths()` lists `workspace/data/library_registry.json` and `literature-ai/backend/data/library_registry.json` as shadow registries, not as active precedence inputs.
5. `get_active_database_info()` prefers the registered active library DB when it exists, has a `papers` table, and `papers_total > 0`.
6. Candidate scanning can inspect workspace/backend DBs only after the registered active DB fails that preferred check.
7. `main.py` and Celery worker startup call `activate_active_library_database()`, which uses the same canonical registry path.

Answers to D4-2C questions:

- Runtime registry priority: `literature-ai/data/library_registry.json`.
- Repo project-level registry usage: yes, it is the canonical registry.
- Backend registry priority: no; `literature-ai/backend/data/library_registry.json` is a shadow/stale risk input for audits, not the active source of truth.
- Backend stale target fallback: it does not control runtime resolution through the current active DB helper.
- Backend/data_backup fallback risk: not observed in the active resolver. `data_backup` is not in `_candidate_sqlite_paths()`.
- Backend/data/libraries/default 0-paper fallback risk: not when canonical 15-paper DB exists, because the registered active DB is returned before scan fallback.
- CWD drift risk: not observed. Resolution stayed canonical from repo root, `literature-ai`, and `literature-ai/backend`.

## 3. Read-Only Runtime Diagnostic

Canonical registry path:

- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`

Shadow registry paths reported by code:

- `D:\Desktop\03_代码与开发\AI-shujvku\data\library_registry.json`
- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\data\library_registry.json`

Registered active library:

- active library: `默认文献库`
- active library root: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default`
- active library DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- registry entry found: yes

Runtime `get_active_database_info()`:

- `db_kind`: `sqlite`
- `db_path`: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `effective_db_path`: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `effective_storage_root`: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\storage`
- `matches_active_library_db_path`: true
- `effective_matches_active_library_db_path`: true
- `effective_db_has_papers_table`: true
- `effective_db_papers_total`: 15
- `recovered_from_candidate_scan`: false

Additional read-only SQLite verification used `mode=ro`:

- resolved DB exists: yes
- effective DB exists: yes
- `papers_total`: 15
- stable sample exists: yes
- stable sample row:
  - id: `3978dc79f94f4457863fd68449ae293d`
  - title: `锂硫电池非均相电催化剂`

## 4. CWD Stability Check

Read-only imports with `PYTHONPATH` pinned to `literature-ai/backend` returned the same DB from:

- `D:\Desktop\03_代码与开发\AI-shujvku`
- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai`
- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend`

All three returned:

- `db_path`: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `effective_db_path`: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `effective_db_papers_total`: 15
- active/effective match flags: true

## 5. Backend Stale Registry Risk Gate

Known stale registry:

- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\data\library_registry.json`

Known stale target:

- `D:\Desktop\代码开发\AI检索数据库\literature-ai\backend\data\libraries\default\database.sqlite`

Gate result:

- The stale backend registry did not override canonical runtime resolution.
- The current active DB helper resolved both `db_path` and `effective_db_path` to the canonical 15-paper DB.
- No code change was required for this gate.

Remaining risk:

- The stale backend registry file still exists on disk and can confuse manual tools or future code that bypasses `canonical_registry_path()`.
- Startup activation uses `LibraryManager`, which can write registry/library metadata as part of normal app startup behavior. This D4-2C pass did not run startup activation and did not write those files.

## 6. Safety

- DB write: no
- DB copy/move/delete: no
- Registry write: no
- Migration apply: no
- Extraction apply: no
- Reprocessing apply: no
- Verified review write: no
- Materialize: no
- Artifact cleanup: no

## 7. Conclusion

D4-2C confirms that the current runtime active DB resolution is stable for the canonical 15-paper DB:

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`

D4-2 can proceed to the next read-only coverage snapshot only after product confirmation that this canonical project-level registry is the intended runtime source of truth.
