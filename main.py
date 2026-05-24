import sys
from loguru import logger

# 保护打包无控制台 (pythonw / PyInstaller --noconsole) 环境下的 stdout/stderr，防止 print 导致 I/O 阻塞死锁
class SafeStream:
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

# 强制重定向标准输出，彻底阻断在 Windows GUI 环境下的 GIL 阻塞
sys.stdout = SafeStream("INFO")
sys.stderr = SafeStream("ERROR")


from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


def main():
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("Lit AI Collector")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

