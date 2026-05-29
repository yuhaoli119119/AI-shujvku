const { test, expect } = require('@playwright/test');

test.describe('Layout Constraints', () => {
  test.use({ viewport: { width: 1920, height: 1080 } });

  test('writing_assistant sticky nav and layout gaps', async ({ page }) => {
    await page.goto('file:///' + __dirname.replace(/\\/g, '/') + '/../pages/writing_assistant/index.html');

    // Wait for nav to mount
    await page.waitForSelector('#topnav-mount .topnav');

    // Check gap between nav and h2
    const navBox = await page.locator('#topnav-mount').boundingBox();
    const h2Box = await page.locator('.header-container h2').boundingBox();
    
    // gap = h2 top - nav bottom
    const gap = h2Box.y - (navBox.y + navBox.height);
    expect(gap).toBeLessThan(80);
    expect(gap).toBeGreaterThanOrEqual(10); // Ensure there is some gap
    
    // Scroll down significantly
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(100);

    // Re-check nav bounding box to ensure it's sticky
    const scrolledNavBox = await page.locator('#topnav-mount').boundingBox();
    expect(scrolledNavBox.y).toBeCloseTo(0, -1); // should remain close to 0

    // Ensure main components are visible in viewport
    const btnBox = await page.locator('#btnSearch').boundingBox();
    expect(btnBox).not.toBeNull();
    expect(btnBox.y).toBeLessThan(1080); // Should be visible without scrolling

    const resultsTitleBox = await page.locator('.results-count-title').boundingBox();
    expect(resultsTitleBox).not.toBeNull();
    expect(resultsTitleBox.y).toBeLessThan(1080);
  });

  test('ai_writer sticky nav', async ({ page }) => {
    await page.goto('file:///' + __dirname.replace(/\\/g, '/') + '/../pages/ai_writer/index.html');
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
