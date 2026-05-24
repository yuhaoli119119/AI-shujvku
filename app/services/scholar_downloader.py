# -*- coding: utf-8 -*-
"""PDF 下载服务 - 基于 PyPaperBot 的 SciHub/SciDB 下载功能"""
import os
import re
import time
import random
import requests
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from loguru import logger


class NetInfo:
    SciHub_URL = None
    SciDB_URL = "https://annas-archive.se/scidb/"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    SciHub_URLs_repo = "https://sci-hub.41610.org/"


def get_schihub_pdf(html):
    """从 SciHub 页面提取 PDF 链接"""
    soup = BeautifulSoup(html, "html.parser")
    result = None
    
    iframe = soup.find(id='pdf')
    plugin = soup.find(id='plugin')
    download_scidb = soup.find("a", text=lambda text: text and "Download" in text, href=re.compile(r"\.pdf$"))
    embed_scihub = soup.find("embed")
    
    if iframe is not None:
        result = iframe.get("src")
    if plugin is not None and result is None:
        result = plugin.get("src")
    if result is not None and result[0] != "h":
        result = "https:" + result
    if download_scidb is not None and result is None:
        result = download_scidb.get("href")
    if embed_scihub is not None and result is None:
        result = embed_scihub.get("original-url")
    
    return result


def urljoin(base, url):
    """安全的 URL 拼接"""
    if url and not url.startswith(('http://', 'https://')):
        return base.rstrip('/') + '/' + url.lstrip('/')
    return url


def get_save_dir(folder, fname):
    """获取不重复的保存路径"""
    dir_ = os.path.join(folder, fname)
    n = 1
    while os.path.exists(dir_):
        n += 1
        name, ext = os.path.splitext(fname)
        dir_ = os.path.join(folder, f"({n}){name}{ext}")
    return dir_


def sanitize_filename(title, use_doi=False, doi=None):
    """清理文件名"""
    if use_doi and doi:
        return re.sub(r'[^\w\-_. ]', '_', doi) + ".pdf"
    else:
        # 替换不安全字符
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
        safe_title = re.sub(r'\s+', '_', safe_title)
        return safe_title[:200] + ".pdf"


class PDFDownloader:
    def __init__(self, proxy: str = None, scihub_url: str = None, scidb_url: str = None):
        self.proxy = proxy
        self.headers = NetInfo.HEADERS
        
        if scihub_url:
            NetInfo.SciHub_URL = scihub_url
        elif not NetInfo.SciHub_URL:
            self._set_scihub_url()
            
        if scidb_url:
            NetInfo.SciDB_URL = scidb_url
            
        logger.info(f"使用 Sci-Hub: {NetInfo.SciHub_URL}")
        logger.info(f"使用 Sci-DB: {NetInfo.SciDB_URL}")
    
    def _make_request(self, url, stream=False):
        """发送 HTTP 请求"""
        kwargs = {
            "headers": self.headers,
            "timeout": 60,
            "allow_redirects": True
        }
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        if stream:
            kwargs["stream"] = True
        return requests.get(url, **kwargs)
    
    def _set_scihub_url(self):
        """自动查找可用的 SciHub 镜像"""
        try:
            r = requests.get(NetInfo.SciHub_URLs_repo, headers=self.headers, timeout=10)
            from .scholar_service import scihub_urls
            links = scihub_urls(r.text)
            for link in links:
                try:
                    r = requests.get(link, headers=self.headers, timeout=10)
                    if r.status_code == 200:
                        NetInfo.SciHub_URL = link
                        return
                except:
                    pass
        except Exception as e:
            logger.warning(f"查找 SciHub 镜像失败: {e}")
        NetInfo.SciHub_URL = "https://sci-hub.st"
    
    def download(self, doi: str = None, scholar_link: str = None, 
                 title: str = "paper", save_dir: str = None,
                 use_doi_as_filename: bool = False) -> dict:
        """
        下载论文 PDF (全面升级：整合了高阶的 PaperFinder 20+ 多源并发引擎)
        
        Args:
            doi: 论文 DOI
            scholar_link: Google Scholar 链接
            title: 论文标题（用于文件名）
            save_dir: 保存目录
            use_doi_as_filename: 是否用 DOI 作为文件名
            
        Returns:
            dict: 包含下载结果的字典
        """
        result = {
            "success": False,
            "path": None,
            "source": None,
            "error": None
        }
        
        if not save_dir:
            save_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(save_dir, exist_ok=True)
        
        # 1. 尝试使用高阶 PaperFinder 引擎进行深度多源获取
        try:
            from paper_finder import PaperFinder
            logger.info(f"🚀 [PaperFinder] 启动最高权限文献下载: {title[:50]}... (代理: {self.proxy})")
            finder = PaperFinder(silent_init=True, proxy=self.proxy)
            
            # 拼接最佳解析标识（优先 DOI，其次 URL 链接，最后 Title）
            ref_str = doi or scholar_link or title
            if ref_str:
                pf_result = finder.find(ref_str, output_dir=Path(save_dir))
                if pf_result.success:
                    result["success"] = True
                    result["path"] = str(pf_result.filepath) if pf_result.filepath else None
                    result["source"] = pf_result.source or "PaperFinder"
                    logger.info(f"✓ [PaperFinder] 获取成功: {result['path']} (源: {result['source']})")
                    return result
                else:
                    logger.warning(f"⚠ [PaperFinder] 文献获取失败: {pf_result.error}，尝试原版通道降级下载...")
        except Exception as pf_err:
            logger.error(f"PaperFinder 运行异常: {pf_err}，尝试原版通道降级处理...")
            
        # 2. 原版降级下载逻辑（保证极佳的向下兼容性与容灾性）
        fname = sanitize_filename(title, use_doi_as_filename, doi)
        save_path = get_save_dir(save_dir, fname)
        
        failed = 0
        max_attempts = 5
        
        while failed < max_attempts:
            try:
                url = ""
                dwn_source = ""
                
                if failed == 0 and doi:
                    # 尝试 SciDB (Annas Archive)
                    url = urljoin(NetInfo.SciDB_URL, doi)
                    dwn_source = "SciDB"
                elif failed == 1 and doi:
                    # 尝试 SciHub
                    url = urljoin(NetInfo.SciHub_URL, doi)
                    dwn_source = "SciHub"
                elif failed == 2 and scholar_link:
                    # 通过 Scholar 链接尝试
                    url = urljoin(NetInfo.SciHub_URL, scholar_link)
                    dwn_source = "Scholar-SciHub"
                elif failed == 3 and scholar_link and scholar_link.endswith('.pdf'):
                    # 直接 PDF 链接
                    url = scholar_link
                    dwn_source = "Direct"
                else:
                    failed += 1
                    continue
                
                if not url:
                    failed += 1
                    continue
                
                logger.info(f"尝试从降级源 {dwn_source} 下载: {title[:50]}...")
                
                response = self._make_request(url)
                content_type = response.headers.get('content-type', '')
                
                # 检查是否是 PDF
                is_pdf = 'application/pdf' in content_type or 'application/octet-stream' in content_type
                
                # 如果是 HTML 页面，尝试提取 PDF 链接
                if not is_pdf and ('text/html' in content_type or not content_type):
                    pdf_link = get_schihub_pdf(response.text)
                    if pdf_link:
                        response = self._make_request(pdf_link)
                        content_type = response.headers.get('content-type', '')
                        is_pdf = 'application/pdf' in content_type or 'application/octet-stream' in content_type
                
                if is_pdf:
                    with open(save_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    result["success"] = True
                    result["path"] = save_path
                    result["source"] = dwn_source
                    logger.info(f"下载成功: {save_path}")
                    return result
                else:
                    logger.debug(f"非 PDF 响应，尝试下一个源")
                    
            except Exception as e:
                logger.debug(f"下载尝试 {failed + 1} 失败: {e}")
            
            failed += 1
            time.sleep(random.uniform(1, 3))
        
        result["error"] = "所有下载源均失败"
        logger.error(f"下载失败: {title[:50]}")
        return result
    
    def batch_download(self, papers: list, save_dir: str = None) -> list:
        """
        批量下载论文
        
        Args:
            papers: 论文列表，每项包含 doi, title, scholar_link
            save_dir: 保存目录
            
        Returns:
            list: 下载结果列表
        """
        results = []
        total = len(papers)
        
        for i, paper in enumerate(papers):
            logger.info(f"下载进度: {i + 1}/{total}")
            
            result = self.download(
                doi=paper.get("doi"),
                scholar_link=paper.get("scholar_link"),
                title=paper.get("title", "paper"),
                save_dir=save_dir
            )
            results.append({
                "title": paper.get("title"),
                **result
            })
            
            # 避免请求过快
            if i < total - 1:
                time.sleep(random.uniform(2, 5))
        
        return results
