#!/usr/bin/env node
/* =====================================================================
   focused-diff —— 逐条对比两份「定向回归」结果（基线 vs 当前）
   ---------------------------------------------------------------------
   用法：
     node frontend/tools/focused-diff.js tools/baselines/focused-2026-09-21.json /tmp/litai-uicheck/focused-current.json

   两份输入都支持以下两种格式，自动识别：
     1) Playwright JSON reporter 的原始报告（有 .suites，含 stdout 等大字段）
     2) tools/focused-suite.sh 冻结的紧凑摘要（有 .specs 数组：file/title/status）
   对比粒度：spec（文件 + 标题）→ 状态（passed / failed / timedOut / skipped）。
   退出码：0 = 无新增失败；1 = 有新增失败或记录集合变化；2 = 用法/读取错误。
   ===================================================================== */
'use strict';
const fs = require('fs');

const [, , aPath, bPath] = process.argv;
if (!aPath || !bPath) {
  console.error('用法: node focused-diff.js <基线.json> <当前.json>');
  process.exit(2);
}

const load = (p) => {
  let d;
  try { d = JSON.parse(fs.readFileSync(p, 'utf8')); }
  catch (e) { console.error('读取失败 ' + p + ': ' + e.message); process.exit(2); }
  const specs = new Map();
  if (Array.isArray(d.specs)) {
    d.specs.forEach((s) => specs.set(key(s), s.status || 'unknown'));
  } else {
    const walk = (s) => {
      (s.specs || []).forEach((sp) => {
        const t = (sp.tests || [])[0] || {};
        const r = (t.results || [])[0] || {};
        specs.set(key(sp), r.status || t.status || 'unknown');
      });
      (s.suites || []).forEach(walk);
    };
    (d.suites || []).forEach(walk);
  }
  return { stats: d.stats || {}, specs };
};
const key = (s) => (s.file || '') + ' › ' + (s.title || '');
const isPass = (st) => st === 'passed' || st === 'expected';

const A = load(aPath), B = load(bPath);
const fmt = (s) => 'expected=' + (s.expected === undefined ? '?' : s.expected)
  + ' unexpected=' + (s.unexpected === undefined ? '?' : s.unexpected)
  + ' flaky=' + (s.flaky === undefined ? '?' : s.flaky);
console.log('# 基线 ' + aPath + '：' + A.specs.size + ' 条，' + fmt(A.stats));
console.log('# 当前 ' + bPath + '：' + B.specs.size + ' 条，' + fmt(B.stats));

const onlyA = [...A.specs.keys()].filter((k) => !B.specs.has(k));
const onlyB = [...B.specs.keys()].filter((k) => !A.specs.has(k));
const newFail = [], fixed = [], stillFail = [];
A.specs.forEach((st, k) => {
  if (!B.specs.has(k)) return;
  const st2 = B.specs.get(k);
  if (isPass(st) && !isPass(st2)) newFail.push(k + '  [' + st + ' -> ' + st2 + ']');
  else if (!isPass(st) && isPass(st2)) fixed.push(k);
  else if (!isPass(st)) stillFail.push(k);
});
console.log('# 只在基线有 ' + onlyA.length + ' 条' + (onlyA.length ? '：\n  ' + onlyA.join('\n  ') : ''));
console.log('# 只在当前有 ' + onlyB.length + ' 条' + (onlyB.length ? '：\n  ' + onlyB.join('\n  ') : ''));
console.log('# 新增失败 ' + newFail.length + ' 条' + (newFail.length ? '：\n  ' + newFail.join('\n  ') : ''));
console.log('# 由失败转通过 ' + fixed.length + ' 条');
console.log('# 一向失败（既有环境性） ' + stillFail.length + ' 条');
const ok = newFail.length === 0 && onlyA.length === 0 && onlyB.length === 0;
console.log('# 结论：' + (ok ? '逐条一致，0 新增失败 ✔' : '存在差异 ✘'));
process.exit(ok ? 0 : 1);
