import { escapeHtml } from './render-list.js';

function fact(label, value) {
  return value ? `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>` : '';
}

function parseStructuredEvidence(value) {
  if (!value || typeof value === 'object') return value || null;
  const text = String(value).trim();
  if (!text.startsWith('{') && !text.startsWith('[')) return null;
  try {
    return JSON.parse(text);
  } catch (_) {
    return null;
  }
}

function findEvidenceLocation(value, depth = 0) {
  if (!value || typeof value !== 'object' || depth > 6) return null;
  if (value.evidence_location && typeof value.evidence_location === 'object') {
    return value.evidence_location;
  }
  for (const key of ['raw_payload', 'metadata', 'source_record']) {
    const match = findEvidenceLocation(value[key], depth + 1);
    if (match) return match;
  }
  return null;
}

function displayEvidence(item) {
  const rawEvidence = item.evidence_text || item.evidence || '';
  const structured = parseStructuredEvidence(rawEvidence);
  const location = findEvidenceLocation(structured) || {};
  const evidenceText = location.quoted_text || location.evidence_text;
  if (evidenceText) return { text: evidenceText, location };
  if (structured) {
    return {
      text: '结构化证据已收录；完整数据保留在下方“技术详情”中。',
      location,
    };
  }
  return { text: rawEvidence || '未提供可显示的证据文本', location };
}

function displayLocation(item, structuredLocation) {
  const locator = item.evidence_locator && typeof item.evidence_locator === 'object'
    ? item.evidence_locator
    : {};
  const pageStart = item.page_start || locator.page_start || locator.page || structuredLocation.page_start || structuredLocation.page;
  const pageEnd = item.page_end || locator.page_end || structuredLocation.page_end;
  const section = item.section_title
    || item.section
    || locator.section_title
    || locator.section
    || structuredLocation.section_title
    || structuredLocation.section;
  return [
    section,
    pageStart ? `第 ${pageStart}${pageEnd && pageEnd !== pageStart ? `–${pageEnd}` : ''} 页` : '',
  ].filter(Boolean).join(' · ');
}

export function renderDetail(root, item) {
  if (!item) {
    root.innerHTML = '<div class="state-card">从左侧选择一条内容以查看证据与技术详情。</div>';
    return;
  }
  const evidence = displayEvidence(item);
  const location = displayLocation(item, evidence.location);
  root.innerHTML = `
    <div class="panel-heading">
      <div><p class="eyebrow">${escapeHtml(item.paper_code || '未关联论文号')}</p><h2>${escapeHtml(item.paper_title || item.category_label || item.category || '内容知识')}</h2></div>
      <span class="tag">${escapeHtml(item.category_label || item.category || '未分类')}</span>
    </div>
    <article class="detail-content">
      <h3>内容</h3><p>${escapeHtml(item.content || '未提供内容')}</p>
      <h3>证据</h3><blockquote>${escapeHtml(evidence.text)}</blockquote>
      <p class="muted">${escapeHtml(location || '未提供页码或章节定位')}</p>
      <dl class="facts">
        ${fact('审核状态', item.review_status)}
        ${fact('引用状态', item.citation_policy || item.citation_status)}
        ${fact('匹配原因', item.match_reason || item.recommended_action)}
        ${fact('来源', item.source_label || item.source_type)}
      </dl>
    </article>
    <details class="technical-details"><summary>技术详情</summary><pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre></details>`;
}
