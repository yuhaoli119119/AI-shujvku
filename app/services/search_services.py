from typing import Any
import xml.etree.ElementTree as ET

import httpx
import requests
from loguru import logger

from .xmol_service import XMOLService


class SearchService:
    def __init__(self, proxy: str = None, timeout: float = 60.0):
        self.proxy = proxy.strip() if proxy and proxy.strip() else None
        self.timeout = timeout
        self.headers = {
            "User-Agent": "LitAICollector/1.0 (mailto:your-email@example.com) PySide6/6.7",
            "Accept": "application/json",
        }
        self.xmol = XMOLService(proxy=self.proxy, timeout=min(timeout, 20.0))

    def _reconstruct_abstract(self, inverted_index: dict[str, list[int]]) -> str:
        if not inverted_index:
            return ""
        try:
            pos_map = {}
            for word, positions in inverted_index.items():
                for pos in positions:
                    pos_map[pos] = word
            return " ".join(pos_map[index] for index in sorted(pos_map.keys()))
        except Exception as exc:
            logger.error(f"Failed to reconstruct OpenAlex abstract: {exc}")
            return ""

    def _request_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        client_kwargs = {"timeout": self.timeout, "follow_redirects": True, "headers": self.headers}
        if self.proxy:
            client_kwargs["proxy"] = self.proxy

        try:
            with httpx.Client(**client_kwargs) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning(f"httpx JSON request failed for {url}: {exc}")

        request_kwargs = {"headers": self.headers, "timeout": self.timeout, "params": params}
        if self.proxy:
            request_kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        try:
            response = requests.get(url, **request_kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error(f"requests JSON fallback failed for {url}: {exc}")
            return None

    def _request_bytes(self, url: str) -> bytes | None:
        get_kwargs = {"timeout": self.timeout, "follow_redirects": True, "headers": self.headers}
        if self.proxy:
            get_kwargs["proxy"] = self.proxy

        try:
            response = httpx.get(url, **get_kwargs)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            logger.warning(f"httpx byte request failed for {url}: {exc}")

        request_kwargs = {"headers": self.headers, "timeout": self.timeout}
        if self.proxy:
            request_kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        try:
            response = requests.get(url, **request_kwargs)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            logger.error(f"requests byte fallback failed for {url}: {exc}")
            return None

    def search_openalex(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        url = "https://api.openalex.org/works"
        params = {
            "search": query,
            "per_page": limit,
            "select": "id,display_name,title,authorships,publication_year,doi,open_access,cited_by_count,abstract_inverted_index,primary_location",
        }
        try:
            data = self._request_json(url, params)
            if not data:
                return []
            results = []
            for work in data.get("results", []):
                results.append(
                    {
                        "id": work.get("id", ""),
                        "title": work.get("display_name") or work.get("title") or "Untitled",
                        "authors": ", ".join(
                            author.get("author", {}).get("display_name", "")
                            for author in work.get("authorships", [])[:3]
                        ),
                        "year": work.get("publication_year"),
                        "doi": work.get("doi", "").replace("https://doi.org/", "") if work.get("doi") else "",
                        "abstract": self._reconstruct_abstract(work.get("abstract_inverted_index", {})),
                        "source": "OpenAlex",
                        "is_oa": work.get("open_access", {}).get("is_oa", False),
                        "citations": work.get("cited_by_count", 0),
                        "oa_url": work.get("open_access", {}).get("oa_url", ""),
                        "journal": work.get("primary_location", {}).get("source", {}).get("display_name", ""),
                        "impact_factor": None,
                    }
                )
            return results
        except Exception as exc:
            logger.error(f"OpenAlex search failed: {exc}")
            return []

    def search_arxiv(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        import feedparser

        url = f"http://export.arxiv.org/api/query?search_query=all:{query}&max_results={limit}"
        try:
            content = self._request_bytes(url)
            if not content:
                return []
            feed = feedparser.parse(content)
            results = []
            for entry in feed.entries:
                results.append(
                    {
                        "id": entry.id.split("/abs/")[-1],
                        "title": entry.title.replace("\n", " ").strip(),
                        "authors": ", ".join(author.name for author in entry.authors[:3]),
                        "year": entry.published[:4] if entry.get("published") else None,
                        "doi": entry.get("arxiv_doi", ""),
                        "abstract": entry.summary,
                        "source": "arXiv",
                        "is_oa": True,
                        "citations": 0,
                        "journal": "",
                        "impact_factor": None,
                        "oa_url": entry.id.replace("/abs/", "/pdf/") + ".pdf",
                    }
                )
            return results
        except Exception as exc:
            logger.error(f"arXiv search failed: {exc}")
            return []

    def search_semantic_scholar(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": limit,
            "fields": "title,authors,year,abstract,citationCount,externalIds,url,openAccessPdf,journal",
        }
        try:
            data = self._request_json(url, params)
            if not data:
                return []
            results = []
            for paper in data.get("data", []):
                external_ids = paper.get("externalIds") or {}
                oa_pdf = paper.get("openAccessPdf") or {}
                journal = paper.get("journal") or {}
                results.append(
                    {
                        "id": paper.get("paperId", "") or paper.get("url", ""),
                        "title": paper.get("title") or "Untitled",
                        "authors": ", ".join(a.get("name", "") for a in (paper.get("authors") or [])[:4]),
                        "year": paper.get("year"),
                        "doi": external_ids.get("DOI", ""),
                        "abstract": paper.get("abstract", "") or "",
                        "source": "Semantic Scholar",
                        "is_oa": bool(oa_pdf.get("url")),
                        "citations": paper.get("citationCount", 0) or 0,
                        "oa_url": oa_pdf.get("url", "") or paper.get("url", ""),
                        "journal": journal.get("name", "") if isinstance(journal, dict) else str(journal or ""),
                        "impact_factor": None,
                    }
                )
            return results
        except Exception as exc:
            logger.error(f"Semantic Scholar search failed: {exc}")
            return []

    def search_pubmed(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            search_data = self._request_json(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "retmax": limit,
                    "sort": "relevance",
                    "term": query,
                },
            )
            if not search_data:
                return []
            ids = search_data.get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []

            summary_data = self._request_json(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "id": ",".join(ids),
                },
            )
            fetch_xml = self._request_bytes(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&retmode=xml&id="
                + ",".join(ids)
            )

            abstracts_by_pmid: dict[str, str] = {}
            if fetch_xml:
                try:
                    root = ET.fromstring(fetch_xml)
                    for article in root.findall(".//PubmedArticle"):
                        pmid = "".join(article.findtext(".//PMID", default="")).strip()
                        abstract_parts = [
                            "".join(node.itertext()).strip()
                            for node in article.findall(".//Abstract/AbstractText")
                            if "".join(node.itertext()).strip()
                        ]
                        if pmid and abstract_parts:
                            abstracts_by_pmid[pmid] = " ".join(abstract_parts)
                except Exception as exc:
                    logger.warning(f"PubMed abstract parse failed: {exc}")

            results = []
            summary_result = (summary_data or {}).get("result", {})
            for pmid in ids:
                item = summary_result.get(str(pmid), {})
                article_ids = item.get("articleids") or []
                doi = ""
                for article_id in article_ids:
                    if article_id.get("idtype") == "doi":
                        doi = article_id.get("value", "")
                        break
                authors = item.get("authors") or []
                results.append(
                    {
                        "id": str(pmid),
                        "title": item.get("title", "") or "Untitled",
                        "authors": ", ".join(a.get("name", "") for a in authors[:4]),
                        "year": str(item.get("pubdate", ""))[:4] if item.get("pubdate") else None,
                        "doi": doi,
                        "abstract": abstracts_by_pmid.get(str(pmid), ""),
                        "source": "PubMed",
                        "is_oa": False,
                        "citations": 0,
                        "oa_url": "",
                        "journal": item.get("fulljournalname", "") or item.get("source", ""),
                        "impact_factor": None,
                    }
                )
            return results
        except Exception as exc:
            logger.error(f"PubMed search failed: {exc}")
            return []

    def search_xmol(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        results = self.xmol.search_papers(query, limit=limit)
        normalized = []
        for index, item in enumerate(results):
            normalized.append(
                {
                    "id": item.get("url") or f"xmol-{index}",
                    "title": item.get("title") or query,
                    "authors": "",
                    "year": None,
                    "doi": item.get("doi", ""),
                    "abstract": item.get("abstract", ""),
                    "source": item.get("source", "X-MOL"),
                    "is_oa": False,
                    "citations": 0,
                    "oa_url": "",
                    "journal": item.get("journal", ""),
                    "impact_factor": item.get("impact_factor"),
                    "xmol_status": item.get("status", "snippet"),
                    "xmol_url": item.get("url", ""),
                }
            )
        return normalized
