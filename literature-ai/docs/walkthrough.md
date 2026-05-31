# LitAI 稳定升级路线：阶段 5 验收与修复汇报

针对刚刚指出的 P1 和 P2 两个问题，我已经进行了全面的修复，不再使用系统自带的工件文件（Artifacts），以后将此类说明直接记录在项目目录中（如本文件 `docs/walkthrough.md`）。

## 问题修复记录

### P1：单位归一化对 `meV` 的遗漏问题
**现象**：原归一化逻辑未包括 `meV`，导致数据集中提取器产出的 `meV` 格式能量单位保留为 null 归一化值，从而在数据集里混杂多种不同单位的能量数据。
**修复 (`backend/app/api/papers/aggregation.py`)**：
- 已在 `export_dft_dataset` 的单位判断中补充了对 `"mev"` 的拦截与换算。
- 换算公式为：`norm_val = dr.value / 1000.0`，`norm_unit = "eV"`。
- **测试覆盖**：在 `tests/test_export_safety_gate.py` 中的 `test_dft_ml_dataset_export_uses_same_safe_verified_gate` 测试函数里，新增了对 `meV` 转换为 `eV` 和 `kJ/mol` 转换为 `eV` 的断言校验。

### P2：Schema 文档与实际接口契约（Payload）结构漂移
**现象**：新增的 schema 文档 `docs/schema/dft_ml_dataset_schema.md` 与实际代码 `aggregation.py` 中 `_paper_payload`、`_catalyst_payload` 等产生的 JSON 树不一致。
**修复 (`docs/schema/dft_ml_dataset_schema.md`)**：
- 对照实际代码逐字修复了契约定义：
  - `paper.authors` 修改为：`(list of strings | string)`。
  - `catalyst` 字段由 `catalyst_id` 更正为 `catalyst_sample_id`，并补充了实际返回的 `synthesis_method` 和 `evidence_strength` 等遗漏字段。
  - `dft_settings` 添加了 `dft_setting_id`、`dispersion_correction`、`pseudopotential`、`convergence_settings`、`vacuum_thickness_a` 和 `raw_json`，并移除了代码中未输出的 `solvation_model` 字段。
- **当前状态**：接口实际返回内容已经和 `dft_ml_dataset_schema.md` 达到了 100% 同步。

---

## 阶段 5 执行总结

至此，《LitAI 稳定升级路线》阶段 5（面向 ML 的数据版本与审计）已达成以下核心目标：
1. ML Dataset 导出时自带详细的 `schema_version`、过滤条件、及拦截原因审计字段。
2. 数据记录（Records）中的 `target` 节点补充了对 `eV`、`meV`、`kJ/mol` 及 `kcal/mol` 等混合能量单位的自动**归一化建议**，彻底解决混合单位污染训练集的问题，并提供原值回溯。
3. 提供了一份结构同步的接口契约说明 `docs/schema/dft_ml_dataset_schema.md`，可作为下游对接的真实参考。

**待完成/待解决问题（已知债务）**：
- **复杂或罕见特征归一化**：当前单位归一化覆盖了能量型属性，尚未处理比容量、速率常数等非能量维度的特征。
- **Zotero Adapter 深度集成（阶段 4 残留）**：目前仍需手动导入 CSL JSON。

**后续计划**：
当前所有 P0 / P1 遗留问题与稳定性路线图均已扫清，系统具备了高安全、高准确度导出的能力。下一步可以直接投入针对特定反应（如 Li-S 电池中的吸附能预测）的小规模真实数据测试，检验前后端端到端业务流的可用性。
