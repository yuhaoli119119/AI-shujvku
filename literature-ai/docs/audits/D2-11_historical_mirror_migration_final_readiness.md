# D2-11 Historical Mirror Migration Final Readiness Dry-run

## 1. Gate 名称

D2-11 Historical Mirror Migration Final Readiness Dry-run

## 2. 当前 HEAD

`8d6a013fc4b15c1fd1ab33f90f65a54ca26785d9`

## 3. Dry-run 结论

- readiness dry-run passed
- no apply executed
- recommended migration mode:
  `db_referenced_only_plus_required_library_metadata`

## 4. Target root 状态

proposed target root:

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default`

- `target_conflicts_count=0`
- `database.sqlite` 不存在
- `library.json` 不存在

## 5. Source root 状态

source root 仍是 historical mirror root:

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\DDesktop代码开发AI检索数据库literature-aibackenddatalibrariesdefault`

## 6. Active DB 状态

- `db_kind=sqlite`
- `active_database_papers_total=15`
- `recovered_from_candidate_scan=false`
- active DB 仍指向 historical mirror root 下的 `database.sqlite`

## 7. Canonical registry

canonical registry:

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`

- 仍指向当前 active mirror root
- 未切换到 target root
- 未改变

## 8. DB-referenced artifacts

- `pdf=6`
- `markdown=6`
- `tei=6`
- `docling_json=6`
- `total=24`
- `missing referenced files=0`
- `duplicate_artifact_paths=0`

## 9. Unreferenced artifacts

- `unreferenced files=162`
- `unreferenced PDFs=140`
- `unreferenced figures=22`

结论：

unreferenced artifacts 必须排除在迁移范围之外。
未来 apply 禁止复制 full source root。

## 10. 已执行 gate

- `d2_historical_mirror_migration_readiness.py passed`
- `d2_shadow_registry_hygiene_gate.py passed`
- `d2_target_conflict_and_artifact_inventory_gate.py passed`
- `d2_real_extraction_coverage_gate.py passed`
- `audit_ai_workflow_boundary.py passed`

## 11. 验证

- `python -m compileall app findpapers tests passed`
- `python -m pytest -q passed, 284 passed, existing warnings only`

## 12. 明确未发生

- no historical mirror migration apply
- no active DB copy
- no active SQLite move
- no canonical registry change
- no full source-root copy
- no real data/artifacts deletion
- no extraction apply
- no verified review write
- no force push

## 13. Remaining risk

- readiness risk remains medium because source is still historical mirror root
- two shadow registries remain stale/dangerous
- unreferenced source-root files are growing, likely from tests/scripts
- future migration apply must be controlled and referenced-only
