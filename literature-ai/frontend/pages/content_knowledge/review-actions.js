import { escapeHtml } from './render-list.js';

const decisions = [
  { value: 'approve_citable', label: '批准可引用' },
  { value: 'writing_only', label: '仅写作使用' },
  { value: 'needs_human', label: '转需人工' },
  { value: 'reject', label: '拒绝' },
];
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isReviewable(item) {
  if (typeof item.reviewable === 'boolean') return item.reviewable && !item.requires_sync;
  if (item.requires_sync === true) return false;
  return UUID_PATTERN.test(String(item.item_id || ''));
}

export function renderReview(root, item, onReview) {
  if (!item) {
    root.innerHTML = '<div class="state-card">选择内容后可进行人工审核。</div>';
    return;
  }
  const risks = (item.risk_flags || item.risks || []).map((risk) => `<li>${escapeHtml(risk)}</li>`).join('') || '<li>未返回风险标记</li>';
  const sourceCorrectionLink = item.paper_id ? `<a class="source-correction-link" href="../review_center/index.html?paper_id=${encodeURIComponent(item.paper_id)}">去审核中心修正源内容</a>` : '';
  const metadata = item.metadata && typeof item.metadata === 'object' ? item.metadata : {};
  const runId = metadata.external_analysis_run_id || metadata.run_id;
  const chartReviewLink = item.category === 'figure_table_evidence' && item.paper_id && runId
    ? `<a class="source-correction-link" href="../review_center/index.html?paper_id=${encodeURIComponent(item.paper_id)}&run_id=${encodeURIComponent(runId)}&mode=evidence">转到图表审核</a>`
    : '';
  const reviewable = isReviewable(item);
  const disabled = reviewable ? '' : ' disabled';
  const sourceTrust = item.source_identity_verified
    ? '来源身份已由服务端核验'
    : '来源仅为声明，身份未认证';
  const decisionControls = decisions.map((entry) => [
    '<label class="decision">',
    `<input type="radio" name="decision" value="${entry.value}"${disabled}>`,
    entry.label,
    '</label>',
  ].join('')).join('');
  root.innerHTML = [
    '<div class="panel-heading"><h2>审核与来源风险</h2></div>',
    '<p class="muted">这里审核证据与引用资格，不会直接编辑 MechanismClaim 或 WritingCard 源内容。</p>',
    sourceCorrectionLink,
    chartReviewLink,
    `<p class="muted">来源可信度：${escapeHtml(sourceTrust)}</p>`,
    `<ul class="risk-list">${risks}</ul>`,
    reviewable ? '' : '<p class="state-card">先同步索引后审核</p>',
    `<form id="reviewForm"><fieldset${disabled}><legend>人工决定</legend>${decisionControls}</fieldset>`,
    `<label class="field" for="reviewReason"><span>原因 <em>（拒绝或转需人工必填）</em></span>`,
    `<textarea id="reviewReason" name="reason" rows="4" placeholder="记录证据不足、来源冲突或判断依据"${disabled}></textarea></label>`,
    `<button class="primary-btn" type="submit"${disabled}>提交审核决定</button></form>`,
  ].join('');
  if (!reviewable) return;
  root.querySelector('#reviewForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const decision = new FormData(event.currentTarget).get('decision');
    const reason = event.currentTarget.reason.value.trim();
    if (!decision) return onReview(null, reason);
    if ((decision === 'reject' || decision === 'needs_human') && !reason) {
      return onReview({ validation: '拒绝或转需人工时必须填写原因。' });
    }
    onReview({ decision, reason, expected_updated_at: item.updated_at || item.expected_updated_at || null });
  });
}
