from __future__ import annotations

import re
from pathlib import Path

from app.db.models import Base, EMBEDDING_DIMENSION


def test_postgresql_baseline_covers_all_model_tables():
    sql = Path("app/migrations/001_init.sql").read_text(encoding="utf-8")
    migration_tables = set(
        re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-zA-Z_][\w]*)", sql, flags=re.IGNORECASE)
    )
    model_tables = set(Base.metadata.tables)

    assert migration_tables == model_tables


def test_postgresql_baseline_uses_current_embedding_dimension():
    sql = Path("app/migrations/001_init.sql").read_text(encoding="utf-8")
    vector_dimensions = set(int(item) for item in re.findall(r"vector\((\d+)\)", sql, flags=re.IGNORECASE))

    assert vector_dimensions == {EMBEDDING_DIMENSION}
