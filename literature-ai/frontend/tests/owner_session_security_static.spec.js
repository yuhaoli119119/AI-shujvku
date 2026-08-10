const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

const frontendRoot = path.join(__dirname, '..');

function read(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8');
}

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const resolved = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return entry.name === 'node_modules' ? [] : sourceFiles(resolved);
    }
    return /\.(?:html|js)$/.test(entry.name) ? [resolved] : [];
  });
}

test('owner unlock uses an HttpOnly server session contract without browser token persistence', async () => {
  const settings = read('pages/settings/index.html');
  const allSource = sourceFiles(frontendRoot)
    .filter((file) => !file.includes(`${path.sep}tests${path.sep}`))
    .map((file) => fs.readFileSync(file, 'utf8'))
    .join('\n');

  expect(settings).toContain('/api/settings/owner-session');
  expect(settings).toContain('credentials = "same-origin"');
  expect(settings).toContain('401 Unauthorized: Owner Session Required');
  expect(settings).toContain('403 Forbidden: Invalid Owner Credential');
  expect(allSource).not.toContain('litai-settings-token');
  expect(allSource).not.toContain('X-Settings-Token');
  expect(allSource).not.toMatch(/(?:localStorage|sessionStorage)\.setItem\([^\n]*(?:token|credential)/i);
});

test('review center posts human decisions through credentialed same-origin fetch paths', async () => {
  const reviewCenter = read('pages/review_center/page.js');

  expect(reviewCenter).toContain('/api/workbench/review-conflicts/manual-decision');
  expect(reviewCenter).toContain('/human-confirm');
  expect(reviewCenter).not.toContain('X-Settings-Token');
});

test('owner gateway never injects or hard-codes Owner credentials', async () => {
  const template = fs.readFileSync(
    path.join(frontendRoot, '..', 'deploy', 'nginx', 'owner.conf.template'),
    'utf8',
  );

  expect(template).not.toContain('X-LitAI-Owner-Token');
  expect(template).not.toContain('LITAI_OWNER_API_TOKEN');
  expect(template).not.toMatch(/proxy_set_header\s+Authorization/i);
  expect(template).toContain('proxy_pass http://literature_ai_backend');
});
