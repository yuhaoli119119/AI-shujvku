import os

from PySide6.QtCore import QPropertyAnimation, QThread, Signal, QTimer, QEasingCurve, QObject, QEvent
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QMessageBox,
)
from sqlmodel import select

from ..core.models import Paper, File
from ..services.search_services import SearchService
from ..services.xmol_service import XMOLService
from ..services.scholar_service import ScholarService
from ..services.scholar_downloader import PDFDownloader
from ..services.findpapers_service import FindpapersService
from ..services.literature_ai_client import LiteratureAIClient
from .toast_dialog import ToastDialog
from loguru import logger


def build_findpapers_kwargs(config) -> dict:
    return {
        "proxy": config.get("proxy") or None,
        "email": config.get("findpapers_email") or None,
        "ieee_api_key": config.get("findpapers_ieee_api_key") or None,
        "wos_api_key": config.get("findpapers_wos_api_key") or None,
        "scopus_api_key": config.get("findpapers_scopus_api_key") or None,
        "pubmed_api_key": config.get("findpapers_pubmed_api_key") or None,
        "openalex_api_key": config.get("findpapers_openalex_api_key") or None,
        "semantic_scholar_api_key": config.get("findpapers_semantic_scholar_api_key") or None,
        "ssl_verify": bool(config.get("findpapers_ssl_verify", True)),
    }


def missing_findpapers_credentials(config, providers) -> list[str]:
    required = {
        "ieee": ("IEEE Xplore", "findpapers_ieee_api_key"),
        "wos": ("Web of Science", "findpapers_wos_api_key"),
        "scopus": ("Scopus", "findpapers_scopus_api_key"),
    }
    missing = []
    for provider in providers:
        info = required.get(provider)
        if info and not (config.get(info[1]) or "").strip():
            missing.append(info[0])
    return missing


class AbstractDialog(QDialog):
    def __init__(self, title, abstract, parent=None):
        super().__init__(parent)
        self.setWindowTitle("摘要详情")
        self.resize(640, 420)
        layout = QVBoxLayout(self)

        lbl_title = QLabel(title)
        lbl_title.setObjectName("sectionTitle")
        lbl_title.setWordWrap(True)
        layout.addWidget(lbl_title)

        text = QTextEdit()
        text.setPlainText(abstract or "暂无摘要内容")
        text.setReadOnly(True)
        layout.addWidget(text)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)


class ScholarSearchThread(QThread):
    """Google Scholar 搜索线程"""
    results_ready = Signal(list)
    error_occurred = Signal(str)
    progress_update = Signal(int)

    def __init__(self, query, pages, min_year, skip_words, proxy, scholar_results=10):
        super().__init__()
        self.query = query
        self.pages = pages
        self.min_year = min_year
        self.skip_words = skip_words
        self.proxy = proxy
        self.scholar_results = scholar_results

    def run(self):
        try:
            self.progress_update.emit(20)
            scholar = ScholarService(proxy=self.proxy)
            results = scholar.search(
                query=self.query,
                pages=self.pages,
                min_year=self.min_year,
                skip_words=self.skip_words,
                scholar_results=self.scholar_results
            )
            self.progress_update.emit(80)
            
            # 转换为统一格式
            normalized = []
            for item in results:
                normalized.append({
                    "id": item.get("link") or "",
                    "title": item.get("title", "Untitled"),
                    "authors": item.get("authors", ""),
                    "year": item.get("year"),
                    "doi": "",
                    "abstract": "",
                    "source": "Google Scholar",
                    "is_oa": bool(item.get("link_pdf")),
                    "citations": item.get("cites", 0),
                    "oa_url": item.get("link_pdf", ""),
                    "journal": "",
                    "impact_factor": None,
                    "scholar_link": item.get("link", ""),
                    "scholar_pdf": item.get("link_pdf", ""),
                })
            
            self.results_ready.emit(normalized)
            self.progress_update.emit(100)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class DownloadThread(QThread):
    """PDF 下载线程"""
    download_complete = Signal(dict)
    error_occurred = Signal(str)
    progress_update = Signal(int, int)  # current, total

    def __init__(self, papers, save_dir, proxy, findpapers_kwargs=None):
        super().__init__()
        self.papers = papers
        self.save_dir = save_dir
        self.proxy = proxy
        self.findpapers_kwargs = findpapers_kwargs or {"proxy": proxy}

    def run(self):
        import sys
        from loguru import logger

        class LoggerStream:
            def __init__(self, level="INFO"):
                self.level = level
                self.encoding = "utf-8"
                self.errors = "replace"
            def write(self, message):
                msg = message.strip()
                if msg:
                    try:
                        if self.level == "ERROR":
                            logger.error(msg)
                        else:
                            logger.info(msg)
                    except Exception:
                        pass
            def flush(self):
                pass
            def isatty(self):
                return False
            def readable(self):
                return False
            def writable(self):
                return True
            def seekable(self):
                return False

        redirector = LoggerStream("INFO")
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = redirector
        sys.stderr = redirector

        try:
            fp_service = FindpapersService(**self.findpapers_kwargs)
            downloader = PDFDownloader(proxy=self.proxy)
            total = len(self.papers)
            
            for i, paper in enumerate(self.papers):
                self.progress_update.emit(i + 1, total)
                
                # 如果这个 Paper 对象由 findpapers 产生，则使用高级 findpapers 引擎下载
                if paper.get("_paper_obj"):
                    try:
                        metrics = fp_service.download(
                            papers=[paper["_paper_obj"]],
                            output_directory=self.save_dir,
                            show_progress=False
                        )
                        success = metrics.get("downloaded_papers", 0) > 0
                        
                        import glob
                        import os
                        clean_title = "".join(c for c in paper.get("title", "") if c.isalnum() or c in " -_")[:50]
                        pattern = os.path.join(self.save_dir, f"*{clean_title}*.pdf")
                        found_files = glob.glob(pattern)
                        path = found_files[0] if found_files else os.path.join(self.save_dir, f"{paper.get('title')}.pdf")
                        
                        self.download_complete.emit({
                            "title": paper.get("title"),
                            "success": success,
                            "path": path,
                            "source": "Findpapers Engine",
                            "error": "" if success else "Findpapers download failed"
                        })
                    except Exception as fp_exc:
                        logger.error(f"Findpapers download failed: {fp_exc}")
                        # 降级使用普通下载器
                        result = downloader.download(
                            doi=paper.get("doi"),
                            scholar_link=paper.get("scholar_link"),
                            title=paper.get("title", "paper"),
                            save_dir=self.save_dir
                        )
                        self.download_complete.emit({
                            "title": paper.get("title"),
                            **result
                        })
                else:
                    result = downloader.download(
                        doi=paper.get("doi"),
                        scholar_link=paper.get("scholar_link"),
                        title=paper.get("title", "paper"),
                        save_dir=self.save_dir
                    )
                    self.download_complete.emit({
                        "title": paper.get("title"),
                        **result
                    })
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


def apply_bubble_glow(widget, color_hex="#6C7BFF"):
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    from PySide6.QtCore import QPropertyAnimation, QEasingCurve, QObject, QEvent
    from PySide6.QtGui import QColor
    
    # 将 Hex 颜色转化为带有半透明度的气泡氛围色彩
    c = QColor(color_hex)
    c.setAlpha(160)
    
    effect = QGraphicsDropShadowEffect(widget)
    effect.setColor(c)
    effect.setOffset(0, 0)
    effect.setBlurRadius(10)
    widget.setGraphicsEffect(effect)
    
    # 动态呼吸动画（模拟气泡的持续平滑膨胀与收缩）
    breath_anim = QPropertyAnimation(effect, b"blurRadius")
    breath_anim.setDuration(2400)  # 2.4秒慢节奏气泡呼吸
    breath_anim.setStartValue(10)
    breath_anim.setKeyValueAt(0.5, 24)  # 扩张状态
    breath_anim.setEndValue(10)
    breath_anim.setEasingCurve(QEasingCurve.InOutQuad)
    breath_anim.setLoopCount(-1)
    breath_anim.start()
    
    if not hasattr(widget, "_anims"):
        widget._anims = []
    widget._anims.append(breath_anim)
    
    # 点击瞬间的气泡脉冲反馈动画
    def on_click():
        pulse = QPropertyAnimation(effect, b"blurRadius")
        pulse.setDuration(350)
        pulse.setStartValue(12)
        pulse.setKeyValueAt(0.15, 42)  # 点击瞬间像气泡膨胀破裂的绚丽动态反馈
        pulse.setEndValue(12)
        pulse.setEasingCurve(QEasingCurve.OutQuad)
        
        breath_anim.pause()
        pulse.finished.connect(lambda: breath_anim.resume())
        pulse.start()
        widget._temp_pulse = pulse  # 绑定引用防垃圾回收
        
    # 如果是按钮，绑定点击信号
    if hasattr(widget, "clicked"):
        widget.clicked.connect(on_click)
    else:
        # 非按钮类输入框通过事件过滤器捕获点击/获得焦点状态
        class ClickFilter(QObject):
            def __init__(self, callback, parent=None):
                super().__init__(parent)
                self.callback = callback
            def eventFilter(self, obj, event):
                if event.type() in [QEvent.MouseButtonPress, QEvent.FocusIn]:
                    self.callback()
                return False
                
        click_filter = ClickFilter(on_click, widget)
        widget.installEventFilter(click_filter)
        widget._click_filter = click_filter


class AIWorkflowThread(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, lit_ai_url, payload):
        super().__init__()
        self.client = LiteratureAIClient(lit_ai_url)
        self.payload = payload

    def run(self):
        try:
            result = self.client.ai_workflow(self.payload)
            self.finished_ok.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class SearchPage(QWidget):
    paper_added = Signal()

    def __init__(self, pm, config):
        super().__init__()
        self.pm = pm
        self.config = config
        self.results = []
        self.thread = None
        self.download_thread = None
        self.direct_download_thread = None
        self.ai_workflow_thread = None
        self.setup_ui()

    def animate_entry(self, widget, delay_ms=0, duration_ms=500):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)

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

    def _toggle_search_panel(self):
        is_visible = not self.search_panel.isHidden()
        self.search_panel.setHidden(is_visible)
        self.btn_toggle_search.setText("展开面板" if is_visible else "折叠面板")

    def bind_import_action(self, slot):
        self.btn_import_pdf.clicked.connect(slot)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 24, 24, 24)
        hero_layout.setSpacing(8)

        # 主搜索折叠面板（设置背景为透明，防止破坏卡片立体背景）
        self.search_panel = QWidget()
        self.search_panel.setStyleSheet("background: transparent; border: none;")
        search_panel_layout = QVBoxLayout(self.search_panel)
        search_panel_layout.setContentsMargins(0, 0, 0, 0)
        search_panel_layout.setSpacing(8)

        title = QLabel("全球文献检索")
        title.setObjectName("pageTitle")
        search_panel_layout.addWidget(title)

        subtitle = QLabel("聚合 OpenAlex、arXiv、X-MOL 与 Google Scholar 信息。支持批量下载 PDF。")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        search_panel_layout.addWidget(subtitle)

        search_bar = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("输入关键词、主题、期刊、论文标题或 DOI")
        self.query_input.setFixedHeight(42)
        self.query_input.returnPressed.connect(self.start_search)
        search_bar.addWidget(self.query_input)

        self.btn_search = QPushButton("开始检索")
        self.btn_search.setObjectName("primary_btn")
        self.btn_search.setFixedSize(120, 42)
        self.btn_search.clicked.connect(self.start_search)
        search_bar.addWidget(self.btn_search)

        self.btn_direct_download = QPushButton("精确定位下载")
        self.btn_direct_download.setObjectName("secondary_btn")
        self.btn_direct_download.setFixedSize(120, 42)
        self.btn_direct_download.setToolTip("通过 DOI、ISBN、URL 或标题触发 20+ 多源并发获取引擎精确定位并自动下载文献")
        self.btn_direct_download.clicked.connect(self.start_direct_download)
        search_bar.addWidget(self.btn_direct_download)

        self.btn_ai_workflow = QPushButton("AI自动查文献")
        self.btn_ai_workflow.setObjectName("secondary_btn")
        self.btn_ai_workflow.setFixedSize(132, 42)
        self.btn_ai_workflow.setToolTip("调用 Literature AI 自动完成 AI改写检索、搜索、下载、入库与解析")
        self.btn_ai_workflow.clicked.connect(self.start_ai_workflow)
        search_bar.addWidget(self.btn_ai_workflow)
        
        search_panel_layout.addLayout(search_bar)

        # 选项区域
        options_layout = QVBoxLayout()
        options_layout.setSpacing(10)
        
        # 第一排：数据源选择（单行精简自适应布局）
        row1_layout = QHBoxLayout()
        sources_label = QLabel("检索源:")
        sources_label.setStyleSheet("font-weight: bold;")
        row1_layout.addWidget(sources_label)
        
        self.cb_openalex = QCheckBox("OpenAlex")
        self.cb_openalex.setChecked(True)
        self.cb_arxiv = QCheckBox("arXiv")
        self.cb_arxiv.setChecked(True)
        self.cb_pubmed = QCheckBox("PubMed")
        self.cb_pubmed.setChecked(True)
        self.cb_semantic_scholar = QCheckBox("Semantic Scholar")
        self.cb_semantic_scholar.setChecked(True)
        self.cb_xmol = QCheckBox("X-MOL")
        self.cb_xmol.setChecked(True)
        self.cb_scholar = QCheckBox("Google Scholar")
        
        row1_layout.addWidget(self.cb_openalex)
        row1_layout.addWidget(self.cb_arxiv)
        row1_layout.addWidget(self.cb_pubmed)
        row1_layout.addWidget(self.cb_semantic_scholar)
        row1_layout.addWidget(self.cb_xmol)
        row1_layout.addWidget(self.cb_scholar)
        row1_layout.addStretch()
        options_layout.addLayout(row1_layout)
        
        # 第二排：Scholar 与 检索数量高级选项
        row2_layout = QHBoxLayout()
        scholar_options = QHBoxLayout()
        scholar_options.addWidget(QLabel("页数:"))
        self.scholar_pages = QSpinBox()
        self.scholar_pages.setRange(1, 10)
        self.scholar_pages.setValue(1)
        self.scholar_pages.setToolTip("Google Scholar 搜索页数")
        scholar_options.addWidget(self.scholar_pages)
        scholar_options.addWidget(QLabel("年份≥"))
        self.scholar_min_year = QSpinBox()
        self.scholar_min_year.setRange(1900, 2030)
        self.scholar_min_year.setValue(2000)
        self.scholar_min_year.setToolTip("最小发表年份")
        scholar_options.addWidget(self.scholar_min_year)
        row2_layout.addLayout(scholar_options)
        row2_layout.addSpacing(20)
        
        # 数量限制
        row2_layout.addWidget(QLabel("检索数量:"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(5, 100)
        self.limit_spin.setValue(int(self.config.get("search_limit", 20)))
        row2_layout.addWidget(self.limit_spin)
        row2_layout.addStretch()
        options_layout.addLayout(row2_layout)
        
        search_panel_layout.addLayout(options_layout)
        hero_layout.addWidget(self.search_panel)
        layout.addWidget(hero)
        self.animate_entry(hero, delay_ms=60, duration_ms=500)

        self.progress = QProgressBar()
        self.progress.setHidden(True)
        self.progress_label = QLabel()
        self.progress_label.setHidden(True)
        layout.addWidget(self.progress)
        layout.addWidget(self.progress_label)

        # 操作按钮
        action_layout = QHBoxLayout()
        self.btn_import_pdf = QPushButton("导入本地 PDF")
        self.btn_import_pdf.setObjectName("primary_btn")
        action_layout.addWidget(self.btn_import_pdf)

        self.btn_download_all = QPushButton("下载选中 PDF")
        self.btn_download_all.setObjectName("primary_btn")
        self.btn_download_all.clicked.connect(self.download_selected)
        self.btn_download_all.setEnabled(False)
        action_layout.addWidget(self.btn_download_all)

        # 缩放按钮放下载PDF右侧 (折叠面板 / 展开面板)
        self.btn_toggle_search = QPushButton("折叠面板")
        self.btn_toggle_search.setCheckable(True)
        self.btn_toggle_search.setObjectName("secondary_btn")
        self.btn_toggle_search.clicked.connect(self._toggle_search_panel)
        action_layout.addWidget(self.btn_toggle_search)

        action_layout.addStretch()
        layout.addLayout(action_layout)

        # 动态读取当前主题配置，精准应用气泡呼吸辉光及点击反馈
        try:
            from .theme_manager import get_theme_palette
            theme_name = self.config.get("theme", "Neumorphism")
            palette = get_theme_palette(theme_name)
            accent_hex = palette.accent
        except Exception:
            accent_hex = "#6C7BFF"

        # 对核心功能框及按钮施加动态气泡呼吸和点击脉冲效果
        apply_bubble_glow(self.btn_search, accent_hex)
        apply_bubble_glow(self.btn_ai_workflow, accent_hex)
        apply_bubble_glow(self.scholar_min_year, accent_hex)
        apply_bubble_glow(self.query_input, accent_hex)

        table_card = QFrame()
        table_card.setObjectName("tableCard")
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(16, 16, 16, 16)
        table_layout.setSpacing(12)

        header = QLabel("检索结果")
        header.setObjectName("sectionTitle")
        table_layout.addWidget(header)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["", "标题", "年份", "期刊", "IF", "被引", "来源", "DOI / ID", "摘要", "操作"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        table_layout.addWidget(self.table)
        layout.addWidget(table_card)
        self.animate_entry(table_card, delay_ms=160, duration_ms=500)

    def _refresh_source_readiness(self):
        """No-op as key-required sources are removed."""
        pass

    def start_search(self):
        if self.thread and self.thread.isRunning():
            return
        query = self.query_input.text().strip()
        if not query:
            ToastDialog.information(self, "提示", "请输入检索关键词。")
            return

        providers = []
        if self.cb_openalex.isChecked():
            providers.append("openalex")
        if self.cb_arxiv.isChecked():
            providers.append("arxiv")
        if self.cb_pubmed.isChecked():
            providers.append("pubmed")
        if self.cb_semantic_scholar.isChecked():
            providers.append("semantic_scholar")
        if self.cb_xmol.isChecked():
            providers.append("xmol")
        if self.cb_scholar.isChecked():
            providers.append("scholar")
            
        if not providers:
            ToastDialog.warning(self, "提示", "请至少选择一个检索源。")
            return

        self.config.set("search_limit", self.limit_spin.value())
        self.btn_search.setEnabled(False)
        self.btn_download_all.setEnabled(False)
        self.progress.setHidden(False)
        self.progress.setValue(5)
        self.progress_label.setHidden(False)
        self.table.setRowCount(0)

        if "scholar" in providers and len(providers) == 1:
            # 纯 Scholar 搜索
            self.progress_label.setText("正在搜索 Google Scholar...")
            self.thread = ScholarSearchThread(
                query, 
                self.scholar_pages.value(),
                self.scholar_min_year.value(),
                None,  # skip_words
                self.config.get("proxy"),
                self.limit_spin.value()
            )
            self.thread.results_ready.connect(self.display_results)
            self.thread.error_occurred.connect(self.handle_error)
            self.thread.progress_update.connect(self.progress.setValue)
            self.thread.start()
        else:
            # 混合搜索（不含 Scholar 或多源）
            self.progress_label.setText("正在搜索...")
            self._start_mixed_search(query, providers)

    def _start_mixed_search(self, query, providers):
        """混合搜索（OpenAlex/arXiv/X-MOL）"""
        self.thread = SearchThread(
            query,
            providers,
            self.limit_spin.value(),
            self.config.get("proxy"),
            findpapers_kwargs=build_findpapers_kwargs(self.config),
            scholar_pages=self.scholar_pages.value(),
            scholar_min_year=self.scholar_min_year.value(),
        )
        self.thread.results_ready.connect(self.display_results)
        self.thread.error_occurred.connect(self.handle_error)
        self.thread.progress_update.connect(self.progress.setValue)
        self.thread.start()

    def display_results(self, results):
        self.btn_search.setEnabled(True)
        self.progress.setHidden(True)
        self.progress_label.setHidden(True)
        self.table.setRowCount(0)
        
        if not results:
            ToastDialog.information(self, "提示", "没有找到结果，请换个关键词试试。")
            return

        self.results = results
        for row, res in enumerate(results):
            self.table.insertRow(row)
            
            # 复选框
            checkbox = QCheckBox()
            checkbox.setObjectName("row_checkbox")
            self.table.setCellWidget(row, 0, checkbox)
            
            # 标题
            title_item = QTableWidgetItem(res["title"])
            title_item.setToolTip(res["title"])
            self.table.setItem(row, 1, title_item)
            
            # 年份
            self.table.setItem(row, 2, QTableWidgetItem(str(res.get("year") or "")))
            
            # 期刊
            self.table.setItem(row, 3, QTableWidgetItem(res.get("journal", "")))
            
            # 影响因子
            self.table.setItem(row, 4, QTableWidgetItem(str(res.get("impact_factor") or "-")))
            
            # 被引
            self.table.setItem(row, 5, QTableWidgetItem(str(res.get("citations", "-"))))
            
            # 来源
            self.table.setItem(row, 6, QTableWidgetItem(res.get("source", "")))
            
            # DOI/ID
            doi = res.get("doi") or res.get("id", "")
            self.table.setItem(row, 7, QTableWidgetItem(doi[:50] + "..." if len(doi) > 50 else doi))
            
            # 摘要按钮
            btn_view = QPushButton("查看")
            btn_view.clicked.connect(lambda _, r=res: self.show_abstract(r))
            self.table.setCellWidget(row, 8, btn_view)
            
            # 操作按钮
            btn_add = QPushButton("加入项目")
            btn_add.setObjectName("primary_btn")
            btn_add.clicked.connect(lambda _, r=res: self.add_to_project(r))
            self.table.setCellWidget(row, 9, btn_add)

    def on_selection_changed(self):
        """选中行变化时的处理"""
        selected_rows = set()
        for index in self.table.selectionModel().selectedRows():
            selected_rows.add(index.row())
        # 也检查复选框
        for row in range(self.table.rowCount()):
            checkbox = self.table.cellWidget(row, 0)
            if checkbox and checkbox.isChecked():
                selected_rows.add(row)
        
        self.btn_download_all.setEnabled(len(selected_rows) > 0)

    def show_abstract(self, res):
        abstract = res.get("abstract", "")
        if not abstract:
            abstract = "暂无摘要内容"
        AbstractDialog(res["title"], abstract, self).exec()

    def handle_error(self, error):
        self.btn_search.setEnabled(True)
        self.progress.setHidden(True)
        self.progress_label.setHidden(True)
        QMessageBox.critical(self, "检索出错", error)

    def add_to_project(self, res):
        if not self.pm.current_project_path:
            ToastDialog.warning(self, "提示", "请先打开或创建项目。")
            return

        try:
            with self.pm.get_session() as session:
                existing = None
                if res.get("doi"):
                    existing = session.exec(select(Paper).where(Paper.doi == res["doi"])).first()
                if not existing:
                    existing = session.exec(select(Paper).where(Paper.title == res["title"])).first()
                if existing:
                    ToastDialog.information(self, "提示", "这篇文献已经在当前项目中。")
                    return

                paper = Paper(
                    title=res["title"],
                    year=int(res["year"]) if res.get("year") else None,
                    doi=res.get("doi"),
                    abstract=res.get("abstract"),
                    source=res.get("source"),
                    journal=res.get("journal"),
                    impact_factor=res.get("impact_factor"),
                    oa_url=res.get("oa_url"),
                    is_oa=1 if res.get("is_oa") else 0,
                )
                session.add(paper)
                session.commit()

            self.paper_added.emit()
            ToastDialog.information(self, "成功", f"已加入项目：{res['title'][:36]}")
        except Exception as exc:
            ToastDialog.critical(self, "错误", str(exc))

    def download_selected(self):
        """下载选中的论文"""
        selected_papers = []
        for row in range(self.table.rowCount()):
            checkbox = self.table.cellWidget(row, 0)
            if checkbox and checkbox.isChecked():
                if row < len(self.results):
                    selected_papers.append(self.results[row])
        
        if not selected_papers:
            ToastDialog.warning(self, "提示", "请先选择要下载的论文。")
            return
        
        # 选择保存目录
        from PySide6.QtWidgets import QFileDialog
        save_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not save_dir:
            return
        
        self.btn_download_all.setEnabled(False)
        self.btn_search.setEnabled(False)
        self.progress.setHidden(False)
        self.progress.setMaximum(len(selected_papers))
        self.progress.setValue(0)
        self.progress_label.setHidden(False)
        self.progress_label.setText("正在下载 PDF...")
        
        self.download_thread = DownloadThread(
            selected_papers,
            save_dir,
            self.config.get("proxy"),
            findpapers_kwargs=build_findpapers_kwargs(self.config),
        )
        self.download_thread.download_complete.connect(self.on_download_complete)
        self.download_thread.error_occurred.connect(self.on_download_error)
        self.download_thread.progress_update.connect(self.on_download_progress)
        self.download_thread.finished.connect(self.on_download_finished)
        self.download_thread.start()

    def on_download_progress(self, current, total):
        self.progress.setValue(current)
        self.progress_label.setText(f"正在下载 {current}/{total}...")

    def on_download_complete(self, result):
        if result["success"]:
            print(f"下载成功: {result['path']} (来源: {result['source']})")
        else:
            print(f"下载失败: {result['title'][:30]}... - {result['error']}")

    def on_download_error(self, error):
        ToastDialog.critical(self, "下载错误", error)

    def on_download_finished(self):
        self.btn_search.setEnabled(True)
        self.btn_download_all.setEnabled(True)
        self.progress.setHidden(True)
        self.progress_label.setHidden(True)
        ToastDialog.information(self, "完成", "PDF 下载任务已完成！")

    def cleanup(self):
        """终止所有运行中的线程，确保页面可安全销毁"""
        for thr in (self.thread, self.download_thread, self.direct_download_thread, self.ai_workflow_thread):
            if thr and thr.isRunning():
                thr.quit()
                if not thr.wait(2000):
                    thr.terminate()
                    thr.wait(2000)
        self.thread = None
        self.download_thread = None
        self.direct_download_thread = None
        self.ai_workflow_thread = None

    def start_ai_workflow(self):
        if self.ai_workflow_thread and self.ai_workflow_thread.isRunning():
            ToastDialog.information(self, "提示", "AI 自动查文献任务正在运行中。")
            return
        query = self.query_input.text().strip()
        if not query:
            ToastDialog.information(self, "提示", "请输入检索关键词。")
            return
        lit_ai_url = (self.config.get("literature_ai_url") or "").strip()
        if not lit_ai_url:
            ToastDialog.warning(self, "提示", "请先在系统设置中配置 Literature AI 服务地址。")
            return

        providers = []
        if self.cb_openalex.isChecked():
            providers.append("openalex")
        if self.cb_arxiv.isChecked():
            providers.append("arxiv")
        if self.cb_pubmed.isChecked():
            providers.append("pubmed")
        if self.cb_semantic_scholar.isChecked():
            providers.append("semantic_scholar")

        payload = {
            "query": query,
            "model": self.config.get("llm_model", "deepseek-chat"),
            "max_results": min(self.limit_spin.value(), 10),
            "max_downloads": min(max(1, self.limit_spin.value() // 2), 5),
            "providers": providers,
            "skip_existing": True,
        }

        self.btn_search.setEnabled(False)
        self.btn_ai_workflow.setEnabled(False)
        self.progress.setHidden(False)
        self.progress.setMaximum(0)
        self.progress_label.setHidden(False)
        self.progress_label.setText("正在调用 Literature AI 执行自动查文献工作流...")

        self.ai_workflow_thread = AIWorkflowThread(lit_ai_url, payload)
        
        def on_success(result):
            self.btn_search.setEnabled(True)
            self.btn_ai_workflow.setEnabled(True)
            self.progress.setHidden(True)
            self.progress_label.setHidden(True)
            # AI results sync to db:
            added = result.get("added_count", 0)
            downloaded = result.get("downloaded_count", 0)
            ToastDialog.information(self, "成功", f"AI 工作流执行完毕！\n新增入库: {added} 篇\n成功下载 PDF: {downloaded} 篇")
            self.paper_added.emit()

        def on_failed(error_msg):
            self.btn_search.setEnabled(True)
            self.btn_ai_workflow.setEnabled(True)
            self.progress.setHidden(True)
            self.progress_label.setHidden(True)
            ToastDialog.critical(self, "AI 任务失败", f"执行出错: {error_msg}")

        self.ai_workflow_thread.finished_ok.connect(on_success)
        self.ai_workflow_thread.failed.connect(on_failed)
        self.ai_workflow_thread.start()

    def start_direct_download(self):
        """启动精确定位下载"""
        if self.direct_download_thread and self.direct_download_thread.isRunning():
            ToastDialog.warning(self, "提示", "已有精确定位下载任务正在运行，请等待其完成。")
            return
        query = self.query_input.text().strip()
        if not query:
            ToastDialog.information(self, "提示", "请先在输入框中输入文献的 DOI、ISBN、URL 或标题。")
            return

        if not self.pm.current_project_path:
            ToastDialog.warning(self, "提示", "精确定位下载的文献将自动关联并收录到当前项目文献库，请先创建或打开项目。")
            return

        # 1. 弹出高保真暗黑风精确定位终端控制台日志弹窗
        self.direct_log_dialog = DirectAcquireLogDialog(query, self)
        
        # 2. 启动定位线程
        save_dir = os.path.join(self.pm.current_project_path, "papers", "pdf")
        os.makedirs(save_dir, exist_ok=True)  # 确保本地 PDF 目录存在，防止底层获取引擎保存失败
        proxy = self.config.get("proxy")
        
        self.direct_download_thread = DirectAcquireThread(query, save_dir, proxy)
        
        # 3. 关联信号槽
        self.direct_download_thread.progress_log.connect(self.direct_log_dialog.append_log)
        self.direct_download_thread.finished_success.connect(self.on_direct_download_success)
        self.direct_download_thread.finished_failed.connect(self.on_direct_download_failed)
        
        # 绑定终止按钮
        self.direct_log_dialog.btn_stop.clicked.connect(self.stop_direct_download)
        
        self.direct_download_thread.start()
        
        # 显示弹窗
        self.direct_log_dialog.exec()

    def stop_direct_download(self):
        """请求终止当前正在运行的精确定位下载任务"""
        if self.direct_download_thread and self.direct_download_thread.isRunning():
            self.direct_log_dialog.append_log("\n🛑 正在向引擎发送终止信号，请稍候...")
            self.direct_download_thread.stop()
            self.direct_download_thread.quit()
            self.direct_download_thread.wait()
            self.direct_log_dialog.append_log("🛑 任务已被用户手动终止。")
            self.direct_log_dialog.btn_stop.setEnabled(False)
            self.direct_log_dialog.btn_close.setEnabled(True)

    def on_direct_download_success(self, paper_info):
        """定位下载成功的回调"""
        self.direct_log_dialog.append_log(f"\n🎉 文献获取成功！已保存至 {paper_info.get('path')}")
        self.direct_log_dialog.append_log(f"🤖 正在解析并收录《{paper_info.get('title')}》至数据库...")
        
        # 启用关闭按钮，禁用终止按钮
        self.direct_log_dialog.btn_stop.setEnabled(False)
        self.direct_log_dialog.btn_close.setEnabled(True)
        
        # 导入收录到 SQLite 数据库中
        self.import_acquired_paper(paper_info)

    def on_direct_download_failed(self, error):
        """定位下载失败的回调"""
        self.direct_log_dialog.append_log(f"\n❌ 获取失败，原因: {error}")
        
        self.direct_log_dialog.btn_stop.setEnabled(False)
        self.direct_log_dialog.btn_close.setEnabled(True)
        
        ToastDialog.critical(self, "获取失败", f"精确定位下载未能获取该文献，原因: {error}")

    def import_acquired_paper(self, info):
        """将定位下载的文献完美收录并关联至项目文献库"""
        try:
            with self.pm.get_session() as session:
                existing = None
                if info.get("doi"):
                    existing = session.exec(select(Paper).where(Paper.doi == info["doi"])).first()
                if not existing:
                    existing = session.exec(select(Paper).where(Paper.title == info["title"])).first()
                
                if existing:
                    paper = existing
                else:
                    paper = Paper(
                        title=info["title"],
                        year=int(info["year"]) if info.get("year") else None,
                        doi=info.get("doi"),
                        abstract=info.get("abstract"),
                        source=info.get("source"),
                        journal=info.get("journal"),
                        publisher=info.get("publisher"),
                        oa_url=info.get("oa_url"),
                        is_oa=info.get("is_oa") or 0,
                    )
                    session.add(paper)
                    session.commit()
                    session.refresh(paper)
                
                # 拷贝文件至规范路径 papers/pdf/{paper.id}.pdf
                if info.get("path") and os.path.exists(info["path"]):
                    dest_path = os.path.join(self.pm.current_project_path, "papers", "pdf", f"{paper.id}.pdf")
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    
                    import shutil
                    if str(info["path"]) != str(dest_path):
                        shutil.copy2(info["path"], dest_path)
                        try:
                            os.remove(info["path"])
                        except:
                            pass
                    
                    # 注册 File 关联
                    existing_file = session.exec(select(File).where(File.paper_id == paper.id)).first()
                    if existing_file:
                        existing_file.file_path = dest_path
                        existing_file.status = "downloaded"
                        existing_file.original_url = info.get("source")
                        session.add(existing_file)
                    else:
                        session.add(
                            File(
                                paper_id=paper.id,
                                file_type="pdf",
                                file_path=dest_path,
                                status="downloaded",
                                original_url=info.get("source"),
                            )
                        )
                    session.commit()
            
            # 通知数据变更并刷新页面
            self.paper_added.emit()
            
            parent = self.window()
            if hasattr(parent, "on_data_changed"):
                parent.on_data_changed()
                
            ToastDialog.information(self, "收录成功", f"《{info['title'][:26]}...》已成功定位下载并收录！")
        except Exception as e:
            ToastDialog.critical(self, "收录失败", f"文献已下载，但写入数据库时发生错误: {e}")


# 保留原有的 SearchThread
class SearchThread(QThread):
    results_ready = Signal(list)
    error_occurred = Signal(str)
    progress_update = Signal(int)
 
    def __init__(
        self,
        query,
        providers,
        limit,
        proxy,
        findpapers_kwargs=None,
        scholar_pages=1,
        scholar_min_year=None,
    ):
        super().__init__()
        self.query = query
        self.providers = providers
        self.limit = limit
        self.proxy = proxy
        self.findpapers_kwargs = findpapers_kwargs or {"proxy": proxy}
        self.scholar_pages = scholar_pages
        self.scholar_min_year = scholar_min_year
        self.service = SearchService(proxy=proxy)
        self.xmol = XMOLService(proxy=proxy)
 
    def run(self):
        try:
            all_results = []
            
            # 过滤出 findpapers 引擎支持的数据库
            findpapers_dbs = []
            supported_fp_dbs = ["arxiv", "openalex", "pubmed", "semantic_scholar", "ieee", "wos", "scopus"]
            for p in self.providers:
                if p in supported_fp_dbs:
                    findpapers_dbs.append(p)
            
            progress = 10
            self.progress_update.emit(progress)
            
            # 1. 运行 findpapers 统一的多数据库高级检索
            if findpapers_dbs:
                try:
                    fp_service = FindpapersService(**self.findpapers_kwargs)
                    
                    # findpapers 查询语法规范，如果没被中括号包围则自动补充
                    fp_query = self.query.strip()
                    if not (fp_query.startswith("[") and fp_query.endswith("]")):
                        fp_query = f"[{fp_query}]"
                        
                    fp_result = fp_service.search(
                        fp_query,
                        databases=findpapers_dbs,
                        max_papers_per_database=self.limit,
                        show_progress=False,
                    )
                    
                    fp_dicts = FindpapersService.result_to_display_dicts(fp_result)
                    for item in fp_dicts:
                        normalized_item = {
                            "id": item.get("doi") or item.get("url") or item.get("title"),
                            "title": item.get("title", "Untitled"),
                            "authors": item.get("authors", ""),
                            "year": item.get("year"),
                            "doi": item.get("doi", ""),
                            "abstract": item.get("abstract", ""),
                            "source": f"Findpapers ({item.get('databases', '')})",
                            "is_oa": bool(item.get("is_open_access")),
                            "citations": item.get("citations") or 0,
                            "oa_url": item.get("pdf_url") or item.get("url") or "",
                            "journal": item.get("source", ""),
                            "impact_factor": None,
                            "_paper_obj": item.get("_paper_obj"),  # 完整保留底层 Paper 实体
                        }
                        all_results.append(normalized_item)
                except Exception as fp_exc:
                    logger.error(f"Findpapers 联合检索失败: {fp_exc}")
                
                progress += 40
                self.progress_update.emit(progress)

            # 2. 运行 X-MOL 定制学术爬虫搜索
            if "xmol" in self.providers:
                try:
                    xmol_results = self.service.search_xmol(self.query, limit=self.limit)
                    all_results.extend(xmol_results)
                except Exception as xmol_exc:
                    logger.error(f"X-MOL 检索失败: {xmol_exc}")
                
                progress += 30
                self.progress_update.emit(progress)
 
            # 3. 对搜索结果进行元数据增强（如利用 X-MOL 获取无摘要文献的完整中文/外文摘要）
            enhanced = []
            if "scholar" in self.providers:
                try:
                    scholar = ScholarService(proxy=self.proxy)
                    scholar_results = scholar.search(
                        query=self.query,
                        pages=self.scholar_pages,
                        min_year=self.scholar_min_year,
                        skip_words=None,
                        scholar_results=self.limit,
                    )
                    for item in scholar_results:
                        all_results.append(
                            {
                                "id": item.get("link") or item.get("title"),
                                "title": item.get("title", "Untitled"),
                                "authors": item.get("authors", ""),
                                "year": item.get("year"),
                                "doi": "",
                                "abstract": "",
                                "source": "Google Scholar",
                                "is_oa": bool(item.get("link_pdf")),
                                "citations": item.get("cites", 0),
                                "oa_url": item.get("link_pdf", ""),
                                "journal": "",
                                "impact_factor": None,
                                "scholar_link": item.get("link", ""),
                                "scholar_pdf": item.get("link_pdf", ""),
                            }
                        )
                except Exception as scholar_exc:
                    logger.error(f"Google Scholar 娣峰悎妫€绱㈠け璐? {scholar_exc}")

                progress = min(progress + 20, 95)
                self.progress_update.emit(progress)

            for item in all_results:
                merged = dict(item)
                if not merged.get("abstract") or not merged.get("journal"):
                    try:
                        if item.get("doi"):
                            details = self.xmol.get_details_by_doi(item["doi"])
                        else:
                            details = self.xmol.get_details_by_title(item.get("title", ""))
     
                        if details.get("abstract") and (not merged.get("abstract") or merged["source"].startswith("X-MOL")):
                            merged["abstract"] = details["abstract"]
                        if details.get("journal") and not merged.get("journal"):
                            merged["journal"] = details["journal"]
                        if details.get("impact_factor") and not merged.get("impact_factor"):
                            merged["impact_factor"] = details["impact_factor"]
                        if details.get("doi") and not merged.get("doi"):
                            merged["doi"] = details["doi"]
                        if details.get("status") == "snippet":
                            merged["source"] = f"{merged['source']} / X-MOL Snippet"
                        elif details.get("status") == "full":
                            merged["source"] = f"{merged['source']} / X-MOL"
                    except Exception:
                        pass
 
                enhanced.append(merged)
 
            # 4. 对所有数据源进行深度去重合并
            deduped = {}
            for item in enhanced:
                key = (item.get("doi") or item.get("id") or item.get("title", "")).strip().lower()
                if not key:
                    continue
 
                if key not in deduped:
                    deduped[key] = item
                    continue
 
                current = deduped[key]
                for field in ["abstract", "journal", "doi", "oa_url"]:
                    if not current.get(field) and item.get(field):
                        current[field] = item[field]
                if not current.get("impact_factor") and item.get("impact_factor"):
                    current["impact_factor"] = item["impact_factor"]
                if not current.get("_paper_obj") and item.get("_paper_obj"):
                    current["_paper_obj"] = item["_paper_obj"]
                current["source"] = " / ".join(
                    sorted(set(filter(None, (current.get("source", "") + " / " + item.get("source", "")).split(" / "))))
                )
 
            # 5. 排序（按是否有摘要、被引频次降序）
            def _sort_year(value):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0

            merged_results = sorted(
                deduped.values(),
                key=lambda item: (1 if item.get("abstract") else 0, item.get("citations", 0), _sort_year(item.get("year"))),
                reverse=True,
            )
            self.results_ready.emit(merged_results)
            self.progress_update.emit(100)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class DirectAcquireLogDialog(QDialog):
    """精确定位下载过程实时终端日志弹窗"""
    def __init__(self, ref_str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("精确定位下载控制台")
        self.resize(680, 420)
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e24;
                color: #f8f8f2;
            }
            QPlainTextEdit {
                background-color: #0f0f13;
                color: #50fa7b;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: 1px solid #44475a;
                border-radius: 6px;
            }
            QPushButton {
                min-width: 90px;
                padding: 6px 14px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton#stop_btn {
                background-color: #ff5555;
                color: white;
                border: none;
            }
            QPushButton#stop_btn:hover {
                background-color: #ff6e6e;
            }
            QPushButton#close_btn {
                background-color: #6272a4;
                color: white;
                border: none;
            }
            QPushButton#close_btn:hover {
                background-color: #7284b8;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        
        # 头部说明
        self.info_lbl = QLabel(f"正在定位并下载: {ref_str[:60]}...")
        self.info_lbl.setStyleSheet("font-weight: bold; font-size: 13px; color: #8be9fd;")
        layout.addWidget(self.info_lbl)
        
        # 实时控制台
        from PySide6.QtWidgets import QPlainTextEdit
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        layout.addWidget(self.log_output)
        
        # 底部按钮区
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.btn_stop = QPushButton("终止下载")
        self.btn_stop.setObjectName("stop_btn")
        btn_layout.addWidget(self.btn_stop)
        
        self.btn_close = QPushButton("关闭")
        self.btn_close.setObjectName("close_btn")
        self.btn_close.setEnabled(False)
        btn_layout.addWidget(self.btn_close)
        
        layout.addLayout(btn_layout)
        
        self.btn_close.clicked.connect(self.accept)
        
    def append_log(self, text):
        self.log_output.appendPlainText(text)
        # 自动滚动到底部
        self.log_output.ensureCursorVisible()


class DirectAcquireThread(QThread):
    progress_log = Signal(str)
    finished_success = Signal(dict)
    finished_failed = Signal(str)

    def __init__(self, ref_str, save_dir, proxy):
        super().__init__()
        self.ref_str = ref_str
        self.save_dir = save_dir
        self.proxy = proxy
        self.finder = None

    def run(self):
        import sys
        from pathlib import Path
        
        # 捕捉输出的重定向器
        class StdoutRedirector:
            def __init__(self, signal):
                self.signal = signal
                self.encoding = 'utf-8'
                self.errors = 'replace'
            def write(self, text):
                if text and text.strip():
                    self.signal.emit(text.strip())
            def flush(self):
                pass
            def isatty(self):
                return False
            def readable(self):
                return False
            def writable(self):
                return True
            def seekable(self):
                return False
                
        redirector = StdoutRedirector(self.progress_log)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        
        sys.stdout = redirector
        sys.stderr = redirector
        
        try:
            from paper_finder import PaperFinder
            # 实例化 PaperFinder，传入代理
            self.finder = PaperFinder(silent_init=True, proxy=self.proxy)
            
            # 运行定位与下载
            self.progress_log.emit("🤖 启动 PaperFinder 20+ 多源并发学术引擎...")
            result = self.finder.find(self.ref_str, output_dir=Path(self.save_dir))
            
            if result.success:
                self.finished_success.emit({
                    "title": result.metadata.get("title") if result.metadata else "Unknown Paper",
                    "doi": result.metadata.get("doi") if result.metadata else None,
                    "year": result.metadata.get("year") if result.metadata else None,
                    "journal": result.metadata.get("journal") if result.metadata else None,
                    "publisher": result.metadata.get("publisher") if result.metadata else None,
                    "abstract": result.metadata.get("abstract") if result.metadata else "",
                    "is_oa": 1 if result.metadata.get("is_oa") else 0,
                    "oa_url": result.metadata.get("oa_url") if result.metadata else None,
                    "path": str(result.filepath) if result.filepath else None,
                    "source": result.source or "PaperFinder"
                })
            else:
                self.finished_failed.emit(result.error or "未能在任何公开或平行通道中定位该文献。")
        except Exception as e:
            self.finished_failed.emit(str(e))
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    def stop(self):
        if self.finder:
            self.finder.request_cancel()
