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
        updateCatalystNameChangeState(editorKey);
    }
}

function catalystBasicInfoDftResultIds(form) {
    return String(form && form.dataset.dftResultIds || "")
        .split(",")
        .map(function(item) { return item.trim(); })
        .filter(Boolean);
}

function setCatalystBasicInfoError(form, message) {
    const errorBox = form && form.querySelector('[data-role="basic-info-error"]');
    if (!errorBox) return;
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
}

function updateCatalystNameChangeState(editorKey) {
    const form = document.querySelector(catalystBasicInfoFormSelector(editorKey));
    if (!form) return;
    const confirmation = form.querySelector('[data-role="name-change-confirmation"]');
    if (!confirmation) return;
    const originalName = String(form.dataset.originalName || "").trim();
    const currentName = catalystBasicInfoFormValue(form, "name");
    const resultIds = catalystBasicInfoDftResultIds(form);
    const needsProtectedChange = form.dataset.mode !== "create" && currentName !== originalName && resultIds.length > 0;
    confirmation.hidden = !needsProtectedChange;
    const countNode = confirmation.querySelector('[data-role="affected-dft-count"]');
    if (countNode) countNode.textContent = String(resultIds.length);
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
    const originalName = String(form.dataset.originalName || "").trim();
    const currentName = catalystBasicInfoFormValue(form, "name");
    const affectedDftResultIds = catalystBasicInfoDftResultIds(form);
    const protectedNameChange = !createMode && currentName !== originalName && affectedDftResultIds.length > 0;
    setCatalystBasicInfoError(form, "");
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
        payload.dft_result_ids = affectedDftResultIds;
        if (!payload.dft_result_ids.length) {
            showToast("当前框内没有可关联的 DFT 数据。", "error");
            return;
        }
    }
    if (protectedNameChange) {
        const reason = catalystBasicInfoFormValue(form, "name_change_reason");
        if (!reason) {
            const message = "请填写催化剂名称修改原因。";
            setCatalystBasicInfoError(form, message);
            showToast(message, "error");
            return;
        }
        const confirmed = window.confirm(
            "请确认仍是同一个催化剂样本，本次仅修正名称。\n" +
            "将同步更新 " + affectedDftResultIds.length + " 条 DFT 数据的 Identity v2，" +
            "原核验状态和可导出状态保持不变。\n" +
            "如果数据实际属于另一个催化剂，请取消并使用‘整组重新关联’。"
        );
        if (!confirmed) return;
        payload.confirm_name_change_with_dft = true;
        payload.name_change_reason = reason;
        payload.expected_current_name = originalName;
        payload.affected_dft_result_ids = affectedDftResultIds;
        payload.expected_dft_result_count = affectedDftResultIds.length;
    }
    try {
        showToast(createMode ? "正在创建并关联催化剂基础信息..." : "正在保存催化剂基础信息...", "info");
        const result = await fetchJSON(
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
        const nameChange = result && result.name_change;
        const successMessage = protectedNameChange && nameChange && nameChange.requires_reverification === false
            ? "催化剂名称及 Identity v2 已更新，原核验状态保持不变。"
            : (createMode ? "催化剂基础信息已创建并关联。" : "催化剂基础信息已保存并标准化。");
        showToast(successMessage, "success");
        await refreshSelectedPaperDetail({
            reason: "update_catalyst_basic_info",
            mode: "dft",
            forceRefresh: true,
            invalidateCache: true,
        });
    } catch (error) {
        const message = error && error.message ? error.message : String(error || "未知错误");
        setCatalystBasicInfoError(form, message);
        showToast("保存催化剂基础信息失败：" + message, "error");
    }
}

window.parseCatalystMetalCenters = parseCatalystMetalCenters;
window.toggleCatalystBasicInfoEditor = toggleCatalystBasicInfoEditor;
window.updateCatalystNameChangeState = updateCatalystNameChangeState;
window.saveCatalystBasicInfo = saveCatalystBasicInfo;
