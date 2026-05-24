const { test, expect } = require('@playwright/test');

const PAGES = [
    { name: 'Dashboard', path: '/pages/dashboard/index.html', coreSelector: '.panel-card' },
    { name: 'Ingestion Center', path: '/pages/ingestion/index.html', coreSelector: '.dropzone' },
    { name: 'Literature Library', path: '/pages/literature_library/index.html', coreSelector: '#paperList' },
    { name: 'Paper Detail', path: '/pages/paper_detail/index.html', coreSelector: '.panel-card' },
    { name: 'DFT Database', path: '/pages/dft_database/index.html', coreSelector: '#dftTable' },
    { name: 'AI Writing Studio', path: '/pages/ai_writer/index.html', coreSelector: '#paperChecklist' },
    { name: 'Settings', path: '/pages/settings/index.html', coreSelector: '.field' }
];

const VIEWPORTS = [
    { width: 1440, height: 900 },
    { width: 1280, height: 800 },
    { width: 1024, height: 768 }
];

test.describe('Literature AI Front-end Smoke Tests', () => {
    let consoleErrors = [];

    test.beforeEach(({ page }) => {
        consoleErrors = [];
        page.on('console', msg => {
            if (msg.type() === 'error') {
                consoleErrors.push(msg.text());
            }
        });
        page.on('pageerror', err => {
            consoleErrors.push(err.message);
        });
    });

    for (const pageInfo of PAGES) {
        test.describe(`Page: ${pageInfo.name}`, () => {
            for (const viewport of VIEWPORTS) {
                test(`renders correctly at ${viewport.width}x${viewport.height}`, async ({ page }) => {
                    await page.setViewportSize(viewport);
                    
                    // Go to page
                    const url = `http://localhost:8000${pageInfo.path}`;
                    const response = await page.goto(url);
                    
                    // Expect page to load successfully
                    expect(response.status()).toBe(200);

                    // Wait for main elements to load
                    await page.waitForTimeout(1000);

                    // Verify that the core component exists on the page
                    const coreElement = page.locator(pageInfo.coreSelector);
                    await expect(coreElement.first()).toBeVisible();

                    // Verify no red console errors exist
                    expect(consoleErrors).toEqual([]);
                });
            }

            test(`core interactions and buttons work`, async ({ page }) => {
                await page.setViewportSize({ width: 1280, height: 800 });
                const url = `http://localhost:8000${pageInfo.path}`;
                await page.goto(url);
                await page.waitForTimeout(1000);

                // Specific page action validations
                if (pageInfo.name === 'Dashboard') {
                    // Click the refresh button or verify quick links
                    const quickLink = page.locator('a.action-btn').first();
                    await expect(quickLink).toBeVisible();
                } else if (pageInfo.name === 'Ingestion Center') {
                    // Switch tabs
                    await page.click('button.ingest-tab:has-text("DOI")');
                    await expect(page.locator('#tab-doi')).toBeVisible();

                    await page.click('button.ingest-tab:has-text("在线文献检索")');
                    await expect(page.locator('#tab-online')).toBeVisible();
                } else if (pageInfo.name === 'Literature Library') {
                    // Switch library tabs
                    await page.click('button.tab-btn:has-text("内部 AI 整理归纳")');
                    await expect(page.locator('#tab-writer')).toBeVisible();

                    await page.click('button.tab-btn:has-text("外部 AI 审核")');
                    await expect(page.locator('#tab-review')).toBeVisible();
                } else if (pageInfo.name === 'DFT Database') {
                    // Click export CSV button
                    const csvBtn = page.locator('button:has-text("导出 CSV")');
                    await expect(csvBtn).toBeVisible();
                } else if (pageInfo.name === 'AI Writing Studio') {
                    // Click generate outline or load package
                    const packBtn = page.locator('button:has-text("生成整理结果"), button:has-text("撰写学术草稿")');
                    await expect(packBtn.first()).toBeVisible();
                } else if (pageInfo.name === 'Settings') {
                    // Switch settings tabs
                    await page.click('button:has-text("IDE 连接")');
                    await expect(page.locator('#section-ide')).toBeVisible();

                    await page.click('button:has-text("主题外观")');
                    await expect(page.locator('#section-theme')).toBeVisible();
                }

                // Verify console remains clean after clicking
                expect(consoleErrors).toEqual([]);
            });
        });
    }
});
