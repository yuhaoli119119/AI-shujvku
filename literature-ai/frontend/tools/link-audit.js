#!/usr/bin/env node
/* =====================================================================
   link-audit —— 全站内部链接体检（每个 <a href> 解析成绝对地址逐个请求）
   ---------------------------------------------------------------------
   用法：
     BASE=http://172.18.0.6:8000 node frontend/tools/link-audit.js
     PAGES=a,b,c OUT=/tmp/links.json node frontend/tools/link-audit.js
   检查项：
     1) 每个唯一目标返回码必须是 2xx/304（登录跳转 302 记 warning，不算异常）；
     2) 指向 paper_detail 的链接必须带 paper_id（否则会落到空详情页）；
     3) 指向 paper_detail 的链接若是「从别的页面跳过来的定位链接」，应带 from=（信息项，不判失败）；
     4) 每页 jsErr 数（pageerror）必须为 0。
   退出码：0 = 无异常；1 = 有异常；2 = 脚本错误。
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
const PAGES = (process.env.PAGES || 'dashboard,dft_audit_center,dft_database,external_analysis_workbench,extraction_workflow,ingestion,literature_library,literature_screening,mechanism_knowledge,paper_detail,review_center,settings,visuals,ai_writer,content_knowledge').split(',').map(s => s.trim()).filter(Boolean);
const BASE = (process.env.BASE || 'http://172.18.0.6:8000').replace(/\/$/, '');
const OUT = process.env.OUT || '';
const SETTLE = Number(process.env.SETTLE || 6000);
/* 慢页面渲染数据需要更长的静置：6 秒时它们的链接还没进 DOM，会漏检。 */
const PAGE_SETTLE = { dft_database: 24000, visuals: 12000, review_center: 9000, mechanism_knowledge: 9000, literature_screening: 9000, paper_detail: 9000 };
/* 这些页面必须有"进入论文详情"的入口（链接或整行点击）；其余页面没有是正常的，只统计不判失败。 */
const EXPECT_DETAIL_ENTRY = ['literature_library', 'literature_screening', 'mechanism_knowledge', 'dft_database', 'review_center'];

(async () => {
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const cache = new Map();
  const problems = [];
  const perPage = [];

  const probe = async (url) => {
    if (cache.has(url)) return cache.get(url);
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    let status = 0, err = '';
    try {
      const resp = await p.request.get(url, { maxRedirects: 0, timeout: 15000 });
      status = resp.status();
    } catch (e) { err = String(e.message).slice(0, 80); }
    await ctx.close();
    const rec = { status, err };
    cache.set(url, rec);
    return rec;
  };

  for (const page of PAGES) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const p = await ctx.newPage();
    const errs = [];
    p.on('pageerror', e => errs.push(String(e.message).slice(0, 120)));
    await p.goto(`${BASE}/pages/${page}/index.html`, { waitUntil: 'load', timeout: 30000 }).catch(() => {});
    await p.waitForTimeout(PAGE_SETTLE[page] || SETTLE);
    const hrefs = await p.$$eval('a[href]', as => as.map(a => a.getAttribute('href')).filter(Boolean));
    const uniq = [...new Set(hrefs)];
    const local = uniq.filter(h => !/^(https?:)?\/\//i.test(h) && !/^(mailto|tel|javascript):/i.test(h));
    const origin = await p.evaluate(() => location.origin + '/pages/' + location.pathname.split('/').slice(-2, -1)[0] + '/');
    let bad = 0;
    for (const h of local) {
      const abs = new URL(h, origin).href;
      const { status, err } = await probe(abs);
      const ok = (status >= 200 && status < 400) || status === 304;
      if (!ok) { bad++; problems.push(`${page}: ${h} -> ${status || err}`); }
    }
    const pd = local.filter(h => /paper_detail\/index\.html/.test(h));
    /* literature_library 的详情入口是整行点击（tr[data-paper-id]），不是 <a>，单独计。 */
    const rowEntries = await p.$$eval('tr[data-paper-id], [data-detail-paper-id]', rs => rs.length).catch(() => 0);
    const missingPid = pd.filter(h => !/[?&](paper_id|id)=[^&]+/.test(h));
    const missingFrom = pd.filter(h => /paper_id=/.test(h) && !/[?&]from=/.test(h));
    if (missingPid.length) problems.push(`${page}: ${missingPid.length} 个 paper_detail 链接缺 paper_id`);
    if (pd.length + rowEntries === 0 && EXPECT_DETAIL_ENTRY.includes(page)) problems.push(`${page}: 页面上没有任何"进入论文详情"的入口（链接/行点击都算），可能是渲染未完成或入口被删`);
    if (errs.length) problems.push(`${page}: ${errs.length} 个 pageerror`);
    perPage.push({ page, anchors: hrefs.length, uniq: uniq.length, local: local.length, pd: pd.length, rowEntries, missingPid: missingPid.length, missingFrom: missingFrom.length, bad, jsErr: errs.length });
    console.log(`${page.padEnd(30)} anchors=${hrefs.length} uniq=${uniq.length} local=${local.length} paperDetail=${pd.length} 行入口=${rowEntries} 缺pid=${missingPid.length} 缺from=${missingFrom.length} bad=${bad} jsErr=${errs.length}`);
    await ctx.close();
  }
  await browser.close();
  const uniqTargets = cache.size;
  console.log(`\n# 检查过的唯一目标 ${uniqTargets} 个；异常 ${problems.length} 个`);
  problems.slice(0, 20).forEach(x => console.log('  ✘ ' + x));
  if (OUT) fs.writeFileSync(OUT, JSON.stringify({ perPage, problems, uniqTargets }, null, 1));
  process.exit(problems.length ? 1 : 0);
})().catch(e => { console.error('LINK-AUDIT-ERROR', e && e.stack || e); process.exit(2); });
