const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

test('explicit missing PDF status wins over stale database paths', () => {
  const page = read('pages/literature_library/page.js');
  const api = read('pages/literature_library/api.js');

  const pageMissing = 'if(p&&p.pdf_exists===false||status.pdf_exists===false)return false';
  const pageFallback = 'return Boolean(p&&p.pdf_path';
  expect(page).toContain(pageMissing);
  expect(page.indexOf(pageMissing)).toBeLessThan(page.indexOf(pageFallback));

  const apiMissing = 'if (paper.pdf_exists === false || artifactStatus.pdf_exists === false) return false;';
  const apiFallback = 'return !!paper.pdf_path;';
  expect(api).toContain(apiMissing);
  expect(api.indexOf(apiMissing)).toBeLessThan(api.indexOf(apiFallback));
});
