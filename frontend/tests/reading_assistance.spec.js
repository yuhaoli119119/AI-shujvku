const {test,expect}=require('@playwright/test');
const fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'pages/paper_detail/index.html'),'utf8');
const id='c0abf59a-c8ac-4c7f-aa56-a81a0a637f1f';
const guide={one_sentence:'一句话研究结论',problem:'本文研究问题',main_finding:'主要发现与测试条件',content_scope:'main_text',guide:{research_objective:'确定反应目标',innovation:'作者提出，本文对照仅支持局部比较',limitations:'范围有限'},argument_flow:['问题→方案','证据→结论'],evidence:[{asset_id:'12345678-abcd-abcd-abcd-123456789abc',question:'哪个对照支持结论？',finding:'由已存图注支持'}],missing:['SI 图像未读取'],source_files:[{role:'main',name:'paper.pdf',complete_text:true}]};
async function boot(page,{saved=guide,job={status:'not_started'},english='Original English abstract',chinese='忠实中文摘要',responses=[]}={}){
 await page.goto('http://127.0.0.1:4173/pages/paper_detail/reading-assistance.js');
 await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,''));
 await page.addScriptTag({path:path.join(root,'pages/paper_detail/reading-assistance.js')});
 await page.addScriptTag({content:html.slice(html.indexOf('    const API_BASE ='),html.lastIndexOf('</script>'))});
 await page.evaluate(({id,saved,job,english,chinese,responses})=>{
  window.posts=[];window.gets=[];window.readIndex=0;
  fetchJSON=async(url,opt)=>{if(opt?.method==='POST'){posts.push({url,body:JSON.parse(opt.body)});return {status:'running',job_id:'guide-one'}}gets.push(url);if(url==='/api/auth/me')return {csrf_token:'fixture'};const next=responses[window.readIndex++];if(next?.error)throw Error(next.error);if(next?.hang)return new Promise((_,reject)=>opt.signal.addEventListener('abort',()=>reject(Error('request timed out'))));return {guide:saved,abstract_zh:chinese,job,...next};};
  renderCurrentFigures=()=>{};renderDeepLinkBar=()=>{};applyDftDeepLink=()=>{};applyStructuredDeepLink=()=>{};ensureFullDetail=()=>{};
  Object.assign(state,{paperId:id,paper:{id,title:'Test title',abstract:english,abstract_zh:chinese,reading_guide:saved?{beginner_zh:saved}:null,comprehensive_analysis:{},counts:{},authors:[],figures:[],tables:[]},deepLink:{},rebuildStatus:'ready',rebuildRows:[]});
  renderPaperDetails();
 },{id,saved,job,english,chinese,responses});
}

test('saved Chinese defaults, English toggle, progressive guide and scoped evidence links',async({page})=>{
 await boot(page);await expect(page.locator('#paperAbstract')).toHaveText('忠实中文摘要');
 await page.getByRole('button',{name:'查看英文原摘要'}).click();await expect(page.locator('#paperAbstract')).toHaveText('Original English abstract');
 await page.getByRole('button',{name:'查看中文摘要'}).click();await expect(page.locator('#paperAbstract')).toHaveText('忠实中文摘要');
 await expect(page.locator('#pdQuickReading')).toContainText('一句话研究结论');await expect(page.locator('#pdReadingScope')).toContainText('正文文字');
 await page.getByText('研究目标',{exact:true}).click();await expect(page.locator('#pdBeginnerGuide')).toContainText('确定反应目标');
 await page.getByText('关键证据 · 1 个图表',{exact:true}).click();
 const link=page.getByRole('link',{name:'查看对应图表与来源 →'});await expect(link).toHaveAttribute('href',new RegExp('tab=figures.*target_id=12345678'));
 expect(await page.evaluate(()=>posts.length)).toBe(0);
});

test('partial/missing guide and failure allow independent retry without science calls',async({page})=>{
 await boot(page,{saved:null,chinese:'',job:{status:'failed',can_retry:true,error:'服务暂不可用，已有数据保留'}});
 await expect(page.locator('#paperAbstract')).toHaveText('Original English abstract');await expect(page.locator('#pdAbstractNote')).toContainText('中文译文待补充');
 await expect(page.locator('#pdQuickReading')).toBeHidden();await expect(page.locator('#pdReadingJob')).toContainText('已有数据保留');
 await page.getByRole('button',{name:'重试中文导读'}).click();
 const calls=await page.evaluate(()=>posts);expect(calls).toHaveLength(1);expect(calls[0].url).toContain('/reading-assistance/jobs');
 expect(calls.some(c=>/rows\/import|ai-extract/.test(c.url))).toBe(false);
});

test('persisted text and adversarial model strings remain text, not executable markup',async({page})=>{
 const evil='<img src=x onerror="window.pwned=true"><script>window.pwned=true</script>';
 await boot(page,{saved:{...guide,one_sentence:evil,guide:{research_objective:evil},terms:[{term:evil,explanation:evil}],evidence:[{asset_id:'javascript:alert(1)',question:evil,finding:evil}]},chinese:evil});
 await expect(page.locator('#pdQuickReading')).toContainText(evil);expect(await page.evaluate(()=>window.pwned)).toBeUndefined();
 expect(await page.locator('#pdBeginnerGuide img,#pdQuickReading img,#pdBeginnerGuide script').count()).toBe(0);
 expect(await page.locator('#pdBeginnerGuide a[href^="javascript:"]').count()).toBe(0);
});

test('overview stays selected when data loads, explicit tab remains usable and diagnostics are collapsed',async({page})=>{
 await boot(page);
 await page.evaluate(()=>{EditableDataGrid={mount:()=>{}};renderSupplementaryFiles=()=>{};installHistoryTransferButtons=()=>{};state.rebuildRows=[{id:'row',data_type:'dft',values:[]}];state.defaultDataTabApplied=false;switchDetailTab('summary');renderCurrentData();});
 await expect(page.locator('#tab-summary')).toHaveClass(/active/);await expect(page.locator('#pdParsingDetails')).not.toHaveAttribute('open','');
 await page.locator('#pdParsingDetails>summary').click();await expect(page.locator('#pdAcceptanceStatus')).toBeVisible();
 await page.evaluate(()=>switchDetailTab('dft'));await expect(page.locator('#tab-dft')).toHaveClass(/active/);
});


test('one generation progresses through saved-running to terminal without reload',async({page})=>{
 await page.clock.install();
 await boot(page,{saved:null,responses:[
  {job:{status:'not_started'}},
  {guide:null,job:{status:'running',saved:false}},
  {guide,job:{status:'running',saved:true}},
  {guide,job:{status:'completed',saved:true}}
 ]});
 await page.locator('#pdGenerateReading').click();
 await expect(page.locator('#pdReadingJob')).toContainText('生成中');
 await expect(page.locator('#pdQuickReading')).toBeHidden();
 await page.clock.runFor(6000);
 await expect(page.locator('#pdQuickReading')).toContainText('一句话研究结论');
 await expect(page.locator('#pdReadingJob')).toContainText('已保存，可以阅读；任务正在收尾');
 await expect(page.locator('#pdGenerateReading')).toBeDisabled();
 await page.getByText('研究目标',{exact:true}).click();
 await page.getByRole('button',{name:'查看英文原摘要'}).click();
 await page.clock.runFor(8000);
 await expect(page.locator('#paperAbstract')).toHaveText('Original English abstract');
 await expect(page.locator('#pdBeginnerGuide details').filter({hasText:'确定反应目标'})).toHaveAttribute('open','');
 await expect(page.locator('#pdReadingJob')).toHaveText('中文导读已保存。');
 await expect(page.locator('#pdGenerateReading')).toBeEnabled();
 expect(await page.evaluate(()=>posts.length)).toBe(1);
 const reads=await page.evaluate(()=>readIndex);await page.clock.runFor(120000);
 expect(await page.evaluate(()=>readIndex)).toBe(reads);
});

test('transient polling failure recovers saved content and terminal state with no new POST',async({page})=>{
 await page.clock.install();
 await boot(page,{saved:null,responses:[
  {job:{status:'not_started'}},{job:{status:'running',saved:false}},
  {error:'temporary network disconnect'},
  {guide,job:{status:'running',saved:true}},
  {guide,job:{status:'completed',saved:true}}
 ]});
 await page.locator('#pdGenerateReading').click();
 await page.clock.runFor(6000);
 await expect(page.locator('#pdReadingJob')).toContainText('暂时无法读取状态');
 await expect(page.locator('#pdGenerateReading')).toBeDisabled();
 await page.clock.runFor(6000);
 await expect(page.locator('#pdQuickReading')).toContainText('一句话研究结论');
 await expect(page.locator('#pdReadingJob')).toContainText('任务正在收尾');
 await page.clock.runFor(10000);
 await expect(page.locator('#pdReadingJob')).toHaveText('中文导读已保存。');
 await expect(page.locator('#pdGenerateReading')).toBeEnabled();
 expect(await page.evaluate(()=>posts.length)).toBe(1);
});

test('repeated read errors stop with a read-only manual recovery, preserving the running job',async({page})=>{
 await page.clock.install();
 await boot(page,{responses:[{guide,job:{status:'running',saved:true}},
  ...Array.from({length:4},()=>({error:'offline'})),
  {guide,job:{status:'completed',saved:true}}]});
 await page.clock.runFor(6000);await page.clock.runFor(6000);await page.clock.runFor(12000);await page.clock.runFor(24000);
 await expect(page.locator('#pdReadingJob')).toContainText('自动核对已暂停');
 await expect(page.locator('#pdQuickReading')).toContainText('一句话研究结论');
 await expect(page.locator('#pdGenerateReading')).toBeDisabled();
 const reads=await page.evaluate(()=>readIndex);await page.clock.runFor(120000);
 expect(await page.evaluate(()=>readIndex)).toBe(reads);
 await page.getByRole('button',{name:'重新核对状态'}).click();
 await expect(page.locator('#pdReadingJob')).toHaveText('中文导读已保存。');
 await expect(page.locator('#pdGenerateReading')).toBeEnabled();
 expect(await page.evaluate(()=>posts.length)).toBe(0);
});

test('a long-running saved job has bounded polling and manual check never declares completion',async({page})=>{
 await page.clock.install();await boot(page,{job:{status:'running',saved:true}});
 for(let n=0;n<40;n++) await page.clock.runFor(15000);
 expect(await page.evaluate(()=>readIndex)).toBe(41);
 await expect(page.locator('#pdReadingJob')).toContainText('自动核对已暂停');
 const reads=await page.evaluate(()=>readIndex);await page.clock.runFor(120000);
 expect(await page.evaluate(()=>readIndex)).toBe(reads);
 await page.getByRole('button',{name:'重新核对状态'}).click();
 await expect(page.locator('#pdReadingJob')).toContainText('任务正在收尾');
 await expect(page.locator('#pdGenerateReading')).toBeDisabled();
 expect(await page.evaluate(()=>posts.length)).toBe(0);
});


test('an unresponsive GET times out and recovers with a read-only retry',async({page})=>{
 await page.clock.install();
 await boot(page,{responses:[{hang:true},{guide,job:{status:'completed',saved:true}}]});
 await page.clock.runFor(20000);
 await expect(page.locator('#pdReadingJob')).toContainText('暂时无法读取状态');
 await page.clock.runFor(6000);
 await expect(page.locator('#pdReadingJob')).toHaveText('中文导读已保存。');
 await expect(page.locator('#pdGenerateReading')).toBeEnabled();
 expect(await page.evaluate(()=>posts.length)).toBe(0);
});
