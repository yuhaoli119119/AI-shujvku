const { test, expect } = require('@playwright/test');

test.describe('Layout Constraints', () => {
  test.use({ viewport: { width: 1920, height: 1080 } });

  test('ai_writer sticky nav', async ({ page }) => {
    await page.goto('http://127.0.0.1:4173/pages/ai_writer/index.html');
    await page.waitForSelector('#topnav-mount .topnav');

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(100);

    const scrolledNavBox = await page.locator('#topnav-mount').boundingBox();
    expect(scrolledNavBox.y).toBeCloseTo(0, -1);
  });
  
  test('literature_library sticky nav', async ({ page }) => {
    await page.goto('file:///' + __dirname.replace(/\\/g, '/') + '/../pages/literature_library/index.html');
    await page.waitForSelector('#topnav-mount .topnav');

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(100);

    const scrolledNavBox = await page.locator('#topnav-mount').boundingBox();
    expect(scrolledNavBox.y).toBeCloseTo(0, -1);
  });
});
