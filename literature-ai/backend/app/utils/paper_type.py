from typing import List, Optional

def normalize_paper_type_filter(target_paper_type: Optional[str]) -> Optional[List[str]]:
    """
    Normalizes a requested paper type string into a single-character prefix filter list.
    E.g., "A-1" -> ["A"], "C" -> ["C"], None -> None.
    """
    if not target_paper_type:
        return None
    stripped = target_paper_type.strip()
    if not stripped:
        return None
    prefix = stripped[0].upper()
    return [prefix]
