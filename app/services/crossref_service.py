# -*- coding: utf-8 -*-
"""Crossref API 服务 - 基于 PyPaperBot"""
import requests
import time
import random
import bibtexparser
from loguru import logger


class CrossrefService:
    def __init__(self, proxy: str = None, timeout: float = 30.0):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = {
            'User-Agent': 'LitAICollector/1.0 (mailto:contact@example.com)',
            'Accept': 'application/json'
        }
    
    def _make_request(self, url):
        """发送 HTTP 请求"""
        kwargs = {"headers": self.headers, "timeout": self.timeout}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return requests.get(url, **kwargs)
    
    def get_bibtex(self, doi: str) -> str:
        """通过 DOI 获取 BibTeX"""
        try:
            url = f"http://api.crossref.org/works/{doi}/transform/application/x-bibtex"
            response = self._make_request(url)
            if response.status_code == 200:
                return response.text
            return ""
        except Exception as e:
            logger.error(f"获取 BibTeX 失败: {e}")
            return ""
    
    def get_paper_by_doi(self, doi: str) -> dict:
        """通过 DOI 获取论文完整信息"""
        paper = {
            "doi": doi,
            "title": None,
            "journal": None,
            "year": None,
            "authors": None,
            "bibtex": None
        }
        
        try:
            url = f"https://api.crossref.org/works/{doi}"
            response = self._make_request(url)
            if response.status_code == 200:
                data = response.json().get("message", {})
                paper["title"] = data.get("title", [None])[0] if data.get("title") else None
                
                container_titles = data.get("short-container-title", [])
                if not container_titles:
                    container_titles = data.get("container-title", [])
                paper["journal"] = container_titles[0] if container_titles else None
                
                paper["year"] = data.get("published-print", {}).get("date-parts", [[None]])[0][0]
                if not paper["year"]:
                    paper["year"] = data.get("published-online", {}).get("date-parts", [[None]])[0][0]
                
                authors = data.get("author", [])
                if authors:
                    paper["authors"] = " and ".join([
                        f"{a.get('given', '')} {a.get('family', '')}".strip()
                        for a in authors
                    ])
                
                paper["bibtex"] = self.get_bibtex(doi)
        except Exception as e:
            logger.error(f"获取论文信息失败: {e}")
        
        return paper
    
    def search_by_title(self, title: str) -> list[dict]:
        """通过标题搜索论文"""
        results = []
        try:
            params = {
                'query.bibliographic': title.lower(),
                'sort': 'relevance',
                'select': "DOI,title,deposited,author,short-container-title,published-print,published-online"
            }
            url = "https://api.crossref.org/works"
            response = self._make_request(url, params=params)
            
            if response.status_code == 200:
                data = response.json()
                for item in data.get("message", {}).get("items", []):
                    result = {
                        "doi": item.get("DOI"),
                        "title": item.get("title", [None])[0] if item.get("title") else None,
                        "journal": (item.get("short-container-title") or [None])[0],
                        "year": None,
                        "authors": None,
                        "timestamp": item.get("deposited", {}).get("timestamp", 0)
                    }
                    
                    date_parts = item.get("published-print", {}).get("date-parts", [[None]])
                    if not date_parts or not date_parts[0][0]:
                        date_parts = item.get("published-online", {}).get("date-parts", [[None]])
                    if date_parts and date_parts[0]:
                        result["year"] = date_parts[0][0]
                    
                    authors = item.get("author", [])
                    if authors:
                        result["authors"] = " and ".join([
                            f"{a.get('given', '')} {a.get('family', '')}".strip()
                            for a in authors
                        ])
                    
                    results.append(result)
        except Exception as e:
            logger.error(f"标题搜索失败: {e}")
        
        time.sleep(random.uniform(1, 3))
        return results
    
    def _make_request(self, url, params=None):
        """发送 HTTP 请求"""
        kwargs = {"headers": self.headers, "timeout": self.timeout}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        if params:
            return requests.get(url, params=params, **kwargs)
        return requests.get(url, **kwargs)
