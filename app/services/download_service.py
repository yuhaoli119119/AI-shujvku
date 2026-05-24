import os
import httpx
from loguru import logger

class DownloadService:
    def __init__(self, proxy=None):
        self.proxy = proxy
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def acquire_paper(self, ref_str: str, dest_path: str) -> bool:
        """
        使用 PaperFinder 高级引擎，通过 DOI、标题、URL 等任何标识获取文献 PDF
        """
        if not ref_str: return False
        
        try:
            from paper_finder import PaperFinder
            from pathlib import Path
            logger.info(f"🚀 [PaperFinder] 启动高级文献获取，标识: {ref_str} (代理: {self.proxy})")
            
            # 实例化 PaperFinder，传入代理
            finder = PaperFinder(silent_init=True, proxy=self.proxy)
            
            # 由于 PaperFinder 会将文件下载到指定目录，我们可以将 dest_path 分解为目录和文件名
            dest_dir = os.path.dirname(dest_path)
            
            # 运行高级查找与下载
            result = finder.find(ref_str, output_dir=Path(dest_dir))
            
            if result.success and result.filepath and os.path.exists(result.filepath):
                # 如果生成的文件名与目标路径不同，我们将其复制重命名为 dest_path
                if str(result.filepath) != str(dest_path):
                    import shutil
                    os.makedirs(dest_dir, exist_ok=True)
                    shutil.copy2(result.filepath, dest_path)
                    try:
                        os.remove(result.filepath) # 删除原下载的文件，保持 papers/pdf 规范整洁
                    except:
                        pass
                logger.success(f"[PaperFinder] 获取成功，已关联并重命名为: {dest_path}")
                return True
            else:
                logger.warning(f"[PaperFinder] 下载未成功: {result.error}")
        except Exception as e:
            logger.error(f"[PaperFinder] 运行出错: {e}")
        return False

    def download_pdf(self, url: str, dest_path: str) -> bool:
        """
        根据 URL 下载 PDF 文件到指定路径
        """
        if not url: return False
        
        try:
            logger.info(f"正在尝试下载 PDF: {url}")
            with httpx.Client(proxy=self.proxy, headers=self.headers, timeout=60.0, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code == 200 and b"%PDF" in resp.content[:100]:
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    with open(dest_path, "wb") as f:
                        f.write(resp.content)
                    logger.success(f"PDF 下载成功: {dest_path}")
                    return True
                else:
                    logger.warning(f"下载失败或内容不是有效的 PDF, 状态码: {resp.status_code}")
        except Exception as e:
            logger.error(f"下载异常: {e}")
            
        return False

    def get_oa_pdf_url(self, doi: str) -> str:
        """
        通过 Unpaywall API 寻找免费的 PDF 链接
        """
        if not doi: return None
        api_url = f"https://api.unpaywall.org/v2/{doi}?email=unbearable.lightness@gmail.com"
        
        try:
            with httpx.Client(proxy=self.proxy, timeout=10.0) as client:
                resp = client.get(api_url)
                if resp.status_code == 200:
                    data = resp.json()
                    best_oa = data.get("best_oa_location")
                    if best_oa:
                        return best_oa.get("url_for_pdf")
        except: pass
        return None
