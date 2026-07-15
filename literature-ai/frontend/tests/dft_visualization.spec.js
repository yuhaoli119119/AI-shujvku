const { test, expect } = require("@playwright/test");

const BASE_URL = process.env.TEST_BASE_URL || "http://127.0.0.1:4173";

const fields = {
  fields: [
    { key: "li2s_adsorption_energy", label: "Li2S 吸附能", type: "number" },
    { key: "li2s_dissociation_barrier", label: "Li2S 解离能垒", type: "number" },
    { key: "li2s_bader_charge_transfer", label: "Li2S Bader 电荷转移", type: "number" },
    { key: "li_s_bond_max", label: "Li-S 最大键长", type: "number" },
    { key: "d_band_center", label: "d 带中心", numeric: true },
    { key: "catalyst_name", label: "催化剂名称", type: "string" },
    { key: "paper_id", label: "论文 ID", type: "string" },
  ],
};

function point(id, catalyst, paperCode, xValue, yValue, includeBarriers = false) {
  return {
    catalyst_sample_id: id,
    catalyst_name: catalyst,
    paper: { paper_id: "paper-" + id, paper_code: paperCode },
    x: { value: xValue, source_record_ids: ["nested-x-" + id], candidates: [] },
    y: {
      value: yValue,
      source_record_ids: ["nested-y-" + id],
      candidates: includeBarriers ? [
        { pathway: "path A", value: 0.77, source_record_id: "barrier-" + id + "-a" },
        { pathway: "path B", value: 1.02, source_record_id: "barrier-" + id + "-b", selected_for_regression: true },
      ] : [],
    },
    x_source_record_ids: ["top-x-" + id],
    y_source_record_ids: ["top-y-" + id],
  };
}

function correlationPayload(url) {
  const minN = Number(url.searchParams.get("min_n"));
  const xField = url.searchParams.get("x_field");
  const yField = url.searchParams.get("y_field");
  const insufficient = minN > 3;
  const chargeBond = xField === "li2s_bader_charge_transfer";
  const points = chargeBond
    ? [
      point("sample-fe-001", "Fe-GDY", "B0102", 0.42, 2.31),
      point("sample-co-002", "Co-N4", "B0078", 0.36, 2.42),
      point("sample-ni-003", "Ni-SAC", "B0102", 0.31, 2.54),
    ]
    : [
      point("sample-fe-001", "Fe-GDY", "B0102", -2.1, 0.81, true),
      point("sample-co-002", "Co-N4", "B0078", -1.7, 1.02, true),
      point("sample-ni-003", "Ni-SAC", "B0102", -1.2, 1.24),
    ];
  return {
    x_field: xField,
    y_field: yField,
    x_label: chargeBond ? "Li2S Bader 电荷转移" : "Li2S 吸附能",
    y_label: chargeBond ? "Li-S 最大键长" : "Li2S 解离能垒",
    min_n: minN,
    n: 3,
    n_catalysts: insufficient ? 2 : 3,
    n_papers: 2,
    ready: !insufficient,
    statistics: insufficient
      ? { pearson_r: null, spearman_rho: null, r_squared: null, slope: null, intercept: null }
      : { pearson_r: -0.91, spearman_rho: -1, r_squared: 0.83, slope: -0.5, intercept: 2.1 },
    insufficient_reason: insufficient ? "样本数 3 小于最少样本数 5，未绘制拟合线。" : null,
    warnings: insufficient ? ["min_n_not_reached", "fewer_than_two_contributing_papers"] : [],
    excluded_count: 12,
    excluded_reasons: { identity_v2_required: 1290, missing_catalyst_sample_id: 567, context_mismatch: 12 },
    points,
  };
}

async function installVisualMocks(page, correlationRequests) {
  await page.route("**/api/libraries", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([
      { name: "Other Library", is_active: false },
      { name: "Active Library", is_active: true },
    ]),
  }));
  await page.route("**/api/visuals/overview*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ summary: {
      total_dft_rows: 377,
      exportable_dft_rows: 375,
      v2_row_ready_numeric_rows: 320,
      distinct_exportable_catalysts: 42,
      contributing_papers: 18,
    } }),
  }));
  await page.route("**/api/visuals/analysis-fields*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(fields),
  }));
  await page.route("**/api/visuals/catalyst-correlation?*", (route) => {
    const url = new URL(route.request().url());
    correlationRequests.push(url);
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

  test("uses backend-defined nested same-catalyst data and forwards library_name", async ({ page }) => {
    const correlationRequests = [];
    const visualRequests = [];
    const oldRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/visuals/")) visualRequests.push(new URL(request.url()));
      if (request.url().includes("correlation-pairs")) oldRequests.push(request.url());
    });
    await installVisualMocks(page, correlationRequests);
    await page.goto(BASE_URL + "/pages/visuals/index.html?library_name=Selected%20Library");

    await expect(page.locator("#metrics")).toContainText("DFT 总记录");
    await expect(page.locator("#metricDftTotal")).toHaveText("377");
    await expect(page.locator("#metricExportEligible")).toHaveText("375");
    await expect(page.locator("#metricV2Numeric")).toHaveText("320");
    await expect(page.locator("#metricCatalysts")).toHaveText("42");
    await expect(page.locator("#metricPapers")).toHaveText("18");
    await expect(page.locator("#xField")).toHaveValue("li2s_adsorption_energy");
    await expect(page.locator("#yField")).toHaveValue("li2s_dissociation_barrier");
    await expect(page.locator("#xField option")).toHaveCount(5);
    await expect(page.locator("#xField")).not.toContainText("催化剂名称");
    await expect(page.locator("#xField")).not.toContainText("论文 ID");
    await expect.poll(() => correlationRequests.length).toBe(1);
    expect(correlationRequests[0].searchParams.get("x_field")).toBe("li2s_adsorption_energy");
    expect(correlationRequests[0].searchParams.get("y_field")).toBe("li2s_dissociation_barrier");
    expect(correlationRequests[0].searchParams.get("library_name")).toBe("Selected Library");
    await expect.poll(() => visualRequests.filter((url) => url.pathname === "/api/visuals/overview").length).toBe(1);
    expect(visualRequests.find((url) => url.pathname === "/api/visuals/overview").searchParams.get("library_name")).toBe("Selected Library");
    expect(visualRequests.find((url) => url.pathname === "/api/visuals/analysis-fields").searchParams.get("library_name")).toBe("Selected Library");
    expect(oldRequests).toEqual([]);
    await expect(page.locator(".correlation-cell")).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText("19×19");
    await expect(page.locator(".fit-line")).toHaveCount(1);
    await expect(page.locator("#statPearson")).toHaveText("-0.91");
    await expect(page.locator("#statSpearman")).toHaveText("-1");
    await expect(page.locator("#statR2")).toHaveText("0.83");
    await expect(page.locator("#statSlope")).toHaveText("-0.5");
    await expect(page.locator("#statIntercept")).toHaveText("2.1");
    await expect(page.locator("#statCatalysts")).toHaveText("3");
    await expect(page.locator("#statPapers")).toHaveText("2");
    await expect(page.locator("#diagnosticTitle")).toHaveText("当前数据可以进行拟合");
    await expect(page.locator("#diagnosticMessage")).toContainText("3 个同一催化剂有效配对");
    await expect(page.locator("#technicalDiagnostics")).not.toHaveAttribute("open", "");
    await page.locator("#technicalDiagnostics summary").click();
    await expect(page.locator("#excludedBox")).toContainText("历史记录尚未完成 Identity V2 结构化身份：1290 次");
    await expect(page.locator("#excludedBox")).toContainText("记录未明确绑定催化剂样品：567 次");
    await expect(page.locator("#excludedBox")).toContainText("计算设置、位点或构型不兼容：12 次");
    await expect(page.locator("#technicalDiagnostics")).toContainText("同一记录可能同时命中多个原因");

    const second = page.locator("#detailsBody tr").nth(1);
    await expect(second).toContainText("sample-co-002");
    await expect(second).toContainText("B0078");
    await expect(second).toContainText("-1.7");
    await expect(second).toContainText("1.02");
    await expect(second).toContainText("nested-x-sample-co-002");
    await expect(second).toContainText("nested-y-sample-co-002");
    await expect(second).toContainText("path A：0.77");
    await expect(second).toContainText("path B：1.02（用于汇总/回归的最大可比能垒）");

    await page.getByRole("button", { name: "快捷关系：Li2S Bader 电荷转移 vs Li-S 最大键长" }).click();
    await expect.poll(() => correlationRequests.length).toBe(2);
    expect(correlationRequests[1].searchParams.get("x_field")).toBe("li2s_bader_charge_transfer");
    expect(correlationRequests[1].searchParams.get("y_field")).toBe("li_s_bond_max");
    expect(correlationRequests[1].searchParams.get("library_name")).toBe("Selected Library");
    expect(oldRequests).toEqual([]);
  });

  test("clamps min_n to 3 and never draws a line when backend is not ready", async ({ page }) => {
    const correlationRequests = [];
    await installVisualMocks(page, correlationRequests);
    await page.goto(BASE_URL + "/pages/visuals/index.html");
    await expect.poll(() => correlationRequests.length).toBe(1);
    expect(correlationRequests[0].searchParams.get("library_name")).toBe("Active Library");

    await page.locator("#minN").fill("2");
    await page.locator("#minN").dispatchEvent("change");
    await expect.poll(() => correlationRequests.length).toBe(2);
    expect(correlationRequests[1].searchParams.get("min_n")).toBe("3");

    await page.locator("#minN").fill("5");
    await page.locator("#minN").dispatchEvent("change");
    await expect.poll(() => correlationRequests.length).toBe(3);
    await expect(page.locator(".fit-line")).toHaveCount(0);
    await expect(page.locator("#fitNotice")).toContainText("样本数 3 小于最少样本数 5");
    await expect(page.locator("#statPearson")).toHaveText("—");
    await expect(page.locator("#statR2")).toHaveText("—");
    await expect(page.locator("#diagnosticTitle")).toHaveText("当前数据不足，暂不进行拟合");
    await expect(page.locator("#diagnosticMessage")).toContainText("至少需要 5 个");
    await page.locator("#technicalDiagnostics summary").click();
    await expect(page.locator("#warningsBox")).toContainText("有效配对未达到最少样本数");
    await expect(page.locator("#warningsBox")).toContainText("跨文献可信度不足");
  });

  test("offers fixed catalyst CSV and audit JSON downloads with library scope and clear errors", async ({ page }) => {
    const correlationRequests = [];
    const exportRequests = [];
    await installVisualMocks(page, correlationRequests);
    await page.route("**/api/dft/catalyst-dataset.csv*", (route) => {
      exportRequests.push(new URL(route.request().url()));
      return route.fulfill({
        status: 200,
        contentType: "text/csv; charset=utf-8",
        headers: { "Content-Disposition": 'attachment; filename="dft_catalyst_dataset_v1.csv"' },
        body: "\ufeffcatalyst_name,catalyst_sample_id\r\nFe-N-C,sample-1\r\n",
      });
    });
    await page.route("**/api/dft/catalyst-dataset?*", (route) => {
      exportRequests.push(new URL(route.request().url()));
      return route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Exports are disabled by server policy" }),
      });
    });
    await page.goto(BASE_URL + "/pages/visuals/index.html?library_name=Selected%20Library");

    await expect(page.getByRole("button", { name: "导出催化剂宽表 CSV" })).toBeVisible();
    await expect(page.getByRole("button", { name: "下载审计 JSON" })).toBeVisible();
    await expect(page.locator(".export-panel")).toContainText("pandas、随机森林、XGBoost");
    await expect(page.locator(".export-panel")).toContainText("不是直接训练表");

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "导出催化剂宽表 CSV" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("dft_catalyst_dataset_v1.csv");
    await expect(page.locator("#exportStatus")).toContainText("催化剂宽表 CSV已开始下载");

    await page.getByRole("button", { name: "下载审计 JSON" }).click();
    await expect(page.locator("#exportStatus")).toContainText("审计 JSON下载失败：请求失败（403）");
    await expect(page.locator("#exportStatus")).toContainText("Exports are disabled by server policy");

    await expect.poll(() => exportRequests.length).toBe(2);
    expect(exportRequests.map((url) => url.pathname)).toEqual([
      "/api/dft/catalyst-dataset.csv",
      "/api/dft/catalyst-dataset",
    ]);
    expect(exportRequests.every((url) => url.searchParams.get("library_name") === "Selected Library")).toBe(true);
  });
});
