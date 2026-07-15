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
    root.innerHTML = '<div class="state-card">没有匹配的内容知识。请调整搜索或筛选条件。</div>';
    return;
  }
  root.innerHTML = items.map((item) => [
    `<button class="knowledge-item ${item.item_id === selectedId ? 'is-selected' : ''}" type="button"`,
    ` data-item-id="${escapeHtml(item.item_id)}">`,
    `<span class="item-paper">${text(item.paper_code)}</span>`,
    `<strong>${text(item.paper_title || item.category_label || item.category)}</strong>`,
    `<span class="item-preview">${text(item.content)}</span>`,
    '<span class="tag-row">',
    `<span class="tag">${text(item.category_label || item.category)}</span>`,
    `<span class="tag ${text(item.citation_policy, 'needs_review')}">${text(item.citation_policy, '待审核')}</span>`,
    '</span></button>',
  ].join('')).join('');
  root.querySelectorAll('[data-item-id]').forEach((button) => button.addEventListener('click', () => onSelect(button.dataset.itemId)));
}

export function renderListError(root, message) {
  root.innerHTML = `<div class="state-card error">内容知识加载失败：${escapeHtml(message)}</div>`;
}
