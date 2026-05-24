# -*- coding: utf-8 -*-
"""Google Scholar 搜索服务 - 基于 PyPaperBot"""
import time
import requests
import functools
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
from loguru import logger


class NetInfo:
    SciHub_URL = None
    SciDB_URL = "https://annas-archive.se/scidb/"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    SciHub_URLs_repo = "https://sci-hub.41610.org/"


def schoolar_parser(html):
    """解析 Google Scholar 搜索结果页面"""
    result = []
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.findAll("div", class_="gs_r gs_or gs_scl"):
        if not _is_book(element):
            title = None
            link = None
            link_pdf = None
            cites = None
            year = None
            authors = None
            for h3 in element.findAll("h3", class_="gs_rt"):
                for a in h3.findAll("a"):
                    title = a.text
                    link = a.get("href")
                    break
            for a in element.findAll("a"):
                if "Cited by" in a.text:
                    try:
                        cites = int(re.search(r'\d+', a.text).group())
                    except:
                        cites = None
                if "[PDF]" in a.text:
                    link_pdf = a.get("href")
            for div in element.findAll("div", class_="gs_a"):
                try:
                    parts = div.text.replace('\u00A0', ' ').split(" - ")
                    if len(parts) >= 2:
                        authors = parts[0]
                        source_and_year = parts[-1]
                        if not authors.strip().endswith('\u2026'):
                            authors = authors.replace(', ', ';')
                        else:
                            authors = None
                        try:
                            year = int(source_and_year[-4:])
                            if not (1000 <= year <= 3000):
                                year = None
                            else:
                                year = str(year)
                        except ValueError:
                            year = None
                except ValueError:
                    continue
            if title is not None:
                result.append({
                    'title': title,
                    'link': link,
                    'cites': cites,
                    'link_pdf': link_pdf,
                    'year': year,
                    'authors': authors
                })
    return result


def _is_book(tag):
    """检查是否是书籍条目"""
    for span in tag.findAll("span", class_="gs_ct2"):
        if span.text == "[B]":
            return True
    return False


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


def scihub_urls(html):
    """从页面提取 SciHub URL 列表"""
    result = []
    soup = BeautifulSoup(html, "html.parser")
    for ul in soup.findAll("ul"):
        for a in ul.findAll("a"):
            link = a.get("href")
            if link and (link.startswith("https://sci-hub.") or link.startswith("http://sci-hub.")):
                result.append(link)
    return result


def similar_strings(a, b):
    """计算两个字符串的相似度"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def filter_min_date(papers, min_year):
    """按年份过滤论文"""
    new_list = []
    for paper in papers:
        if paper.get("year") is not None:
            try:
                if int(paper["year"]) >= min_year:
                    new_list.append(paper)
            except:
                pass
    return new_list


class ScholarService:
    def __init__(self, proxy: str = None, timeout: float = 60.0):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = NetInfo.HEADERS
        
    def _make_request(self, url):
        """发送 HTTP 请求"""
        kwargs = {"headers": self.headers, "timeout": self.timeout}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return requests.get(url, **kwargs)
    
    def set_scihub_url(self):
        """自动查找可用的 SciHub 镜像"""
        try:
            r = requests.get(NetInfo.SciHub_URLs_repo, headers=self.headers, timeout=10)
            links = scihub_urls(r.text)
            for link in links:
                try:
                    r = requests.get(link, headers=self.headers, timeout=10)
                    if r.status_code == 200:
                        NetInfo.SciHub_URL = link
                        return link
                except:
                    pass
        except Exception as e:
            logger.warning(f"查找 SciHub 镜像失败: {e}")
        NetInfo.SciHub_URL = "https://sci-hub.st"
        return NetInfo.SciHub_URL
    
    def search(self, query: str, pages: int = 1, min_year: int = None, 
               skip_words: str = None, cites: str = None, 
               scholar_results: int = 10) -> list[dict]:
        """
        搜索 Google Scholar
        
        Args:
            query: 搜索关键词
            pages: 搜索页数
            min_year: 最小发表年份
            skip_words: 排除包含这些词的论文（逗号分隔）
            cites: 被引用论文 ID
            scholar_results: 每页结果数
        """
        base_url = "https://scholar.google.com/scholar?hl=en&as_vis=1&as_sdt=1,5"
        query_url = None

        if query and len(query) > 7 and (query.startswith("http://") or query.startswith("https://")):
            query_url = query
        else:
            url_parts = [base_url]
            if query:
                url_parts.append(f"q={quote(query)}")
            if skip_words:
                url_parts.append(self._parse_skip_list(skip_words).lstrip("&"))
            if cites:
                url_parts.append(f"cites={cites}")
            if min_year:
                url_parts.append(f"as_ylo={min_year}")
            query_url = "&".join(url_parts)
        
        all_results = []
        page_range = range(1, pages + 1) if pages > 0 else range(0, 1)
        
        for i in page_range:
            try:
                if "%d" in query_url:
                    res_url = query_url % (scholar_results * (i - 1))
                elif "start=" in query_url:
                    res_url = query_url
                else:
                    separator = "&" if "?" in query_url else "?"
                    res_url = f"{query_url}{separator}start={scholar_results * (i - 1)}"
                logger.info(f"正在获取 Google Scholar 第 {i} 页...")
                
                response = self._make_request(res_url)
                html = response.text
                
                if "Sorry, we can't verify that you're not a robot" in html:
                    logger.warning("被 Google Scholar 限制，请稍后重试或使用代理")
                    break
                    
                papers = schoolar_parser(html)
                if len(papers) > scholar_results:
                    papers = papers[:scholar_results]
                    
                logger.info(f"Google Scholar 第 {i} 页: 找到 {len(papers)} 篇论文")
                
                if papers:
                    if min_year:
                        papers = filter_min_date(papers, min_year)
                    all_results.extend(papers)
                
                time.sleep(2)  # 避免请求过快
                
            except Exception as e:
                logger.error(f"搜索第 {i} 页时出错: {e}")
                continue
        
        return all_results
    
    def _parse_skip_list(self, skip_words):
        """解析排除词列表"""
        output = ""
        for word in skip_words.split(","):
            word = word.strip()
            if word:
                if " " in word:
                    output += '+-"' + word + '"'
                else:
                    output += '+-' + word
        return output
    
    def search_by_doi(self, doi: str) -> dict:
        """通过 DOI 搜索论文信息"""
        from .crossref_service import CrossrefService
        crossref = CrossrefService(proxy=self.proxy)
        return crossref.get_paper_by_doi(doi)
