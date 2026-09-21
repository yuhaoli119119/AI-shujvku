#!/usr/bin/env node
/* =====================================================================
   audit-diff —— 逐条对比两份 audit-responsive.js 结果（基线 vs 当前）
   ---------------------------------------------------------------------
   用法：
     node frontend/tools/audit-diff.js 基线.json 当前.json
     node frontend/tools/audit-diff.js FINAL14.json FINAL15.json --quiet
   只打印"有差异"的记录（新增/缺失/字段变化），并给出汇总与退出码：
     0 = 逐条一致；1 = 存在差异；2 = 用法/读取错误。
   对比字段：sw/iw/sh/scrolled/hOverflow/bodyOverflowHidden/bad/errors/ok/hasTopnav
   （ms 是耗时，默认忽略，便于不同并发度之间对比。）
   ===================================================================== */
'use strict';
const fs = require('fs');

const [, , aPath, bPath, ...flags] = process.argv;
if (!aPath || !bPath) {
  console.error('用法: node audit-diff.js <基线.json> <当前.json> [--quiet]');
  process.exit(2);
}
const quiet = flags.includes('--quiet');
const FIELDS = ['sw', 'iw', 'sh', 'scrolled', 'hOverflow', 'bodyOverflowHidden', 'hasTopnav', 'ok'];

const load = p => {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); }
  catch (e) { console.error(`读取失败 ${p}: ${e.message}`); process.exit(2); }
};
const key = r => `${r.size}|${r.page}`;
const norm = v => Array.isArray(v) ? v.slice().sort().join(' ¦ ') : (v === undefined ? '' : String(v));

const A = load(aPath), B = load(bPath);
const ma = new Map(A.map(r => [key(r), r]));
const mb = new Map(B.map(r => [key(r), r]));

const onlyA = [...ma.keys()].filter(k => !mb.has(k));
const onlyB = [...mb.keys()].filter(k => !ma.has(k));
const changed = [];
for (const [k, ra] of ma) {
  const rb = mb.get(k);
  if (!rb) continue;
  const diffs = [];
  for (const f of FIELDS) {
    const x = ra[f], y = rb[f];
    if (f === 'ok') { if (!!x !== !!y) diffs.push(`ok ${x} -> ${y}`); continue; }
    if (norm(x) !== norm(y)) diffs.push(`${f} ${norm(x) || '(空)'} -> ${norm(y) || '(空)'}`);
  }
  if (diffs.length) changed.push({ k, diffs });
}

console.log(`# 基线 ${aPath}（${A.length} 条） vs 当前 ${bPath}（${B.length} 条）`);
if (!quiet) {
  const bk = [...ma.keys()].sort(), ck = [...mb.keys()].sort();
  if (JSON.stringify(bk) === JSON.stringify(ck)) console.log('# 记录集合一致（同页面同尺寸）');
}
if (onlyA.length) console.log(`# 仅基线有 ${onlyA.length} 条: ${onlyA.slice(0, 8).join(', ')}${onlyA.length > 8 ? ' …' : ''}`);
if (onlyB.length) console.log(`# 仅当前有 ${onlyB.length} 条: ${onlyB.slice(0, 8).join(', ')}${onlyB.length > 8 ? ' …' : ''}`);
if (changed.length) {
  console.log(`# 字段变化 ${changed.length} 条:`);
  for (const c of changed) console.log(`  DIFF ${c.k.padEnd(24)} ${c.diffs.join(' | ')}`);
} else {
  console.log('# 字段变化 0 条（逐条一致）');
}
const total = onlyA.length + onlyB.length + changed.length;
console.log(`# 结论：${total === 0 ? '逐条 0 差异 ✔' : `共 ${total} 处差异 ✘`}`);
process.exit(total ? 1 : 0);
