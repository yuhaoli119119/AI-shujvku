# ML Explorer 数据探索页

## 页面范围

入口：`/pages/ml_explorer/index.html`。页面只读展示锂硫双原子单库的冻结开发数据，包括用途账、当前 readiness 目标、历史 OOF/目标覆盖、材料分布和 F1/F2 SHAP。

页面不新增分析 API，也不写数据库。登录、页面和静态 JSON 均沿用 owner `/pages/*` 会话门禁；公开只读站不开放该目录。

## 接入文件白名单

Git 跟踪：

```text
frontend/pages/ml_explorer/index.html
frontend/pages/ml_explorer/page.css
frontend/pages/ml_explorer/chart-adapter.js
frontend/pages/ml_explorer/charts.js
frontend/pages/ml_explorer/app.js
frontend/pages/ml_explorer/data/.gitkeep
frontend/pages/visuals/index.html       # 仅入口链接
.dockerignore                          # 禁止私有 JSON 进入镜像构建上下文
.gitignore                             # 仅放行 data/.gitkeep
.env.example                           # 必需私有资产目录说明
docker-compose.yml                     # 仅 backend 私有数据只读 mount
deploy/scripts/github_deploy.py        # 私有 mount、来源、scope 与 hash 校验
deploy/tests/test_github_deploy.py     # 部署门禁正反向测试
docs/README.md
docs/GITHUB_DEPLOYMENT.md
docs/ML_EXPLORER.md
docs/releases/release-notes.md
README.md
```

Git 忽略、作为私有发布资产：

```text
frontend/pages/ml_explorer/data/app_data.json
frontend/pages/ml_explorer/data/manifest.json
outputs/ml-explorer-web-candidate/
```

不得把当前工作树的无关 dirty 文件和旧 `visuals` 修改带入集成提交。

## 私有资产

冻结资产：

- `app_data.json`：3,002,847 B，SHA-256 `2bda8ebbc4acbc90fb9184edce69292f56254b8b276360e202def4811d7e772b`。
- `manifest.json`：1,853 B，SHA-256 `7b0ad6c59be9709e681b3c6b8039ee013f3119c93e31c53c45794fba03506d95`。

部署到持久数据目录：

```text
/opt/AI-shujvku/literature-ai/data/ml-explorer-releases/<bundle-sha>/
  app_data.json
  manifest.json
```

该目录会整体只读挂载到页面数据路径，因此只放页面需要的两份 JSON；校验清单或发布回执放在目录外。

在私有 `.env` 设置 `LITAI_ML_EXPLORER_ASSET_DIR` 指向该 SHA 目录。变量没有缺省路径；新提交缺少变量时 `docker compose config` 会在计划阶段拒绝，不会产生“服务启动但页面无数据”的半完成状态。回退到旧提交后旧 compose 不引用该变量，变量残留不影响旧部署。

## 使用边界

- 177,948 个统计单元、22,678 行、142 篇文献和 5,775 个精确材料不是训练样本计数。
- OOF 是历史 `dummy_median` 诊断；当前 RDS、Step I/II 和完整路径没有目标模型。
- SHAP 只解释 F1/F2 RandomForest `material_descriptor_cv` 六吸附任务，不是因果、外部验证或材料排名。
- history RDS 89/72 与当前 RDS 68/64/0 分开；旧 barrier 缺来源组/fold 时保持未知。

## 验证与回退

发布前运行静态契约、页面交互、移动端、鉴权、bundle hash、旧页面与代表性文献成果回归。回退到先前应用 commit 即可移除入口和页面；私有资产目录和数据库成果保留，不执行整库恢复或重解析。
