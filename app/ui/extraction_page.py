import json

from PySide6.QtCore import QPropertyAnimation, QThread, Signal, QTimer, QEasingCurve, Qt
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
from sqlmodel import select

from ..core.models import File, Paper
from ..core.paper_naming import build_display_title
from ..services.literature_ai_client import LiteratureAIClient, generate_chinese_title
from .toast_dialog import ToastDialog


class CopyableLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setCursor(Qt.IBeamCursor)
        self.setWordWrap(False)
        self.setToolTip(text)


def format_json_to_markdown(data):
    md = []
    
    # Title
    md.append("# 📊 文献高精度 AI 抽取与结构化分析报告 (Comprehensive Analysis)\n")
    md.append("本报告由 LitAI Collector 智能文献抽取引擎自动生成。系统已对全文数据进行语义解析，并自动翻译为中文。\n\n")

    paper_type = data.get("paper_type", "Unknown")
    confidence = data.get("type_confidence", 0.0)
    md.append(f"**📚 文献学术分类 (Study Type):** `{paper_type}` (置信度: {confidence:.2f})\n\n")
    md.append("---\n")

    # 1. 小白模式总结 (Layman Summary)
    layman = data.get("layman_summary") or {}
    md.append("## 🧑‍🏫 小白模式总结 (Layman Summary)\n")
    md.append(f"**🌟 一句话总结 (One Sentence Takeaway):**\n> {layman.get('one_sentence_takeaway') or '无'}\n\n")
    md.append(f"**🌍 实际应用价值 (Real World Impact):**\n> {layman.get('real_world_impact') or '无'}\n\n")
    md.append("---\n")

    # 2. 论文写作逻辑 (Writing Logic)
    writing = data.get("writing_logic") or {}
    md.append("## 📝 论文写作思路与策略 (Writing Strategy)\n")
    md.append(f"**🔍 研究痛点与包装 (Research Gap Framing):**\n> {writing.get('research_gap_framing') or '无'}\n\n")
    md.append(f"**🎯 核心假设 (Core Hypothesis):**\n> {writing.get('core_hypothesis') or '无'}\n\n")
    
    chain = writing.get("evidence_chain") or []
    if chain:
        md.append("**🔗 论证链条 (Evidence Chain):**\n")
        for step in chain:
            if isinstance(step, dict):
                md.append(f"- {step.get('step_description', '')}")
            else:
                md.append(f"- {step}")
        md.append("\n")
        
    md.append(f"**🏁 结论升华 (Conclusion Mapping):**\n> {writing.get('conclusion_mapping') or '无'}\n\n")
    md.append("---\n")

    # 3. 实验方法细节 (Experimental Details)
    exp_details = data.get("experimental_details") or {}
    md.append("## 🧪 实验方法与步骤 (Experimental Details)\n")
    md.append(f"**🛠️ 合成步骤 (Synthesis Steps):**\n{exp_details.get('synthesis_steps') or '无'}\n\n")
    
    char_methods = exp_details.get("characterization_methods") or []
    if char_methods:
        md.append("**🔬 表征手段 (Characterization Methods):**\n")
        for m in char_methods:
            md.append(f"- {m}")
        md.append("\n")
        
    perf_tests = exp_details.get("performance_tests") or []
    if perf_tests:
        md.append("**⚡ 性能测试 (Performance Tests):**\n")
        for p in perf_tests:
            md.append(f"- {p}")
        md.append("\n")
    md.append("---\n")

    # 4. 计算方法细节 (Computational Details)
    comp_details = data.get("computational_details") or {}
    md.append("## 💻 计算模拟参数 (Computational Details)\n")
    md.append(f"- **软件与泛函 (Software & Functional):** {comp_details.get('software_and_functional') or '无'}\n")
    md.append(f"- **截断能与 K点 (Cutoff & K-Points):** {comp_details.get('cutoff_energy_and_kpoints') or '无'}\n")
    md.append(f"- **溶剂化模型 (Solvation Model):** {comp_details.get('solvation_model') or '无'}\n\n")
    md.append("---\n")
    
    # 5. 实验与计算结果 (Results)
    exp_results = data.get("experimental_results") or {}
    md.append("## 🏆 核心发现与结果 (Key Results)\n")
    md.append(f"**📊 关键性能指标 (Key Performance Metrics):**\n{exp_results.get('key_performance_metrics') or '无'}\n\n")
    md.append(f"**🔬 结构与机理发现 (Characterization Findings):**\n{exp_results.get('characterization_findings') or '无'}\n\n")
    
    # FIX: 使用正确的字段名 computational_results 而非 dft_results
    comp_results = data.get("computational_results") or []
    if comp_results:
        md.append("### ⚡ 第一性原理计算结果 (Computational Results)\n")
        md.append("| 物理量类别 (Category) | 吸附质/中间体 (Species) | 反应步骤 (Reaction Step) | 数值 (Value) | 单位 (Unit) | 数据来源 (Source) |\n")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for res in comp_results:
            cat_cn = res.get("category", "计算数值")
            species = res.get("species") or res.get("adsorbate") or "N/A"
            step = res.get("reaction_step") or "N/A"
            val = res.get("value")
            val_str = f"**{val}**" if val is not None else "N/A"
            unit = res.get("unit") or "N/A"
            source = res.get("source") or "N/A"
            md.append(f"| {cat_cn} | `{species}` | {step} | {val_str} | {unit} | {source} |\n")
        md.append("\n")
        
    return "".join(md)


class JsonViewerDialog(QDialog):
    def __init__(self, title: str, content: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(850, 680)
        
        layout = QVBoxLayout(self)
        
        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        
        font = self.editor.font()
        font.setPointSize(10.5)
        self.editor.setFont(font)
        
        is_json = False
        self.formatted_md = ""
        self.raw_content = content
        
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                is_json = True
                self.formatted_md = format_json_to_markdown(data)
        except Exception:
            pass
            
        if is_json:
            self.editor.setMarkdown(self.formatted_md)
        else:
            self.editor.setPlainText(content)
            
        layout.addWidget(self.editor)
        
        btn_layout = QHBoxLayout()
        
        self.toggle_btn = QPushButton("显示原始 JSON (Show Raw JSON)")
        self.toggle_btn.clicked.connect(self.toggle_format)
        btn_layout.addWidget(self.toggle_btn)
        
        close_btn = QPushButton("关闭 (Close)")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        
        layout.addLayout(btn_layout)
        
        self.is_raw = False
        if not is_json:
            self.toggle_btn.setEnabled(False)

    def toggle_format(self):
        if self.is_raw:
            self.editor.setMarkdown(self.formatted_md)
            self.toggle_btn.setText("显示原始 JSON (Show Raw JSON)")
            self.is_raw = False
        else:
            self.editor.setPlainText(self.raw_content)
            self.toggle_btn.setText("显示格式化报告 (Show Formatted Report)")
            self.is_raw = True


class RemoteSyncThread(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, pm, config, paper_id: str):
        super().__init__()
        self.pm = pm
        self.config = config
        self.paper_id = paper_id

    def run(self):
        try:
            with self.pm.get_session() as session:
                paper = session.get(Paper, self.paper_id)
                file_record = session.exec(select(File).where(File.paper_id == self.paper_id)).first()
                if not paper or not file_record:
                    raise ValueError("当前论文还没有可用的 PDF 文件。")
                lit_ai_url = self.config.get("literature_ai_url", "").strip()
                if not lit_ai_url:
                    raise ValueError("请先在系统设置中配置 Literature AI 服务地址。")
                client = LiteratureAIClient(lit_ai_url)
                payload = {
                    "pdf_path": file_record.file_path,
                    "title": paper.title,
                    "doi": paper.doi,
                    "authors": [a.strip() for a in (paper.authors or "").replace(";", ",").split(",") if a.strip()],
                    "year": paper.year,
                    "journal": paper.journal,
                    "abstract": paper.abstract,
                }
                existing = None
                if paper.remote_paper_id:
                    existing = {"id": paper.remote_paper_id}
                else:
                    existing = client.find_by_source_path(file_record.file_path)
                if existing:
                    remote_id = str(existing["id"])
                else:
                    ingest_result = client.ingest_path(payload)
                    remote_id = str(ingest_result["paper_id"])
                detail = client.get_paper(remote_id)
                paper.remote_paper_id = remote_id
                if detail.get("title"):
                    paper.title = detail["title"]
                if detail.get("abstract") and not paper.abstract:
                    paper.abstract = detail["abstract"]
                try:
                    chinese_title = generate_chinese_title(
                        detail.get("title") or paper.title or "",
                        api_key=self.config.get("api_key", ""),
                        base_url=self.config.get("base_url", ""),
                        model=self.config.get("llm_model", "gpt-4o-mini"),
                    )
                except Exception:
                    chinese_title = None
                if chinese_title:
                    paper.chinese_title = chinese_title
                session.add(paper)
                session.commit()
            self.finished_ok.emit("已同步到 Literature AI，并完成远端解析。")
        except Exception as exc:
            self.failed.emit(str(exc))


class RemoteExtractThread(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, pm, config, paper_id: str):
        super().__init__()
        self.pm = pm
        self.config = config
        self.paper_id = paper_id

    def run(self):
        try:
            with self.pm.get_session() as session:
                paper = session.get(Paper, self.paper_id)
                if not paper:
                    raise ValueError("未找到论文记录。")
                lit_ai_url = self.config.get("literature_ai_url", "").strip()
                if not lit_ai_url:
                    raise ValueError("请先在系统设置中配置 Literature AI 服务地址。")
                client = LiteratureAIClient(lit_ai_url)
                if not paper.remote_paper_id:
                    raise ValueError("该论文尚未同步到 Literature AI，请先点击解析。")
                result = client.extract_paper(paper.remote_paper_id)
            self.finished_ok.emit(f"远端抽取完成：DFT {result.get('dft_results', 0)}，机理 {result.get('mechanism_claims', 0)}。")
        except Exception as exc:
            self.failed.emit(str(exc))


class ExtractionPage(QWidget):
    data_changed = Signal()

    def __init__(self, pm, config):
        super().__init__()
        self.pm = pm
        self.config = config
        self.worker = None
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

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)

        title_block = QVBoxLayout()
        title = QLabel("AI 抽取工作台")
        title.setObjectName("pageTitle")
        subtitle = QLabel("在这里完成 PDF 解析、结构化抽取和结果查看。")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header_row.addLayout(title_block, stretch=1)



        layout.addLayout(header_row)

        card = QFrame()
        card.setObjectName("tableCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        self.status_label = QLabel("请选择一篇已有 PDF 的文献，同步到 Literature AI 后再查看和重跑抽取。")
        self.status_label.setObjectName("pageSubtitle")
        card_layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["标题", "PDF", "编号", "LitAI", "结果", "解析", "抽取"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(52)
        card_layout.addWidget(self.table)
        layout.addWidget(card)
        self.animate_entry(card, delay_ms=80, duration_ms=500)

    def refresh_project_context(self):
        pass

    def refresh_list(self):
        self.table.setRowCount(0)
        if not self.pm.engine:
            return

        with self.pm.get_session() as session:
            papers = session.exec(select(Paper).order_by(Paper.created_at.desc())).all()
            for row, paper in enumerate(papers):
                self.table.insertRow(row)
                file_record = session.exec(select(File).where(File.paper_id == paper.id)).first()

                title_label = CopyableLabel(build_display_title(paper.paper_number, paper.chinese_title, paper.title))
                title_label.setMargin(6)
                self.table.setCellWidget(row, 0, title_label)
                self.table.setItem(row, 1, QTableWidgetItem("已就绪" if file_record else "缺少 PDF"))
                self.table.setItem(row, 2, QTableWidgetItem(f"{paper.paper_number:03d}" if paper.paper_number else "-"))
                self.table.setItem(row, 3, QTableWidgetItem("已同步" if paper.remote_paper_id else "未同步"))

                parse_btn = QPushButton("解析")
                parse_btn.setFixedSize(78, 32)
                parse_btn.clicked.connect(lambda _, pid=paper.id: self.start_parse(pid))
                if not file_record:
                    parse_btn.setEnabled(False)
                self.table.setCellWidget(row, 5, parse_btn)

                extract_btn = QPushButton("抽取")
                extract_btn.setObjectName("primary_btn")
                extract_btn.setFixedSize(78, 32)
                extract_btn.clicked.connect(lambda _, pid=paper.id: self.start_extract(pid))
                if not file_record or not paper.remote_paper_id:
                    extract_btn.setEnabled(False)
                self.table.setCellWidget(row, 6, extract_btn)

                if paper.remote_paper_id:
                    view_btn = QPushButton("查看结果")
                    view_btn.setFixedHeight(32)
                    view_btn.clicked.connect(lambda _, p=paper: self.show_result(p))
                    self.table.setCellWidget(row, 4, view_btn)
                else:
                    self.table.setItem(row, 4, QTableWidgetItem("-"))

    def start_parse(self, paper_id: str):
        if self.worker and self.worker.isRunning():
            ToastDialog.warning(self, "提示", "已有解析或抽取任务正在后台运行，请等待其完成后再启动新任务。")
            return
        self.status_label.setText("正在同步到 Literature AI 并执行远端解析，请稍候...")
        self.worker = RemoteSyncThread(self.pm, self.config, paper_id)
        self.worker.finished_ok.connect(self.on_worker_success)
        self.worker.failed.connect(self.on_worker_failed)
        self.worker.start()

    def start_extract(self, paper_id: str):
        if self.worker and self.worker.isRunning():
            ToastDialog.warning(self, "提示", "已有解析或抽取任务正在后台运行，请等待其完成后再启动新任务。")
            return
        self.status_label.setText("正在重跑 Literature AI 远端抽取，请稍候...")
        self.worker = RemoteExtractThread(self.pm, self.config, paper_id)
        self.worker.finished_ok.connect(self.on_worker_success)
        self.worker.failed.connect(self.on_worker_failed)
        self.worker.start()

    def on_worker_success(self, message: str):
        self.status_label.setText(message)
        self.refresh_list()
        self.data_changed.emit()
        ToastDialog.information(self, "成功", message)

    def on_worker_failed(self, message: str):
        self.status_label.setText("执行失败，请检查提示信息。")
        ToastDialog.critical(self, "失败", message)

    def show_result(self, paper):
        lit_ai_url = self.config.get("literature_ai_url", "").strip()
        if not lit_ai_url or not paper.remote_paper_id:
            ToastDialog.warning(self, "提示", "该论文尚未同步到 Literature AI。")
            return
        try:
            detail = LiteratureAIClient(lit_ai_url).get_paper(paper.remote_paper_id)
            payload = detail.get("comprehensive_analysis")
            if payload is None:
                payload = detail
            formatted = json.dumps(payload, ensure_ascii=False, indent=2)
            JsonViewerDialog(
                f"抽取结果 - {build_display_title(paper.paper_number, paper.chinese_title, paper.title)[:32]}",
                formatted,
                self,
            ).exec()
        except Exception as exc:
            ToastDialog.critical(self, "失败", str(exc))

    def cleanup(self):
        if self.worker and self.worker.isRunning():
            self.worker.quit()
            if not self.worker.wait(2000):
                self.worker.terminate()
                self.worker.wait(2000)
        self.worker = None
