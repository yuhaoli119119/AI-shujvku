#!/usr/bin/env node
/**
 * codex_web_dispatch.cjs — 通过 Codex-web 现有本机 IPC 桥派发/跟踪 Codex-web 任务（目标模式）
 *
 * 设计约束（务必遵守）：
 *  - 只复用现有 Codex-web 服务（ws://[::1]:49000/__backend/ipc），不修改、不重启、不新起 app-server 冒充它。
 *  - 每次连接由 Codex-web 自行创建独立 renderer 窗口；桥已初始化，不需要 initialize。
 *  - 只允许 thread/start 新建、thread/goal/set 设目标、turn/start 发指令、thread/read + thread/goal/get 读取；
 *    thread/resume 仅用于恢复自己创建的任务。绝不使用 thread/fork，绝不加载其它会话。
 *  - create/turn 超时不自动重发，避免产生重复任务；已拿到的 threadId/turnId 会写入 state 目录。
 *  - 输出不打印任何凭据/环境变量；prompt 只从 --prompt / --prompt-file 读取。
 *
 * 目标模式：create = thread/start → thread/goal/set(active) → turn/start。
 *
 * 用法：
 *   node codex_web_dispatch.cjs models   # 只读连通性自检
 *   node codex_web_dispatch.cjs create   --cwd /opt/AI-shujvku/literature-ai --prompt "任务文本" [--objective "..."]
 *   node codex_web_dispatch.cjs create   --cwd /opt/AI-shujvku/literature-ai --prompt-file /tmp/task.md
 *   node codex_web_dispatch.cjs read     --thread <threadId> [--json]
 *   node codex_web_dispatch.cjs summary  --thread <threadId>
 *   node codex_web_dispatch.cjs status   --thread <threadId>
 *   node codex_web_dispatch.cjs goal     --thread <threadId>
 *   node codex_web_dispatch.cjs goal     --thread <threadId> --objective "新目标" [--status active]
 *   node codex_web_dispatch.cjs goal     --thread <threadId> --complete      # 仅验收齐全后使用
 *   node codex_web_dispatch.cjs followup --thread <threadId> --prompt "后续指令"
 *   node codex_web_dispatch.cjs interrupt --thread <threadId>
 *   node codex_web_dispatch.cjs wait     --thread <threadId> [--timeout 1800] [--interval 5]
 *
 * 退出码：0 成功；1 参数/连接错误；2 RPC error；3 超时（未重发，需人工用 read 跟踪）。
 */

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const WS_URI = process.env.CODEX_WEB_IPC_URI || "ws://[::1]:49000/__backend/ipc";
const HOST_ID = process.env.CODEX_WEB_HOST_ID || "local";
const CHANNEL_FROM_VIEW = "codex_desktop:message-from-view";
const CHANNEL_FOR_VIEW = "codex_desktop:message-for-view";

const CWD_WHITELIST = ["/opt/AI-shujvku/literature-ai", "/opt/literature-ai"];
const DEFAULT_CWD = "/opt/AI-shujvku/literature-ai";
const DEFAULT_STATE_DIR =
  process.env.CODEX_WEB_DISPATCH_STATE ||
  "/opt/AI-shujvku/literature-ai/outputs/codex-web-dispatch";

const GOAL_STATUSES = [
  "active",
  "paused",
  "blocked",
  "usageLimited",
  "budgetLimited",
  "complete",
];

const RPC_TIMEOUT_OVERRIDE_MS = Number(
  process.env.CODEX_WEB_DISPATCH_RPC_TIMEOUT_MS || 0
);

function loadWs() {
  const candidates = [
    "/home/2401liyuhao/apps/codex/web/node_modules/ws",
    path.join(__dirname, "node_modules", "ws"),
    "ws",
  ];
  for (const c of candidates) {
    try {
      return require(c);
    } catch (_) {
      /* try next */
    }
  }
  console.error(
    "[dispatch] 找不到 ws 库；请用 /opt/node22/bin/node 运行，或设置 NODE_PATH 指向 Codex-web 的 node_modules"
  );
  process.exit(1);
}

function parseArgs(argv) {
  const opts = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        opts[key] = true;
      } else {
        opts[key] = next;
        i += 1;
      }
    } else {
      opts._.push(a);
    }
  }
  return opts;
}

function nowIso() {
  return new Date().toISOString();
}

function uniqueId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

class Bridge {
  constructor(uri) {
    const WebSocket = loadWs();
    this.WebSocket = WebSocket;
    this.uri = uri;
    this.socket = null;
    this.pending = new Map();
    this.closed = false;
  }

  connect(timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`连接超时: ${this.uri}`)),
        timeoutMs
      );
      let socket;
      try {
        socket = new this.WebSocket(this.uri);
      } catch (err) {
        clearTimeout(timer);
        reject(err);
        return;
      }
      this.socket = socket;
      socket.on("open", () => {
        clearTimeout(timer);
        resolve();
      });
      socket.on("error", (err) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      });
      socket.on("message", (raw) => this.onMessage(raw));
      socket.on("close", () => {
        this.closed = true;
        const err = new Error(
          "与 Codex-web 的 IPC 连接已断开（任务不一定停止；可用同一 threadId 重新 read）"
        );
        for (const p of this.pending.values()) p.reject(err);
        this.pending.clear();
      });
    });
  }

  onMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(String(raw));
    } catch (_) {
      return;
    }
    if (msg.type !== "ipc-main-event") return;
    if (msg.channel !== CHANNEL_FOR_VIEW) return;
    const args = Array.isArray(msg.args) ? msg.args : [];
    for (const item of args) {
      if (!item || item.type !== "mcp-response") continue;
      const message = item.message || {};
      const id = message.id;
      const pending = this.pending.get(id);
      if (!pending) continue;
      this.pending.delete(id);
      clearTimeout(pending.timer);
      if (message.error) {
        const err = new Error(
          `RPC error: ${JSON.stringify(message.error).slice(0, 800)}`
        );
        err.rpcError = message.error;
        pending.reject(err);
      } else {
        pending.resolve(message.result);
      }
    }
  }

  call(method, params, timeoutMs = 180000) {
    return new Promise((resolve, reject) => {
      if (!this.socket || this.closed) {
        reject(new Error("IPC 未连接"));
        return;
      }
      const outerId = uniqueId("dispatch");
      const rpcId = uniqueId("rpc");
      const effectiveTimeoutMs =
        Number.isFinite(RPC_TIMEOUT_OVERRIDE_MS) &&
        RPC_TIMEOUT_OVERRIDE_MS > 0
          ? Math.min(RPC_TIMEOUT_OVERRIDE_MS, timeoutMs)
          : timeoutMs;
      const timer = setTimeout(() => {
        this.pending.delete(rpcId);
        const err = new Error(
          `RPC 超时(${effectiveTimeoutMs}ms): method=${method} rpcId=${rpcId}（脚本不会自动重发）`
        );
        err.timeout = true;
        err.rpcId = rpcId;
        reject(err);
      }, effectiveTimeoutMs);
      this.pending.set(rpcId, { resolve, reject, timer });
      this.socket.send(
        JSON.stringify({
          type: "ipc-renderer-invoke",
          requestId: outerId,
          channel: CHANNEL_FROM_VIEW,
          args: [
            {
              type: "mcp-request",
              hostId: HOST_ID,
              request: { id: rpcId, method, params: params || {} },
            },
          ],
        })
      );
    });
  }

  close() {
    try {
      if (this.socket) this.socket.close();
    } catch (_) {
      /* ignore */
    }
  }
}

function saveState(record) {
  try {
    fs.mkdirSync(DEFAULT_STATE_DIR, { recursive: true });
    const file = path.join(DEFAULT_STATE_DIR, `${record.threadId}.json`);
    fs.writeFileSync(
      file,
      `${JSON.stringify({ ...record, savedAt: nowIso() }, null, 2)}\n`,
      "utf8"
    );
    return file;
  } catch (err) {
    console.error(`[dispatch] 写 state 失败: ${err.message}`);
    return null;
  }
}

function assertCwd(cwd) {
  if (!CWD_WHITELIST.includes(cwd)) {
    throw new Error(
      `cwd 不在白名单内: ${cwd}\n允许: ${CWD_WHITELIST.join(", ")}`
    );
  }
}

function readPrompt(opts) {
  if (opts["prompt-file"]) {
    return fs.readFileSync(String(opts["prompt-file"]), "utf8");
  }
  if (typeof opts.prompt === "string") return opts.prompt;
  throw new Error("需要 --prompt 或 --prompt-file");
}

function threadStatus(thread) {
  if (!thread) return "unknown";
  const status = thread.status;
  if (typeof status === "string") return status;
  if (status && typeof status === "object") {
    for (const key of ["type", "state", "status", "kind"]) {
      if (typeof status[key] === "string") return status[key];
    }
    return Object.keys(status)[0] || "unknown";
  }
  return "unknown";
}

function lastTurn(thread) {
  const turns = Array.isArray(thread && thread.turns) ? thread.turns : [];
  return turns.length ? turns[turns.length - 1] : null;
}

function summarizeTurn(turn) {
  const out = { turnId: turn && turn.id, status: turn && turn.status, items: [] };
  const items = (turn && turn.items) || [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    if (item.type === "agentMessage" && typeof item.text === "string") {
      out.items.push({ type: "agentMessage", text: item.text });
    } else if (item.type === "commandExecution") {
      out.items.push({
        type: "commandExecution",
        status: item.status,
        command: Array.isArray(item.command)
          ? item.command.join(" ")
          : item.command,
        aggregatedOutput:
          typeof item.aggregatedOutput === "string"
            ? item.aggregatedOutput.slice(0, 4000)
            : undefined,
      });
    } else if (item.type === "reasoning" || item.type === "plan") {
      out.items.push({ type: item.type });
    }
  }
  return out;
}

function requireGoalResult(result, context) {
  const goal = result && result.goal;
  if (!goal || typeof goal !== "object") {
    throw new Error(`${context} 未返回有效 goal`);
  }
  return goal;
}

function assertGoalStatus(goal, expectedStatus, context) {
  if (goal.status !== expectedStatus) {
    throw new Error(
      `${context} 返回 goal.status=${String(goal.status)}，要求 ${expectedStatus}`
    );
  }
}

function errorExitCode(err) {
  return err.timeout ? 3 : err.rpcError ? 2 : 1;
}

function commandText(command) {
  return Array.isArray(command) ? command.join(" ") : String(command || "");
}

function truncateText(text, maxLength = 600) {
  if (typeof text !== "string") return undefined;
  return text.length > maxLength
    ? `${text.slice(0, Math.max(0, maxLength - 1))}…`
    : text;
}

function finalReport(turn) {
  if (!turn || turn.status !== "completed") return null;
  const messages = agentMessages(turn);
  return messages.length ? messages[messages.length - 1].text : null;
}

function agentMessages(turn) {
  return ((turn && turn.items) || []).filter(
    (item) =>
      item &&
      item.type === "agentMessage" &&
      typeof item.text === "string" &&
      item.text.trim()
  );
}

function latestProgress(turn) {
  const messages = agentMessages(turn);
  if (!messages.length) return null;
  if (turn && turn.status === "completed") {
    return messages.length > 1 ? messages[messages.length - 2].text : null;
  }
  return messages[messages.length - 1].text;
}

function unresolvedExceptionSummary(
  turn,
  maxExceptions = 3,
  maxCharsPerException = 200
) {
  const exceptions = [];
  const status = turn && turn.status;
  if (status === "failed" || status === "interrupted") {
    exceptions.push(`turn ${status}`);
  }

  const items = (turn && turn.items) || [];
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (
      item &&
      (item.type === "commandExecution" ||
        item.type === "fileChange" ||
        item.type === "patchApply") &&
      item.status === "completed"
    ) {
      break;
    }
    if (
      item &&
      item.type === "commandExecution" &&
      (item.status === "failed" || item.status === "declined")
    ) {
      let summary = `commandExecution ${item.status}: ${commandText(
        item.command
      )}`;
      if (
        typeof item.aggregatedOutput === "string" &&
        item.aggregatedOutput.trim()
      ) {
        summary += ` | ${item.aggregatedOutput.replace(/\s+/g, " ").trim()}`;
      }
      exceptions.push(truncateText(summary, maxCharsPerException));
    }
    if (exceptions.length >= maxExceptions) {
      break;
    }
  }
  return exceptions;
}

async function fetchGoal(bridge, threadId) {
  const result = await bridge.call("thread/goal/get", { threadId }, 60000);
  if (!result || typeof result !== "object" || !("goal" in result)) {
    throw new Error("thread/goal/get 未返回 goal 字段（可能是协议或权限异常）");
  }
  if (
    result.goal !== null &&
    (!result.goal ||
      typeof result.goal !== "object" ||
      typeof result.goal.status !== "string")
  ) {
    throw new Error("thread/goal/get 返回的 goal 不是 null 或有效对象");
  }
  return result.goal;
}

function printGoal(goal) {
  if (goal === null) {
    console.log(
      "goal: null（协议确认当前无目标/已清空；不是读取错误，也不能证明历史 complete）"
    );
    return;
  }
  if (!goal || typeof goal !== "object") {
    console.log("goal: (无效返回)");
    return;
  }
  console.log(`goal.status: ${goal.status}`);
  console.log(
    `goal.usage: tokensUsed=${goal.tokensUsed} tokenBudget=${
      goal.tokenBudget === null || goal.tokenBudget === undefined
        ? "no-limit"
        : goal.tokenBudget
    } timeUsedSeconds=${goal.timeUsedSeconds}`
  );
  console.log("goal.objective:");
  console.log(goal.objective);
}

async function withBridge(fn) {
  const bridge = new Bridge(WS_URI);
  try {
    await bridge.connect();
  } catch (err) {
    console.error(`[dispatch] 无法连接 IPC 桥 ${WS_URI}: ${err.message}`);
    process.exit(1);
  }
  try {
    await fn(bridge);
  } finally {
    bridge.close();
  }
}

async function cmdModels() {
  await withBridge(async (bridge) => {
    const result = await bridge.call("model/list", {}, 60000);
    const data = (result && result.data) || [];
    console.log(`[dispatch] model/list 返回 ${data.length} 个模型`);
    for (const m of data) {
      console.log(`  - ${m.id}${m.displayName ? ` (${m.displayName})` : ""}`);
    }
  });
}

async function cmdCreate(opts) {
  const cwd = String(opts.cwd || DEFAULT_CWD);
  assertCwd(cwd);
  const prompt = readPrompt(opts);
  const objective =
    typeof opts.objective === "string" ? opts.objective : prompt;
  await withBridge(async (bridge) => {
    const startParams = {
      cwd,
      approvalPolicy: "never",
      sandbox: "danger-full-access",
    };
    if (typeof opts.model === "string") startParams.model = opts.model;
    const started = await bridge.call("thread/start", startParams, 120000);
    const threadId = started && started.thread && started.thread.id;
    if (!threadId) throw new Error("thread/start 未返回 thread.id");
    console.log(`threadId: ${threadId}`);
    console.log(`cwd: ${started.cwd || cwd}`);

    let goal;
    try {
      const goalResult = await bridge.call(
        "thread/goal/set",
        { threadId, objective, status: "active" },
        60000
      );
      goal = requireGoalResult(goalResult, "thread/goal/set");
      assertGoalStatus(goal, "active", "thread/goal/set");
      if (typeof goal.objective !== "string") {
        throw new Error("thread/goal/set 未返回 objective 字符串");
      }
      console.log(`goal: ${goal.status}（目标模式已启用）`);
    } catch (err) {
      const goalStatus = err.timeout
        ? "set-timeout"
        : err.rpcError
          ? "set-rpc-error"
          : "set-invalid-response";
      saveState({
        threadId,
        turnId: null,
        cwd,
        objective,
        goalStatus,
        goalError: err.message,
        phase: "goal-set-failed",
        kind: "create",
      });
      console.error(
        `[dispatch] thread/goal/set 失败（${err.message}）；已保存 threadId，fail-closed 不发送 turn，也不会新建线程。`
      );
      process.exit(errorExitCode(err));
    }

    const stateFile = saveState({
      threadId,
      cwd,
      objective,
      goalStatus: goal.status,
      kind: "create",
    });
    if (stateFile) console.log(`state: ${stateFile}`);

    let turnId = null;
    try {
      const turnResult = await bridge.call(
        "turn/start",
        {
          threadId,
          input: [{ type: "text", text: prompt }],
          approvalPolicy: "never",
          sandboxPolicy: { type: "dangerFullAccess" },
        },
        120000
      );
      turnId = turnResult && turnResult.turn && turnResult.turn.id;
      console.log(`turnId: ${turnId}`);
      saveState({
        threadId,
        turnId,
        cwd,
        objective,
        goalStatus: goal.status,
        kind: "create",
      });
      console.log(
        "[dispatch] 记住：goal 只有验收齐全后才用 `goal --thread <id> --complete` 置为 complete。"
      );
    } catch (err) {
      console.error(
        `[dispatch] turn/start 未确认成功: ${err.message}\n` +
          `[dispatch] 任务可能已在 ${threadId} 上启动；请用 read --thread ${threadId} 跟踪，不要重发 create。`
      );
      process.exit(err.timeout ? 3 : 2);
    }
  });
}

async function cmdFollowup(opts) {
  const threadId = opts.thread && String(opts.thread);
  if (!threadId) throw new Error("followup 需要 --thread <threadId>");
  const prompt = readPrompt(opts);
  const objective =
    typeof opts.objective === "string" ? opts.objective : undefined;
  await withBridge(async (bridge) => {
    let goal = await fetchGoal(bridge, threadId);
    const needsExplicitGoal = !goal || goal.status !== "active";
    if (needsExplicitGoal && objective === undefined) {
      throw new Error(
        `当前 goal 是 ${
          goal === null ? "null（无目标/已清空）" : goal.status
        }；同一线程追加新任务必须显式提供 --objective，脚本会先将其置为 active，再发送 turn。`
      );
    }

    const result = await readThread(bridge, threadId);
    const thread = result && result.thread;
    const lastTurnBeforeFollowup = lastTurn(thread);
    const turnStatusBeforeFollowup =
      lastTurnBeforeFollowup && lastTurnBeforeFollowup.status;
    if (
      turnStatusBeforeFollowup === "inProgress" ||
      turnStatusBeforeFollowup === "queued"
    ) {
      throw new Error(
        `线程 ${threadId} 的最后 turn 状态是 ${turnStatusBeforeFollowup}（goal=${
          goal === null ? "null" : goal.status
        }）；为避免重复启动，不发送 followup。`
      );
    }

    if (needsExplicitGoal || objective !== undefined) {
      const goalResult = await bridge.call(
        "thread/goal/set",
        { threadId, objective, status: "active" },
        60000
      );
      goal = requireGoalResult(goalResult, "thread/goal/set");
      assertGoalStatus(goal, "active", "thread/goal/set");
    }

    let turnId = null;
    try {
      const turnResult = await bridge.call(
        "turn/start",
        {
          threadId,
          input: [{ type: "text", text: prompt }],
          approvalPolicy: "never",
          sandboxPolicy: { type: "dangerFullAccess" },
        },
        120000
      );
      turnId = turnResult && turnResult.turn && turnResult.turn.id;
      console.log(`threadId: ${threadId}`);
      console.log(`turnId: ${turnId}`);
      saveState({
        threadId,
        turnId,
        objective: goal ? goal.objective : objective,
        goalStatus: goal ? goal.status : undefined,
        kind: "followup",
      });
    } catch (err) {
      saveState({
        threadId,
        turnId: null,
        objective: goal ? goal.objective : objective,
        goalStatus: goal ? goal.status : undefined,
        phase: "turn-start-failed",
        kind: "followup",
      });
      console.error(
        `[dispatch] turn/start 未确认成功: ${err.message}\n` +
          `[dispatch] 请用 read --thread ${threadId} 确认状态，不要重发同一指令。`
      );
      process.exit(errorExitCode(err));
    }
  });
}

async function cmdInterrupt(opts) {
  const threadId = opts.thread && String(opts.thread);
  if (!threadId) throw new Error("interrupt 需要 --thread <threadId>");
  await withBridge(async (bridge) => {
    const result = await readThread(bridge, threadId);
    const thread = result && result.thread;
    const turn = lastTurn(thread);
    const turnId = (typeof opts.turn === "string" && opts.turn) || (turn && turn.id);
    if (!turnId) throw new Error("找不到可中断的 turn（thread 上没有 turns）");
    const status = turn && turn.status;
    if (status && status !== "inProgress" && status !== "queued") {
      console.log(`[dispatch] 最后一轮状态是 ${status}，无需中断。`);
      return;
    }
    await bridge.call("turn/interrupt", { threadId, turnId }, 60000);
    console.log(`threadId: ${threadId}`);
    console.log(`interrupted turnId: ${turnId}`);
  });
}

async function cmdGoal(opts) {
  const threadId = opts.thread && String(opts.thread);
  if (!threadId) throw new Error("goal 需要 --thread <threadId>");

  let status = opts.status ? String(opts.status) : undefined;
  if (opts.complete) status = "complete";
  if (status && !GOAL_STATUSES.includes(status)) {
    throw new Error(`--status 只能是: ${GOAL_STATUSES.join(", ")}`);
  }
  const objective =
    typeof opts.objective === "string" ? opts.objective : undefined;
  if (!status && !objective) {
    await withBridge(async (bridge) => {
      const goal = await fetchGoal(bridge, threadId);
      console.log(`threadId: ${threadId}`);
      printGoal(goal);
    });
    return;
  }
  if (status === "complete") {
    console.log(
      "[dispatch] 只有验收齐全才置 complete；本条命令不校验验收，请确认已人工核对结果。"
    );
  }
  await withBridge(async (bridge) => {
    const params = { threadId, status: status || "active" };
    if (objective !== undefined) params.objective = objective;
    const result = await bridge.call("thread/goal/set", params, 60000);
    const goal = requireGoalResult(result, "thread/goal/set");
    assertGoalStatus(goal, status || "active", "thread/goal/set");
    console.log(`threadId: ${threadId}`);
    printGoal(goal);
  });
}

async function readThread(bridge, threadId) {
  return bridge.call("thread/read", { threadId, includeTurns: true }, 120000);
}

function printSummary(threadId, thread, goal) {
  console.log(`threadId: ${threadId}`);
  console.log(`threadStatus: ${threadStatus(thread)}`);
  printGoal(goal);
  const turn = lastTurn(thread);
  if (!turn) {
    console.log("turns: 0");
    console.log("lastTurnStatus: none");
    console.log("latestProgress: (none)");
    console.log("finalReport: (none)");
    console.log("exceptions: none");
    return;
  }
  const total = Array.isArray(thread.turns) ? thread.turns.length : 0;
  console.log(`turns: ${total} (last ${turn.id})`);
  console.log(`lastTurnStatus: ${turn.status}`);
  const progress = latestProgress(turn);
  console.log("--- latestProgress ---");
  console.log(progress || "(none)");
  const report = finalReport(turn);
  console.log("--- finalReport ---");
  console.log(report || "(none)");
  const exceptions = unresolvedExceptionSummary(turn);
  console.log("--- exceptions ---");
  if (!exceptions.length) {
    console.log("none");
  } else {
    for (const exception of exceptions) console.log(exception);
  }
}

async function cmdRead(opts) {
  const threadId = opts.thread && String(opts.thread);
  if (!threadId) throw new Error("read/status 需要 --thread <threadId>");
  await withBridge(async (bridge) => {
    const [result, goal] = await Promise.all([
      readThread(bridge, threadId),
      fetchGoal(bridge, threadId),
    ]);
    const thread = result && result.thread;
    if (opts.json) {
      console.log(JSON.stringify({ goal, thread }, null, 2));
      return;
    }
    printSummary(threadId, thread, goal);
  });
}

async function cmdWait(opts) {
  const threadId = opts.thread && String(opts.thread);
  if (!threadId) throw new Error("wait 需要 --thread <threadId>");
  const timeout = Number(opts.timeout || 1800) * 1000;
  const interval = Number(opts.interval || 5) * 1000;
  const deadline = Date.now() + timeout;
  await withBridge(async (bridge) => {
    let lastGoalStatus;
    let lastTurnStatus;
    for (;;) {
      const [result, goal] = await Promise.all([
        readThread(bridge, threadId),
        fetchGoal(bridge, threadId),
      ]);
      const thread = result && result.thread;
      const turn = lastTurn(thread);
      const status = turn && turn.status;
      const goalStatus = goal === null ? "null" : goal && goal.status;
      if (goalStatus !== lastGoalStatus || status !== lastTurnStatus) {
        console.log(
          `[dispatch] goalStatus=${goalStatus || "unknown"} lastTurnStatus=${
            status || "none"
          } (${nowIso()})`
        );
        lastGoalStatus = goalStatus;
        lastTurnStatus = status;
      }
      if (status === "failed" || status === "interrupted") {
        printSummary(threadId, thread, goal);
        process.exit(4);
      }
      const turnRunning = status === "inProgress" || status === "queued";
      if (!turnRunning && !(goal && goal.status === "active")) {
        printSummary(threadId, thread, goal);
        return;
      }
      if (Date.now() > deadline) {
        console.error(
          `[dispatch] 等待超时：goalStatus=${goalStatus || "unknown"} lastTurnStatus=${
            status || "none"
          }；线程 ${threadId} 未达到可停止状态。用 read --thread ${threadId} 继续跟踪，不要重发任务。`
        );
        process.exit(3);
      }
      await new Promise((r) => setTimeout(r, interval));
    }
  });
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const cmd = opts._[0];
  try {
    switch (cmd) {
      case "models":
        await cmdModels();
        break;
      case "create":
        await cmdCreate(opts);
        break;
      case "followup":
        await cmdFollowup(opts);
        break;
      case "read":
      case "status":
      case "summary":
        await cmdRead(opts);
        break;
      case "goal":
        await cmdGoal(opts);
        break;
      case "interrupt":
        await cmdInterrupt(opts);
        break;
      case "wait":
        await cmdWait(opts);
        break;
      default:
        console.log(
          "用法: codex_web_dispatch.cjs <models|create|read|status|summary|goal|followup|interrupt|wait> [选项]\n" +
            "  create   --cwd /opt/AI-shujvku/literature-ai --prompt \"...\" | --prompt-file <path> [--objective \"...\"] [--model <id>]\n" +
            "  read     --thread <threadId> [--json]（默认摘要；--json 为完整 JSON）\n" +
            "  summary  --thread <threadId>（低输出摘要）\n" +
            "  goal     --thread <threadId> [--objective \"...\"] [--status active|paused|blocked|complete] [--complete]\n" +
            "  followup --thread <threadId> --prompt \"...\" [--objective \"...\"]\n" +
            "  interrupt --thread <threadId> [--turn <turnId>]   # 中断正在跑的一轮\n" +
            "  wait     --thread <threadId> [--timeout 1800] [--interval 5]"
        );
        process.exit(cmd ? 1 : 0);
    }
  } catch (err) {
    console.error(`[dispatch] ${err.message}`);
    process.exit(err.timeout ? 3 : err.rpcError ? 2 : 1);
  }
}

main();
