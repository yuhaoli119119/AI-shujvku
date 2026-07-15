import { state, setSelected, writeUrlState } from './state.js';
import { initFilters } from './filters.js';
import { listItems, getItem, reviewItem, advancedAction } from './api.js';
import { renderList, renderListError } from './render-list.js';
import { renderDetail } from './render-detail.js';
import { renderReview, isReviewable } from './review-actions.js';

const listRoot = document.querySelector('#listRoot');
const detailRoot = document.querySelector('#detailRoot');
const reviewRoot = document.querySelector('#reviewRoot');

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
    return { label: `当前论文：${item.paper_code || item.paper_id}`, filters: { ...state.filters, paper_id: item.paper_id } };
  }
  if (state.filters.paper_id) return { label: `当前论文：${state.filters.paper_id}`, filters: state.filters };
  if (state.filters.library_name) return { label: `当前文献库：${state.filters.library_name}`, filters: state.filters };
  return null;
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
    await advancedAction(action, scope?.filters || state.filters);
    showMessage(action === 'sync' ? '内容索引已同步，正在刷新列表。' : '高级操作已提交；结果以服务端返回状态为准。');
    if (action === 'sync') await loadItems();
  } catch (error) {
    showMessage(`高级操作失败：${error.message}`, true);
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
  updateSyncScope();
  loadItems();
});
