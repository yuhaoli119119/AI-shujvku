import os
import httpx
from PySide6.QtCore import QThread, Signal

class DownloadThread(QThread):
    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, url, save_path, proxy=None):
        super().__init__()
        self.url = url
        self.save_path = save_path
        self.proxy = proxy

    def run(self):
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            
            with httpx.Client(proxies=self.proxy, timeout=60.0) as client:
                with client.stream("GET", self.url, follow_redirects=True) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("Content-Length", 0))
                    downloaded = 0
                    
                    with open(self.save_path, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            f.write(chunk)
                            if total > 0:
                                downloaded += len(chunk)
                                self.progress.emit(int(downloaded / total * 100))
            
            self.finished.emit(True, self.save_path)
        except Exception as e:
            self.finished.emit(False, str(e))
