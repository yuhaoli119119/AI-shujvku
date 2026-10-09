from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests


API_BASE = "http://127.0.0.1:8000"
LIBRARY_NAME = "石墨炔"
ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "imports" / "graphdiyne_30" / "pdfs"

DELETE_DOIS = {
    "10.1038/s41467-025-62785-9",  # title does not clearly identify graphdiyne
    "10.1039/d3ma90095a",  # correction notice, not a primary paper
    "10.1021/jacs.3c01319",  # title does not clearly identify graphdiyne
    "10.1016/j.ijhydene.2022.03.209",  # retracted
    "10.1103/physrevb.105.085123",  # graphynes umbrella title, less targeted
}

REPLACEMENTS = [
    {
        "title": "N-, B-, P-, Al-, As-, and Ga-graphdiyne/graphyne lattices: first-principles investigation of mechanical, optical and electronic properties",
        "doi": "10.1039/c9tc00082h",
        "year": 2019,
        "journal": "Journal of Materials Chemistry C",
        "authors": ["Bohayra Mortazavi", "Masoud Shahrokhi", "Mohamed El-Amine Madjet", "Tanveer Hussain", "Xiaoying Zhuang"],
        "abstract": "We predicted novel N-, B-, P-, Al-, As-, Ga-graphdiyne/graphyne 2D lattices and explored their mechanical, thermal stability, electronic and optical characteristics.",
        "pdf": "64_10.1039_c9tc00082h.pdf",
    },
    {
        "title": "Boron-graphdiyne: a superstretchable semiconductor with low thermal conductivity and ultrahigh capacity for Li, Na and Ca ion storage",
        "doi": "10.1039/c8ta02627k",
        "year": 2018,
        "journal": "Journal of Materials Chemistry A",
        "authors": ["Bohayra Mortazavi", "Masoud Shahrokhi", "Xiaoying Zhuang", "Timon Rabczuk"],
        "abstract": "Density functional theory and molecular dynamics simulations were used to study mechanical, thermal, electronic, optical, and ion-storage properties of single-layer boron-graphdiyne.",
        "pdf": "65_10.1039_c8ta02627k.pdf",
    },
    {
        "title": "Theoretical Investigation: 2D N-Graphdiyne Nanosheets as Promising Anode Materials for Li/Na Rechargeable Storage Devices",
        "doi": "10.1021/acsanm.8b01751",
        "year": 2018,
        "journal": "ACS Applied Nano Materials",
        "authors": ["Meysam Makaremi", "Bohayra Mortazavi", "Timon Rabczuk", "Geoffrey A. Ozin", "Chandra Veer Singh"],
        "abstract": "First-principles calculations evaluate N-graphdiyne nanosheets as anode materials for Li and Na rechargeable storage devices.",
        "pdf": "66_10.1021_acsanm.8b01751.pdf",
    },
    {
        "title": "Structural and Electronic Properties of Graphdiyne Carbon Nanotubes from Large-Scale DFT Calculations",
        "doi": "10.1021/acs.jpcc.6b05265",
        "year": 2016,
        "journal": "The Journal of Physical Chemistry C",
        "authors": ["Sangavi Pari", "Abigail Cuellar", "Bryan M. Wong"],
        "abstract": "Large-scale DFT calculations investigate structural relaxation, effective mass, and band-gap scaling of graphdiyne carbon nanotubes.",
        "pdf": "73_10.1021_acs.jpcc.6b05265.pdf",
    },
    {
        "title": "Graphdiyne as a promising material for detecting amino acids",
        "doi": "10.1038/srep16720",
        "year": 2015,
        "journal": "Scientific Reports",
        "authors": ["Xi Chen", "Pengfei Gao", "Lei Guo", "Shengli Zhang"],
        "abstract": "Ab initio calculations and molecular dynamics simulations investigate adsorption of amino acids on single-layer graphdiyne.",
        "pdf": "74_10.1038_srep16720.pdf",
    },
]


def record_activity(action: str, **extra: Any) -> None:
    payload = {
        "agent": "Codex",
        "action": action,
        "status": extra.pop("status", "completed"),
        "library_name": LIBRARY_NAME,
        **extra,
    }
    requests.post(f"{API_BASE}/api/jobs/agent-activities", json=payload, timeout=20).raise_for_status()


def wait_job(job_id: str, *, timeout: float = 900.0, interval: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = requests.get(f"{API_BASE}/api/jobs/{job_id}", timeout=30)
        response.raise_for_status()
        job = response.json()
        status = job.get("status")
        if status == "completed":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            return result or job
        if status in {"failed", "cancelled"}:
            raise RuntimeError(job.get("error") or f"job {job_id} {status}")
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for job {job_id}")


def container_path(host_path: Path) -> str:
    data_root = (ROOT / "data").resolve()
    rel = host_path.resolve().relative_to(data_root).as_posix()
    return f"/data/{rel}"


def main() -> int:
    rows = requests.get(f"{API_BASE}/api/papers/", params={"library_name": LIBRARY_NAME, "limit": 200}, timeout=30).json()
    deleted = []
    for row in rows:
        doi = (row.get("doi") or "").lower()
        if doi not in DELETE_DOIS:
            continue
        paper_id = row["id"]
        requests.delete(
            f"{API_BASE}/api/papers/{paper_id}",
            params={"delete_pdf": "true", "delete_derived": "true"},
            timeout=60,
        ).raise_for_status()
        deleted.append({"paper_id": paper_id, "doi": doi, "title": row.get("title")})

    imported = []
    for item in REPLACEMENTS:
        pdf_path = PDF_DIR / item["pdf"]
        if not pdf_path.exists():
            raise FileNotFoundError(pdf_path)
        payload = {
            "pdf_path": container_path(pdf_path),
            "title": item["title"],
            "doi": item["doi"],
            "authors": item["authors"],
            "year": item["year"],
            "journal": item["journal"],
            "abstract": item["abstract"],
            "library_name": LIBRARY_NAME,
        }
        response = requests.post(f"{API_BASE}/api/papers/ingest/path/jobs", json=payload, timeout=30)
        response.raise_for_status()
        data = wait_job(response.json()["job_id"])
        imported.append({"paper_id": data.get("paper_id"), "doi": item["doi"], "title": item["title"]})

    record_activity(
        "graphdiyne_final_curation",
        title="石墨炔最终文献清理与替换完成",
        details={"deleted": deleted, "imported": imported},
        metrics={"deleted_count": len(deleted), "imported_count": len(imported)},
    )
    print(json.dumps({"deleted": deleted, "imported": imported}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
