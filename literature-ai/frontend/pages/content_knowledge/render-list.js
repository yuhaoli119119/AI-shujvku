export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[char]);
}
const text = (value, fallback = '—') => escapeHtml(value || fallback);

export function renderList(root, items, selectedId, onSelect) {
  if (!items.length) {
    root.innerHTML = '<div class="state-card">没有匹配的论文内容。请调整搜索或筛选条件。</div>';
    return;
  }
  root.innerHTML = items.map((item) => {
    const auditOnly = item.item_kind === 'audit' || item.source_type === 'external_analysis_candidate';
    const lifecycle = item.audit_state_label || (
      item.audit_state === 'terminal_history' ? '终态 / 历史审计记录' : '审计状态未知 / 需处理'
    );
    const policyTag = auditOnly
      ? `<span class="tag">${text(lifecycle)}</span><span class="tag audit-only-label">仅审计，不可写作 / 引用</span>`
      : `<span class="tag ${text(item.citation_policy, 'needs_review')}">权威门禁：${text(item.review_gate_status, 'blocked')}</span>`;
    return [
    `<button class="knowledge-item ${auditOnly ? 'audit-only' : ''} ${item.item_id === selectedId ? 'is-selected' : ''}" type="button"`,
    ` data-item-id="${escapeHtml(item.item_id)}">`,
    `<span class="item-paper">${text(item.paper_code)}</span>`,
    `<strong>${text(item.paper_title || item.category_label || item.category)}</strong>`,
    `<span class="item-preview">${text(item.content)}</span>`,
    '<span class="tag-row">',
    `<span class="tag">${text(item.category_label || item.category)}</span>`,
    policyTag,
    '</span></button>',
  ].join('');
  }).join('');
  root.querySelectorAll('[data-item-id]').forEach((button) => button.addEventListener('click', () => onSelect(button.dataset.itemId)));
}

export function renderListError(root, message) {
  root.innerHTML = `<div class="state-card error">论文内容加载失败：${escapeHtml(message)}</div>`;
}
