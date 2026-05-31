from __future__ import annotations

from typing import Any
from uuid import UUID
from dataclasses import dataclass
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import Paper


@dataclass(frozen=True)
class ExportCard:
    draft_text: str
    paper_id: UUID | None
    evidence_status: str | None


@dataclass(frozen=True)
class WritingExportRequest:
    cards: list[ExportCard]
    export_format: str = "markdown"
    include_bibliography: bool = True


class WritingExportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def export(self, request: WritingExportRequest) -> dict[str, Any]:
        compiled_markdown_lines = []
        csl_json_list = []
        bibtex_blocks = []
        
        contains_unverified = False
        processed_paper_ids = set()

        for idx, card in enumerate(request.cards):
            is_verified = (card.evidence_status == "safe_verified")
            if not is_verified:
                contains_unverified = True

            text = card.draft_text.strip()
            if not is_verified:
                # Flag unverified paragraphs clearly
                text = f"**[UNVERIFIED]** {text}"

            compiled_markdown_lines.append(text)

            if card.paper_id and card.paper_id not in processed_paper_ids:
                processed_paper_ids.add(card.paper_id)
                paper = self.session.get(Paper, card.paper_id)
                if paper:
                    csl, bib = self._generate_bibliography_entry(paper, not is_verified)
                    csl_json_list.append(csl)
                    bibtex_blocks.append(bib)

        # Build final markdown document
        final_markdown = "\n\n".join(compiled_markdown_lines)
        if contains_unverified:
            warning_banner = (
                "> [!WARNING]\n"
                "> This document contains unverified draft citations. "
                "Ensure all evidence is promoted to `safe_verified` before using in a final manuscript.\n\n"
            )
            final_markdown = warning_banner + final_markdown

        bibliography_text = ""
        if request.include_bibliography and bibtex_blocks:
            final_markdown += "\n\n## References\n\n```bibtex\n"
            final_markdown += "\n\n".join(bibtex_blocks)
            final_markdown += "\n```"
            bibliography_text = "\n\n".join(bibtex_blocks)

        return {
            "compiled_markdown": final_markdown,
            "bibliography": {
                "bibtex": bibliography_text,
                "csl_json": csl_json_list,
            },
            "safety": {
                "contains_unverified": contains_unverified,
                "generates_bibliography": True,
                "read_only": True,
            }
        }

    def _generate_bibliography_entry(self, paper: Paper, unverified: bool) -> tuple[dict, str]:
        author_str = " and ".join(paper.authors) if paper.authors else "Unknown"
        bib_id = f"ref_{str(paper.id)[:8]}"
        note = "UNVERIFIED DRAFT CITATION" if unverified else "Verified Evidence-backed Citation"
        
        bibtex = f"""@article{{{bib_id},
  title={{{paper.title or 'Unknown'}}},
  author={{{author_str}}},
  journal={{{paper.journal or 'Unknown'}}},
  year={{{paper.year or 'Unknown'}}},
  doi={{{paper.doi or 'Unknown'}}},
  note={{{note}}}
}}"""

        csl = {
            "id": bib_id,
            "type": "article-journal",
            "title": paper.title or "Unknown",
            "author": [{"family": a} for a in (paper.authors or ["Unknown"])],
            "container-title": paper.journal or "Unknown",
            "issued": {"date-parts": [[paper.year]]} if paper.year else {},
            "DOI": paper.doi or "Unknown",
            "note": note
        }
        
        return csl, bibtex
