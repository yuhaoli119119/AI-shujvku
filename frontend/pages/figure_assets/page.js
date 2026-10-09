"use strict";

const state = { papers: [], files: [], assets: [], selected: null, paperId: "" };
let cropSubmitting = false, editSubmitting = false, loadedPaperGeneration = null;

function $(id) { return document.getElementById(id); }

function status(message, kind) {
  const area = $("statusArea");
  if (!message) { area.innerHTML = ""; return; }
  const cls = kind === "error" ? "rebuild-error" : kind === "progress" ? "rebuild-progress" : "rebuild-empty";
  area.innerHTML = `<div class="${cls}" role="status">${message}</div>`;
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options));
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { const payload = await response.json(); detail = payload.detail || detail; } catch (_) {}
    throw new Error(String(detail));
  }
  return response.json();
}

async function loadPapers() {
  status("正在读取文献列表…", "progress");
  const data = await api("/api/rebuild/papers?limit=200");
  state.papers = data.items;
  $("paperSelect").innerHTML = state.papers.map((paper) => `
    <option value="${paper.id}">${paper.paper_code || paper.serial_number || paper.id} · ${paper.title || "无标题"}</option>
  `).join("");
  const initial = new URLSearchParams(location.search).get("paper_id");
  if (initial && state.papers.some((paper) => paper.id === initial)) $("paperSelect").value = initial;
  status("");
  await loadCurrentPaper();
}

function syncWriteControls() {
  const loaded = !!state.paperId && loadedPaperGeneration === paperLoadGeneration;
  const source = state.files.some(file => file.id === $("cropFile").value && file.paper_id === state.paperId);
  const selected = state.selected && state.selected.paper_id === state.paperId && state.assets.some(asset => asset.id === state.selected.id && asset.paper_id === state.paperId);
  $("cropBtn").disabled = !loaded || cropSubmitting || !source;
  $("editBtn").disabled = !loaded || editSubmitting || !selected;
}

async function loadCurrentPaper() {
  closeContextViewer();
  const loadGeneration = ++paperLoadGeneration;
  loadedPaperGeneration = null;
  const paperId = $("paperSelect").value;
  state.paperId = paperId;
  state.selected = null;
  state.files = [];
  state.assets = [];
  renderFiles();
  renderAssets();
  syncWriteControls();
  if (!paperId) return;
  status("正在读取图表资料…", "progress");
  try {
    const [files, assets] = await Promise.all([
      api(`/api/rebuild/papers/${paperId}/files`),
      api(`/api/rebuild/papers/${paperId}/assets`),
    ]);
    if (loadGeneration !== paperLoadGeneration || state.paperId !== paperId) return;
    state.files = files.items;
    state.assets = assets.items;
    loadedPaperGeneration = loadGeneration;
    renderFiles();
    renderAssets();
    syncWriteControls();
    status("");
  } catch (error) {
    if (loadGeneration === paperLoadGeneration) status(`读取失败：${error.message}`, "error");
  }
}

function renderFiles() {
  if (!state.files.length) {
    $("fileList").innerHTML = `<div class="rebuild-empty">尚未关联正文/SI</div>`;
    $("cropFile").innerHTML = "";
    return;
  }
  $("fileList").innerHTML = state.files.map((file) => `
    <div style="margin-bottom:8px">
      <strong>${file.role === "main" ? "正文" : "SI"}</strong>
      <div>${file.original_filename}</div>
      <a href="/api/rebuild/files/${file.id}/preview" target="_blank" rel="noopener">预览 PDF</a>
    </div>
  `).join("");
  $("cropFile").innerHTML = state.files.map((file) => `
    <option value="${file.id}">${file.role === "main" ? "正文" : "SI"} · ${file.original_filename}</option>
  `).join("");
}


function assetText(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function supersedingAsset(asset, assets) {
  const provenance = asset.provenance;
  if (provenance?.lifecycle !== "superseded" || typeof provenance.reason !== "string" || !provenance.reason.trim()) return null;
  const id = provenance.superseded_by_asset_id;
  if (typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) return null;
  const matches = assets.filter(item => item.id === id);
  if (matches.length !== 1) return null;
  const replacement = matches[0];
  if (replacement.id === asset.id || replacement.paper_id !== asset.paper_id ||
      replacement.provenance?.lifecycle === "superseded" ||
      !["draft","reviewed","corrected"].includes(replacement.status) ||
      !asset.file_id || replacement.file_id !== asset.file_id ||
      replacement.asset_type !== asset.asset_type || replacement.figure_label !== asset.figure_label ||
      (replacement.subfigure_label || "") !== (asset.subfigure_label || "")) return null;
  return replacement;
}

let contextGeneration = 0, contextAbort = null, contextBlobUrl = null, paperLoadGeneration = 0;

function resolveContextView(asset, assets = state.assets, files = state.files, paperId = state.paperId) {
  const marker = asset.provenance?.context_view;
  if (marker == null) return null;
  const requireContext = (ok, code) => { if (!ok) throw new Error(code); };
  try {
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    requireContext(marker && typeof marker === "object" && !Array.isArray(marker) && marker.schema === "litai_context_roi_v1", "invalid_marker");
    requireContext(asset.asset_type === "subfigure" && asset.paper_id === paperId && uuid.test(marker.context_asset_id), "invalid_target");
    const matches = assets.filter(item => item.id === marker.context_asset_id);
    requireContext(matches.length === 1, "missing_or_duplicate_context");
    const context = matches[0];
    requireContext(context.id !== asset.id && context.paper_id === asset.paper_id && context.asset_type === "figure" && !context.subfigure_label, "context_identity_mismatch");
    requireContext(["draft", "reviewed", "corrected"].includes(context.status) && context.provenance?.lifecycle !== "superseded" && !context.provenance?.context_view, "context_not_current_whole_figure");
    requireContext(asset.file_id && context.file_id === asset.file_id && asset.figure_label && context.figure_label === asset.figure_label, "source_mismatch");
    const registered = files.filter(file => file.id === asset.file_id && file.paper_id === paperId);
    requireContext(registered.length === 1, "source_not_registered");
    const pages = context.page_numbers?.filter(page => Number.isInteger(page) && page > 0 && asset.page_numbers?.includes(page)) || [];
    const pageNumber = marker.context_page_number ?? (pages.length === 1 ? pages[0] : null);
    requireContext(Number.isInteger(pageNumber) && pages.includes(pageNumber), "source_page_ambiguous");
    requireContext(Number.isInteger(marker.context_asset_version) && marker.context_asset_version > 0 && context.version === marker.context_asset_version && context.image_url, "context_version_or_image_missing");
    requireContext(typeof marker.context_image_sha256 === "string" && /^[a-f0-9]{64}$/i.test(marker.context_image_sha256), "invalid_image_hash");
    requireContext([marker.context_width_px, marker.context_height_px].every(n => Number.isInteger(n) && n > 0 && n <= 50000) && marker.context_width_px * marker.context_height_px <= 100000000, "invalid_image_dimensions");
    requireContext(marker.roi_coordinate_space === "normalized_context_png" && Array.isArray(marker.roi) && marker.roi.length === 4 && marker.roi.every(n => typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 1) && marker.roi[0] < marker.roi[2] && marker.roi[1] < marker.roi[3], "invalid_roi");
    const review = marker.review;
    requireContext(typeof marker.reason === "string" && marker.reason.trim() && marker.subfigure_crop_assessment === "failed" && review?.state === "approved_context_locator" && typeof review.evidence_id === "string" && review.evidence_id.trim() && typeof review.checked_by === "string" && review.checked_by.trim() && typeof review.checked_at === "string" && Number.isFinite(Date.parse(review.checked_at)) && typeof review.evidence_sha256 === "string" && /^[a-f0-9]{64}$/i.test(review.evidence_sha256), "context_review_missing");
    return { marker, context, pageNumber };
  } catch (error) { return { error: error.message }; }
}

function contextViewerReady() {
  return ["contextViewer", "contextClose", "contextTitle", "contextReason", "contextLoadStatus", "contextCaption", "contextStage", "contextImage", "contextOverlay", "contextRoi", "contextOriginal", "contextWhole", "contextPdf"].every(id => !!$(id)) && typeof $("contextViewer").showModal === "function";
}

function contextAction(asset) {
  const resolved = resolveContextView(asset);
  if (!resolved) return "";
  if (!contextViewerReady()) return '<p class="rebuild-context-warning">整图上下文查看功能已更新，请刷新页面后使用；原裁图仍保留。</p>';
  if (resolved.error) return '<p class="rebuild-context-warning">整图定位资料无效，仍保留原裁图。</p>';
  return `<p class="rebuild-context-warning">独立子图裁图未通过核对；可另看整图上下文与位置。</p><button class="rebuild-btn ghost" type="button" data-context="${assetText(asset.id)}">查看整图及子图位置</button>`;
}

function closeContextViewer() {
  contextGeneration++;
  contextAbort?.abort(); contextAbort = null;
  if (contextBlobUrl) URL.revokeObjectURL(contextBlobUrl);
  contextBlobUrl = null;
  const viewer = $("contextViewer");
  if (viewer?.open) viewer.close();
  $("contextImage")?.removeAttribute("src");
  if ($("contextCaption")) $("contextCaption").textContent = "";
  if ($("contextStage")) $("contextStage").hidden = true;
}

async function openContextViewer(assetId) {
  if (!contextViewerReady()) { status("请刷新页面后使用整图上下文查看功能；原裁图仍保留。", "error"); return; }
  closeContextViewer();
  const asset = state.assets.find(item => item.id === assetId), resolved = asset && resolveContextView(asset);
  if (!resolved || resolved.error) { status("整图定位资料无效，仍保留原裁图。", "error"); return; }
  const generation = contextGeneration, paperId = state.paperId, snapshot = JSON.stringify(resolved.marker);
  const { context, marker, pageNumber } = resolved, viewer = $("contextViewer");
  const current = () => generation === contextGeneration && state.paperId === paperId && viewer.open;
  $("contextTitle").textContent = `${asset.figure_label} ${asset.subfigure_label || ""} · 整图上下文＋子图位置`;
  $("contextReason").textContent = marker.reason;
  $("contextCaption").textContent = typeof context.caption === "string" && context.caption.trim()
    ? context.caption : "整图资料未提供图注；请通过下方同一来源 PDF 核对完整图注。";
  $("contextLoadStatus").textContent = "正在核对整图文件…";
  $("contextOriginal").href = `/api/rebuild/assets/${asset.id}`;
  $("contextWhole").href = `/api/rebuild/assets/${context.id}`;
  $("contextPdf").href = `/api/rebuild/files/${asset.file_id}/preview#page=${pageNumber}`;
  viewer.showModal(); contextAbort = new AbortController();
  let localUrl = null;
  try {
    const response = await fetch(`/api/rebuild/assets/${context.id}`, { credentials: "same-origin", signal: contextAbort.signal });
    if (!response.ok) throw new Error("image_http_failed");
    const bytes = await response.arrayBuffer();
    if (!current()) return;
    if (bytes.byteLength > 32 * 1024 * 1024 || bytes.byteLength < 8) throw new Error("image_size_invalid");
    const signature = new Uint8Array(bytes, 0, 8);
    if (![137,80,78,71,13,10,26,10].every((n,i) => signature[i] === n)) throw new Error("image_not_png");
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(n => n.toString(16).padStart(2,"0")).join("");
    if (!current()) return;
    const freshAsset = state.assets.find(item => item.id === assetId), fresh = freshAsset && resolveContextView(freshAsset);
    if (!fresh || fresh.error || JSON.stringify(fresh.marker) !== snapshot || fresh.context.id !== context.id || digest !== marker.context_image_sha256.toLowerCase()) throw new Error("context_changed_or_hash_mismatch");
    localUrl = URL.createObjectURL(new Blob([bytes], { type: "image/png" })); contextBlobUrl = localUrl;
    const image = $("contextImage"); image.src = localUrl; await image.decode();
    if (!current()) return;
    if (image.naturalWidth !== marker.context_width_px || image.naturalHeight !== marker.context_height_px) throw new Error("image_dimensions_mismatch");
    image.alt = `${asset.figure_label} 完整上下文整图；边框仅定位子图 ${asset.subfigure_label || ""}`;
    const svg = $("contextOverlay"), box = $("contextRoi"), [x0,y0,x1,y1] = marker.roi;
    svg.setAttribute("viewBox", `0 0 ${image.naturalWidth} ${image.naturalHeight}`);
    for (const [key,value] of Object.entries({ x:x0*image.naturalWidth,y:y0*image.naturalHeight,width:(x1-x0)*image.naturalWidth,height:(y1-y0)*image.naturalHeight })) box.setAttribute(key,String(value));
    $("contextStage").hidden = false;
    $("contextLoadStatus").textContent = "整图文件已核对；边框仅表示子图位置，独立裁图仍未通过核对。";
  } catch (error) {
    if (!current()) return;
    $("contextStage").hidden = true; $("contextImage").removeAttribute("src");
    $("contextLoadStatus").textContent = "整图定位资料失效或读取失败；原裁图仍保留，可通过下方链接查看。";
  } finally {
    if (localUrl && (!current() || $("contextStage").hidden)) {
      URL.revokeObjectURL(localUrl); if (contextBlobUrl === localUrl) contextBlobUrl = null;
    }
  }
}

function renderAssets() {
  const superseded = state.assets.filter(asset => supersedingAsset(asset, state.assets));
  const visible = $("showSuperseded").checked ? state.assets : state.assets.filter(asset => !supersedingAsset(asset, state.assets));
  $("assetSummary").textContent = `${visible.length} 个图表/子图对象${superseded.length ? $("showSuperseded").checked ? `（含 ${superseded.length} 个历史版本）` : ` · 已隐藏 ${superseded.length} 个历史版本` : ""}`;
  const grid = $("assetGrid");
  if (!visible.length) {
    grid.innerHTML = `<div class="rebuild-empty" style="grid-column:1/-1">还没有图表对象；可由 AI 工具批量生成，也可在左侧创建。</div>`;
    return;
  }
  grid.innerHTML = visible.map((asset) => `
    <article class="rebuild-asset-card" data-id="${asset.id}">
      ${asset.image_url ? `<img src="${asset.image_url}" alt="${asset.figure_label || asset.asset_key}">` : `<div class="rebuild-empty">无裁图</div>`}
      <div class="rebuild-asset-body">
        ${supersedingAsset(asset,state.assets) ? `<div class="rebuild-panel" style="box-shadow:none"><span class="rebuild-tag warn">历史版本</span> 已由 ${assetText(supersedingAsset(asset,state.assets).figure_label || supersedingAsset(asset,state.assets).asset_key)} 替代。<p>${assetText(asset.provenance.reason)}</p><button class="rebuild-btn ghost" type="button" data-current="${assetText(supersedingAsset(asset,state.assets).id)}">查看当前版本</button></div>` : ""}
        <div>
          <span class="rebuild-tag">${asset.asset_type === "table" ? "表" : asset.asset_type === "subfigure" ? "子图" : "图"}</span>
          ${asset.figure_label ? `<span class="rebuild-tag">${asset.figure_label}</span>` : ""}
          ${asset.subfigure_label ? `<span class="rebuild-tag">${asset.subfigure_label}</span>` : ""}
          ${asset.unreadable_fields.length ? `<span class="rebuild-tag warn">局部不可读</span>` : ""}
          <span class="rebuild-tag">页 ${asset.page_numbers.join(", ") || "—"}</span>
        </div>
        <h3>${asset.figure_label || asset.asset_key}</h3>
        <p>${assetText(asset.caption || "未填写图注")}</p>
        <p><strong>解释：</strong>${asset.explanation || "未填写"}</p>
        <p><strong>坐标：</strong>${asset.x_axis_unit || "?"} / ${asset.y_axis_unit || "?"}</p>
        ${contextAction(asset)}
        <button class="rebuild-btn ghost" type="button" data-edit="${asset.id}">选择纠正</button>
      </div>
    </article>
  `).join("");
  grid.querySelectorAll("button[data-context]").forEach(button => button.addEventListener("click", () => openContextViewer(button.dataset.context)));
  grid.querySelectorAll("button[data-current]").forEach(button => button.addEventListener("click", () => selectAsset(button.dataset.current)));
  grid.querySelectorAll("button[data-edit]").forEach((button) => {
    button.addEventListener("click", () => selectAsset(button.dataset.edit));
  });
}

function selectAsset(id) {
  state.selected = state.assets.find((asset) => asset.id === id && asset.paper_id === state.paperId) || null;
  syncWriteControls();
  if (!state.selected) return;
  $("editCaption").value = state.selected.caption || "";
  $("editExplanation").value = state.selected.explanation || "";
  $("editContext").value = state.selected.context_text || "";
  $("editXUnit").value = state.selected.x_axis_unit || "";
  $("editYUnit").value = state.selected.y_axis_unit || "";
  $("editStatus").value = state.selected.status;
  syncWriteControls();
}

async function submitCrop(event) {
  event.preventDefault();
  if (cropSubmitting || $("cropBtn").disabled || !state.paperId || loadedPaperGeneration !== paperLoadGeneration) return;
  const sourceFile = state.files.find(file => file.id === $("cropFile").value && file.paper_id === state.paperId);
  if (!sourceFile) { status("请选择本篇文献已关联的来源文件。", "error"); return; }
  const paperId = state.paperId, generation = paperLoadGeneration;
  const current = () => state.paperId === paperId && paperLoadGeneration === generation;
  let regions;
  try { regions = JSON.parse($("regions").value); }
  catch (_) { status("裁剪区域 JSON 格式不正确", "error"); return; }
  const payload = {
    asset_key: $("assetKey").value.trim(),
    asset_type: $("assetType").value,
    file_id: sourceFile.id,
    figure_label: $("figureLabel").value.trim() || null,
    subfigure_label: $("subfigureLabel").value.trim() || null,
    caption: $("caption").value.trim() || null,
    explanation: $("explanation").value.trim() || null,
    regions,
    status: "draft",
  };
  cropSubmitting = true;
  syncWriteControls();
  status("正在从真实 PDF 渲染裁图…", "progress");
  try {
    await api(`/api/rebuild/papers/${paperId}/assets/crop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!current()) return;
    status("裁图已保存。");
    await loadCurrentPaper();
  } catch (error) {
    if (current()) status(`裁图失败：${error.message}`, "error");
  } finally {
    cropSubmitting = false;
    syncWriteControls();
  }
}

async function submitEdit(event) {
  event.preventDefault();
  if (editSubmitting || $("editBtn").disabled || loadedPaperGeneration !== paperLoadGeneration || !state.selected || state.selected.paper_id !== state.paperId) return;
  const paperId = state.paperId, generation = paperLoadGeneration, selectedId = state.selected.id;
  const current = () => state.paperId === paperId && paperLoadGeneration === generation && state.selected?.id === selectedId;
  const payload = {
    asset_key: state.selected.asset_key,
    asset_type: state.selected.asset_type,
    file_id: state.selected.file_id,
    figure_label: state.selected.figure_label,
    subfigure_label: state.selected.subfigure_label,
    caption: $("editCaption").value,
    explanation: $("editExplanation").value,
    context_text: $("editContext").value,
    x_axis_unit: $("editXUnit").value,
    y_axis_unit: $("editYUnit").value,
    status: $("editStatus").value,
  };
  editSubmitting = true;
  syncWriteControls();
  try {
    await api(`/api/rebuild/papers/${paperId}/assets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!current()) return;
    status("纠正已保存。");
    await loadCurrentPaper();
  } catch (error) {
    if (current()) status(`保存失败：${error.message}`, "error");
  } finally {
    editSubmitting = false;
    syncWriteControls();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (contextViewerReady()) {
    $("contextClose").addEventListener("click", closeContextViewer);
    $("contextViewer").addEventListener("cancel", event => { event.preventDefault(); closeContextViewer(); });
    $("contextViewer").addEventListener("close", () => { if (!$("contextViewer").open && (contextBlobUrl || contextAbort)) closeContextViewer(); });
  }
  TopNav.init({ currentPage: "figure-assets", mountId: "topnav-mount" });
  $("showSuperseded").addEventListener("change", renderAssets);
  $("paperSelect").addEventListener("change", loadCurrentPaper);
  $("cropFile").addEventListener("change", syncWriteControls);
  $("refreshBtn").addEventListener("click", loadCurrentPaper);
  $("cropForm").addEventListener("submit", submitCrop);
  $("editForm").addEventListener("submit", submitEdit);
  loadPapers().catch((error) => status(`初始化失败：${error.message}`, "error"));
});
