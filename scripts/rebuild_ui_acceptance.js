const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const sessionJson = process.argv[2];
const outputDir = process.argv[3];
if (!sessionJson || !outputDir) {
  console.error("usage: node scripts/rebuild_ui_acceptance.js '<cookies-json>' <output-dir>");
  process.exit(2);
}

const cookies = JSON.parse(sessionJson);
const baseURL = process.env.BASE_URL || "http://127.0.0.1:8000";
const paperId = cookies.paperId;
if (!paperId) {
  console.error("cookies-json must include paperId");
  process.exit(2);
}

async function waitForNotContains(page, selector, text) {
  await page.waitForFunction(
    ({ selector, text }) => {
      const element = document.querySelector(selector);
      return element && !element.textContent.includes(text);
    },
    { selector, text },
    { timeout: 30000 }
  );
}

async function accept(browser, label, viewport, suffix) {
  const errors = [];
  const context = await browser.newContext({ viewport, acceptDownloads: true });
  await context.addCookies(
    cookies.cookies.map((cookie) => ({
      name: cookie.name,
      value: cookie.value,
      domain: cookies.domain || "127.0.0.1",
      path: "/",
      secure: baseURL.startsWith("https://"),
      sameSite: "Lax",
    }))
  );
  const scoped = await context.newPage();
  scoped.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  scoped.on("pageerror", (error) => errors.push(String(error)));
  const screenshots = {};

  const literature = await scoped.goto(`${baseURL}/pages/literature_library/rebuild.html`, { waitUntil: "domcontentloaded" });
  if (!literature.ok()) throw new Error(`literature HTTP ${literature.status()}`);
  await waitForNotContains(scoped, "#resultSummary", "正在读取");
  await scoped.click("#paperRows button[data-id]");
  await scoped.waitForFunction(() => !document.querySelector("#selectedPaper").textContent.includes("请选择一篇论文"));
  screenshots.literature = path.join(outputDir, `${suffix}-literature.png`);
  await scoped.screenshot({ path: screenshots.literature, fullPage: true });

  const figures = await scoped.goto(`${baseURL}/pages/figure_assets/index.html?paper_id=${paperId}`, { waitUntil: "domcontentloaded" });
  if (!figures.ok()) throw new Error(`figure assets HTTP ${figures.status()}`);
  await waitForNotContains(scoped, "#assetSummary", "请选择论文");
  await scoped.waitForSelector("#assetGrid article");
  await scoped.click("#assetGrid button[data-edit]");
  await scoped.waitForFunction(() => !document.querySelector("#editBtn").disabled);
  screenshots.figures = path.join(outputDir, `${suffix}-figure-assets.png`);
  await scoped.screenshot({ path: screenshots.figures, fullPage: true });

  const dataTable = await scoped.goto(`${baseURL}/pages/data_table/index.html`, { waitUntil: "domcontentloaded" });
  if (!dataTable.ok()) throw new Error(`data table HTTP ${dataTable.status()}`);
  await waitForNotContains(scoped, "#rowSummary", "正在读取");
  await scoped.selectOption("#reactionFilter", "CO2RR");
  await scoped.click("#applyBtn");
  await waitForNotContains(scoped, "#rowSummary", "正在读取");
  await scoped.waitForSelector("#tableBody button[data-row]");
  const debug = await scoped.evaluate(() => ({
    rows: state.rows.length,
    buttons: document.querySelectorAll("#tableBody button[data-row]").length,
    tafel: document.querySelector('#tableBody button[data-field="tafel_slope_mv_dec"]')?.textContent || null,
  }));
  console.log("data-table-debug", JSON.stringify(debug));
  const sourceButton = scoped
    .locator('#tableBody button[data-field="tafel_slope_mv_dec"]')
    .filter({ hasText: "36" })
    .first();
  await sourceButton.scrollIntoViewIfNeeded();
  await sourceButton.click();
  const modalState = await scoped.evaluate(() => document.querySelector("#sourceModal").className);
  console.log("source-modal-state", modalState);
  await scoped.waitForSelector("#sourceModal.open");
  const correctionState = await scoped.evaluate(() => {
    const form = document.querySelector("#correctionForm");
    return {
      visible: Boolean(form && form.offsetParent !== null),
      rowId: form?.dataset.rowId || "",
      fieldName: form?.dataset.fieldName || "",
    };
  });
  console.log("correction-form-state", JSON.stringify(correctionState));
  if (!correctionState.visible || !correctionState.rowId || !correctionState.fieldName) {
    throw new Error(`correction form invalid: ${JSON.stringify(correctionState)}`);
  }
  await scoped.click("#closeModal");
  screenshots.dataTable = path.join(outputDir, `${suffix}-data-table.png`);
  await scoped.screenshot({ path: screenshots.dataTable, fullPage: true });

  const analysis = await scoped.goto(`${baseURL}/pages/summary_analysis/index.html`, { waitUntil: "domcontentloaded" });
  if (!analysis.ok()) throw new Error(`summary analysis HTTP ${analysis.status()}`);
  await scoped.selectOption("#reactionFilter", "CO2RR");
  await scoped.selectOption("#dataTypeFilter", "dft");
  await scoped.selectOption("#xField", "gibbs_free_energy_cooh_ev");
  await scoped.selectOption("#yField", "limiting_potential_ratio_ucouh2");
  await scoped.click("#analyzeBtn");
  await scoped.waitForSelector("#resultArea .rebuild-metrics");
  const downloadPromise = scoped.waitForEvent("download");
  await scoped.click("#csvLink");
  const download = await downloadPromise;
  screenshots.csv = path.join(outputDir, `${suffix}-analysis.csv`);
  await download.saveAs(screenshots.csv);
  screenshots.analysis = path.join(outputDir, `${suffix}-summary-analysis.png`);
  await scoped.screenshot({ path: screenshots.analysis, fullPage: true });

  await context.close();
  return { suffix, errors, screenshots };
}

(async () => {
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const results = [];
  results.push(await accept(browser, "desktop", { width: 1440, height: 1000 }, "desktop"));
  results.push(await accept(browser, "narrow", { width: 390, height: 844 }, "narrow"));

  const anonymous = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await anonymous.newPage();
  const response = await page.goto(`${baseURL}/pages/literature_library/rebuild.html`);
  const authUrl = page.url();
  await anonymous.close();

  console.log(JSON.stringify({ results, anonymousRedirect: authUrl }, null, 2));
  await browser.close();
  if (results.some((result) => result.errors.length)) process.exitCode = 1;
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
