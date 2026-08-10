// Phase 1 加固基线测量（v3.1 口径修正版）
// 修正：页面壳与完整数据链分开测量，各自 5 次相同停止条件；报告 n/median/max（5 样本不报 p95）
// 口径：CDP Network.loadingFinished.encodedDataLength 报告的 encoded bytes
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = 'http://localhost:8000';
const PAPER_ID = '4fec6e12-fe27-4078-bc43-46444bca9bec';
const RUNS = 5;
const DEFAULT_OUTPUT = fileURLToPath(new URL('./baseline-v3-content-after.json', import.meta.url));
const OUTPUT = process.env.BASELINE_OUTPUT ? path.resolve(process.env.BASELINE_OUTPUT) : DEFAULT_OUTPUT;
const startedAt = new Date().toISOString();
const LAN_PAGES = new Set(['external_analysis_workbench', 'ingestion', 'literature_library', 'review_center', 'dft_database', 'paper_detail']);

const PAGES = [
  'ai_writer', 'content_knowledge', 'dashboard', 'dft_audit_center',
  'dft_database', 'dft_ml_dataset', 'external_analysis_workbench',
  'extraction_workflow', 'ingestion', 'literature_library',
  'literature_screening', 'mechanism_knowledge', 'paper_detail',
  'review_center', 'settings', 'share', 'theme-preview', 'visuals',
  'writing_assistant', 'writing_cards',
  `paper_detail/index.html?paper_id=${PAPER_ID}`,
  `external_analysis_workbench/index.html?paper_id=${PAPER_ID}`,
];
// 需要完整数据链测量的页面（等待业务就绪标志）
const CHAIN_PAGES = {
  [`external_analysis_workbench/index.html?paper_id=${PAPER_ID}`]: () => {
    const f = document.getElementById('schemaForm');
    return f && f.innerHTML.length > 200 && !f.innerHTML.includes('正在');
  },
};

function urlOf(p) { return p.includes('/') ? `${BASE}/pages/${p}` : `${BASE}/pages/${p}/index.html`; }
function median(a) { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
const r1 = (n) => Math.round(n * 10) / 10;

const browser = await chromium.launch();
const all = [];

async function measureOnce(pageUrl, { lan = false, chainFn = null } = {}) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Network.enable');
  if (lan) await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 20, downloadThroughput: 10 * 1024 * 1024 / 8, uploadThroughput: 5 * 1024 * 1024 / 8 });

  const m = { encodedBytes: 0, requests: 0, status4xx5xx: 0, requestFailed: 0, consoleErrors: 0, dcl: 0, load: 0, fcp: 0, lcp: 0, longTaskCount: 0, longTaskMax: 0, chainReadyMs: 0 };
  cdp.on('Network.loadingFinished', (e) => { m.encodedBytes += e.encodedDataLength || 0; });
  cdp.on('Network.responseReceived', (e) => {
    m.requests++;
    if (e.response && e.response.status >= 400) m.status4xx5xx++;
  });
  page.on('requestfailed', () => m.requestFailed++);
  page.on('console', (msg) => { if (msg.type() === 'error') m.consoleErrors++; });

  await page.addInitScript(() => {
    window.__perf = { fcp: 0, lcp: 0, lt: [] };
    try {
      new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.name === 'first-contentful-paint') window.__perf.fcp = e.startTime; }).observe({ entryTypes: ['paint'] });
      new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__perf.lcp = e.startTime; }).observe({ entryTypes: ['largest-contentful-paint'] });
      new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__perf.lt.push(e.duration); }).observe({ entryTypes: ['longtask'] });
    } catch (_) {}
  });

  const t0 = Date.now();
  try {
    await page.goto(pageUrl, { waitUntil: 'load', timeout: 45000 });
    if (chainFn) {
      // 数据链口径：等待业务就绪标志，记录完整链墙钟与字节
      await page.waitForFunction(chainFn, { timeout: 45000 }).catch(() => { m.chainTimeout = true; });
      m.chainReadyMs = Date.now() - t0;
    } else {
      // 页面壳口径：固定 800ms 收尾，所有运行相同停止条件
      await page.waitForTimeout(800);
    }
    const t = await page.evaluate(() => {
      const n = performance.getEntriesByType('navigation')[0] || {};
      return { dcl: n.domContentLoadedEventEnd || 0, load: n.loadEventEnd || 0, fcp: window.__perf.fcp, lcp: window.__perf.lcp, lt: window.__perf.lt };
    });
    m.dcl = r1(t.dcl); m.load = r1(t.load); m.fcp = r1(t.fcp); m.lcp = r1(t.lcp);
    m.longTaskCount = t.lt.length; m.longTaskMax = r1(t.lt.reduce((a, b) => Math.max(a, b), 0));
  } catch (e) {
    m.error = String(e).slice(0, 150);
  }
  await ctx.close();
  return m;
}

function aggregate(runs, key, scale = 1) {
  const ok = runs.filter((r) => !r.error);
  const vals = ok.map((r) => r[key] / scale);
  if (!vals.length) return { n: 0, med: null, max: null };
  return { n: ok.length, med: r1(median(vals)), max: r1(Math.max(...vals)) };
}

function rawValues(runs, key, scale = 1) {
  return runs.map((r) => r.error ? null : r1(r[key] / scale));
}

for (const p of PAGES) {
  const url = urlOf(p);
  const shellRuns = [];
  for (let i = 0; i < RUNS; i++) shellRuns.push(await measureOnce(url));
  const row = {
    page: p,
    shell_n: aggregate(shellRuns, 'dcl').n,
    dcl_med: aggregate(shellRuns, 'dcl').med, dcl_max: aggregate(shellRuns, 'dcl').max,
    lcp_med: aggregate(shellRuns, 'lcp').med, lcp_max: aggregate(shellRuns, 'lcp').max,
    shellKB_med: aggregate(shellRuns, 'encodedBytes', 1024).med, shellKB_max: aggregate(shellRuns, 'encodedBytes', 1024).max,
    req_med: aggregate(shellRuns, 'requests').med,
    ltMax: r1(Math.max(0, ...shellRuns.filter((r) => !r.error).map((r) => r.longTaskMax))),
    s4xx5xx: shellRuns.reduce((a, r) => a + r.status4xx5xx, 0),
    reqFail: shellRuns.reduce((a, r) => a + r.requestFailed, 0),
    consoleErr: shellRuns.reduce((a, r) => a + r.consoleErrors, 0),
    shell_raw: {
      dcl_ms: rawValues(shellRuns, 'dcl'),
      lcp_ms: rawValues(shellRuns, 'lcp'),
      encoded_kb: rawValues(shellRuns, 'encodedBytes', 1024),
    },
  };
  if (LAN_PAGES.has(p.split('/')[0])) {
    const lanRuns = [];
    for (let i = 0; i < 3; i++) lanRuns.push(await measureOnce(url, { lan: true }));
    row.lan_dcl_med = aggregate(lanRuns, 'dcl').med;
    row.lan_lcp_med = aggregate(lanRuns, 'lcp').med;
  }
  if (CHAIN_PAGES[p]) {
    const chainRuns = [];
    for (let i = 0; i < RUNS; i++) chainRuns.push(await measureOnce(url, { chainFn: CHAIN_PAGES[p] }));
    row.chain_n = aggregate(chainRuns, 'chainReadyMs').n;
    row.chainReady_med = aggregate(chainRuns, 'chainReadyMs').med;
    row.chainReady_max = aggregate(chainRuns, 'chainReadyMs').max;
    row.chainKB_med = aggregate(chainRuns, 'encodedBytes', 1024).med;
    row.chainKB_max = aggregate(chainRuns, 'encodedBytes', 1024).max;
    row.chainTimeouts = chainRuns.filter((r) => r.chainTimeout).length;
    row.chain_raw = {
      ready_ms: rawValues(chainRuns, 'chainReadyMs'),
      encoded_kb: rawValues(chainRuns, 'encodedBytes', 1024),
      timed_out: chainRuns.map((r) => Boolean(r.chainTimeout)),
    };
  }
  all.push(row);
  console.log(`[done] ${p} shell: DCL=${row.dcl_med}/${row.dcl_max}ms KB=${row.shellKB_med}/${row.shellKB_max} 4xx5xx=${row.s4xx5xx} cerr=${row.consoleErr}` +
    (row.chainReady_med ? ` | chain: ready=${row.chainReady_med}/${row.chainReady_max}ms KB=${row.chainKB_med}/${row.chainKB_max} timeouts=${row.chainTimeouts}` : '') +
    (row.lan_dcl_med ? ` | LAN DCL=${row.lan_dcl_med}` : ''));
}

const output = {
  schema_version: 3,
  started_at: startedAt,
  finished_at: new Date().toISOString(),
  base_url: BASE,
  paper_id: PAPER_ID,
  paper_detail_mode: 'content',
  runs_per_measurement: RUNS,
  encoded_bytes_definition: 'CDP Network.loadingFinished.encodedDataLength reported encoded bytes',
  stop_conditions: {
    shell: "page load event, then fixed 800 ms settle",
    full_chain: "page load event, then schemaForm business-ready predicate",
  },
  pages: all,
};
fs.writeFileSync(OUTPUT, JSON.stringify(output, null, 2));
console.log(`\nwritten: ${OUTPUT}`);
await browser.close();
