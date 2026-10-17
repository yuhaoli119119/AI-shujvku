const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const rebuild = fs.readFileSync(path.join(__dirname,'../../frontend/pages/literature_library/rebuild.js'),'utf8');
const detail = fs.readFileSync(path.join(__dirname,'../../frontend/pages/paper_detail/index.html'),'utf8');
function extract(source,name) {
  const start=source.indexOf(`function ${name}(`);assert(start>=0,name);
  const tail=source.slice(start), next=tail.slice(1).search(/\n(?:    )?(?:async )?function /);
  return next<0?tail:tail.slice(0,next+1);
}
test('acceptance stays pending/unknown honestly and shows all formal decision states',()=>{
  const context=vm.createContext({});vm.runInContext(extract(detail,'acceptanceStatusText'),context);
  for(const [status,label] of Object.entries({pending:'待提取验收',extracted:'已提取，待规则检查',rules_checked:'待独立验收',needsfix:'需要修正',accepted:'独立验收通过',partial:'部分覆盖',review_incomplete:'独立验收未完成'})){
    assert(context.acceptanceStatusText({status}).includes(label));
  }
  assert(context.acceptanceStatusText(null).includes('待核实'));
  assert(context.acceptanceStatusText({status:'accepted',stale:true}).includes('需重新验收'));
  assert(!context.acceptanceStatusText({status:'accepted',stale:true}).includes('独立验收通过'));
});
test('disabled queue, persistent pause and native dependency are visible without starting work',()=>{
  const context=vm.createContext({});vm.runInContext(extract(rebuild,'queueSummaryText'),context);
  const text=context.queueSummaryText({control:{paused:true},capabilities:{enabled:false}});
  assert(text.includes('尚未启用')&&text.includes('已暂停')&&text.includes('不会启动'));
  assert(context.queueSummaryText({control:{paused:false},capabilities:{enabled:true}}).includes('协调任务继续'));
});
test('raw-only table rule remains partial and explicitly pending representation',()=>{
  const context=vm.createContext({});vm.runInContext(extract(detail,'acceptanceStatusText'),context);
  const value=context.acceptanceStatusText({status:'partial',progress:{rules:{passed:false,
    issues:[{code:'table_representation_incomplete',format:'text',reason:'pending_structure'}]}}});
  assert(value.includes('表格待结构化')&&value.includes('原始文本可查看复制'));
  assert(!value.includes('独立验收通过'));
});
test('needs_attention stops the frontend native status timer without treating it as success',async()=>{
  let stopped=0, started=0;
  const context=vm.createContext({state:{selectedId:'paper',selected:{}},api:async()=>({status:'running',needs_attention:true}),
    renderJobStatus:()=>{},renderJobArea:()=>{},renderSelection:()=>{},stopPolling:()=>stopped++,startPolling:()=>started++});
  vm.runInContext(extract(rebuild,'pollJobStatus').replace(/^function /,'async function '),context);
  await context.pollJobStatus('paper');assert.equal(stopped,1);assert.equal(started,0);
});
