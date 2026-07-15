function json(response) { return response.json().catch(() => ({})); }

export async function listItems(filters, offset, limit) {
  const params = new URLSearchParams({ ...filters, offset: String(offset), limit: String(limit) });
  const response = await fetch(`/api/content-knowledge?${params}`);
  const body = await json(response);
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  const items = Array.isArray(body) ? body : (body.items || []);
  return { items, total: body.total ?? items.length, offset: body.offset ?? offset, limit: body.limit ?? limit, hasMore: body.has_more ?? false, schemaVersion: body.schema_version };
}

export async function getItem(itemId) {
  const response = await fetch(`/api/content-knowledge/items/${encodeURIComponent(itemId)}`);
  if (response.status === 404) return null;
  const body = await json(response);
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body.item || body;
}

export async function reviewItem(itemId, body) {
  const response = await fetch(`/api/content-knowledge/items/${encodeURIComponent(itemId)}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const payload = await json(response);
  if (response.status === 409) { const error = new Error('审核状态已被更新，请重载后再决定。'); error.conflict = true; throw error; }
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload.item || payload;
}

export async function advancedAction(action, filters) {
  const routes = { sync: '/api/content-knowledge/sync', bundle: '/api/content-knowledge/review-bundles', import: '/api/content-knowledge/review-bundles/import', export: '/api/content-knowledge/writing-plan' };
  const response = await fetch(routes[action], { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...filters, reviewer: 'human-ui' }) });
  const payload = await json(response);
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}
