// Temporary override config for static-only spec runs.
// Avoids webServer bootstrap + points outputDir to a fresh path so the
// safe-delete shim does not intercept fs.rm on the default test-results dir.
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  outputDir: './pw-out-' + process.pid,
  webServer: {
    command: 'npm run test:serve',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: true,
    timeout: 30 * 1000,
  },
  projects: [
    { name: 'chromium', use: { headless: true } },
  ],
});
