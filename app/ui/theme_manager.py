from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    """Neumorphism + Cafe Warm 融合设计令牌"""

    name: str
    window: str          # 主背景
    window_alt: str      # 次级背景（侧边栏、标题栏）
    panel: str           # 卡片背景
    panel_alt: str       # 次级卡片 / 按压态
    border: str          # 普通分隔线
    shadow_light: str    # 高光色（Neumorphism 上/左光照）
    shadow_dark: str     # 暗部色（Neumorphism 下/右阴影）
    text: str
    text_muted: str
    accent: str
    accent_soft: str
    accent_alt: str
    danger: str
    success: str
    warning: str
    hero_start: str
    hero_end: str


# ═══════════════════════════════════════════════════════════
# 主题定义：Neumorphism 立体风格 + Cafe 暖色调
# ═══════════════════════════════════════════════════════════

THEMES = {
    "Neumorphism": ThemePalette(
        name="Neumorphism",
        # ── Surface 层级（暖灰递进）──
        window="#F2F0ED",
        window_alt="#EBE8E3",
        panel="#F0EDE8",
        panel_alt="#E8E4DE",
        # ── 边框 & Neumorphism 光照 ──
        border="#D5D0C8",
        shadow_light="#FFFFFF",
        shadow_dark="#C8C3BC",
        # ── 文字（Cafe 深棕系）──
        text="#3E2B1E",
        text_muted="#7A6B5E",
        # ── 强调色（Cafe 咖啡棕柔化）──
        accent="#6B5344",
        accent_soft="#D5C8BA",
        accent_alt="#8B7B6B",
        # ── 状态色（柔和处理）──
        danger="#B85050",
        success="#4A8C5F",
        warning="#B8892A",
        # ── Hero 渐变（自然暖绿 → 暖灰）──
        hero_start="#8B9E7A",
        hero_end="#A89888",
    ),
    "Refined": ThemePalette(
        name="Refined",
        # ── Surface 层级（Refined 纯白优雅）──
        window="#FFFFFF",
        window_alt="#F8F9FA",
        panel="#F1F5F9",
        panel_alt="#E2E8F0",
        # ── 边框 & Neumorphism 光照 ──
        border="#E2E8F0",
        shadow_light="#FFFFFF",
        shadow_dark="#CBD5E1",
        # ── 文字 ──
        text="#111827",
        text_muted="#6B7280",
        # ── 强调色（Refined 蓝紫系）──
        accent="#3B82F6",
        accent_soft="#BFDBFE",
        accent_alt="#60A5FA",
        # ── 状态色 ──
        danger="#DC2626",
        success="#16A34A",
        warning="#D97706",
        # ── Hero 渐变 ──
        hero_start="#3B82F6",
        hero_end="#8B5CF6",
    ),
    "Sleek": ThemePalette(
        name="Sleek",
        # ── Surface 层级（Sleek 冷灰极简）──
        window="#F8F9FA",
        window_alt="#F1F3F5",
        panel="#E9ECEF",
        panel_alt="#DEE2E6",
        # ── 边框 & Neumorphism 光照 ──
        border="#DEE2E6",
        shadow_light="#FFFFFF",
        shadow_dark="#CED4DA",
        # ── 文字 ──
        text="#212529",
        text_muted="#868E96",
        # ── 强调色（Sleek 冷灰蓝）──
        accent="#495057",
        accent_soft="#ADB5BD",
        accent_alt="#6C757D",
        # ── 状态色 ──
        danger="#C92A2A",
        success="#2B8A3E",
        warning="#E67700",
        # ── Hero 渐变 ──
        hero_start="#495057",
        hero_end="#ADB5BD",
    ),
    "Midnight": ThemePalette(
        name="Midnight",
        # ── Surface 层级（深空黑蓝）──
        window="#0F1117",
        window_alt="#161922",
        panel="#1A1D26",
        panel_alt="#222633",
        # ── 边框 & Neumorphism 光照（暗色反转）──
        border="#2A2F3A",
        shadow_light="#2E3542",
        shadow_dark="#0A0C10",
        # ── 文字（冷灰白）──
        text="#E2E5EC",
        text_muted="#7A8199",
        # ── 强调色（午夜蓝紫）──
        accent="#6C7BFF",
        accent_soft="#3D4566",
        accent_alt="#8C9AFF",
        # ── 状态色 ──
        danger="#FF5C5C",
        success="#4ADE80",
        warning="#FBBF24",
        # ── Hero 渐变（深蓝紫 → 紫罗兰）──
        hero_start="#4F46E5",
        hero_end="#9333EA",
    ),
}


def get_theme_names():
    return list(THEMES.keys())


def get_theme_palette(theme_name: str) -> ThemePalette:
    return THEMES.get(theme_name, THEMES["Neumorphism"])


def _is_dark(hex_color: str) -> bool:
    """根据 hex 颜色判断是否为深色背景"""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return brightness < 128


def build_stylesheet(theme_name):
    t = get_theme_palette(theme_name)
    is_dark = _is_dark(t.window)

    # ── Neumorphism 边框宏 ──
    # 凸起：上左亮，下右暗
    raised_border = f"""
        border-top: 1px solid {t.shadow_light};
        border-left: 1px solid {t.shadow_light};
        border-bottom: 1px solid {t.shadow_dark};
        border-right: 1px solid {t.shadow_dark};
    """
    # 凹陷：上左暗，下右亮
    inset_border = f"""
        border-top: 1px solid {t.shadow_dark};
        border-left: 1px solid {t.shadow_dark};
        border-bottom: 1px solid {t.shadow_light};
        border-right: 1px solid {t.shadow_light};
    """

    return f"""
/* ═══════════════════════════════════════════════
   Lit AI Collector — Neumorphism + Cafe Warm
   Design System: Neumorphism tactile + Cafe palette
   Layout: Dashboard modular grids
   ═══════════════════════════════════════════════ */

QWidget {{
    background-color: {t.window};
    color: {t.text};
    font-family: "Microsoft YaHei", "PingFang SC", "IBM Plex Sans", sans-serif;
    font-size: 14px;
}}
QLabel {{
    background-color: transparent;
}}
QMainWindow {{
    background-color: {t.window};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}

/* ── 侧边栏 ── */
#sidebar {{
    background-color: {t.window_alt};
    {inset_border}
    min-width: 0px;
    max-width: 320px;
}}
#sidebar_logo {{
    color: {t.accent};
    background-color: transparent;
    font-size: 22px;
    font-weight: 700;
    padding: 28px 24px 10px 24px;
    letter-spacing: 2px;
}}
#sidebar_subtitle {{
    color: {t.text_muted};
    background-color: transparent;
    font-size: 11px;
    padding: 0 24px 18px 24px;
}}

/* ── 导航按钮：凸起 → 按压效果 ── */
#nav_btn {{
    text-align: left;
    padding: 12px 20px;
    margin: 6px 14px;
    border-radius: 12px;
    background-color: {t.panel};
    color: {t.text_muted};
    font-size: 14px;
    font-weight: 600;
    {raised_border}
}}
#nav_btn:hover {{
    background-color: {t.panel_alt};
    color: {t.text};
}}
#nav_btn[active="true"] {{
    background-color: {t.window_alt};
    color: {t.accent};
    {inset_border}
}}
#nav_btn[collapsed="true"] {{
    text-align: center;
    padding: 0px;
    margin: 6px 10px;
}}

/* ── 顶部栏 ── */
#top_bar {{
    background-color: {t.window};
    border-bottom: 1px solid {t.border};
}}
#top_bar_title {{
    color: {t.text};
    background-color: transparent;
    font-size: 16px;
    font-weight: 700;
}}
#top_bar_meta {{
    color: {t.text_muted};
    background-color: transparent;
    font-size: 12px;
}}

/* ── 自定义标题栏 ── */
#title_bar {{
    background-color: {t.window_alt};
    border-bottom: 1px solid {t.border};
}}
#win_ctrl_btn {{
    background-color: {t.panel};
    {raised_border}
    border-radius: 8px;
    color: {t.text_muted};
    font-size: 13px;
    font-weight: 600;
    padding: 0;
}}
#win_ctrl_btn:hover {{
    background-color: {t.panel_alt};
    color: {t.text};
}}
#win_close_btn {{
    background-color: {t.panel};
    {raised_border}
    border-radius: 8px;
    color: {t.text_muted};
    font-size: 13px;
    font-weight: 600;
    padding: 0;
}}
#win_close_btn:hover {{
    background-color: {t.danger};
    color: #FFFFFF;
}}

/* ── 页面标题 ── */
#pageTitle {{
    color: {t.text};
    background-color: transparent;
    font-size: 26px;
    font-weight: 700;
}}
#pageSubtitle {{
    color: {t.text_muted};
    background-color: transparent;
    font-size: 14px;
}}

/* ── 卡片：凸起效果 ── */
#card, #tableCard, #heroCard, #metricCard, #settingCard {{
    background-color: {t.panel};
    border-radius: 18px;
    {raised_border}
}}
#heroCard {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t.hero_start}, stop:1 {t.hero_end});
    border: none;
}}
#metricCard:hover {{
    background-color: {t.panel_alt};
}}

/* ── 统计值 ── */
#metricValue {{
    color: {t.accent};
    background-color: transparent;
    font-size: 32px;
    font-weight: 700;
}}
#metricLabel {{
    color: {t.text_muted};
    background-color: transparent;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
}}

/* ── 强调标签 ── */
#accentLabel {{
    color: {t.accent};
    background-color: transparent;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
}}
#sectionTitle {{
    color: {t.text};
    background-color: transparent;
    font-size: 17px;
    font-weight: 700;
}}

/* ── 按钮 ── */
QPushButton {{
    background-color: {t.panel};
    color: {t.text};
    font-weight: 600;
    border-radius: 12px;
    padding: 10px 18px;
    {raised_border}
}}
QPushButton:hover {{
    background-color: {t.panel_alt};
}}
QPushButton:pressed {{
    background-color: {t.window_alt};
    {inset_border}
}}
QPushButton:disabled {{
    color: {t.text_muted};
    background-color: {t.panel};
}}

/* ── 主按钮：强调色 ── */
#primary_btn {{
    background-color: {t.accent};
    color: #FFFFFF;
    border: none;
    {raised_border}
}}
#primary_btn:hover {{
    background-color: {t.accent_alt};
}}
#primary_btn:pressed {{
    background-color: {t.accent};
    {inset_border}
}}

/* ── 危险按钮 ── */
#danger_btn {{
    background-color: {t.danger};
    color: #FFFFFF;
    border: none;
    {raised_border}
}}
#danger_btn:hover {{
    background-color: #C86060;
}}

/* ── 输入框：凹陷效果 ── */
QLineEdit, QTextEdit, QSpinBox, QComboBox {{
    background-color: {t.window_alt};
    color: {t.text};
    border-radius: 12px;
    padding: 10px 14px;
    {inset_border}
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {t.accent_soft};
    border: 2px solid {t.accent_soft};
}}

/* ── 表格 ── */
QTableWidget {{
    background-color: transparent;
    alternate-background-color: {t.window};
    gridline-color: {t.border};
    border: none;
    selection-background-color: {t.accent_soft};
    selection-color: {t.text};
}}
QTableWidget QPushButton {{
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 13px;
    min-height: 24px;
}}
QSplitter::handle {{
    background-color: {t.window_alt};
}}
QSplitter::handle:horizontal {{
    width: 8px;
}}
QSplitter::handle:hover {{
    background-color: {t.accent_soft};
}}
QHeaderView::section {{
    background-color: {t.panel_alt};
    color: {t.text_muted};
    border: none;
    border-bottom: 1px solid {t.border};
    padding: 10px;
    font-weight: 700;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QTableCornerButton::section {{
    background-color: {t.panel_alt};
    border: none;
}}

/* ── 进度条 ── */
QProgressBar {{
    background-color: {t.window_alt};
    {inset_border}
    border-radius: 10px;
    min-height: 14px;
    text-align: center;
    color: {t.text_muted};
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {t.accent};
    border-radius: 8px;
}}

/* ── 侧边栏切换按钮 ── */
#toggle_sidebar_btn {{
    background-color: {t.panel};
    {raised_border}
    border-radius: 10px;
    color: {t.text_muted};
    font-size: 16px;
    padding: 0;
}}
#toggle_sidebar_btn:hover {{
    color: {t.accent};
    background-color: {t.panel_alt};
}}

/* ── 底栏 ── */
#bottom_bar {{
    background-color: {t.window_alt};
    border-top: 1px solid {t.border};
}}
#statusLabel {{
    color: {t.text_muted};
    background-color: transparent;
    font-size: 11px;
}}

/* ── 滚动条 ── */
QScrollBar:vertical {{
    background-color: transparent;
    width: 8px;
    margin: 4px 0;
}}
QScrollBar::handle:vertical {{
    background-color: {t.border};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {t.text_muted};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    border: none;
}}

/* ── 复选框 ── */
QCheckBox {{
    color: {t.text};
    background-color: transparent;
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 6px;
    background-color: {t.panel};
    {raised_border}
}}
QCheckBox::indicator:checked {{
    background-color: {t.accent};
    border: 2px solid {t.accent};
}}

/* ── 对话框背景 ── */
QDialog {{
    background-color: {t.window};
}}
#centralWidget {{
    background-color: {t.window};
    {'border: 1px solid ' + t.border + ';' if not is_dark else ''}
}}
"""
