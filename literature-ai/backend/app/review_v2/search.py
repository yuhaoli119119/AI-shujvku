from __future__ import annotations
from typing import Any
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import CatalystSample, DFTSetting, Paper, PaperFigure, PaperRelationship
from .figure_types import REGISTRY, resolve_query

class FigureSearchService:
    def __init__(self, session: Session): self.session = session
    def search(self, *, query: str | None = None, figure_type: str | None = None, paper_id: UUID | None = None, year: int | None = None, material_system: str | None = None, catalyst: str | None = None, dft_condition: str | None = None, limit: int = 50) -> dict[str, Any]:
        stmt = select(PaperFigure, Paper).join(Paper, Paper.id == PaperFigure.paper_id)
        if paper_id:
            linked = self.session.scalars(select(PaperRelationship.target_paper_id).where(PaperRelationship.source_paper_id == paper_id, PaperRelationship.relationship_type == "supplementary")).all()
            stmt = stmt.where(Paper.id.in_([paper_id, *linked]))
        if year: stmt = stmt.where(Paper.year == year)
        rows = self.session.execute(stmt).all()
        type_targets = {figure_type} if figure_type else set()
        if figure_type and figure_type not in REGISTRY: type_targets = resolve_query(figure_type)
        query_types = resolve_query(query or "")
        results = []
        for fig, paper in rows:
            data = fig.reading_explanation if isinstance(fig.reading_explanation, dict) else {}
            types = list(data.get("figure_types") or [])
            panels = list(data.get("panel_types") or [])
            panel_keys = {key for panel in panels if isinstance(panel, dict) for key in panel.get("figure_types") or []}
            all_keys = set(types) | panel_keys
            if type_targets and not (all_keys & type_targets): continue
            if query_types and not (all_keys & query_types): continue
            catalysts = self.session.scalars(select(CatalystSample.name).where(CatalystSample.paper_id == paper.id)).all()
            dft_rows = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id == paper.id)).all()
            catalyst_text = " ".join(x or "" for x in catalysts)
            analysis = paper.comprehensive_analysis if isinstance(paper.comprehensive_analysis, dict) else {}
            material_text = " ".join(str(analysis.get(k) or "") for k in ("material_system", "battery_system", "research_topic"))
            dft_text = " ".join(" ".join(str(getattr(x, k, "") or "") for k in ("software", "functional", "dispersion_correction")) for x in dft_rows)
            if catalyst and catalyst.casefold() not in catalyst_text.casefold(): continue
            if material_system and material_system.casefold() not in material_text.casefold() and material_system.casefold() not in (paper.title or "").casefold(): continue
            if dft_condition and dft_condition.casefold() not in dft_text.casefold(): continue
            haystack = " ".join(str(x or "") for x in (fig.caption, fig.figure_role, fig.content_summary, data.get("summary_zh"), data.get("detailed_explanation_zh"), " ".join(data.get("type_search_terms") or []), paper.title, catalyst_text, material_text, dft_text)).casefold()
            if query and not query_types and query.casefold() not in haystack: continue
            matched_panels = [p for p in panels if isinstance(p, dict) and (not (query_types or type_targets) or set(p.get("figure_types") or []) & (query_types or type_targets))]
            results.append({"figure_id": str(fig.id), "source_paper_id": str(fig.paper_id), "paper_code": paper.paper_code, "paper_title": paper.title, "year": paper.year, "figure_label": fig.figure_label, "page": fig.page, "image_path": fig.image_path, "figure_type_primary": data.get("figure_type_primary"), "figure_types": types, "panel_types": panels, "matched_panel_types": matched_panels, "figure_role": fig.figure_role, "summary_zh": data.get("summary_zh"), "detailed_explanation_zh": data.get("detailed_explanation_zh"), "type_confidence": data.get("type_confidence"), "type_match": sorted(all_keys & (query_types or type_targets)) if (query_types or type_targets) else [], "catalysts": [x for x in catalysts if x], "dft_conditions": dft_text})
            if len(results) >= limit: break
        return {"query": query, "figure_type": figure_type, "resolved_query_types": sorted(query_types), "count": len(results), "items": results}
