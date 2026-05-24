# AGENTS.md -- AI 协作者同步规则

## 项目概述

Literature AI：文献解析与 AI 写作辅助系统，面向 DFT 催化/锂硫电池研究。

技术栈：FastAPI + PostgreSQL/pgvector + Redis + MinIO + GROBID + Docling + Celery + 静态前端。

## 同步规则（所有 AI 必须遵守）

### 1. 修改前先同步
每次开始工作前，读取 `README.md` 中"Current Progress"章节和本文件。
如果项目启用了 git，先执行：
```bash
git -C literature-ai log --oneline -10
git -C literature-ai status
```

### 2. 修改记录
每次代码变更后，在 `README.md` 的"Current Progress → Recent Changes"表格中追加一行记录：
```
| YYYY-MM-DD | 简要描述改动内容 | 影响文件 |
```
不要覆写已有记录。

### 3. 提交规范
```bash
git -C literature-ai add -A
git -C literature-ai commit -m "描述性标题

详细说明（可选）"
```

### 4. 进度同步到 README.md
每次提交后，更新 `README.md` 中的项目进度百分比和未完成清单。

### 5. 测试验证
改动涉及后端逻辑时，先确认测试能通过：
```bash
cd literature-ai/backend && python -m pytest tests/ -x --tb=short
```
如果测试失败，优先修复测试而非跳过。

## 常用命令

```bash
# 启动开发环境
cd literature-ai
docker compose up --build

# 运行所有测试
cd literature-ai/backend && python -m pytest tests/ -x --tb=short

# 运行特定测试
cd literature-ai/backend && python -m pytest tests/test_rag_workflow.py -x --tb=short

# 查看后端 API
curl http://localhost:8000/api/health

# 构建前端（静态页面直接访问）
# 前端页面通过 http://localhost:8000/pages/... 访问
```

## 项目结构

```
literature-ai/
  backend/
    app/
      api/          # FastAPI 路由
      db/           # SQLAlchemy 模型和 session
      extractors/   # Stage 2 抽取器
      parsers/      # GROBID/Docling 解析器
      normalizers/  # 数据规范化
      rag/          # 检索 + 写作 + citation guard
      schemas/      # Pydantic 请求/响应模型
      services/     # 业务服务层
      workers/      # Celery 异步任务
    tests/
  frontend/
    pages/
      literature_library/  # 文献库（本地+在线检索+导入）
      paper_detail/        # 论文详情
      dft_database/        # DFT 数据库
      mechanism_knowledge/ # 机理知识
      writing_cards/       # 写作卡片
      ai_writer/           # AI 写作
  prompts/           # LLM prompt 配置文件
  storage/           # PDF/TEI/提取物等
```

## 关键文件速查

| 文件 | 用途 |
|------|------|
| `backend/app/rag/writer.py` | 写作主控，编排 retrieval → prompt → backend → guard |
| `backend/app/rag/prompt_builder.py` | Evidence pack 压缩和 prompt 组装 |
| `backend/app/rag/citation_guard.py` | 数值上下文匹配校验 |
| `backend/app/rag/retriever.py` | 混合词法+向量检索 |
| `backend/app/rag/backends.py` | rule / llm_stub / openai_compatible 后端 |
| `backend/app/services/discovery_service.py` | 在线文献搜索和下载 |
| `backend/app/config.py` | 所有配置项（环境变量前缀 LITAI_） |

## 行为准则

**1. 只做被要求的，不画蛇添足**
- 不加没被要求的东西，小修改不顺手重做旁边的代码
- 不为不可能发生的情况做预防，只在真正需要的边界做校验

**2. 如实汇报**
- 事情没解决就说没解决，附具体情况
- 没做过的步骤就说没做过，不暗示做过了
- 事情确实做好了，不堆免责声明

**3. 不知道就说不知道**
- 不编造或预期结果
- 不在没信息的情况下编造一个看起来合理的答案

**4. 先看再改，不凭空编造**
- 必须先读过文件内容，才能编辑或总结它
- 严禁没读就改或凭记忆修改
- 没读就改会产生幻觉

**5. 前端调试排错规范（防止执行流被掐断）**
- **全局诊断优先**：当交互功能完全无反应时，严禁直接去优化或修改局部逻辑。必须在功能入口第一行通过 `console.log` 确认代码是否成功执行到这里。
- **防御性 DOM 编程**：严禁假定 DOM 元素一定存在。获取元素后必须做空值判断（如 `if (!el) return;`）或频繁使用可选链（`?.`），防止单个未捕获的报错直接掐断后续所有 JS 的初始化。

**6. 最小改动 + 先确认后行动（CRITICAL）**
- **每次只做最小改动**：只改动完成当前任务所必需的最少文件和最少代码。严禁顺手修改"旁边看起来可以优化"的内容。
- **较大改动前必须征得用户确认**：凡是涉及多文件修改、架构调整、删除/重命名、破坏性操作（如 `git checkout`、`git reset`、`rm -rf`），必须先向用户说明改动范围和风险，等待明确同意后再执行。
- **不确定就先问**：在执行任何操作前，若对目标文件、改动范围、预期效果有不确定之处，必须先向用户提问确认，再开始行动。严禁"先做了再说"或"做完再汇报"。
- **破坏性操作需二次确认**：`git checkout -- .`、`git clean -fd`、批量删除文件等不可逆操作，即使用户要求执行，也必须先列出将被影响的文件清单，再次确认后才能执行。


## 待办事项优先级

1. 真实 LLM 接入并提升段落质量
2. Evidence pack 去重和排序策略
3. Citation guard 从数值安全扩展到事实安全
4. 前端产品化 polish
5. 端到端集成测试
