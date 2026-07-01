from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import ActiveSiteMetal, CatalystSample, ElementProperty, Paper
from app.services.active_site_enrichment_service import ActiveSiteEnrichmentService
from app.services.element_property_import_service import ElementPropertyImportService


def test_builtin_element_property_import_covers_118_elements_and_missing_metals(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        result = ElementPropertyImportService(session).import_builtin_snapshot()
        session.commit()
        assert result["row_count"] == 118
        assert result["inserted_count"] == 118

    with SessionLocal() as session:
        assert session.scalar(select(func.count(ElementProperty.symbol))) == 118
        for symbol in ("Zr", "Ag", "Au", "Hf", "Nb", "Sc", "Tc", "Y"):
            row = session.get(ElementProperty, symbol)
            assert row is not None
            assert row.atomic_number is not None
            assert row.electronegativity_pauling is not None
            assert row.data_version == "periodic_table_118_v1"


def test_active_site_backfill_refuses_multi_metal_screening_sets(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Active site paper", library_name="锂硫双原子", pdf_path="active-site.pdf")
        session.add(paper)
        session.flush()
        dual = CatalystSample(
            paper_id=paper.id,
            name="Fe-Co DAC",
            catalyst_type="dual_atom",
            metal_centers=["Fe", "Co"],
        )
        screening = CatalystSample(
            paper_id=paper.id,
            name="M-BP screening",
            catalyst_type="dual_atom",
            metal_centers=["Co", "Fe", "Ni"],
        )
        session.add_all([dual, screening])
        session.flush()
        dual_id = dual.id
        screening_id = screening.id
        result = ActiveSiteEnrichmentService(session).backfill_confirmed_sites(library_name="锂硫双原子")
        session.commit()

        assert result["inserted_count"] == 2
        assert result["skipped"] == {"screening_set_not_active_site": 1}

    with SessionLocal() as session:
        rows = session.scalars(select(ActiveSiteMetal).order_by(ActiveSiteMetal.site_role)).all()
        assert [row.site_role for row in rows] == ["M1", "M2"]
        assert [row.element_symbol for row in rows] == ["Fe", "Co"]
        assert {row.normalized_pair_key for row in rows} == {"Fe-Co"}
        assert {row.enrichment_status for row in rows} == {"system_enriched"}
        assert {row.catalyst_sample_id for row in rows} == {dual_id}
        assert session.scalar(
            select(func.count(ActiveSiteMetal.id)).where(ActiveSiteMetal.catalyst_sample_id == screening_id)
        ) == 0
