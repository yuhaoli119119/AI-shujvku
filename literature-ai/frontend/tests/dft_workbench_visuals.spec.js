const { test, expect } = require("@playwright/test");

const BASE_URL = process.env.TEST_BASE_URL || "http://127.0.0.1:4173";

const mockFields = {
  fields: [
    { key: "li2s_adsorption_energy", label: "Li2S 吸附能", type: "number", unit: "eV" },
    { key: "li2s_dissociation_barrier", label: "Li2S 解离能垒", type: "number", unit: "eV" },
    { key: "li2s_bader_charge_transfer", label: "Li2S Bader 电荷转移", type: "number", unit: "e" },
    { key: "li_s_bond_max", label: "Li-S 最大键长", type: "number", unit: "Å" },
    { key: "d_band_center", label: "d 带中心", numeric: true, unit: "eV" },
    { key: "rds_delta_g", label: "决速步自由能", type: "number", unit: "eV" },
  ],
};

const mockOverview = {
  summary: {
    total_dft_rows: 377,
    exportable_dft_rows: 375,
    v2_row_ready_numeric_rows: 320,
    distinct_exportable_catalysts: 42,
    contributing_papers: 18,
  },
  descriptor_correlation: {
    variables: [
      { key: "adsorption_energy", label: "吸附能" },
      { key: "li2s_decomposition_barrier", label: "Li2S 分解能垒" },
      { key: "d_band_center", label: "d 带中心" },
      { key: "charge_transfer", label: "电荷转移" },
      { key: "work_function", label: "功函数" },
      { key: "rds_energy", label: "决速步自由能" },
    ],
    cells: [
      { x_property: "adsorption_energy", y_property: "adsorption_energy", pearson_r: 1.0, n: 42 },
      { x_property: "adsorption_energy", y_property: "li2s_decomposition_barrier", pearson_r: -0.84, n: 12 },
      { x_property: "adsorption_energy", y_property: "d_band_center", pearson_r: 0.72, n: 15 },
      { x_property: "adsorption_energy", y_property: "charge_transfer", pearson_r: -0.65, n: 10 },
      { x_property: "adsorption_energy", y_property: "work_function", pearson_r: 0.51, n: 8 },
      { x_property: "adsorption_energy", y_property: "rds_energy", pearson_r: -0.45, n: 7 },
      { x_property: "li2s_decomposition_barrier", y_property: "d_band_center", pearson_r: -0.78, n: 11 },
    ],
  },
};

const mockPairs = {
  descriptor: "d_band_center",
  target_property: "adsorption_energy",
  pearson_r: 0.72,
  n: 15,
  slope: 0.65,
  intercept: -1.2,
  points: [
    { x: -2.1, y: -2.5, catalyst_name: "Fe-N4", paper_title: "Single-atom Fe-N4 for Li-S batteries", journal: "Adv. Mater.", year: 2023, doi: "10.1002/adma.2023001" },
    { x: -1.8, y: -2.3, catalyst_name: "Co-N4", paper_title: "Co-N4 electrocatalysts in sulfur reduction", journal: "ACS Nano", year: 2022, doi: "10.1021/acsnano.202202" },
    { x: -1.5, y: -2.1, catalyst_name: "Ni-N4", paper_title: "Nickel single atoms on carbon", journal: "Nat. Commun.", year: 2023, doi: "10.1038/s41467.202303" },
  ],
};

const mockCatalystDataset = {
  total: 2,
  rows: [
    {
      catalyst_sample_id: "samp-001",
      catalyst_name: "Fe-GDY",
      paper_code: "B0102",
      doi: "10.1002/anie.20230102",
      li2s_adsorption_energy: -2.15,
      li2s_dissociation_barrier: 0.82,
      li2s_bader_charge_transfer: 0.42,
      li1_s_bond_length: 2.12,
      li2_s_bond_length: 2.18,
      li_s_bond_max: 2.31,
      d_band_center: -1.85,
      rds_delta_g: 0.35,
    },
    {
      catalyst_sample_id: "samp-002",
      catalyst_name: "Co-N4",
      paper_code: "B0078",
      doi: "10.1021/acsnano.2022078",
      li2s_adsorption_energy: -1.72,
      li2s_dissociation_barrier: 1.05,
      li2s_bader_charge_transfer: 0.36,
      li1_s_bond_length: 2.22,
      li2_s_bond_length: 2.25,
      li_s_bond_max: 2.42,
      d_band_center: -1.45,
      rds_delta_g: 0.48,
    },
  ],
};

test.describe("DFT 数据分析 3-Tab 工作台交互测试", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/libraries", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([
        { name: "锂硫双原子催化剂", is_active: true, paper_count: 24 },
        { name: "单原子催化剂库", is_active: false, paper_count: 15 },
      ]),
    }));

    await page.route("**/api/visuals/overview*", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(mockOverview),
    }));

    await page.route("**/api/visuals/analysis-fields*", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(mockFields),
    }));

    await page.route("**/api/visuals/correlation-pairs*", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(mockPairs),
    }));

    await page.route("**/api/dft/catalyst-dataset?*", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(mockCatalystDataset),
    }));
  });

  test("切换到相关性矩阵 Tab 并下钻联动关系探索", async ({ page }) => {
    await page.goto(BASE_URL + "/pages/visuals/index.html");

    // 1. 点击切换到「相关性矩阵」Tab
    await page.locator("#tabNavMatrix").click();
    await expect(page.locator("#tabNavMatrix")).toHaveClass(/is-active/);
    await expect(page.locator("#tabMatrix")).toBeVisible();
    await expect(page.locator("#tabRelations")).toBeHidden();

    // 2. 检查矩阵表头和单元格渲染
    await expect(page.locator("#badgeVarCount")).toContainText("6 个变量");
    await expect(page.locator("#matrixTable")).toBeVisible();
    const cells = page.locator(".heatmap-cell");
    await expect(cells).not.toHaveCount(0);

    // 3. 点击一个非对角线单元格进行下钻
    const activeCell = page.locator('.heatmap-cell:not(.is-diagonal):not(.is-insufficient)').first();
    await activeCell.click();
    await expect(activeCell).toHaveClass(/is-active-cell/);

    // 4. 检查下钻卡片渲染
    await expect(page.locator("#matrixDetailCard")).toBeVisible();
    await expect(page.locator("#detailBadgeR")).toBeVisible();
    await expect(page.locator("#detailBadgeN")).toBeVisible();
    await expect(page.locator("#interpretText")).toContainText("相关趋势");
    await expect(page.locator("#matrixPaperList li")).not.toHaveCount(0);

    // 5. 点击「进入关系探索」联动跳转回 Tab 1
    await page.locator("#jumpToExploreBtn").click();
    await expect(page.locator("#tabNavRelations")).toHaveClass(/is-active/);
    await expect(page.locator("#tabRelations")).toBeVisible();
    await expect(page.locator("#tabMatrix")).toBeHidden();
  });

  test("切换到 ML 数据集 Tab 并验证特征规范与数据预览", async ({ page }) => {
    await page.goto(BASE_URL + "/pages/visuals/index.html");

    // 1. 点击切换到「ML 数据集」Tab
    await page.locator("#tabNavDataset").click();
    await expect(page.locator("#tabNavDataset")).toHaveClass(/is-active/);
    await expect(page.locator("#tabDataset")).toBeVisible();
    await expect(page.locator("#tabRelations")).toBeHidden();

    // 2. 检查顶部 5 个 KPI 指标卡
    await expect(page.locator("#dsTotalDft")).toHaveText("377");
    await expect(page.locator("#dsExportableDft")).toHaveText("375");
    await expect(page.locator("#dsNumericRate")).toHaveText("85.3%");
    await expect(page.locator("#dsCatalystCount")).toHaveText("42");
    await expect(page.locator("#dsContributingPapers")).toHaveText("18");

    // 3. 检查特征规范表格
    await expect(page.locator("#featuresCountBadge")).toContainText("6 个特征");
    const featureRows = page.locator("#featuresBody tr");
    await expect(featureRows).toHaveCount(6);
    await expect(featureRows.first()).toContainText("Li₂S 吸附能");

    // 4. 检查数据宽表预览
    const previewRows = page.locator("#datasetPreviewBody tr");
    await expect(previewRows).toHaveCount(2);
    await expect(previewRows.nth(0)).toContainText("Fe-GDY");
    await expect(previewRows.nth(1)).toContainText("Co-N₄");

    // 5. 检查导出卡片存在
    await expect(page.locator("#exportCsvCardBtn")).toBeVisible();
    await expect(page.locator("#exportJsonCardBtn")).toBeVisible();
  });
});
