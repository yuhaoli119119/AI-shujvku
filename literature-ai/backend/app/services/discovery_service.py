from __future__ import annotations

import math
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from app.utils.text_cleaning import normalize_text_tree


class DiscoveryService:
    """Thin adapter over direct provider APIs plus the legacy download engine."""

    DEFAULT_SEARCH_PROVIDERS = ["openalex", "arxiv"]
    DEFAULT_DOWNLOAD_PROVIDERS = ["openalex", "crossref", "arxiv", "semantic_scholar", "web_scraping"]

    def __init__(self) -> None:
        self._ensure_findpapers_path()
        from findpapers import Engine

        self.engine = Engine()

    def search(
        self,
        query: str,
        providers: list[str] | None = None,
        limit: int = 10,
        target_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_query = query.strip()
        if not normalized_query:
            return []

        active_providers = self._normalize_providers(providers, self.DEFAULT_SEARCH_PROVIDERS)
        if not active_providers:
            return []

        merged: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        per_provider_limit = max(1, math.ceil(limit / max(len(active_providers), 1)))

        for provider in active_providers:
            try:
                if provider == "openalex":
                    items = self._search_openalex(normalized_query, max(limit, per_provider_limit), target_types=target_types)
                elif provider == "arxiv":
                    items = self._search_arxiv(normalized_query, max(limit, per_provider_limit), target_types=target_types)
                else:
                    items = self._search_via_engine(normalized_query, [provider], per_provider_limit)
            except Exception as e:
                # Log or handle provider failure, fallback to next
                print(f"Provider {provider} failed: {e}", file=sys.stderr)
                continue

            for item in items:
                key = self._dedupe_key(item)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        return merged[:limit]

    def fetch_metadata(
        self,
        identifier: str,
        providers: list[str] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        active_providers = self._normalize_providers(providers, self.DEFAULT_DOWNLOAD_PROVIDERS)
        if "web_scraping" not in active_providers:
            active_providers.append("web_scraping")
        paper = self.engine.get(identifier.strip(), databases=active_providers, timeout=15.0, verbose=False)
        if paper is None:
            raise ValueError("No paper metadata found for the given identifier")
        return paper, self._serialize_paper(paper)

    def download_pdf(self, paper: Any, dest_dir: Path) -> Path:
        metrics = self.engine.download([paper], str(dest_dir), num_workers=1, timeout=30.0, verbose=False, show_progress=False)
        if int(metrics.get("downloaded_papers", 0)) < 1:
            raise ValueError("Unable to download PDF for the given identifier")
        pdf_files = sorted(dest_dir.glob("*.pdf"))
        if not pdf_files:
            raise ValueError("PDF download reported success but no file was found")
        return pdf_files[0]

    def download_pdf_url(self, pdf_url: str, dest_dir: Path, filename: str | None = None) -> Path:
        url = (pdf_url or "").strip()
        if not url:
            raise ValueError("Missing direct PDF URL")
        target_name = filename or f"{uuid.uuid4()}.pdf"
        target_path = dest_dir / target_name
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            content = response.content
        if not content.startswith(b"%PDF"):
            raise ValueError("Direct URL did not return a PDF file")
        target_path.write_bytes(content)
        return target_path

    def _search_openalex(self, query: str, limit: int, target_types: list[str] | None = None) -> list[dict[str, Any]]:
        per_page = min(max(limit, 1), 200)

        # 根据分类类型动态构建 filter
        type_map = {"computational": "article", "experimental": "article", "review": "review"}
        content_types: set[str] = set()
        for t in (target_types or []):
            if t in type_map:
                content_types.add(type_map[t])
        if content_types:
            type_filter = f"type:{'|'.join(sorted(content_types))}"
        else:
            type_filter = "type:article|review"

        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            response = client.get(
                "https://api.openalex.org/works",
                params={"search": query, "per-page": per_page, "filter": type_filter},
                headers={"User-Agent": "LiteratureAI/1.0"},
            )
            response.raise_for_status()
            payload = response.json()
        results = []
        for item in payload.get("results", [])[:limit]:
            results.append(
                normalize_text_tree(
                    {
                        "identifier": item.get("doi") or item.get("id") or item.get("primary_location", {}).get("landing_page_url"),
                        "title": item.get("display_name") or "",
                        "doi": item.get("doi"),
                        "year": item.get("publication_year"),
                        "journal": (
                            ((item.get("primary_location") or {}).get("source") or {}).get("display_name")
                            or ((item.get("best_oa_location") or {}).get("source") or {}).get("display_name")
                        ),
                        "authors": [
                            ((authorship.get("author") or {}).get("display_name"))
                            for authorship in item.get("authorships", []) or []
                            if (authorship.get("author") or {}).get("display_name")
                        ],
                        "abstract": self._expand_openalex_abstract(item.get("abstract_inverted_index")),
                        "url": item.get("doi") or item.get("id") or (item.get("primary_location") or {}).get("landing_page_url"),
                        "pdf_url": (
                            (item.get("best_oa_location") or {}).get("pdf_url")
                            or (item.get("primary_location") or {}).get("pdf_url")
                            or (item.get("open_access") or {}).get("oa_url")
                        ),
                        "is_open_access": (item.get("open_access") or {}).get("is_oa"),
                        "databases": ["openalex"],
                    }
                )
            )
        return results

    @staticmethod
    def _build_arxiv_query(query: str, target_types: list[str] | None = None) -> str:
        """将自然语言查询转换为 arXiv Boolean 查询，根据分类类型追加领域关键词。

        target_types 映射:
        - "computational" -> 追加 ti:DFT/ab-initio/first-principles 等
        - "experimental" -> 追加 ti:electrochem/cataly/half-cell 等
        - "review" -> 追加 ti:review/progress/overview 等
        """
        base = query.replace("[", "").replace("]", "").strip()
        if not target_types:
            return f"all:{base}"

        type_filters = {
            "computational": '(ti:"density functional" OR ti:DFT OR ti:"ab initio" OR ti:"first principles" OR ti:"molecular dynamics")',
            "experimental": '(ti:electrochem* OR ti:cataly* OR ti:"half-cell" OR ti:"full cell" OR ti:characterization)',
            "review": '(ti:review OR ti:"state of the art" OR ti:progress OR ti:overview OR ti:advances)',
        }

        parts = [f'all:"{base}"']
        for t in target_types:
            if t in type_filters:
                parts.append(type_filters[t])

        return " AND ".join(parts)

    def _search_arxiv(self, query: str, limit: int, target_types: list[str] | None = None) -> list[dict[str, Any]]:
        arxiv_query = self._build_arxiv_query(query, target_types)
        max_results = min(max(limit, 1), 100)
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            response = client.get(
                "http://export.arxiv.org/api/query",
                params={"search_query": arxiv_query, "start": 0, "max_results": max_results},
                headers={"User-Agent": "LiteratureAI/1.0"},
            )
            response.raise_for_status()
            xml_text = response.text

        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        results = []
        for entry in root.findall("atom:entry", ns)[:limit]:
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
            year = None
            if published[:4].isdigit():
                year = int(published[:4])
            authors = [
                (author.findtext("atom:name", default="", namespaces=ns) or "").strip()
                for author in entry.findall("atom:author", ns)
                if (author.findtext("atom:name", default="", namespaces=ns) or "").strip()
            ]
            entry_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
            doi = (entry.findtext("arxiv:doi", default="", namespaces=ns) or "").strip() or None
            pdf_url = None
            for link in entry.findall("atom:link", ns):
                title_attr = (link.attrib.get("title") or "").lower()
                href = (link.attrib.get("href") or "").strip()
                if title_attr == "pdf" and href:
                    pdf_url = href
                    break
            results.append(
                normalize_text_tree(
                    {
                        "identifier": doi or entry_id or pdf_url or title,
                        "title": title,
                        "doi": doi,
                        "year": year,
                        "journal": "arXiv",
                        "authors": authors,
                        "abstract": summary,
                        "url": entry_id or pdf_url,
                        "pdf_url": pdf_url,
                        "is_open_access": True,
                        "databases": ["arxiv"],
                    }
                )
            )
        return results

    def _search_via_engine(self, query: str, providers: list[str], limit: int) -> list[dict[str, Any]]:
        if "[" not in query and "]" not in query:
            if " AND " not in query.upper() and " OR " not in query.upper():
                safe_query = " AND ".join(word for word in query.split() if word.strip())
                wrapped_query = f"[{safe_query}]"
            else:
                wrapped_query = f"[{query}]"
        else:
            wrapped_query = query
        result = self.engine.search(
            wrapped_query,
            databases=providers,
            max_papers_per_database=limit,
            num_workers=min(max(len(providers), 1), 4),
            show_progress=False,
            enrichment_databases=[],
        )
        return [self._serialize_paper(paper) for paper in result.papers[:limit]]

    @staticmethod
    def _expand_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
        if not inverted_index:
            return None
        positions: dict[int, str] = {}
        for token, indexes in inverted_index.items():
            for index in indexes or []:
                positions[index] = token
        if not positions:
            return None
        return " ".join(token for _, token in sorted(positions.items()))

    @staticmethod
    def _dedupe_key(item: dict[str, Any]) -> str:
        doi = (item.get("doi") or "").strip().lower()
        if doi:
            return f"doi:{doi}"
        title = (item.get("title") or "").strip().lower()
        year = item.get("year")
        if title:
            return f"title:{title}|year:{year or ''}"
        return f"id:{item.get('identifier') or uuid.uuid4()}"

    @staticmethod
    def _normalize_providers(providers: list[str] | None, defaults: list[str]) -> list[str]:
        return [item.strip().lower() for item in (providers or defaults) if item and item.strip()]

    @staticmethod
    def _serialize_paper(paper: Any) -> dict[str, Any]:
        publication_date = getattr(paper, "publication_date", None)
        source = getattr(paper, "source", None)
        authors = []
        for author in getattr(paper, "authors", []) or []:
            name = getattr(author, "name", None) or (str(author) if author else None)
            if name:
                authors.append(name)
        identifier = getattr(paper, "doi", None) or getattr(paper, "url", None) or getattr(paper, "pdf_url", None) or getattr(paper, "title", "")
        return normalize_text_tree({
            "identifier": identifier,
            "title": getattr(paper, "title", ""),
            "doi": getattr(paper, "doi", None),
            "year": getattr(publication_date, "year", None) if publication_date else None,
            "journal": getattr(source, "title", None) if source else None,
            "authors": authors,
            "abstract": getattr(paper, "abstract", None),
            "url": getattr(paper, "url", None),
            "pdf_url": getattr(paper, "pdf_url", None),
            "is_open_access": getattr(paper, "is_open_access", None),
            "databases": sorted(list(getattr(paper, "databases", []) or [])),
        })

    @staticmethod
    def _ensure_findpapers_path() -> None:
        module_path = Path(__file__).resolve()
        # 将 literature-ai/backend 目录加入 sys.path，保证内置子包 findpapers 能够被正确导入
        backend_dir = module_path.parents[2]
        if backend_dir.exists():
            backend_dir_str = str(backend_dir)
            if backend_dir_str not in sys.path:
                sys.path.insert(0, backend_dir_str)
