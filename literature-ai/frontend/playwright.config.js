const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  webServer: {
    command: 'npm run test:serve',
    url: 'http://127.0.0.1:4173',
    // Never attach the test runner to the production owner gateway on 8000.
    reuseExistingServer: false,
    timeout: 30 * 1000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
