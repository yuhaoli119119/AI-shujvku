#!/usr/bin/env python3
"""
逐年份搜索并入库单/双原子催化剂 A 类文献到双原子催化剂数据库。

用法:
  python search_and_ingest.py                  # 搜索所有年份 (2020-2026)
  python search_and_ingest.py --year 2024      # 只搜索某一年
  python search_and_ingest.py --dry-run        # 只搜索不入库
  python search_and_ingest.py --auto-approve   # 自动审批并入库
  python search_and_ingest.py --min-score 0.5  # 只入库相关性评分 >= 0.5 的文献

API 流程:
  1. POST /api/intake/search → 搜索文献，返回 candidates (含 id, relevance_score, screening_tier)
  2. POST /api/intake/candidates/{id}/approve → 审批
  3. POST /api/intake/candidates/{id}/ingest → 入库
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import os

# Fix Windows console encoding
if sys.platform == "win32":
    os.system("")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://localhost:8000"

# 年份范围
YEAR_RANGE = list(range(2026, 2019, -1))  # 2026 -> 2020

# 搜索查询模板 - 覆盖单原子、双原子催化剂的关键词
QUERIES_PER_YEAR = [
    # 双原子催化剂
    "dual-atom catalyst OR diatomic catalyst OR dual-site catalyst DFT electrocatalysis",
    # 单原子催化剂（计算/实验型）
    "single-atom catalyst DFT computational electrocatalysis",
    # 异核双原子
    "heteronuclear dual-atom catalyst OR bimetallic single-atom catalyst DFT",
    # ORR/HER/OER/CO2RR 相关
    "dual-atom catalyst ORR OER HER CO2RR density functional theory",
]

# A 类文献分类标签
TARGET_TYPES = ["computational", "experimental"]

# 搜索渠道：默认使用后端默认渠道，可通过 --providers 指定
DEFAULT_PROVIDERS = None  # None = 使用后端默认 (openalex + arxiv + 已配置的扩展渠道)
ALL_PROVIDERS = ["openalex", "arxiv", "semantic_scholar", "pubmed", "scopus", "ieee"]


def api_request(method, path, data=None, timeout=120):
    """发送 API 请求"""
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"} if data else {}

    if data:
        body = json.dumps(data).encode("utf-8")
    else:
        body = None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"error": True, "status": e.code, "detail": error_body}
    except urllib.error.URLError as e:
        return {"error": True, "detail": str(e)}
    except Exception as e:
        return {"error": True, "detail": str(e)}


def search_papers(query, max_results=30, target_types=None, providers=None):
    """搜索外部文献"""
    payload = {
        "query": query,
        "max_results": max_results,
    }
    if target_types:
        payload["target_types"] = target_types
    if providers:
        payload["providers"] = providers

    return api_request("POST", "/api/intake/search", payload, timeout=60)


def approve_candidate(candidate_id):
    """审批候选文献"""
    return api_request("POST", f"/api/intake/candidates/{candidate_id}/approve", timeout=30)


def ingest_candidate(candidate_id, library_name=None):
    """入库候选文献"""
    payload = {}
    if library_name:
        payload["library_name"] = library_name
    return api_request("POST", f"/api/intake/candidates/{candidate_id}/ingest", payload, timeout=120)


def filter_by_year(candidates, year_min, year_max):
    """按年份过滤候选文献"""
    filtered = []
    for c in candidates:
        y = c.get("year")
        if y and year_min <= y <= year_max:
            filtered.append(c)
    return filtered


def filter_a_type(candidates):
    """过滤 A 类文献（计算型+实验型，排除纯综述）

    A 类文献定义：包含 DFT 计算或实验研究的原创论文。
    排除纯综述、展望、教程等。
    """
    filtered = []
    for c in candidates:
        title = (c.get("title") or "").lower()
        abstract = (c.get("abstract") or "").lower()
        # screening_tier 由后端 AI 自动评估
        tier = c.get("screening_tier", "")
        score = c.get("relevance_score", 0)

        # 如果后端判定为 recommended 或 highly_recommended，直接保留
        if tier in ("recommended", "highly_recommended"):
            # 但仍需排除纯综述
            review_keywords = ["review", "perspective", "outlook", "progress", "roadmap", "tutorial", "recent advances", "recent progress"]
            is_review = any(kw in title for kw in review_keywords)
            if is_review:
                # 综述只有在包含 DFT/计算内容时才保留
                computational_keywords = [
                    "dft", "density functional", "computational", "first-principles",
                    "ab initio", "theoretical calculation", "high-throughput screening",
                    "machine learning"
                ]
                text = title + " " + abstract
                if not any(kw in text for kw in computational_keywords):
                    continue
            filtered.append(c)
            continue

        # 低分或不相关的跳过
        if tier == "not_recommended" or (score and score < 0.3):
            continue

        # 对于 tier=neutral 或无 tier 的，手动判断
        computational_keywords = [
            "dft", "density functional", "computational", "first-principles",
            "ab initio", "theoretical", "calculation", "simulation",
            "machine learning", "high-throughput"
        ]
        experimental_keywords = [
            "synthesis", "experiment", "electrochem", "cataly",
            "performance", "activity", "stability", "characteriz"
        ]

        text = title + " " + abstract
        has_computational = any(kw in text for kw in computational_keywords)
        has_experimental = any(kw in text for kw in experimental_keywords)

        # 纯综述排除
        review_keywords = ["review", "perspective", "outlook", "progress", "roadmap", "tutorial"]
        is_review = any(kw in title for kw in review_keywords)
        if is_review and not has_computational:
            continue

        if has_computational or has_experimental:
            filtered.append(c)

    return filtered


def search_year(year, max_per_query=20, min_score=0.0, providers=None):
    """搜索某一年份的文献"""
    all_candidates = []
    seen_dois = set()

    for query_template in QUERIES_PER_YEAR:
        # 在查询中加入年份
        query = f"{query_template} {year}"
        print(f"  查询: {query[:80]}...")

        result = search_papers(query, max_results=max_per_query, target_types=TARGET_TYPES, providers=providers)

        if result.get("error"):
            print(f"  [!] 搜索出错: {result.get('detail', '')[:100]}")
            continue

        candidates = result.get("candidates", [])
        # 按年份过滤
        year_filtered = filter_by_year(candidates, year, year)
        # 按相关性评分过滤
        score_filtered = [c for c in year_filtered if c.get("relevance_score", 0) >= min_score]
        # A 类过滤
        a_type_filtered = filter_a_type(score_filtered)

        new_count = 0
        for c in a_type_filtered:
            doi = c.get("doi", "")
            if doi and doi not in seen_dois:
                seen_dois.add(doi)
                all_candidates.append(c)
                new_count += 1

        print(f"    返回 {len(candidates)} -> 年份 {len(year_filtered)} -> 评分 {len(score_filtered)} -> A类+{new_count}")
        time.sleep(1.5)  # 避免 API 限流

    # 按相关性评分降序排序
    all_candidates.sort(key=lambda c: c.get("relevance_score", 0), reverse=True)
    return all_candidates


def main():
    parser = argparse.ArgumentParser(description="搜索并入库单/双原子催化剂文献")
    parser.add_argument("--year", type=int, help="只搜索指定年份")
    parser.add_argument("--dry-run", action="store_true", help="只搜索，不入库")
    parser.add_argument("--auto-approve", action="store_true", help="自动审批并入库")
    parser.add_argument("--max-per-query", type=int, default=20, help="每次查询最大结果数")
    parser.add_argument("--min-score", type=float, default=0.0, help="最低相关性评分阈值")
    parser.add_argument("--library", type=str, default="双原子催化剂", help="目标库名")
    parser.add_argument("--providers", type=str, nargs="*", default=None,
                        help="搜索渠道 (openalex arxiv semantic_scholar pubmed scopus ieee). 默认使用后端配置")
    args = parser.parse_args()

    years = [args.year] if args.year else YEAR_RANGE
    providers = args.providers if args.providers else DEFAULT_PROVIDERS

    # 检查后端
    health = api_request("GET", "/api/health", timeout=10)
    if health.get("error"):
        print("[X] 无法连接到后端，请确保服务正在运行")
        sys.exit(1)
    print(f"[OK] 后端正常 | 活跃库: {health.get('active_library', '?')} | 现有论文: {health.get('effective_db_papers_total', '?')}")
    if providers:
        print(f"[Providers] 指定渠道: {providers}")
    else:
        print(f"[Providers] 使用后端默认渠道 (通常为 openalex + arxiv + 已配置的扩展渠道)")

    total_ingested = 0
    total_found = 0
    all_results = {}  # year -> candidates

    for year in years:
        print(f"\n{'='*60}")
        print(f"[Search] {year} 年文献...")
        print(f"{'='*60}")

        candidates = search_year(year, max_per_query=args.max_per_query, min_score=args.min_score, providers=providers)

        if not candidates:
            print(f"  {year} 年未找到符合条件的文献")
            all_results[year] = []
            continue

        print(f"\n  [List] {year} 年找到 {len(candidates)} 篇 A 类文献:")
        for i, c in enumerate(candidates, 1):
            title = (c.get("title") or "")[:70]
            doi = (c.get("doi") or "")[:50]
            oa = "[OA]" if c.get("is_open_access") else "[  ]"
            cid = c.get("id", "N/A")[:12]
            score = c.get("relevance_score", 0)
            tier = c.get("screening_tier", "?")
            status = c.get("status", "?")
            print(f"    {i:2d}. {oa} score={score:.2f} tier={tier} status={status} [{cid}...] {title}")
            print(f"         {doi}")

        all_results[year] = candidates
        total_found += len(candidates)

        if args.dry_run:
            print(f"  [Dry] dry-run 模式，跳过入库")
            continue

        if not args.auto_approve:
            answer = input(f"\n  是否入库这 {len(candidates)} 篇文献？(y/n/a=all): ").strip().lower()
            if answer == 'a':
                args.auto_approve = True
            elif answer != 'y':
                print(f"  跳过 {year} 年")
                continue

        # 审批并入库
        for i, c in enumerate(candidates, 1):
            cid = c.get("id")
            title = (c.get("title") or "")[:50]
            if not cid:
                print(f"    [!] 跳过（无 id）: {title}")
                continue

            # 检查状态，已 approved/ingesting/ingested 的跳过
            status = c.get("status", "")
            if status in ("approved", "ingesting", "ingested"):
                print(f"    [Skip] 已处理: {title} (status={status})")
                continue

            # 审批
            approve_result = approve_candidate(cid)
            if approve_result.get("error"):
                err_detail = str(approve_result.get('detail', ''))[:80]
                print(f"    [!] 审批失败 [{cid[:12]}]: {err_detail}")
                # 可能已经审批过了，尝试直接入库
                if "already" in err_detail.lower() or "approved" in err_detail.lower():
                    pass  # 继续入库
                else:
                    continue

            # 入库
            ingest_result = ingest_candidate(cid, library_name=args.library)
            if ingest_result.get("error"):
                err_detail = str(ingest_result.get('detail', ''))[:80]
                print(f"    [!] 入库失败 [{cid[:12]}]: {err_detail}")
                continue

            total_ingested += 1
            job_id = ingest_result.get("job_id", "?")
            print(f"    [OK] 入库成功 ({i}/{len(candidates)}): {title} [job={job_id}]")
            time.sleep(2)  # 避免过快请求

    # 保存搜索结果摘要
    summary_path = os.path.join(os.path.dirname(__file__), "search_results_summary.json")
    summary = {}
    for year, cands in all_results.items():
        summary[year] = [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "doi": c.get("doi"),
                "year": c.get("year"),
                "journal": c.get("journal"),
                "score": c.get("relevance_score"),
                "tier": c.get("screening_tier"),
                "status": c.get("status"),
                "is_open_access": c.get("is_open_access"),
            }
            for c in cands
        ]
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n  搜索结果已保存到: {summary_path}")

    print(f"\n{'='*60}")
    print(f"[Summary]")
    print(f"  搜索到 A 类文献: {total_found} 篇")
    print(f"  成功入库: {total_ingested} 篇")
    if args.dry_run:
        print(f"  [Dry] dry-run 模式，未实际入库")


if __name__ == "__main__":
    main()
