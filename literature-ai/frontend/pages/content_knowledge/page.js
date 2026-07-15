import { state, setSelected, writeUrlState } from './state.js';
import { initFilters } from './filters.js';
import {
  listItems,
  getItem,
  reviewItem,
  syncIndex,
  createReviewBundle,
  validateReviewBundle,
  applyReviewBundle,
  finalizeReviewBundle,
  createWritingPlan,
} from './api.js';
import { renderList, renderListError } from './render-list.js';
import { renderDetail } from './render-detail.js';
import { renderReview, isReviewable } from './review-actions.js';

const listRoot = document.querySelector('#listRoot');
const detailRoot = document.querySelector('#detailRoot');
const reviewRoot = document.querySelector('#reviewRoot');
let currentBundle = null;

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

function renderBundleStatus(text) {
  document.querySelector('#bundleControls').hidden = false;
  document.querySelector('#bundleStatus').textContent = text;
}

async function generateBundle() {
  const paperId = selectedPaperId();
  if (!paperId) return showMessage('请先选择一篇论文，再生成审核包。', true);
  try {
    currentBundle = await createReviewBundle({ paper_id: paperId, run_id: state.filters.run_id || undefined });
    const manifest = currentBundle.manifest || {};
    renderBundleStatus(`审核包已生成：${currentBundle.bundle_id}；范围 ${manifest.scope_type || 'paper'}；项目 ${manifest.item_count ?? (manifest.items || []).length}。`);
  } catch (error) {
    showMessage(`生成审核包失败：${error.message}`, true);
  }
}

async function copyBundleInstruction() {
  if (!currentBundle) await generateBundle();
  if (!currentBundle) return;
  const text = `${currentBundle.manifest?.instructions || ''}\n\nRETURN TEMPLATE:\n${JSON.stringify(currentBundle.return_template || {}, null, 2)}`;
  try {
    await navigator.clipboard.writeText(text);
    showMessage('IDE AI 指令与 JSON 模板已复制。');
  } catch (_) {
    document.querySelector('#bundleResultInput').value = text;
    showMessage('无法使用剪贴板，指令已显示在输入框。');
  }
}

async function validateBundle() {
  if (!currentBundle) return showMessage('请先生成审核包。', true);
  try {
    const result = JSON.parse(document.querySelector('#bundleResultInput').value);
    const response = await validateReviewBundle(currentBundle.bundle_id, result);
    renderBundleStatus(`回传校验通过。${response.unresolved?.length ? `仍有 ${response.unresolved.length} 项未解决。` : '可应用审核回传。'}`);
  } catch (error) {
    showMessage(`回传校验失败：${error.message}`, true);
  }
}

async function applyBundle() {
  if (!currentBundle) return showMessage('请先生成并校验审核包。', true);
  try {
    const response = await applyReviewBundle(currentBundle.bundle_id);
    const unresolved = response.needs_human || response.unresolved?.length || 0;
    renderBundleStatus(`已应用 ${response.applied ?? 0} 项。${unresolved ? `仍有 ${unresolved} 项未解决。` : '可完成审核。'}`);
    document.querySelector('#finalizeBundleButton').hidden = Boolean(unresolved);
    await loadItems();
  } catch (error) {
    showMessage(`应用审核回传失败：${error.message}`, true);
  }
}

async function finalizeBundle() {
  if (!currentBundle) return;
  try {
    await finalizeReviewBundle(currentBundle.bundle_id);
    renderBundleStatus('审核已完成。');
    await loadItems();
  } catch (error) {
    showMessage(`无法完成审核：${error.message}`, true);
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
  document.querySelector('#applyBundleButton').addEventListener('click', applyBundle);
  document.querySelector('#finalizeBundleButton').addEventListener('click', finalizeBundle);
  document.querySelector('#writingPlanButton').addEventListener('click', generateWritingPlan);
  updateSyncScope();
  loadItems();
});
