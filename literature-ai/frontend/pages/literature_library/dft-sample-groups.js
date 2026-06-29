// DFT catalyst sample grouping and catalyst summary renderers.
function isDftCandidateCardTitle(title) {
    return title === "候选 DFT 数据" || title === "DFT 候选结果";
}

function nestedReadableValue(item, path) {
    const parts = String(path || "").split(".");
    let value = item;
    for (let i = 0; i < parts.length; i += 1) {
        if (!value || typeof value !== "object") return "";
        value = value[parts[i]];
    }
    if (value && typeof value === "object" && !Array.isArray(value) && value.value !== undefined) {
        value = value.value;
    }
    const text = readableValue(value);
    return text && text !== "-" ? text : "";
}

function firstNestedReadableValue(item, paths) {
    for (let i = 0; i < paths.length; i += 1) {
        const value = nestedReadableValue(item, paths[i]);
        if (value) return value;
    }
    return "";
}

function firstGroupReadableValue(group, paths) {
    const entries = group && Array.isArray(group.entries) ? group.entries : [];
    for (let i = 0; i < entries.length; i += 1) {
        const value = firstNestedReadableValue(entries[i].item || {}, paths);
        if (value) return value;
    }
    return "";
}

function renderDftCatalystInfoField(label, value, missingText) {
    const text = value || missingText || "待补";
    const missing = !value;
    return '<div class="readable-field' + (missing ? ' missing-field' : '') + '">' +
        '<div class="k">' + esc(label) + '</div>' +
        '<div class="v">' + esc(text) + '</div>' +
    '</div>';
}

const CATALYST_BASIC_INFO_SUPPORTS = [
    "UNKNOWN",
    "graphene",
    "N_doped_carbon",
    "carbon",
    "C3N4",
    "C2N",
    "GeC",
    "MoS2",
    "MXene",
    "TiO2",
    "CeO2",
    "other"
];
const CATALYST_BASIC_INFO_TYPES = [
    "unknown",
    "single_atom",
    "dual_atom",
    "multi_atom_cluster",
    "surface",
    "defect_site"
];
const CATALYST_BASIC_INFO_TYPE_LABELS = {
    unknown: "待确认",
    single_atom: "单原子（single_atom）",
    dual_atom: "双原子（dual_atom）",
    multi_atom_cluster: "多原子团簇（multi_atom_cluster）",
    surface: "表面位点（surface）",
    defect_site: "缺陷位点（defect_site）"
};
const CATALYST_BASIC_INFO_SUPPORT_LABELS = {
    UNKNOWN: "待确认",
    graphene: "石墨烯（graphene）",
    N_doped_carbon: "氮掺杂碳（N_doped_carbon）",
    carbon: "碳载体（carbon）",
    C3N4: "氮化碳（C3N4）",
    C2N: "C2N",
    GeC: "GeC",
    MoS2: "MoS2",
    MXene: "MXene",
    TiO2: "TiO2",
    CeO2: "CeO2",
    other: "其他（other）"
};

function renderCatalystBasicInfoOption(value, selectedValue, labels) {
    const label = labels && labels[value] ? labels[value] : value;
    return '<option value="' + escAttr(value) + '"' + (String(value) === String(selectedValue || "") ? " selected" : "") + '>' + esc(label) + '</option>';
}

function renderDftCatalystDescriptorSummary(sample) {
    sample = sample || {};
    const metal1 = sample.metal_1_descriptors || {};
    const metal2 = sample.metal_2_descriptors || {};
    const combined = sample.dac_combined_descriptors || {};
    const parts = [];
    if (metal1.element_symbol) {
        parts.push("M1 " + metal1.element_symbol + " χ=" + readableValue(metal1.electronegativity));
    }
    if (metal2.element_symbol) {
        parts.push("M2 " + metal2.element_symbol + " χ=" + readableValue(metal2.electronegativity));
    }
    if (combined.electronegativity_delta !== null && combined.electronegativity_delta !== undefined) {
        parts.push("Δχ=" + readableValue(combined.electronegativity_delta));
    }
    return parts.join("；");
}

function renderDftCatalystBasicInfoForm(sample, group) {
    sample = sample || {};
    const sampleId = sample.id ? String(sample.id) : "";
    const editorKey = sampleId || group.key;
    const dftResultIds = group.entries
        .map(function(entry) { return entry && entry.item && entry.item.id ? String(entry.item.id) : ""; })
        .filter(Boolean);
    const supportValue = sample.support_normalized || sample.support || "";
    const catalystType = sample.catalyst_type || "unknown";
    const metalCenters = Array.isArray(sample.metal_centers) ? sample.metal_centers.join(", ") : "";
    return '<div class="dft-basic-info-form" data-editor-key="' + escAttr(editorKey) + '"' +
        ' data-mode="' + (sampleId ? "update" : "create") + '"' +
        ' data-catalyst-sample-id="' + escAttr(sampleId) + '"' +
        ' data-dft-result-ids="' + escAttr(dftResultIds.join(",")) + '" hidden>' +
        (!sampleId ? '<div class="subtle dft-basic-info-edit-note">保存后系统会自动创建基础信息记录，并关联本框内的 DFT 数据；无需先去其他页面绑定。</div>' : '') +
        '<div class="dft-basic-info-grid">' +
            '<label class="dft-basic-info-field"><span>名称</span><input type="text" data-field="name" value="' + escAttr(sample.name || group.meta.catalystLabel || "") + '"></label>' +
            '<label class="dft-basic-info-field"><span>催化剂类型</span><select data-field="catalyst_type">' + CATALYST_BASIC_INFO_TYPES.map(function(value) { return renderCatalystBasicInfoOption(value, catalystType, CATALYST_BASIC_INFO_TYPE_LABELS); }).join("") + '</select></label>' +
            '<label class="dft-basic-info-field"><span>金属中心</span><input type="text" data-field="metal_centers" placeholder="例如：Fe, Co" autocomplete="off" spellcheck="false" value="' + escAttr(metalCenters) + '"><small>请填元素符号；多个推荐用逗号分隔</small></label>' +
            '<label class="dft-basic-info-field"><span>载体/基底</span><select data-field="support">' + CATALYST_BASIC_INFO_SUPPORTS.map(function(value) { return renderCatalystBasicInfoOption(value, supportValue, CATALYST_BASIC_INFO_SUPPORT_LABELS); }).join("") + '</select></label>' +
            '<label class="dft-basic-info-field dft-basic-info-span-2"><span>配位环境</span><input type="text" data-field="coordination" placeholder="例如：Fe-N4 或 Co-Ge bridge" value="' + escAttr(sample.coordination || "") + '"></label>' +
            '<label class="dft-basic-info-field dft-basic-info-span-2"><span>合成/构型说明</span><input type="text" data-field="synthesis_method" placeholder="选填" value="' + escAttr(sample.synthesis_method || "") + '"></label>' +
            '<label class="dft-basic-info-field"><span>证据页码</span><input type="text" data-field="evidence_page" placeholder="选填" value=""></label>' +
            '<label class="dft-basic-info-field dft-basic-info-span-3"><span>证据原文</span><input type="text" data-field="evidence_text" placeholder="选填，可粘贴对应原文" value=""></label>' +
        '</div>' +
        '<div class="filter-actions dft-basic-info-actions">' +
            '<button type="button" class="btn primary small" onclick="saveCatalystBasicInfo(\'' + escAttr(editorKey) + '\')">' + (sampleId ? "保存基础信息" : "创建并关联") + '</button>' +
            '<button type="button" class="btn ghost small" onclick="toggleCatalystBasicInfoEditor(\'' + escAttr(editorKey) + '\')">取消</button>' +
        '</div>' +
    '</div>';
}

function renderDftCatalystBaseInfo(group, catalystSample) {
    const sample = catalystSample || {};
    const metalCenters = readableValue(sample.metal_centers || firstGroupReadableValue(group, [
        "metal_centers",
        "evidence_payload.metal_centers",
        "active_site_ref.metal_centers",
        "evidence_payload.active_site_ref.metal_centers"
    ]));
    const fields = [
        renderDftCatalystInfoField("催化剂/材料", group.meta.catalystLabel),
        renderDftCatalystInfoField("活性位点", group.meta.activeSiteLabel === "活性位点待补" ? "" : group.meta.activeSiteLabel, "活性位点待补"),
        renderDftCatalystInfoField("金属中心", metalCenters && metalCenters !== "-" ? metalCenters : ""),
        renderDftCatalystInfoField("催化剂类型", readableValue(sample.catalyst_type)),
        renderDftCatalystInfoField("配位环境", readableValue(sample.coordination) || firstGroupReadableValue(group, [
            "coordination_environment",
            "active_site_ref.coordination_environment",
            "evidence_payload.coordination_environment",
            "evidence_payload.active_site_ref.coordination_environment"
        ])),
        renderDftCatalystInfoField("载体/基底", readableValue(sample.support) || firstGroupReadableValue(group, [
            "support",
            "support_material",
            "active_site_ref.support",
            "evidence_payload.support",
            "evidence_payload.active_site_ref.support"
        ])),
        renderDftCatalystInfoField("金属-金属距离", firstGroupReadableValue(group, [
            "metal_metal_distance_A",
            "metal_metal_distance",
            "active_site_ref.metal_metal_distance_A",
            "active_site_ref.metal_metal_distance",
            "evidence_payload.metal_metal_distance_A",
            "evidence_payload.active_site_ref.metal_metal_distance_A"
        ])),
        renderDftCatalystInfoField("吸附位点", firstGroupReadableValue(group, [
            "adsorption_site",
            "active_site_ref.adsorption_site",
            "evidence_payload.adsorption_site",
            "evidence_payload.active_site_ref.adsorption_site"
        ])),
        renderDftCatalystInfoField("吸附构型", firstGroupReadableValue(group, [
            "adsorption_mode",
            "active_site_ref.adsorption_mode",
            "evidence_payload.adsorption_mode",
            "evidence_payload.active_site_ref.adsorption_mode"
        ])),
        renderDftCatalystInfoField("元素描述符", renderDftCatalystDescriptorSummary(sample) || firstGroupReadableValue(group, [
            "metal_descriptor_summary",
            "element_descriptor_summary",
            "evidence_payload.metal_descriptor_summary",
            "evidence_payload.element_descriptor_summary"
        ]), "由金属中心自动生成")
    ];
    return '<details class="section-card readable-card dft-catalyst-base-info">' +
        '<summary><h3 style="margin:0;">催化剂基础信息</h3><span class="subtle">证据可选填；字段会标准化</span>' +
            '<button type="button" class="btn ghost small" onclick="event.stopPropagation(); toggleCatalystBasicInfoEditor(\'' + escAttr(sample && sample.id ? String(sample.id) : group.key) + '\')">' +
                (sample && sample.id ? "编辑基础信息" : "补充基础信息") +
            '</button>' +
        '</summary>' +
        '<div class="readable-grid compact-readable-grid" style="margin-top:8px;">' + fields.join("") + '</div>' +
        renderDftCatalystBasicInfoForm(sample, group) +
    '</details>';
}

function dftSampleGroupMeta(item) {
    item = item || {};
    const catalystSampleId = firstNestedReadableValue(item, [
        "catalyst_sample_id",
        "active_site_ref.catalyst_sample_id",
        "evidence_payload.catalyst_sample_id",
        "evidence_payload.active_site_ref.catalyst_sample_id"
    ]);
    const catalystLabel = firstNestedReadableValue(item, [
        "catalyst",
        "catalyst_name",
        "material_identity",
        "material",
        "normalized_material",
        "structure_name",
        "evidence_payload.material_identity",
        "evidence_payload.material",
        "evidence_payload.normalized_material",
        "active_site_ref.material_identity",
        "active_site_ref.material",
        "active_site_ref.structure_name"
    ]);
    const activeSiteKey = firstNestedReadableValue(item, [
        "active_site_instance_key",
        "active_site_ref.active_site_instance_key",
        "active_site_ref.instance_key",
        "evidence_payload.active_site_instance_key",
        "evidence_payload.active_site_ref.active_site_instance_key",
        "evidence_payload.active_site_ref.instance_key"
    ]);
    const sampleKey = catalystSampleId || catalystLabel || "unbound-catalyst";
    const siteKey = activeSiteKey || "unbound-active-site";
    return {
        key: sampleKey + "|" + siteKey,
        catalystLabel: catalystLabel || (catalystSampleId ? ("CatalystSample " + catalystSampleId) : "未绑定催化剂"),
        activeSiteLabel: activeSiteKey || "活性位点待补",
        catalystSampleId: catalystSampleId
    };
}

function isDftItemExportable(item) {
    const safety = item && item.export_safety || {};
    const candidateStatus = String(item && item.candidate_status || "").trim().toLowerCase();
    const workflowState = String(item && item.dft_workflow_state || "").trim().toLowerCase();
    return safety.is_exportable === true ||
        safety.eligible === true ||
        candidateStatus === "ml_ready" ||
        workflowState === "exportable";
}

function renderDftSampleGroups(items, renderItem, options) {
    options = options || {};
    const catalystSamplesById = options.catalystSamplesById || {};
    const groups = [];
    const byKey = {};
    items.forEach(function(item, index) {
        const meta = dftSampleGroupMeta(item);
        if (!byKey[meta.key]) {
            byKey[meta.key] = {
                key: meta.key,
                meta: meta,
                entries: []
            };
            groups.push(byKey[meta.key]);
        }
        byKey[meta.key].entries.push({ item: item, index: index });
    });
    let displayIndex = 0;
    return groups.map(function(group, groupIndex) {
        const readyCount = group.entries.filter(function(entry) { return isDftItemExportable(entry.item); }).length;
        const body = group.entries.map(function(entry) {
            const currentDisplayIndex = displayIndex;
            displayIndex += 1;
            return renderItem(entry.item, currentDisplayIndex);
        }).join("");
        const groupTitle = groups.length > 1 ? ("催化剂样本 " + (groupIndex + 1)) : "催化剂样本";
        const catalystSample = group.meta.catalystSampleId ? catalystSamplesById[String(group.meta.catalystSampleId)] : null;
        const catalystNavigationAttrs = group.meta.catalystSampleId
            ? ' data-codex-item-type="catalyst_sample" data-target-id="' + escAttr(String(group.meta.catalystSampleId)) + '"'
            : "";
        const groupOpenAttr = (
            (group.meta.catalystSampleId && isPendingNavigationItem("catalyst_sample", { id: group.meta.catalystSampleId })) ||
            group.entries.some(function(entry) { return isPendingNavigationItem("dft_result", entry.item); })
        ) ? " open" : "";
        return '<details class="section-card dft-sample-group" data-role="dft-sample-group" data-dft-sample-key="' + escAttr(group.key) + '"' + catalystNavigationAttrs + groupOpenAttr + '>' +
            '<summary><div class="dft-sample-summary">' +
                '<div><h3>' + esc(groupTitle) + '</h3><div class="subtle">' + esc(group.meta.catalystLabel) + ' / ' + esc(group.meta.activeSiteLabel) + '</div></div>' +
                '<div class="dft-sample-meta">' +
                    '<span class="status-chip">DFT ' + group.entries.length + ' 条</span>' +
                    '<span class="status-chip ' + (readyCount ? 'ok' : 'meta') + '">可导出 ' + readyCount + '</span>' +
                    (group.meta.catalystSampleId ? '<span class="status-chip none">基础信息已关联</span>' : '<span class="status-chip meta">基础信息待补</span>') +
                '</div>' +
            '</div></summary>' +
            '<div class="dft-sample-group-body">' + renderDftCatalystBaseInfo(group, catalystSample) + body + '</div>' +
        '</details>';
    }).join("");
}

const CODEX_ITEM_TYPE_BY_CARD_TITLE = {
    "DFT 设置": "dft_setting",
    "催化剂样本": "catalyst_sample",
    "DFT 结果": "dft_result",
    "候选 DFT 数据": "dft_result",
    "DFT 候选结果": "dft_result",
    "电化学性能": "electrochemical_performance",
    "机理声明": "mechanism_claim",
    "写作卡片": "writing_card",
    "表格": "table"
};
