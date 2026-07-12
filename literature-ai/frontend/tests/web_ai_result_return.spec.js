const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const REVIEW_CENTER = fs.readFileSync(
  path.resolve(__dirname, '../pages/review_center/index.html'),
  'utf8'
);
const FEATURE_SCOPE = REVIEW_CENTER.slice(
  REVIEW_CENTER.indexOf('// WEB_AI_RETURN_FEATURE_START'),
  REVIEW_CENTER.indexOf('// WEB_AI_RETURN_FEATURE_END')
);
const FILE_DROP_SCOPE = FEATURE_SCOPE.slice(
  FEATURE_SCOPE.indexOf('async function handleWebAiFileDrop'),
  FEATURE_SCOPE.indexOf('function clearWebAiReturnInput')
);
const BUILD_INSTRUCTION_START = FEATURE_SCOPE.indexOf('function buildLocalAiImportInstruction');
const EVIDENCE_INSTRUCTION_SCOPE = FEATURE_SCOPE.slice(
  FEATURE_SCOPE.indexOf('if (webAiReturnState.mode === "evidence")', BUILD_INSTRUCTION_START),
  FEATURE_SCOPE.indexOf('if (webAiReturnState.mode !== "dft")')
);

test('review center exposes a selected-paper web AI result return entry', () => {
  expect(REVIEW_CENTER).toContain('id="returnDftWorkflowOption" value="return_dft"');
  expect(REVIEW_CENTER).toContain('回传网页 AI 结果');
  expect(REVIEW_CENTER).toContain('openWebAiReturnDialog("dft")');
  expect(FEATURE_SCOPE).toContain('回传 DFT JSON（" + (nextPaperCode');
  expect(FEATURE_SCOPE).toContain('rows.length !== 1');
  expect(FEATURE_SCOPE).toContain('function focusedSingleMainPaperRow()');
  expect(FEATURE_SCOPE).toContain('rows.length !== 1 ? focusedSingleMainPaperRow() : null');
});

test('validation uses the selected paper id and copy stays disabled until success', () => {
  expect(FEATURE_SCOPE).toContain('encodeURIComponent(target.paper_id) + "/dft-review-result/validate"');
  expect(REVIEW_CENTER).toContain('id="webAiCopyInstructionBtn" type="button" onclick="copyLocalAiImportInstruction()" disabled');
  expect(FEATURE_SCOPE).toContain('if (copyButton) copyButton.disabled = false;');
  expect(FEATURE_SCOPE).toContain('Boolean(data && data.valid === true && data.import_analysis_request)');
});

test('pasted web AI JSON can be extracted from common chat wrappers', () => {
  expect(FEATURE_SCOPE).toContain('function parseWebAiJsonText(rawText)');
  expect(FEATURE_SCOPE).toContain('function extractFirstJsonPayload(text)');
  expect(FEATURE_SCOPE).toContain('text.match(/```(?:json)?');
  expect(FEATURE_SCOPE).toContain('parseWebAiJsonText(rawText)');
  expect(FEATURE_SCOPE).not.toContain('JSON.parse(rawText)');
});

test('failed validation never imports and temporary review data is not persisted', () => {
  expect(FEATURE_SCOPE).not.toContain('/api/external-analysis/import');
  expect(FEATURE_SCOPE).not.toContain('import_analysis(');
  expect(FEATURE_SCOPE).not.toContain('localStorage');
  expect(FEATURE_SCOPE).not.toContain('sessionStorage');
  expect(FEATURE_SCOPE).not.toContain('indexedDB');
  expect(FEATURE_SCOPE).not.toContain('IndexedDB');
  expect(FEATURE_SCOPE).toContain('valid=false，立即停止，不调用 import_analysis');
});

test('a dropped JSON file is read only into current-page memory', () => {
  expect(REVIEW_CENTER).toContain('ondrop="handleWebAiFileDrop(event)"');
  expect(REVIEW_CENTER).toContain('优先把网页 AI 回复的 .json 文件拖到这里');
  expect(FILE_DROP_SCOPE).toContain('const rawText = await file.text();');
  expect(FILE_DROP_SCOPE).toContain('textarea.value = rawText;');
  expect(FILE_DROP_SCOPE).toContain('JSON 文件不能超过 5 MB');
  expect(FILE_DROP_SCOPE).not.toContain('fetch(');
  expect(FILE_DROP_SCOPE).not.toContain('localStorage');
  expect(FILE_DROP_SCOPE).not.toContain('sessionStorage');
  expect(FILE_DROP_SCOPE).not.toContain('indexedDB');
});

test('switching papers clears pasted content, validation state, and copy permission', () => {
  expect(FEATURE_SCOPE).toContain('webAiReturnState.paperId !== nextPaperId');
  expect(FEATURE_SCOPE).toContain('clearWebAiReturnTransientState("已切换文献');
  expect(FEATURE_SCOPE).toContain('closeWebAiReturnDialog();');
  expect(FEATURE_SCOPE).toContain('textarea.value = "";');
  expect(FEATURE_SCOPE).toContain('copyButton.disabled = true;');
});

test('copied instruction is product-neutral and requires fresh validation plus authenticated MCP', () => {
  expect(FEATURE_SCOPE).toContain('重新 POST 到 /api/papers/');
  expect(FEATURE_SCOPE).toContain('只使用这次新返回的 import_analysis_request');
  expect(FEATURE_SCOPE).toContain('逐条调用 get_codex_item');
  expect(FEATURE_SCOPE).toContain('read_paper_page 核对原 PDF');
  expect(FEATURE_SCOPE).toContain('local_ai_verification={verified_against_pdf:true');
  expect(FEATURE_SCOPE).toContain('stage_status 为 completed/not_required');
  expect(FEATURE_SCOPE).toContain('completed_snapshot_fingerprint');
  expect(FEATURE_SCOPE).toContain('优先调用当前会话已认证 MCP 的 import_analysis');
  expect(FEATURE_SCOPE).toContain('禁止直接写 PostgreSQL');
  expect(FEATURE_SCOPE).toContain('dft_readback.object_versions');
  expect(FEATURE_SCOPE).not.toMatch(/Codex|DeepSeek|Anti-Gravity|ChatGPT/);
});

test('DFT web AI prompt requires one-pass existing review and missing-data discovery', () => {
  expect(REVIEW_CENTER).toContain('START_HERE.md');
  expect(REVIEW_CENTER).toContain('WEB_AI_FILL_THIS.json');
  expect(REVIEW_CENTER).toContain('OUTPUT_RULES.json');
  expect(REVIEW_CENTER).toContain('<paper_code>_web_ai_result.json');
  expect(REVIEW_CENTER).toContain('以 JSON 文件附件回复');
  expect(REVIEW_CENTER).toContain('不要把长 JSON 粘贴在聊天正文中');
  expect(REVIEW_CENTER).toContain('target_id=\\\"new\\\" 当且仅当 decision=\\\"new_candidate\\\"');
  expect(REVIEW_CENTER).toContain('coverage_acknowledgement.expected_target_ids');
  expect(REVIEW_CENTER).toContain('manifest.target_dft_result_ids');
  expect(REVIEW_CENTER).toContain('返回 NEEDS_HUMAN');
  expect(FEATURE_SCOPE).toContain('<strong>coverage</strong>');
  expect(FEATURE_SCOPE).toContain('missing_target_ids');
  expect(REVIEW_CENTER).toContain('existing_terminal_context');
  expect(REVIEW_CENTER).toContain('unreviewed_supporting_context');
  expect(REVIEW_CENTER).toContain('DFT 全量核验（已有+查漏）');
  expect(REVIEW_CENTER).toContain('missing_data_search_complete');
  expect(REVIEW_CENTER).toContain('扫描全部 eligible_for_auto_apply=true');
  expect(REVIEW_CENTER).toContain('/dft-review-state');
});

test('DFT validation failure can copy a constrained repair prompt with the original JSON', () => {
  expect(FEATURE_SCOPE).toContain('lastValidationIssues');
  expect(FEATURE_SCOPE).toContain('复制 JSON 修复提示');
  expect(FEATURE_SCOPE).toContain('function copyWebAiJsonRepairPrompt()');
  expect(FEATURE_SCOPE).toContain('只修复下面 JSON 的格式和契约错误');
  expect(FEATURE_SCOPE).toContain('待修复的原始 JSON：');
  expect(FEATURE_SCOPE).toContain('rawText');
  expect(FEATURE_SCOPE).toContain('_web_ai_result.json');
  expect(FEATURE_SCOPE).toContain('不要把 JSON 粘贴到聊天正文');
});

test('DFT evidence mismatch routes to local semantic verification instead of repeated web repair', () => {
  expect(FEATURE_SCOPE).toContain('unrelated_evidence_id');
  expect(FEATURE_SCOPE).toContain('function copyLocalAiDftEvidenceVerificationInstruction()');
  expect(FEATURE_SCOPE).toContain('这是证据语义不匹配，不是 JSON 格式错误。');
  expect(FEATURE_SCOPE).toContain('不要再把同一份 JSON 反复交回网页 AI');
});

test('chart validation failure repairs the web AI JSON before offering local AI work', () => {
  expect(FEATURE_SCOPE).toContain('function copyWebAiEvidenceJsonRepairPrompt()');
  expect(FEATURE_SCOPE).toContain('复制图表 JSON 修复提示');
  expect(FEATURE_SCOPE).toContain('先交回网页 AI修复，不需要发送给本地 AI。');
  expect(FEATURE_SCOPE).toContain('每一条 figure_actions/table_actions 都必须有一个或多个真实 evidence_ids');
  expect(FEATURE_SCOPE).toContain('删除该不受支持的 CREATE 动作');
  expect(FEATURE_SCOPE).toContain('if (copyButton) copyButton.disabled = true;');
});

test('chart-review local AI instruction requires every in-scope figure after web apply', () => {
  expect(FEATURE_SCOPE).toContain('无论网页 AI 是否报告问题，都必须执行本地 AI 全量图片复核');
  expect(FEATURE_SCOPE).toContain('if (copyButton) copyButton.disabled = false;');
  expect(EVIDENCE_INSTRUCTION_SCOPE).toContain('必须重新核验 get_chart_review_task 返回的每一张范围内图片');
  expect(EVIDENCE_INSTRUCTION_SCOPE).toContain('不得只处理 unresolved_actions');
  expect(EVIDENCE_INSTRUCTION_SCOPE).toContain('每一个非 NEEDS_HUMAN 的 figure_action 都必须附 local_ai_verification');
  expect(EVIDENCE_INSTRUCTION_SCOPE).toContain('unresolvedSummary');
  expect(EVIDENCE_INSTRUCTION_SCOPE).not.toContain('BEGIN WEB AI CHART REVIEW JSON');
  expect(EVIDENCE_INSTRUCTION_SCOPE).not.toContain('rawText');
});

test('bundle export uses readable API errors and the real workflow selector state', () => {
  expect(REVIEW_CENTER).toContain('function apiErrorMessageFromPayload(data, status)');
  expect(REVIEW_CENTER).toContain('figure_table_review_not_completed');
  expect(REVIEW_CENTER).toContain('stage_status=');
  expect(REVIEW_CENTER).toContain('document.getElementById("webAiWorkflowSelect")');
  expect(REVIEW_CENTER).not.toContain('webAiBundlePromptBtn');
});
