// 下钻探测：两个异常页面的请求构成
import { chromium } from 'playwright';

const BASE = 'http://localhost:8000';
const browser = await chromium.launch();

for (const name of ['external_analysis_workbench', 'ingestion']) {
  const page = await (await browser.newContext()).newPage();
  const rows = [];
  page.on('response', async (res) => {
    let size = 0;
    try { size = (await res.body()).length; } catch (_) {}
    rows.push({ url: res.url().replace(BASE, ''), status: res.status(), size });
  });
  await page.goto(`${BASE}/pages/${name}/index.html`, { waitUntil: 'load', timeout: 30000 });
  await page.waitForTimeout(15000);
  console.log(`\n=== ${name} ===`);
  rows.sort((a, b) => b.size - a.size);
  for (const r of rows) console.log(`${(r.size / 1024).toFixed(1).padStart(8)}KB  ${r.status}  ${r.url.slice(0, 110)}`);
  await page.context().close();
}
await browser.close();
