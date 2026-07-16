async function readJson(response) {
  return response.json().catch(() => ({}));
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const body = await readJson(response);
  if (!response.ok) {
    const error = new Error(body.detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

export async function listItems(filters, offset, limit) {
  const params = new URLSearchParams({ ...filters, offset: String(offset), limit: String(limit) });
  const body = await request(`/api/content-knowledge?${params}`);
  const items = Array.isArray(body) ? body : (body.items || []);
  return {
    items,
    total: body.total ?? items.length,
    offset: body.offset ?? offset,
    limit: body.limit ?? limit,
    hasMore: body.has_more ?? false,
    schemaVersion: body.schema_version,
  };
}

export async function getItem(itemId) {
  const response = await fetch(`/api/content-knowledge/items/${encodeURIComponent(itemId)}`);
  if (response.status === 404) return null;
  const body = await readJson(response);
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body.item || body;
}

export async function reviewItem(itemId, body) {
  try {
    const result = await request(
      `/api/content-knowledge/items/${encodeURIComponent(itemId)}/review`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    );
    return result.item || result;
  } catch (error) {
    if (error.status === 409) {
      error.message = '审核状态已被更新，请重载后再决定。';
      error.conflict = true;
    }
    throw error;
  }
}

export function syncIndex(scope) {
  const params = new URLSearchParams(scope);
  return request(`/api/content-knowledge/sync?${params}`, { method: 'POST' });
}

export function createReviewBundleV2(body) {
  return request('/api/content-knowledge/review-bundles/v2', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function validateReviewBundleProposal(bundleId, result) {
  return request(`/api/content-knowledge/review-bundles/${encodeURIComponent(bundleId)}/web-proposal/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(result),
  });
}

export function getLocalVerificationPlan(bundleId) {
  return request(`/api/content-knowledge/review-bundles/${encodeURIComponent(bundleId)}/local-verification-plan`);
}

export function downloadReviewBundle(bundleId, downloadUrl) {
  return fetch(downloadUrl || `/api/content-knowledge/review-bundles/${encodeURIComponent(bundleId)}/download`);
}

export function createWritingPlan(query, paperIds) {
  return request('/api/content-knowledge/writing-plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, paper_ids: paperIds }),
  });
}
