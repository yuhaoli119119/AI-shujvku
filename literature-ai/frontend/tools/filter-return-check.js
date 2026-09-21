#!/usr/bin/env node
/* =====================================================================
   filter-return-check —— 「筛选 → 进详情 → 返回」状态还原 + return_to 安全 验收
   ---------------------------------------------------------------------
   用法（在服务器上、容器运行中）：
     cd /opt/literature-ai/frontend && node tools/filter-return-check.js
     BASE=http://172.18.0.6:8000 ONLY=literature_screening node tools/filter-return-check.js
     OUT=/tmp/x.json node tools/filter-return-check.js

   覆盖 6 组断言（2026-09-21 实测 PASS=120 FAIL=0）：
     1) 5 个列表页：设 2 项非默认筛选（+分页）→ 记 URL 与条数 → 进详情断言带 from/return_to
        → 断言详情页返回按钮与面包屑指向该 URL → 真实点击返回 → 断言控件值/条数/URL 逐项一致
     2) 冷启动：新 context 直开带参 URL，控件值、条数、URL 都要与第 1 步一致
     3) return_to 开放重定向：11 个恶意/非法值必须被拒并回落到文献库
     4) 合法同源白名单 return_to 必须被接受；嵌套 return_to 必须被剔除
     5) 无 return_to 时 from 仍决定去向与按钮文案
     6) literature_screening 的「显式应用」语义：未点 Apply Filters 时 URL 与列表都不变

   环境变量：BASE（被测源，默认 http://172.18.0.6:8000）、ONLY（逗号分隔页名，只跑指定页）、
             OUT（报告路径，默认 /tmp/litai-uicheck/filter-return-check.json）

   ⚠ 与库内容绑定的硬编码期望值（库内容变化时会失败，按下面的办法重新取值）：
     - literature_screening 无筛选 = 99 条；has_pdf=true&year_min=2015 = 79 条
       取法：curl -s "$BASE/api/library/papers/filter?limit=200&has_pdf=true&year_min=2015"
     - dft_database 表格只查 status=exportable（"all" 是 253 条）：
       adsorption_energy 命中 33 条，所以每页保持默认 25 才能出第 2 页（8 行）
       取法：curl -s "$BASE/api/papers/compare?status=exportable&compact=1&sort=catalyst_group&limit=1&property_type=adsorption_energy"
     - literature_library 默认最大文献库（双原子催化剂 69 篇）里 pdf=true 命中 65 条
   ===================================================================== */'use strict';
const path = require('path');
function loadPlaywright() {
  const candidates = [
    path.resolve(__dirname, '..', 'node_modules', 'playwright-core'),
    path.resolve(__dirname, '..', 'node_modules', '@playwright', 'test'),
    '/opt/AI-shujvku/literature-ai/frontend/node_modules/playwright-core',
  ];
  for (const c of candidates) { try { return require(c); } catch (_) {} }
  console.error('找不到 playwright，请先在 frontend/ 下 npm install');
  process.exit(2);
}
const PW = loadPlaywright();
const BASE = (process.env.BASE || 'http://172.18.0.6:8000').replace(/\/$/, '');
const OUT = process.env.OUT || '/tmp/litai-uicheck/filter-return-check.json';
const ONLY = (process.env.ONLY || '').split(',').map(x => x.trim()).filter(Boolean);

let PASS = 0, FAIL = 0; const fails = [], warns = [], notes = [];
function ck(name, ok, extra) {
  if (ok) { PASS++; console.log('  PASS  ' + name); }
  else { FAIL++; fails.push(name + (extra ? ' :: ' + extra : '')); console.log('  FAIL  ' + name + (extra ? '  :: ' + extra : '')); }
}
function warn(msg) { warns.push(msg); console.log('  WARN  ' + msg); }

const cfg_screening_count = '79';   /* /api/library/papers/filter?has_pdf=true&year_min=2015 的真值 */
const CFG = [
  {
    key: 'literature_library', path: '/pages/literature_library/index.html',
    probe: { controls: [['librarySelect','select'],['searchInput','text'],['yearFilter','select'],['journalFilter','select'],['typeFilter','select'],['dftFilter','select'],['contentFilter','select'],['pdfFilter','select'],['sortSelect','select'],['pageSizeSelect','select']],
             total: '#resultCount', rows: '#paperRows tr[data-paper-id]' },
    wants: { sort: 'title_asc', pdf: 'true', size: '10', page: '2' },
    setup: async (pg) => {
      await pg.selectOption('#sortSelect', 'title_asc');
      await pg.selectOption('#pdfFilter', 'true');
      await pg.waitForTimeout(400);
      await pg.selectOption('#pageSizeSelect', '10');
      await pg.waitForTimeout(900);
      await pg.click('#pageButtons button[data-page="2"]');
    },
    entry: async (pg) => { await pg.click('#paperRows tr[data-paper-id]:nth-child(3)'); },
    ready: async (pg) => { await pg.waitForFunction(() => document.querySelectorAll('#paperRows tr[data-paper-id]').length > 0, null, { timeout: 40000 }); }
  },
  {
    key: 'literature_screening', path: '/pages/literature_screening/index.html',
    probe: { controls: [['filterYearMin','text'],['filterYearMax','text'],['filterJournalInc','text'],['filterJournalExc','text'],['filterIFMin','text'],['filterIFMax','text'],['filterNeedsMetadata','check'],['filterHasPdf','check'],['filterHasParsedText','check'],['filterHasExtractionOutput','check'],['filterHasVerifiedEvidence','check'],['filterHasSafeVerifiedEvidence','check'],['filterExcludeFromCitation','select'],['filterCitationPriority','select']],
             total: '#resultCount', rows: '#resultsTableBody tr' },
    wants: { year_min: '2015', has_pdf: '1' },
    awaitResult: '79',
    setup: async (pg) => {
      await pg.fill('#filterYearMin', '2015');
      await pg.waitForTimeout(500);
      await pg.check('#filterHasPdf');
      await pg.click('button:has-text("Apply Filters")');
      try { await pg.waitForFunction(() => { const el = document.getElementById('resultCount'); return el && el.textContent.replace(/\D/g, '') === '79'; }, null, { timeout: 30000 }); } catch (_) { notes.push('screening 未等到 79，靠 settle 兜底'); }
    },
    entry: async (pg) => { await pg.click('#resultsTableBody a.screening-title-link'); },
    ready: async (pg) => {
      const filtered = await pg.evaluate(() => location.search.includes('year_min=2015'));
      if (!filtered) { await pg.waitForFunction(() => { const el = document.getElementById('resultCount'); return el && el.textContent.trim().length > 0; }, null, { timeout: 25000 }); return; }
      await pg.waitForFunction((n) => { const el = document.getElementById('resultCount'); return el && el.textContent.replace(/\D/g, '') === n; }, cfg_screening_count, { timeout: 40000 });
    }
  },
  {
    key: 'dft_database', path: '/pages/dft_database/index.html',
    probe: { controls: [['libraryFilter','select'],['propertyType','select'],['adsorbate','text'],['catalystName','select'],['minConfidence','text'],['datasetProfile','select'],['dftPageSizeSelect','select']],
             total: null, rows: '#dftList tr:not(.empty-state)' },
    wants: { energy: 'adsorption_energy', page: '2' },
    note: 'DFT 表格只查 status=exportable，池子 253 条；加 adsorption_energy 后 33 条，故每页保持默认 25 才能验证第 2 页',
    setup: async (pg) => {
      await pg.selectOption('#propertyType', 'adsorption_energy');
      await pg.waitForTimeout(3000);
      await pg.waitForSelector('#dftPageSizeSelect', { timeout: 40000 });
      await pg.waitForTimeout(2000);
      await pg.waitForFunction(() => [...document.querySelectorAll('.page-indicator')].some(b => b.textContent.trim() === '2'), null, { timeout: 40000 });
      await pg.evaluate(() => { const b = [...document.querySelectorAll('.page-indicator')].find(x => x.textContent.trim() === '2'); if (!b) throw new Error('未渲染出第 2 页按钮'); b.click(); });
    },
    entry: async (pg) => {
      await pg.evaluate(() => { const a = document.querySelector('#dftList a[href*="paper_detail"]'); if (a) a.removeAttribute('target'); });
      await pg.click('#dftList a[href*="paper_detail"]');
    },
    entryNote: 'DFT 详情链接是 target=_blank；仅在测试里摘掉 target 以同标签验证',
    ready: async (pg) => {
      await pg.waitForFunction(() => { const bar = document.getElementById('dftPaginationBar'); return !!bar && bar.innerHTML.trim().length > 0; }, null, { timeout: 70000 });
      await pg.waitForFunction(() => document.querySelectorAll('#dftList tr').length > 0 && !document.querySelector('#dftList td.empty-state'), null, { timeout: 70000 });
    }
  },
  {
    key: 'mechanism_knowledge', path: '/pages/mechanism_knowledge/index.html',
    probe: { controls: [], total: null, rows: '.tab-pane.active a[href*="paper_detail"]' },
    setup: async (pg, rec) => {
      for (const tab of ['adsorbates', 'gaps', 'catalysts']) {
        await pg.click('.tab-btn[data-tab="' + tab + '"]');
        try { await pg.waitForSelector('#tab-' + tab + ' a[href*="paper_detail"]', { timeout: 15000 }); } catch (_) { continue; }
        rec.actualTab = tab;
        notes.push('mechanism_knowledge 用的是 tab=' + tab);
        return;
      }
      throw new Error('mechanism_knowledge 四个页签都没有详情入口');
    },
    wants: { tab: 'dynamic' },
    entry: async (pg) => { await pg.click('.tab-pane.active a[href*="paper_detail"]'); },
    ready: async (pg) => { await pg.waitForFunction(() => document.querySelectorAll('.tab-pane.active a[href*="paper_detail"]').length > 0, null, { timeout: 40000 }); }
  },
  {
    key: 'visuals', path: '/pages/visuals/index.html',
    probe: { controls: [['librarySelect','select'],['xField','select'],['yField','select'],['minN','text'],['matrixScopeSelect','select'],['matrixReactionSelect','select'],['matrixAdsorbateSelect','select'],['matrixFamilySelect','select'],['matrixMinN','text']],
             total: null, rows: '#matrixPaperList a[href*="paper_detail"]' },
    wants: { tab: 'matrix', matrix_min_n: '4' },
    setup: async (pg, rec) => {
      await pg.click('#tabNavMatrix');
      rec.actualTab = 'matrix';
      await pg.waitForSelector('#matrixPaperList', { timeout: 40000 });
      await pg.waitForTimeout(1500);
      await pg.evaluate(() => { const s = document.getElementById('matrixMinN'); if (s) { s.value = '4'; s.dispatchEvent(new Event('change', { bubbles: true })); } });
      try { await pg.waitForSelector('#matrixPaperList a[href*="paper_detail"]', { timeout: 30000 }); }
      catch (_) { warn('visuals matrix 首次无详情入口，回落 scope=all 重试'); await pg.selectOption('#matrixScopeSelect', 'all'); await pg.waitForSelector('#matrixPaperList a[href*="paper_detail"]', { timeout: 30000 }); rec.scopeFallback = true; }
    },
    entry: async (pg) => { await pg.click('#matrixPaperList a[href*="paper_detail"]'); },
    ready: async (pg) => { await pg.waitForSelector('#matrixPaperList', { timeout: 40000 }); }
  }
];

function errorsOn(pg) {
  const errs = [];
  pg.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
  pg.on('pageerror', (e) => errs.push('pageerror: ' + String(e.message).slice(0, 200)));
  return errs;
}

async function snap(pg, probe) {
  return await pg.evaluate((p) => {
    const controls = {};
    (p.controls || []).forEach(([id, t]) => {
      const el = document.getElementById(id);
      controls[id] = el ? (t === 'check' ? (el.checked ? '1' : '0') : String(el.value)) : null;
    });
    const totalEl = p.total ? document.querySelector(p.total) : null;
    return {
      pathname: location.pathname,
      search: location.search,
      href: location.origin + location.pathname + location.search,
      params: [...new URLSearchParams(location.search).entries()].map(([k, v]) => k + '=' + v).sort(),
      controls,
      total: totalEl ? (String(totalEl.textContent).replace(/[^\d]/g, '') || '0') : null,
      rows: p.rows ? document.querySelectorAll(p.rows).length : null
    };
  }, probe);
}

/* 等页面自己稳住：连续 3 次（≥1.5s）URL/控件/条数都不变才取样 */
async function settle(pg, cfg, capMs) {
  const probe = cfg.probe || cfg;
  if (cfg.ready) { try { await cfg.ready(pg); } catch (_) {} }
  const cap = capMs || 45000; const t0 = Date.now();
  let last = null, same = 0, s = null;
  while (Date.now() - t0 < cap) {
    s = await snap(pg, probe);
    const key = JSON.stringify([s.params, s.total, s.rows, s.controls]);
    if (key === last) { same++; if (same >= 6 && Date.now() - t0 > 4000) return s; } else { same = 0; last = key; }
    await pg.waitForTimeout(700);
  }
  s = await snap(pg, probe); s._timeout = true; return s;
}

const DETAIL_BASE = BASE + '/pages/paper_detail/index.html';
const norm = (u) => { const x = new URL(u, DETAIL_BASE); return x.pathname + x.search; };

async function runRoundTrip(browser, cfg, rec) {
  console.log('\n================ ' + cfg.key + ' ================');
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const pg = await ctx.newPage();
  const errs = errorsOn(pg);
  await pg.goto(BASE + cfg.path, { waitUntil: 'load' });
  await settle(pg, cfg, 40000);
  await cfg.setup(pg, rec);
  const before = await settle(pg, cfg, 40000);
  console.log('  进详情前 URL  : ' + before.search);
  console.log('  进详情前 结果 : total=' + before.total + ' rows=' + before.rows);

  for (const [k, v] of Object.entries(cfg.wants)) {
    const got = new URLSearchParams(before.search).get(k);
    const want = (v === 'dynamic') ? rec.actualTab : v;
    ck(cfg.key + ' URL 写入 ' + k + '=' + want, got === want, 'got=' + got);
  }
  ck(cfg.key + ' 筛选后结果非空', Number(before.total === null ? before.rows : before.total) > 0, 'total=' + before.total + ' rows=' + before.rows);

  await cfg.entry(pg);
  await pg.waitForURL(/paper_detail/, { timeout: 40000 });
  await pg.waitForTimeout(800);
  const detailUrl = pg.url();
  const rt = new URL(detailUrl).searchParams.get('return_to');
  ck(cfg.key + ' 进详情 URL 带 return_to', !!rt, 'url=' + detailUrl.slice(0, 200));
  ck(cfg.key + ' return_to == 进去前的列表 URL', !!rt && norm(rt) === norm(before.href), 'return_to=' + rt + ' vs ' + norm(before.href));
  ck(cfg.key + ' 详情 URL 带 from=' + cfg.key, new URL(detailUrl).searchParams.get('from') === cfg.key);

  const detailLinks = await pg.evaluate(() => {
    const g = (id) => { const a = document.getElementById(id); return a ? { href: a.getAttribute('href'), text: (a.textContent || '').trim() } : null; };
    const bar = document.getElementById('pdDeepLinkBar');
    const barLinks = bar && !bar.hidden ? [...bar.querySelectorAll('a')].map(a => a.getAttribute('href')) : [];
    return { back: g('pdReturnList'), crumb: g('pdBreadcrumbHome'), fallback: g('pdFallbackBack'), barLinks };
  });
  ck(cfg.key + ' 详情页「返回列表」href == 进去前列表 URL', !!detailLinks.back && norm(detailLinks.back.href) === norm(before.href), 'href=' + (detailLinks.back && detailLinks.back.href));
  ck(cfg.key + ' 面包屑 href == 进去前列表 URL', !!detailLinks.crumb && norm(detailLinks.crumb.href) === norm(before.href), 'href=' + (detailLinks.crumb && detailLinks.crumb.href));
  const expectedListHref = norm(before.href);
  ck(cfg.key + ' 空态返回按钮同源同目标', !detailLinks.fallback || norm(detailLinks.fallback.href) === expectedListHref, 'href=' + (detailLinks.fallback && detailLinks.fallback.href));
  ck(cfg.key + ' 定位条内返回链接同目标', detailLinks.barLinks.length === 0 || detailLinks.barLinks.some(h => norm(h) === expectedListHref), JSON.stringify(detailLinks.barLinks));

  let clicked = true;
  try {
    await pg.waitForFunction(() => { const el = document.getElementById('paperContent'); return el && el.offsetParent !== null; }, null, { timeout: 40000 });
    await pg.click('#pdReturnList', { timeout: 15000 });
  } catch (e) {
    clicked = false;
    warn(cfg.key + ' 详情内容 40s 内不可见，改用 goto(返回链接) 验证：' + String(e.message).slice(0, 60));
    await pg.goto(norm(before.href) === '' ? BASE + cfg.path : BASE + expectedListHref, { waitUntil: 'load' });
  }
  await pg.waitForURL((u) => new URL(u).pathname === cfg.path, { timeout: 40000 });
  const after = await settle(pg, cfg, 40000);
  ck(cfg.key + ' 返回后落在 ' + cfg.key + '（' + (clicked ? '真实点击返回' : 'goto 兜底') + '）', after.pathname === cfg.path);
  ck(cfg.key + ' 返回后 URL 参数逐项一致', JSON.stringify(after.params) === JSON.stringify(before.params),
     'before=' + JSON.stringify(before.params) + ' after=' + JSON.stringify(after.params));
  ck(cfg.key + ' 返回后控件值全部还原', JSON.stringify(after.controls) === JSON.stringify(before.controls),
     'before=' + JSON.stringify(before.controls) + ' after=' + JSON.stringify(after.controls));
  ck(cfg.key + ' 返回后结果条数一致', after.total === before.total && after.rows === before.rows,
     'before total/rows=' + before.total + '/' + before.rows + ' after=' + after.total + '/' + after.rows);
  ck(cfg.key + ' 全程 0 JS 报错', errs.length === 0, errs.slice(0, 3).join(' | '));

  const ctx2 = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const pg2 = await ctx2.newPage();
  const errs2 = errorsOn(pg2);
  await pg2.goto(before.href, { waitUntil: 'load' });
  const cold = await settle(pg2, cfg, 40000);
  ck(cfg.key + ' 冷启动控件值 == 进详情前', JSON.stringify(cold.controls) === JSON.stringify(before.controls), JSON.stringify(cold.controls));
  ck(cfg.key + ' 冷启动结果条数一致', cold.total === before.total && cold.rows === before.rows,
     'cold total/rows=' + cold.total + '/' + cold.rows + ' vs ' + before.total + '/' + before.rows);
  ck(cfg.key + ' 冷启动 URL 未被改写', JSON.stringify(cold.params) === JSON.stringify(before.params),
     'cold=' + JSON.stringify(cold.params));
  ck(cfg.key + ' 冷启动 0 JS 报错', errs2.length === 0, errs2.slice(0, 3).join(' | '));

  await ctx.close(); await ctx2.close();
  rec.before = before; rec.after = after; rec.cold = cold; rec.detailUrl = detailUrl; rec.detailLinks = detailLinks;
}

(async () => {
  const browser = await PW.chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const report = { base: BASE, pages: {}, security: {} };
  let paperId = null;
  const list = ONLY.length ? CFG.filter(c => ONLY.includes(c.key)) : CFG;

  for (const cfg of list) {
    const rec = {};
    try { await runRoundTrip(browser, cfg, rec); }
    catch (e) { FAIL++; fails.push(cfg.key + ' 执行异常'); console.log('  FAIL  ' + cfg.key + ' 执行异常 :: ' + String(e.message).slice(0, 300)); }
    report.pages[cfg.key] = rec;
    if (cfg.key === 'literature_library') {
      const ctx = await browser.newContext(); const pg = await ctx.newPage();
      await pg.goto(BASE + cfg.path, { waitUntil: 'load' }); await pg.waitForTimeout(4000);
      paperId = await pg.evaluate(() => { const tr = document.querySelector('#paperRows tr[data-paper-id]'); return tr ? tr.dataset.paperId : null; });
      await ctx.close();
    }
  }

  if (ONLY.length && !ONLY.includes('literature_library')) {
    const ctx = await browser.newContext(); const pg = await ctx.newPage();
    await pg.goto(BASE + '/pages/literature_library/index.html', { waitUntil: 'load' }); await pg.waitForTimeout(4000);
    paperId = await pg.evaluate(() => { const tr = document.querySelector('#paperRows tr[data-paper-id]'); return tr ? tr.dataset.paperId : null; });
    await ctx.close();
  }

  /* literature_screening 是「显式应用」语义：改控件不该动 URL/列表，点 Apply 才一起动 */
async function checkScreeningApplyGating(browser) {
  console.log('\n================ literature_screening：显式应用语义 ================');
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const pg = await ctx.newPage();
  const errs = errorsOn(pg);
  const count = () => pg.evaluate(() => document.getElementById('resultCount').textContent.replace(/\D/g, ''));
  await pg.goto(BASE + '/pages/literature_screening/index.html', { waitUntil: 'load' });
  await pg.waitForFunction(() => { const el = document.getElementById('resultCount'); return el && el.textContent.replace(/\D/g, '') === '99'; }, null, { timeout: 40000 });
  const beforeUrl = await pg.evaluate(() => location.search);
  const beforeCount = await count();
  await pg.fill('#filterYearMin', '2015');
  await pg.check('#filterHasPdf');
  await pg.waitForTimeout(3000);
  const midUrl = await pg.evaluate(() => location.search);
  const midCount = await count();
  ck('未点 Apply 时 URL 不变', midUrl === beforeUrl, 'before=' + beforeUrl + ' mid=' + midUrl);
  ck('未点 Apply 时列表不变', midCount === beforeCount, 'before=' + beforeCount + ' mid=' + midCount);
  await pg.click('button:has-text("Apply Filters")');
  await pg.waitForFunction(() => document.getElementById('resultCount').textContent.replace(/\D/g, '') === '79', null, { timeout: 30000 });
  const afterUrl = await pg.evaluate(() => location.search);
  const q = new URLSearchParams(afterUrl);
  ck('点 Apply 后 URL 与列表一起更新', q.get('year_min') === '2015' && q.get('has_pdf') === '1', 'url=' + afterUrl);
  await pg.evaluate(() => clearFilters());
  await pg.waitForFunction(() => document.getElementById('resultCount').textContent.replace(/\D/g, '') === '99', null, { timeout: 30000 });
  await pg.waitForTimeout(1200);
  const clearedUrl = await pg.evaluate(() => location.search);
  ck('Clear Filters 后 URL 一起清空', clearedUrl === '', 'cleared=' + clearedUrl);
  ck('显式应用语义下 0 JS 报错', errs.length === 0, errs.slice(0, 3).join(' | '));
  await ctx.close();
}

  if (!ONLY.length || ONLY.includes('literature_screening')) {
    try { await checkScreeningApplyGating(browser); }
    catch (e) { FAIL++; fails.push('literature_screening 显式应用语义 执行异常'); console.log('  FAIL  screening 显式应用语义 执行异常 :: ' + String(e.message).slice(0, 200)); }
  }

console.log('\n================ return_to 安全 ================');
  const pid = paperId || '1b6dfbb2-c1eb-4887-acd2-0fa8e18aa62a';
  const good = '/pages/visuals/index.html?tab=matrix';
  const cases = [
    ['外部域名', 'https://evil.example.com/'],
    ['协议相对', '//evil.example.com/pages/visuals/index.html'],
    ['路径穿越', '/pages/../admin'],
    ['javascript:', 'javascript:alert(1)'],
    ['非白名单页', '/pages/dashboard/index.html'],
    ['详情页自身', '/pages/paper_detail/index.html?paper_id=x'],
    ['内网地址', 'http://127.0.0.1:8000/pages/visuals/index.html'],
    ['超长', '/pages/visuals/index.html?' + 'a'.repeat(600)],
    ['控制字符', '/pages/visuals/index.html%0d%0aX-Injected:1'],
    ['缺 index.html', '/pages/visuals/'],
    ['data:', 'data:text/html,<script>alert(1)</script>']
  ];
  const fallback = '/pages/literature_library/index.html';
  for (const [label, payload] of cases) {
    const ctx = await browser.newContext(); const pg = await ctx.newPage();
    await pg.goto(BASE + '/pages/paper_detail/index.html?paper_id=' + encodeURIComponent(pid) + '&from=literature_library&return_to=' + encodeURIComponent(payload), { waitUntil: 'load' });
    await pg.waitForTimeout(3500);
    const href = await pg.evaluate(() => { const a = document.getElementById('pdReturnList'); return a ? a.getAttribute('href') : null; });
    const u = new URL(href, DETAIL_BASE);
    ck('拒绝 ' + label, u.origin === BASE && norm(href) === fallback, 'href=' + href);
    report.security[label] = { payload, href };
    await ctx.close();
  }
  {
    const ctx = await browser.newContext(); const pg = await ctx.newPage();
    await pg.goto(BASE + '/pages/paper_detail/index.html?paper_id=' + encodeURIComponent(pid) + '&from=literature_library&return_to=' + encodeURIComponent(good), { waitUntil: 'load' });
    await pg.waitForTimeout(3500);
    const href = await pg.evaluate(() => document.getElementById('pdReturnList').getAttribute('href'));
    ck('接受合法 return_to（同源白名单页）', norm(href) === norm(good), 'href=' + href);
    await ctx.close();
  }
  {
    const nested = '/pages/visuals/index.html?tab=matrix&return_to=' + encodeURIComponent('https://evil.example.com/');
    const ctx = await browser.newContext(); const pg = await ctx.newPage();
    await pg.goto(BASE + '/pages/paper_detail/index.html?paper_id=' + encodeURIComponent(pid) + '&from=visuals&return_to=' + encodeURIComponent(nested), { waitUntil: 'load' });
    await pg.waitForTimeout(3500);
    const href = await pg.evaluate(() => document.getElementById('pdReturnList').getAttribute('href'));
    const u = new URL(href, DETAIL_BASE);
    ck('嵌套 return_to 被剔除', u.origin === BASE && !String(href).includes('evil') && u.pathname === '/pages/visuals/index.html' && u.searchParams.get('tab') === 'matrix', 'href=' + href);
    await ctx.close();
  }
  for (const [src, page] of [['review_center', '/pages/review_center/index.html'], ['dft_database', '/pages/dft_database/index.html']]) {
    const ctx = await browser.newContext(); const pg = await ctx.newPage();
    await pg.goto(BASE + '/pages/paper_detail/index.html?paper_id=' + encodeURIComponent(pid) + '&from=' + src, { waitUntil: 'load' });
    await pg.waitForTimeout(3500);
    const r = await pg.evaluate(() => ({ href: document.getElementById('pdReturnList').getAttribute('href'), text: document.getElementById('pdReturnList').textContent.trim(), crumb: document.getElementById('pdBreadcrumbHome').textContent.trim() }));
    const ru = new URL(r.href, DETAIL_BASE);
    const okPath = ru.pathname === page;
    const okPid = (src !== 'review_center') || ru.searchParams.get('paper_id') === pid;
    ck('无 return_to 时 from=' + src + ' 决定去向', okPath && okPid, 'href=' + r.href);
    ck('无 return_to 时 from=' + src + ' 按钮文案已随来源切换，当前=「' + r.text + '」', r.text !== '返回列表' && r.text.length > 0);
    await ctx.close();
  }
  {
    const rt = '/pages/literature_library/index.html?pdf=true&size=10&page=2';
    const entry = '/pages/literature_library/index.html?paper_id=' + encodeURIComponent(pid) + '&from=literature_library&return_to=' + encodeURIComponent(rt);
    const ctx = await browser.newContext(); const pg = await ctx.newPage();
    await pg.goto(BASE + entry, { waitUntil: 'load' });
    await pg.waitForTimeout(5000);
    const landed = pg.url();
    ck('literature_library replace 透传 return_to', new URL(landed).pathname === '/pages/paper_detail/index.html' && new URL(landed).searchParams.get('return_to') === rt, 'landed=' + landed.slice(0, 200));
    const href = await pg.evaluate(() => document.getElementById('pdReturnList').getAttribute('href'));
    ck('透传后返回目标带筛选参数', norm(href) === rt, 'href=' + href);
    await ctx.close();
  }

  await browser.close();
  report.summary = { PASS, FAIL, fails, warns, notes };
  require('fs').writeFileSync(OUT, JSON.stringify(report, null, 2));
  console.log('\n================ 汇总 ================');
  console.log('PASS=' + PASS + ' FAIL=' + FAIL + '  WARN=' + warns.length);
  warns.forEach((w) => console.log('WARN: ' + w));
  if (fails.length) { console.log('失败清单：'); fails.forEach((f) => console.log(' - ' + f)); }
  console.log('报告：' + OUT);
  process.exit(FAIL ? 1 : 0);
})();
