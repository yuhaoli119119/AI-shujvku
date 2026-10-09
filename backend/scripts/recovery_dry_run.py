import sys
from pathlib import Path
import json

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from app.db.session import session_scope
from app.db.models import Paper
from app.config import get_settings
from app.utils.artifact_paths import resolve_persisted_artifact_path

def get_safe_doi(doi: str) -> str:
    if not doi:
        return ""
    return doi.replace("/", "_").replace("\\", "_")

def run_recovery_dry_run():
    settings = get_settings()
    pdf_dir = settings.storage_paths["pdf"]
    md_dir = settings.storage_paths["markdown"]
    docling_dir = settings.storage_paths["docling_json"]
    tei_dir = settings.storage_paths["tei"]
    
    available_pdfs = list(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []
    available_mds = list(md_dir.glob("*.md")) if md_dir.exists() else []
    available_doclings = list(docling_dir.glob("*.json")) if docling_dir.exists() else []
    available_teis = list(tei_dir.glob("*.xml")) if tei_dir.exists() else []
    
    print(f"Available PDFs: {len(available_pdfs)}")
    
    report = []
    
    with session_scope(settings.database_url) as db:
        papers = db.query(Paper).all()
        for paper in papers:
            paper_info = {
                "paper_id": str(paper.id),
                "serial_number": paper.serial_number,
                "title": paper.title,
                "doi": paper.doi,
                "stored_pdf_path": paper.pdf_path,
                "stored_md_path": paper.markdown_path,
                "stored_docling_path": paper.docling_json_path,
                "stored_tei_path": paper.tei_path,
                "stored_workspace_path": getattr(paper, "workspace_path", None),
                "fixes": {}
            }
            
            # PDF matching
            pdf_res = resolve_persisted_artifact_path(paper.pdf_path, category="pdf", settings=settings)
            if not (pdf_res and pdf_res.exists()):
                # try to match
                safe_doi = get_safe_doi(paper.doi) if paper.doi else ""
                candidates = []
                for p in available_pdfs:
                    if safe_doi and safe_doi in p.name:
                        candidates.append(p)
                    elif str(paper.id) in p.name:
                        candidates.append(p)
                
                # If safe_doi didn't match, maybe extract DOI from DB path
                if not candidates and paper.pdf_path:
                    parts = paper.pdf_path.split("_")
                    if len(parts) >= 3:
                        extracted_doi = "_".join(parts[2:]).replace(".pdf", "")
                        for p in available_pdfs:
                            if extracted_doi in p.name:
                                candidates.append(p)
                                
                if len(candidates) == 1:
                    paper_info["fixes"]["pdf_path"] = f"storage/pdf/{candidates[0].name}"
                elif len(candidates) > 1:
                    paper_info["fixes"]["pdf_path"] = "MULTIPLE_CANDIDATES"
            
            # MD matching
            md_res = resolve_persisted_artifact_path(paper.markdown_path, category="markdown", settings=settings)
            if not (md_res and md_res.exists()):
                safe_doi = get_safe_doi(paper.doi) if paper.doi else ""
                candidates = []
                for p in available_mds:
                    if safe_doi and safe_doi in p.name:
                        candidates.append(p)
                    elif str(paper.id) in p.name:
                        candidates.append(p)
                if not candidates and paper.markdown_path:
                    parts = paper.markdown_path.split("_")
                    if len(parts) >= 3:
                        extracted_doi = "_".join(parts[2:]).replace(".md", "")
                        for p in available_mds:
                            if extracted_doi in p.name:
                                candidates.append(p)
                if len(candidates) == 1:
                    paper_info["fixes"]["markdown_path"] = f"storage/markdown/{candidates[0].name}"
                elif len(candidates) > 1:
                    paper_info["fixes"]["markdown_path"] = "MULTIPLE_CANDIDATES"

            # DOCLING matching
            doc_res = resolve_persisted_artifact_path(paper.docling_json_path, category="docling_json", settings=settings)
            if not (doc_res and doc_res.exists()):
                safe_doi = get_safe_doi(paper.doi) if paper.doi else ""
                candidates = []
                for p in available_doclings:
                    if safe_doi and safe_doi in p.name:
                        candidates.append(p)
                    elif str(paper.id) in p.name:
                        candidates.append(p)
                if not candidates and paper.docling_json_path:
                    parts = paper.docling_json_path.split("_")
                    if len(parts) >= 3:
                        extracted_doi = "_".join(parts[2:]).replace(".docling.json", "")
                        for p in available_doclings:
                            if extracted_doi in p.name:
                                candidates.append(p)
                if len(candidates) == 1:
                    paper_info["fixes"]["docling_json_path"] = f"storage/docling_json/{candidates[0].name}"
                elif len(candidates) > 1:
                    paper_info["fixes"]["docling_json_path"] = "MULTIPLE_CANDIDATES"
                    
            # TEI matching
            tei_res = resolve_persisted_artifact_path(paper.tei_path, category="tei", settings=settings)
            if not (tei_res and tei_res.exists()):
                safe_doi = get_safe_doi(paper.doi) if paper.doi else ""
                candidates = []
                for p in available_teis:
                    if safe_doi and safe_doi in p.name:
                        candidates.append(p)
                    elif str(paper.id) in p.name:
                        candidates.append(p)
                if not candidates and paper.tei_path:
                    parts = paper.tei_path.split("_")
                    if len(parts) >= 3:
                        extracted_doi = "_".join(parts[2:]).replace(".tei.xml", "")
                        for p in available_teis:
                            if extracted_doi in p.name:
                                candidates.append(p)
                if len(candidates) == 1:
                    paper_info["fixes"]["tei_path"] = f"storage/tei/{candidates[0].name}"
                elif len(candidates) > 1:
                    paper_info["fixes"]["tei_path"] = "MULTIPLE_CANDIDATES"
            
            # Workspace matching
            if paper.workspace_path != f"by_id/{paper.id}":
                 paper_info["fixes"]["workspace_path"] = f"by_id/{paper.id}"
            
            report.append(paper_info)
            
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    
    with open("reports/artifact_path_recovery_dry_run.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        
    with open("reports/artifact_path_recovery_dry_run.md", "w", encoding="utf-8") as f:
        f.write("# Artifact Path Recovery Dry Run\n\n")
        f.write(f"Total Papers: {len(report)}\n\n")
        for p in report:
            f.write(f"## {p['title']} (ID: {p['paper_id']})\n")
            f.write(f"- DOI: {p['doi']}\n")
            if p['fixes']:
                f.write("### Proposed Fixes\n")
                for k, v in p['fixes'].items():
                    f.write(f"- **{k}**: `{v}`\n")
            else:
                f.write("*No fixes needed or possible.*\n")
            f.write("\n")
            
    print("Dry run complete. See reports/artifact_path_recovery_dry_run.json and .md")

if __name__ == '__main__':
    run_recovery_dry_run()
