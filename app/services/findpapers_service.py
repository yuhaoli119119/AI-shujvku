"""FindpapersService: 适配层，将 findpapers 库的功能封装为当前项目可用的服务。

提供多数据库学术搜索、PDF下载、DOI/URL查找、引文雪球等功能。
所有方法均为线程安全，可直接在 QThread 中调用。

支持的数据库:
    arxiv, crossref, ieee, openalex, pubmed, scopus, semantic_scholar, wos

使用示例::

    from app.services.findpapers_service import FindpapersService

    svc = FindpapersService(proxy="http://127.0.0.1:7890")
    result = svc.search("[machine learning]", databases=["arxiv", "openalex"])
    for paper in result.papers:
        print(paper.title)

    svc.download(result.papers, "./pdfs")
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from loguru import logger

from findpapers.engine import Engine
from findpapers.core.paper import Paper
from findpapers.core.search_result import SearchResult
from findpapers.core.citation_graph import CitationGraph
from findpapers.utils.persistence import (
    save_to_json,
    save_to_bibtex,
    save_to_csv,
    load_from_json,
    load_from_bibtex,
)


class FindpapersService:
    """Findpapers 功能适配服务。

    封装 findpapers.Engine，为当前项目提供统一的学术论文搜索、下载、查找和引文分析接口。
    所有配置通过构造函数传入，运行时方法保持无状态（除 Engine 实例内部状态外）。

    Parameters
    ----------
    proxy : str | None
        HTTP/HTTPS 代理地址，如 ``"http://127.0.0.1:7890"``。
    ieee_api_key : str | None
        IEEE Xplore API key。
    scopus_api_key : str | None
        Elsevier / Scopus API key。
    pubmed_api_key : str | None
        NCBI PubMed API key（可选，提升速率限制）。
    openalex_api_key : str | None
        OpenAlex API key（可选）。
    email : str | None
        联系邮箱，用于 OpenAlex/CrossRef 的 polite pool 访问。
    semantic_scholar_api_key : str | None
        Semantic Scholar API key（可选）。
    wos_api_key : str | None
        Web of Science API key。
    ssl_verify : bool
        是否验证 SSL 证书。使用机构代理时设为 ``False``。
    """

    def __init__(
        self,
        proxy: str | None = None,
        ieee_api_key: str | None = None,
        scopus_api_key: str | None = None,
        pubmed_api_key: str | None = None,
        openalex_api_key: str | None = None,
        email: str | None = None,
        semantic_scholar_api_key: str | None = None,
        wos_api_key: str | None = None,
        ssl_verify: bool = True,
    ) -> None:
        self._engine = Engine(
            ieee_api_key=ieee_api_key,
            scopus_api_key=scopus_api_key,
            pubmed_api_key=pubmed_api_key,
            openalex_api_key=openalex_api_key,
            email=email,
            semantic_scholar_api_key=semantic_scholar_api_key,
            wos_api_key=wos_api_key,
            proxy=proxy,
            ssl_verify=ssl_verify,
        )
        logger.info("FindpapersService 初始化完成 (proxy={}, ssl_verify={})", proxy, ssl_verify)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        databases: list[str] | None = None,
        max_papers_per_database: int | None = None,
        since: dt.date | None = None,
        until: dt.date | None = None,
        num_workers: int = 1,
        timeout: float | None = 10.0,
        show_progress: bool = True,
    ) -> SearchResult:
        """跨数据库搜索学术论文。

        Parameters
        ----------
        query : str
            查询字符串。语法：用方括号包裹搜索词，支持 AND/OR/AND NOT 运算符。
            例如：``"[machine learning] AND [healthcare]"``
            可加字段过滤前缀：ti(标题), abs(摘要), key(关键词), au(作者), src(来源), aff(机构)。
            例如：``"ti[deep learning] AND abs[transformer]"``
        databases : list[str] | None
            要查询的数据库列表。``None`` 表示自动选择所有可用数据库。
            可选值：``"arxiv"``, ``"ieee"``, ``"openalex"``, ``"pubmed"``,
            ``"scopus"``, ``"semantic_scholar"``, ``"wos"``。
        max_papers_per_database : int | None
            每个数据库最多返回论文数。``None`` 不限。
        since : dt.date | None
            只返回此日期及之后发表的论文。
        until : dt.date | None
            只返回此日期及之前发表的论文。
        num_workers : int
            并行搜索线程数。默认 1（顺序执行）。
        show_progress : bool
            是否显示进度条。

        Returns
        -------
        SearchResult
            包含 papers 列表、查询元数据等。

        Examples
        --------
        >>> result = svc.search("[neural network]", databases=["arxiv", "openalex"])
        >>> print(f"找到 {len(result.papers)} 篇论文")
        """
        logger.info("开始搜索: query={}, databases={}", query, databases)
        result = self._engine.search(
            query,
            databases=databases,
            max_papers_per_database=max_papers_per_database,
            since=since,
            until=until,
            num_workers=num_workers,
            timeout=timeout,
            verbose=False,
            show_progress=show_progress,
        )
        logger.info("搜索完成: {} 篇论文", len(result.papers))
        return result

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download(
        self,
        papers: list[Paper],
        output_directory: str,
        *,
        num_workers: int = 1,
        timeout: float | None = 30.0,
        show_progress: bool = True,
    ) -> dict[str, int | float]:
        """批量下载 PDF 文件。

        对每篇论文尝试所有已知 URL，并跟踪 HTML 落页以解析实际 PDF 链接。
        文件以 ``year-title.pdf`` 格式命名保存到 output_directory。

        Parameters
        ----------
        papers : list[Paper]
            要下载的论文列表（通常来自 search() 结果）。
        output_directory : str
            PDF 保存目录。不存在则自动创建。
        num_workers : int
            并行下载线程数。默认 1。
        timeout : float | None
            单次请求超时秒数。``None`` 禁用超时。
        show_progress : bool
            是否显示进度条。

        Returns
        -------
        dict[str, int | float]
            包含 total_papers, downloaded_papers, runtime_in_seconds 等指标。
        """
        logger.info("开始下载: {} 篇论文 -> {}", len(papers), output_directory)
        metrics = self._engine.download(
            papers,
            output_directory,
            num_workers=num_workers,
            timeout=timeout,
            show_progress=show_progress,
        )
        logger.info(
            "下载完成: {}/{} 成功, 耗时 {:.1f}s",
            metrics.get("downloaded_papers", 0),
            metrics.get("total_papers", 0),
            metrics.get("runtime_in_seconds", 0),
        )
        return metrics

    # ------------------------------------------------------------------
    # 单篇查找
    # ------------------------------------------------------------------

    def get(
        self,
        identifier: str,
        *,
        databases: list[str] | None = None,
        timeout: float | None = 10.0,
    ) -> Paper | None:
        """通过 DOI 或 URL 获取单篇论文的完整元数据。

        支持三种标识符格式：
        - 裸 DOI：``"10.1038/nature12373"``
        - DOI URL：``"https://doi.org/10.1038/nature12373"``
        - 落页 URL：``"https://arxiv.org/abs/1706.03762"```

        Parameters
        ----------
        identifier : str
            DOI、DOI URL 或论文落页 URL。
        databases : list[str] | None
            要查询的数据源列表。``None`` 使用全部可用源。
        timeout : float | None
            请求超时秒数。

        Returns
        -------
        Paper | None
            论文对象，未找到时返回 None。
        """
        logger.info("查找论文: {}", identifier)
        paper = self._engine.get(
            identifier,
            databases=databases,
            timeout=timeout,
        )
        if paper:
            logger.info("找到论文: {}", paper.title)
        else:
            logger.warning("未找到论文: {}", identifier)
        return paper

    # ------------------------------------------------------------------
    # 引文雪球
    # ------------------------------------------------------------------

    def snowball(
        self,
        papers: list[Paper] | Paper,
        *,
        max_depth: int = 1,
        direction: str = "both",
        top_n_per_level: int | None = None,
        databases: list[str] | None = None,
        since: dt.date | None = None,
        until: dt.date | None = None,
        num_workers: int = 1,
        show_progress: bool = True,
    ) -> CitationGraph:
        """从种子论文构建引文图（雪球法）。

        从一篇或多篇种子论文出发，迭代获取其参考文献（向后）和/或引用文献（向前），
        构建有向引文图。

        Parameters
        ----------
        papers : list[Paper] | Paper
            种子论文（来自 search() 或 get()）。
        max_depth : int
            最大迭代深度。1 = 仅直接邻居。
        direction : str
            ``"backward"``（参考文献）、``"forward"``（引用文献）、``"both"``。
        top_n_per_level : int | None
            每层仅保留被引次数最高的 N 篇。``None`` 不限。
        databases : list[str] | None
            引文数据源。``None`` 使用全部（OpenAlex, Semantic Scholar, CrossRef）。
        since : dt.date | None
            仅包含此日期之后发表的发现论文。
        until : dt.date | None
            仅包含此日期之前发表的发现论文。
        num_workers : int
            并行查询线程数。
        show_progress : bool
            是否显示进度条。

        Returns
        -------
        CitationGraph
            有向引文图，节点为论文，边表示引用关系。

        Examples
        --------
        >>> seed = svc.get("10.1038/nature12373")
        >>> graph = svc.snowball(seed, max_depth=1, direction="forward")
        >>> print(f"{graph.node_count} 个节点, {graph.edge_count} 条边")
        """
        seed_count = len(papers) if isinstance(papers, list) else 1
        logger.info("开始雪球: {} 篇种子, depth={}, direction={}", seed_count, max_depth, direction)
        graph = self._engine.snowball(
            papers,
            max_depth=max_depth,
            direction=direction,
            top_n_per_level=top_n_per_level,
            databases=databases,
            since=since,
            until=until,
            num_workers=num_workers,
            show_progress=show_progress,
        )
        logger.info("雪球完成: {} 个节点, {} 条边", graph.node_count, graph.edge_count)
        return graph

    # ------------------------------------------------------------------
    # 数据持久化工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def save_json(data: SearchResult | CitationGraph | list[Paper], path: str) -> None:
        """保存数据到 JSON 文件。

        Parameters
        ----------
        data : SearchResult | CitationGraph | list[Paper]
            要保存的数据。
        path : str
            输出文件路径。
        """
        save_to_json(data, path)
        logger.info("已保存 JSON: {} ({})", path, type(data).__name__)

    @staticmethod
    def save_bibtex(papers: list[Paper], path: str) -> None:
        """保存论文列表到 BibTeX 文件。

        Parameters
        ----------
        papers : list[Paper]
            论文列表。
        path : str
            输出 .bib 文件路径。
        """
        save_to_bibtex(papers, path)
        logger.info("已保存 BibTeX: {} ({} 篇)", path, len(papers))

    @staticmethod
    def save_csv(papers: list[Paper], path: str) -> None:
        """保存论文列表到 CSV 文件。

        Parameters
        ----------
        papers : list[Paper]
            论文列表。
        path : str
            输出 .csv 文件路径。
        """
        save_to_csv(papers, path)
        logger.info("已保存 CSV: {} ({} 篇)", path, len(papers))

    @staticmethod
    def load_json(path: str) -> SearchResult | CitationGraph | list[Paper]:
        """从 JSON 文件加载数据。

        Parameters
        ----------
        path : str
            JSON 文件路径。

        Returns
        -------
        SearchResult | CitationGraph | list[Paper]
            加载的数据对象。
        """
        data = load_from_json(path)
        logger.info("已加载 JSON: {} ({})", path, type(data).__name__)
        return data

    @staticmethod
    def load_bibtex(path: str) -> list[Paper]:
        """从 BibTeX 文件加载论文列表。

        Parameters
        ----------
        path : str
            .bib 文件路径。

        Returns
        -------
        list[Paper]
            加载的论文列表。
        """
        papers = load_from_bibtex(path)
        logger.info("已加载 BibTeX: {} ({} 篇)", path, len(papers))
        return papers

    # ------------------------------------------------------------------
    # 数据转换工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def paper_to_dict(paper: Paper) -> dict[str, Any]:
        """将 Paper 对象转换为字典（适合 JSON 序列化或 UI 展示）。

        Parameters
        ----------
        paper : Paper
            论文对象。

        Returns
        -------
        dict[str, Any]
            字典形式的论文数据。
        """
        return paper.to_dict()

    @staticmethod
    def papers_to_dicts(papers: list[Paper]) -> list[dict[str, Any]]:
        """批量转换 Paper 列表为字典列表。

        Parameters
        ----------
        papers : list[Paper]
            论文列表。

        Returns
        -------
        list[dict[str, Any]]
            字典列表。
        """
        return [p.to_dict() for p in papers]

    @staticmethod
    def result_to_display_dicts(result: SearchResult) -> list[dict[str, Any]]:
        """将 SearchResult 转换为适合 UI 表格展示的字典列表。

        每个字典包含精简的字段，便于在表格中显示。

        Parameters
        ----------
        result : SearchResult
            搜索结果。

        Returns
        -------
        list[dict[str, Any]]
            展示用字典列表。
        """
        rows = []
        for paper in result.papers:
            d = paper.to_dict()
            row = {
                "title": d.get("title", ""),
                "authors": ", ".join(
                    a.get("name", "") for a in d.get("authors", [])
                ),
                "year": (
                    d.get("publication_date", "")[:4]
                    if d.get("publication_date")
                    else None
                ),
                "source": d.get("source", {}).get("title", "") if d.get("source") else "",
                "doi": d.get("doi", ""),
                "citations": d.get("citations"),
                "abstract": d.get("abstract", "")[:200] + ("..." if len(d.get("abstract", "")) > 200 else ""),
                "databases": ", ".join(d.get("databases", [])),
                "paper_type": d.get("paper_type"),
                "is_open_access": d.get("is_open_access"),
                "url": d.get("url", ""),
                "pdf_url": d.get("pdf_url", ""),
                "_paper_obj": paper,
            }
            rows.append(row)
        return rows
