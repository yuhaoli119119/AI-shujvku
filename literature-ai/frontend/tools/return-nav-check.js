#!/usr/bin/env node
/* =====================================================================
   return-nav-check —— 跨页跳转体检：从各入口页真实点击进论文详情，
   核对「面包屑 / 返回按钮 / 定位条 / 手机底部动作条」是否回到来源页。
   ---------------------------------------------------------------------
   用法：
     BASE=http://172.18.0.6:8000 node frontend/tools/return-nav-check.js
     PID=<paper_id> WIDTH=390 WIDTH2=768 node frontend/tools/return-nav-check.js
   入口与期望来源（2026-09-21 起 paper_detail 支持 from 参数 + referrer 兜底）：
     review_center → 返回审核中心 / literature_library → 返回文献库 /
     literature_screening → 返回文献筛选 / mechanism_knowledge → 返回机理知识 /
     dft_database → 返回 DFT 数据库 / dashboard → 返回工作台 / visuals → 返回图表
   dft_audit_center 是 location.replace 存根（跳到审核中心），按 review_center 期望。
   退出码：0 = 全部 PASS；1 = 有 FAIL；2 = 脚本错误。
   ===================================================================== */
'use strict';
const fs = require('fs');
const path = require('path');

function loadPlaywright() {
  const candidates = [
    path.resolve(__dirname, '..', 'node_modules', 'playwright-core'),
    path.resolve(__dirname, '..', 'node_modules', '@playwright', 'test'),
    '/opt/AI-shujvku/literature-ai/frontend/node_modules/playwright-core',
  ];
  for (const c of candidates) { try { const m = require(c); if (m && m.chromium) return m; } catch (_) {} }
  throw new Error('找不到 playwright-core：请先 cd frontend && npm install');
}
const BASE = (process.env.BASE || 'http://172.18.0.6:8000').replace(/\/$/, '');
const PID_ENV = process.env.PID || '';
const SETTLE = Number(process.env.SETTLE || 6000);
const OUT = process.env.OUT || '';
const QUICK = process.env.QUICK === '1';

/* wait：入口页本身渲染数据所需静置（dft_database / visuals 要等表格与热力图） */
const ENTRIES = [
  { name: 'review_center', label: '返回审核中心', url: pid => `/pages/review_center/index.html?paper_id=${pid}`, pick: 'a[href*="paper_detail"]', wait: 8000 },
  { name: 'literature_library', label: '返回文献库', url: () => '/pages/literature_library/index.html', pick: 'tbody tr[data-paper-id]', wait: 9000, row: true },
  { name: 'literature_screening', label: '返回文献筛选', url: () => '/pages/literature_screening/index.html', pick: 'a[href*="paper_detail"]', wait: 8000 },
  { name: 'mechanism_knowledge', label: '返回机理知识', url: () => '/pages/mechanism_knowledge/index.html', pick: 'a[href*="paper_detail"]', wait: 8000 },
  { name: 'dft_database', label: '返回 DFT 数据库', url: () => '/pages/dft_database/index.html', pick: 'a[href*="paper_detail"]', wait: 24000 },
  { name: 'dashboard', label: '返回工作台', url: () => '/pages/dashboard/index.html', pick: 'a[href*="paper_detail"]', wait: 8000 },
  { name: 'visuals', label: '返回图表', url: () => '/pages/visuals/index.html', pick: '#matrixPaperList a[href*="paper_detail"]', wait: 10000, prep: async p => { await p.click('#tabNavMatrix').catch(() => {}); await p.waitForTimeout(2000); }, waitFor: '#matrixPaperList a[href*="paper_detail"]' },
  { name: 'dft_audit_center', label: '返回审核中心', url: () => '/pages/dft_audit_center/index.html', pick: 'a[href*="paper_detail"]', wait: 9000, expectSource: 'review_center' },
];

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const results = [];

  const findPaperId = async () => {
    if (PID_ENV) return PID_ENV;
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const p = await ctx.newPage();
    await p.goto(`${BASE}/pages/review_center/index.html`, { waitUntil: 'load' });
    await p.waitForTimeout(9000);
    const pid = await p.$$eval('a[href*="paper_detail"]', as => {
      const h = as.map(a => a.getAttribute('href')).find(x => /paper_id=/.test(x || ''));
      return h ? new URLSearchParams(String(h).split('?')[1]).get('paper_id') : '';
    });
    await ctx.close();
    return pid;
  };
  const pid = await findPaperId();
  if (!pid) { console.error('找不到可用 paper_id（审核中心可能无数据）'); process.exit(2); }
  console.log(`# paper_id = ${pid} | BASE=${BASE} | QUICK=${QUICK ? 1 : 0}`);

  for (const e of ENTRIES) {
    if (QUICK && e.wait > 12000) { results.push({ entry: e.name, ok: null, note: 'QUICK=1 跳过慢入口' }); continue; }
    const ctx = await browser.newContext({ viewport: { width: Number(process.env.WIDTH || 1440), height: 900 } });
    const p = await ctx.newPage();
    const errs = []; p.on('pageerror', x => errs.push(String(x.message).slice(0, 100)));
    await p.goto(BASE + e.url(pid), { waitUntil: 'load' });
    await p.waitForTimeout(e.wait);
    if (e.prep) await e.prep(p);
    if (e.waitFor) await p.waitForSelector(e.waitFor, { timeout: 60000 }).catch(() => {});
    const has = await p.$(e.pick).catch(() => null);
    if (!has) {
      results.push({ entry: e.name, ok: false, note: '入口页没有可点击的详情入口（需先触发查询）' });
      await ctx.close(); continue;
    }
    const before = ctx.pages().length;
    await Promise.all([ctx.waitForEvent('page', { timeout: 10000 }).catch(() => null), p.click(e.pick).catch(() => null)]);
    await p.waitForTimeout(2500);
    const pages = ctx.pages();
    const detail = pages.length > before ? pages[pages.length - 1] : p;
    await detail.waitForLoadState('load').catch(() => {});
    await detail.waitForTimeout(SETTLE);
    const info = await detail.evaluate(() => {
      /* 返回相对路径要按当前详情页地址解析成 pathname 再比较，直接 includes('/pages/...') 会误判。 */
      const abs = h => { try { return new URL(h, location.href).pathname; } catch (_) { return ''; } };
      const t = el => el ? { text: (el.textContent || '').trim(), href: el.getAttribute('href') || '', path: abs(el.getAttribute('href') || '') } : null;
      const dock = document.getElementById('pdDeepLinkDock');
      return {
        url: location.pathname + location.search,
        crumb: t(document.querySelector('.pd-breadcrumb a')),
        back: t(document.getElementById('pdReturnList')),
        dock: dock && !dock.hidden ? [...dock.querySelectorAll('a')].map(a => ({ text: a.textContent.trim(), path: abs(a.getAttribute('href') || '') })) : [],
      };
    });
    const src = e.expectSource || e.name;
    const okSource = new RegExp(`(^|[?&])from=${src}(&|$)`).test(info.url);
    const wantPath = `/pages/${src}/index.html`;
    const okLabel = !!info.back && info.back.text.startsWith(e.label);
    const okTarget = !!info.back && info.back.path === wantPath;
    const okReturnUrl = (info.crumb ? info.crumb.path === wantPath : true);
    /* 定位条/底部动作条里的返回按钮也必须回来源页（有则必须一致） */
    const okDock = info.dock.every(d => !/返回/.test(d.text) || d.path === wantPath);
    const ok = okSource && okLabel && okTarget && okReturnUrl && okDock && errs.length === 0;
    results.push({ entry: e.name, ok, url: info.url, crumb: info.crumb, back: info.back, dock: info.dock, jsErr: errs.length });
    console.log(`${ok ? 'PASS' : 'FAIL'} ${e.name.padEnd(22)} 按钮=${info.back ? info.back.text + ' -> ' + info.back.path : '(无)'} | 面包屑=${info.crumb ? info.crumb.text + ' -> ' + info.crumb.path : '(无)'} | jsErr=${errs.length}${info.dock.length ? ' | 底部条=' + JSON.stringify(info.dock.map(d => d.text + ' -> ' + d.path)) : ''}`);
    await ctx.close();
  }
  await browser.close();
  const pass = results.filter(r => r.ok === true).length, fail = results.filter(r => r.ok === false).length, skip = results.filter(r => r.ok === null).length;
  console.log(`\n# 来源感知返回核对 PASS=${pass} FAIL=${fail} SKIP=${skip}`);
  results.filter(r => r.ok === false).forEach(r => console.log('  ✘ ' + r.entry + ' ' + (r.note || JSON.stringify(r.back))));
  if (OUT) fs.writeFileSync(OUT, JSON.stringify(results, null, 1));
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('RETURN-NAV-ERROR', e && e.stack || e); process.exit(2); });
