const fs = require("fs");
const path = require("path");
const { test, expect } = require("@playwright/test");

const root = path.resolve(__dirname, "..");
const read = file => fs.readFileSync(path.join(root, file), "utf8");

test("literature library is a filter-plus-list page without embedded workspace", async () => {
  const html = read("pages/literature_library/index.html");
  const js = read("pages/literature_library/page.js");
  expect(html).toContain('id="filterPanel"');
  expect(html).toContain('id="paperRows"');
  expect(html).toContain('id="yearFilter"');
  expect(html).toContain('id="journalFilter"');
  expect(html).toContain('id="typeFilter"');
  expect(html).toContain('id="dftFilter"');
  expect(html).toContain('id="contentFilter"');
  expect(html).toContain('id="pdfFilter"');
  expect(html).not.toContain('class="workspace"');
  expect(html).not.toContain('id="dragHandle"');
  expect(js).toContain('../paper_detail/index.html?paper_id=');
  expect(js).not.toContain("EventSource");
  expect(js).not.toContain("/stream");
});

test("independent detail exposes summary, figure reading, lightbox, and server export", async () => {
  const html = read("pages/paper_detail/index.html");
  expect(html).toContain('id="paperAbstract"');
  expect(html).toContain("abstract_zh");
  expect(html).toContain("查看英文原摘要");
  expect(html).toContain("pd-figure-reading-block");
  expect(html).toContain("pdLightbox");
  expect(html).toContain("prevFigureInLightbox");
  expect(html).toContain("nextFigureInLightbox");
  expect(html).toContain("e.key === \"Escape\"");
  expect(html).toContain('method: "POST"');
  expect(html).toContain("server_path");
  expect(html).not.toContain("a.download");
});
