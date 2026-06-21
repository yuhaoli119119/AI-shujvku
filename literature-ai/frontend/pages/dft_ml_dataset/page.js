(function setupDftMlDatasetPage(global) {
    const API_URL = "/api/papers/export/dft-dataset";
    const LIBRARIES_URL = "/api/libraries";
    const DATASET_SCHEMA = "dft_results_ml_v2";
    const CURRENT_LIBRARY_STORAGE_KEY = "litai_current_library";
    const EXPORTS_DISABLED_MESSAGE = "当前服务器策略已关闭导出接口；DFT ML 数据集页面暂不可用。如需恢复，请由 Owner 在设置中显式开启导出。";
    const state = {
        payload: null,
        filteredRecords: [],
        filteredLmRecords: [],
        libraries: [],
        expandedRecordIds: new Set(),
        exportsPolicyDisabled: false,
        serverFilters: {
            library_name: "",
            year_min: "",
            year_max: "",
        },
        clientFilters: {
            readiness: "all",
            canonical_property_type: "",
            property_subtype: "",
            canonical_adsorbate: "",
            ml_blocker: "",
            setting_link_status: "",
            descriptor: "all",
        },
    };

    const utils = {
        buildSummary,
        buildMlReadyCsv,
        deriveFilterOptions,
        filterDataset,
        getDbandCenterEntry,
        getPreferredMlSetting,
        getRecordTarget,
        recordMatchesFilters,
        summarizeVisibleCounts,
    };
    global.DFTMLDatasetUtils = utils;

    const PROPERTY_LABELS = {
        adsorption_energy: "吸附能",
        reaction_barrier: "反应势垒",
        d_band_center: "d 带中心",
        band_gap: "带隙",
        gibbs_free_energy_change: "吉布斯自由能变化",
        permeance: "渗透率",
    };

    const SETTING_STATUS_LABELS = {
        clear_primary: "已明确主 setting",
        ambiguous: "存在歧义",
        missing: "缺少结果级 setting",
    };

    const LOCATOR_STATUS_LABELS = {
        exact_page: "精确页码",
        text_only: "仅文本定位",
        weak: "弱定位",
        missing: "缺少定位",
    };

    const REVIEW_GATE_LABELS = {
        safe_verified: "安全通过",
        verified: "已核验",
        blocked: "已阻止",
    };

    const BLOCKER_LABELS = {
        ambiguous_result_setting_link: "结果级 setting 绑定存在歧义",
        missing_result_setting_link: "缺少结果级 setting 绑定",
        energy_basis_requires_explicit_modeling: "能量单位带基准限定，需显式建模",
        descriptor_instance_ambiguous: "descriptor 实例范围不明确",
        missing_canonical_adsorbate: "缺少规范吸附物",
        missing_normalized_value: "缺少归一化数值",
        missing_numeric_value: "缺少数值",
        unrecognized_energy_unit: "能量单位无法识别",
    };

    function qs(id) {
        return document.getElementById(id);
    }

    function readSettingsToken() {
        return sessionStorage.getItem("litai-settings-token");
    }

    async function fetchJSON(url, options) {
        const requestOptions = options || {};
        requestOptions.headers = requestOptions.headers || {};
        const token = readSettingsToken();
        if (token) {
            requestOptions.headers["X-Settings-Token"] = token;
        }
        const response = await fetch(url, requestOptions);
        const text = await response.text();
        let data = null;
        try {
            data = text ? JSON.parse(text) : null;
        } catch (_) {
            data = null;
        }
        if (!response.ok) {
            const detail = data && data.detail ? data.detail : (text || ("HTTP " + response.status));
            throw new Error(detail);
        }
        return data;
    }

    function getStoredLibraryName() {
        try {
            return global.localStorage.getItem(CURRENT_LIBRARY_STORAGE_KEY) || "";
        } catch (_) {
            return "";
        }
    }

    function rememberLibraryName(name) {
        try {
            if (name) {
                global.localStorage.setItem(CURRENT_LIBRARY_STORAGE_KEY, name);
            } else {
                global.localStorage.removeItem(CURRENT_LIBRARY_STORAGE_KEY);
            }
        } catch (_) {
            // localStorage can be unavailable in strict browser modes.
        }
    }

    function getQueryLibraryName() {
        try {
            return new URLSearchParams(global.location.search).get("library_name") || "";
        } catch (_) {
            return "";
        }
    }

    function showToast(message, kind) {
        const container = qs("toastContainer");
        if (!container) {
            return;
        }
        const toast = document.createElement("div");
        toast.className = kind === "error" ? "toast error" : "toast";
        toast.textContent = message;
        container.appendChild(toast);
        global.setTimeout(() => {
            toast.remove();
        }, 3200);
    }

    function setStatus(message, type) {
        const panel = qs("statusPanel");
        if (!panel) {
            return;
        }
        if (!message) {
            panel.hidden = true;
            panel.textContent = "";
            panel.className = "status-panel";
            return;
        }
        panel.hidden = false;
        panel.textContent = message;
        panel.className = "status-panel " + (type || "info");
    }

    function isExportsDisabledError(error) {
        return normalizeText(error && error.message) === "Exports are disabled by server policy";
    }

    function setExportsPolicyDisabled(disabled) {
        state.exportsPolicyDisabled = !!disabled;
        [
            "refreshButton",
            "applyServerFiltersButton",
            "exportCsvButton",
            "exportJsonButton",
        ].forEach(id => {
            const node = qs(id);
            if (node) {
                node.disabled = state.exportsPolicyDisabled;
            }
        });
    }

    function renderPolicyDisabledState() {
        state.payload = null;
        state.filteredRecords = [];
        state.filteredLmRecords = [];
        renderSummary();
        renderTable();
        qs("resultsMeta").textContent = "导出策略关闭中，当前页不加载 dft_results_ml_v2。";
        setStatus(EXPORTS_DISABLED_MESSAGE, "policy");
    }

    function formatDateTime(value) {
        if (!value) {
            return "-";
        }
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) {
            return String(value);
        }
        return parsed.toLocaleString("zh-CN", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    function formatNumber(value) {
        if (value === null || value === undefined || value === "") {
            return "-";
        }
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return String(value);
        }
        if (Number.isInteger(value)) {
            return String(value);
        }
        return value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function toPrettyJson(value) {
        return JSON.stringify(value == null ? null : value, null, 2);
    }

    function normalizeText(value) {
        return String(value == null ? "" : value).trim();
    }

    function normalizeList(values) {
        return (Array.isArray(values) ? values : [])
            .map(item => normalizeText(item))
            .filter(Boolean);
    }

    function readableLabel(rawValue, labelMap) {
        const raw = normalizeText(rawValue);
        if (!raw) {
            return "—";
        }
        const mapped = labelMap[raw];
        return mapped ? (mapped + "（" + raw + "）") : raw;
    }

    function getRecordTarget(record) {
        return record && record.target ? record.target : record && record.claim ? record.claim : {};
    }

    function getPreferredMlSetting(record) {
        return record && record.linked_dft_setting ? record.linked_dft_setting : null;
    }

    function getLinkedSettingLabel(record) {
        const linkedSetting = getPreferredMlSetting(record);
        if (!linkedSetting) {
            return record && record.setting_link_status === "ambiguous"
                ? readableLabel("ambiguous", SETTING_STATUS_LABELS)
                : "未唯一绑定";
        }
        const functional = normalizeText(linkedSetting.functional);
        const software = normalizeText(linkedSetting.software);
        const parts = [functional, software].filter(Boolean);
        return parts.length ? parts.join(" / ") : ("setting " + (linkedSetting.dft_setting_id || ""));
    }

    function getDbandCenterEntry(record) {
        const descriptor = record && record.descriptor_fields ? record.descriptor_fields.d_band_center : null;
        if (Array.isArray(descriptor)) {
            return descriptor[0] || null;
        }
        return descriptor || null;
    }

    function formatDbandCenter(record) {
        const entry = getDbandCenterEntry(record);
        if (!entry) {
            return "—";
        }
        const value = entry.value != null ? formatNumber(entry.value) : "-";
        return entry.unit ? (value + " " + entry.unit) : value;
    }

    function formatValueWithUnit(value, unit) {
        if (value == null && !unit) {
            return "—";
        }
        const number = value == null ? "-" : formatNumber(value);
        return unit ? (number + " " + unit) : number;
    }

    function buildSummary(payload) {
        const metadata = payload && payload.metadata ? payload.metadata : {};
        return {
            total_candidates: Number(metadata.total_candidates || 0),
            eligible_count: Number(metadata.eligible_count || 0),
            numeric_record_count: Number(metadata.numeric_record_count || 0),
            numeric_ml_ready_count: Number(metadata.numeric_ml_ready_count || 0),
            numeric_blocked_count: Number(metadata.numeric_blocked_count || 0),
            lm_record_count: Number(metadata.lm_record_count || 0),
            schema_version: metadata.schema_version || "-",
            generated_at: metadata.created_at || null,
        };
    }

    function summarizeVisibleCounts(records, lmRecords) {
        const visibleReady = records.filter(record => record.is_ml_ready).length;
        const visibleBlocked = records.length - visibleReady;
        return {
            visible_numeric_records: records.length,
            visible_ready_records: visibleReady,
            visible_blocked_records: visibleBlocked,
            visible_lm_records: lmRecords.length,
        };
    }

    function deriveFilterOptions(records) {
        const propertyTypes = new Set();
        const subtypes = new Set();
        const adsorbates = new Set();
        const blockers = new Set();
        const settingStatuses = new Set();
        records.forEach(record => {
            const target = getRecordTarget(record);
            if (target.canonical_property_type) {
                propertyTypes.add(target.canonical_property_type);
            }
            if (target.property_subtype) {
                subtypes.add(target.property_subtype);
            }
            if (target.canonical_adsorbate) {
                adsorbates.add(target.canonical_adsorbate);
            }
            normalizeList(record.ml_blockers).forEach(item => blockers.add(item));
            if (record.setting_link_status) {
                settingStatuses.add(record.setting_link_status);
            }
        });
        return {
            canonical_property_types: Array.from(propertyTypes).sort(),
            property_subtypes: Array.from(subtypes).sort(),
            canonical_adsorbates: Array.from(adsorbates).sort(),
            ml_blockers: Array.from(blockers).sort(),
            setting_link_statuses: Array.from(settingStatuses).sort(),
        };
    }

    function commonRecordFields(record) {
        const target = getRecordTarget(record);
        const paper = record && record.paper ? record.paper : {};
        return {
            canonical_property_type: normalizeText(target.canonical_property_type),
            property_subtype: normalizeText(target.property_subtype),
            canonical_adsorbate: normalizeText(target.canonical_adsorbate),
            year: paper.year == null ? null : Number(paper.year),
            setting_link_status: normalizeText(record && record.setting_link_status),
        };
    }

    function recordMatchesFilters(record, filters) {
        const fields = commonRecordFields(record);
        if (filters.canonical_property_type && fields.canonical_property_type !== filters.canonical_property_type) {
            return false;
        }
        if (filters.property_subtype && fields.property_subtype !== filters.property_subtype) {
            return false;
        }
        if (filters.canonical_adsorbate && fields.canonical_adsorbate !== filters.canonical_adsorbate) {
            return false;
        }
        if (filters.setting_link_status && fields.setting_link_status !== filters.setting_link_status) {
            return false;
        }
        if (filters.readiness === "ready" && !record.is_ml_ready) {
            return false;
        }
        if (filters.readiness === "blocked" && record.is_ml_ready) {
            return false;
        }
        if (filters.ml_blocker && !normalizeList(record.ml_blockers).includes(filters.ml_blocker)) {
            return false;
        }
        if (filters.descriptor === "with_d_band_center" && !getDbandCenterEntry(record)) {
            return false;
        }
        if (filters.descriptor === "without_d_band_center" && getDbandCenterEntry(record)) {
            return false;
        }
        return true;
    }

    function lmRecordMatchesFilters(record, filters) {
        const fields = commonRecordFields(record);
        if (filters.canonical_property_type && fields.canonical_property_type !== filters.canonical_property_type) {
            return false;
        }
        if (filters.property_subtype && fields.property_subtype !== filters.property_subtype) {
            return false;
        }
        if (filters.canonical_adsorbate && fields.canonical_adsorbate !== filters.canonical_adsorbate) {
            return false;
        }
        if (filters.setting_link_status && fields.setting_link_status !== filters.setting_link_status) {
            return false;
        }
        if (filters.readiness !== "all" || filters.ml_blocker || filters.descriptor !== "all") {
            return false;
        }
        return true;
    }

    function filterDataset(payload, filters) {
        const allRecords = payload && Array.isArray(payload.records) ? payload.records : [];
        const allLmRecords = payload && Array.isArray(payload.lm_records) ? payload.lm_records : [];
        return {
            records: allRecords.filter(record => recordMatchesFilters(record, filters)),
            lm_records: allLmRecords.filter(record => lmRecordMatchesFilters(record, filters)),
        };
    }

    function csvEscape(value) {
        const text = String(value == null ? "" : value);
        if (/[",\n]/.test(text)) {
            return "\"" + text.replace(/"/g, "\"\"") + "\"";
        }
        return text;
    }

    function buildMlReadyCsv(records) {
        const readyRecords = (Array.isArray(records) ? records : []).filter(record => record && record.is_ml_ready);
        const headers = [
            "record_id",
            "canonical_property_type",
            "property_subtype",
            "canonical_adsorbate",
            "normalized_value",
            "normalized_unit",
            "raw_value",
            "raw_unit",
            "d_band_center",
            "functional",
            "software",
            "catalyst_name",
            "paper_title",
            "year",
        ];
        const lines = [headers.join(",")];
        readyRecords.forEach(record => {
            const target = getRecordTarget(record);
            const dBandCenter = getDbandCenterEntry(record);
            const linkedSetting = getPreferredMlSetting(record) || {};
            const row = [
                record.record_id,
                target.canonical_property_type || "",
                target.property_subtype || "",
                target.canonical_adsorbate || "",
                target.normalized_value == null ? "" : target.normalized_value,
                target.normalized_unit || "",
                target.value == null ? "" : target.value,
                target.unit || "",
                dBandCenter && dBandCenter.value != null ? dBandCenter.value : "",
                linkedSetting.functional || "",
                linkedSetting.software || "",
                record.catalyst && record.catalyst.name ? record.catalyst.name : "",
                record.paper && record.paper.title ? record.paper.title : "",
                record.paper && record.paper.year != null ? record.paper.year : "",
            ].map(csvEscape);
            lines.push(row.join(","));
        });
        return lines.join("\n");
    }

    function downloadText(filename, content, mimeType) {
        const blob = new Blob([content], { type: mimeType });
        const url = global.URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        global.URL.revokeObjectURL(url);
    }

    function setCardValue(id, value) {
        const el = qs(id);
        if (el) {
            el.textContent = value == null ? "-" : String(value);
        }
    }

    function renderSummary() {
        const summary = buildSummary(state.payload);
        setCardValue("statTotalCandidates", summary.total_candidates);
        setCardValue("statEligibleCount", summary.eligible_count);
        setCardValue("statNumericRecordCount", summary.numeric_record_count);
        setCardValue("statNumericReadyCount", summary.numeric_ml_ready_count);
        setCardValue("statNumericBlockedCount", summary.numeric_blocked_count);
        setCardValue("statLmCount", summary.lm_record_count);
        qs("schemaVersionBadge").textContent = "契约版本: " + summary.schema_version;
        qs("generatedAtBadge").textContent = "生成时间: " + formatDateTime(summary.generated_at);
        const libraryLabel = state.serverFilters.library_name || "全部";
        qs("libraryBadge").textContent = "文献库: " + libraryLabel;
    }

    function populateSelect(selectId, values, placeholder, labelFormatter) {
        const select = qs(selectId);
        if (!select) {
            return;
        }
        const current = select.value;
        const options = ['<option value="">' + escapeHtml(placeholder || "全部") + "</option>"];
        values.forEach(value => {
            const label = labelFormatter ? labelFormatter(value) : value;
            options.push('<option value="' + escapeHtml(value) + '">' + escapeHtml(label) + "</option>");
        });
        select.innerHTML = options.join("");
        if (values.includes(current)) {
            select.value = current;
        }
    }

    function renderFilterOptions() {
        const records = state.payload && Array.isArray(state.payload.records) ? state.payload.records : [];
        const options = deriveFilterOptions(records);
        populateSelect("propertyTypeFilter", options.canonical_property_types, "全部", value => readableLabel(value, PROPERTY_LABELS));
        populateSelect("propertySubtypeFilter", options.property_subtypes, "全部", value => readableLabel(value, PROPERTY_LABELS));
        populateSelect("adsorbateFilter", options.canonical_adsorbates, "全部");
        populateSelect("blockerFilter", options.ml_blockers, "全部", value => readableLabel(value, BLOCKER_LABELS));
        populateSelect("settingStatusFilter", options.setting_link_statuses, "全部", value => readableLabel(value, SETTING_STATUS_LABELS));
        qs("propertyTypeFilter").value = state.clientFilters.canonical_property_type;
        qs("propertySubtypeFilter").value = state.clientFilters.property_subtype;
        qs("adsorbateFilter").value = state.clientFilters.canonical_adsorbate;
        qs("blockerFilter").value = state.clientFilters.ml_blocker;
        qs("settingStatusFilter").value = state.clientFilters.setting_link_status;
        qs("descriptorFilter").value = state.clientFilters.descriptor;
        qs("readinessFilter").value = state.clientFilters.readiness;
    }

    function renderLibraries() {
        const select = qs("libraryFilter");
        if (!select) {
            return;
        }
        const current = state.serverFilters.library_name;
        const options = ['<option value="">全部文献库</option>'];
        state.libraries.forEach(library => {
            const label = library.is_active ? (library.name + " (当前)") : library.name;
            options.push('<option value="' + escapeHtml(library.name) + '">' + escapeHtml(label) + "</option>");
        });
        select.innerHTML = options.join("");
        select.value = current || "";
    }

    function materialLabel(record) {
        if (record && record.catalyst && record.catalyst.name) {
            return record.catalyst.name;
        }
        const sampleContext = record && record.sample_context ? record.sample_context : {};
        const components = sampleContext.instance_components || {};
        return components.material_identity || components.material || components.structure_name || "—";
    }

    function readinessBadge(record) {
        return record.is_ml_ready
            ? '<span class="pill ready">可训练</span>'
            : '<span class="pill blocked">已阻止</span>';
    }

    function blockersHtml(blockers) {
        const values = normalizeList(blockers);
        if (!values.length) {
            return '<span class="pill ready">无</span>';
        }
        return '<div class="blocker-list">' + values.map(item => (
            '<span class="blocker-chip" title="' + escapeHtml(item) + '">' + escapeHtml(readableLabel(item, BLOCKER_LABELS)) + "</span>"
        )).join("") + "</div>";
    }

    function instanceComponentsHtml(components) {
        const entries = Object.entries(components || {}).filter(([, value]) => value !== null && value !== undefined && value !== "");
        if (!entries.length) {
            return '<p class="muted">无额外实例上下文。</p>';
        }
        return '<div class="pill-list">' + entries.map(([key, value]) => (
            '<span class="pill"><strong>' + escapeHtml(key) + ":</strong> " + escapeHtml(Array.isArray(value) ? value.join(", ") : value) + "</span>"
        )).join("") + "</div>";
    }

    function settingsListHtml(settings) {
        const items = Array.isArray(settings) ? settings : [];
        if (!items.length) {
            return '<p class="muted">无</p>';
        }
        return '<ul>' + items.map(setting => {
            const parts = [
                setting.dft_setting_id,
                setting.functional,
                setting.software,
            ].filter(Boolean);
            return "<li>" + escapeHtml(parts.join(" / ")) + "</li>";
        }).join("") + "</ul>";
    }

    function renderDetailRow(record) {
        const target = getRecordTarget(record);
        const sampleContext = record.sample_context || {};
        const provenance = record.provenance || {};
        const linkedSetting = getPreferredMlSetting(record);
        return (
            '<tr class="detail-row">' +
                '<td colspan="14">' +
                    '<div class="detail-panel">' +
                        '<div class="detail-grid">' +
                            '<section class="detail-card">' +
                                "<h3>证据与来源</h3>" +
                                "<p><strong>证据文本</strong></p>" +
                                "<pre>" + escapeHtml(provenance.evidence_text || "—") + "</pre>" +
                                "<p><strong>来源章节 / 图表</strong></p>" +
                                "<pre>" + escapeHtml((provenance.source_section || "—") + " / " + (provenance.source_figure || "—")) + "</pre>" +
                            "</section>" +
                            '<section class="detail-card">' +
                                "<h3>推荐训练设置</h3>" +
                                (linkedSetting
                                    ? ("<pre>" + escapeHtml(toPrettyJson(linkedSetting)) + "</pre>")
                                    : '<p class="detail-warning">当前没有唯一 linked_dft_setting，可训练判断不会回退到 paper_level_dft_settings。</p>') +
                            "</section>" +
                            '<section class="detail-card">' +
                                "<h3>Setting 审计信息</h3>" +
                                "<p><strong>绑定状态</strong>: " + escapeHtml(readableLabel(record.setting_link_status || "—", SETTING_STATUS_LABELS)) + "</p>" +
                                "<p><strong>绑定原因</strong>: " + escapeHtml(record.setting_link_reason || "—") + "</p>" +
                                "<p><strong>候选 settings</strong></p>" +
                                settingsListHtml(record.setting_link_candidates) +
                                '<p class="detail-warning">paper_level_dft_settings / dft_settings 仅供审计与兼容，不应作为唯一训练 setting。</p>' +
                                "<p><strong>论文级 settings</strong></p>" +
                                settingsListHtml(record.paper_level_dft_settings) +
                            "</section>" +
                            '<section class="detail-card">' +
                                "<h3>实例上下文</h3>" +
                                "<p><strong>实例键</strong></p>" +
                                "<pre>" + escapeHtml(sampleContext.instance_key || "—") + "</pre>" +
                                "<p><strong>实例组成</strong></p>" +
                                instanceComponentsHtml(sampleContext.instance_components) +
                            "</section>" +
                            '<section class="detail-card">' +
                                "<h3>Descriptor 字段</h3>" +
                                "<pre>" + escapeHtml(toPrettyJson(record.descriptor_fields || {})) + "</pre>" +
                            "</section>" +
                            '<section class="detail-card">' +
                                "<h3>阻止原因</h3>" +
                                blockersHtml(record.ml_blockers) +
                                "<p><strong>归一化数值 / 单位</strong></p>" +
                                "<pre>" + escapeHtml(formatValueWithUnit(target.normalized_value, target.normalized_unit)) + "</pre>" +
                            "</section>" +
                        "</div>" +
                    "</div>" +
                "</td>" +
            "</tr>"
        );
    }

    function renderTable() {
        const tbody = qs("recordsTableBody");
        if (!tbody) {
            return;
        }
        if (!state.payload) {
            tbody.innerHTML = '<tr><td colspan="14" class="empty-row">等待加载数据。</td></tr>';
            return;
        }
        if (!state.filteredRecords.length) {
            tbody.innerHTML = '<tr><td colspan="14" class="empty-row">当前筛选条件下没有数值记录。可以尝试清空 blocker 或 property 过滤器。</td></tr>';
            return;
        }
        const rows = [];
        state.filteredRecords.forEach(record => {
            const target = getRecordTarget(record);
            const paper = record.paper || {};
            const provenance = record.provenance || {};
            const isExpanded = state.expandedRecordIds.has(record.record_id);
            rows.push(
                "<tr>" +
                    "<td>" + readinessBadge(record) + "</td>" +
                    '<td class="mono">' + escapeHtml(String(record.ml_readiness_score)) + "</td>" +
                    "<td>" + blockersHtml(record.ml_blockers) + "</td>" +
                    "<td><strong>" + escapeHtml(readableLabel(target.canonical_property_type || "—", PROPERTY_LABELS)) + "</strong></td>" +
                    "<td>" + escapeHtml(readableLabel(target.property_subtype || "—", PROPERTY_LABELS)) + "</td>" +
                    "<td>" + escapeHtml(target.canonical_adsorbate || "—") + "</td>" +
                    '<td class="mono">' + escapeHtml(formatValueWithUnit(target.value, target.unit)) + "</td>" +
                    '<td class="mono">' + escapeHtml(formatValueWithUnit(target.normalized_value, target.normalized_unit)) + "</td>" +
                    "<td>" + escapeHtml(getLinkedSettingLabel(record)) + "</td>" +
                    '<td class="mono">' + escapeHtml(formatDbandCenter(record)) + "</td>" +
                    "<td>" + escapeHtml(materialLabel(record)) + "</td>" +
                    "<td><strong>" + escapeHtml(paper.title || "—") + "</strong><div class=\"muted\">" + escapeHtml(paper.year == null ? "—" : paper.year) + "</div></td>" +
                    "<td>" + escapeHtml(readableLabel(provenance.locator_status || "—", LOCATOR_STATUS_LABELS) + " / " + readableLabel(provenance.review_gate_status || "—", REVIEW_GATE_LABELS)) + "</td>" +
                    '<td><button type="button" class="btn btn-ghost toggle-detail-btn" data-record-id="' + escapeHtml(record.record_id) + '">' + (isExpanded ? "收起" : "详情") + "</button></td>" +
                "</tr>"
            );
            if (isExpanded) {
                rows.push(renderDetailRow(record));
            }
        });
        tbody.innerHTML = rows.join("");
        tbody.querySelectorAll(".toggle-detail-btn").forEach(button => {
            button.addEventListener("click", () => {
                toggleDetail(button.getAttribute("data-record-id"));
            });
        });
    }

    function toggleDetail(recordId) {
        if (!recordId) {
            return;
        }
        if (state.expandedRecordIds.has(recordId)) {
            state.expandedRecordIds.delete(recordId);
        } else {
            state.expandedRecordIds.add(recordId);
        }
        renderTable();
    }

    function readClientFiltersFromDom() {
        state.clientFilters = {
            readiness: qs("readinessFilter").value,
            canonical_property_type: qs("propertyTypeFilter").value,
            property_subtype: qs("propertySubtypeFilter").value,
            canonical_adsorbate: qs("adsorbateFilter").value,
            ml_blocker: qs("blockerFilter").value,
            setting_link_status: qs("settingStatusFilter").value,
            descriptor: qs("descriptorFilter").value,
        };
    }

    function readServerFiltersFromDom() {
        state.serverFilters = {
            library_name: normalizeText(qs("libraryFilter").value),
            year_min: normalizeText(qs("yearMinFilter").value),
            year_max: normalizeText(qs("yearMaxFilter").value),
        };
        rememberLibraryName(state.serverFilters.library_name);
    }

    function renderResultsMeta() {
        const counts = summarizeVisibleCounts(state.filteredRecords, state.filteredLmRecords);
        qs("resultsMeta").textContent =
            "当前显示 " + counts.visible_numeric_records +
            " 条数值记录，其中可训练 " + counts.visible_ready_records +
            " 条，已阻止 " + counts.visible_blocked_records +
            " 条；LM 辅助记录 " + counts.visible_lm_records + " 条。";
    }

    function applyClientFilters() {
        readClientFiltersFromDom();
        const filtered = filterDataset(state.payload, state.clientFilters);
        state.filteredRecords = filtered.records;
        state.filteredLmRecords = filtered.lm_records;
        renderResultsMeta();
        renderTable();
    }

    function validatePayload(payload) {
        if (!payload || !payload.metadata || payload.metadata.schema_version !== DATASET_SCHEMA) {
            throw new Error("导出接口未返回 dft_results_ml_v2。");
        }
        if (!Array.isArray(payload.records) || !Array.isArray(payload.lm_records)) {
            throw new Error("导出接口缺少 records / lm_records。");
        }
    }

    async function loadLibraries() {
        try {
            const libraries = await fetchJSON(LIBRARIES_URL);
            state.libraries = Array.isArray(libraries) ? libraries : [];
            const active = state.libraries.find(library => library.is_active);
            if (active && !state.serverFilters.library_name) {
                state.serverFilters.library_name = active.name;
            }
            renderLibraries();
        } catch (error) {
            showToast("加载文献库失败：" + error.message, "error");
        }
    }

    function buildDatasetUrl() {
        const params = new URLSearchParams();
        if (state.serverFilters.library_name) {
            params.set("library_name", state.serverFilters.library_name);
        }
        if (state.serverFilters.year_min) {
            params.set("year_min", state.serverFilters.year_min);
        }
        if (state.serverFilters.year_max) {
            params.set("year_max", state.serverFilters.year_max);
        }
        const query = params.toString();
        return query ? (API_URL + "?" + query) : API_URL;
    }

    async function refreshDataset() {
        if (state.exportsPolicyDisabled) {
            renderPolicyDisabledState();
            return;
        }
        readServerFiltersFromDom();
        renderLibraries();
        setStatus("正在实时读取 dft_results_ml_v2 导出数据...", "loading");
        try {
            const payload = await fetchJSON(buildDatasetUrl());
            setExportsPolicyDisabled(false);
            validatePayload(payload);
            state.payload = payload;
            state.expandedRecordIds.clear();
            renderSummary();
            renderFilterOptions();
            applyClientFilters();
            setStatus(
                "已实时加载 " + payload.records.length + " 条数值记录与 " + payload.lm_records.length +
                " 条 LM 辅助记录；推荐训练 setting 固定为 linked_dft_setting。",
                "info"
            );
        } catch (error) {
            if (isExportsDisabledError(error)) {
                setExportsPolicyDisabled(true);
                renderPolicyDisabledState();
                return;
            }
            state.payload = null;
            state.filteredRecords = [];
            state.filteredLmRecords = [];
            renderSummary();
            renderTable();
            qs("resultsMeta").textContent = "加载失败。";
            setStatus("读取 DFT ML 数据集失败：" + error.message, "error");
            showToast("读取失败：" + error.message, "error");
        }
    }

    function clearFilters() {
        state.clientFilters = {
            readiness: "all",
            canonical_property_type: "",
            property_subtype: "",
            canonical_adsorbate: "",
            ml_blocker: "",
            setting_link_status: "",
            descriptor: "all",
        };
        qs("readinessFilter").value = "all";
        qs("propertyTypeFilter").value = "";
        qs("propertySubtypeFilter").value = "";
        qs("adsorbateFilter").value = "";
        qs("blockerFilter").value = "";
        qs("settingStatusFilter").value = "";
        qs("descriptorFilter").value = "all";
        applyClientFilters();
    }

    function exportMlReadyCsv() {
        if (state.exportsPolicyDisabled) {
            showToast("当前服务器策略已关闭导出功能。", "error");
            return;
        }
        const csv = buildMlReadyCsv(state.filteredRecords);
        downloadText("dft_ml_ready_records.csv", csv, "text/csv;charset=utf-8");
        showToast("已导出当前筛选后的可训练 CSV。");
    }

    function exportV2Json() {
        if (state.exportsPolicyDisabled) {
            showToast("当前服务器策略已关闭导出功能。", "error");
            return;
        }
        if (!state.payload) {
            showToast("没有可导出的 payload。", "error");
            return;
        }
        const exportPayload = {
            metadata: state.payload.metadata,
            records: state.filteredRecords,
            lm_records: state.filteredLmRecords,
        };
        downloadText("dft_results_ml_v2.filtered.json", JSON.stringify(exportPayload, null, 2), "application/json;charset=utf-8");
        showToast("已导出当前筛选后的 V2 JSON。");
    }

    function bindEvents() {
        qs("refreshButton").addEventListener("click", refreshDataset);
        qs("applyServerFiltersButton").addEventListener("click", refreshDataset);
        qs("clearFiltersButton").addEventListener("click", clearFilters);
        qs("exportCsvButton").addEventListener("click", exportMlReadyCsv);
        qs("exportJsonButton").addEventListener("click", exportV2Json);

        [
            "readinessFilter",
            "propertyTypeFilter",
            "propertySubtypeFilter",
            "adsorbateFilter",
            "blockerFilter",
            "settingStatusFilter",
            "descriptorFilter",
        ].forEach(id => {
            qs(id).addEventListener("change", applyClientFilters);
        });
    }

    function init() {
        state.serverFilters.library_name = getQueryLibraryName() || getStoredLibraryName() || "";
        if (state.serverFilters.library_name) {
            rememberLibraryName(state.serverFilters.library_name);
        }
        TopNav.init({ currentPage: "dft-ml-dataset", mountId: "topnav-mount" });
        bindEvents();
        loadLibraries().finally(refreshDataset);
    }

    document.addEventListener("DOMContentLoaded", init);
})(window);
