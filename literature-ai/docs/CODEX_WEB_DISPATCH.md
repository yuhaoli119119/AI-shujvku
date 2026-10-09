# 总指挥 → Codex-web 派发流程（目标模式，已实测）

本文把"总指挥在外部派任务、Codex-web 在服务器上执行"这条流程固化下来，并提供可复用脚本。

- 脚本：`/opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs`（运行目录同名副本：`/opt/literature-ai/scripts/codex_web_dispatch.cjs`）
- 通道：复用**现有** Codex-web 服务的本机 IPC 桥，**不修改、不重启、不新起独立 app-server 冒充它**。

## 1. 通道原理（已实测）

Codex-web 是服务器上已有的服务，监听本机回环：`ws://127.0.0.1:8214/__backend/ipc`（`49000` 现为带鉴权的反向代理，派发直连 `8214`）。
桥已初始化完成，**不需要发 `initialize`**；一次连接 = Codex-web 自己创建一个独立 renderer 窗口。

发（外→内）：

```json
{
  "type": "ipc-renderer-invoke",
  "requestId": "唯一外层ID",
  "channel": "codex_desktop:message-from-view",
  "args": [
    {
      "type": "mcp-request",
      "hostId": "local",
      "request": { "id": "唯一RPC-ID", "method": "model/list", "params": {} }
    }
  ]
}
```

收（内→外）：

```json
{
  "type": "ipc-main-event",
  "channel": "codex_desktop:message-for-view",
  "args": [
    { "type": "mcp-response", "message": { "id": "唯一RPC-ID", "result": {} } }
  ]
}
```

失败时用 `message.error` 取代 `message.result`。**连接断开不会中止任务**（已实测：断开重连后仍能读到运行中的任务状态）。

### RPC 方法（按本机 `codex-cli 0.155.1` 协议核验）

| 方法 | 用途 | 关键参数 |
|---|---|---|
| `model/list` | 列出可用模型（只读） | `{}` |
| `thread/start` | 新建任务线程 | `{cwd, approvalPolicy:"never", sandbox:"danger-full-access"}`，可加 `model` |
| `thread/goal/set` | 设定/更新目标 | `{threadId, objective, status:"active"}`（`status` ∈ `active/paused/blocked/usageLimited/budgetLimited/complete`；可选 `tokenBudget`） |
| `thread/goal/get` | 读取目标 | `{threadId}` |
| `turn/start` | 在同一线程发一轮指令 | `{threadId, input:[{type:"text",text:"..."}], approvalPolicy:"never", sandboxPolicy:{type:"dangerFullAccess"}}` |
| `thread/read` | 读线程状态与轮次 | `{threadId, includeTurns:true}` |

返回值：`thread/start` → `thread.id`；`thread/goal/set|get` → `goal{objective,status,tokensUsed,tokenBudget,timeUsedSeconds}`；
`turn/start` → `turn.id`；`thread/read` → `thread.status`、`thread.turns[]`（最后一轮 `turn.status` 是 `inProgress`/`completed`，
`items[]` 含 `agentMessage.text` 与 `commandExecution.status`/`aggregatedOutput`）。

`thread/goal/get` 的合法返回只有两种：`goal:null`（当前无目标或目标已被清空）或完整 `goal` 对象（可能为
`active/paused/blocked/usageLimited/budgetLimited/complete`）。RPC error、权限错误、连接错误或响应缺少 `goal`
字段都是读取失败，不能伪装成“未设置”。`goal:null` 不能反推出历史状态曾为 `complete`。

协议依据：官方 app-server 文档 <https://developers.openai.com/codex/app-server>（本机可用
`codex app-server generate-json-schema --out <dir>` 导出当前版本 schema 核对）；上面这套**自定义桥的消息格式**是本项目实测结果，
不要照搬官方 stdio 传输假设。

## 2. 目标模式（`create` 的默认行为）

```
thread/start  →  thread/goal/set {objective, status:"active"}  →  turn/start
```

- **默认就是目标模式**：每次 `create` 都会先给新线程设一个 `active` 目标，再发第一条指令。
  Codex-web 会围绕该目标自行持续推进，不必总指挥反复催。
- `create` 对目标启用是 **fail-closed**：`thread/goal/set` 超时、RPC error 或未返回有效 `active goal` 时，
  脚本会保存已拿到的 `threadId` 与真实失败状态并以非 0 退出；不会发送 `turn/start`，也不会重新 `thread/start`。
- `objective` 默认取 `--prompt` 全文；也可以用 `--objective "..."` 单独指定（推荐：目标写"完成标准"，prompt 写具体要求）。
- **只有验收齐全才把 goal 置为 `complete`**：

```bash
# 验收通过后再执行（脚本会先打印提醒）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs goal \
  --thread <threadId> --complete
```

### 目标模式的边界（务必如实理解）

1. **目标模式不是对网络中断的绝对保障。** 它只保证"目标状态被记录下来、Codex-web 会据此持续执行"，
   并不能保证桥断线、服务重启、模型额度耗尽等外部故障下任务一定跑完。
2. **任务结果可以通过 `thread/read` 随时取回**（含断线重连后），这是取结果的可靠途径。
3. **本脚本（以及 Codex-web 本身）不会主动唤醒外部总指挥**，也不伪装任何"唤醒/通知"功能。
4. 因此**每次派发后必须设置合适的回查机制**。本项目现行做法：总指挥侧已建立 Codex heartbeat，
   每 10 分钟回查"正在执行的任务"与"验收任务"：
   - 无变化 → 静默，不产生通知；
   - 任务完成或失败 → 才通知总指挥。
   回查用 `read` / `status`（读 turn 与 goal）即可，**不需要重建任务**，也不要用 `create` 重复派发同一件事。
5. 实测行为：**目标结束后 goal 可能被清空**（`thread/goal/get` 返回 `goal: null`）。
   所以判断"是否完成"要**同时看 goal 与 `lastTurnStatus`**，不能只看 `goal.status`，也不能把 `null` 当成
   读取失败或历史 `complete`。`read` / `status` / `summary` / `wait` 都会同时打印 goal 与 turn，就是为此。

## 3. 在哪儿运行脚本

桥监听本机回环，脚本必须在**能访问 `127.0.0.1:8214` 的服务器主机**上执行；后端容器通过 `litai-codex-web-bridge.service`（`172.18.0.1:8214`）访问同一上游。
若总指挥不在服务器上，就通过 SSH 在服务器内调用（别名按本机 `~/.ssh/config` 实际配置，例如 `ai-shujvku-cf`）：

```bash
ssh ai-shujvku-cf 'cd /opt/ai-shujvku-src && /opt/node22/bin/node scripts/codex_web_dispatch.cjs models'
```

服务器侧可核验的事实（2026-09-22 实测）：sshd 监听 `0.0.0.0:22`；账号 `2401liyuhao` 属于 `wheel` + `docker` 组且 `sudo` 免密；
`authorized_keys` 中存在 `ai-shujvku@MUYV` 公钥。别名本身写在客户端 `~/.ssh/config`，**服务器侧无法核验**，以实连结果为准。

## 4. 命令用法

```bash
# 只读：列出模型（最轻量的连通性自检）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs models

# 新建任务 + 目标模式 + 首轮指令（cwd 必须在白名单内）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs create \
  --cwd /opt/ai-shujvku-src --prompt "任务文本" --objective "完成标准"

# 用服务器上的文件当 prompt（长任务建议这样）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs create \
  --cwd /opt/ai-shujvku-src --prompt-file /opt/literature-ai/outputs/dispatch-tasks/task-001.md

# 读状态（同时打印 goal 与 turn；断线后也用这个）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs summary --thread <threadId>
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs status --thread <threadId> --json

# 完整 JSON（显式要求时才回传完整 turn/tool 数据）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs read --thread <threadId> --json

# 只查目标
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs goal --thread <threadId>

# 改目标 / 暂停 / 验收完成
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs goal --thread <threadId> --objective "新目标" --status active
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs goal --thread <threadId> --complete

# 同一任务追加指令（不是新建、不是 fork）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs followup \
  --thread <threadId> --prompt "补充要求" --objective "新的明确完成标准"

# 阻塞等待 goal 与 turn 达到可停止状态（超时不重发，只提示继续 read）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs wait \
  --thread <threadId> --timeout 1800 --interval 5

# 中断正在跑的一轮（派错了、跑偏了、要止损时用；不影响其它线程）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs interrupt --thread <threadId>

# 暂停目标（不再围绕目标自动续跑）
/opt/node22/bin/node /opt/ai-shujvku-src/scripts/codex_web_dispatch.cjs goal --thread <threadId> --status paused
```

- `create` / `followup` 会打印并保存 `threadId`、`turnId`、`objective`；state 默认落在
  `~/.local/state/codex-web-dispatch/<threadId>.json`（可用环境变量 `CODEX_WEB_DISPATCH_STATE` 改）。
- `read` / `status` / `summary` 默认输出低额摘要：goal、最后 turn 状态、`latestProgress`、`finalReport`
  与最近未解决异常摘要。`latestProgress` 是进行中 turn 的最新进度；`finalReport` 只在 turn `completed`
  后取最后一条 agent 消息，不再把进行中 commentary 伪装成最终报告。异常默认最多 3 条、每条 200 字符，
  且只保留最后一次成功 command/file/patch 之后仍失败的尾部异常；历史失败若已被后续成功操作覆盖，不作为当前失败。
  只有显式 `read --json` 才返回完整 `goal`、`thread`、工具输出与历史 items。
- `followup` 先读取 goal 和线程状态：goal 为 `null`、`complete` 或其它非 `active` 状态时，必须显式提供
  `--objective`，脚本验证置为 `active` 后才发送 turn；最后 turn 仍 `inProgress/queued` 时拒绝发送
  （即使 goal 已被清空），避免重复启动。
- `wait` 同时观察 goal 与最后 turn。turn 已结束但 goal 仍为 `active` 时继续观察；`failed/interrupted`
  turn 如实输出并退出，不会无限空转；等待到时仍活跃则以超时退出且不重发。
- **超时不盲目重发 `create`/`turn/start`**：脚本会打印已拿到的 ID，让你改用 `read` 跟踪，避免重复任务。
- `cwd` 只允许 `/opt/ai-shujvku-src` 或 `/opt/literature-ai`；不传 `--cwd` 时默认 `/opt/ai-shujvku-src`。
- `interrupt` 只中断该线程当前这一轮，不删线程、不影响其它任务；派错任务时优先用它止损，不要用新建任务"覆盖"。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数或连接错误 |
| 2 | RPC 返回 `error` |
| 3 | 超时（脚本未重发，需人工用 `read` 跟踪） |
| 4 | `wait` 观察到最后 turn 为 `failed` 或 `interrupted` |

## 5. 模型选择（默认不改用户模型）

- 不传 `--model` 时**完全沿用用户当前模型配置**，脚本不覆盖全局设置。
- 需要临时指定时用 `--model <provider 模型 ID>`（只影响本次线程）。
- 服务器实际模型映射（从当前服务的客户端 bundle 只读核验，2026-09-22）：

| UI 名称 | 真实 provider 模型 ID |
|---|---|
| GPT-6-Astra | `cn:deepseek-v4-pro` |
| GPT-5.6-Sol | **`cn:glm-5.3`** |
| GPT-5.6-Terra | `cn:deepseek-v4.1-flash` |
| GPT-5.6-Luna | **`cn:glm-5.3-flash`** |
| GPT-5.5 | `cn:kimi-k2.6` |

即：Flash 额度不足要切轻量版 GLM-5.3 时，用 `--model cn:glm-5.3-flash`；后端 `ai_extract_service` 默认主模型 `cn:deepseek-v4.1-flash`，备用 `cn:glm-5.3-flash`。

## 6. 安全范围（硬约束）

1. **不改 Codex-web**：不动它的源码、配置、进程；不新起独立 app-server 冒充它；不重启它。
2. **禁止 `thread/fork`**，禁止加载其它会话历史。`thread/resume` 只允许恢复**你自己创建**的同一任务。
3. 每个任务只在一个线程里执行；任务内部不分叉、不创建子任务。
4. 输出与状态文件不得包含凭据；不要把 token/key 写进 prompt、日志或仓库文件。
5. 派发出去的子任务同样受仓库根 `AGENTS.md` 的服务器边界与删除规则约束（子代理看不到本对话，任务提示必须自包含）。
6. 本流程的“只读”是操作约束，不是操作系统级技术隔离；不得把它说成安全沙箱。

## 7. 并发约束

- **绝不允许两个执行单元同时改同一个文件。**
- 默认只开少量并行：按**互相独立的文件**拆任务；同一文件/同一目录的改动串行做。
- 不要"为了十几个小活开十几个代理"——并行度以不冲突、可验收为准，默认 2～4 个封顶。
- 每个并行任务只写自己的产出，最后统一汇总；冲突由总指挥合并，不让子任务互相覆盖。

## 8. 已实测记录（2026-09-22）

| 项目 | 结果 |
|---|---|
| `model/list` | 成功，返回 5 个模型（gpt-6-astra / gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna / gpt-5.5） |
| 读取自身运行中任务 | 成功（`thread/read` 返回 `status`、`lastTurnStatus: inProgress`） |
| 冒烟任务 1（普通模式） | threadId `01a0c5ec-d008-7773-af47-9670a7509140`，turnId `01a0c5ec-d098-72c0-a8b4-27065f33ea8b`，`completed`，回复 `CODEX_WEB_SMOKE_20260922_OK` |
| `thread/goal/set` + `thread/goal/get` | 成功：设置后读到 `status: active`，usage 随之计数 |
| 目标模式冒烟任务 | threadId `01a0c5f0-7bf2-7552-b142-0d4a45624568`，turnId `01a0c5f0-7d25-75c3-b7e8-381851a1dd0c`，`completed`，回复 `GOAL_MODE_SMOKE_20260922_OK` |
| goal 置 `complete` | 成功，且 `complete` 状态可保持、可读回 |
| goal 生命周期 | 目标达成（turn 跑完）后 `thread/goal/get` 返回 `goal: null` → 判断完成要结合 `lastTurnStatus` |
| `turn/interrupt` | 成功：误建线程 `01a0c5f5-cd34-76b1-9b5f-2017c130fd04` 被中断，`lastTurnStatus: interrupted`，随后 `goal --status paused` 生效 |
| 单元/mock 测试 | 13/13 通过：goal 设置失败不发 turn、超时不重复创建、无目标/complete followup 必须显式 objective、active 运行中拒绝重复 followup、goal 清空但 turn 运行中仍拒绝重复 followup、读取错误不伪装未设置、wait 区分 active goal 与完成 turn、failed turn 不空转、summary 区分 latestProgress/finalReport、不把历史失败当当前失败、尾部异常最多 3 条且每条 200 字符 |
| 修复后目标模式冒烟 | threadId `01a0c603-8fbc-78c3-900c-7f29386cefa5`，turnId `01a0c603-9142-79c0-98bc-d8b184b20028`，最终 `completed`，回复 `GOAL_MODE_FIX_SMOKE_20260922_OK`，goal 清空为 `null` |
| 同线程 followup 冒烟 | 同 threadId，turnId `01a0c603-da35-7bc3-9ed5-dd17b5cf3df0`，显式新 objective 后 `completed`，回复 `GOAL_MODE_FIX_FOLLOWUP_20260922_OK`，goal 清空为 `null` |
| summary 专项冒烟 | 完成态线程 `01a0c603-8fbc-78c3-900c-7f29386cefa5` 显示 `finalReport=GOAL_MODE_FIX_FOLLOWUP_20260922_OK`、`latestProgress=(none)`、`exceptions=none`；活动主任务 `01a0c5fd-5b16-7672-beee-a0a3407797f3` 显示最新进度、`finalReport=(none)`、`exceptions=none` |
| 范围说明 | 2026-09-22 总指挥更新：全项目业务重建与其它进度文档由主任务 `01a0c5fd-5b16-7672-beee-a0a3407797f3` 并发负责；本脚本专项只验收派发脚本、测试与 `CODEX_WEB_DISPATCH.md`，不把全量文档清洗或业务文件状态作为本脚本完成条件 |

## 9. 当前对话文献批次协调（阶段二源码候选，2026-10-03）

操作与恢复见 [BATCH_COORDINATION.md](BATCH_COORDINATION.md)。该流程优先由当前窗口调用原生 create_thread/read_thread/wait_threads/send_message_to_thread 与 automation_update(kind=heartbeat)。脚本不是批次状态真源，不自动续跑。每篇派发前持久记录意图，返回ID立即写入批次；创建请求超时不能重新create。默认串行，执行任务不能再派发。

原生任务能力不可用时，本IPC脚本仅是明确说明的适配路径；必须将 CODEX_WEB_DISPATCH_STATE 设置到本批次项目目录。已有脚本不是原子批次账本，且 thread/start 返回ID至保存state之间仍有窗口；批次意图会阻止重复创建，但丢失ID时可能需要用户在原请求/任务界面核对。不要调用本脚本wait长轮询替代原生heartbeat，不读取Codex私有状态/其他会话，不启动app-server。原生heartbeat不可用则暂停并说明不能自动持续推进。
