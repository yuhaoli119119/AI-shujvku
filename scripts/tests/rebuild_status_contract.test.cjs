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
test('stored quality shows actionable identities/unknowns and keeps raw text inert',()=>{
  const context=vm.createContext({esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))});
  vm.runInContext(extract(detail,'storedQualityHtml'),context);
  const html=context.storedQualityHtml({counts:{error:1,unknown:2},issues_total:3,
    issues:[{severity:'error',code:'physical_page_out_of_range',kind:'source',id:'source-1',file_id:'file-1',page:99,action_zh:'<img src=x onerror=alert(1)>'}],
    table_completeness:[{complete:false,asset_id:'table-1',file_id:'file-1',physical_pages:[71,72],reason:'pending_structure'}],
    has_more:true,next_offset:1,output_revision:'a'.repeat(64)});
  assert(html.includes('错误 1')&&html.includes('未知 2')&&html.includes('file-1')&&html.includes('71, 72'));
  assert(!html.includes('<img')&&html.includes('&lt;img'));
  assert(html.includes('下一页缺项')&&html.includes('不能猜测行列')&&html.includes('全文覆盖仍须'));
});
test('quality UI performs only an explicit read and ignores stale detail responses',async()=>{
  let requests=[];const area={textContent:'',innerHTML:''};let current=true;
  const context=vm.createContext({state:{paperId:'paper',detailGeneration:1},document:{getElementById:()=>area},
    fetchJSON:async path=>{requests.push(path);return {issues:[]};},currentDetailIs:()=>current,storedQualityHtml:()=>'<p>read only</p>'});
  vm.runInContext(extract(detail,'loadStoredQuality').replace(/^function /,'async function '),context);
  assert.equal(requests.length,0);await context.loadStoredQuality();
  assert.equal(requests.length,1);assert(requests[0].startsWith('/api/rebuild/papers/paper/quality?'));
  assert.equal(area.innerHTML,'<p>read only</p>');current=false;area.innerHTML='current paper';
  await context.loadStoredQuality();assert.equal(area.innerHTML,'current paper');
});
test('needs_attention stops the frontend native status timer without treating it as success',async()=>{
  let stopped=0, started=0;
  const context=vm.createContext({state:{selectedId:'paper',selected:{}},api:async()=>({status:'running',needs_attention:true}),
    renderJobStatus:()=>{},renderJobArea:()=>{},renderSelection:()=>{},stopPolling:()=>stopped++,startPolling:()=>started++});
  vm.runInContext(extract(rebuild,'pollJobStatus').replace(/^function /,'async function '),context);
  await context.pollJobStatus('paper');assert.equal(stopped,1);assert.equal(started,0);
});
