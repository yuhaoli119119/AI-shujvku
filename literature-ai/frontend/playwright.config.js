const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  webServer: {
    command: 'npm run test:serve',
    url: 'http://127.0.0.1:8000',
    reuseExistingServer: true,
    timeout: 30 * 1000,
  },
});
