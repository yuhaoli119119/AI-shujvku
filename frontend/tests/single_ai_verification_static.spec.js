const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

const frontendRoot = path.join(__dirname, '..');

function read(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8');
}

test('content knowledge exposes single-AI outcomes and audit evidence', async () => {
  const page = read('pages/content_knowledge/index.html');
  const detail = read('pages/content_knowledge/render-detail.js');

  for (const status of ['ai_verified', 'auto_repaired', 'auto_rejected', 'needs_human']) {
    expect(page).toContain(status);
  }
  expect(page).toContain('单一获授权 AI');
  expect(detail).toContain('AI 自动验收审计');
  expect(detail).toContain("verification.confidence");
  expect(detail).toContain("verification.page");
  expect(detail).toContain("verification.evidence_checks");
  expect(detail).toContain("verification.locator_checks");
  expect(detail).toContain("verification.source_identity");
});

test('review center presents human work as an exception queue', async () => {
  const page = read('pages/review_center/index.html');

  expect(page).toContain('AI 自动验收异常队列');
  expect(page).toContain('人工只处理');
  expect(page).not.toContain('双 AI');
});
