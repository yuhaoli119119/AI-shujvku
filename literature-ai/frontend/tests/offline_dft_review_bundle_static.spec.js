const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');


function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}


test('literature detail hides the ambiguous offline DFT review bundle export entry', async () => {
  const html = read('pages/literature_library/index.html');
  const actions = read('pages/literature_library/page-actions.js');

  expect(html).not.toContain('导出 AI 核验包');
  expect(html).not.toContain('onclick="exportSelectedDftReviewBundle()"');
  expect(actions).toContain('/dft-review-bundle');
  expect(actions).toContain('{ method: "POST" }');
  expect(actions).toContain('服务器未长期保存该压缩包');
});
