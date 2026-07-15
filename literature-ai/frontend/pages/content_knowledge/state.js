export const state = { items: [], total: 0, offset: 0, startOffset: 0, limit: 25, hasMore: false, selectedId: null, filters: {} };

export function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  state.selectedId = params.get('selected') || null;
  state.offset = Number(params.get('offset') || 0);
  state.filters = Object.fromEntries([...params.entries()].filter(([key]) => key !== 'selected' && key !== 'offset'));
  return state.filters;
}

export function writeUrlState({ resetOffset = false } = {}) {
  if (resetOffset) state.offset = 0;
  const params = new URLSearchParams();
  Object.entries(state.filters).forEach(([key, value]) => {
    if (value !== '' && value != null && value !== false) params.set(key, String(value));
  });
  if (state.offset) params.set('offset', String(state.offset));
  if (state.selectedId) params.set('selected', state.selectedId);
  window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`);
}

export function setSelected(itemId) { state.selectedId = itemId || null; writeUrlState(); }
