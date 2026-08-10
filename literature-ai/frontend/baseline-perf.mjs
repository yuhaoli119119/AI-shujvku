// Phase 0 基线测量脚本：对 20 个前端页面采集传输量 / DCL / 长任务 / 接口频率
// 用法：node baseline-perf.mjs   （需 backend 在 localhost:8000 运行）
import { chromium } from 'playwright';
import fs from 'node:fs';

const BASE = 'http://localhost:8000';
const OBSERVE_MS = 15000; // 首屏加载后继续观察 15s，覆盖 12s 轮询周期
const PAGES = [
  'ai_writer', 'content_knowledge', 'dashboard', 'dft_audit_center',
  'dft_database', 'dft_ml_dataset', 'external_analysis_workbench',
  'extraction_workflow', 'ingestion', 'literature_library',
  'literature_screening', 'mechanism_knowledge', 'paper_detail',
  'review_center', 'settings', 'share', 'theme-preview', 'visuals',
  'writing_assistant', 'writing_cards',
];

const results = [];
const browser = await chromium.launch();

for (const name of PAGES) {
  const url = `${BASE}/pages/${name}/index.html`;
  const ctx = await browser.newContext();
  const page = await ctx.newPage();

  const stat = {
    page: name, url, ok: true, error: '',
    requests: 0, apiRequestsLoad: 0, apiRequestsObserve: 0,
    transferBytes: 0, jsCount: 0, cssCount: 0,
    dcl: 0, load: 0, longTasks: 0, longTaskTotalMs: 0, jsErrors: [],
  };

  await page.addInitScript(() => {
    window.__longTasks = [];
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__longTasks.push(e.duration);
      }).observe({ entryTypes: ['longtask'] });
    } catch (_) { /* longtask 不可用时跳过 */ }
  });

  let loaded = false;
  page.on('response', async (res) => {
    stat.requests++;
    const u = res.url();
    if (u.includes('/api/')) (loaded ? stat.apiRequestsObserve++ : stat.apiRequestsLoad++);
    if (/\.js(\?|$)/.test(u)) stat.jsCount++;
    if (/\.css(\?|$)/.test(u)) stat.cssCount++;
    try { stat.transferBytes += (await res.body()).length; } catch (_) {}
  });
  page.on('pageerror', (err) => stat.jsErrors.push(String(err).slice(0, 200)));

  try {
    const t0 = Date.now();
    await page.goto(url, { waitUntil: 'load', timeout: 30000 });
    loaded = true;
    const timing = await page.evaluate(() => {
      const n = performance.getEntriesByType('navigation')[0] || {};
      return { dcl: Math.round(n.domContentLoadedEventEnd || 0), load: Math.round(n.loadEventEnd || 0) };
    });
    stat.dcl = timing.dcl; stat.load = timing.load;
    await page.waitForTimeout(OBSERVE_MS);
    const lt = await page.evaluate(() => window.__longTasks || []);
    stat.longTasks = lt.length;
    stat.longTaskTotalMs = Math.round(lt.reduce((a, b) => a + b, 0));
    stat.totalMs = Date.now() - t0;
  } catch (e) {
    stat.ok = false; stat.error = String(e).slice(0, 200);
  }
  results.push(stat);
  console.log(`[done] ${name}: DCL=${stat.dcl}ms transfer=${(stat.transferBytes / 1024).toFixed(0)}KB req=${stat.requests} api(load/observe)=${stat.apiRequestsLoad}/${stat.apiRequestsObserve} longTasks=${stat.longTasks} jsErr=${stat.jsErrors.length}`);
  await ctx.close();
}

await browser.close();
fs.writeFileSync('baseline-results.json', JSON.stringify(results, null, 2));
console.log('\nwritten: baseline-results.json');
