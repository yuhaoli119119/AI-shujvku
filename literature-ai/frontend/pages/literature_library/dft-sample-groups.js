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

function normalizedDftSampleLabel(value) {
    return String(value || "")
        .trim()
        .toLowerCase()
        .replace(/[\s_\-\/|()[\]{}.,;:]+/g, "");
}

function dftSampleGroupCatalystLabel(group, catalystSample) {
    const sampleLabel = firstNestedReadableValue(catalystSample || {}, [
        "name",
        "material_identity",
        "catalyst",
        "normalized_material",
        "structure_name"
    ]);
    const rawLabel = (group && group.meta && group.meta.catalystLabel) || "";
    const catalystSampleId = group && group.meta && group.meta.catalystSampleId;
    const isIdentifierFallback = catalystSampleId && rawLabel === ("CatalystSample " + catalystSampleId);
    if (sampleLabel) return sampleLabel;
    if (rawLabel && !isIdentifierFallback) return rawLabel;
    return catalystSampleId ? "已关联催化剂（名称待补）" : "未绑定催化剂";
}

function dftSampleGroupActiveSiteLabel(group, catalystLabel) {
    const rawLabel = group && group.meta ? String(group.meta.activeSiteLabel || "").trim() : "";
    if (!rawLabel || rawLabel === "活性位点待补") return rawLabel || "活性位点待补";

    const keyParts = rawLabel.split("|").map(function(part) { return part.trim(); }).filter(Boolean);
    const firstPart = keyParts[0] || rawLabel;
    const firstNormalized = normalizedDftSampleLabel(firstPart);
    const isRepeatedKey = keyParts.length > 1 && keyParts.every(function(part) {
        return normalizedDftSampleLabel(part) === firstNormalized;
    });
    const displayLabel = isRepeatedKey ? firstPart : rawLabel;
    return normalizedDftSampleLabel(displayLabel) === normalizedDftSampleLabel(catalystLabel) ? "" : displayLabel;
}

function shortCatalystSampleId(value) {
    const text = String(value || "").trim();
    return text ? text.slice(0, 8) : "";
}

function catalystSampleSelectLabel(sample) {
    sample = sample || {};
    const sampleId = String(sample.id || "").trim();
    const name = firstNestedReadableValue(sample, ["name", "material_identity", "catalyst"]) || "名称待补";
    return name + " · " + shortCatalystSampleId(sampleId);
}

function renderDftGroupRebindForm(group, allItems, catalystSamplesById) {
    const sourceSampleId = String(group && group.meta && group.meta.catalystSampleId || "").trim();
    if (!sourceSampleId) return "";
    const editorKey = "rebind:" + group.key;
    const resultIds = (allItems || []).filter(function(item) {
        return String(dftSampleGroupMeta(item).catalystSampleId || "") === sourceSampleId;
    }).map(function(item) {
        return String(item && item.id || "").trim();
    }).filter(Boolean);
    const targetSamples = Object.keys(catalystSamplesById || {}).map(function(sampleId) {
        return catalystSamplesById[sampleId];
    }).filter(function(sample) {
        return sample &&
            String(sample.id || "") !== sourceSampleId &&
            Boolean(String(sample.name || "").trim());
    }).sort(function(left, right) {
        return catalystSampleSelectLabel(left).localeCompare(catalystSampleSelectLabel(right), "zh-CN");
    });
    const options = targetSamples.map(function(sample) {
        return '<option value="' + escAttr(String(sample.id || "")) + '">' + esc(catalystSampleSelectLabel(sample)) + '</option>';
    }).join("");
    return '<div class="dft-group-rebind-form" data-editor-key="' + escAttr(editorKey) + '"' +
            ' data-source-sample-id="' + escAttr(sourceSampleId) + '"' +
            ' data-dft-result-ids="' + escAttr(resultIds.join(",")) + '" hidden>' +
            '<div class="subtle">将改绑当前催化剂样本在所有活性位点子组中的 <strong>' + resultIds.length + '</strong> 条 DFT 数据。</div>' +
            '<label><span>目标催化剂样本</span><select data-field="target_sample_id" onchange="updateDftGroupRebindTarget(\'' + escAttr(editorKey) + '\')">' +
                '<option value="">请选择目标样本</option>' + options +
            '</select></label>' +
            '<div class="dft-group-rebind-full-id" data-role="selected-target-id">完整 UUID：未选择</div>' +
            '<label><span>修改原因（必填）</span><textarea data-field="reason" rows="2" placeholder="说明命名错误及改绑依据"></textarea></label>' +
            '<div class="dft-group-rebind-warning">提交后，整组数据会恢复为 system_candidate，并重新进入待复核流程。</div>' +
            '<div class="dft-group-rebind-error" data-role="rebind-error" hidden></div>' +
            '<div class="filter-actions">' +
                '<button type="button" class="btn primary small" data-role="rebind-submit" onclick="submitDftGroupRebind(\'' + escAttr(editorKey) + '\')"' + (targetSamples.length ? "" : " disabled") + '>确认整组重新关联</button>' +
                '<button type="button" class="btn ghost small" onclick="toggleDftGroupRebindEditor(\'' + escAttr(editorKey) + '\')">取消</button>' +
            '</div>' +
            (targetSamples.length ? "" : '<div class="subtle">当前文献没有其他已命名的可选催化剂样本。</div>') +
        '</div>';
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
    const rawGroupLabel = group && group.meta ? String(group.meta.catalystLabel || "") : "";
    const fallbackSampleLabel = sampleId ? ("CatalystSample " + sampleId) : "";
    const sampleName = sample.name || (rawGroupLabel === fallbackSampleLabel ? "" : rawGroupLabel);
    return '<div class="dft-basic-info-form" data-editor-key="' + escAttr(editorKey) + '"' +
        ' data-mode="' + (sampleId ? "update" : "create") + '"' +
        ' data-catalyst-sample-id="' + escAttr(sampleId) + '"' +
        ' data-dft-result-ids="' + escAttr(dftResultIds.join(",")) + '" hidden>' +
        (!sampleId ? '<div class="subtle dft-basic-info-edit-note">保存后系统会自动创建基础信息记录，并关联本框内的 DFT 数据；无需先去其他页面绑定。</div>' : '') +
        '<div class="dft-basic-info-grid">' +
            '<label class="dft-basic-info-field"><span>名称</span><input type="text" data-field="name" value="' + escAttr(sampleName) + '"></label>' +
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

function renderDftCatalystBaseInfo(group, catalystSample, allItems, catalystSamplesById) {
    const sample = catalystSample || {};
    const sourceSampleId = String(group && group.meta && group.meta.catalystSampleId || "").trim();
    const basicInfoEditorKey = sample && sample.id ? String(sample.id) : group.key;
    const rebindEditorKey = "rebind:" + group.key;
    const catalystLabel = dftSampleGroupCatalystLabel(group, sample);
    const activeSiteLabel = dftSampleGroupActiveSiteLabel(group, catalystLabel);
    const metalCenters = readableValue(sample.metal_centers || firstGroupReadableValue(group, [
        "metal_centers",
        "evidence_payload.metal_centers",
        "active_site_ref.metal_centers",
        "evidence_payload.active_site_ref.metal_centers"
    ]));
    const fields = [
        renderDftCatalystInfoField("催化剂/材料", catalystLabel),
        renderDftCatalystInfoField("活性位点", activeSiteLabel, "活性位点待补"),
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
    const rebindButton = sourceSampleId
        ? '<button type="button" class="btn ghost small" onclick="event.stopPropagation(); toggleDftGroupRebindEditor(\'' + escAttr(rebindEditorKey) + '\')">整组重新关联</button>'
        : "";
    return '<details class="section-card readable-card dft-catalyst-base-info">' +
        '<summary><h3 style="margin:0;">催化剂基础信息</h3><span class="subtle">证据可选填；字段会标准化</span>' +
            '<span class="dft-catalyst-base-actions">' +
                rebindButton +
                '<button type="button" class="btn ghost small" onclick="event.stopPropagation(); toggleCatalystBasicInfoEditor(\'' + escAttr(basicInfoEditorKey) + '\')">' +
                    (sample && sample.id ? "编辑基础信息" : "补充基础信息") +
                '</button>' +
            '</span>' +
        '</summary>' +
        renderDftGroupRebindForm(group, allItems, catalystSamplesById) +
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
        const linkedCatalystSample = group.meta.catalystSampleId ? catalystSamplesById[String(group.meta.catalystSampleId)] : null;
        const catalystSample = linkedCatalystSample || (group.meta.catalystSampleId ? { id: group.meta.catalystSampleId } : null);
        const catalystLabel = dftSampleGroupCatalystLabel(group, catalystSample);
        const activeSiteLabel = dftSampleGroupActiveSiteLabel(group, catalystLabel);
        const summaryLabels = [catalystLabel, activeSiteLabel].filter(Boolean);
        const catalystNavigationAttrs = group.meta.catalystSampleId
            ? ' data-codex-item-type="catalyst_sample" data-target-id="' + escAttr(String(group.meta.catalystSampleId)) + '"'
            : "";
        const groupOpenAttr = (
            (group.meta.catalystSampleId && isPendingNavigationItem("catalyst_sample", { id: group.meta.catalystSampleId })) ||
            group.entries.some(function(entry) { return isPendingNavigationItem("dft_result", entry.item); })
        ) ? " open" : "";
        return '<details class="section-card dft-sample-group" data-role="dft-sample-group" data-dft-sample-key="' + escAttr(group.key) + '"' + catalystNavigationAttrs + groupOpenAttr + '>' +
            '<summary><div class="dft-sample-summary">' +
                '<div><h3>' + esc(groupTitle) + '</h3><div class="subtle">' + esc(summaryLabels.join(" / ")) + '</div></div>' +
                '<div class="dft-sample-meta">' +
                    '<span class="status-chip">DFT ' + group.entries.length + ' 条</span>' +
                    '<span class="status-chip ' + (readyCount ? 'ok' : 'meta') + '">可导出 ' + readyCount + '</span>' +
                    (group.meta.catalystSampleId ? '<span class="status-chip none">基础信息已关联</span>' : '<span class="status-chip meta">基础信息待补</span>') +
                '</div>' +
            '</div></summary>' +
            '<div class="dft-sample-group-body">' +
                renderDftCatalystBaseInfo(group, catalystSample, items, catalystSamplesById) +
                body +
            '</div>' +
        '</details>';
    }).join("");
}

function dftGroupRebindForm(editorKey) {
    const escaped = window.CSS && typeof window.CSS.escape === "function"
        ? window.CSS.escape(String(editorKey || ""))
        : String(editorKey || "").replace(/["\\]/g, "\\$&");
    return document.querySelector('.dft-group-rebind-form[data-editor-key="' + escaped + '"]');
}

function toggleDftGroupRebindEditor(editorKey) {
    const form = dftGroupRebindForm(editorKey);
    if (!form) {
        showToast("当前整组重新关联表单不可用。", "error");
        return;
    }
    form.hidden = !form.hidden;
    if (!form.hidden) {
        const basicInfoCard = form.closest("details.dft-catalyst-base-info");
        if (basicInfoCard) basicInfoCard.open = true;
    }
}

function updateDftGroupRebindTarget(editorKey) {
    const form = dftGroupRebindForm(editorKey);
    if (!form) return;
    const select = form.querySelector('[data-field="target_sample_id"]');
    const fullId = form.querySelector('[data-role="selected-target-id"]');
    if (fullId) fullId.textContent = "完整 UUID：" + (select && select.value ? select.value : "未选择");
}

async function submitDftGroupRebind(editorKey) {
    const form = dftGroupRebindForm(editorKey);
    if (!form || !state.selectedPaperId) {
        showToast("当前文献或整组重新关联表单不可用。", "error");
        return;
    }
    const targetSelect = form.querySelector('[data-field="target_sample_id"]');
    const reasonInput = form.querySelector('[data-field="reason"]');
    const errorBox = form.querySelector('[data-role="rebind-error"]');
    const submitButton = form.querySelector('[data-role="rebind-submit"]');
    const targetSampleId = String(targetSelect && targetSelect.value || "").trim();
    const reason = String(reasonInput && reasonInput.value || "").trim();
    const resultIds = String(form.dataset.dftResultIds || "").split(",").map(function(value) {
        return value.trim();
    }).filter(Boolean);
    if (!targetSampleId) {
        showToast("请选择目标催化剂样本。", "error");
        return;
    }
    if (!reason) {
        showToast("请填写整组重新关联的修改原因。", "error");
        return;
    }
    if (!resultIds.length) {
        showToast("当前来源样本没有可改绑的 DFT 数据。", "error");
        return;
    }
    if (errorBox) {
        errorBox.hidden = true;
        errorBox.textContent = "";
    }
    if (submitButton) submitButton.disabled = true;
    try {
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            "/catalyst-samples/" + encodeURIComponent(form.dataset.sourceSampleId) +
            "/rebind-dft-results",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    target_sample_id: targetSampleId,
                    dft_result_ids: resultIds,
                    expected_result_count: resultIds.length,
                    confirm_rebind: true,
                    reason: reason,
                    reviewer: "literature_library_user",
                }),
            }
        );
        showToast("整组 DFT 数据已重新关联，并已进入待复核流程。", "success");
        await refreshSelectedPaperDetail({
            reason: "rebind_dft_result_group",
            mode: "dft",
            forceRefresh: true,
            invalidateCache: true,
        });
    } catch (error) {
        const message = error && error.message ? error.message : String(error || "未知错误");
        if (errorBox) {
            errorBox.textContent = message;
            errorBox.hidden = false;
        }
        showToast("整组重新关联失败：" + message, "error");
    } finally {
        if (submitButton && document.body.contains(submitButton)) submitButton.disabled = false;
    }
}

window.shortCatalystSampleId = shortCatalystSampleId;
window.catalystSampleSelectLabel = catalystSampleSelectLabel;
window.toggleDftGroupRebindEditor = toggleDftGroupRebindEditor;
window.updateDftGroupRebindTarget = updateDftGroupRebindTarget;
window.submitDftGroupRebind = submitDftGroupRebind;

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
