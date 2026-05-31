
        const API_BASE = window.location.origin;

        function showToast(msg, type = "success") {
            const container = document.getElementById("toast-container");
            const el = document.createElement("div");
            el.className = `toast toast-${type}`;
            el.textContent = msg;
            container.appendChild(el);
            setTimeout(() => el.remove(), 3000);
        }

        function copyText(elementId) {
            const el = document.getElementById(elementId);
            const text = el.textContent || el.innerText || "";
            navigator.clipboard.writeText(text).then(() => {
                showToast("已复制到剪贴板");
            }).catch(() => {
                showToast("复制失败", "error");
            });
        }

        function showSection(name, evt) {
            document.querySelectorAll(".section-content").forEach(el => el.classList.remove("active"));
            document.querySelectorAll(".section-nav button").forEach(el => el.classList.remove("active"));
            document.getElementById("section-" + name).classList.add("active");
            if (evt && evt.target) evt.target.classList.add("active");
        }

        function showGuidePane(name, evt) {
            document.querySelectorAll(".guide-pane").forEach(el => el.classList.remove("active"));
            document.querySelectorAll(".guide-nav button").forEach(el => el.classList.remove("active"));
            document.getElementById("guide-pane-" + name).classList.add("active");
            if (evt && evt.target) evt.target.classList.add("active");
        }

        function initThemeControls() {
            const currentTheme = ThemeManager.getTheme();
            const currentMode = ThemeManager.getMode();

            document.querySelectorAll(".theme-pill").forEach(pill => {
                pill.classList.toggle("active", pill.dataset.theme === currentTheme);
                pill.addEventListener("click", () => {
                    document.querySelectorAll(".theme-pill").forEach(item => item.classList.remove("active"));
                    pill.classList.add("active");
                    ThemeManager.setTheme(pill.dataset.theme);
                });
            });

            document.querySelectorAll(".mode-btn").forEach(btn => {
                btn.classList.toggle("active", btn.dataset.mode === currentMode);
                btn.addEventListener("click", () => {
                    document.querySelectorAll(".mode-btn").forEach(item => item.classList.remove("active"));
                    btn.classList.add("active");
                    ThemeManager.setMode(btn.dataset.mode);
                });
            });
        }

        async function fetchSettingsJSON(url, options) {
            const requestOptions = options || {};
            requestOptions.headers = requestOptions.headers || {};
            const token = sessionStorage.getItem("litai-settings-token");
            if (token) requestOptions.headers["X-Settings-Token"] = token;

            const resp = await fetch(url, requestOptions);
            if (resp.status === 403) {
                showToast("无权访问该接口，请先确认是否需要管理员 Token。", "error");
                throw new Error("403 Forbidden: Admin Token Required");
            }

            const text = await resp.text();
            let data = null;
            try { data = text ? JSON.parse(text) : null; } catch (_) {}
            if (!resp.ok) {
                throw new Error((data && data.detail) || ("HTTP " + resp.status));
            }
            return data;
        }

        function toggleEmbeddingFields() {
            const provider = document.getElementById("embedding_provider").value;
            document.getElementById("embedding-api-fields").style.display =
                provider === "openai_compatible" ? "block" : "none";
        }

        function makeBadge(label, ok, detail) {
            const cls = ok ? "badge-ok" : "badge-warn";
            return `<span class="badge ${cls}">${label}${detail ? " · " + detail : ""}</span>`;
        }

        async function loadSettings() {
            try {
                document.getElementById("settings_token").value = sessionStorage.getItem("litai-settings-token") || "";
                const data = await fetchSettingsJSON(`${API_BASE}/api/settings`);

                document.getElementById("embedding_provider").value = data.embedding_provider || "deterministic";
                document.getElementById("embedding_api_base").value = data.embedding_api_base || "";
                document.getElementById("embedding_api_key").value = data.embedding_api_key || "";
                document.getElementById("embedding_model").value = data.embedding_model || "text-embedding-3-small";
                document.getElementById("embedding_dimension").value = data.embedding_dimension || "1536";

                document.getElementById("writer_backend").value = data.writer_backend || "openai_compatible";
                document.getElementById("writer_api_base").value = data.writer_api_base || "";
                document.getElementById("writer_api_key").value = data.writer_api_key || "";
                document.getElementById("writer_model").value = data.writer_model || "deepseek-chat";

                document.getElementById("mcp_api_keys").value = data.mcp_api_keys || "";
                toggleEmbeddingFields();
            } catch (e) {
                showToast("加载配置失败: " + e.message, "error");
            }
        }

        async function loadStatus() {
            try {
                const data = await fetchSettingsJSON(`${API_BASE}/api/settings/status`);
                const writerMissing = data.writer && data.writer.missing && data.writer.missing.length
                    ? "缺少 " + data.writer.missing.join(" / ")
                    : "";
                const writerDetail = data.writer && data.writer.configured
                    ? `${data.writer.backend} / ${data.writer.model}`
                    : ((data.writer && data.writer.message) || "Writer LLM 尚未配置完整") + (writerMissing ? " · " + writerMissing : "");

                const parser = data.internal_parser || data.writer || {};
                const parserMissing = parser.missing && parser.missing.length
                    ? "缺少 " + parser.missing.join(" / ")
                    : "";
                const parserDetail = parser.configured
                    ? `复用 Writer LLM / ${parser.model}`
                    : (parser.message || "内部解析配置未完成") + (parserMissing ? " · " + parserMissing : "");

                document.getElementById("status-content").innerHTML = [
                    makeBadge("Embedding", data.embedding && data.embedding.configured, `${data.embedding.provider} / ${data.embedding.model}`),
                    makeBadge("Writer", data.writer && data.writer.configured, writerDetail),
                    makeBadge("内部解析", parser.configured, parserDetail),
                    makeBadge("MCP", data.mcp && data.mcp.has_keys, data.mcp && data.mcp.enabled ? "已启用" : "未启用")
                ].join("");
            } catch (e) {
                document.getElementById("status-content").innerHTML = '<span class="badge badge-err">无法获取状态</span>';
            }
        }

        async function saveSettings() {
            const tokenVal = document.getElementById("settings_token").value.trim();
            sessionStorage.setItem("litai-settings-token", tokenVal);

            const settings = [
                { key: "embedding_provider", value: document.getElementById("embedding_provider").value },
                { key: "embedding_api_base", value: document.getElementById("embedding_api_base").value || null },
                { key: "embedding_api_key", value: document.getElementById("embedding_api_key").value || null },
                { key: "embedding_model", value: document.getElementById("embedding_model").value || null },
                { key: "embedding_dimension", value: document.getElementById("embedding_dimension").value || null },
                { key: "writer_backend", value: document.getElementById("writer_backend").value },
                { key: "writer_api_base", value: document.getElementById("writer_api_base").value || null },
                { key: "writer_api_key", value: document.getElementById("writer_api_key").value || null },
                { key: "writer_model", value: document.getElementById("writer_model").value || null },
                { key: "mcp_api_keys", value: document.getElementById("mcp_api_keys").value || null }
            ];

            try {
                const result = await fetchSettingsJSON(`${API_BASE}/api/settings`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ settings })
                });
                showToast(`配置已保存（更新 ${result.updated} 项）`);
                await loadSettings();
                await loadStatus();
            } catch (e) {
                showToast("保存失败: " + e.message, "error");
            }
        }

        async function loadIdePrompts() {
            try {
                const data = await fetchSettingsJSON(`${API_BASE}/api/settings/ide-prompts`);
                document.getElementById("prompt-text").textContent = data.suggested_prompt || "";
                document.getElementById("cursor-text").textContent = data.cursor_config_json || "";
                document.getElementById("ide-base-url").textContent = data.base_url || "-";
                document.getElementById("ide-mcp-url").textContent = data.mcp_url || "-";
                document.getElementById("ide-local-ip").textContent = data.local_ip || "-";
                document.getElementById("ide-hostname").textContent = data.hostname || "-";
                document.getElementById("ide-status").innerHTML = `<span class="badge badge-ok">服务已就绪 · ${data.base_url || "-"}</span>`;
            } catch (e) {
                document.getElementById("ide-status").innerHTML = '<span class="badge badge-err">无法获取 IDE 连接信息</span>';
            }
        }

        document.addEventListener("DOMContentLoaded", () => {
            TopNav.init({ currentPage: "settings", mountId: "topnav-mount" });
            initThemeControls();
            loadSettings();
            loadStatus();
            loadIdePrompts();
            document.getElementById("embedding_provider").addEventListener("change", toggleEmbeddingFields);
        });
    