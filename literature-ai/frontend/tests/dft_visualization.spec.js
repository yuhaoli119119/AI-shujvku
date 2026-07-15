const { test, expect } = require("@playwright/test");

const BASE_URL = process.env.TEST_BASE_URL || "http://127.0.0.1:4173";

const fields = {
  fields: [
    { key: "li2s_adsorption_energy", label: "Li2S 吸附能", numeric: true },
    { key: "li2s_dissociation_barrier", label: "Li2S 解离能垒", numeric: true },
    { key: "li2s_bader_charge_transfer", label: "Li2S Bader 电荷转移", numeric: true },
    { key: "li_s_max_bond_length", label: "Li-S 最大键长", numeric: true },
    { key: "raw_claim_text", label: "原始文本", numeric: false },
  ],
};

function correlationPayload(url) {
  const minN = Number(url.searchParams.get("min_n"));
  const xField = url.searchParams.get("x_field");
  const yField = url.searchParams.get("y_field");
  const insufficient = minN > 3;
  const chargeBond = xField === "li2s_bader_charge_transfer";
  return {
    x_field: xField,
    y_field: yField,
    x_label: chargeBond ? "Li2S Bader 电荷转移" : "Li2S 吸附能",
    y_label: chargeBond ? "Li-S 最大键长" : "Li2S 解离能垒",
    min_n: minN,
    n: 3,
    catalyst_count: insufficient ? 2 : 3,
    paper_count: 2,
    statistics: insufficient ? {} : { pearson_r: -0.91, spearman_rho: -1, r_squared: 0.83, slope: -0.5, intercept: 2.1 },
    fit: insufficient
      ? { ready: false, reason: "样本数 3 小于最少样本数 5，未绘制拟合线。" }
      : { ready: true, line: [{ x: -2, y: 3.1 }, { x: -1, y: 2.6 }] },
    warnings: ["贡献论文较少，结果仅用于探索性比较。"],
    excluded_reasons: ["1 条记录因 X/Y 数值不可比被排除。"],
    points: [
      {
        catalyst_sample_id: "sample-fe-001",
        catalyst_name: "Fe-GDY",
        paper_code: "B0102",
        x_value: chargeBond ? 0.42 : -2.1,
        y_value: chargeBond ? 2.31 : 0.81,
        x_source_record_ids: ["dft-x-fe"],
        y_source_record_ids: ["dft-y-fe"],
        barrier_candidates: [
          { pathway: "S-S cleavage", value: 0.63, source_record_id: "barrier-fe-1" },
          { pathway: "Li2S dissociation", value: 0.81, source_record_id: "barrier-fe-2", selected_for_summary: true },
        ],
      },
      {
        catalyst_sample_id: "sample-co-002",
        catalyst_name: "Co-N4",
        paper_code: "B0078",
        x_value: chargeBond ? 0.36 : -1.7,
        y_value: chargeBond ? 2.42 : 1.02,
        x_source_record_ids: ["dft-x-co-2"],
        y_source_record_ids: ["dft-y-co-2"],
        barrier_candidates: [
          { pathway: "path A", value: 0.77, source_record_id: "barrier-co-a" },
          { pathway: "path B", value: 1.02, source_record_id: "barrier-co-b", selected_for_regression: true },
        ],
      },
      {
        catalyst_sample_id: "sample-ni-003",
        catalyst_name: "Ni-SAC",
        paper_code: "B0102",
        x_value: chargeBond ? 0.31 : -1.2,
        y_value: chargeBond ? 2.54 : 1.24,
        x_source_record_ids: ["dft-x-ni"],
        y_source_record_ids: ["dft-y-ni"],
      },
    ],
  };
}

async function installVisualMocks(page, requests) {
  await page.route("**/api/visuals/overview", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ summary: {
      total_dft_rows: 377,
      exportable_dft_rows: 375,
      v2_row_ready_numeric_rows: 320,
      distinct_exportable_catalysts: 42,
      contributing_papers: 18,
    } }),
  }));
  await page.route("**/api/visuals/analysis-fields", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(fields),
  }));
  await page.route("**/api/visuals/catalyst-correlation?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(correlationPayload(url)) });
  });
}

test.describe("DFT 数据可视化迁移", () => {
  test("topnav no longer offers DFT ML dataset and old URL is a clear migration page", async ({ page }) => {
    await page.goto(BASE_URL + "/pages/dft_ml_dataset/index.html");
    await expect(page.locator("body")).toContainText("DFT ML 数据集页面已退役");
    await expect(page.locator("body")).toContainText("数据统计已移至“数据可视化”");
    await expect(page.locator("#topnav-mount")).not.toContainText("DFT ML 数据集");
    await expect(page.locator("#refreshButton")).toHaveCount(0);
    await expect(page.locator("#v3TaskSelect")).toHaveCount(0);
  });

  test("uses backend-defined same-catalyst correlation data without the old matrix API", async ({ page }) => {
    const requests = [];
    const oldRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("correlation-pairs")) oldRequests.push(request.url());
    });
    await installVisualMocks(page, requests);
    await page.goto(BASE_URL + "/pages/visuals/index.html");

    await expect(page.locator("#metrics")).toContainText("DFT 总记录");
    await expect(page.locator("#metricDftTotal")).toHaveText("377");
    await expect(page.locator("#metricExportEligible")).toHaveText("375");
    await expect(page.locator("#metricV2Numeric")).toHaveText("320");
    await expect(page.locator("#metricCatalysts")).toHaveText("42");
    await expect(page.locator("#metricPapers")).toHaveText("18");
    await expect(page.locator("#xField")).toHaveValue("li2s_adsorption_energy");
    await expect(page.locator("#yField")).toHaveValue("li2s_dissociation_barrier");
    await expect(page.locator("#xField option")).toHaveCount(4);
    await expect.poll(() => requests.length).toBe(1);
    expect(requests[0].searchParams.get("x_field")).toBe("li2s_adsorption_energy");
    expect(requests[0].searchParams.get("y_field")).toBe("li2s_dissociation_barrier");
    expect(oldRequests).toEqual([]);
    await expect(page.locator(".correlation-cell")).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText("19×19");
    await expect(page.locator(".fit-line")).toHaveCount(1);
    await expect(page.locator("#statPearson")).toHaveText("-0.91");
    await expect(page.locator("#statSpearman")).toHaveText("-1");
    await expect(page.locator("#statR2")).toHaveText("0.83");
    await expect(page.locator("#statSlope")).toHaveText("-0.5");
    await expect(page.locator("#statIntercept")).toHaveText("2.1");
    await expect(page.locator("#warningsBox")).toContainText("贡献论文较少");
    await expect(page.locator("#excludedBox")).toContainText("数值不可比");

    const second = page.locator("#detailsBody tr").nth(1);
    await expect(second).toContainText("sample-co-002");
    await expect(second).toContainText("B0078");
    await expect(second).toContainText("dft-x-co-2");
    await expect(second).toContainText("dft-y-co-2");
    await expect(second).toContainText("path A：0.77");
    await expect(second).toContainText("path B：1.02（用于汇总/回归的最大可比能垒）");

    await page.getByRole("button", { name: "快捷关系：Li2S Bader 电荷转移 vs Li-S 最大键长" }).click();
    await expect.poll(() => requests.length).toBe(2);
    expect(requests[1].searchParams.get("x_field")).toBe("li2s_bader_charge_transfer");
    expect(requests[1].searchParams.get("y_field")).toBe("li_s_max_bond_length");
    expect(oldRequests).toEqual([]);
  });

  test("does not draw a regression line when n is below min_n", async ({ page }) => {
    const requests = [];
    await installVisualMocks(page, requests);
    await page.goto(BASE_URL + "/pages/visuals/index.html");
    await expect.poll(() => requests.length).toBe(1);
    await page.locator("#minN").fill("5");
    await page.locator("#minN").dispatchEvent("change");
    await expect.poll(() => requests.length).toBe(2);
    await expect(page.locator(".fit-line")).toHaveCount(0);
    await expect(page.locator("#fitNotice")).toContainText("样本数 3 小于最少样本数 5");
  });
});
