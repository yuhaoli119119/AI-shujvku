const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.join(__dirname, '../pages/paper_detail/index.html'), 'utf8');
const helpers = html.slice(html.indexOf('    function isDefaultMainFigure('), html.indexOf('    function renderCurrentFigures('));
const files = [
  {id:'main', paper_id:'paper', role:'main'},
  {id:'si', paper_id:'paper', role:'si'},
  {id:'related', paper_id:'si-paper', role:'main'},
];
const assets = [
  {id:'whole', paper_id:'paper', file_id:'main', asset_type:'figure', figure_label:'Figure 1', page_numbers:[2], image_path:'whole.png'},
  {id:'panel', paper_id:'paper', file_id:'main', asset_type:'subfigure', figure_label:'Figure 1', subfigure_label:'a', page_numbers:[2], image_path:'panel.png', reading_explanation:{summary_zh:'轨道杂化关键词', detailed_explanation_zh:'保留的子图中文解读'}},
  {id:'panel-no-label', paper_id:'paper', file_id:'main', asset_type:'subfigure', figure_label:'Figure 2', page_numbers:[3]},
  {id:'panel-old-type', paper_id:'paper', file_id:'main', asset_type:'figure', figure_label:'Figure 3', subfigure_label:'b', page_numbers:[3]},
  {id:'si-whole', paper_id:'paper', file_id:'si', asset_type:'figure', figure_label:'Figure S1', page_numbers:[1]},
  {id:'related-si', paper_id:'si-paper', file_id:'related', asset_type:'figure', figure_label:'Figure S2', page_numbers:[2]},
  {id:'unknown-source', paper_id:'paper', file_id:'missing', asset_type:'figure', figure_label:'Figure 4', page_numbers:[4]},
];
function context() {
  const c = {state:{paperId:'paper'}, currentSourceFile:id=>files.find(f=>f.id===id), currentDisplayRole:f=>f?.role==='si'||f?.paper_id!=='paper'?'si':f?.role};
  vm.createContext(c); vm.runInContext(helpers,c); return c;
}
test('default admits only main whole assets and structured legacy whole labels; full input is preserved', () => {
  const c=context();
  c.figures=[...assets.map(a=>({_currentAsset:a})),
    {id:'legacy',paper_id:'paper',figure_label:'fig_5'},
    {id:'legacy-candidate',paper_id:'paper',figure_label:'fig_candidate_6'},
    {id:'legacy-panel',paper_id:'paper',figure_label:'Figure 5a'},
    {id:'legacy-si',paper_id:'si-paper',figure_label:'Figure 6'},
    {id:'legacy-unknown',paper_id:'paper',figure_label:'unknown',caption:'Figure 7'},
    {id:'legacy-explicit-si',paper_id:'paper',file_id:'si',figure_label:'Figure 8'}];
  const before=JSON.stringify(c.figures);
  vm.runInContext('result=selectGalleryFigures(figures, "")',c);
  expect(c.result.map(f=>f._currentAsset?.id||f.id)).toEqual(['whole','legacy','legacy-candidate']);
  expect(JSON.stringify(c.figures)).toBe(before);
  for (const q of ['轨道杂化关键词','Figure1a','Figure 1 (a)','panel']) {
    c.query=q;vm.runInContext('result=selectGalleryFigures(figures, query)',c);
    expect(c.result.some(f=>f._currentAsset?.id==='panel')).toBe(true);
  }
  vm.runInContext('result=selectGalleryFigures(figures, "FigureS1")',c);
  expect(c.result[0]._currentAsset.id).toBe('si-whole');
  vm.runInContext('result=selectGalleryFigures(figures, "")',c);
  expect(c.result).toHaveLength(3);
  expect(JSON.stringify(c.figures)).toBe(before);
});

test('real gallery search opens retained panel image, reading and PDF source; clearing restores default', async ({page}) => {
  await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,''));
  const script = html.slice(html.indexOf('    const API_BASE ='), html.lastIndexOf('</script>'));
  await page.addScriptTag({content:script});
  await page.evaluate(({assets,files})=>{
    Object.assign(state,{paperId:'paper',paper:{id:'paper',source_pdf_sha256:'main-hash',figures:[],tables:[]},rebuildAssets:assets,rebuildFiles:files,rebuildFamilyIds:['paper','si-paper'],rebuildRows:[],rebuildStatus:'ready',deepLink:{},figureSearch:''});
    renderCurrentData=()=>{};
    document.getElementById('paperContent').style.display='block';
    document.getElementById('tab-figures').classList.add('active');
    renderCurrentFigures();
  },{assets,files});
  await expect(page.locator('#figuresList .pd-figure-card')).toHaveCount(1);
  await page.locator('#pdFigureSearch').fill('轨道杂化关键词');
  await expect(page.locator('#figuresList [data-current-asset="panel"]')).toHaveCount(1);
  await page.locator('#figuresList .pd-figure-img-wrap').click();
  await expect(page.locator('#pdLightbox')).toBeVisible();
  await expect(page.locator('#pdLightboxImg')).toHaveAttribute('src',/\/api\/rebuild\/assets\/panel/);
  await expect(page.locator('#pdLightboxReadingContent')).toContainText('保留的子图中文解读');
  await expect(page.locator('#pdLightboxPdfAction a[data-current-file="main"]')).toHaveAttribute('href',/\/files\/main\/preview.*#page=2/);
  await page.locator('#pdLightbox').getByText('关闭 ✕',{exact:true}).click();
  await page.locator('#pdFigureSearch').fill('Figure1a');
  await expect(page.locator('#figuresList [data-current-asset="panel"]')).toHaveCount(1);
  await page.locator('#pdFigureSearch').fill('FigureS1');
  await expect(page.locator('#figuresList [data-current-asset="si-whole"]')).toHaveCount(1);
  await page.locator('#pdFigureSearch').fill('');
  await expect(page.locator('#figuresList .pd-figure-card')).toHaveCount(1);
  expect(await page.evaluate(()=>state.rebuildAssets.length)).toBe(assets.length);
  expect(await page.evaluate(()=>state.rebuildAssets.find(a=>a.id==='panel').reading_explanation.detailed_explanation_zh)).toBe('保留的子图中文解读');
});

test('asset deep link populates full-inventory search and targets the hidden panel', async ({page}) => {
  const c=context();
  c.window={location:{search:'?paper_id=paper&tab=figures&target_id=panel'}};
  c.URLSearchParams=URLSearchParams;c.safeReturnTo=()=>'';c.resolveDetailTab=t=>t;
  vm.runInContext(html.slice(html.indexOf('    function parseParams('),html.indexOf('    function syncAiToolLinks(')),c);
  vm.runInContext('parseParams();',c);
  expect(c.state.figureSearch).toBe('panel');
  c.figures=assets.map(a=>({_currentAsset:a}));
  vm.runInContext('result=selectGalleryFigures(figures,state.figureSearch)',c);
  expect(c.result.some(f=>f._currentAsset.id==='panel')).toBe(true);
});
