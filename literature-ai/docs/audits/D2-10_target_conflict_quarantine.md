# D2-10 Target Conflict Quarantine Apply Gate

## 1. Gate 名称

D2-10 Target Conflict Quarantine Apply Gate

## 2. 执行结论

- target root conflict 已清空
- `target_conflicts_count=0`

## 3. Quarantine 目录

`D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backups\d2_target_root_quarantine_20260526_175210`

## 4. 被隔离文件

原路径：

- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\library.json`

quarantine 后路径：

- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backups\d2_target_root_quarantine_20260526_175210\database.sqlite`
- `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backups\d2_target_root_quarantine_20260526_175210\library.json`

## 5. SHA256

`database.sqlite`

`a2541603ed0f8d745a61395ac06576afa84c980afe48d6445b3e5479052d4db0`
`->`
`a2541603ed0f8d745a61395ac06576afa84c980afe48d6445b3e5479052d4db0`

`library.json`

`8353827d54bd22075f6c1d0aefff183b447c49da39e841eb90a43bc89ec54db7`
`->`
`8353827d54bd22075f6c1d0aefff183b447c49da39e841eb90a43bc89ec54db7`

## 6. Active DB 状态

- `db_kind=sqlite`
- `active_database_papers_total=15`
- `recovered_from_candidate_scan=false`
- active SQLite SHA256: `a1c7fb7b7a3b1beba57a532b994a8dd14d306c4b67f3a8fe842ad74fd73852df`

## 7. canonical registry

- 未改变
- SHA256: `40d3073efffda894fa6d1016747b6086df6a8b87878d8e3d7ace0d19899afb01`
- 仍未指向 target root runtime DB

## 8. 已执行审计

- `d2_shadow_registry_hygiene_gate.py exit_code=0`
- `d2_historical_mirror_migration_readiness.py exit_code=0`
- `d2_target_conflict_and_artifact_inventory_gate.py exit_code=0`
- `d2_real_extraction_coverage_gate.py exit_code=0`
- `audit_ai_workflow_boundary.py exit_code=0`

## 9. 验证

- `python -m compileall app findpapers tests` passed
- `python -m pytest -q` passed, `284 passed`, existing warnings only

## 10. 明确禁止事项未发生

- no historical mirror migration apply
- no active SQLite move
- no canonical registry change
- no full source root copy
- no real data/artifacts deletion
- no extraction apply
- no verified review write
