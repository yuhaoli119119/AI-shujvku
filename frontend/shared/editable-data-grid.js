/* Shared current-data table. Never aggregate or choose a preferred condition. */
(function (global) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const metricLabels={XPS_binding_energy:'XPS 结合能',xps_binding_energy:'XPS 结合能',binding_energy:'结合能',adsorption_energy:'吸附能',reaction_free_energy:'反应自由能',activation_energy:'活化能',energy_barrier:'能垒',specific_capacity:'比容量',capacity:'比容量',c_rate:'倍率',cycle_life:'循环寿命',retention:'容量保持率',overpotential:'过电位',tafel_slope:'Tafel 斜率',rct_ohm:'电荷转移电阻',rg_ohm:'界面电阻 Rg',rs_ohm:'串联电阻 Rs',exafs_delta_e0_ev:'EXAFS 能量偏移 ΔE₀',exafs_sigma2_a2:'EXAFS Debye–Waller 因子 σ²',exafs_sigma2_1e3_angstrom2:'EXAFS Debye–Waller 因子 σ²',xanes_intermediate_ev:'XANES 中间峰能量',xanes_pi_star_ev:'XANES π* 峰能量',xanes_sigma_star_ev:'XANES σ* 峰能量',sei_layer_thickness_um:'SEI 层厚度'};
  // Translate established scientific phrases; unfamiliar fragments remain visible.
  // Unit suffixes are naming conventions only: displayed units come from stored values.
  const scientificPhrases = {
    decomposition_barrier:'分解能垒',decomposition_energy_barrier:'分解能垒',reaction_free_energy:'反应自由能',reaction_step_free_energy:'反应步骤自由能',
    rate_determining_step:'速率决定步骤',rate_limiting_step:'限速步骤',free_energy_difference:'自由能差',free_energy_change:'自由能变化',gibbs_free_energy:'吉布斯自由能',free_energy_barrier:'自由能垒',free_energy:'自由能',
    adsorption_energy:'吸附能',adsorption_free_energy:'吸附自由能',binding_energy:'结合能',activation_energy:'活化能',formation_energy:'形成能',decomposition_energy:'分解能',dissociation_energy:'解离能',solvation_energy:'溶剂化能',electronic_energy:'电子能',total_energy:'总能量',migration_barrier:'迁移能垒',diffusion_barrier:'扩散能垒',dissociation_barrier:'解离能垒',delithiation_barrier:'脱锂能垒',desorption_barrier:'脱附能垒',delithiation_energy:'脱锂能',cohesive_energy:'内聚能',cleavage_energy:'解理能',
    initial_discharge_capacity:'初始放电比容量',specific_discharge_capacity:'放电比容量',gravimetric_capacity:'质量比容量',specific_capacity:'比容量',areal_capacity:'面积比容量',discharge_capacity:'放电容量',reversible_capacity:'可逆容量',nucleation_capacity:'成核容量',precipitation_capacity:'沉积容量',dissolution_capacity:'溶解容量',stable_capacity:'稳定容量',rate_capacity:'倍率容量',capacity_retention:'容量保持',capacity_recovery_ratio:'容量恢复比',capacity_decay_rate:'容量衰减率',capacity_decay:'容量衰减',capacity_fade:'容量衰减',
    charge_transfer_resistance:'电荷转移电阻',charge_transfer:'电荷转移',charge_density_difference:'电荷密度差',charge_conversion:'充电转化',discharge_conversion:'放电转化',current_density:'电流密度',coulombic_efficiency:'库仑效率',energy_efficiency:'能量效率',electrical_conductivity:'电导率',d_band_center:'d 带中心',band_gap:'带隙',magnetic_moment:'磁矩',bond_length:'键长',bond_distance:'键距',bond_order:'键级',coordination_number:'配位数',oxidation_state:'氧化态',net_charge:'净电荷',interatomic_distance:'原子间距',interlayer_distance:'层间距',interlayer_spacing:'层间距',surface_area:'比表面积',pore_volume:'孔体积',pore_size:'孔径',pore_diameter:'孔径',peak_potential:'峰电位',peak_position:'峰位',onset_potential:'起始电位',onset_voltage:'起始电压',peak_current:'峰电流',voltage_hysteresis:'电压滞后',potential_hysteresis:'电位滞后',voltage_range:'电压范围',cut_off_voltage:'截止电压',sulfur_loading:'硫负载量',metal_loading:'金属负载量',sulfur_utilization:'硫利用率',mass_fraction:'质量分数',component_fraction:'组分占比',atom_percent:'原子占比',at_percent:'原子占比',wt_percent:'质量占比',at_pct:'原子占比',atom_pct:'原子占比',nitrogen_content:'氮含量',metal_content:'金属含量',electrolyte_to_sulfur_ratio:'电解液与硫比',electrolyte_sulfur_ratio:'电解液与硫比',li_diffusion_coefficient:'锂扩散系数',li_ion_diffusion:'锂离子扩散',transference_number:'迁移数',
    pyridinic_n:'吡啶氮',pyrrolic_n:'吡咯氮',graphitic_n:'石墨氮',oxidized_n:'氧化氮',spin_polarized:'自旋极化',spin_down:'自旋向下',spin_up:'自旋向上',after_adsorption:'吸附后',per_cycle:'每循环',after_cycles:'循环后',after_20_cycles:'20 次循环后',after_1000cyc:'1000 次循环后',after200cyc:'200 次循环后',after_500_cycles:'500 次循环后',after_200_cycles:'200 次循环后',
    energy_threshold:'能量收敛阈值',force_tolerance:'力收敛阈值',frequency_threshold:'频率阈值',dispersion_correction:'色散校正',kmesh_separation:'k 点网格间距',contact_angle:'接触角',exchange_parameter:'交换参数',entropy_correction:'熵修正',inner_potential_correction:'内电位修正',debye_waller:'德拜–沃勒',r_factor:'R 因子',linear_fit:'线性拟合',feat_importance:'特征重要性',nucleation_peak_time:'成核峰时间',nucleation_time:'成核时间',formation_onset:'生成起始',round_trip:'往返',
  };
  const scientificTokens = {capacity:'容量',barrier:'能垒',energy:'能量',charge:'电荷',decomposition:'分解',adsorption:'吸附',desorption:'脱附',dissociation:'解离',delithiation:'脱锂',formation:'形成',diffusion:'扩散',solvation:'溶剂化',potential:'电位',voltage:'电压',overpotential:'过电位',polarization:'极化',hysteresis:'滞后',resistance:'电阻',conductivity:'电导率',cycle:'循环',cycles:'循环',cyc:'循环',cycle_count:'循环次数',cycle_number:'循环次数',cycle_life:'循环寿命',count:'数量',number:'数量',retention:'保持率',recovery:'恢复',ratio:'比值',fraction:'占比',percent:'百分比',percentage:'百分比',change:'变化',difference:'差值',shift:'偏移',lower:'下限',upper:'上限',min:'最小值',max:'最大值',avg:'平均',average:'平均',initial:'初始',recovered:'恢复后',stable:'稳定',stability:'稳定性',relative:'相对',reported:'原文报告',predicted:'预测',pred:'预测',rate:'倍率',long_cycling:'长循环',peak:'峰',position:'位置',time:'时间',hours:'时长',temperature:'温度',descriptor:'描述符',occupancy:'占据',unoccupied:'未占据',orbital:'轨道',states:'态',total:'总',atoms:'原子',metal:'金属',sulfur:'硫',nitrogen:'氮',elemental:'元素',sulfur_atom:'硫原子',single:'单个',layer:'层',thickness:'厚度',external:'外部',mesopore:'介孔',micropore:'微孔',supercell:'超胞',graphene:'石墨烯',monolayer:'单层',functional:'泛函',code:'计算软件',cutoff:'截断',training:'训练集',train:'训练集',test:'测试集',validation:'验证集',accuracy:'准确率',recall:'召回率',specificity:'特异度',slope:'斜率',intercept:'截距',relation:'关系',linewidth:'线宽',isomer_shift:'同质异能位移',quadrupole_splitting:'四极分裂',stem:'STEM',xps:'XPS',xanes:'XANES',exafs:'EXAFS',bet:'BET',bader:'Bader',ddec6:'DDEC6',lowdin:'Löwdin',voronoi:'Voronoi',icohp:'ICOHP',pdos:'PDOS',dos:'DOS',cv:'CV',zpe:'零点能',raman:'拉曼',tafel:'Tafel',li:'Li',s:'S',co:'Co',fe:'Fe',ni:'Ni',n:'N',c:'C',o:'O',cl:'Cl'};
  const unitSuffix= /_(?:ev_per_atom|ev_per_cell|ev_per_ang|mah_gs|mah_g|mah_cm2|m2_g|cm3_g|mg_cm2|ul_mg|mv_dec|ma_cm2|ma_g|mmol_g|cm2_s|s_m|mg_l|g_cm3|mm_s|angstrom2|angstrom|ang2|bohr_magneton|ev|mev_a2|mv|v|nm|um|pm|a2|ang|deg|cm1|ohm|ub|e|k|s)$/i;
  function metricLabel(name) {
    if(metricLabels[name])return metricLabels[name];
    let text=String(name).replace(unitSuffix,'');
    if(/^deltaG_/i.test(text))text=text.replace(/^deltaG_/i,'自由能变化_').replace(/_to_/g,' → ');
    if(/^delta_g_rds$/i.test(text))return '速率决定步骤自由能变化';
    for(const [phrase,label] of Object.entries(scientificPhrases).sort((a,b)=>b[0].length-a[0].length))text=text.replace(new RegExp('(^|_)'+phrase+'(?=_|$)','gi'),(_,prefix)=>prefix+label);
    const tokens=text.split('_').filter(Boolean).map(token=>scientificTokens[token.toLowerCase()] || token);
    text=tokens.join(' · ');
    return /[\u3400-\u9fff]/.test(text)?text:`其他指标 · ${name}`;
  }
  const labels = {software:'计算软件',basis_set:'基组',pseudopotential:'赝势',bsse_correction:'BSSE 校正',charge_analysis:'电荷分析',force_convergence_ev_per_A:'力收敛阈值（eV/Å）',adsorbate:'吸附物',configuration:'构型',reaction_step:'反应步骤',rate:'倍率',c_rate:'倍率（C）',cycles:'循环次数',cycle_number:'循环次数',temperature:'温度',potential:'电位',pH:'pH',method:'计算方法',functional:'泛函',solvent:'溶剂',support:'载体',site:'位点',code:'计算软件',dft_code:'DFT 计算软件',kpoints:'k 点网格',k_points:'k 点网格',vacuum_A:'真空层厚度（Å）',vacuum_angstrom:'真空层厚度（Å）',cutoff_eV:'截断能（eV）',energy_cutoff_ev:'截断能（eV）',force_conv_eV_per_A:'力收敛阈值（eV/Å）',energy_conv_eV:'能量收敛阈值（eV）',spin_polarized:'自旋极化',dispersion_correction:'色散校正',dispersion:'色散校正',reaction_cycle:'记录用途',U_eV:'Hubbard U（eV）',hubbard_u_ev:'Hubbard U（eV）',current_rate:'测试倍率',scan_rate_mV_s:'扫描速率（mV/s）',sulfur_loading_mg_cm2:'硫负载量（mg/cm²）',electrolyte:'电解液',voltage_range:'电压范围',step:'步骤',figure:'图号',table:'表号'};
  function readable(value) {
    if (value == null || value === '') return '未记录';
    if (Array.isArray(value)) return value.map(readable).join('、');
    if (typeof value === 'object') return Object.entries(value).filter(([key]) => !key.startsWith('_')).map(([key,v]) => `${labels[key] || metricLabel(key)}：${readable(v)}`).join('；') || '未记录条件';
    return String(value);
  }
  function fields(rows, templates) {
    const known = new Map((templates?.common_fields || []).map(f => [f.name,f]));
    rows.forEach(row => (templates?.reactions?.[row.reaction]?.fields || []).forEach(f => known.set(f.name,f)));
    const saved = new Set(rows.flatMap(row => (row.values || []).map(v => v.field_name)));
    const classify=name=>/^(?:xps|xanes|exafs|xrd|raman)(?:_|$)/i.test(name)?'结构与表征':/adsorp/.test(name)?'吸附能与构型':/free_energy|reaction_energy|^deltaG_|^delta_g_/i.test(name)?'反应自由能':/barrier|activation/.test(name)?'能垒':/capacity|retention|cycle|rate|loading/.test(name)?'性能与条件':/xps|xrd|exafs|xanes|raman|bet|binding_energy|structure|spectr|surface_area|pore|coordination/.test(name)?'结构与表征':/potential|tafel|resistance|conductivity|voltage|rct|rs_ohm|rg_ohm|efficiency/.test(name)?'电化学指标':'其他指标';
    const priority=group=>({'吸附能与构型':0,'反应自由能':1,'能垒':2,'性能与条件':0,'电化学指标':3,'结构与表征':4,'其他指标':5})[group] ?? 5;
    return [...known.values()].filter(f => saved.has(f.name)).map(f=>({...f,group:f.group || classify(f.name)})).concat([...saved].filter(name => !known.has(name)).map(name => ({name,label:metricLabel(name),group:classify(name)}))).sort((a,b)=>priority(a.group)-priority(b.group));
  }
  function groups(rows, familyId) {
    const map = new Map();
    rows.forEach(row => {
      const uncertain = row.identity_status === 'pending' || !row.material || /^(unknown|未知|身份待定)$/i.test(row.material.trim());
      const key = uncertain ? row.id : JSON.stringify([familyId || row.paper_id,row.data_type,row.material,row.support ?? null,row.active_site ?? null,row.active_site_type ?? null]);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(row);
    });
    return [...map.values()];
  }
  const valueText = v => v.value_type === 'missing' ? `缺失：${v.missing_reason || '未报告'}` : `${v.raw_value ?? v.numeric_value ?? '未报告'}${v.unit ? ' ' + v.unit : ''}`;
  function valueButton(row,v) { return `<button type="button" class="edg-value" data-edg-row="${esc(row.id)}" data-edg-value="${esc(v.id)}">${esc(valueText(v))}${v.is_estimated ? '（估读）' : ''}${['value_conflict','unit_conflict'].includes(v.conflict?.status) ? ' ⚠ 冲突' : ''}</button>`; }
  function conditionContent(row) {
    const entries=Object.entries(row.condition || {}).filter(([key])=>!key.startsWith('_'));
    const chips=entries.map(([key,value])=>`<div class="edg-condition-item"><span>${esc(labels[key] || metricLabel(key))}：</span><b>${esc(readable(value))}</b></div>`);
    return `<div class="edg-condition-meta">${chips.slice(0,4).join('') || '<span class="edg-muted">未记录条件</span>'}</div>${chips.length>4?`<details class="edg-more"><summary>完整条件 · 另 ${chips.length-4} 项</summary><div class="edg-condition-meta">${chips.slice(4).join('')}</div></details>`:''}`;
  }
  function mount(container, options) {
    const rows=options.rows || [], columns=fields(rows,options.templates), grouped=groups(rows,options.familyId);
    const view=options.viewState || {}, hidden=view.hidden || new Set(), expanded=view.expanded || new Set(), metricFocus=view.metricFocus || new Map(), fileCache=new Map(); let selectedGroup=view.selectedGroup || '全部', query=view.query || '', chooserOpen=view.chooserOpen || false;
    Object.assign(view,{hidden,expanded,metricFocus});
    const categories=[...new Set(columns.map(f=>f.group))];
    // Wide papers open with a readable metric group; all saved fields remain selectable.
    if(!view.selectedGroup && columns.length>8)selectedGroup=categories[0];
    function metricDetails(group,index,name) {
      const field=columns.find(f=>f.name===name), pairs=group.flatMap(r=>(r.values || []).filter(v=>v.field_name===name).map(v=>[r,v]));
      const item=(label,value)=>`<div class="edg-condition-item"><span>${esc(label)}：</span><b>${esc(value == null || value === '' ? '未记录' : readable(value))}</b></div>`;
      return `<div class="edg-detail-heading">${esc(group[0].material)} · ${esc(field?.label || metricLabel(name))}明细 <span>${pairs.length} 条记录，点击数值查看完整来源或编辑</span></div><p class="edg-hint">每条数值对应各自的对象与条件；同一对象的不同来源／条件分别保留，缺失信息显示“未记录”。</p>${pairs.map(([r,v],i)=>{
        const c=r.condition || {}, files=options.files || fileCache.get(r.paper_id) || [];
        const sources=(v.sources || []).map(source=>{
          const file=files.find(f=>f.id===source.file_id), role=displayRole(file,r.paper_id,options.familyId);
          const roleText=role==='si'?'SI':role==='main'?'正文':'来源文件';
          const page=Number(source.page_number), locator=[roleText,file?.original_filename,Number.isInteger(page)&&page>0?`PDF 第 ${page} 页`:'页码未记录',source.label].filter(Boolean).join(' · ');
          const href=source.file_id?`/api/rebuild/files/${encodeURIComponent(source.file_id)}/preview${Number.isInteger(page)&&page>0?'#page='+page:''}`:'';
          return `<div>${href?`<a href="${esc(href)}" target="_blank" rel="noopener">${esc(locator)}</a>`:esc(locator)}</div>`;
        }).join('') || '<span class="edg-muted">未保存来源</span>';
        return `<section class="edg-condition edg-metric-record" data-current-row="${esc(r.id)}"><header><strong>记录 ${i+1} · ${esc(field?.label || metricLabel(name))}</strong>${valueButton(r,v)}</header><div class="edg-condition-meta">${item('吸附物／研究对象',c.adsorbate)}${item('位点',c.site ?? r.active_site)}${item('构型',({edge:'边缘（edge）',basal:'基面（basal）'})[r.configuration ?? c.configuration] || r.configuration || c.configuration)}${c.reaction_cycle?item('记录用途',({adsorption:'吸附能计算',adsorption_geometry:'吸附构型分析'})[c.reaction_cycle] || c.reaction_cycle):''}${c.reaction_step || c.step?item('反应步骤',c.reaction_step ?? c.step):''}${c.solvent?item('溶剂',c.solvent):''}</div><div class="edg-metric-sources"><span>来源：</span>${sources}</div>${v.notes?`<p class="edg-hint">备注：${esc(v.notes)}</p>`:''}<details class="edg-more"><summary>计算／测试条件与材料详情</summary>${conditionContent(r)}${Object.keys(r.properties || {}).some(k=>!k.startsWith('_'))?`<p>${esc(readable(r.properties))}</p>`:''}<button class="edg-identity-button" data-edg-identity="${esc(r.id)}">编辑材料与条件</button></details></section>`;
      }).join('')}<button class="edg-expand" data-edg-show-all="${index}">查看该材料全部 ${group.length} 条条件记录</button>`;
    }
    function loadMetricFiles(group) {
      if(options.files)return;
      [...new Set(group.map(r=>r.paper_id))].forEach(owner=>{
        if(fileCache.has(owner))return;
        fileCache.set(owner,[]);
        api(`/api/rebuild/papers/${encodeURIComponent(owner)}/files`).then(data=>{fileCache.set(owner,data.items || []);render();}).catch(()=>{});
      });
    }
    function render() {
      Object.assign(view,{selectedGroup,query,chooserOpen});
      const visible=columns.filter(f=>!hidden.has(f.name) && (selectedGroup==='全部' || f.group===selectedGroup));
      const shown=grouped.map((group,index)=>({group,index})).filter(({group})=>(!query || [group[0].material,group[0].support,group[0].active_site].join(' ').toLowerCase().includes(query.toLowerCase())) && (selectedGroup==='全部' || group.some(row=>(row.values || []).some(value=>visible.some(field=>field.name===value.field_name)))));
      container.innerHTML=`<div class="edg-tools"><span>${grouped.length} 个催化剂 · ${rows.length} 条独立条件</span><div class="edg-actions"><input data-edg-search type="search" placeholder="搜索材料、载体或位点" aria-label="搜索催化剂" value="${esc(query)}"><button class="edg-select-button" data-edg-chooser aria-expanded="${chooserOpen}">指标列选择 · ${visible.length}/${columns.length}</button></div></div>
      <div class="edg-categories" aria-label="指标分组">${['全部',...categories].map(c=>`<button data-edg-category="${esc(c)}" aria-pressed="${c===selectedGroup}">${esc(c)} <small>${c==='全部'?columns.length:columns.filter(f=>f.group===c).length}</small></button>`).join('')}</div>
      ${chooserOpen?`<div class="edg-column-panel"><div class="edg-column-title">选择需要比较的指标 <button data-edg-all>全部显示</button><button data-edg-close>收起</button></div><div class="edg-column-list">${categories.map(c=>`<fieldset><legend>${esc(c)}</legend>${columns.filter(f=>f.group===c).map(f=>`<label><input type="checkbox" data-edg-column="${esc(f.name)}" ${hidden.has(f.name)?'':'checked'}><span title="${esc(f.name)}">${esc(f.label)}</span></label>`).join('')}</fieldset>`).join('')}</div></div>`:''}
      <div class="edg-hint">当前显示 ${shown.length}/${grouped.length} 个材料／位点组合；无此组指标的材料暂不显示。${selectedGroup!=='全部'?'当前按指标分组比较，可切换“全部”查看所有字段。':'点击具体值查看来源与编辑。'}</div>
      <div class="edg-scroll"><table class="edg-table"><thead><tr><th>催化剂</th><th>载体／位点</th>${visible.map(f=>`<th title="${esc(f.name)}">${esc(f.label)}${f.unit?`<small>${esc(f.unit)}</small>`:''}</th>`).join('')}</tr></thead><tbody>${shown.map(({group,index})=>{
        const row=group[0];
        return `<tr data-edg-group="${index}"><th><span class="edg-material">${esc(row.material || '身份待定')}</span>${row.identity_status==='pending'?'<small>身份待定，独立保留</small>':''}<button class="edg-expand" data-edg-expand="${index}" aria-expanded="${expanded.has(index)}">${expanded.has(index)?'收起':'展开'}条件（${group.length}）</button></th><td><div class="edg-site">${esc(row.support || '载体未记录')}</div><div class="edg-site-detail">${esc(row.active_site || '位点未记录')}</div><small>${esc(({single_atom:'单原子',dual_atom:'双原子',support:'载体',bulk:'体相'})[row.active_site_type] || row.active_site_type || '')}</small></td>${visible.map(f=>{const values=group.flatMap(r=>(r.values || []).filter(v=>v.field_name===f.name).map(v=>[r,v]));return `<td>${values.length===1?valueButton(...values[0]):values.length?`<button class="edg-value edg-multiple" data-edg-metric="${esc(f.name)}" data-edg-metric-group="${index}" aria-expanded="${expanded.has(index) && metricFocus.get(index)===f.name}">多组（${values.length}）</button>`:'<span class="edg-muted">—</span>'}</td>`;}).join('')}</tr>
        <tr class="edg-conditions" data-edg-conditions="${index}" ${expanded.has(index)?'':'hidden'}><td colspan="${visible.length+2}"><div class="edg-condition-wrap">${metricFocus.has(index)?metricDetails(group,index,metricFocus.get(index)):`<div class="edg-detail-heading">${esc(row.material)} · 条件与证据数据 <span>${group.length} 条记录，各条件单独保存</span></div>${group.map((r,i)=>`<section class="edg-condition" data-current-row="${esc(r.id)}"><header><strong>条件 ${i+1}${r.configuration?` · ${esc(r.configuration)}`:''}</strong><button class="edg-identity-button" data-edg-identity="${esc(r.id)}">编辑材料与条件</button></header>${conditionContent(r)}${Object.keys(r.properties || {}).some(k=>!k.startsWith('_'))?`<details class="edg-more"><summary>附加信息</summary><p>${esc(readable(r.properties))}</p></details>`:''}<div class="edg-condition-values">${(r.values || []).map(v=>`<div><span title="${esc(v.field_name)}">${esc(columns.find(f=>f.name===v.field_name)?.label || metricLabel(v.field_name))}</span>${valueButton(r,v)}</div>`).join('')}</div></section>`).join('')}`}</div></td></tr>`;
      }).join('') || `<tr><td colspan="${visible.length+2}">${query?'没有匹配的催化剂。':'此分组没有选中的数据列，请选择指标或切换全部。'}</td></tr>`}</tbody></table></div>`;
      container.querySelectorAll('[data-edg-expand]').forEach(button=>button.onclick=()=>{const id=Number(button.dataset.edgExpand);metricFocus.delete(id);expanded.has(id)?expanded.delete(id):expanded.add(id);render();});
      container.querySelectorAll('[data-edg-show-all]').forEach(button=>button.onclick=()=>{const index=Number(button.dataset.edgShowAll);metricFocus.delete(index);expanded.add(index);render();});
      container.querySelectorAll('[data-edg-metric]').forEach(button=>button.onclick=()=>{
        const index=Number(button.dataset.edgMetricGroup), name=button.dataset.edgMetric;
        if(expanded.has(index) && metricFocus.get(index)===name){expanded.delete(index);metricFocus.delete(index);}else{expanded.add(index);metricFocus.set(index,name);loadMetricFiles(grouped[index]);}
        render();
      });
      container.querySelector('[data-edg-chooser]').onclick=()=>{chooserOpen=!chooserOpen;render();};
      container.querySelector('[data-edg-close]')?.addEventListener('click',()=>{chooserOpen=false;render();});
      container.querySelector('[data-edg-all]')?.addEventListener('click',()=>{hidden.clear();selectedGroup='全部';render();});
      container.querySelectorAll('[data-edg-category]').forEach(button=>button.onclick=()=>{selectedGroup=button.dataset.edgCategory;render();});
      container.querySelectorAll('[data-edg-column]').forEach(input=>input.onchange=()=>{input.checked?hidden.delete(input.dataset.edgColumn):hidden.add(input.dataset.edgColumn);render();});
      container.querySelector('[data-edg-search]').oninput=event=>{query=event.target.value;const pos=event.target.selectionStart;render();const input=container.querySelector('[data-edg-search]');input.focus();if(pos!==null)input.setSelectionRange(pos,pos);};
      container.querySelectorAll('[data-edg-value]').forEach(button=>button.onclick=()=>{const row=rows.find(r=>r.id===button.dataset.edgRow),value=row.values.find(v=>v.id===button.dataset.edgValue);options.onValue?options.onValue(row,value):editValue(row,value,options);});
      container.querySelectorAll('[data-edg-identity]').forEach(button=>button.onclick=()=>{const row=rows.find(r=>r.id===button.dataset.edgIdentity);options.onIdentity?options.onIdentity(row):editIdentity(row,options);});
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
  function displayRole(file,owner,familyId) {return file?.role==='si' || familyId && owner!==familyId ? 'si' : file?.role;}
  function sourceHtml(source, files, owner, familyId) {
    const file = files.find(f => f.id === source.file_id), page = Number(source.page_number);
    const link = source.file_id ? `/api/rebuild/files/${encodeURIComponent(source.file_id)}/preview${Number.isInteger(page) && page > 0 ? '#page=' + page : ''}` : '';
    return `<article class="edg-source"><strong>${esc(displayRole(file,owner,familyId) === 'si' ? 'SI' : file?.role === 'main' ? '正文' : '来源未登记')} · ${esc(file?.original_filename || '')}</strong><div>${esc(source.label || source.source_kind)} · ${page > 0 ? 'PDF 第 ' + page + ' 页' : '页码未记录'}</div><blockquote>${esc(source.quote || source.estimate_basis || '未保存引文')}</blockquote>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">打开证据 PDF</a>` : ''}${source.asset_id ? ` <a href="/api/rebuild/assets/${encodeURIComponent(source.asset_id)}" target="_blank" rel="noopener">打开证据图表</a>` : ''}</article>`;
  }
  // Match the backend's raw-number grammar without changing the stored text.
  // Python str.strip()/regex whitespace includes NEL and excludes BOM.
  const numericSpace = '[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]';
  const numericMantissa = '[+\\-−]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)';
  const numericExponent = '[+\\-−]?[0-9]{1,6}';
  const superscriptExponent = '[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,6}';
  const numericTrim = new RegExp(`^${numericSpace}+|${numericSpace}+$`, 'g');
  const decimalNotation = new RegExp(`^(${numericMantissa})(?:[eE](${numericExponent}))?$`);
  const powerNotation = new RegExp(`^(?:(${numericMantissa})${numericSpace}*[×·]${numericSpace}*)?10(?:${numericSpace}*\\^${numericSpace}*(${numericExponent})|(${superscriptExponent}))$`);
  const superscriptAscii = Object.fromEntries(Array.from('⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹', (glyph,index) => [glyph,'+-0123456789'[index]]));
  function rawNumericProjection(raw) {
    const text=String(raw).replace(numericTrim,'');
    const match=decimalNotation.exec(text) || powerNotation.exec(text);
    // JS $ may match before a final newline; require the entire input as Python does.
    if(!match || match[0]!==text)return null;
    const coefficient=(match[1] || '1').replace(/−/g,'-');
    const exponent=(match[2] || (match[3] && Array.from(match[3],glyph=>superscriptAscii[glyph]).join('')) || '0').replace(/−/g,'-');
    // Parse the full coefficient/exponent once: coefficient * 10**exponent would
    // round twice and may underflow/overflow an otherwise representable value.
    const number=Number(`${coefficient}e${exponent}`);
    return Number.isFinite(number) && !(number===0 && /[1-9]/.test(coefficient)) ? number : null;
  }
  async function editValue(row,value,options) {
    const panel = dialog(`${row.material} · ${options.templates?.common_fields?.find(f=>f.name===value.field_name)?.label || Object.values(options.templates?.reactions || {}).flatMap(t=>t.fields || []).find(f=>f.name===value.field_name)?.label || metricLabel(value.field_name)}`), body=panel.querySelector('[data-body]');
    body.textContent='正在读取来源…';
    let files;
    try {files=(await api(`/api/rebuild/papers/${row.paper_id}/files`)).items;} catch(error) {body.textContent=`来源读取失败：${error.message}`; return;}
    const source = value.sources?.find(s => s.id === value.preferred_source_id) || value.sources?.[0] || {};
    const input = (name,label,v,type='text') => `<label>${label}<input name="${name}" type="${type}" ${type==='number' ? 'step="any"' : ''} value="${esc(v ?? '')}"></label>`;
    body.innerHTML = `<div>${(value.sources || []).map(s => sourceHtml(s,files,row.paper_id,options.familyId)).join('') || '未保存来源'}</div><details><summary>修改历史与冲突</summary><pre>${esc(JSON.stringify(value.conflict || {},null,2))}</pre></details><form>${input('raw_value','原值（保留精度）',value.raw_value)}${input('numeric_value','数值（无法确定时留空）',value.numeric_value,'number')}${input('unit','单位',value.unit)}<label>值类型<select name="value_type">${['explicit','estimated','derived','missing'].map(t=>`<option value="${t}" ${t===value.value_type?'selected':''}>${({explicit:'原文明确值',estimated:'估读值',derived:'推导值',missing:'缺失'})[t]}</option>`).join('')}</select></label>${input('missing_reason','缺失原因',value.missing_reason)}${input('notes','备注',value.notes)}<label>来源文件<select name="file_id"><option value="">来源文件未标注</option>${files.map(f=>`<option value="${esc(f.id)}" ${f.id===source.file_id?'selected':''}>${displayRole(f,row.paper_id,options.familyId)==='si'?'SI':'正文'} · ${esc(f.original_filename)}</option>`).join('')}</select></label>${input('page_number','PDF 页码',source.page_number,'number')}${input('label','图表号／表号',source.label)}<label>原文引文<textarea name="quote">${esc(source.quote || '')}</textarea></label><label>修改理由<textarea name="correction_reason" required></textarea></label><button type="submit">保存修改</button><p data-status role="status"></p></form>`;
    const form=body.querySelector('form'), status=form.querySelector('[data-status]'), key=`litai.value-correction.${value.id}`;
    const rawInput=form.elements.namedItem('raw_value'),numericInput=form.elements.namedItem('numeric_value');
    numericInput.readOnly=true;numericInput.setAttribute('aria-label','数值（由原值自动读取）');
    rawInput.addEventListener('input',()=>{const number=rawNumericProjection(rawInput.value);numericInput.value=number===null ? '' : String(number);});
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
    body.innerHTML=`<p>服务器将读取历史原值，原历史记录保留。缺失值保持为空，范围值保持上下界原文；无法确定来源的记录须先核对。</p><form><label>历史字段<select name="legacy_field">${legacyFields.map(f=>`<option value="${f}">${esc(f)} · ${esc(record[f] ?? '缺失')}</option>`).join('')}</select></label><label>目标指标字段<input name="field_name" required pattern="[A-Za-z][A-Za-z0-9_]*"></label><label>单位<input name="unit" value="${esc(record.unit || '')}"></label><label>反应<select name="reaction">${Object.entries(templates.reactions || {}).map(([name,t])=>`<option value="${esc(name)}">${esc(t.label || name)}</option>`).join('')}</select></label><label>材料<input name="material" required></label><label>载体<input name="support"></label><label>位点<input name="active_site"></label><label>活性中心类型<select name="active_site_type">${(templates.active_site_types || []).map(t=>`<option value="${esc(t.value)}">${esc(t.label)}</option>`).join('')}</select></label><label>条件（JSON 对象）<textarea name="condition">{}</textarea></label><label>来源 PDF<select name="file_id" required>${(options.files || []).map(f=>`<option value="${esc(f.id)}">${f.role==='si'?'SI':'正文'} · ${esc(f.original_filename)}</option>`).join('')}</select></label><label>PDF 页码<input name="page_number" type="number" min="1" required></label><label>图表号<input name="label"></label><label>原文引文<textarea name="quote"></textarea></label><label>估读／推导依据（原记录属于估读或推导时填写）<textarea name="estimate_basis"></textarea></label><label>转入理由<textarea name="reason" required></textarea></label><button type="submit">确认带来源转入</button><p data-status role="status"></p></form>`;
    const form=body.querySelector('form'), status=form.querySelector('[data-status]');let intent=null;
    form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{if(!intent){const d=Object.fromEntries(new FormData(form));const condition=JSON.parse(d.condition);if(!condition || typeof condition!=='object' || Array.isArray(condition))throw Error('条件须为 JSON 对象');intent={legacy_kind:kind,legacy_id:record.id,legacy_field:d.legacy_field,field_name:d.field_name,unit:d.unit || null,reaction:d.reaction,material:d.material,support:d.support || null,active_site:d.active_site || null,active_site_type:d.active_site_type,condition,source:{file_id:d.file_id,source_kind:'text',page_number:Number(d.page_number),label:d.label || null,quote:d.quote || null,estimate_basis:d.estimate_basis || null},reason:d.reason,request_id:crypto.randomUUID()};}await api(`/api/rebuild/papers/${paperId}/history-transfer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(intent)});await options.reload?.();panel.close();}catch(error){status.textContent='转入未确认：'+error.message+'；重试将复用原请求。';}finally{button.disabled=false;}};
  }
  async function editIdentity(row,options) {
    const panel=dialog('编辑材料与条件'), body=panel.querySelector('[data-body]');body.textContent='正在读取依据文件…';
    let files;try{files=(await api(`/api/rebuild/papers/${row.paper_id}/files`)).items;}catch(error){body.textContent='文件读取失败：'+error.message;return;}
    const field=(name,label,value)=>`<label>${label}<input name="${name}" value="${esc(value)}"></label>`;
    body.innerHTML=`<p>条件：${esc(readable(row.condition))}。各条件保留独立记录，不合并数值。</p><form>${field('material','材料',row.material)}${field('support','载体',row.support)}${field('active_site','位点',row.active_site)}${field('configuration','构型',row.configuration)}<label>活性中心类型<select name="active_site_type">${(options.templates?.active_site_types || []).map(t=>`<option value="${esc(t.value)}" ${t.value===row.active_site_type?'selected':''}>${esc(t.label)}</option>`).join('')}</select></label><label>完整条件（JSON 对象）<textarea name="condition">${esc(JSON.stringify(row.condition || {},null,2))}</textarea></label><label>依据 PDF<select name="file_id" required>${files.map(f=>`<option value="${esc(f.id)}">${displayRole(f,row.paper_id,options.familyId)==='si'?'SI':'正文'} · ${esc(f.original_filename)}</option>`).join('')}</select></label><label>PDF 页码<input name="page_number" type="number" min="1" required></label>${field('label','图表号','')}<label>原文依据<textarea name="quote"></textarea></label><label>修改理由<textarea name="correction_reason" required></textarea></label><button type="submit">保存身份与条件</button><p data-status role="status"></p></form>`;
    const form=body.querySelector('form'), status=form.querySelector('[data-status]'),key=`litai.identity-correction.${row.paper_id}.${row.id}`;
    if(row.identity_status==='pending'){status.textContent='身份待定：请通过带来源的数据导入补交缺失身份。';form.querySelector('[type=submit]').disabled=true;return;}
    form.onsubmit=async event=>{event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;try{let intent;const saved=sessionStorage.getItem(key);if(saved)intent=JSON.parse(saved);else{const d=Object.fromEntries(new FormData(form)),condition=JSON.parse(d.condition);if(!condition || typeof condition!=='object' || Array.isArray(condition))throw Error('条件须为 JSON 对象');intent={paper_id:row.paper_id,row_key:row.row_key,expected_revision:row.identity_revision,request_id:crypto.randomUUID(),changes:{material:d.material,support:d.support || null,active_site:d.active_site || null,configuration:d.configuration || null,active_site_type:d.active_site_type,condition},correction_reason:d.correction_reason,evidence:[{file_id:d.file_id,source_kind:'text',page_number:Number(d.page_number),label:d.label || null,quote:d.quote || null}]};sessionStorage.setItem(key,JSON.stringify(intent));}await api(`/api/rebuild/rows/${row.id}/identity`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(intent)});sessionStorage.removeItem(key);await options.reload?.();panel.close();}catch(error){if(error.status && error.status<500)sessionStorage.removeItem(key);status.textContent=error.status===409?'此行已被修改或与已有身份条件冲突，请刷新后核对。':'保存未确认：'+error.message+'。重试将复用原请求。';}finally{button.disabled=false;}};
  }
  global.EditableDataGrid={mount,groups,fields,readable,metricLabel,displayRole,editValue,sourceHtml,transferHistory};
})(window);
