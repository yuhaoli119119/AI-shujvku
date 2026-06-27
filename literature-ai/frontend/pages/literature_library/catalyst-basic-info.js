function catalystBasicInfoCssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
        return window.CSS.escape(value);
    }
    return String(value || "").replace(/["\\]/g, "\\$&");
}

function catalystBasicInfoFormValue(form, field) {
    const node = form.querySelector('[data-field="' + field + '"]');
    return node ? String(node.value || "").trim() : "";
}

function parseCatalystMetalCenters(value) {
    const tokens = String(value || "")
        .split(/[，,;；\s]+/)
        .map(function(item) { return item.trim(); })
        .filter(Boolean);
    const values = [];
    const invalid = [];
    tokens.forEach(function(token) {
        if (!/^[A-Za-z]{1,2}$/.test(token)) {
            invalid.push(token);
            return;
        }
        const symbol = token.charAt(0).toUpperCase() + token.slice(1).toLowerCase();
        if (!values.includes(symbol)) values.push(symbol);
    });
    return { values: values, invalid: invalid };
}

function catalystBasicInfoFormSelector(editorKey) {
    return '.dft-basic-info-form[data-editor-key="' + catalystBasicInfoCssEscape(String(editorKey || "")) + '"]';
}

function toggleCatalystBasicInfoEditor(editorKey) {
    const form = document.querySelector(catalystBasicInfoFormSelector(editorKey));
    if (!form) {
        showToast("当前催化剂基础信息表单不可用。", "error");
        return;
    }
    const nextHidden = !form.hidden;
    form.hidden = nextHidden;
    if (!nextHidden) {
        const card = form.closest("details");
        if (card) card.open = true;
    }
}

async function saveCatalystBasicInfo(editorKey) {
    if (!state.selectedPaperId) {
        showToast("请先选择一篇文献。", "error");
        return;
    }
    const form = document.querySelector(catalystBasicInfoFormSelector(editorKey));
    if (!form) {
        showToast("当前催化剂基础信息表单不可用。", "error");
        return;
    }
    const parsedMetalCenters = parseCatalystMetalCenters(catalystBasicInfoFormValue(form, "metal_centers"));
    if (parsedMetalCenters.invalid.length) {
        showToast(
            "金属中心请使用元素符号并用逗号分隔，例如 Fe, Co。无法识别：" + parsedMetalCenters.invalid.join("、"),
            "error"
        );
        return;
    }
    const evidencePage = catalystBasicInfoFormValue(form, "evidence_page");
    const evidenceText = catalystBasicInfoFormValue(form, "evidence_text");
    const sampleId = String(form.dataset.catalystSampleId || "").trim();
    const createMode = form.dataset.mode === "create";
    const payload = {
        name: catalystBasicInfoFormValue(form, "name") || null,
        catalyst_type: catalystBasicInfoFormValue(form, "catalyst_type") || "unknown",
        metal_centers: parsedMetalCenters.values,
        coordination: catalystBasicInfoFormValue(form, "coordination") || null,
        support: catalystBasicInfoFormValue(form, "support") || "UNKNOWN",
        synthesis_method: catalystBasicInfoFormValue(form, "synthesis_method") || null,
        source: "literature_library_frontend",
        reviewer: "literature_library_user",
        evidence_payload: {
            page: evidencePage || null,
            quoted_text: evidenceText || null,
        },
    };
    if (createMode) {
        payload.dft_result_ids = String(form.dataset.dftResultIds || "")
            .split(",")
            .map(function(item) { return item.trim(); })
            .filter(Boolean);
        if (!payload.dft_result_ids.length) {
            showToast("当前框内没有可关联的 DFT 数据。", "error");
            return;
        }
    }
    try {
        showToast(createMode ? "正在创建并关联催化剂基础信息..." : "正在保存催化剂基础信息...", "info");
        await fetchJSON(
            API_BASE + "/" + encodeURIComponent(state.selectedPaperId) +
            (createMode
                ? "/catalyst-samples/from-dft-group"
                : "/catalyst-samples/" + encodeURIComponent(sampleId) + "/basic-info"),
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            }
        );
        showToast(createMode ? "催化剂基础信息已创建并关联。" : "催化剂基础信息已保存并标准化。", "success");
        await refreshSelectedPaperDetail({ reason: "update_catalyst_basic_info", mode: "full" });
    } catch (error) {
        showToast("保存催化剂基础信息失败：" + error.message, "error");
    }
}

window.parseCatalystMetalCenters = parseCatalystMetalCenters;
window.toggleCatalystBasicInfoEditor = toggleCatalystBasicInfoEditor;
window.saveCatalystBasicInfo = saveCatalystBasicInfo;
