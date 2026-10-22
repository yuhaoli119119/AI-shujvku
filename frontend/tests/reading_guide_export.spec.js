const {test, expect} = require('@playwright/test');
const fs = require('fs'), path = require('path'), vm = require('vm');
const detail = fs.readFileSync(path.join(__dirname, '../pages/paper_detail/index.html'), 'utf8');
const start = detail.indexOf('    async function exportReadingGuideHtml()');
const end = detail.indexOf('\n    document.addEventListener("keydown"', start);
const exportFunction = detail.slice(start, end);

test('guide export reports the exact failed asset instead of object text or a successful download', async ({page}) => {
  const notices = [], downloads = [];
  const context = vm.createContext({
    state: {paperId: 'paper'}, API_BASE: '/api/papers', encodeURIComponent,
    showToast: message => notices.push(message),
    fetch: async () => ({ok: false, json: async () => ({detail: {
      code: 'reading_guide_projection_incomplete',
      issues: [{asset_id: 'stable-asset-uuid', code: 'image_not_found'}],
    }})}),
    URL: {createObjectURL: () => downloads.push('unexpected')},
  });
  vm.runInContext(exportFunction, context);
  await context.exportReadingGuideHtml();
  expect(notices.at(-1)).toContain('stable-asset-uuid: image_not_found');
  expect(notices.at(-1)).not.toContain('[object Object]');
  expect(downloads).toEqual([]);
  await page.setContent('<div role="status"></div>');
  await page.getByRole('status').evaluate((el, message) => el.textContent = message, notices.at(-1));
  await expect(page.getByRole('status')).toContainText('reading_guide_projection_incomplete');
});

test('guide export retains legacy string error details', async () => {
  let message;
  const context = vm.createContext({state: {paperId: 'paper'}, API_BASE: '/api/papers', encodeURIComponent,
    showToast: value => message = value,
    fetch: async () => ({ok: false, json: async () => ({detail: 'Paper not found'})}),
  });
  vm.runInContext(exportFunction, context);
  await context.exportReadingGuideHtml();
  expect(message).toBe('导出失败: Paper not found');
});
