/* ============================================================
   topnav.js - TopNav Class + ThemeManager Class
   ============================================================ */

class ThemeManager {
  static STORAGE_THEME = "litai-theme";
  static STORAGE_MODE = "litai-mode";

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

class TopNav {
  static NAV_ITEMS = [
    { id: "literature", label: "文献库", href: "../literature_library/index.html" },
    { id: "ingestion", label: "入库", href: "../ingestion/index.html" },
    { id: "dft-database", label: "DFT 数据库", href: "../dft_database/index.html" },
    { id: "visuals", label: "数据可视化", href: "../visuals/index.html" },
    { id: "review-center", label: "审核中心", href: "../review_center/index.html" },
    { id: "extraction-workflow", label: "提取向导", href: "../extraction_workflow/index.html" },
    { id: "writing-assistant", label: "写作辅助", href: "../writing_assistant/index.html" },
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
        '<button class="topnav-theme-btn" id="theme-toggle-btn" title="主题设置" aria-label="主题设置">' +
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
    const themeLabels = {
      material: "简洁",
      expressive: "实验",
      gradient: "渐变",
      impeccable: "海报",
      neumorphism: "浮雕",
      refined: "书卷",
    };

    const modeLabels = {
      light: "浅色",
      dark: "深色",
      eyecare: "护眼",
    };

    const themePills = ThemeManager.THEMES.map(function(theme) {
      return `<button class="theme-pill" data-theme="${theme}">${themeLabels[theme] || theme}</button>`;
    }).join("");

    const modeButtons = ThemeManager.MODES.map(function(mode) {
      return `<button class="mode-btn" data-mode="${mode}">${modeLabels[mode] || mode}</button>`;
    }).join("");

    return (
      '<div class="theme-panel" id="theme-panel">' +
        '<div class="theme-panel-section">' +
          '<div class="theme-panel-label">界面风格</div>' +
          '<div class="theme-pills">' + themePills + "</div>" +
        "</div>" +
        '<div class="theme-panel-section">' +
          '<div class="theme-panel-label">显示模式</div>' +
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

      panel.querySelectorAll(".theme-pill").forEach(function(pill) {
        pill.addEventListener("click", function() {
          ThemeManager.setTheme(this.getAttribute("data-theme"));
        });
      });

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
