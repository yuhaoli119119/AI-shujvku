import { state, setSelected, writeUrlState } from './state.js';
import { initFilters } from './filters.js';
import {
  listItems,
  getItem,
  reviewItem,
  syncIndex,
  createReviewBundleV2,
  validateReviewBundleProposal,
  getLocalVerificationPlan,
  getLocalVerificationStatus,
  downloadReviewBundle,
  createWritingPlan,
} from './api.js';
import { renderList, renderListError } from './render-list.js';
import { renderDetail } from './render-detail.js';
import { renderReview, isReviewable } from './review-actions.js';

const listRoot = document.querySelector('#listRoot');
const detailRoot = document.querySelector('#detailRoot');
const reviewRoot = document.querySelector('#reviewRoot');
let currentBundle = null;
let currentPlan = null;
let currentVerificationStatus = null;
let uploadedProposalKey = null;
const MAX_PROPOSAL_BYTES = 5 * 1024 * 1024;

function showMessage(text, isError = false) {
  const element = document.querySelector('#actionMessage');
  element.hidden = !text;
  element.textContent = text || '';
  element.classList.toggle('error', isError);
}

function selectedItem() {
  return state.items.find((item) => item.item_id === state.selectedId) || null;
}

function getSyncScope() {
  const item = selectedItem();
  if (item?.paper_id) {
    return {
      label: `当前论文：${item.paper_code || item.paper_id}`,
      filters: {
        paper_id: item.paper_id,
        include_candidates: state.filters.include_candidates || 'true',
      },
    };
  }
  if (state.filters.paper_id) {
    return {
      label: `当前论文：${state.filters.paper_id}`,
      filters: {
        paper_id: state.filters.paper_id,
        include_candidates: state.filters.include_candidates || 'true',
      },
    };
  }
  if (state.filters.library_name) {
    return {
      label: `当前文献库：${state.filters.library_name}`,
      filters: {
        library_name: state.filters.library_name,
        include_candidates: state.filters.include_candidates || 'true',
      },
    };
  }
  return null;
}

function renderScopeBanner() {
  const banner = document.querySelector('#scopeBanner');
  if (state.filters.run_id) {
    banner.textContent = `当前审核范围：AI 批次 ${state.filters.run_id}`;
    return;
  }
  if (state.filters.paper_id) {
    banner.textContent = `当前审核范围：论文 ${state.filters.paper_id}`;
    return;
  }
  if (state.filters.library_name) {
    banner.textContent = `当前审核范围：文献库 ${state.filters.library_name}`;
    return;
  }
  banner.textContent = '当前审核范围：全部内容；建议先选择论文或文献库。';
}

function updateSyncScope() {
  const scope = getSyncScope();
  document.querySelector('#syncScope').textContent = scope
    ? `同步范围：${scope.label}`
    : '同步范围：请先选择论文或文献库。为避免误触发全库同步，当前不能同步。';
}

function renderSelectedItem() {
  const item = selectedItem();
  renderDetail(detailRoot, item);
  renderReview(reviewRoot, item, submitReview);
  updateSyncScope();
}

function renderMetadata() {
  const shown = state.items.length ? `${state.startOffset + 1}–${state.startOffset + state.items.length}` : '0';
  document.querySelector('#resultRange').textContent = `显示 ${shown}，共 ${state.total} 条`;
  document.querySelector('#resultCount').textContent = `${state.total} 条`;
  document.querySelector('#loadMoreButton').hidden = !state.hasMore;
  renderScopeBanner();
}

async function loadItems({ append = false } = {}) {
  try {
    if (!append) listRoot.innerHTML = '<div class="state-card">加载中…</div>';
    const payload = await listItems(state.filters, state.offset, state.limit);
    if (!append) state.startOffset = payload.offset;
    state.items = append ? [...state.items, ...payload.items] : payload.items;
    state.total = payload.total;
    state.limit = payload.limit;
    state.hasMore = payload.hasMore;
    document.querySelector('#schemaStatus').textContent = payload.schemaVersion || '内容索引';
    renderList(listRoot, state.items, state.selectedId, selectItem);
    renderMetadata();
    if (state.selectedId) await hydrateSelectedItem();
    renderSelectedItem();
  } catch (error) {
    renderListError(listRoot, error.message);
    showMessage('无法加载内容知识。', true);
  }
}

async function selectItem(itemId) {
  setSelected(itemId);
  renderList(listRoot, state.items, itemId, selectItem);
  await hydrateSelectedItem();
  renderSelectedItem();
}

async function hydrateSelectedItem() {
  const index = state.items.findIndex((item) => item.item_id === state.selectedId);
  if (index < 0 || !isReviewable(state.items[index])) return;
  try {
    const fullItem = await getItem(state.selectedId);
    if (fullItem) state.items[index] = { ...state.items[index], ...fullItem };
  } catch (error) {
    showMessage(`详情加载失败：${error.message}`, true);
  }
}

async function submitReview(review) {
  if (!review) return showMessage('请选择一个审核决定。', true);
  if (review.validation) return showMessage(review.validation, true);
  const item = selectedItem();
  try {
    const updatedItem = await reviewItem(item.item_id, { ...review, reviewer: 'human-ui' });
    const index = state.items.findIndex((row) => row.item_id === item.item_id);
    state.items[index] = { ...item, ...updatedItem };
    showMessage('审核决定已保存，并已局部刷新该内容。');
    renderList(listRoot, state.items, state.selectedId, selectItem);
    renderSelectedItem();
  } catch (error) {
    showMessage(error.message, true);
    if (error.conflict) await loadItems();
  }
}

async function runAdvancedAction(action) {
  try {
    const scope = action === 'sync' ? getSyncScope() : null;
    if (action === 'sync' && !scope) {
      return showMessage('请先按论文号筛选、选择一条内容，或选择文献库后再同步索引。', true);
    }
    if (action === 'sync' && !window.confirm(`确认同步内容索引？范围为：${scope.label}`)) return;
    await syncIndex(scope.filters);
    showMessage('内容索引已同步，正在刷新列表。');
    await loadItems();
  } catch (error) {
    showMessage(`高级操作失败：${error.message}`, true);
  }
}

function selectedPaperId() {
  const selected = selectedItem();
  if (selected?.paper_id) return selected.paper_id;
  const scopedItem = state.items.find((item) => (
    item.paper_id === state.filters.paper_id || item.paper_code === state.filters.paper_id
  ));
  return scopedItem?.paper_id || null;
}

function selectedModule() {
  return document.querySelector('#bundleModule').value;
}

function countOf(source, ...keys) {
  for (const key of keys) {
    if (source && source[key] != null) return source[key];
  }
  return 0;
}

function firstDefined(source, ...keys) {
  for (const key of keys) {
    if (source && source[key] != null) return source[key];
  }
  return null;
}

function bundleSummary(bundle) {
  const manifest = bundle.manifest || bundle.summary || {};
  const targets = Array.isArray(manifest.targets) ? manifest.targets : [];
  const allowedPages = Array.isArray(manifest.allowed_pages) ? manifest.allowed_pages : [];
  const objectCount = firstDefined(bundle, 'object_count', 'target_count', 'item_count') ?? firstDefined(manifest, 'object_count', 'target_count', 'item_count') ?? (targets.length || 0);
  const pageCount = firstDefined(bundle, 'unique_evidence_page_count', 'unique_page_count') ?? firstDefined(manifest, 'unique_evidence_page_count', 'unique_page_count') ?? (allowedPages.length || 0);
  const status = bundle.status || manifest.status || 'created';
  return `审核包已生成：${bundle.bundle_id}；对象 ${objectCount}；唯一证据页 ${pageCount}；状态 ${status}。`;
}

function renderBundleStatus(text) {
  document.querySelector('#bundleControls').hidden = false;
  document.querySelector('#bundleStatus').textContent = text;
}

const VERIFICATION_STATUS_LABELS = {
  awaiting_local_verification: '待本地核验',
  partial: '部分完成',
  awaiting_human: '待人工',
  finalized: '已完成',
  stale: '已失效',
  failed: '失败',
};

function displayValue(value, fallback = '—') {
  return value === 0 ? '0' : (value == null || value === '' ? fallback : String(value));
}

function renderVerificationStatus(status) {
  currentVerificationStatus = status;
  const root = document.querySelector('#localVerificationStatus');
  root.hidden = false;
  root.replaceChildren();
  const title = document.createElement('h3');
  title.textContent = '本地核验最终状态';
  root.append(title);
  if (!status || typeof status !== 'object') {
    const empty = document.createElement('p');
    empty.textContent = '暂无本地核验状态。';
    root.append(empty);
    return;
  }
  const statusLine = document.createElement('p');
  statusLine.className = 'verification-status-line';
  statusLine.textContent = `总状态：${VERIFICATION_STATUS_LABELS[status.status] || '待本地核验'}（${status.status || 'unknown'}）`;
  root.append(statusLine);

  const counts = status.object_counts || {};
  const countLine = document.createElement('p');
  countLine.textContent = `对象计数：必需 ${displayValue(counts.required, '0')}；已应用 ${displayValue(counts.applied, '0')}；待处理 ${displayValue(counts.pending, '0')}；已失效 ${displayValue(counts.stale, '0')}；失败 ${displayValue(counts.failed, '0')}；待人工 ${displayValue(counts.awaiting_human, '0')}`;
  root.append(countLine);

  const before = status.formal_eligibility_before || {};
  const after = status.formal_eligibility_after || {};
  const delta = status.formal_eligibility_delta || {};
  const eligibility = document.createElement('p');
  eligibility.textContent = `正式资格（前 → 后，变化）：可写作 ${displayValue(before.writing, '0')} → ${displayValue(after.writing, '0')}（${displayValue(delta.writing, '0')}）；可引用 ${displayValue(before.citation, '0')} → ${displayValue(after.citation, '0')}（${displayValue(delta.citation, '0')}）；RAG ${displayValue(before.rag, '0')} → ${displayValue(after.rag, '0')}（${displayValue(delta.rag, '0')}）`;
  root.append(eligibility);

  const metrics = status.metrics || {};
  const metricLine = document.createElement('p');
  metricLine.textContent = `读取指标：逻辑读取 ${displayValue(metrics.logical_page_read_count ?? metrics.logical_reads, '—')}；物理读取 ${displayValue(metrics.physical_page_read_attempt_count ?? metrics.physical_reads, '—')}；重试 ${displayValue(metrics.page_read_retry_count ?? metrics.retries, '—')}；缓存命中 ${displayValue(metrics.page_cache_hit_count ?? metrics.cache_hits, '—')}`;
  root.append(metricLine);

  const results = Array.isArray(status.results) ? status.results.filter((result) => ['stale', 'failed', 'awaiting_human'].includes(result.status)) : [];
  const resultTitle = document.createElement('p');
  resultTitle.textContent = '需关注结果：';
  root.append(resultTitle);
  if (!results.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '暂无已失效、失败或待人工结果。';
    root.append(empty);
  } else {
    const list = document.createElement('ul');
    results.forEach((result) => {
      const item = document.createElement('li');
      const targetType = result.target_type || result.object_type || '对象信息不可用';
      const targetId = result.target_id || result.plan_item_id || '—';
      const field = result.field_name || result.field || `对象信息不可用（计划项 ${result.plan_item_id || '—'}）`;
      const reason = result.reason || result.error_code || (Array.isArray(result.stale_reasons) ? result.stale_reasons.join('、') : result.stale_reasons) || result.verification_note || '—';
      item.textContent = `目标类型 ${targetType}；字段 ${field}；状态 ${VERIFICATION_STATUS_LABELS[result.status] || result.status || '—'}；原因 ${reason}；目标 ${targetId}`;
      list.append(item);
    });
    root.append(list);
  }
  const gate = document.createElement('p');
  gate.className = 'safety-gate';
  gate.textContent = '安全提示：网页 AI 全部 PASS 仍不会直接解锁；只有认证本地 AI 完成 PDF 核验与受控应用后，正式资格才变化。此页面仅读取状态，不提供应用或写入入口。';
  root.append(gate);
}

function renderVerificationStatusLoading() {
  const root = document.querySelector('#localVerificationStatus');
  root.hidden = false;
  root.replaceChildren();
  const message = document.createElement('p');
  message.textContent = '正在读取本地核验状态…';
  root.append(message);
}

function renderVerificationStatusError(error) {
  const root = document.querySelector('#localVerificationStatus');
  root.hidden = false;
  root.replaceChildren();
  const message = document.createElement('p');
  message.className = 'error';
  message.textContent = `本地核验状态读取失败：${error.message}`;
  root.append(message);
}

async function loadVerificationStatus() {
  if (!currentBundle?.bundle_id) return;
  renderVerificationStatusLoading();
  try {
    renderVerificationStatus(await getLocalVerificationStatus(currentBundle.bundle_id));
  } catch (error) {
    renderVerificationStatusError(error);
  }
}

async function generateBundle() {
  const paperId = selectedPaperId();
  if (!paperId) return showMessage('请先选择一篇论文，再生成审核包。', true);
  try {
    currentBundle = await createReviewBundleV2({ paper_id: paperId, module: selectedModule() });
    uploadedProposalKey = null;
    currentVerificationStatus = null;
    renderPlan(null);
    renderVerificationStatus(null);
    renderBundleStatus(bundleSummary(currentBundle));
    document.querySelector('#bundleDownloadButton').disabled = false;
    document.querySelector('#bundleFile').value = '';
  } catch (error) {
    showMessage(`生成审核包失败：${error.message}`, true);
  }
}

async function copyBundleInstruction() {
  if (!currentBundle) await generateBundle();
  if (!currentBundle) return;
  const text = currentBundle.web_ai_instruction || currentBundle.instructions || currentBundle.manifest?.instructions || `${currentBundle.prompt || ''}\n\nRETURN TEMPLATE:\n${JSON.stringify(currentBundle.return_template || {}, null, 2)}`;
  const fallback = document.querySelector('#bundleInstructionOutput');
  fallback.value = text;
  try {
    await navigator.clipboard.writeText(text);
    fallback.hidden = true;
    showMessage('网页 AI 指令已复制。');
  } catch (_) {
    fallback.hidden = false;
    showMessage('无法使用剪贴板，网页 AI 指令已显示在只读文本框中。', true);
  }
}

async function validateBundle() {
  if (!currentBundle) return showMessage('请先生成审核包。', true);
  renderPlan(null);
  try {
    const input = document.querySelector('#bundleResultInput').value.trim();
    if (!input) throw new Error('请先上传或粘贴网页 AI JSON。');
    const result = JSON.parse(input);
    const response = await validateReviewBundleProposal(currentBundle.bundle_id, result);
    renderBundleStatus(`网页 AI 回传校验完成：${response.valid === false ? '未通过' : '通过'}。${response.message || ''}`);
    if (response.valid === false) {
      const root = document.querySelector('#localPlan');
      root.hidden = false;
      root.replaceChildren();
      const title = document.createElement('p');
      title.textContent = '校验错误：';
      root.append(title);
      (Array.isArray(response.errors) ? response.errors : ['网页 AI 建议未通过校验。']).forEach((error) => {
        const item = document.createElement('li');
        item.textContent = typeof error === 'string' ? error : JSON.stringify(error);
        const list = root.querySelector('ul') || document.createElement('ul');
        if (!list.parentNode) root.append(list);
        list.append(item);
      });
      return;
    }
    await loadLocalPlan();
    await loadVerificationStatus();
  } catch (error) {
    showMessage(error instanceof SyntaxError ? 'JSON 解析失败：请上传严格有效的 JSON。' : `网页 AI 回传校验失败：${error.message}`, true);
  }
}

function renderPlan(plan) {
  currentPlan = plan;
  const root = document.querySelector('#localPlan');
  if (!plan) { root.hidden = true; root.replaceChildren(); return; }
  root.hidden = false;
  root.replaceChildren();
  const summary = plan.summary || plan;
  const stats = document.createElement('p');
  const metrics = plan.metrics || {};
  const logicalReads = firstDefined(metrics, 'logical_page_read_count');
  const unresolved = firstDefined(metrics, 'unresolved_page_target_count');
  const metricText = `${logicalReads != null ? `；逻辑页读取 ${logicalReads}` : ''}${unresolved != null ? `；未解决页目标 ${unresolved}` : ''}`;
  stats.textContent = `本地核验计划：网页已核验 ${countOf(summary, 'web_reviewed_target_count')}；本地需核验 ${countOf(summary, 'local_required_target_count')}；本地跳过 ${countOf(summary, 'local_skipped_target_count')}；唯一页 ${countOf(summary, 'unique_page_count')}${metricText}`;
  root.append(stats);
  const batches = plan.page_batches || plan.batches || [];
  if (batches.length) {
    const list = document.createElement('ul');
    batches.forEach((batch) => {
      const item = document.createElement('li');
      const targetCount = firstDefined(batch, 'target_count', 'object_count') ?? (Array.isArray(batch.checks) ? batch.checks.length : 0);
      item.textContent = `第 ${batch.page ?? batch.page_start ?? '?'} 页批次：${targetCount} 个对象`;
      list.append(item);
    });
    root.append(list);
  }
  const gate = document.createElement('p');
  gate.className = 'safety-gate';
  gate.textContent = '安全门：只读。未完成本地核验，写作资格与引用资格未改变。';
  root.append(gate);
  const copyButton = document.createElement('button');
  copyButton.type = 'button';
  copyButton.id = 'copyLocalInstructionButton';
  copyButton.className = 'secondary-btn';
  copyButton.textContent = '复制精简本地 AI 核验指令';
  copyButton.addEventListener('click', copyLocalInstruction);
  root.append(copyButton);
}

async function copyLocalInstruction() {
  if (!currentPlan) return showMessage('请先通过网页 AI 建议校验。', true);
  const supplied = currentPlan.local_ai_instruction || currentPlan.instruction || '';
  const text = `不要读取整包；按唯一证据页读取；逐对象返回；无锁阅读后短锁回填。\n${supplied}`.trim();
  try {
    await navigator.clipboard.writeText(text);
    showMessage('精简本地 AI 核验指令已复制。');
  } catch (_) {
    showMessage('无法使用剪贴板，请手动复制本地核验指令。', true);
  }
}

async function loadLocalPlan() {
  try {
    const plan = await getLocalVerificationPlan(currentBundle.bundle_id);
    renderPlan(plan);
  } catch (error) {
    showMessage(`获取本地核验计划失败：${error.message}`, true);
  }
}

async function downloadBundle() {
  if (!currentBundle) return showMessage('请先生成审核包。', true);
  try {
    const response = await downloadReviewBundle(currentBundle.bundle_id, currentBundle.download_url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (!contentType.includes('zip') && !contentType.includes('octet-stream')) throw new Error('响应不是 ZIP 文件');
    const blob = await response.blob();
    if (!blob.size) throw new Error('ZIP 响应为空');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${currentBundle.bundle_id}.zip`;
    document.body.append(link);
    link.click();
    setTimeout(() => { URL.revokeObjectURL(url); link.remove(); }, 1000);
  } catch (error) {
    showMessage(`下载审核包失败：${error.message}`, true);
  }
}

async function readProposalFile(file) {
  if (!file) return;
  const key = `${file.name}:${file.size}:${file.lastModified}`;
  if (key === uploadedProposalKey) return showMessage('该 JSON 已上传过，请选择新的文件。', true);
  renderPlan(null);
  if (file.size > MAX_PROPOSAL_BYTES) return showMessage('网页 AI JSON 超过 5 MB 大小限制。', true);
  try {
    const text = await file.text();
    JSON.parse(text);
    document.querySelector('#bundleResultInput').value = text;
    uploadedProposalKey = key;
    showMessage(`已载入网页 AI JSON：${file.name}`);
  } catch (error) {
    showMessage(error instanceof SyntaxError ? 'JSON 解析失败：请上传严格有效的 JSON。' : `读取文件失败：${error.message}`, true);
  }
}

async function generateWritingPlan() {
  const query = document.querySelector('#writingPlanQuery').value.trim();
  if (!query) return showMessage('请先输入写作主题或检索词。', true);
  try {
    const paperId = selectedPaperId();
    const plan = await createWritingPlan(query, paperId ? [paperId] : []);
    const output = document.querySelector('#writingPlanResult');
    output.hidden = false;
    output.textContent = JSON.stringify(plan, null, 2);
  } catch (error) {
    showMessage(`生成写作证据计划失败：${error.message}`, true);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  TopNav.init({ currentPage: 'content-knowledge', mountId: 'topnav-mount' });
  initFilters(() => {
    updateSyncScope();
    loadItems();
  });
  document.querySelector('#loadMoreButton').addEventListener('click', () => {
    state.offset += state.limit;
    writeUrlState();
    loadItems({ append: true });
  });
  document.querySelectorAll('[data-advanced]').forEach((button) => {
    button.addEventListener('click', () => runAdvancedAction(button.dataset.advanced));
  });
  document.querySelector('#createBundleButton').addEventListener('click', generateBundle);
  document.querySelector('#copyBundleButton').addEventListener('click', copyBundleInstruction);
  document.querySelector('#validateBundleButton').addEventListener('click', validateBundle);
  document.querySelector('#refreshVerificationStatusButton').addEventListener('click', loadVerificationStatus);
  document.querySelector('#bundleDownloadButton').addEventListener('click', downloadBundle);
  const bundleFile = document.querySelector('#bundleFile');
  bundleFile.addEventListener('click', (event) => { event.currentTarget.value = ''; });
  bundleFile.addEventListener('change', (event) => readProposalFile(event.target.files[0]));
  const dropZone = document.querySelector('#bundleDropZone');
  ['dragenter', 'dragover'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.add('is-dragging'); }));
  ['dragleave', 'drop'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.remove('is-dragging'); }));
  dropZone.addEventListener('drop', (event) => readProposalFile(event.dataTransfer.files[0]));
  document.querySelector('#writingPlanButton').addEventListener('click', generateWritingPlan);
  updateSyncScope();
  loadItems();
});
