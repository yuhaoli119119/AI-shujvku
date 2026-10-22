const {test,expect}=require('@playwright/test');
const fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'pages/paper_detail/index.html'),'utf8');
const id='c0abf59a-c8ac-4c7f-aa56-a81a0a637f1f';
const guide={one_sentence:'一句话研究结论',problem:'本文研究问题',main_finding:'主要发现与测试条件',content_scope:'main_text',guide:{research_objective:'确定反应目标',innovation:'作者提出，本文对照仅支持局部比较',limitations:'范围有限'},argument_flow:['问题→方案','证据→结论'],evidence:[{asset_id:'12345678-abcd-abcd-abcd-123456789abc',question:'哪个对照支持结论？',finding:'由已存图注支持'}],missing:['SI 图像未读取'],source_files:[{role:'main',name:'paper.pdf',complete_text:true}]};
async function boot(page,{saved=guide,job={status:'not_started'},english='Original English abstract',chinese='忠实中文摘要'}={}){
 await page.goto('http://127.0.0.1:4173/pages/paper_detail/reading-assistance.js');
 await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,''));
 await page.addScriptTag({path:path.join(root,'pages/paper_detail/reading-assistance.js')});
 await page.addScriptTag({content:html.slice(html.indexOf('    const API_BASE ='),html.lastIndexOf('</script>'))});
 await page.evaluate(({id,saved,job,english,chinese})=>{
  window.posts=[];window.gets=[];
  fetchJSON=async(url,opt)=>{if(opt?.method==='POST'){posts.push({url,body:JSON.parse(opt.body)});return {status:'running',job_id:'guide-one'}}gets.push(url);if(url==='/api/auth/me')return {csrf_token:'fixture'};return {guide:saved,abstract_zh:chinese,job};};
  renderCurrentFigures=()=>{};renderDeepLinkBar=()=>{};applyDftDeepLink=()=>{};applyStructuredDeepLink=()=>{};ensureFullDetail=()=>{};
  Object.assign(state,{paperId:id,paper:{id,title:'Test title',abstract:english,abstract_zh:chinese,reading_guide:saved?{beginner_zh:saved}:null,comprehensive_analysis:{},counts:{},authors:[],figures:[],tables:[]},deepLink:{},rebuildStatus:'ready',rebuildRows:[]});
  renderPaperDetails();
 },{id,saved,job,english,chinese});
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
