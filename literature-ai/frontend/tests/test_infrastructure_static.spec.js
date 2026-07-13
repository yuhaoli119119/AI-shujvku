const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const frontendRoot = path.resolve(__dirname, '..');

test('Playwright uses an isolated server and never reuses the owner gateway', () => {
  const config = fs.readFileSync(path.join(frontendRoot, 'playwright.config.js'), 'utf8');
  const packageJson = JSON.parse(fs.readFileSync(path.join(frontendRoot, 'package.json'), 'utf8'));

  expect(config).toContain("url: 'http://127.0.0.1:4173'");
  expect(config).toContain('reuseExistingServer: false');
  expect(config).not.toContain("url: 'http://127.0.0.1:8000'");
  expect(packageJson.scripts['test:serve']).toBe('python -m http.server 4173');
});
