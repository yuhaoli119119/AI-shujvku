const { test, expect } = require('@playwright/test');
const base = 'http://127.0.0.1:4173';
const fields = ['li2s_adsorption_energy', 'li2s_dissociation_barrier', 'li2s_bader_charge_transfer'];
const cells = fields.flatMap((x, i) => fields.slice(i + 1).map(y => ({x_property:x,y_property:y,pearson_r:0.8,n:3})));
const pairPayload = marker => ({ready:true,pearson_r:0.8,n:3,points:[{x:1,y:2,paper_title:marker,paper_id:marker},{x:2,y:3,paper_title:marker,paper_id:marker},{x:3,y:4,paper_title:marker,paper_id:marker}]});
async function setup(page, handler) {
  const requests = [];
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); requests.push(url.pathname);
    let data = {};
    if (url.pathname === '/api/libraries') data = [{name:'测试库',is_active:true}];
    else if (url.pathname.endsWith('/analysis-fields')) data = {fields:fields.map(key=>({key,label:key,numeric:true,type:'number'}))};
    else if (url.pathname.endsWith('/overview')) data = {descriptor_correlation:{variables:fields.map(key=>({key})),cells}};
    else if (url.pathname.endsWith('/correlation-pairs')) {
      if (handler && await handler(route, url)) return;
      data = pairPayload('initial-paper');
    } else if (url.pathname.endsWith('/data-revision')) data = {source:'rebuild',revision:'revision-1'};
    await route.fulfill({json:data});
  });
  await page.goto(base + '/pages/visuals/index.html?library_name=测试库&tab=matrix');
  await expect(page.locator('#miniPointsLayer circle')).toHaveCount(3);
  return requests;
}
test('matrix entry loads only visible analysis and refresh keeps selected pair', async ({page}) => {
  let count = 0;
  const requests = await setup(page, async (route) => { count++; await route.fulfill({json:pairPayload('paper-'+count)}); return true; });
  expect(requests).not.toContain('/api/visuals/catalyst-correlation');
  const cell = page.locator('.heatmap-cell.is-ready-cell').nth(1);
  await cell.click();
  await expect(page.locator('#matrixDetailCard')).toHaveAttribute('aria-busy','false');
  const title = await page.locator('#detailPairTitle').textContent();
  await page.locator('#refreshAnalysis').click();
  await expect(page.locator('#detailPairTitle')).toHaveText(title);
  await expect(page.locator('.heatmap-cell.is-active-cell')).toHaveCount(1);
  await expect(page.locator('#matrixDetailCard')).toHaveAttribute('aria-busy','false');
  expect(count).toBe(3); // Full response refresh issues one new pair request.
});
test('refresh mini chart fetches fresh data rather than cached response', async ({page}) => {
  let count = 0;
  await setup(page, async route => { count++; await route.fulfill({json:pairPayload('fresh-paper-'+count)}); return true; });
  await expect(page.locator('#matrixPaperList')).toContainText('fresh-paper-1');
  const response = page.waitForResponse(r => r.url().includes('correlation-pairs'));
  await page.getByRole('button',{name:'刷新小图'}).click();
  await response;
  await expect(page.locator('#matrixPaperList')).toContainText('fresh-paper-2');
  expect(count).toBe(2);
});
test('rapid pair clicks clear stale chart immediately and ignore late failed request', async ({page}) => {
  let mode = 'initial';
  let releaseOld;
  const oldGate = new Promise(resolve => {releaseOld=resolve});
  let oldEntered;
  const entered = new Promise(resolve=>{oldEntered=resolve});
  let releaseNew;
  const newGate = new Promise(resolve=>{releaseNew=resolve});
  await setup(page, async route => {
    if (mode === 'slow-old') {
      oldEntered(); await oldGate;
      await route.fulfill({status:500,json:{detail:'OLD FAILURE'}}).catch(()=>{}); return true;
    }
    if (mode === 'slow-new') { await newGate; await route.fulfill({json:pairPayload('latest-paper')}); return true; }
    return false;
  });
  mode='slow-old';
  await page.locator('.heatmap-cell.is-ready-cell').nth(1).click();
  await entered;
  await expect(page.locator('#miniPointsLayer circle')).toHaveCount(0);
  await expect(page.locator('#rangeXValue')).toHaveText('—');
  await expect(page.locator('#matrixPaperList li')).toHaveCount(0);
  mode='slow-new';
  await page.locator('.heatmap-cell.is-ready-cell').nth(3).click();
  releaseOld(); releaseNew();
  await expect(page.locator('#matrixPaperList')).toContainText('latest-paper');
  await expect(page.locator('#matrixDetailCard')).toHaveAttribute('aria-busy','false');
  await expect(page.locator('#interpretText')).not.toContainText('OLD FAILURE');
});
