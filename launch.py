import sys
from pathlib import Path

# Ensure app/ is importable regardless of CWD
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from app.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Lit AI Collector")
    app.setApplicationDisplayName("Lit AI Collector")
    app.setOrganizationName("lit-ai")

    base_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    icon_path = base_dir / "big_big_wolf.png"
    app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
