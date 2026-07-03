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

test('review center exposes a selected-paper web AI result return entry', () => {
  expect(REVIEW_CENTER).toContain('id="webAiReturnEntryBtn"');
  expect(REVIEW_CENTER).toContain('回传网页 AI 结果');
  expect(FEATURE_SCOPE).toContain('回传网页 AI 结果（" + (nextPaperCode');
  expect(FEATURE_SCOPE).toContain('rows.length !== 1');
});

test('validation uses the selected paper id and copy stays disabled until success', () => {
  expect(FEATURE_SCOPE).toContain('encodeURIComponent(target.paper_id) + "/dft-review-result/validate"');
  expect(REVIEW_CENTER).toContain('id="webAiCopyInstructionBtn" type="button" onclick="copyLocalAiImportInstruction()" disabled');
  expect(FEATURE_SCOPE).toContain('document.getElementById("webAiCopyInstructionBtn").disabled = false;');
  expect(FEATURE_SCOPE).toContain('if (!data || data.valid !== true || !data.import_analysis_request)');
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
  expect(FEATURE_SCOPE).toContain('优先调用当前会话已认证 MCP 的 import_analysis');
  expect(FEATURE_SCOPE).toContain('禁止直接写 PostgreSQL');
  expect(FEATURE_SCOPE).toContain('run_id、候选数量、冲突项、需要人工处理的项目');
  expect(FEATURE_SCOPE).not.toMatch(/Codex|DeepSeek|Anti-Gravity|ChatGPT/);
});
