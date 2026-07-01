# Local Working Area

`local/` 用来集中放置当前机器上的本地资产，不属于项目正式源码。

## 建议分区

- `backups/repo-migrations/`：仓库级迁移、清理前保留的备份。
- `backups/runtime/`：运行期数据、数据库、对象存储等备份。
- `backups/security/`：安全排查或权限修复相关备份。
- `test-fixtures/`：可复用的测试样本、PDF 集合、保留的验收工作区。
- `test-runs/`：一次性回归运行结果和临时验证输出。

## 当前分类

- `test-fixtures/backend/`：后端链路 smoke 用的本地 PDF 输入集合。
- `test-fixtures/pdf-regression/new_real_papers/`：`run_new_real_papers_e2e.py` 使用的真实 PDF 回归输入。
- `test-fixtures/pdf-eval/pdfs/`：保留的 pdf-eval 原始输入 PDF。
- `test-runs/pdf-eval/legacy_snapshot/`：历史 pdf-eval 运行输出，包括 `storage/`、`eval.sqlite`、`ingestion_results.json`。
- `test-runs/pdf-regression/`：新的 PDF 回归脚本运行结果目录。

## 使用约定

- 新的本地备份、测试样本、回归结果优先放在 `local/`，不要再散落到仓库根目录。
- 需要随仓库长期保留的正式交付物，放到 `literature-ai/deliverables/`。
- 一次性系统运行输出，放到 `literature-ai/outputs/tmp/` 或 `literature-ai/backend/scratch/`。
