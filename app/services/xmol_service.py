import re
import urllib.parse
from typing import Any

import httpx
import requests
from bs4 import BeautifulSoup
from loguru import logger


class XMOLService:
    def __init__(self, proxy=None, timeout: float = 20.0):
        self.proxy = proxy.strip() if proxy and str(proxy).strip() else None
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _client(self):
        kwargs = {"headers": self.headers, "timeout": self.timeout, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.Client(**kwargs)

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    def _normalize_doi(self, doi: str) -> str:
        doi = (doi or "").strip()
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
        return doi.strip().rstrip(".,;")

    def _normalize_title(self, title: str) -> str:
        title = self._clean_text(title)
        title = re.sub(r"\s*[-–—]\s*X-MOL\s*$", "", title, flags=re.I)
        title = re.sub(r"期刊最新论文.*$", "", title)
        title = re.sub(r"最新论文.*$", "", title)
        return title.strip(" -–—,;")

    def _is_low_quality_title(self, title: str) -> bool:
        title = self._normalize_title(title)
        if not title:
            return True
        markers = ("人机验证", "期刊最新论文", "最新论文", "最新文章", "x-mol资讯")
        return any(marker in title for marker in markers)

    def _empty_result(self) -> dict[str, Any]:
        return {
            "title": "",
            "abstract": "",
            "journal": "",
            "impact_factor": None,
            "doi": "",
            "source": "X-MOL",
            "url": "",
            "status": "unavailable",
        }

    def _is_verification_page(self, html: str) -> bool:
        text = (html or "").lower()
        return "人机验证" in html or "aliyuncaptchaconfig" in text or "点击验证" in html

    def _extract_if(self, text: str):
        if not text:
            return None
        match = re.search(r"(?:IF|影响因子)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        return None

    def _extract_doi(self, text: str):
        if not text:
            return ""
            
        # 1. 匹配无空格剥离的常规版本
        match_unstripped = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.I)
        doi_unstripped = ""
        if match_unstripped:
            doi_unstripped = match_unstripped.group(0).rstrip(").,;\"']`")
            
        # 2. 匹配压缩空格后的紧凑版本
        compact = text.replace(" ", "")
        match_stripped = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", compact, re.I)
        doi_stripped = ""
        if match_stripped:
            raw_stripped = match_stripped.group(0)
            # 清理由于剥离空格强行粘连进来的后续正文/声明英文单词
            cleaned_stripped = re.sub(
                r"(?:In|The|Of|Originally|Published|Version|Article|Author|Figure|Table|Measure|Report|Study|Paper|Journal|Page|Volume|Issue|Editor|Press|Read|Here|Click|Http|Https).*$",
                "",
                raw_stripped,
                flags=re.I,
            )
            doi_stripped = cleaned_stripped.rstrip(").,;\"']`")
            
        # 3. 智能选择比较
        len_unstripped = len(re.sub(r"\W", "", doi_unstripped))
        len_stripped = len(re.sub(r"\W", "", doi_stripped))
        
        # 如果剥离空格后匹配出的有效 DOI 长度明显更长（代表原 DOI 被空格截断了），使用剥离后的版本；否则用原版防粘连
        if len_stripped > len_unstripped + 2:
            return self._normalize_doi(doi_stripped)
        return self._normalize_doi(doi_unstripped)



    def _search_via_sogou(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(query)
        search_url = f"https://www.sogou.com/web?query={encoded}"
        results = []
        try:
            kwargs = {"headers": self.headers, "timeout": self.timeout}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            response = requests.get(search_url, **kwargs)
            if response.status_code != 200:
                return results

            soup = BeautifulSoup(response.text, "html.parser")
            seen = set()
            for block in soup.select("div.vrwrap, div.rb"):
                text = self._clean_text(block.get_text(" ", strip=True))
                if not text:
                    continue
                text_lower = text.lower()
                if "x-mol" not in text_lower and "doi" not in text_lower:
                    continue

                anchor = block.select_one("a[href]")
                href = anchor.get("href", "") if anchor else ""
                title = self._normalize_title(anchor.get_text(" ", strip=True)) if anchor else ""
                if self._is_low_quality_title(title):
                    continue
                doi = self._extract_doi(text)
                impact_factor = self._extract_if(text)

                journal = ""
                journal_match = re.search(r"([A-Z][A-Za-z&.\- ]{2,80})\s+(?:IF|影响因子|DOI)", text)
                if journal_match:
                    journal = self._clean_text(journal_match.group(1))

                item = {
                    "title": title,
                    "abstract": text[:600],
                    "journal": journal,
                    "impact_factor": impact_factor,
                    "doi": doi,
                    "source": "X-MOL Search Snippet",
                    "url": href,
                    "status": "snippet",
                }
                dedupe_key = (item["doi"] or item["title"] or item["abstract"]).lower()
                if dedupe_key and dedupe_key not in seen:
                    seen.add(dedupe_key)
                    results.append(item)
                if len(results) >= limit:
                    break
        except Exception as exc:
            logger.warning(f"Sogou X-MOL fallback failed: {exc}")
        return results

    def search_papers(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = self._clean_text(query)
        if not query:
            return []

        xmol_query = f'"{query}" site:x-mol.com'
        results = self._search_via_sogou(xmol_query, limit=limit)
        if not results and "doi" not in query.lower():
            results = self._search_via_sogou(f"{query} x-mol", limit=limit)
        return results

    def get_details_by_doi(self, doi: str) -> dict[str, Any]:
        result = self._empty_result()
        doi = self._normalize_doi(doi)
        if not doi:
            return result

        search_url = f"https://www.x-mol.com/paper/search/result?q={urllib.parse.quote(doi)}"
        try:
            with self._client() as client:
                response = client.get(search_url)
            if response.status_code == 200 and not self._is_verification_page(response.text):
                soup = BeautifulSoup(response.text, "html.parser")
                title_node = soup.select_one("h1, .paper_title, .title")
                abstract_node = soup.select_one(".abstract_content, .paper_abstract, .abstract")
                journal_node = soup.select_one(".magazine_title, .journal_title, .journal-name")
                page_text = self._clean_text(soup.get_text(" ", strip=True))

                result["title"] = self._clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
                result["abstract"] = self._clean_text(abstract_node.get_text(" ", strip=True)) if abstract_node else ""
                result["journal"] = self._clean_text(journal_node.get_text(" ", strip=True)) if journal_node else ""
                result["impact_factor"] = self._extract_if(page_text)
                result["doi"] = self._extract_doi(page_text) or doi
                result["url"] = str(response.url)
                result["status"] = "full"
                return result
        except Exception as exc:
            logger.warning(f"Direct X-MOL DOI fetch failed: {exc}")

        fallback_results = self._search_via_sogou(f'"{doi}" site:x-mol.com', limit=3)
        for item in fallback_results:
            if item.get("doi") == doi or doi in item.get("abstract", "").replace(" ", ""):
                item["status"] = "snippet"
                return item

        result["doi"] = doi
        return result

    def get_details_by_title(self, title: str) -> dict[str, Any]:
        title = self._clean_text(title)
        if not title:
            return self._empty_result()

        candidates = self.search_papers(title, limit=5)
        if candidates:
            best = candidates[0]
            if self._is_low_quality_title(best.get("title", "")):
                result = self._empty_result()
                result["title"] = title
                return result
            if not best.get("title"):
                best["title"] = title
            return best
        result = self._empty_result()
        result["title"] = title
        return result

    def get_abstract_by_doi(self, doi: str) -> str:
        return self.get_details_by_doi(doi).get("abstract", "")
