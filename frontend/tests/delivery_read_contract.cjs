// Isolated DOM contract only; no browser session or production endpoint.
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../pages/paper_detail/index.html'),'utf8');
const source=html.slice(html.indexOf('    let deliveryReadSequence'),html.indexOf('    function acceptanceStatusText'));
class Element{constructor(){this.children=[];this.textContent='';}appendChild(x){this.children.push(x);}replaceChildren(){this.children=[];this.textContent='';}}
const area=new Element();let resolve;
const state={paperId:'paper-1',detailGeneration:1};
const context={state,URLSearchParams,document:{getElementById:()=>area,createElement:()=>new Element()},currentDetailIs:(id,g)=>state.paperId===id&&state.detailGeneration===g,acceptanceStatusText:()=> '独立验收待核实',fetchJSON:()=>new Promise(r=>resolve=r)};
vm.createContext(context);vm.runInContext(source,context);
const response={status_layers:{content:{scope:'unknown_no_coverage_declaration'},independent_review:{}},reading:{abstract_status:'missing',guide_status:'missing'}};
(async()=>{
 let pending=context.loadCurrentDelivery('summary');state.detailGeneration=2;resolve(response);await pending;
 assert.equal(area.textContent,'正在读取当前交付…');assert.equal(area.children.length,0);
 pending=context.loadCurrentDelivery('gaps');resolve({...response,items:[{id:'<script>unsafe</script>',reason:'<img onerror=unsafe>'}],has_more:true,next_offset:25,revision:'x'});await pending;
 assert(area.children.some(x=>x.children.some(y=>y.textContent==='<script>unsafe</script>')));
 assert(area.children.some(x=>x.textContent==='下一页'));
 assert(!source.includes('innerHTML'));
 console.log('PASS: same-paper generation guard, text-only content, bounded next-page control; visual QA not performed');
})().catch(e=>{console.error(e);process.exitCode=1;});
