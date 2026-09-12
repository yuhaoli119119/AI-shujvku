const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');


function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}


function directApplyZip() {
  // A minimal valid ZIP response lets the browser exercise its native download
  // path.  The backend test independently builds and opens the real
  // PostgreSQL-generated ZIP with the same task/evidence contract.
  const name = Buffer.from('direct_apply/field_tasks.json');
  const body = Buffer.from('{"records":[{"record_id":"paper-row","fields":[{"field_name":"value"}]}]}');
  let crc = 0xffffffff;
  for (const byte of body) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  crc = (crc ^ 0xffffffff) >>> 0;
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(body.length, 18);
  local.writeUInt32LE(body.length, 22);
  local.writeUInt16LE(name.length, 26);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0);
  central.writeUInt16LE(20, 4);
  central.writeUInt16LE(20, 6);
  central.writeUInt32LE(crc, 16);
  central.writeUInt32LE(body.length, 20);
  central.writeUInt32LE(body.length, 24);
  central.writeUInt16LE(name.length, 28);
  const centralOffset = local.length + name.length + body.length;
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(1, 8);
  end.writeUInt16LE(1, 10);
  end.writeUInt32LE(central.length + name.length, 12);
  end.writeUInt32LE(centralOffset, 16);
  return Buffer.concat([local, name, body, central, name, end]);
}


test('review center exposes the direct-MCP field-verification ZIP without replacing legacy JSON review', async ({ page }) => {
  const html = read('pages/review_center/index.html');
  const script = read('pages/review_center/page.js');

  await page.goto('http://127.0.0.1:4173/pages/review_center/index.html');
  await expect(page.locator('#webAiWorkflowSelect option[value="export_dft_direct_apply"]')).toHaveText(/直接 MCP，不回传 JSON/);

  expect(html).toContain('export_dft_direct_apply');
  expect(html).toContain('直接 MCP，不回传 JSON');
  expect(script).toContain('/dft-direct-apply-bundle?include_figure_files=true');
  expect(script).toContain('downloadWebAiBundle("dft_direct_apply")');
  expect(script).toContain('return_dft');
  expect(script).toContain('/dft-review-bundle?include_figure_files=true&chart_scope=paper');
});


test('review center clicks the direct-MCP ZIP endpoint and receives an openable download', async ({ page }) => {
  const paperId = '11111111-1111-1111-1111-111111111111';
  let directRequest = null;
  await page.route('**/api/**', route => route.fulfill({ contentType: 'application/json', body: '{}' }));
  await page.route('**/api/workbench/review-center?*', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ rows: [{
      paper_id: paperId, paper_code: 'SYNTHETIC-MAIN', paper_type: 'article', title: 'Synthetic main paper',
    }], metadata: {} }),
  }));
  await page.route(`**/api/papers/${paperId}/dft-review-state`, route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ review_mode: 'paper_reviewed_aggregate', summary: {}, review_gate: { stage_status: 'completed' } }),
  }));
  await page.route(`**/api/papers/${paperId}/dft-direct-apply-bundle?include_figure_files=true`, route => {
    directRequest = { method: route.request().method(), url: route.request().url() };
    return route.fulfill({
      status: 200,
      contentType: 'application/zip',
      headers: {
        'Content-Disposition': 'attachment; filename="SYNTHETIC-MAIN_dft_direct_apply_bundle.zip"',
        'X-LitAI-Direct-Apply': 'true',
      },
      body: directApplyZip(),
    });
  });

  await page.goto('http://127.0.0.1:4173/pages/review_center/index.html');
  await page.getByRole('checkbox', { name: /选择文献 SYNTHETIC-MAIN/ }).check();
  const downloadPromise = page.waitForEvent('download');
  await page.selectOption('#webAiWorkflowSelect', 'export_dft_direct_apply');
  const download = await downloadPromise;
  const downloaded = await download.path();

  expect(directRequest).toEqual({
    method: 'POST',
    url: `http://127.0.0.1:4173/api/papers/${paperId}/dft-direct-apply-bundle?include_figure_files=true`,
  });
  expect(download.suggestedFilename()).toBe('SYNTHETIC-MAIN_dft_direct_apply_bundle.zip');
  expect(fs.readFileSync(downloaded).subarray(0, 4).toString('hex')).toBe('504b0304');
});
