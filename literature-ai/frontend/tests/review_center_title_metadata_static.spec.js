const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const { readPageSource } = require('./helpers/read-page-source');

test('review center title metadata does not repeat the paper code and renders SI as plain green text', () => {
  const source = readPageSource('pages/review_center/index.html');

  expect(source).not.toContain('const shortId = displayCode');
  expect(source).toContain('class="paper-si-inline"');
  expect(source).toContain('color: var(--color-success, #16a34a)');
  expect(source).not.toContain('class="chip compact subtle" title="\' + esc(groupTip || groupLabel)');
});
