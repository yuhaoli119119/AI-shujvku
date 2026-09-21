/* ============================================================
   topnav.js - TopNav Class + ThemeManager Class
   ============================================================ */

class ThemeManager {
  static STORAGE_THEME = "litai-theme";
  static STORAGE_MODE = "litai-mode";

  // 与 shared/tokens.css 里真正实现的主题一一对应；
  // 设置页的 6 个主题 pill 就是这 6 个。
  static THEMES = ["material", "expressive", "gradient", "impeccable", "neumorphism", "refined"];
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

  // 设置页的「主题」pill 调用它（原先缺失，点击会抛 TypeError）。
  static setTheme(theme) {
    if (!ThemeManager.THEMES.includes(theme)) {
      console.warn(`[ThemeManager] Unknown theme: ${theme}`);
      return;
    }
    localStorage.setItem(ThemeManager.STORAGE_THEME, theme);
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

    const currentTheme = ThemeManager.getTheme();
    const currentMode = ThemeManager.getMode();

    container.querySelectorAll(".theme-pill").forEach(function(pill) {
      pill.classList.toggle("active", pill.getAttribute("data-theme") === currentTheme);
    });

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

  // 子页面 / 工具页不在主导航里，但访问时应该让用户看到"我在哪个大板块下"。
  // 键 = 该页 TopNav.init({currentPage}) 里传的值，值 = NAV_ITEMS 里的 id。
  static NAV_ALIASES = {
    "paper-detail": "literature",
    "ingestion": "literature",
    "mechanism": "literature",
    "content-knowledge": "literature",
    "ai-writer": "literature",
    "screening": "literature",
    "dft-audit-center": "review-center",
    "external": "review-center",
    "extraction-workflow": "literature",
  };

  // 主导航放不下的页面，收进"更多"菜单。
  // 注意：dft_audit_center / content_knowledge / external_analysis_workbench 现在只是历史 URL，
  // 打开后会跳回审核中心，所以不再列进菜单（直接改地址栏仍可用），避免出现"点了没去对地方"的错觉。
  static MORE_ITEMS = [
    { label: "论文入库", href: "../ingestion/index.html" },
    { label: "文献筛选", href: "../literature_screening/index.html" },
    { label: "机理知识聚合", href: "../mechanism_knowledge/index.html" },
    { label: "本地 AI 写作", href: "../ai_writer/index.html" },
    { label: "高级提取协议", href: "../extraction_workflow/index.html" },
  ];

  // 通用：给"横向可滚动的条带"加两端渐隐提示（类名 .litai-hscroll，样式在 shared/responsive.css）。
  // 滚动条被藏掉的横向列表在手机上看起来像"内容被硬切了"，这里给出可左右滑的视觉提示。
  // 顶栏 .topnav-items 用的是同一套状态类（is-scrollable / not-start / at-end）。
  static syncScrollHints(root) {
    const scope = root || document;
    const nodes = scope.querySelectorAll(".litai-hscroll");
    Array.prototype.forEach.call(nodes, function(el) {
      const scrollable = el.scrollWidth - el.clientWidth > 4;
      el.classList.toggle("is-scrollable", scrollable);
      if (!scrollable) {
        el.classList.remove("at-end", "not-start");
        return;
      }
      TopNav._syncStripFade(el);
      if (!el.dataset.hscrollBound) {
        el.dataset.hscrollBound = "true";
        el.addEventListener("scroll", function() { TopNav._syncStripFade(el); }, { passive: true });
      }
    });
  }

  static init(config) {
    const currentPage = config.currentPage || "";
    const mountId = config.mountId || "topnav-mount";
    const mountEl = document.getElementById(mountId);

    TopNav.syncScrollHints();
    if (!TopNav._hscrollResizeBound) {
      TopNav._hscrollResizeBound = true;
      window.addEventListener("resize", function() { TopNav.syncScrollHints(); });
      window.addEventListener("orientationchange", function() { TopNav.syncScrollHints(); });
    }

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
    TopNav._syncActiveItem();
    TopNav.syncScrollHints();
  }

  static activeId(currentPage) {
    const raw = String(currentPage || "");
    return TopNav.NAV_ALIASES[raw] || raw;
  }

  static render(currentPage) {
    const activeId = TopNav.activeId(currentPage);
    const items = TopNav.NAV_ITEMS.map(function(item) {
      const isActive = item.id === activeId ? " active" : "";
      return `<a class="topnav-item${isActive}" href="${item.href}">${item.label}</a>`;
    }).join("");

    const moreItems = TopNav.MORE_ITEMS.map(function(item) {
      return `<a class="topnav-more-item" role="menuitem" href="${item.href}">${item.label}</a>`;
    }).join("");

    const moreBlock = TopNav.MORE_ITEMS.length
      ? '<div class="topnav-more" id="topnav-more">' +
          '<button class="topnav-more-btn" id="topnav-more-btn" type="button" aria-haspopup="true" aria-expanded="false">更多 <span aria-hidden="true">▾</span></button>' +
          '<div class="topnav-more-panel" id="topnav-more-panel" role="menu">' + moreItems + "</div>" +
        "</div>"
      : "";

    return (
      '<nav class="topnav" id="topnav">' +
        '<a class="topnav-brand" href="../dashboard/index.html" aria-label="LitAI 工作台">LitAI</a>' +
        '<div class="topnav-items">' + items + "</div>" +
        moreBlock +
        '<button class="topnav-session-btn" id="session-logout-btn" type="button" title="退出登录" aria-label="退出登录">' +
          '<svg class="topnav-session-icon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
            '<path d="M10 17l5-5-5-5v3H3v4h7v3zm9-14h-6v2h6v14h-6v2h6a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/>' +
          '</svg>' +
          '<span class="topnav-session-label">退出登录</span>' +
        '</button>' +
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

  // 手机上主导航是横向可滑动的：把当前页签滚进可见区，否则用户看不到自己在哪一页；
  // 右侧还有内容时给一条渐隐提示，滑到底自动去掉。
  static _syncActiveItem() {
    const strip = document.querySelector(".topnav-items");
    if (!strip) return;
    const apply = function() {
      const scrollable = strip.scrollWidth - strip.clientWidth > 4;
      strip.classList.toggle("is-scrollable", scrollable);
      if (!scrollable) {
        strip.scrollLeft = 0;
        strip.classList.remove("at-end", "not-start");
        return;
      }
      const active = strip.querySelector(".topnav-item.active");
      if (active && active.offsetWidth) {
        const target = active.offsetLeft - (strip.clientWidth - active.offsetWidth) / 2;
        strip.scrollLeft = Math.max(0, Math.min(target, strip.scrollWidth - strip.clientWidth));
      }
      TopNav._syncStripFade(strip);
    };
    apply();
    if (!strip.dataset.fadeBound) {
      strip.dataset.fadeBound = "true";
      strip.addEventListener("scroll", function() { TopNav._syncStripFade(strip); }, { passive: true });
      window.addEventListener("resize", function() { apply(); });
    }
    if (window.requestAnimationFrame) requestAnimationFrame(apply);
  }

  // 被 _syncActiveItem（顶栏）与 syncScrollHints（通用条带）共用
  static _syncStripFade(strip) {
    const atEnd = strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 2;
    strip.classList.toggle("at-end", atEnd);
    strip.classList.toggle("not-start", strip.scrollLeft > 2);
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

    const moreBtn = document.getElementById("topnav-more-btn");
    if (moreBtn && !moreBtn.dataset.bound) {
      moreBtn.dataset.bound = "true";
      moreBtn.addEventListener("click", function(event) {
        event.preventDefault();
        event.stopPropagation();
        const wrap = document.getElementById("topnav-more");
        if (!wrap) return;
        const open = wrap.classList.toggle("open");
        moreBtn.setAttribute("aria-expanded", open ? "true" : "false");
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

        const moreWrap = document.getElementById("topnav-more");
        const moreBtnEl = document.getElementById("topnav-more-btn");
        if (
          moreWrap &&
          moreWrap.classList.contains("open") &&
          !moreWrap.contains(event.target)
        ) {
          moreWrap.classList.remove("open");
          if (moreBtnEl) moreBtnEl.setAttribute("aria-expanded", "false");
        }
      });

      document.addEventListener("keydown", function(event) {
        if (event.key !== "Escape") return;
        const currentPanel = document.getElementById("theme-panel");
        if (currentPanel && currentPanel.classList.contains("open")) {
          currentPanel.classList.remove("open");
        }
        const moreWrap = document.getElementById("topnav-more");
        if (moreWrap && moreWrap.classList.contains("open")) {
          moreWrap.classList.remove("open");
          const moreBtnEl = document.getElementById("topnav-more-btn");
          if (moreBtnEl) moreBtnEl.setAttribute("aria-expanded", "false");
        }
      });
    }
  }
}

// 尽早安装 CSRF 拦截器（topnav.js 在每个工作台页面的 <head> 中同步加载）。
LitAIAuth.installInterceptor();
