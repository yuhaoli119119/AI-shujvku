/* ============================================================
   topnav.js - TopNav Class + ThemeManager Class
   ============================================================ */

class ThemeManager {
  static STORAGE_THEME = "litai-theme";
  static STORAGE_MODE = "litai-mode";

  static THEMES = ["material"];
  static MODES = ["light", "dark", "eyecare"];

  static DEFAULT_THEME = "material";
  static DEFAULT_MODE = "light";

  static init() {
    const theme = ThemeManager.getTheme();
    const mode = ThemeManager.getMode();
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.setAttribute("data-mode", mode);
  }

  static setMode(mode) {
    if (!ThemeManager.MODES.includes(mode)) {
      console.warn(`[ThemeManager] Unknown mode: ${mode}`);
      return;
    }
    localStorage.setItem(ThemeManager.STORAGE_MODE, mode);
    ThemeManager.apply();

    const panel = document.getElementById("theme-panel");
    if (panel) {
      ThemeManager.updateNavState(panel);
    }
  }

  static getTheme() {
    const stored = localStorage.getItem(ThemeManager.STORAGE_THEME);
    if (stored && ThemeManager.THEMES.includes(stored)) {
      return stored;
    }
    return ThemeManager.DEFAULT_THEME;
  }

  static getMode() {
    const stored = localStorage.getItem(ThemeManager.STORAGE_MODE);
    if (stored && ThemeManager.MODES.includes(stored)) {
      return stored;
    }
    return ThemeManager.DEFAULT_MODE;
  }

  static apply() {
    const theme = ThemeManager.getTheme();
    const mode = ThemeManager.getMode();
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.setAttribute("data-mode", mode);
  }

  static updateNavState(container) {
    if (!container) return;

    const currentMode = ThemeManager.getMode();


    container.querySelectorAll(".mode-btn").forEach(function(btn) {
      btn.classList.toggle("active", btn.getAttribute("data-mode") === currentMode);
    });
  }
}

// ============================================================
//   LitAIAuth - workbench session helpers (2026-09-21)
//   会话 Cookie 由后端 /api/auth/login 下发（HttpOnly），前端只读取
//   可读的 CSRF Cookie 并在同源写请求上带 X-CSRF-Token（双提交防护）。
//   installInterceptor() 会为全站 fetch / XMLHttpRequest 自动补这个头，
//   因此各业务页面无需改动。
// ============================================================
class LitAIAuth {
  static SESSION_COOKIE = "litai_session";
  static CSRF_COOKIE = "litai_csrf";
  static CSRF_HEADER = "X-CSRF-Token";
  static LOGIN_URL = "/login";

  static csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)litai_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  static isSameOrigin(url) {
    try {
      return new URL(url, location.href).origin === location.origin;
    } catch (error) {
      return false;
    }
  }

  static needsCsrf(method, url) {
    const verb = String(method || "GET").toUpperCase();
    return verb !== "GET" && verb !== "HEAD" && verb !== "OPTIONS" && LitAIAuth.isSameOrigin(url);
  }

  static redirectToLogin(reason) {
    try {
      if (reason) sessionStorage.setItem("litai.auth.reason", reason);
    } catch (error) { /* private mode: ignore */ }
    if (location.pathname !== LitAIAuth.LOGIN_URL) location.assign(LitAIAuth.LOGIN_URL);
  }

  static installInterceptor() {
    if (window.__litaiAuthInterceptor) return;
    window.__litaiAuthInterceptor = true;

    if (typeof window.fetch === "function") {
      const nativeFetch = window.fetch.bind(window);
      window.fetch = function(input, init) {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        const method = (init && init.method) || (input && input.method) || "GET";
        if (LitAIAuth.needsCsrf(method, url)) {
          const token = LitAIAuth.csrfToken();
          if (token) {
            const options = Object.assign({}, init || {});
            const headers = new Headers(options.headers || (input && input.headers) || undefined);
            if (!headers.has(LitAIAuth.CSRF_HEADER)) headers.set(LitAIAuth.CSRF_HEADER, token);
            options.headers = headers;
            return nativeFetch(input, options);
          }
        }
        return nativeFetch(input, init);
      };
    }

    if (typeof XMLHttpRequest === "function") {
      const nativeOpen = XMLHttpRequest.prototype.open;
      const nativeSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        this.__litaiAuthMethod = method;
        this.__litaiAuthUrl = url;
        return nativeOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        if (LitAIAuth.needsCsrf(this.__litaiAuthMethod, this.__litaiAuthUrl)) {
          const token = LitAIAuth.csrfToken();
          if (token) {
            try {
              this.setRequestHeader(LitAIAuth.CSRF_HEADER, token);
            } catch (error) { /* header already sent: ignore */ }
          }
        }
        return nativeSend.apply(this, arguments);
      };
    }
  }

  static async me() {
    const response = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!response.ok) return null;
    return response.json();
  }

  static async logout() {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": LitAIAuth.csrfToken() },
        body: "{}",
      });
    } catch (error) { /* 网络故障也返回登录页，服务端会话由后端过期兜底 */ }
    location.assign(LitAIAuth.LOGIN_URL);
  }
}

class TopNav {

  static NAV_ITEMS = [
    { id: "dashboard", label: "工作台", href: "../dashboard/index.html" },
    { id: "literature", label: "文献库", href: "../literature_library/index.html" },
    { id: "review-center", label: "审核中心", href: "../review_center/index.html" },
    { id: "dft-database", label: "DFT 数据库", href: "../dft_database/index.html" },
    { id: "visuals", label: "数据分析", href: "../visuals/index.html" },
    { id: "settings", label: "设置", href: "../settings/index.html" },
  ];

  static init(config) {
    const currentPage = config.currentPage || "";
    const mountId = config.mountId || "topnav-mount";
    const mountEl = document.getElementById(mountId);

    if (!mountEl) {
      console.warn(`[TopNav] Mount point #${mountId} not found`);
      return;
    }

    mountEl.innerHTML = TopNav.render(currentPage);

    const existingPanel = document.getElementById("theme-panel");
    if (!existingPanel) {
      const panelContainer = document.createElement("div");
      panelContainer.innerHTML = TopNav.renderThemePanel();
      document.body.appendChild(panelContainer.firstElementChild);
    }

    const panel = document.getElementById("theme-panel");
    if (panel) {
      ThemeManager.updateNavState(panel);
    }

    TopNav._bindEvents();
  }

  static render(currentPage) {
    const items = TopNav.NAV_ITEMS.map(function(item) {
      const isActive = item.id === currentPage ? " active" : "";
      return `<a class="topnav-item${isActive}" href="${item.href}">${item.label}</a>`;
    }).join("");

    return (
      '<nav class="topnav" id="topnav">' +
        '<span class="topnav-brand" aria-label="LitAI">LitAI</span>' +
        '<div class="topnav-items">' + items + "</div>" +
        '<button class="topnav-session-btn" id="session-logout-btn" type="button" title="退出登录" aria-label="退出登录">退出登录</button>' +
        '<button class="topnav-theme-btn" id="theme-toggle-btn" title="显示模式" aria-label="显示模式">' +
          '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
            '<path d="M12 22C6.49 22 2 17.51 2 12S6.49 2 12 2s10 4.04 10 9c0 3.31-2.69 6-6 6h-1.77c-.28 0-.5.22-.5.5 0 .12.05.23.13.33.41.47.64 1.06.64 1.67A2.5 2.5 0 0 1 12 22zm0-18c-4.41 0-8 3.59-8 8s3.59 8 8 8c.28 0 .5-.22.5-.5a.54.54 0 0 0-.14-.35c-.41-.46-.63-1.05-.63-1.65a2.5 2.5 0 0 1 2.5-2.5H16c2.21 0 4-1.79 4-4 0-3.86-3.59-7-8-7z"/>' +
            '<circle cx="6.5" cy="11.5" r="1.5"/>' +
            '<circle cx="9.5" cy="7.5" r="1.5"/>' +
            '<circle cx="14.5" cy="7.5" r="1.5"/>' +
            '<circle cx="17.5" cy="11.5" r="1.5"/>' +
          "</svg>" +
        "</button>" +
      "</nav>"
    );
  }

  static renderThemePanel() {
    const modeLabels = {
      light: "浅色",
      dark: "深色",
      eyecare: "护眼",
    };

    const modeButtons = ThemeManager.MODES.map(function(mode) {
      return `<button class="mode-btn" data-mode="${mode}">${modeLabels[mode] || mode}</button>`;
    }).join("");

    return (
      '<div class="theme-panel" id="theme-panel">' +
        '<div class="theme-panel-section">' +
          '<div class="mode-toggle">' + modeButtons + "</div>" +
        "</div>" +
      "</div>"
    );
  }

  static toggleThemePanel() {
    const panel = document.getElementById("theme-panel");
    if (!panel) return;

    panel.classList.toggle("open");
    if (panel.classList.contains("open")) {
      ThemeManager.updateNavState(panel);
    }
  }

  static _bindEvents() {
    const logoutBtn = document.getElementById("session-logout-btn");
    if (logoutBtn && !logoutBtn.dataset.bound) {
      logoutBtn.dataset.bound = "true";
      logoutBtn.addEventListener("click", function(event) {
        event.preventDefault();
        event.stopPropagation();
        LitAIAuth.logout();
      });
    }

    const themeToggleBtn = document.getElementById("theme-toggle-btn");
    if (themeToggleBtn && !themeToggleBtn.dataset.bound) {
      themeToggleBtn.dataset.bound = "true";
      themeToggleBtn.addEventListener("click", function(event) {
        event.stopPropagation();
        TopNav.toggleThemePanel();
      });
    }

    const panel = document.getElementById("theme-panel");
    if (panel && !panel.dataset.bound) {
      panel.dataset.bound = "true";


      panel.querySelectorAll(".mode-btn").forEach(function(btn) {
        btn.addEventListener("click", function() {
          ThemeManager.setMode(this.getAttribute("data-mode"));
        });
      });

      panel.addEventListener("click", function(event) {
        event.stopPropagation();
      });
    }

    if (!document.body.dataset.topnavBound) {
      document.body.dataset.topnavBound = "true";

      document.addEventListener("click", function(event) {
        const currentPanel = document.getElementById("theme-panel");
        const toggleBtn = document.getElementById("theme-toggle-btn");
        if (
          currentPanel &&
          currentPanel.classList.contains("open") &&
          !currentPanel.contains(event.target) &&
          toggleBtn &&
          !toggleBtn.contains(event.target)
        ) {
          currentPanel.classList.remove("open");
        }
      });

      document.addEventListener("keydown", function(event) {
        if (event.key !== "Escape") return;
        const currentPanel = document.getElementById("theme-panel");
        if (currentPanel && currentPanel.classList.contains("open")) {
          currentPanel.classList.remove("open");
        }
      });
    }
  }
}

// 尽早安装 CSRF 拦截器（topnav.js 在每个工作台页面的 <head> 中同步加载）。
LitAIAuth.installInterceptor();
