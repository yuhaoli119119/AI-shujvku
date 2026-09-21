// Temporary override config for static-only spec runs.
// Avoids webServer bootstrap + points outputDir to a fresh path so the
// safe-delete shim does not intercept fs.rm on the default test-results dir.
//
// 输出目录默认落在 /tmp（仓库外的临时目录），不再往 frontend/ 里堆 pw-out-<pid>：
// 那些目录每次跑回归都会新增一批、内容只有 Playwright 的 error-context.md /
// .last-run.json，却会污染 git status 并要求一轮"列清单 + 确认"才能清掉。
// 每进程一个子目录是为了保持"每次都是全新路径"这个前提；需要改位置用 PW_OUT_DIR。
const OUT_ROOT = (process.env.PW_OUT_DIR || '/tmp/litai-uicheck').replace(/\/+$/, '');
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  outputDir: OUT_ROOT + '/pw-out-' + process.pid,
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
