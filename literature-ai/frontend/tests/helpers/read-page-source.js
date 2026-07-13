const fs = require('fs');
const path = require('path');

const FRONTEND_ROOT = path.resolve(__dirname, '../..');

function readPageSource(relativeHtmlPath) {
  const htmlPath = path.join(FRONTEND_ROOT, relativeHtmlPath);
  const html = fs.readFileSync(htmlPath, 'utf8');
  const pageDir = path.dirname(htmlPath);
  const linkedSources = [];
  for (const match of html.matchAll(/<(?:script[^>]+src|link[^>]+href)=["']\.\/(page\.(?:js|css))["'][^>]*>/g)) {
    linkedSources.push(fs.readFileSync(path.join(pageDir, match[1]), 'utf8'));
  }
  return [html, ...linkedSources].join('\n');
}

module.exports = { readPageSource };
