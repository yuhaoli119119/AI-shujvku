from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ActiveSiteMetal, AuditLog, CatalystSample, DFTResult, Paper
from app.main import app


def test_update_catalyst_basic_info_standardizes_fields_and_audits(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(
            title="Catalyst basic info paper",
            library_name="锂硫双原子",
            pdf_path="basic-info.pdf",
            workflow_status="Initial_Parsed",
        )
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Co-GeC",
            catalyst_type="DAC",
            metal_centers=["co", "Co", "GeC"],
            coordination=None,
            support="Gr",
        )
        session.add(sample)
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={
            "name": "Co-GeC",
            "catalyst_type": "DAC",
            "metal_centers": ["co", "ge"],
            "coordination": "Co-Ge bridge",
            "support": "Gr",
            "source": "ai_auto_basic_info",
            "note": "AI filled from structured DFT grouping.",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    updated = payload["catalyst_sample"]
    assert updated["catalyst_type"] == "dual_atom"
    assert updated["metal_centers"] == ["Co", "Ge"]
    assert updated["support"] == "graphene"
    assert updated["support_normalized"] == "graphene"
    assert updated["metal_1_descriptors"]["element_symbol"] == "Co"
    assert updated["metal_1_descriptors"]["electronegativity"] == 1.88
    assert payload["active_site_refresh"]["active_site_status"] == "refreshed"
    assert payload["active_site_refresh"]["inserted_count"] == 2

    detail = client.get(f"/api/papers/{paper_id}", params={"mode": "full"})
    assert detail.status_code == 200, detail.text
    sample_payload = detail.json()["catalyst_samples_items"][0]
    assert sample_payload["support"] == "graphene"
    assert sample_payload["support_normalized"] == "graphene"
    assert sample_payload["metal_centers"] == ["Co", "Ge"]
    assert sample_payload["metal_1_descriptors"]["element_symbol"] == "Co"
    assert sample_payload["descriptor_blockers"] == []

    with SessionLocal() as session:
        stored = session.get(CatalystSample, UUID(sample_id))
        assert stored is not None
        assert stored.catalyst_type == "dual_atom"
        assert stored.support == "graphene"
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.paper_id == UUID(paper_id),
                AuditLog.action == "update_catalyst_basic_info",
                AuditLog.target_id == sample_id,
            )
        )
        assert audit is not None
        assert audit.payload["normalization"]["raw"]["support"] == "Gr"
        assert audit.payload["after"]["support"] == "graphene"
        assert audit.payload["active_site_refresh"]["active_site_status"] == "refreshed"
        active_site_rows = session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == UUID(sample_id)).order_by(ActiveSiteMetal.site_role)
        ).all()
        assert [row.site_role for row in active_site_rows] == ["M1", "M2"]
        assert [row.element_symbol for row in active_site_rows] == ["Co", "Ge"]
        assert {row.enrichment_status for row in active_site_rows} == {"system_enriched"}


def test_multi_metal_screening_set_does_not_generate_fake_dual_atom_descriptors(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Screening set", library_name="锂硫双原子", pdf_path="screening.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="M-BP screening collection",
            catalyst_type="dual_atom",
            metal_centers=["Co", "Fe", "Ni", "V"],
        )
        session.add(sample)
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    detail = client.get(f"/api/papers/{paper_id}", params={"mode": "full"})
    assert detail.status_code == 200, detail.text
    sample_payload = detail.json()["catalyst_samples_items"][0]
    assert sample_payload["metal_1_descriptors"] is None
    assert sample_payload["metal_2_descriptors"] is None
    assert sample_payload["dac_combined_descriptors"] is None
    assert "screening_set_not_active_site" in sample_payload["descriptor_blockers"]
    assert "too_many_metal_centers_for_descriptor" in sample_payload["descriptor_blockers"]


def test_update_catalyst_basic_info_clears_stale_active_site_rows_for_screening_set(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Stale site cleanup", library_name="锂硫双原子", pdf_path="cleanup.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Fe-Co DAC",
            catalyst_type="dual_atom",
            metal_centers=["Fe", "Co"],
        )
        session.add(sample)
        session.flush()
        session.add_all(
            [
                ActiveSiteMetal(
                    paper_id=paper.id,
                    catalyst_sample_id=sample.id,
                    active_site_key=f"catalyst:{sample.id}|site:confirmed_active_center",
                    site_type="dual_atom",
                    site_role="M1",
                    element_symbol="Fe",
                    element_order=1,
                    order_source="test",
                    normalized_pair_key="Fe-Co",
                    enrichment_status="system_enriched",
                ),
                ActiveSiteMetal(
                    paper_id=paper.id,
                    catalyst_sample_id=sample.id,
                    active_site_key=f"catalyst:{sample.id}|site:confirmed_active_center",
                    site_type="dual_atom",
                    site_role="M2",
                    element_symbol="Co",
                    element_order=2,
                    order_source="test",
                    normalized_pair_key="Fe-Co",
                    enrichment_status="system_enriched",
                ),
            ]
        )
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={
            "catalyst_type": "dual_atom",
            "metal_centers": ["Fe", "Co", "Ni"],
            "source": "literature_library_user",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["active_site_refresh"]["active_site_status"] == "skipped"
    assert payload["active_site_refresh"]["deleted_count"] == 2
    assert payload["active_site_refresh"]["skipped_reason"] == "screening_set_not_active_site"

    with SessionLocal() as session:
        active_site_rows = session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == UUID(sample_id))
        ).all()
        assert active_site_rows == []


def test_update_catalyst_basic_info_rejects_wrong_paper(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Paper A", library_name="A", pdf_path="a.pdf")
        other = Paper(title="Paper B", library_name="B", pdf_path="b.pdf")
        session.add_all([paper, other])
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Fe-N-C")
        session.add(sample)
        session.commit()
        other_id = str(other.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{other_id}/catalyst-samples/{sample_id}/basic-info",
        json={"support": "graphene"},
    )
    assert response.status_code == 404


def test_update_catalyst_basic_info_partial_payload_only_changes_provided_fields(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Partial catalyst update", library_name="A", pdf_path="partial.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Co-GeC",
            catalyst_type="DAC",
            metal_centers=["Co", "Ge"],
            support="graphene substrate",
        )
        session.add(sample)
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={"coordination": "Co-Ge bridge", "source": "ai_patch"},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        stored = session.get(CatalystSample, UUID(sample_id))
        assert stored is not None
        assert stored.coordination == "Co-Ge bridge"
        assert stored.support == "graphene substrate"
        assert stored.catalyst_type == "DAC"


def test_create_catalyst_basic_info_from_dft_group_and_bind_rows(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Unbound DFT group", library_name="A", pdf_path="unbound.pdf")
        session.add(paper)
        session.flush()
        rows = [
            DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.2, unit="eV"),
            DFTResult(paper_id=paper.id, property_type="barrier", value=0.4, unit="eV"),
        ]
        session.add_all(rows)
        session.commit()
        paper_id = str(paper.id)
        row_ids = [str(row.id) for row in rows]

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/from-dft-group",
        json={
            "dft_result_ids": row_ids,
            "name": "Co-GeC",
            "catalyst_type": "DAC",
            "metal_centers": ["Co", "Ge"],
            "support": "Gr",
            "source": "literature_library_frontend",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "created_and_bound"
    assert set(payload["bound_dft_result_ids"]) == set(row_ids)
    assert payload["active_site_refresh"]["active_site_status"] == "refreshed"
    assert payload["active_site_refresh"]["inserted_count"] == 2

    with SessionLocal() as session:
        sample = session.get(CatalystSample, UUID(payload["catalyst_sample_id"]))
        assert sample is not None
        assert sample.name == "Co-GeC"
        assert sample.catalyst_type == "dual_atom"
        assert sample.support == "graphene"
        stored_rows = session.scalars(
            select(DFTResult).where(DFTResult.id.in_([UUID(row_id) for row_id in row_ids]))
        ).all()
        assert {row.catalyst_sample_id for row in stored_rows} == {sample.id}
        active_site_rows = session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == sample.id).order_by(ActiveSiteMetal.site_role)
        ).all()
        assert [row.element_symbol for row in active_site_rows] == ["Co", "Ge"]
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.paper_id == UUID(paper_id),
                AuditLog.action == "create_or_bind_catalyst_sample",
            )
        )
        assert audit is not None
        assert audit.payload["created"] is True
        assert set(audit.payload["bound_dft_result_ids"]) == set(row_ids)


def test_create_catalyst_basic_info_reuses_unique_exact_name(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Reuse sample", library_name="A", pdf_path="reuse.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Co-GeC", support="GeC")
        row = DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.2, unit="eV")
        session.add_all([sample, row])
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/from-dft-group",
        json={
            "dft_result_ids": [row_id],
            "name": " co-gec ",
            "catalyst_type": "dual_atom",
            "metal_centers": ["Co", "Ge"],
            "support": "GeC",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "bound_existing"
    assert payload["catalyst_sample_id"] == sample_id
    with SessionLocal() as session:
        assert session.query(CatalystSample).filter(CatalystSample.paper_id == UUID(paper_id)).count() == 1
        stored = session.get(DFTResult, UUID(row_id))
        assert stored is not None
        assert str(stored.catalyst_sample_id) == sample_id
