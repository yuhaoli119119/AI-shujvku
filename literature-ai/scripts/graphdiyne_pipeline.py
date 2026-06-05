from __future__ import annotations

import csv
import json
import re
import sys
import time
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


API_BASE = "http://127.0.0.1:8000"
LIBRARY_NAME = "石墨炔"
OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
USER_AGENT = "Mozilla/5.0 (compatible; LiteratureAI-GraphdiyneImport/1.0; local curation)"

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "data" / "imports" / "graphdiyne_30"
PDF_DIR = ARTIFACT_DIR / "pdfs"
REPORT_JSON = ARTIFACT_DIR / "graphdiyne_import_report.json"
REPORT_CSV = ARTIFACT_DIR / "graphdiyne_selected_papers.csv"

COMPUTATIONAL_TERMS = [
    "dft",
    "density functional",
    "first-principles",
    "first principles",
    "ab initio",
    "calculation",
    "calculations",
    "computational",
    "theoretical",
    "simulation",
    "molecular dynamics",
    "vasp",
    "gga",
    "pbe",
]

EXPERIMENTAL_TERMS = [
    "experimental",
    "experiment",
    "synthesized",
    "synthesis",
    "prepared",
    "validation",
    "validations",
    "electrochemical",
]

QUERY_SET = [
    "graphdiyne DFT",
    "graphdiyne density functional theory",
    "graphdiyne first-principles",
    "graphdiyne first principles",
    "graphdiyne ab initio",
    "graphdiyne computational",
    "graphdiyne theoretical",
    "graphdiyne simulation",
    "graphdiyne VASP",
    "graphdiyne PBE",
    "graphdiyne adsorption DFT",
    "graphdiyne lithium DFT",
    "graphdiyne sodium DFT",
    "graphdiyne battery DFT",
    "graphdiyne catalyst DFT",
    "graphdiyne single atom catalyst DFT",
    "graphdiyne oxygen reduction DFT",
    "graphdiyne hydrogen evolution DFT",
    "graphdiyne CO2 reduction DFT",
    "graphdiyne nitrogen reduction DFT",
    "graphdiyne electronic structure DFT",
    "graphdiyne magnetic DFT",
    "graphdiyne band gap DFT",
    "graphdiyne nanotube DFT",
    "graphdiyne membrane molecular dynamics",
]


@dataclass
class Candidate:
    key: str
    title: str
    doi: str | None
    year: int | None
    journal: str | None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    source_url: str | None = None
    pdf_urls: list[str] = field(default_factory=list)
    is_open_access: bool | None = None
    openalex_id: str | None = None
    cited_by_count: int = 0
    computational_hits: list[str] = field(default_factory=list)
    experimental_hits: list[str] = field(default_factory=list)
    selected_reason: str = ""
    downloaded_pdf: str | None = None
    paper_id: str | None = None
    import_status: str | None = None
    import_error: str | None = None


def get_json(url: str, *, params: dict[str, Any] | None = None, timeout: float = 25.0) -> dict[str, Any]:
    for attempt in range(3):
        response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if response.status_code in {429, 503} and attempt < 2:
            time.sleep(2 + attempt * 3)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Failed to fetch {url}")


def post_json(url: str, payload: dict[str, Any], *, timeout: float = 240.0) -> dict[str, Any]:
    response = requests.post(url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if response.status_code == 409:
        try:
            return {"_conflict": True, **response.json().get("detail", {})}
        except Exception:
            return {"_conflict": True, "message": response.text}
    response.raise_for_status()
    return response.json()


def wait_job(job_id: str, *, timeout: float = 900.0, interval: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_json(f"{API_BASE}/api/jobs/{job_id}", timeout=30)
        status = job.get("status")
        if status == "completed":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            return result or job
        if status in {"failed", "cancelled"}:
            raise RuntimeError(job.get("error") or f"job {job_id} {status}")
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for job {job_id}")


def record_activity(
    action: str,
    *,
    status: str = "completed",
    title: str | None = None,
    paper_id: str | None = None,
    paper_title: str | None = None,
    query: str | None = None,
    details: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "agent": "Codex",
        "action": action,
        "status": status,
        "library_name": LIBRARY_NAME,
        "title": title or action,
        "paper_id": paper_id,
        "paper_title": paper_title,
        "query": query,
        "details": details or {},
        "metrics": metrics or {},
        "artifacts": artifacts or [],
        "error": error,
    }
    try:
        requests.post(f"{API_BASE}/api/jobs/agent-activities", json=payload, timeout=20).raise_for_status()
    except Exception as exc:
        print(f"[WARN] failed to record activity {action}: {exc}", file=sys.stderr)


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    value = doi.strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.strip().rstrip(".").lower() or None


def inverted_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positions: dict[int, str] = {}
    for token, indexes in index.items():
        for pos in indexes or []:
            positions[int(pos)] = token
    return " ".join(token for _, token in sorted(positions.items())) if positions else None


def add_pdf_url(urls: list[str], value: str | None) -> None:
    if not value:
        return
    url = value.strip()
    if not url or url in urls:
        return
    if "doi.org/" in url.lower() and not url.lower().endswith(".pdf"):
        return
    urls.append(url)


def extract_pdf_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for location_key in ("best_oa_location", "primary_location"):
        location = item.get(location_key) or {}
        add_pdf_url(urls, location.get("pdf_url"))
    for location in item.get("locations") or []:
        add_pdf_url(urls, (location or {}).get("pdf_url"))
    oa_url = (item.get("open_access") or {}).get("oa_url")
    if oa_url and "arxiv.org/abs/" in oa_url:
        add_pdf_url(urls, oa_url.replace("/abs/", "/pdf/"))
    elif oa_url and oa_url.lower().endswith(".pdf"):
        add_pdf_url(urls, oa_url)
    return urls


def text_hits(text: str, terms: list[str]) -> list[str]:
    lower = text.lower()
    return sorted({term for term in terms if term in lower})


def candidate_from_openalex(item: dict[str, Any]) -> Candidate | None:
    title = (item.get("display_name") or "").strip()
    if not title:
        return None
    abstract = inverted_abstract(item.get("abstract_inverted_index"))
    doi = normalize_doi(item.get("doi"))
    key = f"doi:{doi}" if doi else f"openalex:{item.get('id')}"
    journal = (
        ((item.get("primary_location") or {}).get("source") or {}).get("display_name")
        or ((item.get("best_oa_location") or {}).get("source") or {}).get("display_name")
    )
    authors = [
        ((authorship.get("author") or {}).get("display_name"))
        for authorship in item.get("authorships", []) or []
        if (authorship.get("author") or {}).get("display_name")
    ]
    body = " ".join([title, abstract or ""])
    lower_body = body.lower()
    lower_title = title.lower()
    title_is_graphdiyne = "graphdiyne" in lower_title or re.search(r"\bgdy\b", lower_title, flags=re.I)
    body_mentions = lower_body.count("graphdiyne") + len(re.findall(r"\bgdy\b", lower_body, flags=re.I))
    if not title_is_graphdiyne and body_mentions < 2:
        return None
    comp = text_hits(body, COMPUTATIONAL_TERMS)
    if not comp:
        return None
    exp = text_hits(body, EXPERIMENTAL_TERMS)
    return Candidate(
        key=key,
        title=title,
        doi=doi,
        year=item.get("publication_year"),
        journal=journal,
        authors=authors,
        abstract=abstract,
        source_url=item.get("doi") or item.get("id") or ((item.get("primary_location") or {}).get("landing_page_url")),
        pdf_urls=extract_pdf_urls(item),
        is_open_access=(item.get("open_access") or {}).get("is_oa"),
        openalex_id=item.get("id"),
        cited_by_count=int(item.get("cited_by_count") or 0),
        computational_hits=comp,
        experimental_hits=exp,
        selected_reason=("experimental+computational" if exp else "pure/computational")
        + f"; hits={', '.join(comp[:6])}",
    )


def search_candidates() -> dict[str, Candidate]:
    candidates: dict[str, Candidate] = {}
    for query in QUERY_SET:
        payload = get_json(
            OPENALEX,
            params={
                "search": query,
                "per-page": 120,
                "filter": "type:article",
                "select": ",".join(
                    [
                        "id",
                        "doi",
                        "display_name",
                        "publication_year",
                        "primary_location",
                        "best_oa_location",
                        "locations",
                        "open_access",
                        "authorships",
                        "abstract_inverted_index",
                        "cited_by_count",
                    ]
                ),
            },
        )
        for item in payload.get("results") or []:
            candidate = candidate_from_openalex(item)
            if candidate is None:
                continue
            existing = candidates.get(candidate.key)
            if existing is None:
                candidates[candidate.key] = candidate
            else:
                for url in candidate.pdf_urls:
                    add_pdf_url(existing.pdf_urls, url)
        print(f"[search] {query}: total_candidates={len(candidates)}")
    return candidates


def enrich_crossref(candidate: Candidate) -> None:
    if not candidate.doi:
        return
    try:
        payload = get_json(f"{CROSSREF}/{quote(candidate.doi, safe='')}", timeout=20)
        msg = payload.get("message") or {}
    except Exception as exc:
        print(f"[WARN] Crossref enrichment failed for {candidate.doi}: {exc}")
        return
    title = (msg.get("title") or [None])[0]
    if title:
        candidate.title = title
    issued = msg.get("issued", {}).get("date-parts", [[]])
    if issued and issued[0] and issued[0][0]:
        candidate.year = int(issued[0][0])
    container = (msg.get("container-title") or [None])[0]
    if container:
        candidate.journal = container
    authors = []
    for author in msg.get("author") or []:
        given = author.get("given") or ""
        family = author.get("family") or ""
        name = " ".join(part for part in [given, family] if part).strip()
        if name:
            authors.append(name)
    if authors:
        candidate.authors = authors


def enrich_unpaywall(candidate: Candidate) -> None:
    if not candidate.doi:
        return
    try:
        payload = get_json(
            f"{UNPAYWALL}/{quote(candidate.doi, safe='/')}",
            params={"email": "literature-ai@example.com"},
            timeout=20,
        )
    except Exception as exc:
        print(f"[WARN] Unpaywall enrichment failed for {candidate.doi}: {exc}")
        return
    for location_key in ("best_oa_location", "first_oa_location"):
        location = payload.get(location_key) or {}
        add_pdf_url(candidate.pdf_urls, location.get("url_for_pdf"))
        landing = location.get("url")
        if landing and "arxiv.org/abs/" in landing:
            add_pdf_url(candidate.pdf_urls, landing.replace("/abs/", "/pdf/"))
    for location in payload.get("oa_locations") or []:
        add_pdf_url(candidate.pdf_urls, (location or {}).get("url_for_pdf"))


def rank_candidates(candidates: dict[str, Candidate]) -> list[Candidate]:
    for item in candidates.values():
        enrich_crossref(item)
        enrich_unpaywall(item)
    values = list(candidates.values())
    values.sort(
        key=lambda item: (
            1 if item.pdf_urls else 0,
            item.year or 0,
            len(item.computational_hits),
            item.cited_by_count,
        ),
        reverse=True,
    )
    ranked: list[Candidate] = []
    seen_titles: set[str] = set()
    for item in values:
        title_key = re.sub(r"\W+", " ", item.title.lower()).strip()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        ranked.append(item)
    return ranked


def select_final_candidates(ranked: list[Candidate], *, minimum_pdf_count: int = 15) -> list[Candidate]:
    title_primary: list[Candidate] = []
    secondary: list[Candidate] = []
    for item in ranked:
        title_lower = item.title.lower()
        if "graphdiyne" in title_lower or re.search(r"\bgdy\b", title_lower, flags=re.I):
            title_primary.append(item)
        else:
            secondary.append(item)

    probe_pool = [*title_primary[:80], *secondary[:40]]
    for index, item in enumerate(probe_pool, start=1):
        item.downloaded_pdf = download_pdf(index, item)
        print(f"[download] probe {index:02d}/{len(probe_pool)} pdf={'yes' if item.downloaded_pdf else 'no'} {item.title[:80]}")

    with_pdf = [item for item in probe_pool if item.downloaded_pdf]
    without_pdf = [item for item in probe_pool if not item.downloaded_pdf]
    with_pdf.sort(key=lambda item: (item.year or 0, item.cited_by_count), reverse=True)
    without_pdf.sort(key=lambda item: (item.year or 0, item.cited_by_count), reverse=True)

    selected: list[Candidate] = []
    for item in with_pdf[: max(minimum_pdf_count, min(len(with_pdf), 18))]:
        selected.append(item)
    for item in [*with_pdf[len(selected) :], *without_pdf]:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= 30:
            break
    return selected


def safe_pdf_filename(index: int, candidate: Candidate) -> str:
    doi = candidate.doi or candidate.title
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("._")[:90] or f"paper_{index:02d}"
    return f"{index:02d}_{stem}.pdf"


def download_pdf(index: int, candidate: Candidate) -> str | None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    target = PDF_DIR / safe_pdf_filename(index, candidate)
    if target.exists() and target.stat().st_size > 10_000:
        return str(target)
    for url in candidate.pdf_urls:
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,application/octet-stream,text/html;q=0.2,*/*;q=0.1",
            }
            try:
                response = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
            except requests.exceptions.SSLError:
                response = requests.get(url, headers=headers, timeout=60, allow_redirects=True, verify=False)
            if response.status_code in {403, 406} and "mdpi.com" in url:
                cleaner = re.sub(r"\?.*$", "", url)
                response = requests.get(cleaner, headers=headers, timeout=60, allow_redirects=True)
            response.raise_for_status()
            content = response.content
            if not content.lstrip().startswith(b"%PDF"):
                print(f"[pdf] skipped non-PDF {url}")
                continue
            if len(content) < 10_000:
                print(f"[pdf] skipped tiny PDF {url}")
                continue
            target.write_bytes(content)
            return str(target)
        except Exception as exc:
            print(f"[pdf] failed {url}: {exc}")
    return None


def to_container_path(host_path: str) -> str:
    path = Path(host_path).resolve()
    data_root = (ROOT / "data").resolve()
    rel = path.relative_to(data_root).as_posix()
    return f"/data/{rel}"


def import_candidate(candidate: Candidate) -> None:
    metadata = {
        "title": candidate.title,
        "doi": candidate.doi,
        "authors": candidate.authors,
        "year": candidate.year,
        "journal": candidate.journal,
        "abstract": candidate.abstract,
        "library_name": LIBRARY_NAME,
    }
    if candidate.downloaded_pdf:
        payload = {"pdf_path": to_container_path(candidate.downloaded_pdf), **metadata}
        try:
            job = post_json(f"{API_BASE}/api/papers/ingest/path/jobs", payload, timeout=30)
            result = wait_job(job["job_id"])
        except Exception as exc:
            candidate.import_status = "failed"
            candidate.import_error = str(exc)
            return
    else:
        identifier = candidate.doi or candidate.source_url or candidate.title
        try:
            job = post_json(
                f"{API_BASE}/api/papers/discovery/download/jobs",
                {
                    "identifier": identifier,
                    "providers": ["openalex", "crossref", "arxiv", "semantic_scholar", "web_scraping"],
                    "library_name": LIBRARY_NAME,
                },
                timeout=30,
            )
            result = wait_job(job["job_id"])
        except Exception as exc:
            candidate.import_status = "failed"
            candidate.import_error = str(exc)
            return

    candidate.paper_id = str(result.get("paper_id") or result.get("id") or "")
    candidate.import_status = result.get("status") or ("already_exists" if result.get("_conflict") else "completed")
    if not candidate.paper_id and result.get("_conflict"):
        candidate.paper_id = str(result.get("paper_id") or "")


def refresh_candidate_from_database(candidate: Candidate) -> None:
    params: dict[str, Any] = {"limit": 100}
    if candidate.doi:
        params["q"] = candidate.doi
    else:
        params["q"] = candidate.title[:80]
    try:
        rows = requests.get(f"{API_BASE}/api/papers/", params=params, timeout=30).json()
    except Exception:
        return
    normalized_doi = normalize_doi(candidate.doi)
    for row in rows:
        row_doi = normalize_doi(row.get("doi"))
        if normalized_doi and row_doi == normalized_doi:
            candidate.paper_id = row.get("id") or candidate.paper_id
            candidate.import_status = row.get("oa_status") or candidate.import_status
            if row.get("pdf_path") and not candidate.downloaded_pdf:
                candidate.downloaded_pdf = row.get("pdf_path")
            return
        if not normalized_doi and row.get("title") == candidate.title:
            candidate.paper_id = row.get("id") or candidate.paper_id
            candidate.import_status = row.get("oa_status") or candidate.import_status
            return


def reset_library_papers() -> None:
    rows = requests.get(
        f"{API_BASE}/api/papers/",
        params={"library_name": LIBRARY_NAME, "limit": 200},
        timeout=30,
    ).json()
    record_activity(
        "graphdiyne_reset_started",
        title="清理石墨炔第一轮试跑文献",
        metrics={"paper_count": len(rows)},
    )
    for row in rows:
        paper_id = row.get("id")
        if not paper_id:
            continue
        response = requests.delete(
            f"{API_BASE}/api/papers/{paper_id}",
            params={"delete_pdf": "true", "delete_derived": "true"},
            timeout=60,
        )
        response.raise_for_status()
    record_activity(
        "graphdiyne_reset_complete",
        title="石墨炔第一轮试跑文献清理完成",
        metrics={"deleted_count": len(rows)},
    )


def save_reports(selected: list[Candidate]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "index": index,
            "title": item.title,
            "doi": item.doi,
            "year": item.year,
            "journal": item.journal,
            "authors": "; ".join(item.authors[:8]),
            "selected_reason": item.selected_reason,
            "source_url": item.source_url,
            "oa_pdf_url": item.pdf_urls[0] if item.pdf_urls else "",
            "downloaded_pdf": item.downloaded_pdf or "",
            "paper_id": item.paper_id or "",
            "import_status": item.import_status or "",
            "import_error": item.import_error or "",
        }
        for index, item in enumerate(selected, start=1)
    ]
    REPORT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Delete current papers in the graphdiyne library before import.")
    args = parser.parse_args()

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    if args.reset:
        reset_library_papers()
    record_activity(
        "graphdiyne_retrieval_started",
        title="石墨炔 30 篇计算文献检索开始",
        query="graphdiyne DFT / first-principles / computational",
    )
    candidates = search_candidates()
    ranked = rank_candidates(candidates)
    selected = select_final_candidates(ranked, minimum_pdf_count=15)
    pdf_url_count = sum(1 for item in selected if item.pdf_urls)
    record_activity(
        "graphdiyne_candidate_selection",
        title="石墨炔候选文献筛选完成",
        metrics={
            "candidate_count": len(candidates),
            "ranked_count": len(ranked),
            "selected_count": len(selected),
            "selected_with_pdf_url": pdf_url_count,
            "selected_with_downloaded_pdf": sum(1 for item in selected if item.downloaded_pdf),
        },
        artifacts=[{"label": "selected_csv", "path": str(REPORT_CSV)}],
    )

    downloaded = sum(1 for item in selected if item.downloaded_pdf)
    record_activity(
        "graphdiyne_pdf_downloads",
        title="石墨炔开放 PDF 下载完成",
        metrics={"downloaded_pdf_count": downloaded, "target_minimum": 15},
        artifacts=[{"label": "pdf_dir", "path": str(PDF_DIR)}],
    )

    for index, item in enumerate(selected, start=1):
        import_candidate(item)
        refresh_candidate_from_database(item)
        record_activity(
            "graphdiyne_paper_import",
            status="failed" if item.import_status == "failed" else "completed",
            title=f"导入石墨炔文献 {index:02d}",
            paper_id=item.paper_id,
            paper_title=item.title,
            details={
                "doi": item.doi,
                "year": item.year,
                "journal": item.journal,
                "has_downloaded_pdf": bool(item.downloaded_pdf),
                "reason": item.selected_reason,
            },
            error=item.import_error,
        )
        print(f"[import] {index:02d}/{len(selected)} {item.import_status} {item.paper_id} {item.title[:80]}")

    save_reports(selected)
    success = sum(1 for item in selected if item.import_status and item.import_status != "failed")
    record_activity(
        "graphdiyne_import_batch_complete",
        title="石墨炔 30 篇文献批量导入完成",
        metrics={"selected_count": len(selected), "success_count": success, "downloaded_pdf_count": downloaded},
        artifacts=[
            {"label": "report_json", "path": str(REPORT_JSON)},
            {"label": "report_csv", "path": str(REPORT_CSV)},
            {"label": "pdf_dir", "path": str(PDF_DIR)},
        ],
    )
    print(json.dumps({"selected": len(selected), "success": success, "downloaded_pdf": downloaded}, ensure_ascii=False, indent=2))
    return 0 if len(selected) >= 30 and success >= 30 and downloaded >= 15 else 2


if __name__ == "__main__":
    raise SystemExit(main())
