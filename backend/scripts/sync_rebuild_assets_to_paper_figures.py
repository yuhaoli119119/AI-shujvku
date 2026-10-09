"""Read-only compatibility inspector. Legacy image/reading writeback is disabled."""
from __future__ import annotations
import argparse
from collections import defaultdict
from uuid import UUID
from sqlalchemy import select
from app.config import get_settings
from app.db.models import RebuildPaperFile, RebuildVisualAsset
from app.db.session import session_scope


def source_groups(assets, files):
    files = {str(f.id): f for f in files}
    groups = defaultdict(list)
    for asset in assets:
        provenance = asset.provenance if isinstance(asset.provenance, dict) else {}
        if provenance.get("lifecycle") == "superseded" or provenance.get("superseded_by_asset_id"):
            continue
        file = files.get(str(asset.file_id))
        if file is None or file.paper_id != asset.paper_id:
            continue
        # Exact registered source and label: SI S1 never merges with main Figure 1.
        key = (str(asset.paper_id), str(file.id), file.role, asset.figure_label or asset.asset_key)
        groups[key].append(str(asset.id))
    return dict(groups)


def sync_paper(session, paper_id: UUID) -> int:
    """Compatibility return 0; do not read PDF, render, add, commit or mutate any record."""
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_id", type=UUID)
    args = parser.parse_args()
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        assets = list(session.scalars(select(RebuildVisualAsset).where(RebuildVisualAsset.paper_id == args.paper_id)))
        files = list(session.scalars(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == args.paper_id)))
        print({"paper_id": str(args.paper_id), "groups": len(source_groups(assets, files)), "updated": 0, "mode": "read_only"})
if __name__ == "__main__":
    main()
