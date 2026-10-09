/* ============================================================
   只读文献知识库 · readonly frontend（融合版）
   - 公开模式：整库浏览检索（/api/papers/...）
   - 分享模式：?token=xxx，仅访问 token 授权范围（/api/share/...）
   本文件只发起 GET 请求；不存在任何新增/修改/删除类操作。
   ============================================================ */
"use strict";

const PAGE_SIZE = 20;
const state = {
  shareToken: "",
  libraries: [],
  library: "",
  q: "",
  paperType: "",
  hasPdf: "",
  sortBy: "year_serial",
  sortOrder: "desc",
  offset: 0,
  papers: [],
  total: 0,
  selectedId: null,
  detail: null,
  dftLoadedAll: false,
  correctionsLoaded: false,
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function fieldVal(x) {
  if (x === null || x === undefined) return "";
  if (Array.isArray(x)) return x.map(fieldVal).filter(Boolean).join(", ");
  if (typeof x === "object") {
    return x.value ?? x.raw ?? x.display ?? x.text ?? x.name ??
      (x.label ? x.label : JSON.stringify(x));
  }
  return String(x);
}
function fmtAuthors(a) {
  if (!a) return "—";
  if (Array.isArray(a)) return a.length ? a.map(fieldVal).join(", ") : "—";
  return String(a);
}
function fmtBytes(n) {
  if (!n) return "—";
  const mb = n / 1048576;
  return mb >= 1 ? mb.toFixed(1) + " MB" : (n / 1024).toFixed(0) + " KB";
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 2400);
}
async function api(path) {
  const resp = await fetch(path, { method: "GET", headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  const txt = await resp.text();
  return txt ? JSON.parse(txt) : null;
}
function debounce(fn, ms) {
  let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

/* ---------------- 库 & 列表 ---------------- */
async function loadLibraries() {
  try {
    const rows = await api("/api/papers/libraries");
    state.libraries = Array.isArray(rows) ? rows : [];
    const sel = $("#libSelect");
    const urlLib = new URLSearchParams(location.search).get("library") || "";
    const activeGuess = urlLib ||
      (state.libraries.find(l => l.name === "锂硫双原子") || {}).name ||
      (state.libraries.slice().sort((a, b) => (b.paper_count || 0) - (a.paper_count || 0))[0] || {}).name || "";
    state.library = activeGuess;
    sel.innerHTML = state.libraries
      .map(l => `<option value="${esc(l.name)}" ${l.name === activeGuess ? "selected" : ""}>${esc(l.name)}（${l.paper_count || 0}）</option>`)
      .join("");
    const cur = state.libraries.find(l => l.name === state.library);
    state.total = cur ? (cur.paper_count || 0) : 0;
    await loadPapers(true);
  } catch (e) {
    $("#listMeta").textContent = "文献库加载失败：" + e.message;
  }
}

function listQuery() {
  if (state.shareToken) {
    return `/api/share/${encodeURIComponent(state.shareToken)}/papers?limit=${PAGE_SIZE}&offset=${state.offset}`;
  }
  const p = new URLSearchParams();
  if (state.library) p.set("library_name", state.library);
  if (state.q.trim()) p.set("q", state.q.trim());
  if (state.paperType) p.set("paper_type", state.paperType);
  if (state.hasPdf) p.set("has_pdf", state.hasPdf);
  p.set("sort_by", state.sortBy);
  p.set("sort_order", state.sortOrder);
  p.set("limit", String(PAGE_SIZE));
  p.set("offset", String(state.offset));
  return "/api/papers/?" + p.toString();
}

async function loadPapers(reset) {
  if (reset) { state.offset = 0; state.correctionsLoaded = false; }
  const list = $("#paperList");
  list.innerHTML = '<div class="list-skeleton">正在检索文献…</div>';
  try {
    const resp = await api(listQuery());
    // 兼容公开模式(数组)与分享模式({items,limit,offset})
    const rows = Array.isArray(resp) ? resp : (resp.items || []);
    state.papers = rows;
    if (state.shareToken) {
      state.total = state.offset + state.papers.length + (state.papers.length === PAGE_SIZE ? 1 : 0);
    } else if (state.q.trim() || state.paperType || state.hasPdf) {
      state.total = state.offset + state.papers.length + (state.papers.length === PAGE_SIZE ? 1 : 0);
    } else {
      const cur = state.libraries.find(l => l.name === state.library);
      state.total = cur ? (cur.paper_count || 0) : state.papers.length;
    }
    renderList();
    renderPager();
    syncUrl();
  } catch (e) {
    list.innerHTML = `<div class="list-skeleton">加载失败：${esc(e.message)}</div>`;
  }
}

function typeLabel(t) {
  const m = { A: "A", B: "B", C: "C", R: "综述", supplementary: "SI", SI: "SI" };
  return m[t] || (t ? t : "");
}

function renderList() {
  const list = $("#paperList");
  if (!state.papers.length) {
    list.innerHTML = '<div class="empty-block">没有符合条件的文献</div>';
    return;
  }
  list.innerHTML = state.papers.map(p => {
    const counts = p.counts || {};
    const t = p.paper_type || "";
    const tcls = t === "supplementary" ? "supplementary" : t;
    return `
    <div class="p-card ${p.id === state.selectedId ? "active" : ""}" data-id="${esc(p.id)}">
      <div class="p-top">
        <span class="p-year">${esc(p.year || "—")}</span>
        <span class="p-journal">${esc(p.journal || "未标注期刊")}</span>
      </div>
      <div class="p-title">${esc(p.title || "（无标题）")}</div>
      ${p.title_zh ? `<div class="p-title-zh">${esc(p.title_zh)}</div>` : ""}
      <div class="p-foot">
        ${t ? `<span class="mini-chip type-${esc(tcls)}">${esc(typeLabel(t))}</span>` : ""}
        ${p.pdf_exists ? '<span class="mini-chip pdf">PDF</span>' : ""}
        ${counts.figures ? `<span class="mini-chip">图 ${counts.figures}</span>` : ""}
        ${counts.sections ? `<span class="mini-chip">章节 ${counts.sections}</span>` : ""}
        ${counts.dft_results ? `<span class="mini-chip">DFT ${counts.dft_results}</span>` : ""}
      </div>
    </div>`;
  }).join("");
  $$(".p-card", list).forEach(c => c.addEventListener("click", () => selectPaper(c.dataset.id)));
}

function renderPager() {
  const start = state.papers.length ? state.offset + 1 : 0;
  const end = state.offset + state.papers.length;
  const curPage = Math.floor(state.offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
  const scopeLabel = state.shareToken ? "分享范围" : esc(state.library);
  $("#listMeta").textContent = `${scopeLabel} · 共 ${state.total} 篇 · 显示 ${start}–${end}`;
  $("#pageInfo").textContent = `${curPage} / ${pages}`;
  $("#prevPage").disabled = state.offset === 0;
  $("#nextPage").disabled = state.papers.length < PAGE_SIZE;
}

/* ---------------- 详情 ---------------- */
function detailUrl(id) {
  return state.shareToken
    ? `/api/share/${encodeURIComponent(state.shareToken)}/papers/${encodeURIComponent(id)}?mode=full`
    : `/api/papers/${encodeURIComponent(id)}?mode=full`;
}
function correctionsUrl(id) {
  return state.shareToken
    ? `/api/share/${encodeURIComponent(state.shareToken)}/corrections/${encodeURIComponent(id)}`
    : `/api/papers/${encodeURIComponent(id)}/corrections`;
}

async function selectPaper(id) {
  state.selectedId = id;
  state.dftLoadedAll = false;
  state.correctionsLoaded = false;
  renderList();
  $("#detailEmpty").hidden = true;
  $("#detailBody").hidden = false;
  setTab("overview");
  $("#pOverview").innerHTML = '<div class="loading">正在加载文献详情…</div>';
  try {
    const d = await api(detailUrl(id));
    state.detail = d;
    renderDetail();
    syncUrl();
  } catch (e) {
    $("#pOverview").innerHTML = `<div class="empty-block">详情加载失败：${esc(e.message)}</div>`;
  }
}

function metaItem(k, v) {
  return `<div class="meta-item"><span class="mk">${k}</span><span class="mv">${v}</span></div>`;
}
function statBadge(n, label, cls = "") {
  return `<span class="badge ${cls}">${label} ${n ?? 0}</span>`;
}
function statusPill(s) {
  const v = String(s || "").toLowerCase();
  if (/(ml_ready|verified|safe|export|ready|approved|完成)/.test(v)) return `<span class="pill ok">${esc(s)}</span>`;
  if (/(candidate|pending|system|待|requires)/.test(v)) return `<span class="pill wait">${esc(s)}</span>`;
  return s ? `<span class="pill gray">${esc(s)}</span>` : "";
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  $("#dTitle").textContent = d.title || "（无标题）";
  $("#dTitleZh").textContent = d.title_zh || "";
  const qr = d.pdf_quality_report || {};
  const pages = qr.metrics ? qr.metrics.page_count : null;
  const doiHtml = d.doi
    ? `DOI：<a href="https://doi.org/${esc(d.doi)}" target="_blank" rel="noopener">${esc(d.doi)}</a>`
    : "DOI：—";
  $("#dMeta").innerHTML = [
    metaItem("作者", esc(fmtAuthors(d.authors))),
    metaItem("期刊", esc(d.journal || "—") + (d.impact_factor ? `（IF ${d.impact_factor}）` : "")),
    metaItem("年份", esc(d.year || "—")),
    metaItem("文献类型", esc(typeLabel(d.paper_type) || "—")),
    metaItem("所属库", esc(d.library_name || "—")),
    metaItem("PDF", (d.pdf_exists ? `${esc(fmtBytes(d.pdf_file_size || d.pdf_size))}${pages ? ` · ${pages} 页` : ""}` : "无 PDF")),
    metaItem("解析状态", esc(d.workflow_status || "—")),
    metaItem("短号", esc(d.paper_code || d.serial_number || "—")),
  ].join("");
  $("#dDoi").innerHTML = doiHtml;
  const counts = d.counts || {};
  $("#dBadges").innerHTML = [
    statBadge(counts.sections, "章节", "gray"),
    statBadge((d.figures || []).length, "图表"),
    statBadge((d.tables || []).length, "数据表", "gray"),
    statBadge((d.dft_results_items || []).length, "DFT 条目"),
    statBadge((d.mechanism_claims_items || []).length, "机理论断", "gray"),
    statBadge((d.paper_notes || []).length, "批注"),
  ].join("");
  const pdfBtn = $("#dPdfBtn");
  if (d.pdf_exists) {
    pdfBtn.href = `/api/papers/${encodeURIComponent(d.id)}/pdf`;
    pdfBtn.style.display = "";
    $("#dPdfInlineBtn").style.display = "";
  } else { pdfBtn.style.display = "none"; $("#dPdfInlineBtn").style.display = "none"; }

  renderOverview();
  renderSections();
  renderFigures();
  renderTables();
  renderDft(false);
  renderMechanism();
  renderNotes();
  renderTranslation();
  // corrections 懒加载，切换到该 tab 时再请求
  $("#pCorrections").innerHTML = '<div class="loading">切换到此标签页时加载更正历史…</div>';
}

function renderOverview() {
  const d = state.detail;
  const abs = d.abstract || "";
  const absZh = d.abstract_zh || "";
  $("#pOverview").innerHTML = `
    <div class="section-card">
      <h3>摘要 Abstract</h3>
      ${abs ? `<div class="abstract-text">${esc(abs)}</div>` : '<div class="muted">暂无摘要</div>'}
      ${absZh ? `<h3 style="margin-top:16px;">中文摘要</h3><div class="abstract-text">${esc(absZh)}</div>` : ""}
    </div>
    <div class="section-card">
      <h3>文献概况</h3>
      <div class="kvlist">
        <div class="kv"><div class="kk">PDF 质量</div><div class="vv">${esc(d.pdf_quality_status || "—")}</div></div>
        <div class="kv"><div class="kk">类型判定来源</div><div class="vv">${esc(d.classification_source || "—")}</div></div>
        <div class="kv"><div class="kk">入库时间</div><div class="vv">${esc((d.created_at || "").slice(0, 10))}</div></div>
        <div class="kv"><div class="kk">Markdown</div><div class="vv">${d.markdown_path ? "已解析" : "—"}</div></div>
      </div>
    </div>`;
}

function renderSections() {
  const secs = state.detail.sections || [];
  if (!secs.length) { $("#pSections").innerHTML = '<div class="empty-block">暂无解析正文章节</div>'; return; }
  $("#pSections").innerHTML = secs.map(s => `
    <div class="doc-section">
      <h4>${esc(s.section_title || "未命名章节")}<span class="sec-type-tag">${esc(s.section_type || "")}</span></h4>
      <div class="sec-text">${esc(s.text || "")}</div>
    </div>`).join("");
}

function renderFigures() {
  const figs = state.detail.figures || [];
  if (!figs.length) { $("#pFigures").innerHTML = '<div class="empty-block">暂无解析图表</div>'; return; }
  $("#pFigures").innerHTML = `<div class="fig-grid">${figs.map((f, i) => `
    <div class="fig-card" data-i="${i}">
      <img class="fig-thumb" loading="lazy" src="${esc(f.asset_url || "")}" alt="${esc(f.figure_label || "figure")}">
      <div class="fig-info">
        <div class="fig-cap">${esc(f.caption || f.content_summary || "（无图注）")}</div>
        <div class="fig-page">${esc(f.figure_label || "")} ${f.page ? "· 第 " + f.page + " 页" : ""} ${f.figure_role ? "· " + esc(f.figure_role) : ""}</div>
      </div>
    </div>`).join("")}</div>`;
  $$(".fig-card", $("#pFigures")).forEach(c => c.addEventListener("click", () => openLightbox(figs[+c.dataset.i])));
}

function mdTable(md) {
  if (!md || !md.includes("|")) return `<div class="abstract-text">${esc(md || "")}</div>`;
  const lines = md.split("\n").map(l => l.trim()).filter(l => l.startsWith("|"));
  if (lines.length < 2) return `<div class="abstract-text">${esc(md)}</div>`;
  const split = l => l.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  const head = split(lines[0]);
  const body = lines.slice(2).filter(l => !/^[\s|:-]+$/.test(l));
  return `<div class="data-table-wrap"><table class="mk-table">
    <thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${body.map(r => `<tr>${split(r).map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

function renderTables() {
  const tabs = state.detail.tables || [];
  if (!tabs.length) { $("#pTables").innerHTML = '<div class="empty-block">暂无解析数据表</div>'; return; }
  $("#pTables").innerHTML = tabs.map(t => `
    <div class="section-card">
      <h3>${esc(t.caption || "数据表")} <span class="muted small">${t.page ? "· 第 " + t.page + " 页" : ""}</span></h3>
      ${mdTable(t.markdown_content)}
    </div>`).join("");
}

function renderDft(loadedAll) {
  const d = state.detail;
  const samples = d.catalyst_samples_items || [];
  const settings = d.dft_settings_items || [];
  let results = d.dft_results_items || [];
  const total = (d.dft_results_page || {}).total || results.length;
  const canLoadMore = !loadedAll && results.length < total;

  let html = "";
  if (samples.length) {
    html += `<div class="subgroup-title">催化剂样本（${samples.length}）</div><div class="kvlist">` +
      samples.map(s => `
        <div class="kv">
          <div class="vv">${esc(fieldVal(s.name))}</div>
          <div class="kk">${esc(fieldVal(s.catalyst_type))} · 金属中心 ${esc(fieldVal(s.metal_centers) || "—")} · 载体 ${esc(fieldVal(s.support) || "—")}</div>
        </div>`).join("") + `</div>`;
  }
  if (settings.length) {
    html += `<div class="subgroup-title">DFT 计算设置（${settings.length}）</div>` +
      settings.map(s => {
        const rows = ["software", "functional", "dispersion_correction", "pseudopotential", "cutoff_energy", "k_points", "vacuum_thickness"]
          .map(k => s[k] ? `<div class="kv"><div class="kk">${k}</div><div class="vv">${esc(fieldVal(s[k]))}</div></div>` : "").join("");
        return `<div class="kvlist" style="margin-bottom:10px;">${rows}</div>`;
      }).join("");
  }
  html += `<div class="subgroup-title">DFT 结果数据（已载 ${results.length} / ${total}）</div>`;
  if (!results.length) {
    html += '<div class="empty-block">暂无 DFT 结果</div>';
  } else {
    html += `<div class="data-table-wrap"><table class="mk-table dft-table">
      <thead><tr><th>催化剂</th><th>吸附质</th><th>性质类型</th><th>数值</th><th>反应步骤</th><th>来源</th><th>证据原文</th><th>状态</th></tr></thead>
      <tbody>${results.map(r => `
        <tr>
          <td>${esc(r.bound_catalyst_sample ? fieldVal(r.bound_catalyst_sample.name) : fieldVal(r.catalyst))}</td>
          <td>${esc(fieldVal(r.adsorbate) || "—")}</td>
          <td>${esc(r.property_type || "—")}</td>
          <td class="num">${r.value ?? ""} ${esc(r.unit || "")}</td>
          <td>${esc(r.reaction_step || "—")}</td>
          <td class="small">${esc([r.source_section, r.source_figure].filter(Boolean).join(" / ") || "—")}</td>
          <td class="small">${esc(r.evidence_text || "—")}</td>
          <td>${statusPill(r.candidate_status)}</td>
        </tr>`).join("")}</tbody></table></div>`;
    if (canLoadMore) html += `<button class="btn ghost" id="loadAllDft" type="button">加载全部 ${total} 条 DFT 结果</button>`;
  }
  $("#pDft").innerHTML = html;
  const b = $("#loadAllDft");
  if (b) b.addEventListener("click", loadAllDft);
}

async function loadAllDft() {
  const b = $("#loadAllDft"); b.disabled = true; b.textContent = "加载中…";
  try {
    const r = await api(`/api/papers/${encodeURIComponent(state.detail.id)}/dft-results?offset=0&limit=500`);
    state.detail.dft_results_items = r.items || [];
    state.detail.dft_results_page = { total: r.total || (r.items || []).length };
    state.dftLoadedAll = true;
    renderDft(true);
  } catch (e) { toast("加载失败：" + e.message); b.disabled = false; }
}

function renderMechanism() {
  const items = state.detail.mechanism_claims_items || [];
  if (!items.length) { $("#pMechanism").innerHTML = '<div class="empty-block">暂无机理论断</div>'; return; }
  $("#pMechanism").innerHTML = items.map((m, i) => `
    <div class="section-card">
      <h3>${i + 1}. ${esc(fieldVal(m.claim_type) || "机理论断")} ${statusPill(m.candidate_status)}</h3>
      <div class="abstract-text">${esc(fieldVal(m.claim_text))}</div>
      <div class="kvlist" style="margin-top:10px;">
        ${m.key_species ? `<div class="kv"><div class="kk">关键物种</div><div class="vv">${esc(fieldVal(m.key_species))}</div></div>` : ""}
        ${m.mechanism_direction ? `<div class="kv"><div class="kk">方向</div><div class="vv">${esc(fieldVal(m.mechanism_direction))}</div></div>` : ""}
      </div>
    </div>`).join("");
}

/* ---------- Notes (AI 批注) ---------- */
function renderNotes() {
  const notes = state.detail.paper_notes || [];
  if (!notes.length) { $("#pNotes").innerHTML = '<div class="empty-block">暂无 AI 批注</div>'; return; }
  $("#pNotes").innerHTML = notes.map(n => `
    <div class="note-card">
      <div class="note-head">
        <span class="note-source">${esc(n.source || "AI")}</span>
        ${n.field_name ? `<span class="note-field">${esc(n.field_name)}</span>` : ""}
        ${n.section_title ? `<span class="note-loc">📑 ${esc(n.section_title)}${n.page ? " · p." + n.page : ""}</span>` : (n.page ? `<span class="note-loc">p.${esc(n.page)}</span>` : "")}
      </div>
      ${n.quoted_text ? `<div class="note-quote">${esc(n.quoted_text)}</div>` : ""}
      <div class="note-content">${esc(n.content || "")}</div>
    </div>`).join("");
}

/* ---------- Corrections (更正历史，懒加载) ---------- */
async function loadCorrections() {
  if (state.correctionsLoaded) return;
  state.correctionsLoaded = true;
  const id = state.detail.id;
  try {
    const r = await api(correctionsUrl(id));
    const items = r.items || (Array.isArray(r) ? r : []);
    const total = r.total != null ? r.total : items.length;
    if (!items.length) { $("#pCorrections").innerHTML = '<div class="empty-block">暂无更正记录</div>'; return; }
    $("#pCorrections").innerHTML = `
      <div class="corr-toolbar"><span class="muted small">共 ${total} 条更正记录</span></div>
      ${items.map(c => `
        <div class="corr-card">
          <div class="corr-head">
            <span class="corr-field">${esc(c.field_name || "字段")}</span>
            <span class="corr-source">${esc(c.source || "")}</span>
            ${statusPill(c.status)}
            <span class="corr-date">${esc((c.created_at || "").slice(0, 10))}</span>
          </div>
          <div class="corr-value">${esc(c.proposed_value != null ? String(c.proposed_value) : "")}</div>
          ${c.reason ? `<div class="corr-comment">💬 ${esc(c.reason)}</div>` : ""}
        </div>`).join("")}`;
  } catch (e) {
    state.correctionsLoaded = false;
    $("#pCorrections").innerHTML = `<div class="empty-block">更正记录加载失败：${esc(e.message)}</div>`;
  }
}

function renderTranslation() {
  const t = state.detail.full_translation_zh;
  $("#pTranslation").innerHTML = t
    ? `<div class="section-card"><div class="abstract-text" style="white-space:pre-wrap;">${esc(t)}</div></div>`
    : '<div class="empty-block">该文献暂无中文译文</div>';
}

/* ---------------- tabs / overlays ---------------- */
function setTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.dataset.panel === name));
  if (name === "corrections") loadCorrections();
}
function openLightbox(f) {
  $("#lightboxImg").src = f.asset_url || "";
  $("#lightboxCaption").textContent = (f.figure_label || "") + " · " + (f.caption || "");
  $("#lightbox").hidden = false;
}

/* ---------------- URL 同步 ---------------- */
function syncUrl() {
  const p = new URLSearchParams();
  if (state.shareToken) {
    p.set("token", state.shareToken);
  } else {
    if (state.library) p.set("library", state.library);
    if (state.q.trim()) p.set("q", state.q.trim());
  }
  if (state.selectedId) p.set("paper", state.selectedId);
  history.replaceState(null, "", "?" + p.toString());
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $("#libSelect").addEventListener("change", e => { state.library = e.target.value; loadPapers(true); });
  const onSearch = debounce(() => { state.q = $("#searchInput").value; loadPapers(true); }, 350);
  $("#searchInput").addEventListener("input", onSearch);
  $("#typeFilter").addEventListener("change", e => { state.paperType = e.target.value; loadPapers(true); });
  $("#pdfFilter").addEventListener("change", e => { state.hasPdf = e.target.value; loadPapers(true); });
  $("#sortSelect").addEventListener("change", e => {
    [state.sortBy, state.sortOrder] = e.target.value.split(":"); loadPapers(true);
  });
  $("#prevPage").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadPapers(false); });
  $("#nextPage").addEventListener("click", () => { state.offset += PAGE_SIZE; loadPapers(false); });
  $$(".tab").forEach(t => t.addEventListener("click", () => setTab(t.dataset.tab)));
  $("#lightboxClose").addEventListener("click", () => { $("#lightbox").hidden = true; });
  $("#lightbox").addEventListener("click", e => { if (e.target.id === "lightbox") $("#lightbox").hidden = true; });
  $("#pdfClose").addEventListener("click", () => { $("#pdfOverlay").hidden = true; $("#pdfFrame").src = ""; });
  $("#dPdfInlineBtn").addEventListener("click", () => {
    if (!state.detail) return;
    $("#pdfFrame").src = `/api/papers/${encodeURIComponent(state.detail.id)}/pdf`;
    $("#pdfOverlay").hidden = false;
  });
}

/* ---------------- boot ---------------- */
(async function boot() {
  bind();
  const params = new URLSearchParams(location.search);
  state.shareToken = params.get("token") || "";
  if (state.shareToken) {
    // 分享模式：隐藏检索/筛选控件，显示只读分享标识
    document.getElementById("topControls").hidden = true;
    document.getElementById("shareBanner").hidden = false;
    document.getElementById("shareScope").textContent = "授权范围内的文献";
    await loadPapers(true);
  } else {
    $("#searchInput").value = params.get("q") || "";
    state.q = $("#searchInput").value;
    await loadLibraries();
  }
  const paper = params.get("paper");
  if (paper) selectPaper(paper);
})();
