/* Shared current-data table. Never aggregate or choose a preferred condition. */
(function (global) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const metricLabels={XPS_binding_energy:'XPS 结合能',xps_binding_energy:'XPS 结合能',binding_energy:'结合能',adsorption_energy:'吸附能',reaction_free_energy:'反应自由能',activation_energy:'活化能',energy_barrier:'能垒',specific_capacity:'比容量',capacity:'比容量',c_rate:'倍率',cycle_life:'循环寿命',retention:'容量保持率',overpotential:'过电位',tafel_slope:'Tafel 斜率'};
  const labels = {adsorbate:'吸附物',configuration:'构型',reaction_step:'反应步骤',rate:'倍率',c_rate:'倍率（C）',cycles:'循环次数',cycle_number:'循环次数',temperature:'温度',potential:'电位',pH:'pH',method:'计算方法',functional:'泛函',solvent:'溶剂',support:'载体',site:'位点'};
  function readable(value) {
    if (value == null || value === '') return '未记录';
    if (Array.isArray(value)) return value.map(readable).join('、');
    if (typeof value === 'object') return Object.entries(value).filter(([key]) => !key.startsWith('_')).map(([key,v]) => `${labels[key] || key}：${readable(v)}`).join('；') || '未记录条件';
    return String(value);
  }
  function fields(rows, templates) {
    const known = new Map((templates?.common_fields || []).map(f => [f.name,f]));
    rows.forEach(row => (templates?.reactions?.[row.reaction]?.fields || []).forEach(f => known.set(f.name,f)));
    const saved = new Set(rows.flatMap(row => (row.values || []).map(v => v.field_name)));
    const classify=name=>/adsorp/.test(name)?'吸附能与构型':/free_energy|reaction_energy/.test(name)?'反应自由能':/barrier|activation/.test(name)?'能垒':/capacity|retention|cycle|rate|loading/.test(name)?'性能与条件':/xps|xrd|raman|bet|binding_energy|structure|spectr/.test(name)?'结构与表征':/potential|tafel|resistance|conductivity|voltage/.test(name)?'电化学指标':'其他指标';
    return [...known.values()].filter(f => saved.has(f.name)).map(f=>({...f,group:f.group || classify(f.name)})).concat([...saved].filter(name => !known.has(name)).map(name => ({name,label:metricLabels[name] || name,group:classify(name)}))); 
  }
  function groups(rows) {
    const map = new Map();
    rows.forEach(row => {
      const uncertain = row.identity_status === 'pending' || !row.material || /^(unknown|未知|身份待定)$/i.test(row.material.trim());
      const key = uncertain ? row.id : JSON.stringify([row.paper_id,row.data_type,row.material,row.support ?? null,row.active_site ?? null,row.active_site_type ?? null]);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(row);
    });
    return [...map.values()];
  }
  const valueText = v => v.value_type === 'missing' ? `缺失：${v.missing_reason || '未报告'}` : `${v.raw_value ?? v.numeric_value ?? '未报告'}${v.unit ? ' ' + v.unit : ''}`;
  function valueButton(row,v) { return `<button type="button" class="edg-value" data-edg-row="${esc(row.id)}" data-edg-value="${esc(v.id)}">${esc(valueText(v))}${v.is_estimated ? '（估读）' : ''}${['value_conflict','unit_conflict'].includes(v.conflict?.status) ? ' ⚠ 冲突' : ''}</button>`; }
  function mount(container, options) {
    const rows = options.rows || [], columns = fields(rows,options.templates), grouped = groups(rows);
    const hidden = new Set();
    function render() {
      const visible = columns.filter(f => !hidden.has(f.name));
      container.innerHTML = `<div class="edg-tools"><span>${grouped.length} 个催化剂 · ${rows.length} 条独立条件</span><details><summary>指标列选择</summary>${columns.map(f => `<label><input type="checkbox" data-edg-column="${esc(f.name)}" ${hidden.has(f.name) ? '' : 'checked'}>${esc(f.group ? f.group + ' · ' : '')}${esc(f.label)}</label>`).join('')}</details></div><div class="edg-scroll"><table class="edg-table"><thead><tr><th>催化剂</th><th>载体／位点</th>${visible.map(f => `<th>${esc(f.label)}${f.unit ? '（' + esc(f.unit) + '）' : ''}${f.group ? '<small>' + esc(f.group) + '</small>' : ''}</th>`).join('')}</tr></thead><tbody>${grouped.map((group,index) => {
        const row = group[0];
        return `<tr data-edg-group="${index}"><th>${esc(row.material || '身份待定')}${row.identity_status === 'pending' ? '<small>身份待定，独立保留</small>' : ''}<button class="edg-expand" data-edg-expand="${index}" aria-expanded="false">展开条件（${group.length}）</button></th><td>${esc([row.support,row.active_site,row.active_site_type].filter(Boolean).join('／') || '未记录')}</td>${visible.map(f => { const values = group.flatMap(r => (r.values || []).filter(v => v.field_name === f.name).map(v => [r,v])); return `<td>${values.length === 1 ? valueButton(...values[0]) : values.length ? `<button class="edg-value" data-edg-expand="${index}">多组（${values.length}）</button>` : '—'}</td>`; }).join('')}</tr><tr class="edg-conditions" data-edg-conditions="${index}" hidden><td colspan="${visible.length + 2}">${group.map(r => `<section class="edg-condition" data-current-row="${esc(r.id)}"><div><strong>条件：${esc(readable(r.condition))}</strong> <button class="edg-value" data-edg-identity="${esc(r.id)}">编辑材料与条件</button></div>${r.configuration ? `<div>构型：${esc(r.configuration)}</div>` : ''}${Object.keys(r.properties || {}).some(k => !k.startsWith('_')) ? `<p>${esc(readable(r.properties))}</p>` : ''}<div class="edg-condition-values">${(r.values || []).map(v => `<div><span>${esc(columns.find(f => f.name === v.field_name)?.label || v.field_name)}</span>${valueButton(r,v)}</div>`).join('')}</div></section>`).join('')}</td></tr>`;
      }).join('') || `<tr><td colspan="${visible.length + 2}">没有已保存的${options.type === 'dft' ? '计算' : '实验'}数据。</td></tr>`}</tbody></table></div>`;
      container.querySelectorAll('[data-edg-expand]').forEach(button => button.onclick = () => {const child = container.querySelector(`[data-edg-conditions="${button.dataset.edgExpand}"]`); child.hidden = !child.hidden; container.querySelectorAll(`[data-edg-expand="${button.dataset.edgExpand}"]`).forEach(b => b.setAttribute('aria-expanded',String(!child.hidden)));});
      container.querySelectorAll('[data-edg-column]').forEach(input => input.onchange = () => {input.checked ? hidden.delete(input.dataset.edgColumn) : hidden.add(input.dataset.edgColumn); render();});
      container.querySelectorAll('[data-edg-value]').forEach(button => button.onclick = () => {const row = rows.find(r => r.id === button.dataset.edgRow); const value = row.values.find(v => v.id === button.dataset.edgValue); options.onValue ? options.onValue(row,value) : editValue(row,value,options);});
      container.querySelectorAll('[data-edg-identity]').forEach(button => button.onclick = () => {const row = rows.find(r => r.id === button.dataset.edgIdentity); options.onIdentity ? options.onIdentity(row) : editIdentity(row,options);});
    }
    render();
  }
  async function api(path, options) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(),20000);
    try { const headers=new Headers(options?.headers || {}); if(options?.method && !['GET','HEAD'].includes(options.method.toUpperCase()) && global.LitAIAuth?.csrfToken()) headers.set('X-CSRF-Token',global.LitAIAuth.csrfToken()); const response = await fetch(path,{credentials:'same-origin',signal:controller.signal,...options,headers}); const body = await response.json(); if (!response.ok) {const error = Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body)); error.status=response.status; throw error;} return body; } finally {clearTimeout(timer);}
  }
  function dialog(title) {
    const panel = document.createElement('dialog'); panel.className='edg-dialog'; panel.innerHTML=`<header><strong>${esc(title)}</strong><button type="button" data-close>关闭</button></header><div data-body></div>`; document.body.append(panel); panel.querySelector('[data-close]').onclick=() => panel.close(); panel.onclose=() => panel.remove(); panel.showModal(); return panel;
  }
  function sourceHtml(source, files) {
    const file = files.find(f => f.id === source.file_id), page = Number(source.page_number);
    const link = source.file_id ? `/api/rebuild/files/${encodeURIComponent(source.file_id)}/preview${Number.isInteger(page) && page > 0 ? '#page=' + page : ''}` : '';
    return `<article class="edg-source"><strong>${esc(file?.role === 'si' ? 'SI' : file?.role === 'main' ? '正文' : '来源未登记')} · ${esc(file?.original_filename || '')}</strong><div>${esc(source.label || source.source_kind)} · ${page > 0 ? 'PDF 第 ' + page + ' 页' : '页码未记录'}</div><blockquote>${esc(source.quote || source.estimate_basis || '未保存引文')}</blockquote>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">打开证据 PDF</a>` : ''}${source.asset_id ? ` <a href="/api/rebuild/assets/${encodeURIComponent(source.asset_id)}" target="_blank" rel="noopener">打开证据图表</a>` : ''}</article>`;
  }
  async function editValue(row,value,options) {
    const panel = dialog(`${row.material} · ${value.field_name}`), body=panel.querySelector('[data-body]');
    body.textContent='正在读取来源…';
    let files;
    try {files=(await api(`/api/rebuild/papers/${row.paper_id}/files`)).items;} catch(error) {body.textContent=`来源读取失败：${error.message}`; return;}
    const source = value.sources?.find(s => s.id === value.preferred_source_id) || value.sources?.[0] || {};
    const input = (name,label,v,type='text') => `<label>${label}<input name="${name}" type="${type}" ${type==='number' ? 'step="any"' : ''} value="${esc(v ?? '')}"></label>`;
    body.innerHTML = `<div>${(value.sources || []).map(s => sourceHtml(s,files)).join('') || '未保存来源'}</div><details><summary>修改历史与冲突</summary><pre>${esc(JSON.stringify(value.conflict || {},null,2))}</pre></details><form>${input('raw_value','原值（保留精度）',value.raw_value)}${input('numeric_value','数值（空白表示缺失）',value.numeric_value,'number')}${input('unit','单位',value.unit)}<label>值类型<select name="value_type">${['explicit','estimated','derived','missing'].map(t=>`<option ${t===value.value_type?'selected':''}>${t}</option>`).join('')}</select></label>${input('missing_reason','缺失原因',value.missing_reason)}${input('notes','备注',value.notes)}<label>来源文件<select name="file_id"><option value="">来源文件未标注</option>${files.map(f=>`<option value="${esc(f.id)}" ${f.id===source.file_id?'selected':''}>${f.role==='si'?'SI':'正文'} · ${esc(f.original_filename)}</option>`).join('')}</select></label>${input('page_number','PDF 页码',source.page_number,'number')}${input('label','图表号／表号',source.label)}<label>原文引文<textarea name="quote">${esc(source.quote || '')}</textarea></label><label>修改理由<textarea name="correction_reason" required></textarea></label><button type="submit">保存修改</button><p data-status role="status"></p></form>`;
    const form=body.querySelector('form'), status=form.querySelector('[data-status]'), key=`litai.value-correction.${value.id}`;
    const pending=sessionStorage.getItem(key);
    if(pending) {status.textContent='上次保存结果尚未确认。重试将先回读，并使用原请求。'; Array.from(form.elements).forEach(e=>{if(e.type!=='submit')e.disabled=true;});}
    form.onsubmit=async event=> {
      event.preventDefault(); const button=form.querySelector('[type=submit]'); button.disabled=true;
      let payload;
      try {
        if (sessionStorage.getItem(key)) payload=JSON.parse(sessionStorage.getItem(key));
        else {const data=Object.fromEntries(new FormData(form)); if(!data.correction_reason.trim())throw Error('请填写修改理由'); payload={expected_revision:value.value_revision,request_id:crypto.randomUUID(),raw_value:data.value_type==='missing'?null:data.raw_value || null,numeric_value:data.value_type==='missing'||data.numeric_value===''?null:Number(data.numeric_value),unit:data.unit || null,value_type:data.value_type,precision_digits:value.precision_digits,missing_reason:data.value_type==='missing'?data.missing_reason || '未报告':null,notes:data.notes || null,correction_reason:data.correction_reason,source:{...Object.fromEntries(['file_id','asset_id','source_key','source_kind','page_number','label','table_row','table_column','quote','estimate_basis'].filter(k=>source[k]!==undefined).map(k=>[k,source[k]])),file_id:data.file_id || null,source_kind:source.source_kind || 'text',page_number:data.page_number?Number(data.page_number):null,label:data.label || null,quote:data.quote || null}}; if(!payload.expected_revision)throw Error('数据版本缺失，请刷新后重试'); sessionStorage.setItem(key,JSON.stringify(payload));}
        status.textContent='正在核实并保存…';
        const current=await api(`/api/rebuild/values/${value.id}`);
        const audits=current.conflict?.correction_requests || [];
        if(!audits.some(a=>a.request_id===payload.request_id)) await api(`/api/rebuild/values/${value.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        sessionStorage.removeItem(key); await options.reload?.(); status.textContent='已保存并重新读取服务器数据；原值和来源历史保留。'; panel.close();
      } catch(error) {if(error.status && error.status < 500)sessionStorage.removeItem(key); status.textContent=error.status===409?'数据已被其他操作修改。请关闭窗口、刷新并重新核对。':`保存结果尚未确认：${error.message}。重试会先回读服务器并复用原请求。`; } finally {button.disabled=false;}
    };
  }
  function transferHistory(paperId, kind, record, options) {
    const panel=dialog('带来源转入当前数据表'), body=panel.querySelector('[data-body]');
    const legacyFields=kind==='dft'?['value']:['capacity_value','sulfur_loading_mg_cm2','sulfur_content_wt_percent','cycle_number','decay_per_cycle'];
    const templates=options.templates || {};
    body.innerHTML=`<p>服务器将读取历史原值，原历史记录保留。缺失、范围值或无法确定来源的记录须先核对。</p><form><label>历史字段<select name="legacy_field">${legacyFields.map(f=>`<option value="${f}">${esc(f)} · ${esc(record[f] ?? '缺失')}</option>`).join('')}</select></label><label>目标指标字段<input name="field_name" required pattern="[A-Za-z][A-Za-z0-9_]*"></label><label>单位<input name="unit" value="${esc(record.unit || '')}"></label><label>反应<select name="reaction">${Object.entries(templates.reactions || {}).map(([name,t])=>`<option value="${esc(name)}">${esc(t.label || name)}</option>`).join('')}</select></label><label>材料<input name="material" required></label><label>载体<input name="support"></label><label>位点<input name="active_site"></label><label>活性中心类型<select name="active_site_type">${(templates.active_site_types || []).map(t=>`<option value="${esc(t.value)}">${esc(t.label)}</option>`).join('')}</select></label><label>条件（JSON 对象）<textarea name="condition">{}</textarea></label><label>来源 PDF<select name="file_id" required>${(options.files || []).map(f=>`<option value="${esc(f.id)}">${f.role==='si'?'SI':'正文'} · ${esc(f.original_filename)}</option>`).join('')}</select></label><label>PDF 页码<input name="page_number" type="number" min="1" required></label><label>图表号<input name="label"></label><label>原文引文<textarea name="quote"></textarea></label><label>转入理由<textarea name="reason" required></textarea></label><button type="submit">确认带来源转入</button><p data-status role="status"></p></form>`;
    const form=body.querySelector('form'), status=form.querySelector('[data-status]');let intent=null;
    form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{if(!intent){const d=Object.fromEntries(new FormData(form));const condition=JSON.parse(d.condition);if(!condition || typeof condition!=='object' || Array.isArray(condition))throw Error('条件须为 JSON 对象');intent={legacy_kind:kind,legacy_id:record.id,legacy_field:d.legacy_field,field_name:d.field_name,unit:d.unit || null,reaction:d.reaction,material:d.material,support:d.support || null,active_site:d.active_site || null,active_site_type:d.active_site_type,condition,source:{file_id:d.file_id,source_kind:'text',page_number:Number(d.page_number),label:d.label || null,quote:d.quote || null},reason:d.reason,request_id:crypto.randomUUID()};}await api(`/api/rebuild/papers/${paperId}/history-transfer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(intent)});await options.reload?.();panel.close();}catch(error){status.textContent='转入未确认：'+error.message+'；重试将复用原请求。';}finally{button.disabled=false;}};
  }
  async function editIdentity(row,options) {
    const panel=dialog('编辑材料与条件'), body=panel.querySelector('[data-body]');body.textContent='正在读取依据文件…';
    let files;try{files=(await api(`/api/rebuild/papers/${row.paper_id}/files`)).items;}catch(error){body.textContent='文件读取失败：'+error.message;return;}
    const field=(name,label,value)=>`<label>${label}<input name="${name}" value="${esc(value)}"></label>`;
    body.innerHTML=`<p>条件：${esc(readable(row.condition))}。各条件保留独立记录，不合并数值。</p><form>${field('material','材料',row.material)}${field('support','载体',row.support)}${field('active_site','位点',row.active_site)}${field('configuration','构型',row.configuration)}<label>活性中心类型<select name="active_site_type">${(options.templates?.active_site_types || []).map(t=>`<option value="${esc(t.value)}" ${t.value===row.active_site_type?'selected':''}>${esc(t.label)}</option>`).join('')}</select></label><label>完整条件（JSON 对象）<textarea name="condition">${esc(JSON.stringify(row.condition || {},null,2))}</textarea></label><label>依据 PDF<select name="file_id" required>${files.map(f=>`<option value="${esc(f.id)}">${f.role==='si'?'SI':'正文'} · ${esc(f.original_filename)}</option>`).join('')}</select></label><label>PDF 页码<input name="page_number" type="number" min="1" required></label>${field('label','图表号','')}<label>原文依据<textarea name="quote"></textarea></label><label>修改理由<textarea name="correction_reason" required></textarea></label><button type="submit">保存身份与条件</button><p data-status role="status"></p></form>`;
    const form=body.querySelector('form'), status=form.querySelector('[data-status]'),key=`litai.identity-correction.${row.paper_id}.${row.id}`;
    if(row.identity_status==='pending'){status.textContent='身份待定：请通过带来源的数据导入补交缺失身份。';form.querySelector('[type=submit]').disabled=true;return;}
    form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{let intent;const saved=sessionStorage.getItem(key);if(saved)intent=JSON.parse(saved);else{const d=Object.fromEntries(new FormData(form)),condition=JSON.parse(d.condition);if(!condition || typeof condition!=='object' || Array.isArray(condition))throw Error('条件须为 JSON 对象');intent={paper_id:row.paper_id,row_key:row.row_key,expected_revision:row.identity_revision,request_id:crypto.randomUUID(),changes:{material:d.material,support:d.support || null,active_site:d.active_site || null,configuration:d.configuration || null,active_site_type:d.active_site_type,condition},correction_reason:d.correction_reason,evidence:[{file_id:d.file_id,source_kind:'text',page_number:Number(d.page_number),label:d.label || null,quote:d.quote || null}]};sessionStorage.setItem(key,JSON.stringify(intent));}await api(`/api/rebuild/rows/${row.id}/identity`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(intent)});sessionStorage.removeItem(key);await options.reload?.();panel.close();}catch(error){if(error.status && error.status<500)sessionStorage.removeItem(key);status.textContent=error.status===409?'此行已被修改或与已有身份条件冲突，请刷新后核对。':'保存未确认：'+error.message+'。重试将复用原请求。';}finally{button.disabled=false;}};
  }
  global.EditableDataGrid={mount,groups,fields,readable,editValue,sourceHtml,transferHistory};
})(window);
