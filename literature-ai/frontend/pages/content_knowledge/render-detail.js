import { escapeHtml } from './render-list.js';

function fact(label, value) {
  return value ? `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>` : '';
}

export function renderDetail(root, item) {
  if (!item) {
    root.innerHTML = '<div class="state-card">从左侧选择一条内容以查看证据与技术详情。</div>';
    return;
  }
  const location = [
    item.section_title || item.section,
    item.page_start ? `第 ${item.page_start}${item.page_end && item.page_end !== item.page_start ? `–${item.page_end}` : ''} 页` : '',
  ].filter(Boolean).join(' · ');
  root.innerHTML = `
    <div class="panel-heading">
      <div><p class="eyebrow">${escapeHtml(item.paper_code || '未关联论文号')}</p><h2>${escapeHtml(item.paper_title || item.category_label || item.category || '内容知识')}</h2></div>
      <span class="tag">${escapeHtml(item.category_label || item.category || '未分类')}</span>
    </div>
    <article class="detail-content">
      <h3>内容</h3><p>${escapeHtml(item.content || '未提供内容')}</p>
      <h3>证据</h3><blockquote>${escapeHtml(item.evidence_text || item.evidence || '未提供可显示的证据文本')}</blockquote>
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
