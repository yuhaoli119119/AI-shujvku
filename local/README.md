# Local Working Area

`local/` 用来集中放置当前机器上的本地资产，不属于项目正式源码。

## 建议分区

- `backups/repo-migrations/`：仓库级迁移、清理前保留的备份。
- `backups/runtime/`：运行期数据、数据库、对象存储等备份。
- `backups/security/`：安全排查或权限修复相关备份。
- `test-fixtures/`：可复用的测试样本、PDF 集合、保留的验收工作区。
- `test-runs/`：一次性回归运行结果和临时验证输出。

## 使用约定

- 新的本地备份、测试样本、回归结果优先放在 `local/`，不要再散落到仓库根目录。
- 需要随仓库长期保留的正式交付物，放到 `literature-ai/deliverables/`。
- 一次性系统运行输出，放到 `literature-ai/outputs/tmp/` 或 `literature-ai/backend/scratch/`。
