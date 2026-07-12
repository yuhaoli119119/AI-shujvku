const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

test('review center title metadata does not repeat the paper code and renders SI as plain green text', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'review_center', 'index.html'),
    'utf8'
  );

  expect(source).not.toContain('const shortId = displayCode');
  expect(source).toContain('class="paper-si-inline"');
  expect(source).toContain('color: var(--color-success, #16a34a)');
  expect(source).not.toContain('class="chip compact subtle" title="\' + esc(groupTip || groupLabel)');
});
