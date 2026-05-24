import os
import shutil
from pathlib import Path

import fitz
from PySide6.QtCore import QPoint, QPropertyAnimation, Qt, QTimer, QEasingCurve, QEvent
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from sqlmodel import func, select

from ..core.config import ConfigManager
from ..core.models import Chunk, ExtractionJob, File, Paper
from ..core.project_manager import ProjectManager
from .extraction_page import ExtractionPage
from .library_page import LibraryPage
from .project_dialog import ProjectDialog
from .search_page import SearchPage
from .theme_manager import build_stylesheet, get_theme_names
from .toast_dialog import ToastDialog


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lit AI Collector")
        self.resize(1280, 820)
        self.setMinimumSize(900, 620)
        self.pm = ProjectManager()
        self.config = ConfigManager()
        self.pm.current_config = self.config
        self.setObjectName("rootWindow")

        # 无边框
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        # 窗口状态（仅用于跟踪，实际拖拽/缩放由 nativeEvent 处理）
        self._border = 12
        self._is_maximized = False
        self._normal_geometry = None
        self._native_resize_enabled = False

        self.setup_ui()
        self._enable_native_resize_support()
        self.apply_theme(self.config.get("theme", "Neumorphism"))

    # ──────────────── 唯一的无边框窗口机制：nativeEvent (Windows 原生) ────
    # 重复的定义已删除，所有原生窗口交互判定（拖拽、缩放、双击最大化）已统一整合到文件末尾的 nativeEvent 方法中。

    def _toggle_maximize(self):
        """切换窗口的最大化/恢复状态。
        - 在进入最大化前保存当前几何信息，以便恢复时使用。
        - 切换后更新按钮文字并同步 `_is_maximized` 标记，确保 nativeEvent 的判断逻辑一致。
        """
        if self.isMaximized():
            # 恢复为普通窗口
            self.showNormal()
            if self._normal_geometry:
                # 恢复之前记录的几何尺寸（位置+大小）
                self.setGeometry(self._normal_geometry)
            self._is_maximized = False
        else:
            # 记录当前几何信息，随后最大化
            self._normal_geometry = self.geometry()
            self.showMaximized()
            self._is_maximized = True
        # 更新按钮文字，❐ 表示已最大化，□ 表示可最大化
        self.btn_max.setText("❐" if self._is_maximized else "□")

    def eventFilter(self, watched, event):
        """标题栏事件过滤：处理双击最大化"""
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self._is_maximized = self.isMaximized()
            if hasattr(self, "btn_max"):
                self.btn_max.setText("❐" if self._is_maximized else "□")
        super().changeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._enable_native_resize_support()

    def _enable_native_resize_support(self):
        if self._native_resize_enabled or os.name != "nt":
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            if not hwnd:
                return

            GWL_STYLE = -16
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            WS_THICKFRAME = 0x00040000

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            # 只加 WS_THICKFRAME 以支持原生边框缩放
            # 不加 WS_SYSMENU/WS_MAXIMIZEBOX/WS_MINIMIZEBOX，否则系统会额外绘制原生标题栏按钮，与自定义按钮重叠
            style |= WS_THICKFRAME | 0x00020000  # WS_MINIMIZEBOX
            user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
            )
            self._native_resize_enabled = True
        except Exception:
            pass

    # ───────────────────────── UI 构建 ─────────────────────────

    def setup_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 自定义标题栏 ──
        self.title_bar = QFrame()
        self.title_bar.setObjectName("title_bar")
        self.title_bar.setFixedHeight(36)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(12, 0, 8, 0)
        title_layout.setSpacing(8)

        title_icon = QLabel("◆")
        title_icon.setStyleSheet("background:transparent;color:#4dd7ff;font-size:12px;")
        title_icon.setFixedWidth(18)
        title_layout.addWidget(title_icon)

        title_label = QLabel("Lit AI Collector")
        title_label.setStyleSheet("background:transparent;color:#8da0bd;font-size:12px;font-weight:600;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        self.btn_min = QPushButton("−")
        self.btn_min.setObjectName("win_ctrl_btn")
        self.btn_min.setFixedSize(32, 24)
        self.btn_min.clicked.connect(self.showMinimized)

        self.btn_max = QPushButton("□")
        self.btn_max.setObjectName("win_ctrl_btn")
        self.btn_max.setFixedSize(32, 24)
        self.btn_max.clicked.connect(self._toggle_maximize)

        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("win_close_btn")
        self.btn_close.setFixedSize(32, 24)
        self.btn_close.clicked.connect(self.close)

        title_layout.addWidget(self.btn_min)
        title_layout.addWidget(self.btn_max)
        title_layout.addWidget(self.btn_close)
        main_layout.addWidget(self.title_bar)


        # 标题栏安装事件过滤器：双击最大化
        self.title_bar.installEventFilter(self)

        # ── 主体区域 ──
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # 侧边栏
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(0)
        self.sidebar.setMaximumWidth(320)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(4)

        self.sidebar_top = QHBoxLayout()
        self.sidebar_top.setContentsMargins(14, 16, 14, 8)
        self.sidebar_top.setSpacing(0)
        logo_label = QLabel("LIT AI")
        logo_label.setObjectName("sidebar_logo")
        self.sidebar_logo = logo_label
        
        self.btn_toggle_sidebar = QPushButton("☰")
        self.btn_toggle_sidebar.setObjectName("toggle_sidebar_btn")
        self.btn_toggle_sidebar.setFixedSize(44, 36)
        self.btn_toggle_sidebar.setToolTip("折叠 / 展开侧边栏")
        self.btn_toggle_sidebar.clicked.connect(self.toggle_sidebar)
        
        self.sidebar_top.addWidget(self.sidebar_logo)
        self.sidebar_top.addStretch()
        self.sidebar_top.addWidget(self.btn_toggle_sidebar)
        sidebar_layout.addLayout(self.sidebar_top)

        self.sidebar_subtitle = QLabel("Smart literature cockpit")
        self.sidebar_subtitle.setObjectName("sidebar_subtitle")
        sidebar_layout.addWidget(self.sidebar_subtitle)

        self.nav_btns = []
        self.nav_items_data = [
            ("🏠 控制台", "🏠", 0), 
            ("🔍 文献检索", "🔍", 1), 
            ("📚 项目文献库", "📚", 2), 
            ("🧠 AI 抽取", "🧠", 3), 
            ("⚙️ 系统设置", "⚙️", 4)
        ]
        for text, icon, index in self.nav_items_data:
            btn = QPushButton(text)
            btn.setObjectName("nav_btn")
            btn.setCheckable(True)
            btn.setProperty("full_text", text)
            btn.setProperty("icon_text", icon)
            btn.setProperty("collapsed", False)
            btn.clicked.connect(lambda checked, i=index: self.switch_page(i))
            sidebar_layout.addWidget(btn)
            self.nav_btns.append(btn)
        sidebar_layout.addStretch()

        self.sidebar_footer = QLabel("多主题 · 多源检索 · 抽取闭环")
        self.sidebar_footer.setObjectName("sidebar_subtitle")
        self.sidebar_footer.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_footer)

        # 右侧内容区
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)




        self.content_stack = QStackedWidget()
        self.page_home = QWidget()
        self.page_search = SearchPage(self.pm, self.config)
        self.page_library = LibraryPage(self.pm)
        self.page_extraction = ExtractionPage(self.pm, self.config)
        self.page_settings = QWidget()

        self.content_stack.addWidget(self._wrap_page(self.page_home))
        self.content_stack.addWidget(self._wrap_page(self.page_search))
        self.content_stack.addWidget(self._wrap_page(self.page_library))
        self.content_stack.addWidget(self._wrap_page(self.page_extraction))
        self.content_stack.addWidget(self._wrap_page(self.page_settings))

        self.page_search.paper_added.connect(self.on_data_changed)
        self.page_library.library_changed.connect(self.on_data_changed)
        self.page_extraction.data_changed.connect(self.on_data_changed)
        self.page_search.bind_import_action(self.handle_import_pdfs)

        self.init_home_page()
        self.init_settings_page()
        right_layout.addWidget(self.content_stack)
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self.sidebar)
        self.main_splitter.addWidget(right_container)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([220, 1060])
        body_layout.addWidget(self.main_splitter, stretch=1)
        main_layout.addWidget(body, stretch=1)

        # ── 底栏（贯穿整个窗口）──
        self.bottom_bar = QFrame()
        self.bottom_bar.setObjectName("bottom_bar")
        self.bottom_bar.setFixedHeight(28)
        bottom_layout = QHBoxLayout(self.bottom_bar)
        bottom_layout.setContentsMargins(16, 0, 16, 0)
        bottom_layout.setSpacing(0)
        self.status_left = QLabel("就绪")
        self.status_left.setObjectName("statusLabel")
        self.status_right = QLabel("Lit AI Collector v0.2.0")
        self.status_right.setObjectName("statusLabel")
        bottom_layout.addWidget(self.status_left)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.status_right)
        main_layout.addWidget(self.bottom_bar)

        self.setCentralWidget(central)
        self.switch_page(0)

    def make_card(self, object_name="card", shadow=True):
        card = QFrame()
        card.setObjectName(object_name)
        # 已移除 QGraphicsDropShadowEffect：setStyleSheet 时会触发大量重绘，
        # 在复杂窗口上导致 Windows 事件循环卡死。Neumorphism 立体感由边框实现即可。
        return card

    def _wrap_page(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        return scroll

    def animate_entry(self, widget, delay_ms=0, duration_ms=500):
        """淡入 + 轻微上浮入场动画"""
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        widget.setProperty("_entry_y", widget.y() + 18)

        def start_anim():
            try:
                if widget.graphicsEffect() is not effect:
                    return
            except RuntimeError:
                return
            try:
                anim = QPropertyAnimation(effect, b"opacity")
                anim.setDuration(duration_ms)
                anim.setStartValue(0.0)
                anim.setEndValue(1.0)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.finished.connect(lambda: widget.setGraphicsEffect(None))
                anim.start()
                widget._entry_anim = anim
            except RuntimeError:
                pass

        QTimer.singleShot(delay_ms, start_anim)

    def init_home_page(self):
        layout = QVBoxLayout(self.page_home)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        hero = self.make_card("heroCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(28, 28, 28, 28)
        hero_layout.setSpacing(10)

        hero_chip = QLabel("LITERATURE COMMAND")
        hero_chip.setObjectName("accentLabel")
        hero_layout.addWidget(hero_chip)

        self.lbl_home_title = QLabel("控制台")
        self.lbl_home_title.setObjectName("pageTitle")
        hero_layout.addWidget(self.lbl_home_title)

        self.lbl_home_subtitle = QLabel("创建或打开项目后，你可以在一个工作台里完成检索、入库、下载、解析和 AI 抽取。")
        self.lbl_home_subtitle.setObjectName("pageSubtitle")
        self.lbl_home_subtitle.setWordWrap(True)
        hero_layout.addWidget(self.lbl_home_subtitle)

        action_layout = QHBoxLayout()
        btn_new = QPushButton("新建项目")
        btn_new.setObjectName("primary_btn")
        btn_new.clicked.connect(self.handle_new_project)
        btn_open = QPushButton("打开项目")
        btn_open.clicked.connect(self.handle_open_project)
        action_layout.addWidget(btn_new)
        action_layout.addWidget(btn_open)
        action_layout.addStretch()
        hero_layout.addLayout(action_layout)
        layout.addWidget(hero)
        self.animate_entry(hero, delay_ms=80, duration_ms=500)

        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(16)
        self.stats_labels = {}
        for idx, (label, key) in enumerate([("文献总数", "total"), ("本地 PDF", "pdf"), ("已解析", "chunks"), ("抽取完成", "ai_done")]):
            card = self.make_card("metricCard")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(22, 22, 22, 22)
            c_layout.setSpacing(10)
            lbl = QLabel(label)
            lbl.setObjectName("metricLabel")
            c_layout.addWidget(lbl)
            val = QLabel("0")
            val.setObjectName("metricValue")
            c_layout.addWidget(val)
            self.stats_labels[key] = val
            stats_layout.addWidget(card)
            self.animate_entry(card, delay_ms=180 + idx * 100, duration_ms=450)
        layout.addLayout(stats_layout)

        insight_card = self.make_card("card")
        insight_layout = QVBoxLayout(insight_card)
        insight_layout.setContentsMargins(22, 22, 22, 22)
        insight_layout.setSpacing(8)
        insight_title = QLabel("项目状态")
        insight_title.setObjectName("sectionTitle")
        self.home_summary = QLabel("当前还没有项目，建议先创建一个主题明确的文献项目。")
        self.home_summary.setObjectName("pageSubtitle")
        self.home_summary.setWordWrap(True)
        self.activity_board = QTextEdit()
        self.activity_board.setReadOnly(True)
        self.activity_board.setFixedHeight(160)
        insight_layout.addWidget(insight_title)
        insight_layout.addWidget(self.home_summary)
        insight_layout.addWidget(self.activity_board)
        layout.addWidget(insight_card)
        self.animate_entry(insight_card, delay_ms=380, duration_ms=500)
        layout.addStretch()

    def init_settings_page(self):
        layout = QVBoxLayout(self.page_settings)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("系统设置")
        title.setObjectName("pageTitle")
        subtitle = QLabel("把设置拆成清晰的导航分区，常用项、检索源和教程分开管理。")
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.api_key_input = QLineEdit(self.config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.Password)
        
        self.base_url_input = QComboBox()
        self.base_url_input.setEditable(True)
        self.base_url_input.addItems([
            "https://api.openai.com/v1",
            "https://api.deepseek.com/v1",
            "https://api.siliconflow.cn/v1",
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        ])
        self.base_url_input.setCurrentText(self.config.get("base_url", "https://api.openai.com/v1"))
        
        self.model_input = QComboBox()
        self.model_input.setEditable(True)
        self.model_input.addItems([
            "gpt-4o-mini",
            "gpt-4o",
            "deepseek-chat",
            "deepseek-reasoner",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-2.0-flash-exp"
        ])
        self.model_input.setCurrentText(self.config.get("llm_model", "gpt-4o-mini"))
        self.proxy_input = QLineEdit(self.config.get("proxy", ""))
        self.proxy_input.setPlaceholderText("例如：http://127.0.0.1:7890")
        self.literature_ai_url_input = QLineEdit(self.config.get("literature_ai_url", "http://localhost:8000"))
        self.literature_ai_url_input.setPlaceholderText("http://localhost:8000")
        self.findpapers_email_input = QLineEdit(self.config.get("findpapers_email", ""))
        self.findpapers_email_input.setPlaceholderText("example@lab.org")
        self.findpapers_pubmed_key_input = QLineEdit(self.config.get("findpapers_pubmed_api_key", ""))
        self.findpapers_pubmed_key_input.setEchoMode(QLineEdit.Password)
        self.findpapers_openalex_key_input = QLineEdit(self.config.get("findpapers_openalex_api_key", ""))
        self.findpapers_openalex_key_input.setEchoMode(QLineEdit.Password)
        self.findpapers_semantic_key_input = QLineEdit(self.config.get("findpapers_semantic_scholar_api_key", ""))
        self.findpapers_semantic_key_input.setEchoMode(QLineEdit.Password)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(get_theme_names())
        self.theme_combo.setCurrentText(self.config.get("theme", "Neumorphism"))
        self.theme_combo.currentTextChanged.connect(self._on_theme_preview)
        self.theme_tips = QLabel(self._theme_desc(self.theme_combo.currentText()))
        self.theme_tips.setWordWrap(True)
        self.theme_tips.setObjectName("pageSubtitle")

        settings_shell = QHBoxLayout()
        settings_shell.setSpacing(16)

        nav_card = self.make_card("card")
        nav_layout = QVBoxLayout(nav_card)
        nav_layout.setContentsMargins(16, 16, 16, 16)
        nav_layout.setSpacing(10)
        nav_title = QLabel("设置导航")
        nav_title.setObjectName("sectionTitle")
        nav_layout.addWidget(nav_title)

        self.settings_nav_btns = []
        self.settings_stack = QStackedWidget()
        sections = [
            ("常规", self._build_settings_general_page()),
            ("检索网络", self._build_settings_search_page()),
            ("界面主题", self._build_settings_appearance_page()),
            ("使用教程", self._build_settings_tutorial_page()),
        ]
        for index, (label, page) in enumerate(sections):
            btn = QPushButton(label)
            btn.setObjectName("nav_btn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, i=index: self.switch_settings_section(i))
            nav_layout.addWidget(btn)
            self.settings_nav_btns.append(btn)
            self.settings_stack.addWidget(page)
        nav_layout.addStretch()

        stack_card = self.make_card("settingCard")
        stack_layout = QVBoxLayout(stack_card)
        stack_layout.setContentsMargins(24, 24, 24, 24)
        stack_layout.setSpacing(16)
        stack_layout.addWidget(self.settings_stack, stretch=1)

        btn_save = QPushButton("应用并保存设置")
        btn_save.setObjectName("primary_btn")
        btn_save.clicked.connect(self.save_settings)
        stack_layout.addWidget(btn_save)

        settings_shell.addWidget(nav_card, stretch=0)
        settings_shell.addWidget(stack_card, stretch=1)
        layout.addLayout(settings_shell, stretch=1)

        self.switch_settings_section(0)
        self.animate_entry(nav_card, delay_ms=80, duration_ms=420)
        self.animate_entry(stack_card, delay_ms=140, duration_ms=420)

    def _build_settings_section(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        header = QLabel(title)
        header.setObjectName("sectionTitle")
        desc = QLabel(subtitle)
        desc.setObjectName("pageSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(header)
        layout.addWidget(desc)
        return page, layout

    def _add_setting_row(self, layout: QVBoxLayout, label_text: str, widget: QWidget, hint: str | None = None):
        label = QLabel(label_text)
        layout.addWidget(label)
        layout.addWidget(widget)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("pageSubtitle")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)

    def _build_settings_general_page(self) -> QWidget:
        page, layout = self._build_settings_section("常规设置", "管理兼容 OpenAI 协议的模型服务和桥接服务。")
        self._add_setting_row(layout, "兼容 API Key", self.api_key_input, "可填写 OpenAI、DeepSeek 或其他兼容服务的 API Key。")
        self._add_setting_row(layout, "兼容 Base URL", self.base_url_input, "例如 OpenAI 官方地址，或 DeepSeek / 自建网关的兼容地址。")
        self._add_setting_row(layout, "抽取模型", self.model_input)
        self._add_setting_row(
            layout,
            "Literature AI 服务地址",
            self.literature_ai_url_input,
            "桌面端发送文献到 Literature AI 时会使用这个地址。",
        )
        layout.addStretch()
        return page

    def _build_settings_search_page(self) -> QWidget:
        page, layout = self._build_settings_section("检索网络", "和联网检索直接相关的基础配置。已移除必须单独配置 Key 的检索库。")
        self._add_setting_row(layout, "HTTP 代理地址", self.proxy_input)
        self._add_setting_row(
            layout,
            "findpapers Email",
            self.findpapers_email_input,
            "用于 OpenAlex / Crossref 的礼貌访问标识，可以提升稳定性。",
        )
        self._add_setting_row(layout, "PubMed API Key", self.findpapers_pubmed_key_input)
        self._add_setting_row(layout, "OpenAlex API Key", self.findpapers_openalex_key_input)
        self._add_setting_row(layout, "Semantic Scholar API Key", self.findpapers_semantic_key_input)
        layout.addStretch()
        return page

    def _build_settings_appearance_page(self) -> QWidget:
        page, layout = self._build_settings_section("界面主题", "这里专门放视觉风格，避免和网络设置混在一起。")
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("软件主题风格"))
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch()
        layout.addLayout(theme_row)
        layout.addWidget(self.theme_tips)
        layout.addStretch()
        return page

    def _build_settings_tutorial_page(self) -> QWidget:
        page, layout = self._build_settings_section("使用教程", "按下面的顺序走，检索、入库和抽取会顺很多。")
        tutorial = QTextEdit()
        tutorial.setReadOnly(True)
        tutorial.setPlainText(
            "1. 先在控制台新建或打开项目。\n"
            "2. 在文献检索页选择数据源，优先使用 OpenAlex、arXiv、PubMed、Semantic Scholar。\n"
            "3. 搜到结果后先查看题名、DOI、期刊是否正常，再加入项目或批量下载 PDF。\n"
            "4. 已有本地 PDF 可以直接从文献检索页导入，或进入项目文献库统一管理。\n"
            "5. 在 AI 抽取页运行解析与抽取，确认文献入库后的结构化结果。\n"
            "6. 若需要发送到 Literature AI，请先在常规设置里确认服务地址可用。\n\n"
            "小建议：\n"
            "- Google Scholar 和 X-MOL 更适合作为补充源，不建议单独依赖它们做大批量搜索。\n"
            "- 搜索词尽量明确到主题、材料体系、方法或题名关键词，结果会稳定很多。"
        )
        layout.addWidget(tutorial, stretch=1)
        return page

    def switch_settings_section(self, index: int):
        self.settings_stack.setCurrentIndex(index)
        for i, btn in enumerate(self.settings_nav_btns):
            btn.setProperty("active", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _theme_desc(self, theme_name: str) -> str:
        descs = {
            "Neumorphism": "Neumorphism 立体风格与 Cafe 暖色调融合设计，营造柔和、有温度的工作界面。",
            "Refined": "Refined 纯白优雅风格，蓝紫系强调色，简洁专业。",
            "Sleek": "Sleek 冷灰极简风格，低饱和度配色，专注内容本身。",
            "Midnight": "Midnight 深夜模式，深空黑蓝底色搭配蓝紫强调色，适合低光环境长时间使用。",
        }
        return descs.get(theme_name, "")


    def _on_theme_preview(self, theme_name: str):
        """仅更新提示文字，不立即应用样式，避免下拉框切换时卡死"""
        self.theme_tips.setText(self._theme_desc(theme_name))

    def save_settings(self):
        self.config.set("api_key", self.api_key_input.text())
        self.config.set("base_url", self.base_url_input.currentText().strip())
        self.config.set("llm_model", self.model_input.currentText().strip() or "gpt-4o-mini")
        self.config.set("proxy", self.proxy_input.text())
        self.config.set("literature_ai_url", self.literature_ai_url_input.text().strip().rstrip("/"))
        self.config.set("findpapers_email", self.findpapers_email_input.text().strip())
        self.config.set("findpapers_pubmed_api_key", self.findpapers_pubmed_key_input.text().strip())
        self.config.set("findpapers_openalex_api_key", self.findpapers_openalex_key_input.text().strip())
        self.config.set("findpapers_semantic_scholar_api_key", self.findpapers_semantic_key_input.text().strip())
        self.config.set("theme", self.theme_combo.currentText())
        self.apply_theme(self.theme_combo.currentText())
        # 主题应用完成后再弹非模态提示，避免阻塞事件循环
        ToastDialog.information(self, "成功", "设置已更新并即时生效。")

    def _clear_graphics_effects(self, widget):
        """递归清除所有 QGraphicsEffect (包括 shadow 和 opacity)，防止 setStyleSheet 时重绘卡死"""
        for child in widget.findChildren(QWidget):
            eff = child.graphicsEffect()
            if eff is not None:
                try:
                    child.setGraphicsEffect(None)
                    eff.deleteLater()
                except Exception:
                    pass

    def apply_theme(self, theme_name):
        """应用主题样式：清除所有残留 graphics effect → 全局样式表 → 刷新 UI"""
        app = QApplication.instance()
        if app is None:
            return
        # 清除所有残留的图形效果（包括阴影和淡入），彻底杜绝 Windows 在 app.setStyleSheet 时的重绘死锁问题
        self._clear_graphics_effects(self)
        # 使用全局样式表，绕过 QMainWindow.setStyleSheet 在 Windows 上的重绘 bug
        app.setStyleSheet(build_stylesheet(theme_name))
        # 刷新 UI，移除 processEvents 避免重入死锁
        self.update()
        for w in [self.sidebar, getattr(self, 'title_bar', None), getattr(self, 'bottom_bar', None)]:
            if w:
                w.update()

    def nativeEvent(self, event_type, message):
        "处理 Windows 原生消息以实现无边框窗口缩放和拖拽"
        try:
            if event_type in (b"windows_generic_MSG", "windows_generic_MSG", b"windows_dispatcher_MSG", "windows_dispatcher_MSG"):
                import ctypes

                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == 0x0083:  # WM_NCCALCSIZE
                    return True, 0

                if msg.message == 0x0084:  # WM_NCHITTEST
                    global_pos = QCursor.pos()
                    local_pos = self.mapFromGlobal(global_pos)
                    lx, ly = local_pos.x(), local_pos.y()
                    w, h = self.width(), self.height()

                    for btn_name in ("btn_min", "btn_max", "btn_close"):
                        btn = getattr(self, btn_name, None)
                        if btn and btn.isVisible() and btn.rect().contains(btn.mapFromGlobal(global_pos)):
                            return True, 1

                    title_bar_height = self.title_bar.height() if hasattr(self, 'title_bar') and self.title_bar.isVisible() else 36
                    if self._is_maximized:
                        if ly < title_bar_height:
                            return True, 2
                        return True, 1

                    b = self._border
                    left = lx <= b
                    right = lx >= w - b
                    top = ly <= b
                    bottom = ly >= h - b

                    if top and left:
                        return True, 13
                    elif top and right:
                        return True, 14
                    elif bottom and left:
                        return True, 16
                    elif bottom and right:
                        return True, 17
                    elif top:
                        return True, 12
                    elif bottom:
                        return True, 15
                    elif left:
                        return True, 10
                    elif right:
                        return True, 11

                    if ly < title_bar_height:
                        return True, 2

                elif msg.message == 0x00A3:
                    self._toggle_maximize()
                    return True, 0

        except Exception:
            pass

        return super().nativeEvent(event_type, message)

    def update_project_ui(self):
        if self.pm.current_project_name:
            self.lbl_home_title.setText(f"控制台 / {self.pm.current_project_name}")
            self.home_summary.setText(f"项目已加载：{self.pm.current_project_path}")
            self.on_data_changed()
        self.page_search.refresh_project_context()
        self.page_extraction.refresh_project_context()

    def _animate_number(self, label, target, duration_ms=600):
        """数字从当前值滚动增加到目标值"""
        try:
            current = int(label.text())
        except ValueError:
            current = 0

        if current == target:
            return

        steps = max(20, duration_ms // 16)
        delta = (target - current) / steps
        step = [0]

        def tick():
            step[0] += 1
            val = int(current + delta * step[0])
            if step[0] >= steps:
                val = target
                timer.stop()
                timer.deleteLater()
            label.setText(str(val))

        timer = QTimer(self)
        timer.timeout.connect(tick)
        timer.start(16)

    def refresh_stats(self):
        if not self.pm.engine:
            for label in self.stats_labels.values():
                label.setText("0")
            self.activity_board.setPlainText("暂无项目数据。")
            return

        with self.pm.get_session() as session:
            total = session.exec(select(func.count(Paper.id))).one()
            pdf_count = session.exec(select(func.count(File.id))).one()
            parsed_papers = session.exec(select(func.count(func.distinct(Chunk.paper_id)))).one()
            # Count distinct papers that have at least one successful extraction job
            ai_done = session.exec(select(func.count(func.distinct(ExtractionJob.paper_id))).where(ExtractionJob.status == "success")).one()
            waiting_extract = session.exec(
                select(func.count(func.distinct(File.paper_id))).where(File.paper_id.not_in(select(Chunk.paper_id)))
            ).one()

            self._animate_number(self.stats_labels["total"], total)
            self._animate_number(self.stats_labels["pdf"], pdf_count)
            self._animate_number(self.stats_labels["chunks"], parsed_papers)
            self._animate_number(self.stats_labels["ai_done"], ai_done)

            lines = [
                f"已收录文献：{total}",
                f"已落地 PDF：{pdf_count}",
                f"已解析论文：{parsed_papers}",
                f"已完成抽取：{ai_done}",
                f"待解析或待抽取项目：{waiting_extract}",
            ]
            self.activity_board.setPlainText("\n".join(lines))

    def toggle_sidebar(self):
        sizes = self.main_splitter.sizes()
        current = sizes[0] if sizes else self.sidebar.width()
        if current > 80:
            self._sidebar_width = current
            self.main_splitter.setSizes([64, max(self.width() - 64, 600)])
            self.sidebar_logo.setHidden(True)
            self.sidebar_subtitle.setHidden(True)
            self.sidebar_footer.setHidden(True)
            self.sidebar_top.setContentsMargins(10, 16, 10, 8)
            self.btn_toggle_sidebar.setProperty("collapsed", True)
            self.btn_toggle_sidebar.style().unpolish(self.btn_toggle_sidebar)
            self.btn_toggle_sidebar.style().polish(self.btn_toggle_sidebar)
            for btn in self.nav_btns:
                btn.setText(btn.property("icon_text"))
                btn.setProperty("collapsed", True)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
        else:
            restored = getattr(self, "_sidebar_width", 220) or 220
            self.main_splitter.setSizes([restored, max(self.width() - restored, 600)])
            self.sidebar_logo.setText("LIT AI")
            self.sidebar_logo.setHidden(False)
            self.sidebar_subtitle.setHidden(False)
            self.sidebar_footer.setHidden(False)
            self.sidebar_top.setContentsMargins(14, 16, 14, 8)
            self.btn_toggle_sidebar.setProperty("collapsed", False)
            self.btn_toggle_sidebar.style().unpolish(self.btn_toggle_sidebar)
            self.btn_toggle_sidebar.style().polish(self.btn_toggle_sidebar)
            for btn in self.nav_btns:
                btn.setText(btn.property("full_text"))
                btn.setProperty("collapsed", False)
                btn.style().unpolish(btn)
                btn.style().polish(btn)

    def switch_page(self, index):
        self.content_stack.setCurrentIndex(index)
        if index == 0:
            self.refresh_stats()
        if index == 1:
            self.page_search.refresh_project_context()
        if index == 2:
            self.page_library.refresh_list()
        if index == 3:
            self.page_extraction.refresh_project_context()
            self.page_extraction.refresh_list()
        for i, btn in enumerate(self.nav_btns):
            btn.setProperty("active", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def handle_new_project(self):
        dialog = ProjectDialog(self)
        if dialog.exec():
            name, path = dialog.get_data()
            try:
                self.pm.create_project(name, path)
                self.update_project_ui()
            except Exception as exc:
                ToastDialog.critical(self, "错误", str(exc))

    def handle_open_project(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择项目目录")
        if dir_path:
            try:
                self.pm.load_project(dir_path)
                self.update_project_ui()
            except Exception as exc:
                ToastDialog.critical(self, "错误", str(exc))

    def extract_pdf_title(self, pdf_path: str) -> str:
        fallback = Path(pdf_path).stem
        try:
            with fitz.open(pdf_path) as doc:
                meta_title = (doc.metadata or {}).get("title", "").strip()
                if meta_title:
                    return meta_title
                if doc.page_count:
                    text = doc.load_page(0).get_text("text")
                    for line in text.splitlines():
                        clean = line.strip()
                        if len(clean) >= 10:
                            return clean[:300]
        except Exception:
            pass
        return fallback

    def on_data_changed(self):
        self.refresh_stats()
        self.page_library.refresh_list()
        self.page_extraction.refresh_list()

    def handle_import_pdfs(self):
        if not self.pm.current_project_path:
            print("[WARN] handle_import_pdfs: no project open, showing warning")
            ToastDialog.warning(self, "提示", "请先打开或创建项目。")
            return

        files, _ = QFileDialog.getOpenFileNames(self, "选择本地 PDF", "", "PDF Files (*.pdf)")
        if not files:
            return

        imported_count = 0
        skipped_count = 0
        errors = []
        with self.pm.get_session() as session:
            for source_path in files:
                try:
                    title = self.extract_pdf_title(source_path)
                    existing = session.exec(select(Paper).where(Paper.title == title)).first()
                    if existing:
                        skipped_count += 1
                        continue

                    paper = Paper(
                        paper_number=self.pm.next_paper_number(session),
                        title=title,
                        source="Local Import",
                        abstract="",
                    )
                    session.add(paper)
                    session.commit()
                    session.refresh(paper)

                    dest_filename = self.pm.build_pdf_filename(paper.paper_number, paper.title)
                    destination = os.path.join(self.pm.current_project_path, "papers", "pdf", dest_filename)
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(source_path, destination)
                    session.add(
                        File(
                            paper_id=paper.id,
                            file_type="pdf",
                            file_path=destination,
                            status="downloaded",
                            original_url=source_path,
                        )
                    )
                    session.commit()
                    imported_count += 1
                except Exception as exc:
                    errors.append(f"{os.path.basename(source_path)}: {exc}")
                    continue

        self.on_data_changed()
        msg = f"成功导入 {imported_count} 篇 PDF，跳过 {skipped_count} 篇重复文件。"
        if errors:
            msg += f"\n{sorted(errors)[:3]} 等导入失败。"
        ToastDialog.information(self, "导入完成", msg)

    def closeEvent(self, event):
        # 1. 终止所有页面中运行的后台线程
        self.page_search.cleanup()
        self.page_library.cleanup()
        self.page_extraction.cleanup()
        # 2. 关闭数据库连接
        self.pm.close()
        super().closeEvent(event)
