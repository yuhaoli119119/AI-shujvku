const {test, expect} = require('@playwright/test');
const base = 'http://127.0.0.1:4173';
const item = {record_id:'one',paper_id:'p',catalyst_name:'Fe-N',property_type:'adsorption_energy',value:-1,unit:'eV',title:'Evidence paper'};
test('DFT first page does not wait for summary and failed options preserve bookmarked filter', async ({page}) => {
  let releaseSummary;
  const gate = new Promise(resolve => {releaseSummary=resolve});
  const queries=[];
  await page.route('**/api/**', async route => {
    const url=new URL(route.request().url());
    if(url.pathname==='/api/libraries') return route.fulfill({json:[{name:'A',paper_count:1}]});
    if(url.pathname.endsWith('/analysis-summary')) {await gate;return route.fulfill({status:503,json:{detail:'fixture unavailable'}});}
    if(url.pathname.endsWith('/compare')) {queries.push(url.searchParams.get('catalyst_name'));return route.fulfill({json:{items:[item],total:60,has_more:true}});}
    return route.fulfill({json:{}});
  });
  await page.goto(base+'/pages/dft_database/index.html?library_name=A&catalyst=Fe-N');
  await expect(page.locator('#dftList')).toContainText('Evidence paper');
  expect(queries).toEqual(['Fe-N']);
  releaseSummary();
  await expect(page.locator('#qualityDonuts')).toContainText('加载失败');
  await expect(page.locator('#catalystName')).toHaveValue('Fe-N');
  await page.evaluate(()=>setDftPage(2));
  await expect.poll(()=>queries.length).toBe(2);
  expect(queries[1]).toBe('Fe-N');
});
test('identical concurrent DFT summaries are coalesced',async({page})=>{
  let release;const gate=new Promise(r=>release=r);let summaries=0;
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/libraries')return route.fulfill({json:[{name:'A',paper_count:1}]});
    if(path.endsWith('/analysis-summary')){summaries++;await gate;return route.fulfill({json:{total_records:1,catalyst_groups:{'Fe-N':1},exploratory_numeric_records:1}});}
    if(path.endsWith('/compare'))return route.fulfill({json:{items:[item],total:1}});
    return route.fulfill({json:{}});
  });
  await page.goto(base+'/pages/dft_database/index.html?library_name=A');
  await expect(page.locator('#dftList')).toContainText('Evidence paper');
  await page.evaluate(()=>{void loadExportSafetyOverview();});
  expect(summaries).toBe(1);release();
});
for(const width of [1280,390]) {
 test(`DFT layout stays inside ${width}px viewport`,async({page})=>{
  await page.setViewportSize({width,height:800});
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/libraries')return route.fulfill({json:[{name:'A',paper_count:1}]});
    if(path.endsWith('/analysis-summary'))return route.fulfill({json:{total_records:1,catalyst_groups:{'Fe-N':1},exploratory_numeric_records:1}});
    if(path.endsWith('/compare'))return route.fulfill({json:{items:[item],total:1}});
    return route.fulfill({json:{}});
  });
  await page.goto(base+'/pages/dft_database/index.html?library_name=A');
  await expect(page.locator('#dftList')).toContainText('Evidence paper');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:test.info().outputPath(`dft-${width}.png`),fullPage:true});
 });
}
