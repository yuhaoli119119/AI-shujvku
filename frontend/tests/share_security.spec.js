const { test, expect } = require('@playwright/test');

test('share view renders stored XSS payloads as inert text', async ({ page }) => {
  const payload = '<img src=x onerror="window.__xss=(window.__xss||0)+1">';
  const paperId = '11111111-1111-1111-1111-111111111111';
  await page.addInitScript(() => { window.__xss = 0; });
  await page.route('**/api/share/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/papers')) {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [{ id: paperId, title: payload, authors: [payload], journal: payload, year: 2026 }] }),
      });
    }
    if (path.includes('/notes/')) {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [{ source: payload, field_name: payload, section_title: payload, content: payload, quoted_text: payload, page: 1 }] }),
      });
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [] }) });
  });

  await page.goto('http://127.0.0.1:8000/pages/share/index.html?token=test-token');
  await expect(page.locator('.card h3')).toContainText('<img src=x');
  await expect(page.locator('.card img')).toHaveCount(0);
  await page.locator('[data-tab="notes"]').click();
  await page.locator('[data-action="notes"]').click();
  await expect(page.locator('.note-content')).toContainText('<img src=x');
  await expect(page.locator('.note-content img')).toHaveCount(0);
  expect(await page.evaluate(() => window.__xss)).toBe(0);
});
