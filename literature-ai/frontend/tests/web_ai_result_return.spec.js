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
  expect(REVIEW_CENTER).toContain('<option value="return_dft">4 回传 DFT JSON</option>');
  expect(REVIEW_CENTER).toContain('回传网页 AI 结果');
  expect(REVIEW_CENTER).toContain('openWebAiReturnDialog("dft")');
  expect(FEATURE_SCOPE).toContain('回传 DFT JSON（" + (nextPaperCode');
  expect(FEATURE_SCOPE).toContain('rows.length !== 1');
  expect(FEATURE_SCOPE).toContain('function focusedSingleMainPaperRow()');
  expect(FEATURE_SCOPE).toContain('return focusedSingleMainPaperRow();');
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
  expect(REVIEW_CENTER).toContain('或把 .json 文件直接拖到这里');
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

test('DFT web AI prompt and validation summary make full target coverage visible', () => {
  expect(REVIEW_CENTER).toContain('coverage_acknowledgement.expected_target_ids');
  expect(REVIEW_CENTER).toContain('manifest.target_dft_result_ids');
  expect(REVIEW_CENTER).toContain('返回 NEEDS_HUMAN');
  expect(FEATURE_SCOPE).toContain('<strong>coverage</strong>');
  expect(FEATURE_SCOPE).toContain('missing_target_ids');
});

test('chart-review local AI instruction only includes unresolved item summaries', () => {
  expect(FEATURE_SCOPE).toContain('只有应用后仍有未解决项，才复制本地 AI 图表复核指令。');
  expect(FEATURE_SCOPE).toContain('if (copyButton) copyButton.disabled = completed;');
  expect(EVIDENCE_INSTRUCTION_SCOPE).toContain('只看下面这些有问题项；没有问题的 figures/tables 不重新判断。');
  expect(EVIDENCE_INSTRUCTION_SCOPE).toContain('用户剪贴板不提供整包 JSON');
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
