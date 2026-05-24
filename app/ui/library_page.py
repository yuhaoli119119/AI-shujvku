import json
import os
import shutil

from PySide6.QtCore import QPropertyAnimation, Qt, Signal, QTimer, QEasingCurve, QThread
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from loguru import logger
from sqlmodel import select

from ..core.models import File, Paper
from ..core.paper_naming import build_display_title
from ..services.download_service import DownloadService
from ..services.literature_ai_client import LiteratureAIClient, generate_chinese_title
from .toast_dialog import ToastDialog


class CopyableLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setCursor(Qt.IBeamCursor)
        self.setWordWrap(False)
        self.setToolTip(text)


class PaperDetailDialog(QDialog):
    def __init__(self, paper, parent=None):
        super().__init__(parent)
        display_title = build_display_title(
            getattr(paper, "paper_number", None),
            getattr(paper, "chinese_title", None),
            paper.title,
        )
        window_suffix = display_title if len(display_title) <= 30 else f"{display_title[:30]}..."
        self.setWindowTitle(f"文献详情 - {window_suffix}")
        self.resize(700, 500)
        layout = QVBoxLayout(self)

        title_lbl = QLabel(display_title)
        title_lbl.setObjectName("sectionTitle")
        title_lbl.setWordWrap(True)
        title_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(title_lbl)

        info_layout = QHBoxLayout()
        info_lbl = QLabel(
            f"<b>年份:</b> {paper.year or 'N/A'} | "
            f"<b>期刊:</b> {paper.journal or 'N/A'} | "
            f"<b>IF:</b> {paper.impact_factor or 'N/A'}"
        )
        info_layout.addWidget(info_lbl)
        info_layout.addStretch()
        layout.addLayout(info_layout)

        if paper.doi:
            doi_lbl = QLabel(f"<b>DOI:</b> {paper.doi}")
            doi_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(doi_lbl)

        layout.addWidget(QLabel("<b>摘要:</b>"))
        abs_text = QTextEdit()
        abs_text.setPlainText(paper.abstract or "暂无摘要内容")
        abs_text.setReadOnly(True)
        layout.addWidget(abs_text)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)


class LitAISendThread(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, lit_ai_url, payload, api_key="", base_url="", model="gpt-4o-mini"):
        super().__init__()
        self.client = LiteratureAIClient(lit_ai_url)
        self.payload = payload
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def run(self):
        try:
            existing = self.client.find_by_source_path(self.payload["pdf_path"])
            if existing:
                paper_id = existing["id"]
                ingest_result = {"paper_id": paper_id, "title": existing.get("title"), "status": "already_exists"}
            else:
                ingest_result = self.client.ingest_path(self.payload)
                paper_id = ingest_result["paper_id"]
            detail = self.client.get_paper(paper_id)
            chinese_title = None
            try:
                chinese_title = generate_chinese_title(
                    detail.get("title") or self.payload.get("title") or "",
                    api_key=self.api_key,
                    base_url=self.base_url or None,
                    model=self.model,
                )
            except Exception:
                chinese_title = None
            self.finished_ok.emit(
                {
                    "ingest": ingest_result,
                    "detail": detail,
                    "chinese_title": chinese_title,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class LitAISyncThread(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, pm, lit_ai_url, api_key="", base_url="", model="gpt-4o-mini"):
        super().__init__()
        self.pm = pm
        self.client = LiteratureAIClient(lit_ai_url)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def run(self):
        try:
            synced = 0
            created = 0
            linked_files = 0
            skipped = 0
            with self.pm.get_session() as session:
                for remote_item in self.client.iter_all_papers():
                    remote_id = str(remote_item.get("id") or "").strip()
                    if not remote_id:
                        skipped += 1
                        continue

                    local_paper = session.exec(select(Paper).where(Paper.remote_paper_id == remote_id)).first()
                    if not local_paper and remote_item.get("doi"):
                        local_paper = session.exec(select(Paper).where(Paper.doi == remote_item["doi"])).first()
                    if not local_paper and remote_item.get("title"):
                        local_paper = session.exec(select(Paper).where(Paper.title == remote_item["title"])).first()

                    is_new = local_paper is None
                    if is_new:
                        local_paper = Paper(
                            paper_number=self.pm.next_paper_number(session),
                            title=remote_item.get("title") or "Untitled",
                            doi=remote_item.get("doi"),
                            abstract=remote_item.get("abstract"),
                            year=remote_item.get("year"),
                            journal=remote_item.get("journal"),
                            authors="; ".join(remote_item.get("authors") or []),
                            source="Literature AI Sync",
                            remote_paper_id=remote_id,
                        )
                        session.add(local_paper)
                        session.commit()
                        session.refresh(local_paper)
                        created += 1
                    else:
                        local_paper.remote_paper_id = remote_id
                        if remote_item.get("title"):
                            local_paper.title = remote_item["title"]
                        if remote_item.get("abstract") and not local_paper.abstract:
                            local_paper.abstract = remote_item["abstract"]
                        if remote_item.get("year") and not local_paper.year:
                            local_paper.year = remote_item["year"]
                        if remote_item.get("journal") and not local_paper.journal:
                            local_paper.journal = remote_item["journal"]
                        if remote_item.get("authors") and not local_paper.authors:
                            local_paper.authors = "; ".join(remote_item.get("authors") or [])
                        session.add(local_paper)
                        session.commit()

                    try:
                        chinese_title = generate_chinese_title(
                            remote_item.get("title") or local_paper.title or "",
                            api_key=self.api_key,
                            base_url=self.base_url or None,
                            model=self.model,
                        )
                    except Exception:
                        chinese_title = None
                    if chinese_title and chinese_title != local_paper.chinese_title:
                        local_paper.chinese_title = chinese_title
                        session.add(local_paper)
                        session.commit()

                    detail = self.client.get_paper(remote_id)
                    pdf_source = LiteratureAIClient.pick_accessible_pdf_path(detail)
                    if pdf_source:
                        existing_file = session.exec(select(File).where(File.paper_id == local_paper.id)).first()
                        if existing_file and existing_file.file_path and os.path.exists(existing_file.file_path):
                            synced += 1
                            continue

                        dest_filename = self.pm.build_pdf_filename(local_paper.paper_number, local_paper.title)
                        dest_path = os.path.join(self.pm.current_project_path, "papers", "pdf", dest_filename)
                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                        if os.path.abspath(pdf_source) != os.path.abspath(dest_path):
                            shutil.copy2(pdf_source, dest_path)
                        else:
                            dest_path = pdf_source

                        if existing_file:
                            existing_file.file_path = dest_path
                            existing_file.status = "downloaded"
                            existing_file.original_url = detail.get("source_path") or detail.get("pdf_path")
                            session.add(existing_file)
                        else:
                            session.add(
                                File(
                                    paper_id=local_paper.id,
                                    file_type="pdf",
                                    file_path=dest_path,
                                    status="downloaded",
                                    original_url=detail.get("source_path") or detail.get("pdf_path"),
                                )
                            )
                        session.commit()
                        linked_files += 1

                    synced += 1

            self.finished_ok.emit(
                {
                    "synced": synced,
                    "created": created,
                    "linked_files": linked_files,
                    "skipped": skipped,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class LibraryPage(QWidget):
    library_changed = Signal()

    def __init__(self, pm):
        super().__init__()
        self.pm = pm
        self.ds = DownloadService()
        self.download_threads = {}
        self.send_threads = []
        self.sync_thread = None
        self.setup_ui()

    def cleanup(self):
        for paper_id, thread in list(self.download_threads.items()):
            if thread.isRunning():
                thread.quit()
                if not thread.wait(2000):
                    thread.terminate()
                    thread.wait(2000)
        self.download_threads.clear()

        for thread in list(self.send_threads):
            if thread.isRunning():
                thread.quit()
                if not thread.wait(1000):
                    thread.terminate()
                    thread.wait(1000)
        self.send_threads.clear()

        if self.sync_thread and self.sync_thread.isRunning():
            self.sync_thread.quit()
            if not self.sync_thread.wait(2000):
                self.sync_thread.terminate()
                self.sync_thread.wait(2000)
        self.sync_thread = None

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

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # 标题行布局：标题与同步 LitAI 结果按钮在同一水平面上
        header_layout = QHBoxLayout()
        title = QLabel("项目文献库")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)

        header_layout.addStretch()

        self.btn_sync_litai = QPushButton("同步 LitAI 结果")
        self.btn_sync_litai.setFixedHeight(34)
        self.btn_sync_litai.setFixedWidth(120)
        self.btn_sync_litai.clicked.connect(self.sync_from_literature_ai)
        header_layout.addWidget(self.btn_sync_litai)
        layout.addLayout(header_layout)

        subtitle = QLabel("统一管理加入项目的文献、PDF 下载状态和详情。")
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("tableCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["论文标题", "发表年份", "期刊", "IF", "本地 PDF 状态", "交互操作"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(52)
        card_layout.addWidget(self.table)
        layout.addWidget(card)
        self.animate_entry(card, delay_ms=80, duration_ms=500)

    def refresh_list(self):
        if not self.pm.engine:
            self.table.setRowCount(0)
            return

        self.table.setRowCount(0)
        with self.pm.get_session() as session:
            papers = session.exec(select(Paper)).all()
            for row, paper in enumerate(papers):
                self.table.insertRow(row)

                title_label = CopyableLabel(build_display_title(paper.paper_number, paper.chinese_title, paper.title))
                title_label.setMargin(6)
                self.table.setCellWidget(row, 0, title_label)
                self.table.setItem(row, 1, QTableWidgetItem(str(paper.year or "")))
                self.table.setItem(row, 2, QTableWidgetItem(paper.journal or ""))
                self.table.setItem(row, 3, QTableWidgetItem(str(paper.impact_factor or "-")))

                file_record = session.exec(select(File).where(File.paper_id == paper.id)).first()
                is_downloaded = bool(file_record and os.path.exists(file_record.file_path))
                status_text = "已下载" if is_downloaded else "未下载"
                self.table.setItem(row, 4, QTableWidgetItem(status_text))

                btn_container = QWidget()
                btn_layout = QHBoxLayout(btn_container)
                btn_layout.setContentsMargins(5, 2, 5, 2)
                btn_layout.setSpacing(5)

                btn_view = QPushButton("详情")
                btn_view.setFixedHeight(32)
                btn_view.clicked.connect(lambda _, p=paper: self.show_details(p))

                btn_download = QPushButton("下载")
                btn_download.setFixedHeight(32)
                btn_download.clicked.connect(lambda _, p=paper: self.handle_download(p))
                if is_downloaded:
                    btn_download.setEnabled(False)
                    btn_download.setText("完成")

                btn_delete = QPushButton("删除")
                btn_delete.setObjectName("danger_btn")
                btn_delete.setFixedHeight(32)
                btn_delete.clicked.connect(lambda _, p=paper: self.handle_delete(p))

                btn_layout.addWidget(btn_view)
                btn_layout.addWidget(btn_download)
                btn_layout.addWidget(btn_delete)

                if is_downloaded and file_record and self.pm.current_config:
                    lit_ai_url = self.pm.current_config.get("literature_ai_url", "")
                    if lit_ai_url:
                        btn_send_ai = QPushButton("发往LitAI")
                        btn_send_ai.setFixedHeight(32)
                        btn_send_ai.setStyleSheet("color: #6f42c1; border-color: #6f42c1;")
                        btn_send_ai.clicked.connect(
                            lambda _, p=paper, fp=file_record.file_path, btn=btn_send_ai: self._send_to_literature_ai(p, fp, button=btn)
                        )
                        btn_layout.addWidget(btn_send_ai)

                self.table.setCellWidget(row, 5, btn_container)

    def _send_to_literature_ai(self, paper, pdf_path, show_toast=True, button=None):
        lit_ai_url = self.pm.current_config.get("literature_ai_url", "")
        if not lit_ai_url:
            ToastDialog.warning(self, "提示", "请先在系统设置中配置 Literature AI 服务地址。")
            return
        if not os.path.exists(pdf_path):
            ToastDialog.warning(self, "提示", "PDF 文件不存在: " + pdf_path)
            return

        authors_list = []
        if paper.authors:
            authors_list = [a.strip() for a in paper.authors.split(";") if a.strip()] or [
                a.strip() for a in paper.authors.split(",") if a.strip()
            ]
        payload = {
            "pdf_path": pdf_path,
            "title": paper.title,
            "doi": paper.doi,
            "authors": authors_list,
            "year": paper.year,
            "journal": paper.journal,
            "abstract": paper.abstract,
        }

        if button is not None:
            button.setEnabled(False)
            button.setText("发送中...")

        thread = LitAISendThread(
            lit_ai_url,
            payload,
            api_key=self.pm.current_config.get("api_key", ""),
            base_url=self.pm.current_config.get("base_url", ""),
            model=self.pm.current_config.get("llm_model", "gpt-4o-mini"),
        )
        self.send_threads.append(thread)

        def on_success(result):
            ingest = result.get("ingest", {})
            detail = result.get("detail", {})
            chinese_title = result.get("chinese_title")
            with self.pm.get_session() as session:
                local_paper = session.get(Paper, paper.id)
                if local_paper:
                    local_paper.remote_paper_id = str(ingest.get("paper_id") or detail.get("id") or "") or None
                    if detail.get("title"):
                        local_paper.title = detail["title"]
                    if chinese_title:
                        local_paper.chinese_title = chinese_title
                    if detail.get("abstract") and not local_paper.abstract:
                        local_paper.abstract = detail["abstract"]
                    session.add(local_paper)
                    session.commit()
            if button is not None:
                button.setEnabled(True)
                button.setText("发往LitAI")
            self.refresh_list()
            if show_toast:
                ToastDialog.information(
                    self,
                    "发送成功",
                    "已同步到 Literature AI\n论文: "
                    + str(detail.get("title", paper.title))[:50]
                    + "\n状态: "
                    + str(ingest.get("status", "ok")),
                )

        def on_failed(message):
            if button is not None:
                button.setEnabled(True)
                button.setText("发往LitAI")
            if show_toast:
                ToastDialog.critical(self, "发送失败", "无法连接到 Literature AI:\n" + str(message) + "\n\n请确认服务已启动且地址正确。")

        def on_finished():
            if thread in self.send_threads:
                self.send_threads.remove(thread)
            thread.deleteLater()

        thread.finished_ok.connect(on_success)
        thread.failed.connect(on_failed)
        thread.finished.connect(on_finished)
        thread.start()

    def show_details(self, paper):
        PaperDetailDialog(paper, self).exec()

    def sync_from_literature_ai(self):
        if self.sync_thread and self.sync_thread.isRunning():
            ToastDialog.information(self, "提示", "LitAI 同步任务正在进行中，请稍候。")
            return
        lit_ai_url = self.pm.current_config.get("literature_ai_url", "") if self.pm.current_config else ""
        if not lit_ai_url:
            ToastDialog.warning(self, "提示", "请先在系统设置中配置 Literature AI 服务地址。")
            return
        if not self.pm.current_project_path:
            ToastDialog.warning(self, "提示", "请先打开或创建项目。")
            return

        self.btn_sync_litai.setEnabled(False)
        self.btn_sync_litai.setText("同步中...")
        self.sync_thread = LitAISyncThread(
            self.pm,
            lit_ai_url,
            api_key=self.pm.current_config.get("api_key", ""),
            base_url=self.pm.current_config.get("base_url", ""),
            model=self.pm.current_config.get("llm_model", "gpt-4o-mini"),
        )

        def on_success(result):
            self.refresh_list()
            self.library_changed.emit()
            self.refresh_stats_signal()
            ToastDialog.information(
                self,
                "同步完成",
                (
                    f"已同步 {result.get('synced', 0)} 篇 LitAI 论文。"
                    f"\n新建本地记录 {result.get('created', 0)} 篇。"
                    f"\n关联/复制 PDF {result.get('linked_files', 0)} 篇。"
                ),
            )

        def on_failed(message):
            ToastDialog.critical(self, "同步失败", str(message))

        def on_finished():
            self.btn_sync_litai.setEnabled(True)
            self.btn_sync_litai.setText("同步 LitAI 结果")
            if self.sync_thread is not None:
                self.sync_thread.deleteLater()
            self.sync_thread = None

        self.sync_thread.finished_ok.connect(on_success)
        self.sync_thread.failed.connect(on_failed)
        self.sync_thread.finished.connect(on_finished)
        self.sync_thread.start()

    def cleanup_thread(self, paper_id, thread):
        if paper_id in self.download_threads:
            del self.download_threads[paper_id]
        thread.deleteLater()

    def handle_download(self, paper):
        if paper.id in self.download_threads:
            ToastDialog.information(self, "提示", "该文献已在下载中，请稍候。")
            return

        display_title = build_display_title(paper.paper_number, paper.chinese_title, paper.title)
        identifier = paper.doi or paper.title
        if not identifier:
            ToastDialog.warning(self, "提示", "该文献缺少 DOI 或标题，暂时无法自动获取 PDF。")
            return

        self.ds.proxy = self.pm.current_config.get("proxy") if hasattr(self.pm, "current_config") else None
        dest_filename = self.pm.build_pdf_filename(paper.paper_number, paper.title)
        dest_path = os.path.join(self.pm.current_project_path, "papers", "pdf", dest_filename)

        for row in range(self.table.rowCount()):
            title_widget = self.table.cellWidget(row, 0)
            if title_widget and title_widget.text() == display_title:
                self.table.setItem(row, 4, QTableWidgetItem("正在下载..."))
                btn_widget = self.table.cellWidget(row, 5)
                if btn_widget:
                    for btn in btn_widget.findChildren(QPushButton):
                        if btn.text() == "下载":
                            btn.setEnabled(False)
                            btn.setText("下载中...")

        ToastDialog.information(self, "已启动", "多源下载已在后台启动，可以继续浏览页面。")

        thread = LibraryDownloadThread(self.ds, identifier, dest_path, paper.id, paper.doi, paper.title)
        thread.finished_success.connect(lambda dp, pid=paper.id, title=display_title: self.on_download_success(pid, title, dp))
        thread.finished_failed.connect(lambda err, pid=paper.id, title=display_title: self.on_download_failed(pid, title, err))
        thread.finished.connect(lambda t=thread, pid=paper.id: self.cleanup_thread(pid, t))
        self.download_threads[paper.id] = thread
        thread.start()

    def on_download_success(self, paper_id, paper_title, dest_path):
        try:
            with self.pm.get_session() as session:
                existing_file = session.exec(select(File).where(File.paper_id == paper_id)).first()
                if existing_file:
                    existing_file.file_path = dest_path
                    existing_file.status = "downloaded"
                    existing_file.original_url = paper_title
                else:
                    session.add(
                        File(
                            paper_id=paper_id,
                            file_type="pdf",
                            file_path=dest_path,
                            status="downloaded",
                            original_url=paper_title,
                        )
                    )
                session.commit()

            lit_ai_url = self.pm.current_config.get("literature_ai_url", "") if self.pm.current_config else ""
            if lit_ai_url:
                try:
                    with self.pm.get_session() as session:
                        paper_obj = session.get(Paper, paper_id)
                        if paper_obj:
                            self._send_to_literature_ai(paper_obj, dest_path, show_toast=False)
                except Exception:
                    pass

            self.library_changed.emit()
            self.refresh_stats_signal()
            self.refresh_list()
            ToastDialog.information(self, "下载成功", f"文献《{paper_title[:30]}...》已成功下载并入库。")
        except Exception as exc:
            ToastDialog.critical(self, "入库失败", f"PDF 已下载成功，但写入数据库时出错: {exc}")

    def on_download_failed(self, paper_id, paper_title, error):
        self.refresh_list()
        ToastDialog.critical(self, "下载失败", f"未能获取文献《{paper_title[:30]}...》的 PDF: {error}")

    def handle_delete(self, paper):
        display_title = build_display_title(paper.paper_number, paper.chinese_title, paper.title)
        reply = ToastDialog.question(self, "确认", f"确定从项目中删除《{display_title}》吗？")
        if not reply:
            return

        with self.pm.get_session() as session:
            file_records = session.exec(select(File).where(File.paper_id == paper.id)).all()
            for file_record in file_records:
                if file_record.file_path and os.path.exists(file_record.file_path):
                    try:
                        os.remove(file_record.file_path)
                    except OSError:
                        pass
                session.delete(file_record)
            session.delete(paper)
            session.commit()

        self.library_changed.emit()
        self.refresh_stats_signal()
        self.refresh_list()

    def refresh_stats_signal(self):
        parent = self.window()
        if hasattr(parent, "on_data_changed"):
            parent.on_data_changed()
        elif hasattr(parent, "refresh_stats"):
            parent.refresh_stats()


class LibraryDownloadThread(QThread):
    finished_success = Signal(str)
    finished_failed = Signal(str)

    def __init__(self, ds, identifier, dest_path, paper_id, paper_doi, paper_title):
        super().__init__()
        self.ds = ds
        self.identifier = identifier
        self.dest_path = dest_path
        self.paper_id = paper_id
        self.paper_doi = paper_doi
        self.paper_title = paper_title

    def run(self):
        import sys

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
            if hasattr(self.ds, "acquire_paper") and self.ds.acquire_paper(self.identifier, self.dest_path):
                self.finished_success.emit(self.dest_path)
                return

            if not self.paper_doi:
                self.finished_failed.emit("多源检索未成功，且该文献缺少 DOI，无法继续进行 OA 降级下载。")
                return

            logger.info("高级引擎未下载成功，尝试使用 Unpaywall 降级通道。")
            pdf_url = self.ds.get_oa_pdf_url(self.paper_doi)
            if pdf_url and self.ds.download_pdf(pdf_url, self.dest_path):
                self.finished_success.emit(self.dest_path)
                return

            self.finished_failed.emit("未能在多源通道或 OA 降级通道中找到该文献的 PDF。")
        except Exception as exc:
            self.finished_failed.emit(str(exc))
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
