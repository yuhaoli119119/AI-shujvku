# 单篇验收授权与保存

2026-10-26。职责唯一真源为 [AGENTS.md 第2节](../AGENTS.md#2-唯一职责定义与权限硬边界)。本文仅规定已有验收能力的调用，不授予开发、部署、科学裁决或历史身份见证权限。

## 当前能力

生产授权密钥已经配置；是否可读以管理命令运行结果为准。配置密钥、取得本篇短期授权、完成内容核验和保存独立结论是四个不同步骤。不得引用旧“生产尚未配置”的候选说明停止验收，也不得用已配置推断本篇已获授权。

同一工作台账号可由不同原生对话独立验收。总指挥通过原生工具创建本篇验收对话后，只向以下固定管理命令提交本篇身份；命令使用服务器私钥，核实两个原生对话、验收对话原始范围、本篇写者终态和当前版本，调用既有正式冻结/签发服务。总指挥和验收员均不读取私钥，不自行签名，不增加HTTP签发入口。

```bash
python scripts/reviewer_access.py \
  --paper-id PAPER_UUID \
  --coordinator-thread CURRENT_COMMANDER_THREAD_UUID \
  --reviewer-thread THIS_PAPERS_REVIEW_THREAD_UUID \
  --request-id REVIEW_REQUEST_ID
```

该命令仅登记**当前快照**与有限验收权限，不派发、解析或生成内容。coordinator 是当前快照登记任务，不冒充历史解析作者；原有作者、事件和未知历史身份保持原样。输出仅包含回执和私有凭据文件路径。凭据文件在项目 `outputs/reviewer-access-private/` 下，目录700、文件600；不得粘贴其内容到聊天。实际图表核验产物放输出的 evidence_directory，不能放凭据目录。

首次未提供覆盖文件时，冻结记录明确 covered_pages=[] 和“尚未核验”；不是科学通过，也不能用这份零覆盖快照提交 accepted。验收员应继续读取原文、检查页面及必要的普通定点修正，不必等待科学结论才能开始阅读。授权或原生读回失败才报告真实错误，不反复重建对话。

## 核验后更新冻结范围

验收员实际核验后保存 coverage JSON 数组，字段遵循 ExtractionFileCoverage：file_id、source_sha256、reading_pdf_sha256、covered_pages、remaining_reason、no_extractable_content_reason（后两项按实际情况提供）。页码必须来自实际核验，命令不推断覆盖。保存成果已改变时亦重新读取最新版本。

使用同一解析/验收角色对话，以**新的明确 request-id** 和 `--coverage-file FILE` 调用同一命令，获得对应新冻结版本授权。不能自动沿用旧授权宣称新版本通过。原 request-id 仅用于核对/重放同一身份、同一coverage、同一版本；超时先查其私有回执，原ID恢复，不重复create。授权过期则明确以新请求续期，不无限重试。

## 保存验收员已写好的结论

验收员自行形成符合 ExtractionReviewRequest 的 JSON；管理工具不生成结论。reviewer_task_id 必须为本篇验收对话ID，expected_revision 必须为本次冻结 revision。

```bash
python scripts/reviewer_access.py submit \
  --authorization-file /opt/AI-shujvku/literature-ai/outputs/reviewer-access-private/REQUEST_ID/authorization.json \
  --body /opt/AI-shujvku/literature-ai/outputs/PAPER_REVIEW/review.json
```

脚本仅完成正式登录、CSRF及短期授权请求头、一次保存与正式读回，回执私有落盘。不会自动重试未知写入。保存只读核验检查点时使用 `--kind progress`，body 遵循 ReviewProgressRequest，artifact须位于本次 evidence_directory。正文/SI全部核验及规则门槛仍由正式服务检查，未检查范围不能填通过。

## 范围与发布

这两个脚本是管理工具，不是后台队列或任务派发脚本；没有thread/start、turn/start或模型调用。它们调用当前运行版本的既有服务，不替换应用代码，不需要重启六应用或持久服务。工具按已推送固定Git提交发布，应用 git_commit 可仍为原业务版本；工具发布不冒称应用已升级。较大补缺、系统硬错误和同篇写入交接仍按唯一职责规则。
