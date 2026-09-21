#!/usr/bin/env node
/* =====================================================================
   LitAI 工作台响应式审计（并行版）
   ---------------------------------------------------------------------
   用法（在服务器上，容器运行中）：
     node frontend/tools/audit-responsive.js            # 17 页 × 默认 2 档
     PAGES=a,b,c SIZES=390x844,1024x768 BASE=http://172.18.0.6:8000 \
       OUT=/tmp/audit.json WORKERS=6 node frontend/tools/audit-responsive.js

   环境变量：
     BASE      被测站点的安全源/只读源（默认 http://172.18.0.6:8000，即 backend 容器）
     PAGES     逗号分隔的页面目录名
     SIZES     逗号分隔的 WxH（顺序即输出顺序）
     OUT       结果 JSON 路径（逐条记录可与历史基线逐条 diff）
     WORKERS   并发页面数（默认 min(8, CPU)；1 = 串行，等价旧 audit.js）
     SETTLE    每个页面最短静置毫秒（默认 6000；改它会改变量到的高度，基线对比必须一致）
     STABLE    额外等待"高度稳定"毫秒（默认 2500；设 0 关闭，即旧 audit.js 行为）
     STABLE_MAX 高度稳定等待上限毫秒（默认 20000）
     EXTRA     URL 追加参数（如 ?paper_id=...&from=...）
     SHOTS     截图输出目录（可选，输出 <page>-<w>.png 全页截图）
     HERE      额外前缀，用于把 --from 页面带进 URL（默认 ''）

   设计要点（与旧脚本一致，便于和历史基线逐条对比）：
     * 每个 (页面 × 尺寸) 用**全新 context**，避免 localStorage 串页影响高度；
     * 溢出判定只算"真正可见且伸出视口"的元素（祖先 overflow:hidden 裁掉的不算）；
     * 输出记录字段：size/page/sw/iw/sh/scrolled/hOverflow/bodyOverflowHidden/bad/errors/ok/hasTopnav。
   退出码：0 = 没有溢出/报错；1 = 存在溢出或 JS/console 报错；2 = 脚本自身错误。
   ===================================================================== */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

function loadPlaywright() {
  const candidates = [
    path.resolve(__dirname, '..', 'node_modules', 'playwright-core'),
    path.resolve(__dirname, '..', 'node_modules', '@playwright', 'test'),
    '/opt/AI-shujvku/literature-ai/frontend/node_modules/playwright-core',
  ];
  for (const c of candidates) {
    try { const m = require(c); if (m && m.chromium) return m; } catch (_) { /* 下一个 */ }
  }
  throw new Error('找不到 playwright-core：请先 cd frontend && npm install');
}

const DEFAULT_PAGES = 'ai_writer,content_knowledge,dashboard,dft_audit_center,dft_database,external_analysis_workbench,extraction_workflow,ingestion,literature_library,literature_screening,mechanism_knowledge,paper_detail,review_center,settings,visuals,readonly,share';
const DEFAULT_SIZES = '390x844,768x1024,820x1180,1024x768,1280x900,1440x900';

const PAGES = (process.env.PAGES || DEFAULT_PAGES).split(',').map(s => s.trim()).filter(Boolean);
const SIZES = (process.env.SIZES || DEFAULT_SIZES).split(',').map(s => s.trim()).filter(Boolean);
const BASE = (process.env.BASE || 'http://172.18.0.6:8000').replace(/\/$/, '');
const OUT = process.env.OUT || '';
const SHOTS = process.env.SHOTS || '';
const EXTRA = process.env.EXTRA || '';
const SETTLE = Number(process.env.SETTLE || 6000);
const STABLE = Number(process.env.STABLE === undefined ? 2500 : process.env.STABLE);
const STABLE_MAX = Number(process.env.STABLE_MAX || 20000);
const WORKERS = Math.max(1, Number(process.env.WORKERS || Math.min(8, os.cpus().length)));

/* 与旧 audit.js 完全相同的量法：早退靠 ok/bad 判定不变，只多一层"高度稳定"等待。 */
const measure = () => {
  const w = document.documentElement.clientWidth;
  const before = window.scrollY;
  window.scrollTo(0, 600);
  const scrolled = window.scrollY;
  window.scrollTo(0, before);
  const bad = [];
  const visibleRight = el => {
    const r0 = el.getBoundingClientRect();
    let left = r0.left, right = r0.right, top = r0.top, bottom = r0.bottom;
    let anc = el.parentElement;
    while (anc && anc !== document.documentElement) {
      const acs = getComputedStyle(anc);
      if (acs.overflowX !== 'visible' || acs.overflowY !== 'visible') {
        if (acs.overflowX === 'auto' || acs.overflowX === 'scroll') return 'scrollable';
        const ar = anc.getBoundingClientRect();
        left = Math.max(left, ar.left); right = Math.min(right, ar.right);
        top = Math.max(top, ar.top); bottom = Math.min(bottom, ar.bottom);
        if (right <= left || bottom <= top) return null;
      }
      anc = anc.parentElement;
    }
    return right;
  };
  const walk = el => {
    for (const c of el.children) {
      const cs = getComputedStyle(c);
      const r = c.getBoundingClientRect();
      if (r.width > 0 && r.right > w + 1 && cs.overflowX !== 'auto' && cs.overflowX !== 'scroll') {
        const vr = visibleRight(c);
        if (vr === null || vr === 'scrollable') { /* 裁掉/可滚动，不算 */ }
        else if (vr > w + 1) bad.push(c.tagName + '.' + String(c.className || '').slice(0, 30) + '@' + Math.round(vr));
      }
      walk(c);
    }
  };
  walk(document.body);
  return {
    sw: document.documentElement.scrollWidth,
    iw: w,
    sh: document.documentElement.scrollHeight,
    scrolled,
    hOverflow: document.documentElement.scrollWidth > w + 2,
    bodyOverflowHidden: getComputedStyle(document.body).overflowY === 'hidden',
    bad,
  };
};

async function measureOne(browser, { size, page }) {
  const [w, h] = size.split('x').map(Number);
  const started = Date.now();
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  const p = await ctx.newPage();
  const errors = [];
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 140)); });
  p.on('pageerror', e => errors.push('PAGEERROR ' + String(e.message).slice(0, 140)));
  let rec = { size, page, ok: false };
  try {
    await p.goto(`${BASE}/pages/${page}/index.html${EXTRA}`, { waitUntil: 'load', timeout: 30000 });
    await p.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
    await p.waitForTimeout(SETTLE);
    if (STABLE > 0) {
      /* 高度稳定：连续 STABLE 毫秒 scrollHeight 不变即认为渲染完成，避免"量到半张页面"。 */
      const deadline = Date.now() + STABLE_MAX;
      let last = await p.evaluate(() => document.documentElement.scrollHeight);
      let stableSince = Date.now();
      while (Date.now() < deadline) {
        await p.waitForTimeout(250);
        const now = await p.evaluate(() => document.documentElement.scrollHeight);
        if (now !== last) { last = now; stableSince = Date.now(); }
        else if (Date.now() - stableSince >= STABLE) break;
      }
    }
    rec = await p.evaluate(measure);
    rec.ok = true;
    rec.errors = errors;
    rec.hasTopnav = await p.locator('#topnav-mount .topnav').count() > 0;
    if (SHOTS) {
      fs.mkdirSync(SHOTS, { recursive: true });
      await p.screenshot({ path: `${SHOTS}/${page}-${w}.png`, fullPage: true }).catch(() => {});
    }
  } catch (e) {
    rec.error = String(e.message).slice(0, 200);
  }
  await p.close();
  await ctx.close();
  rec.page = page;
  rec.size = size;
  rec.ms = Date.now() - started;
  return rec;
}

(async () => {
  const { chromium } = loadPlaywright();
  const jobs = [];
  for (const size of SIZES) for (const page of PAGES) jobs.push({ size, page });
  const results = new Array(jobs.length);
  let next = 0;
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const t0 = Date.now();
  console.log(`# LitAI 响应式审计 ${jobs.length} 条 | BASE=${BASE} | 并发=${WORKERS} | SETTLE=${SETTLE}ms | STABLE=${STABLE}ms${EXTRA ? ' | EXTRA=' + EXTRA : ''}`);
  await Promise.all(Array.from({ length: Math.min(WORKERS, jobs.length) }, async () => {
    while (true) {
      const i = next++;
      if (i >= jobs.length) break;
      const rec = await measureOne(browser, jobs[i]);
      results[i] = rec;
      const detail = rec.ok
        ? `${rec.sw}/${rec.iw} sh=${rec.sh} scrolled=${rec.scrolled} ovf=${rec.hOverflow} nav=${rec.hasTopnav} errs=${rec.errors.length}${rec.bad.length ? ' BAD:' + rec.bad.slice(0, 3).join('|') : ''}`
        : 'FAIL ' + rec.error;
      console.log(`${rec.size.padEnd(9)} ${rec.page.padEnd(30)} ${detail}  (${(rec.ms / 1000).toFixed(1)}s)`);
    }
  }));
  await browser.close();
  const total = Date.now() - t0;
  const problems = results.filter(r => !r.ok || r.hOverflow || (r.errors || []).length || (r.bad || []).length);
  console.log(`# 用时 ${(total / 1000).toFixed(1)}s（串行约 ${(results.reduce((s, r) => s + (r.ms || 0), 0) / 1000).toFixed(0)}s）`);
  console.log(`# 问题记录 ${problems.length}/${results.length}：ovf=${results.filter(r => r.hOverflow).length} errs=${results.filter(r => (r.errors || []).length).length} bad=${results.filter(r => (r.bad || []).length).length} fail=${results.filter(r => !r.ok).length}`);
  if (OUT) {
    fs.writeFileSync(OUT, JSON.stringify(results, null, 1));
    console.log('WROTE ' + OUT);
  }
  process.exit(problems.length ? 1 : 0);
})().catch(e => { console.error('AUDIT-ERROR', e && e.stack || e); process.exit(2); });
