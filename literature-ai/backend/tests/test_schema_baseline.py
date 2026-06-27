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


def test_postgresql_baseline_keeps_required_extensions_and_library_scoped_doi():
    sql = Path("app/migrations/001_init.sql").read_text(encoding="utf-8")

    for extension in ("vector", "pgcrypto", "pg_trgm"):
        assert re.search(rf"CREATE EXTENSION IF NOT EXISTS\s+{extension}\s*;", sql, flags=re.IGNORECASE)

    assert "CONSTRAINT uq_papers_library_doi UNIQUE (library_name, doi)" in sql
    assert "papers_doi_key" not in sql
    assert not re.search(r"UNIQUE\s*\(\s*doi\s*\)", sql, flags=re.IGNORECASE)
    assert "library_name VARCHAR(255) DEFAULT '默认文献库' NOT NULL" in sql


def test_migration_files_are_utf8_without_common_mojibake_markers():
    markers = ("榛樿", "鏂囩", "\ufffd")

    for path in Path("app/migrations").glob("*.sql"):
        sql = path.read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in sql, f"{path} contains mojibake marker {marker!r}"


def test_project_library_v4_optional_physical_tables_migration_covers_core_entities():
    sql = Path("app/migrations/003_project_library_v4_physical_tables.sql").read_text(encoding="utf-8")

    for table in (
        "project_library_active_site_instances",
        "project_library_adsorbate_properties",
        "project_library_reaction_step_properties",
        "project_library_electronic_properties",
        "project_library_structure_properties",
        "project_library_ambiguous_records",
    ):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS\s+{table}\b", sql, flags=re.IGNORECASE)

    for field in (
        "active_site_instance_key",
        "adsorption_energy_eV",
        "reaction_step",
        "bader_charge_M1",
        "bader_charge_M2",
        "charge_transfer_e",
        "metal_metal_distance_A",
        "coordination_environment",
        "resolution_status",
    ):
        assert field in sql

    assert "UNIQUE (catalyst_sample_id, active_site_instance_key)" in sql
    assert "needs_user_decision" in sql
