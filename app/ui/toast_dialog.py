import random
from PySide6.QtCore import QPoint, QPropertyAnimation, Qt, QTimer, QEasingCurve, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Particle:
    """粒子：关闭时向四周飞散并逐渐透明"""

    def __init__(self, x, y, color):
        self.x = x
        self.y = y
        self.vx = random.uniform(-3.5, 3.5)
        self.vy = random.uniform(-4.5, -0.5)
        self.radius = random.uniform(2.0, 5.5)
        self.alpha = 255
        self.color = color
        self.decay = random.uniform(4, 9)

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += 0.15  # 重力
        self.alpha -= self.decay
        if self.alpha < 0:
            self.alpha = 0

    def is_dead(self):
        return self.alpha <= 0

    def draw(self, painter: QPainter):
        if self.alpha <= 0:
            return
        c = QColor(self.color)
        c.setAlpha(int(self.alpha))
        painter.setBrush(c)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPoint(int(self.x), int(self.y)), int(self.radius), int(self.radius))


class ToastDialog(QDialog):
    """自定义主题弹出框，带入场缩放 + 粒子消散退场效果"""

    closed = Signal()

    ICON_INFO = "info"
    ICON_SUCCESS = "success"
    ICON_WARNING = "warning"
    ICON_ERROR = "error"
    ICON_QUESTION = "question"

    def __init__(
        self,
        parent=None,
        title: str = "提示",
        message: str = "",
        icon_type: str = ICON_INFO,
        buttons=None,
        theme_name: str = None,
    ):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        
        is_modal = (icon_type == self.ICON_QUESTION)
        self.setModal(is_modal)
        self.setFixedWidth(420)

        self._icon_type = icon_type
        self._particles = []
        self._closing = False
        self._result = None

        # ── 动态取色：优先使用传入主题，其次沿 parent 链查找 MainWindow 的 config ──
        from .theme_manager import get_theme_palette

        if theme_name is None:
            p = parent
            while p:
                if hasattr(p, "config"):
                    theme_name = p.config.get("theme", "Neumorphism")
                    break
                p = p.parent()
        t = get_theme_palette(theme_name or "Neumorphism")

        self._panel = t.panel
        self._panel_alt = t.panel_alt
        self._border = t.border
        self._text = t.text
        self._text_muted = t.text_muted
        self._accent = t.accent
        self._accent_soft = t.accent_soft
        self._accent_alt = t.accent_alt
        self._danger = t.danger
        self._success = t.success
        self._warning = t.warning

        self._setup_icon_color()
        self._build_ui(title, message, buttons)
        self._animate_in()

        # 如果是 modeless 且没有交互按钮，则启动 3 秒自动关闭定时器，使用子 QTimer 确保销毁安全
        if not is_modal and not buttons:
            self._auto_close_timer = QTimer(self)
            self._auto_close_timer.setSingleShot(True)
            self._auto_close_timer.timeout.connect(self._start_close)
            self._auto_close_timer.start(3000)

    def _setup_icon_color(self):
        mapping = {
            self.ICON_INFO: (self._accent, "ℹ"),
            self.ICON_SUCCESS: (self._success, "✓"),
            self.ICON_WARNING: (self._warning, "⚠"),
            self.ICON_ERROR: (self._danger, "✕"),
            self.ICON_QUESTION: (self._accent, "?"),
        }
        self._icon_color, self._icon_char = mapping.get(self._icon_type, mapping[self.ICON_INFO])

    def _build_ui(self, title, message, buttons):
        self.container = QWidget(self)
        self.container.setObjectName("toast_container")
        self.container.setStyleSheet(f"""
            #toast_container {{
                background-color: {self._panel};
                border: 1px solid {self._border};
                border-radius: 18px;
                /* 用 CSS 模拟投影，避免 QGraphicsDropShadowEffect 在 setStyleSheet 时触发重绘卡死 */
            }}
        """)

        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # 顶部：图标 + 标题
        header = QHBoxLayout()
        header.setSpacing(12)

        icon_lbl = QLabel(self._icon_char)
        icon_lbl.setStyleSheet(f"""
            color: {self._icon_color};
            background-color: transparent;
            font-size: 22px;
            font-weight: 700;
            min-width: 36px;
            max-width: 36px;
            min-height: 36px;
            max-height: 36px;
            border-radius: 18px;
            border: 2px solid {self._icon_color};
        """)
        icon_lbl.setAlignment(Qt.AlignCenter)
        header.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"""
            color: {self._text};
            background-color: transparent;
            font-size: 16px;
            font-weight: 700;
        """)
        header.addWidget(title_lbl, stretch=1)

        # 关闭按钮
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: none;
                border-radius: 14px;
                color: {self._text_muted};
                font-size: 14px;
            }}
            QPushButton:hover {{
                background-color: {self._panel_alt};
                color: {self._text};
            }}
        """)
        close_btn.clicked.connect(self._start_close)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # 消息正文
        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"""
            color: {self._text_muted};
            background-color: transparent;
            font-size: 14px;
            line-height: 1.6;
        """)
        layout.addWidget(msg_lbl)

        # 按钮区
        if buttons:
            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            for btn_text, btn_role in buttons:
                btn = QPushButton(btn_text)
                is_primary = btn_role == "primary"
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {self._accent if is_primary else self._panel_alt};
                        color: {'#FFFFFF' if is_primary else self._text};
                        border: {'none' if is_primary else f'1px solid {self._border}'};
                        border-radius: 10px;
                        padding: 8px 22px;
                        font-weight: 600;
                        font-size: 13px;
                    }}
                    QPushButton:hover {{
                        background-color: {self._accent_alt if is_primary else self._border};
                    }}
                """)
                btn.clicked.connect(lambda _, r=btn_role: self._on_button(r))
                btn_layout.addWidget(btn)
            layout.addLayout(btn_layout)

        container_layout = QVBoxLayout(self)
        container_layout.setContentsMargins(12, 12, 12, 12)
        container_layout.addWidget(self.container)

    def _animate_in(self):
        self.setWindowOpacity(0.0)
        self.anim_opacity = QPropertyAnimation(self, b"windowOpacity")
        self.anim_opacity.setDuration(280)
        self.anim_opacity.setStartValue(0.0)
        self.anim_opacity.setEndValue(1.0)
        self.anim_opacity.setEasingCurve(QEasingCurve.OutCubic)

        # 居中
        geo = self.geometry()
        if self.parent():
            parent_pos = self.parent().mapToGlobal(QPoint(0, 0))
            x = parent_pos.x() + (self.parent().width() - geo.width()) // 2
            y = parent_pos.y() + (self.parent().height() - geo.height()) // 2
            self.move(x, y)
        else:
            screen = QApplication.primaryScreen().geometry()
            x = screen.x() + (screen.width() - geo.width()) // 2
            y = screen.y() + (screen.height() - geo.height()) // 2
            self.move(x, y)

        self.anim_opacity.start()

    def _on_button(self, role):
        self._result = role
        self._start_close()

    def _start_close(self):
        if self._closing:
            return
        self._closing = True
        # 生成粒子
        geo = self.container.geometry()
        center = QPoint(geo.width() // 2, geo.height() // 2)
        for _ in range(50):
            self._particles.append(Particle(center.x(), center.y(), self._icon_color))
        self.container.hide()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_particles)
        self._timer.start(16)

    def _update_particles(self):
        for p in self._particles:
            p.update()
        self._particles = [p for p in self._particles if not p.is_dead()]
        self.update()

        if not self._particles:
            self._timer.stop()
            self.closed.emit()
            self.accept() if self._result else self.reject()
            self.close()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._closing and self._particles:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            for p in self._particles:
                p.draw(painter)
            painter.end()

    @staticmethod
    def information(parent, title, message, theme_name=None):
        dlg = ToastDialog(parent, title, message, icon_type=ToastDialog.ICON_SUCCESS, theme_name=theme_name)
        if parent:
            if not hasattr(parent, "_active_toasts"):
                parent._active_toasts = []
            parent._active_toasts.append(dlg)
            dlg.closed.connect(lambda: parent._active_toasts.remove(dlg) if dlg in parent._active_toasts else None)
        dlg.show()
        return dlg

    @staticmethod
    def warning(parent, title, message, theme_name=None):
        dlg = ToastDialog(parent, title, message, icon_type=ToastDialog.ICON_WARNING, theme_name=theme_name)
        if parent:
            if not hasattr(parent, "_active_toasts"):
                parent._active_toasts = []
            parent._active_toasts.append(dlg)
            dlg.closed.connect(lambda: parent._active_toasts.remove(dlg) if dlg in parent._active_toasts else None)
        dlg.show()
        return dlg

    @staticmethod
    def critical(parent, title, message, theme_name=None):
        dlg = ToastDialog(parent, title, message, icon_type=ToastDialog.ICON_ERROR, theme_name=theme_name)
        if parent:
            if not hasattr(parent, "_active_toasts"):
                parent._active_toasts = []
            parent._active_toasts.append(dlg)
            dlg.closed.connect(lambda: parent._active_toasts.remove(dlg) if dlg in parent._active_toasts else None)
        dlg.show()
        return dlg

    @staticmethod
    def question(parent, title, message, theme_name=None):
        dlg = ToastDialog(
            parent,
            title,
            message,
            icon_type=ToastDialog.ICON_QUESTION,
            buttons=[("是", "primary"), ("否", "secondary")],
            theme_name=theme_name,
        )
        dlg.exec()
        return dlg._result == "primary"
