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

function renderLinkedFigures(item) {
  const figures = Array.isArray(item?.metadata?.linked_figures)
    ? item.metadata.linked_figures
    : [];
  if (!figures.length) return '';
  return `
    <section class="linked-figures" aria-label="关联的已审核图片">
      <h3>关联的已审核图片</h3>
      <div class="linked-figure-grid">
        ${figures.map((figure) => `
          <figure class="linked-figure">
            ${figure.asset_url ? `<img src="${escapeHtml(figure.asset_url)}" alt="${escapeHtml(figure.figure_label || '论文图片')}" loading="lazy">` : ''}
            <figcaption>
              <strong>${escapeHtml(figure.figure_label || '论文图片')}</strong>
              ${figure.page ? `<span>第 ${escapeHtml(figure.page)} 页</span>` : ''}
              <span>${escapeHtml(figure.content_summary || figure.caption || '')}</span>
            </figcaption>
          </figure>
        `).join('')}
      </div>
    </section>`;
}

function renderAIVerification(item) {
  const verification = item?.review_payload?.ai_verification
    || item?.metadata?.ai_verification
    || item?.review?.review_payload?.ai_verification
    || null;
  if (!verification || verification.actor_type !== 'ai') return '';
  const checks = {
    ...(verification.evidence_checks || {}),
    ...(verification.locator_checks || {}),
  };
  const checkSummary = Object.entries(checks)
    .map(([name, passed]) => `${name}: ${passed ? '通过' : '未通过'}`)
    .join('；');
  return `
    <section class="ai-verification" aria-label="AI 自动验收审计">
      <h3>AI 自动验收审计</h3>
      <dl class="facts">
        ${fact('验收结果', verification.outcome || verification.decision)}
        ${fact('置信度', verification.confidence == null ? null : String(verification.confidence))}
        ${fact('证据页码', verification.page == null ? null : `第 ${verification.page} 页`)}
        ${fact('验收来源', verification.source_label || verification.source_identity)}
        ${fact('策略版本', verification.policy_version)}
        ${fact('确定性复核', checkSummary)}
      </dl>
    </section>`;
}

export function renderDetail(root, item) {
  if (!item) {
    root.innerHTML = '<div class="state-card">从左侧选择一条内容以查看证据与技术详情。</div>';
    return;
  }
  const evidence = displayEvidence(item);
  const location = displayLocation(item, evidence.location);
  const auditOnly = item.item_kind === 'audit' || item.source_type === 'external_analysis_candidate';
  const projectionState = item?.metadata?.projection_state || {};
  const projectionLabel = [projectionState.review_status, projectionState.citation_status]
    .filter(Boolean).join(' / ');
  const auditNotice = auditOnly
    ? `<p class="safety-gate"><strong>仅审计，不可写作 / 引用。</strong> ${escapeHtml(item.audit_state_label || '审计状态未知 / 需处理')}；正式安全门：${escapeHtml(item.review_gate_status || '未通过')}。</p>`
    : '';
  root.innerHTML = `
    <div class="panel-heading">
      <div><p class="eyebrow">${escapeHtml(item.paper_code || '未关联论文号')}</p><h2>${escapeHtml(item.paper_title || item.category_label || item.category || '论文内容')}</h2></div>
      <span class="tag">${escapeHtml(item.category_label || item.category || '未分类')}</span>
    </div>
    <article class="detail-content">
      ${auditNotice}
      <h3>内容</h3><p>${escapeHtml(item.content || '未提供内容')}</p>
      <h3>证据</h3><blockquote>${escapeHtml(evidence.text)}</blockquote>
      <p class="muted">${escapeHtml(location || '未提供页码或章节定位')}</p>
      ${renderAIVerification(item)}
      ${renderLinkedFigures(item)}
      <dl class="facts">
        ${auditOnly ? '' : fact('权威验收门禁', item.review_gate_status || 'blocked')}
        ${auditOnly ? '' : fact('权威可写', item.can_use_for_writing ? '是' : '否')}
        ${auditOnly ? '' : fact('权威可引用', item.can_use_for_citation ? '是' : '否')}
        ${auditOnly ? '' : fact('投影缓存状态（非最终验收）', projectionLabel)}
        ${fact('审计生命周期', item.audit_state_label)}
        ${fact('候选状态', item.candidate_status)}
        ${fact('关联正式对象', item.linked_target_type && item.linked_target_id ? `${item.linked_target_type}:${item.linked_target_id}` : null)}
        ${auditOnly ? '' : fact('引用状态', item.citation_policy || item.citation_status)}
        ${fact('匹配原因', item.match_reason || item.recommended_action)}
        ${fact('来源', item.source_label || item.source_type)}
      </dl>
    </article>
    <details class="technical-details"><summary>技术详情</summary><pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre></details>`;
}
