# Literature AI

`AI-shujvku / literature-ai` 当前处于 `D2 数据底座 / migration readiness` 阶段。本轮文档以降低误导风险为目标，当前活跃业务库仍然是每个文献库目录下的 `SQLite database.sqlite`；`PostgreSQL + pgvector` 仅作为可选能力存在，不是默认活跃库。

## 当前事实

- 当前活跃数据源：`literature-ai/data/libraries/<library>/database.sqlite`
- 当前默认文献库 source of truth：active library 的 SQLite
- `PostgreSQL + pgvector`：可启动、可实验，但不是当前默认业务主库
- 当前阶段：`D2 migration readiness`
- 当前禁止事项：
  - 不要直接执行 migration apply
  - 不要移动 active SQLite
  - 不要改 canonical registry
  - 不要删除真实 `data/`、`artifacts/`、shadow report 或历史审计产物

## 快速启动

1. 进入项目目录：

```bash
cd literature-ai
```

2. 启动本地服务：

```bash
docker compose up --build
```

3. 健康检查：

```bash
curl http://localhost:8000/api/health
```

4. 打开主工作台：

- 文献库工作台：<http://localhost:8000/pages/literature_library/index.html>
- 外部 AI 工作台：<http://localhost:8000/pages/external_analysis_workbench/index.html>

## 文档入口

- 项目协作规则：[literature-ai/AGENTS.md](./literature-ai/AGENTS.md)
- 子项目技术说明：[literature-ai/README.md](./literature-ai/README.md)
- 中文使用说明：[literature-ai/使用说明.md](./literature-ai/%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md)
- 文档索引：[literature-ai/docs/README.md](./literature-ai/docs/README.md)

## 当前建议工作方式

- 每轮开始前先执行：

```bash
git status --short
git log -1 --oneline
git branch -vv
```

- 若任务涉及数据库或迁移，先把目标限定为“审计 / readiness / 文档 / 测试”，不要默认进入 apply。
- 当前真实进度以 `README + AGENTS + git history` 为准，不再依赖旧版 `CHANGES.md`。
