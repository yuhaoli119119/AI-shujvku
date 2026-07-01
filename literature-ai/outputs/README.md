# Literature AI Outputs

`outputs/` 是 `literature-ai/` 系统内的运行期输出目录，分成两个区域：

- `outputs/exports/`：系统导出物、审计 JSON、回归报告等可本地保留的运行结果。
- `outputs/tmp/`：预览图、调试 JSON、截图和会话级临时中间物。

规则：

- 新脚本的运行期结果默认写入 `outputs/exports/`。
- 预览图、截图、调试输出和一次性中间文件写入 `outputs/tmp/`。
- `outputs/tmp/` 默认安全可清理。
- 如果某个产物需要随仓库长期保留，应从 `outputs/exports/` 转入 `../deliverables/`，而不是继续堆在运行目录里。
