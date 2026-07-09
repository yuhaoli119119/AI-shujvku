from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from app.db.models import PaperFigure


SUPPORT_FIGURE_DFT_SIGNAL_RE = re.compile(
    r"(?:\bDFT\b|density\s+functional|first[-\s]?principles?|computational|calculation|"
    r"adsorption\s+energ|binding\s+energ|free\s+energ|gibbs|delta\s*g|Δ\s*G|"
    r"binding\s+strength|Li2S\w*|Li2S\s*n|polysulfide|"
    r"reaction\s+barrier|activation\s+energ|bader|charge\s+transfer|charge\s+density|"
    r"differential\s+charge|electron\s+density|electronic\s+structure|\bDOS\b|\bPDOS\b|"
    r"band\s+structure|orbital\s+occupanc|d[-\s]?orbital|work\s+function|d[-\s]?band|cohp|icohp|\bVASP\b|\bPBE\b|"
    r"optimized\s+(?:structure|configuration)|adsorption\s+(?:configuration|site))",
    re.IGNORECASE,
)


def is_dft_related_support_figure(row: PaperFigure) -> bool:
    text = "\n".join(
        [
            str(row.figure_label or ""),
            str(row.caption or ""),
            str(row.figure_role or ""),
            str(row.content_summary or ""),
            json.dumps(row.key_elements or [], ensure_ascii=False, default=str),
            json.dumps(row.prov or {}, ensure_ascii=False, default=str),
        ]
    )
    return bool(SUPPORT_FIGURE_DFT_SIGNAL_RE.search(text))


def include_figure_in_chart_review_scope(
    row: PaperFigure,
    *,
    main_paper_id: UUID | None = None,
    source_prefix: str | None = None,
) -> bool:
    if source_prefix == "main":
        return True
    if main_paper_id is not None and row.paper_id == main_paper_id:
        return True
    return is_dft_related_support_figure(row)
