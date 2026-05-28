/* ============================================================
   topnav.js — TopNav Class + ThemeManager Class
   ============================================================ */


/* ============================================================
   ThemeManager — Static class for theme persistence & switching
   ============================================================ */
class ThemeManager {
  /** localStorage keys */
  static STORAGE_THEME = 'litai-theme';
  static STORAGE_MODE  = 'litai-mode';

  /** Supported themes and modes */
  static THEMES = ['material', 'expressive', 'gradient', 'impeccable', 'neumorphism', 'refined'];
  static MODES  = ['light', 'dark', 'eyecare'];

  /** Default values */
  static DEFAULT_THEME = 'material';
  static DEFAULT_MODE  = 'light';

  /**
   * Initialize: read localStorage → apply saved theme → prevent FOUC
   * Must be called before DOMContentLoaded (inline script in <head>)
   */
  static init() {
    const theme = ThemeManager.getTheme();
    const mode = ThemeManager.getMode();
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-mode', mode);
  }

  /**
   * Set theme style
   * @param {string} theme - Theme name (material|expressive|gradient|impeccable|neumorphism|refined)
   */
  static setTheme(theme) {
    if (!ThemeManager.THEMES.includes(theme)) {
      console.warn(`[ThemeManager] Unknown theme: ${theme}`);
      return;
    }
    localStorage.setItem(ThemeManager.STORAGE_THEME, theme);
    ThemeManager.apply();

    // Update nav panel state if panel exists
    const panel = document.getElementById('theme-panel');
    if (panel) {
      ThemeManager.updateNavState(panel);
    }
  }

  /**
   * Set display mode
   * @param {string} mode - Mode name (light|dark|eyecare)
   */
  static setMode(mode) {
    if (!ThemeManager.MODES.includes(mode)) {
      console.warn(`[ThemeManager] Unknown mode: ${mode}`);
      return;
    }
    localStorage.setItem(ThemeManager.STORAGE_MODE, mode);
    ThemeManager.apply();

    // Update nav panel state if panel exists
    const panel = document.getElementById('theme-panel');
    if (panel) {
      ThemeManager.updateNavState(panel);
    }
  }

  /**
   * Get current theme from localStorage
   * @returns {string} Current theme name
   */
  static getTheme() {
    const stored = localStorage.getItem(ThemeManager.STORAGE_THEME);
    if (stored && ThemeManager.THEMES.includes(stored)) {
      return stored;
    }
    return ThemeManager.DEFAULT_THEME;
  }

  /**
   * Get current mode from localStorage
   * @returns {string} Current mode name
   */
  static getMode() {
    const stored = localStorage.getItem(ThemeManager.STORAGE_MODE);
    if (stored && ThemeManager.MODES.includes(stored)) {
      return stored;
    }
    return ThemeManager.DEFAULT_MODE;
  }

  /**
   * Apply theme to DOM: set html element's data-theme and data-mode attributes
   */
  static apply() {
    const theme = ThemeManager.getTheme();
    const mode = ThemeManager.getMode();
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.setAttribute('data-mode', mode);
  }

  /**
   * Update active state in the navigation panel
   * @param {HTMLElement} container - The theme panel DOM element
   */
  static updateNavState(container) {
    if (!container) return;

    const currentTheme = ThemeManager.getTheme();
    const currentMode = ThemeManager.getMode();

    // Update theme pills
    container.querySelectorAll('.theme-pill').forEach(function(pill) {
      pill.classList.toggle('active', pill.getAttribute('data-theme') === currentTheme);
    });

    // Update mode buttons
    container.querySelectorAll('.mode-btn').forEach(function(btn) {
      btn.classList.toggle('active', btn.getAttribute('data-mode') === currentMode);
    });
  }
}


/* ============================================================
   TopNav — Static class for navigation injection & theme panel
   ============================================================ */
class TopNav {
  /** Navigation items configuration */
  static NAV_ITEMS = [
    { id: 'dashboard', label: '总览', href: '../dashboard/index.html' },
    { id: 'ingestion', label: '入库中心', href: '../ingestion/index.html' },
    { id: 'literature', label: '文献库', href: '../literature_library/index.html' },
    { id: 'paper-detail', label: '论文详情', href: '../paper_detail/index.html' },
    { id: 'dft-database', label: 'DFT 数据库', href: '../dft_database/index.html' },
    { id: 'mechanism', label: '机理知识', href: '../mechanism_knowledge/index.html' },
    { id: 'ai-writer', label: 'AI 写作', href: '../ai_writer/index.html' },
    { id: 'writing-assistant', label: '写作辅助', href: '../writing_assistant/index.html' },
    { id: 'settings', label: '设置', href: '../settings/index.html' },
  ];

  /**
   * Initialize navigation: inject HTML + bind events + sync theme state
   * @param {Object} config - Configuration object
   * @param {string} config.currentPage - Current page ID for highlighting
   * @param {string} [config.mountId='topnav-mount'] - Mount point element ID
   */
  static init(config) {
    const currentPage = config.currentPage || '';
    const mountId = config.mountId || 'topnav-mount';
    const mountEl = document.getElementById(mountId);

    if (!mountEl) {
      console.warn(`[TopNav] Mount point #${mountId} not found`);
      return;
    }

    // Inject navigation HTML
    mountEl.innerHTML = TopNav.render(currentPage);

    // Inject theme panel into body
    const panelContainer = document.createElement('div');
    panelContainer.innerHTML = TopNav.renderThemePanel();
    document.body.appendChild(panelContainer.firstElementChild);

    // Sync theme state in panel
    const panel = document.getElementById('theme-panel');
    if (panel) {
      ThemeManager.updateNavState(panel);
    }

    // Bind events
    TopNav._bindEvents();
  }

  /**
   * Generate navigation bar HTML
   * @param {string} currentPage - Current page ID for highlighting
   * @returns {string} Complete navigation bar HTML string
   */
  static render(currentPage) {
    const items = TopNav.NAV_ITEMS.map(function(item) {
      const isActive = item.id === currentPage ? ' active' : '';
      return '<a class="topnav-item' + isActive + '" href="' + item.href + '">' + item.label + '</a>';
    }).join('');

    return '' +
      '<nav class="topnav" id="topnav">' +
        '<span class="topnav-brand">LitAI</span>' +
        '<div class="topnav-items">' +
          items +
        '</div>' +
        '<button class="topnav-theme-btn" id="theme-toggle-btn" title="主题设置">' +
          '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
            '<path d="M12 22C6.49 22 2 17.51 2 12S6.49 2 12 2s10 4.04 10 9c0 3.31-2.69 6-6 6h-1.77c-.28 0-.5.22-.5.5 0 .12.05.23.13.33.41.47.64 1.06.64 1.67A2.5 2.5 0 0 1 12 22zm0-18c-4.41 0-8 3.59-8 8s3.59 8 8 8c.28 0 .5-.22.5-.5a.54.54 0 0 0-.14-.35c-.41-.46-.63-1.05-.63-1.65a2.5 2.5 0 0 1 2.5-2.5H16c2.21 0 4-1.79 4-4 0-3.86-3.59-7-8-7z"/>' +
            '<circle cx="6.5" cy="11.5" r="1.5"/>' +
            '<circle cx="9.5" cy="7.5" r="1.5"/>' +
            '<circle cx="14.5" cy="7.5" r="1.5"/>' +
            '<circle cx="17.5" cy="11.5" r="1.5"/>' +
          '</svg>' +
        '</button>' +
      '</nav>';
  }

  /**
   * Generate theme floating panel HTML
   * @returns {string} Theme panel HTML string
   */
  static renderThemePanel() {
    const themePills = ThemeManager.THEMES.map(function(theme) {
      const label = theme.charAt(0).toUpperCase() + theme.slice(1);
      return '<button class="theme-pill" data-theme="' + theme + '">' + label + '</button>';
    }).join('');

    const modeBtns = ThemeManager.MODES.map(function(mode) {
      const labels = { light: 'Light', dark: 'Dark', eyecare: 'Eye-care' };
      return '<button class="mode-btn" data-mode="' + mode + '">' + (labels[mode] || mode) + '</button>';
    }).join('');

    return '' +
      '<div class="theme-panel" id="theme-panel">' +
        '<div class="theme-panel-section">' +
          '<div class="theme-panel-label">设计风格</div>' +
          '<div class="theme-pills">' +
            themePills +
          '</div>' +
        '</div>' +
        '<div class="theme-panel-section">' +
          '<div class="theme-panel-label">显示模式</div>' +
          '<div class="mode-toggle">' +
            modeBtns +
          '</div>' +
        '</div>' +
      '</div>';
  }

  /**
   * Toggle theme panel visibility
   */
  static toggleThemePanel() {
    const panel = document.getElementById('theme-panel');
    if (!panel) return;

    if (panel.classList.contains('open')) {
      panel.classList.remove('open');
    } else {
      panel.classList.add('open');
      // Sync state when opening
      ThemeManager.updateNavState(panel);
    }
  }

  /**
   * Bind all interactive events
   * @private
   */
  static _bindEvents() {
    // Theme toggle button
    const themeToggleBtn = document.getElementById('theme-toggle-btn');
    if (themeToggleBtn) {
      themeToggleBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        TopNav.toggleThemePanel();
      });
    }

    // Theme pills
    const panel = document.getElementById('theme-panel');
    if (panel) {
      // Theme pill clicks
      panel.querySelectorAll('.theme-pill').forEach(function(pill) {
        pill.addEventListener('click', function() {
          const theme = this.getAttribute('data-theme');
          ThemeManager.setTheme(theme);
        });
      });

      // Mode button clicks
      panel.querySelectorAll('.mode-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
          const mode = this.getAttribute('data-mode');
          ThemeManager.setMode(mode);
        });
      });

      // Prevent panel click from closing
      panel.addEventListener('click', function(e) {
        e.stopPropagation();
      });
    }

    // Close panel on outside click
    document.addEventListener('click', function(e) {
      const panel = document.getElementById('theme-panel');
      const themeToggleBtn = document.getElementById('theme-toggle-btn');
      if (panel && panel.classList.contains('open')) {
        if (!panel.contains(e.target) && !themeToggleBtn.contains(e.target)) {
          panel.classList.remove('open');
        }
      }
    });

    // Close panel on Escape key
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        const panel = document.getElementById('theme-panel');
        if (panel && panel.classList.contains('open')) {
          panel.classList.remove('open');
        }
      }
    });
  }
}
