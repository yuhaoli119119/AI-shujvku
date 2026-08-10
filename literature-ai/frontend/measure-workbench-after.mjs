// Phase 2 效果验证：并行化后 workbench 数据链路墙钟时间对比
import { chromium } from 'playwright';

const URL = 'http://localhost:8000/pages/external_analysis_workbench/index.html?paper_id=4fec6e12-fe27-4078-bc43-46444bca9bec';
const browser = await chromium.launch();

for (let i = 0; i < 3; i++) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const apiDone = {};
  const t0 = Date.now();
  page.on('response', (res) => {
    const u = res.url();
    if (u.includes('/api/')) apiDone[u.replace(/.*\/api\//, '/api/').slice(0, 80)] = Date.now() - t0;
  });
  await page.goto(URL, { waitUntil: 'load', timeout: 30000 });
  // 等待 schema 表单真实渲染（数据链全部就绪的标志）
  await page.waitForFunction(() => {
    const f = document.getElementById('schemaForm');
    return f && f.innerHTML.length > 200 && !f.innerHTML.includes('正在');
  }, { timeout: 30000 }).catch(() => {});
  const ready = Date.now() - t0;
  const lastApi = Math.max(0, ...Object.values(apiDone));
  console.log(`run${i + 1}: 表单就绪=${ready}ms 最后一个API完成=${lastApi}ms API数=${Object.keys(apiDone).length}`);
  Object.entries(apiDone).forEach(([u, t]) => console.log(`   ${String(t).padStart(6)}ms  ${u}`));
  await ctx.close();
}
await browser.close();
