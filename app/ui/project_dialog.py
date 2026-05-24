from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
)

from .toast_dialog import ToastDialog


class ProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("创建新项目")
        self.setFixedSize(450, 250)
        self.project_path = ""
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(30, 30, 30, 30)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("项目名称"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如：单原子催化剂研究")
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)

        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("保存位置"))
        self.path_display = QLineEdit()
        self.path_display.setReadOnly(True)
        path_layout.addWidget(self.path_display)

        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self.browse_path)
        path_layout.addWidget(btn_browse)
        layout.addLayout(path_layout)

        btn_confirm = QPushButton("立即创建并开始")
        btn_confirm.setObjectName("primary_btn")
        btn_confirm.clicked.connect(self.accept_creation)
        layout.addWidget(btn_confirm)

    def browse_path(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择项目保存目录")
        if dir_path:
            self.path_display.setText(dir_path)
            self.project_path = dir_path

    def accept_creation(self):
        if not self.name_input.text().strip():
            ToastDialog.warning(self, "提示", "项目名称不能为空")
            return
        if not self.project_path:
            ToastDialog.warning(self, "提示", "请选择保存路径")
            return
        self.accept()

    def get_data(self):
        return self.name_input.text().strip(), self.project_path
