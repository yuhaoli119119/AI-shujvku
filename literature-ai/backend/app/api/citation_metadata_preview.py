from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import get_db_session
from app.db.models import Paper, PaperImpactMetadata, PaperCitationEligibility
from sqlalchemy import select

router = APIRouter()

@router.get("/{paper_id}/citation-metadata-preview")
async def citation_metadata_preview(
    paper_id: UUID,
    session: Session = Depends(get_db_session)
) -> dict:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    impact = session.scalar(select(PaperImpactMetadata).where(PaperImpactMetadata.paper_id == paper_id))
    eligibility = session.scalar(select(PaperCitationEligibility).where(PaperCitationEligibility.paper_id == paper_id))

    # Identify missing fields
    missing_warnings = []
    required_fields = {
        "title": paper.title,
        "authors": paper.authors if paper.authors else None,
        "journal": paper.journal,
        "year": paper.year,
        "DOI": paper.doi,
        "volume": None, # Not modeled in DB currently
        "issue": None, # Not modeled in DB currently
        "pages": None, # Not modeled in DB currently
        "publisher": None, # Not modeled in DB currently
        "impact factor": impact.impact_factor if impact else None
    }
    
    for field, val in required_fields.items():
        if not val:
            missing_warnings.append(f"Missing {field}")

    # BibTeX Draft
    author_str = " and ".join(paper.authors) if paper.authors else "Unknown"
    bibtex_draft = f"""@article{{draft_{str(paper.id)[:8]},
  title={{{paper.title or 'Unknown'}}},
  author={{{author_str}}},
  journal={{{paper.journal or 'Unknown'}}},
  year={{{paper.year or 'Unknown'}}},
  doi={{{paper.doi or 'Unknown'}}},
  note={{DRAFT METADATA ONLY}}
}}"""

    # CSL JSON Draft
    csl_json_draft = {
        "id": f"draft_{str(paper.id)[:8]}",
        "type": "article-journal",
        "title": paper.title or "Unknown",
        "author": [{"family": a} for a in (paper.authors or ["Unknown"])],
        "container-title": paper.journal or "Unknown",
        "issued": {"date-parts": [[paper.year]]} if paper.year else {},
        "DOI": paper.doi or "Unknown",
        "note": "DRAFT METADATA ONLY"
    }

    # Safety status
    exclude_from_citation = eligibility.exclude_from_citation if eligibility else False
    safety_status = "excluded" if exclude_from_citation else "eligible_for_draft"

    return {
        "paper_id": str(paper_id),
        "warning_banner": "DRAFT METADATA ONLY - Do not use as final citation",
        "metadata_preview": required_fields,
        "bibtex_draft": bibtex_draft,
        "csl_json_draft": csl_json_draft,
        "missing_metadata_warnings": missing_warnings,
        "citation_safety_status": safety_status,
        "evidence_status": "metadata_only",
        "safety": {
            "read_only": True,
            "modifies_db": False
        }
    }
