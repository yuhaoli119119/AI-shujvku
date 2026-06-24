import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceSpan,
    MechanismClaim,
    Paper,
    WritingCard,
)
from app.normalizers.chemistry_normalizer import ChemistryNormalizer
from app.schemas.documents import UnifiedPaperDocument, UnifiedSection
from app.services.extraction_schema_service import ExtractionSchemaService
from app.services.extraction_pipeline import ExtractionPipelineService
from app.services.extraction_validator import ExtractionValidator


def _minimal_document(title="Reaction report test"):
    return UnifiedPaperDocument(
        metadata={"title": title},
        abstract="Computational screening of Li-S catalysts.",
        sections=[
            UnifiedSection(
                section_title="Results",
                section_type="results",
                text="DFT results are summarized.",
                page_start=1,
                page_end=1,
            )
        ],
        tables=[],
        figures=[],
        references=[],
        markdown="",
        tei_xml="",
        docling_json={},
        source_pdf_path=Path("test.pdf"),
    )


def _stage2_service_with_stubbed_extractors(session, dft_items):
    service = ExtractionPipelineService(session=session, settings=get_settings())
    service.comprehensive_extractor.extract_quick_classification = lambda doc: {
        "paper_type": "A1",
        "type_confidence": 0.9,
        "classification_source": "quick",
    }
    service.dft_settings_extractor.extract = lambda doc: {}
    service.dft_results_extractor.extract = lambda doc: dft_items
    service.catalyst_extractor.extract = lambda doc: {}
    service.electrochemical_extractor.extract = lambda doc: []
    service.mechanism_extractor.extract = lambda doc: []
    service.writing_card_extractor.extract = lambda doc: {}
    service.comprehensive_extractor.extract = lambda doc: {}
    return service


def test_stage2_preserves_supplementary_paper_type():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        paper = Paper(title="Supporting information", pdf_path="si.pdf", paper_type="supplementary")
        session.add(paper)
        session.flush()
        paper_id = paper.id

        service = _stage2_service_with_stubbed_extractors(session, [])
        service.comprehensive_extractor.extract = lambda doc: {
            "paper_type": "A1",
            "type_confidence": 0.91,
        }

        service.run_stage2(paper, _minimal_document("Supporting information"))
        session.commit()

    with Session(engine) as session:
        stored = session.get(Paper, paper_id)
        assert stored is not None
        assert stored.paper_type == "supplementary"


def test_dft_duplicate_candidates_merge_across_source_locations():
    service = object.__new__(ExtractionPipelineService)
    service.chemistry_normalizer = ChemistryNormalizer()

    items = [
        {
            "category": "adsorption_energy",
            "adsorbate": "water",
            "value": -0.1,
            "unit": "eV",
            "reaction_step": "solvent effect",
            "evidence_text": "Text reports water adsorption energy is -0.1 eV.",
            "source_location": {"section": "Results", "page": 3},
            "confidence": 0.7,
        },
        {
            "category": "adsorption_energy",
            "adsorbate": "H2O",
            "value": -0.1,
            "unit": "eV",
            "reaction_step": "solvent effect",
            "evidence_text": "Table 1 lists H2O adsorption energy as -0.1 eV.",
            "source_location": {"table": "Table 1", "page": 5},
            "confidence": 0.9,
        },
    ]

    merged = service._merge_duplicate_dft_items(items)

    assert len(merged) == 1
    assert merged[0]["evidence_text"] == "Table 1 lists H2O adsorption energy as -0.1 eV."
    assert len(merged[0]["evidence_sources"]) == 2


def test_dft_persist_merges_existing_duplicate_candidate_without_new_row():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = Paper(title="Duplicate Paper", pdf_path="dup.pdf", authors=[])
                session.add(paper)
                session.flush()
                existing = DFTResult(
                    paper_id=paper.id,
                    adsorbate="H2O",
                    property_type="adsorption_energy",
                    value=-0.1,
                    unit="eV",
                    reaction_step="solvent effect",
                    evidence_text="Original evidence.",
                    confidence=0.6,
                    candidate_status="system_candidate",
                    evidence_payload={"evidence_sources": [{"evidence_text": "Original evidence."}]},
                )
                session.add(existing)
                session.flush()

                service = object.__new__(ExtractionPipelineService)
                service.session = session
                service.chemistry_normalizer = ChemistryNormalizer()
                count = service._persist_dft_results(
                    paper.id,
                    [
                        {
                            "category": "adsorption_energy",
                            "adsorbate": "water",
                            "value": -0.1,
                            "unit": "eV",
                            "reaction_step": "solvent effect",
                            "evidence_text": "Updated table evidence.",
                            "source_location": {"table": "Table 1", "page": 4},
                            "confidence": 0.9,
                        }
                    ],
                )
                session.commit()

                assert count == 1
                assert session.query(DFTResult).count() == 1
                session.refresh(existing)
                assert existing.confidence == 0.9
                assert existing.evidence_text == "Updated table evidence."
                assert existing.evidence_payload["duplicate_merge"]["merged"] is True
                assert len(existing.evidence_payload["evidence_sources"]) >= 2
        finally:
            engine.dispose()


def test_extraction_pipeline_persists_stage2_outputs():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)

            with Session(engine) as session:
                paper = Paper(title="Test Paper", pdf_path="test.pdf", authors=[])
                session.add(paper)
                session.flush()

                document = UnifiedPaperDocument(
                    metadata={"title": "Test Paper"},
                    abstract="However, sluggish conversion remains a challenge. In this work, we propose Fe-N4 sites. Delta G = -0.45 eV.",
                    sections=[
                        UnifiedSection(
                            section_title="Results",
                            section_type="results",
                            text=(
                                "The adsorption energy of Li2S4 is -1.23 eV. "
                                "The Fe-N4 site can effectively adsorb polysulfides and suppress shuttle effect. "
                                "The sulfur loading was 4.2 mg/cm2 and the cell delivered 900 mAh/g at 0.5C after 200 cycles."
                            ),
                            page_start=3,
                            page_end=3,
                        ),
                        UnifiedSection(
                            section_title="Computational details",
                            section_type="computational",
                            text=(
                                "The calculations were performed using VASP with PAW and the PBE functional. "
                                "The cutoff energy was set to 30 Ry. A 3 x 3 x 1 k-point grid was used. "
                                "EDIFF = 1e-5 eV. A vacuum layer of 15 A was added. DFT-D3 correction was applied. "
                                "Atomic coordinates are provided in the Supplementary Information."
                            ),
                            page_start=2,
                            page_end=2,
                        ),
                        UnifiedSection(
                            section_title="Experimental Section",
                            section_type="methods",
                            text="A single-atom Fe catalyst with Fe-N4 coordination on nitrogen-doped carbon was synthesized by pyrolysis. XANES and EXAFS confirmed the structure.",
                            page_start=1,
                            page_end=1,
                        ),
                    ],
                    tables=[],
                    figures=[],
                    references=[],
                    markdown="",
                    tei_xml="",
                    docling_json={},
                    source_pdf_path=Path("test.pdf"),
                )

                settings = get_settings()
                service = ExtractionPipelineService(session=session, settings=settings)
                service.comprehensive_extractor.extract_quick_classification = lambda doc: {
                    "paper_type": "B1",
                    "type_confidence": 0.85,
                    "classification_source": "quick"
                }
                summary = service.run_stage2(paper, document)
                session.commit()

                assert summary["dft_settings"] == 1
                assert summary["catalyst_samples"] == 1
                assert summary["dft_results"] >= 1
                assert summary["electrochemical_performance"] == 1
                assert summary["writing_cards"] == 1
                assert session.query(DFTSetting).count() == 1
                setting = session.query(DFTSetting).one()
                assert round(setting.cutoff_energy_ev, 2) == 408.17
                assert session.query(CatalystSample).count() == 1
                assert session.query(DFTResult).count() >= 1
                assert session.query(ElectrochemicalPerformance).count() == 1
                assert session.query(MechanismClaim).count() >= 1
                assert session.query(WritingCard).count() == 1
                assert session.query(EvidenceSpan).count() >= 1
        finally:
            engine.dispose()


def test_run_stage2_preserves_counts_and_returns_dft_processing_report():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="Report Paper", pdf_path="report.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(
                session,
                [
                    {
                        "category": "adsorption_energy",
                        "adsorbate": "Li2S6",
                        "value": -1.25,
                        "unit": "eV",
                        "evidence_text": "The Li2S6 adsorption energy is -1.25 eV.",
                        "confidence": 0.88,
                    }
                ],
            )

            summary = service.run_stage2(paper, _minimal_document())

            for field in (
                "dft_settings",
                "catalyst_samples",
                "dft_results",
                "electrochemical_performance",
                "mechanism_claims",
                "writing_cards",
                "comprehensive_analysis",
            ):
                assert field in summary
            report = summary["dft_processing_report"]
            assert report["paper_id"] == str(paper.id)
            assert report["target_reaction"] == "SRR_LiS"
            assert report["profile_version"] == "reaction_profiles_v1"
            assert report["parse_status"] == "success"
            assert report["dft_candidates_total"] == 1
            assert report["persisted"] == summary["dft_results"] == 1
            assert report["persistence_errors"] == 0
            assert report["label_ready"] == 0
            assert report["tabular_ml_ready_by_task"] == {}
            assert report["rejection_reasons"] == {}
    finally:
        engine.dispose()


def test_run_stage2_reports_partial_success_for_dft_candidate_persistence_error():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="Partial Paper", pdf_path="partial.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(
                session,
                [
                    {
                        "category": "adsorption_energy",
                        "adsorbate": "Li2S6",
                        "value": -1.25,
                        "unit": "eV",
                        "evidence_text": "The first candidate should fail evidence persistence.",
                    },
                    {
                        "category": "adsorption_energy",
                        "adsorbate": "Li2S4",
                        "value": -1.05,
                        "unit": "eV",
                        "evidence_text": "The second candidate should persist.",
                    },
                ],
            )
            calls = 0

            def fail_first_evidence_span(**_):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("simulated evidence persistence failure")

            service._persist_evidence_span = fail_first_evidence_span

            summary = service.run_stage2(paper, _minimal_document("Partial Paper"))
            report = summary["dft_processing_report"]

            assert summary["dft_results"] == 1
            assert report["parse_status"] == "partial_success"
            assert report["dft_candidates_total"] == 2
            assert report["persisted"] == 1
            assert report["persistence_errors"] == 1
            assert report["rejection_reasons"]["persistence_error"] == 1
            assert report["persistence_error_details"][0]["adsorbate"] == "Li2S6"
            assert "simulated evidence persistence failure" in report["persistence_error_details"][0]["reason"]
            assert session.query(DFTResult).count() == 1
            assert session.query(DFTResult).one().adsorbate == "Li2S4"
    finally:
        engine.dispose()


def test_run_stage2_reports_success_when_no_dft_candidates():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="No DFT Paper", pdf_path="nodft.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(session, [])

            summary = service.run_stage2(paper, _minimal_document("No DFT Paper"))
            report = summary["dft_processing_report"]

            assert summary["dft_results"] == 0
            assert report["parse_status"] == "success"
            assert report["dft_candidates_total"] == 0
            assert report["persisted"] == 0
            assert report["persistence_errors"] == 0
            assert report["reaction_valid"] == 0
            assert report["out_of_scope"] == 0
            assert report["ambiguous"] == 0
    finally:
        engine.dispose()


def test_run_stage2_uses_explicit_target_reaction_as_classification_context():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="HER Paper", pdf_path="her.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(
                session,
                [
                    {
                        "category": "gibbs_free_energy_change",
                        "adsorbate": "*H",
                        "value": -0.05,
                        "unit": "eV",
                        "evidence_text": "The adsorption free energy of *H is -0.05 eV.",
                    }
                ],
            )

            summary = service.run_stage2(
                paper,
                _minimal_document("HER Paper"),
                target_reaction="HER",
            )
            report = summary["dft_processing_report"]
            row = session.scalars(select(DFTResult)).one()

            assert report["target_reaction"] == "HER"
            assert report["profile_version"] == "reaction_profiles_v1"
            assert row.reaction_type == "HER"
            assert row.reaction_type_source == "rule"
            assert row.reaction_validation_status == "valid"
    finally:
        engine.dispose()


def test_explicit_other_reaction_candidate_is_preserved_under_her_target():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="Mixed Paper", pdf_path="mixed.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(
                session,
                [
                    {
                        "reaction_type": "OER",
                        "category": "gibbs_free_energy_change",
                        "adsorbate": "*OOH",
                        "value": 0.42,
                        "unit": "eV",
                        "reaction_step": "*O -> *OOH",
                        "evidence_text": "For OER, the *OOH free energy change is 0.42 eV.",
                    }
                ],
            )

            summary = service.run_stage2(
                paper,
                _minimal_document("Mixed Paper"),
                target_reaction="HER",
            )
            row = session.scalars(select(DFTResult)).one()

            assert summary["dft_results"] == 1
            assert summary["dft_processing_report"]["target_reaction"] == "HER"
            assert row.reaction_type == "OER"
            assert row.reaction_type_source == "explicit"
            assert row.reaction_validation_status == "valid"
    finally:
        engine.dispose()


def test_no_dft_candidates_still_report_explicit_target_reaction():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="Empty HER Paper", pdf_path="empty-her.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(session, [])

            summary = service.run_stage2(
                paper,
                _minimal_document("Empty HER Paper"),
                target_reaction="HER",
            )
            report = summary["dft_processing_report"]

            assert report["target_reaction"] == "HER"
            assert report["profile_version"] == "reaction_profiles_v1"
            assert report["parse_status"] == "success"
            assert report["dft_candidates_total"] == 0
    finally:
        engine.dispose()


def test_invalid_target_reaction_uses_unknown_quarantine_profile():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="Unknown Target", pdf_path="unknown.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(
                session,
                [
                    {
                        "category": "gibbs_free_energy_change",
                        "adsorbate": "*H",
                        "value": -0.05,
                        "unit": "eV",
                        "evidence_text": "The adsorption free energy of *H is -0.05 eV.",
                    }
                ],
            )

            summary = service.run_stage2(
                paper,
                _minimal_document("Unknown Target"),
                target_reaction="not-a-real-reaction",
            )
            report = summary["dft_processing_report"]
            row = session.scalars(select(DFTResult)).one()

            assert report["target_reaction"] == "UNKNOWN"
            assert report["profile_version"] == "reaction_profiles_v1"
            assert row.reaction_type == "UNKNOWN"
            assert row.reaction_validation_status == "ambiguous"
    finally:
        engine.dispose()


def test_run_stage2_dft_processing_report_counts_reaction_statuses():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            paper = Paper(title="Status Paper", pdf_path="status.pdf", authors=[])
            session.add(paper)
            session.flush()
            service = _stage2_service_with_stubbed_extractors(
                session,
                [
                    {
                        "category": "adsorption_energy",
                        "adsorbate": "Li2S6",
                        "value": -1.25,
                        "unit": "eV",
                        "evidence_text": "The Li2S6 adsorption energy is -1.25 eV.",
                    },
                    {
                        "reaction_type": "SRR_LiS",
                        "category": "adsorption_energy",
                        "adsorbate": "*OOH",
                        "value": -0.32,
                        "unit": "eV",
                        "evidence_text": "The *OOH adsorption energy is -0.32 eV.",
                    },
                    {
                        "category": "gibbs_free_energy_change",
                        "adsorbate": "*OOH",
                        "value": 0.42,
                        "unit": "eV",
                        "reaction_step": "*O -> *OOH",
                        "evidence_text": "The free energy change for *OOH is 0.42 eV.",
                    },
                ],
            )

            summary = service.run_stage2(paper, _minimal_document("Status Paper"))
            report = summary["dft_processing_report"]

            assert report["parse_status"] == "success"
            assert report["dft_candidates_total"] == 3
            assert report["persisted"] == 3
            assert report["reaction_valid"] == 1
            assert report["out_of_scope"] == 2
            assert report["ambiguous"] == 0
    finally:
        engine.dispose()


def test_extraction_pipeline_merges_computational_views_and_adds_quality_checks():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)

            with Session(engine) as session:
                paper = Paper(title="Merge Paper", pdf_path="merge.pdf", authors=[])
                session.add(paper)
                session.flush()

                document = UnifiedPaperDocument(
                    metadata={"title": "Merge Paper"},
                    abstract="",
                    sections=[
                        UnifiedSection(
                            section_title="Results",
                            section_type="results",
                            text="The adsorption energy of Li2S4 is -1.23 eV.",
                            page_start=3,
                            page_end=3,
                        )
                    ],
                    tables=[],
                    figures=[],
                    references=[],
                    markdown="Results",
                    tei_xml="",
                    docling_json={},
                    source_pdf_path=Path("merge.pdf"),
                )

                settings = get_settings()
                service = ExtractionPipelineService(session=session, settings=settings)
                service.dft_settings_extractor.extract = lambda doc: {}
                service.catalyst_extractor.extract = lambda doc: {}
                service.comprehensive_extractor.extract_quick_classification = lambda doc: {
                    "paper_type": "A1",
                    "type_confidence": 0.9,
                    "classification_source": "quick"
                }
                service.electrochemical_extractor.extract = lambda doc: []
                service.mechanism_extractor.extract = lambda doc: []
                service.writing_card_extractor.extract = lambda doc: {
                    "paper_type": "computational",
                    "research_gap": "gap",
                    "proposed_solution": "solution",
                    "core_hypothesis": "hypothesis",
                    "evidence_chain": [
                        {"text": "The adsorption energy of Li2S4 is -1.23 eV.", "source": "Results"},
                    ],
                    "section_strategy": {},
                    "figure_logic": [],
                    "abstract_logic": "",
                    "introduction_logic": "",
                    "discussion_logic": "",
                }
                service.dft_results_extractor.extract = lambda doc: [
                    {
                        "category": "adsorption_energy",
                        "adsorbate": "Li2S4",
                        "value": -1.23,
                        "unit": "eV",
                        "evidence_text": "The adsorption energy of Li2S4 is -1.23 eV in the Results section.",
                        "source_location": {"section": "Results", "page": 3},
                        "confidence": 0.82,
                    }
                ]
                service.comprehensive_extractor.extract = lambda doc: {
                    "paper_type": "A1",
                    "type_confidence": 0.91,
                    "layman_summary": {
                        "one_sentence_takeaway": "摘要",
                        "real_world_impact": "意义",
                    },
                    "writing_logic": {
                        "research_gap_framing": "gap framing",
                        "core_hypothesis": "hypothesis",
                        "evidence_chain": [],
                        "conclusion_mapping": "mapping",
                    },
                    "computational_results": [],
                }

                summary = service.run_stage2(paper, document)
                session.commit()
                session.refresh(paper)

                assert summary["dft_results"] == 1
                assert paper.comprehensive_analysis is not None
                comp_results = paper.comprehensive_analysis["computational_results"]
                assert len(comp_results) == 1
                assert comp_results[0]["category"] == "adsorption_energy"
                assert paper.comprehensive_analysis["quality_checks"]["computational_results_merged_from_dft"] == 1
                assert paper.comprehensive_analysis["writing_logic"]["evidence_chain"]
        finally:
            engine.dispose()


def test_dft_validation_accepts_orr_potential_metrics():
    validator = ExtractionValidator()

    warnings = validator.validate_payload(
        {
            "DFTResult": [
                {
                    "energy_type": {
                        "value": "limiting_potential",
                        "evidence_text": "Table 1 reports limiting potential UL = 0.66 V.",
                    },
                    "value": {
                        "value": 0.66,
                        "unit": "V",
                        "evidence_text": "Table 1 reports limiting potential UL = 0.66 V.",
                    },
                    "reaction_step": {
                        "value": "Fe-N4-C / constant-Ne",
                        "evidence_text": "Table 1 reports limiting potential UL = 0.66 V.",
                    },
                },
                {
                    "energy_type": {
                        "value": "overpotential",
                        "evidence_text": "Table 1 reports overpotential eta = 0.57 V.",
                    },
                    "value": {
                        "value": 0.57,
                        "unit": "V",
                        "evidence_text": "Table 1 reports overpotential eta = 0.57 V.",
                    },
                },
                {
                    "energy_type": {
                        "value": "potential_determining_step",
                        "evidence_text": "The PDS is *O -> *OH.",
                    },
                    "reaction_step": {
                        "value": "*O -> *OH",
                        "evidence_text": "The PDS is *O -> *OH.",
                    },
                },
            ]
        }
    )

    assert [item.code for item in warnings if item.code == "unknown_energy_type"] == []


def test_dft_setting_schema_excludes_reproducibility_from_review_field():
    service = ExtractionSchemaService(session=None)  # type: ignore[arg-type]
    setting = DFTSetting(
        paper_id=uuid4(),
        convergence_settings={
            "convergence criteria": [{"value": "EDIFF = 1e-5 eV"}],
            "reproducibility": {"score": 70, "risk_level": "medium"},
        },
        raw_json={"extracted": {"convergence criteria": [{"value": "EDIFF = 1e-5 eV"}]}},
    )

    schema = service._dft_setting(setting)

    assert schema.convergence_settings.value == {
        "convergence criteria": [{"value": "EDIFF = 1e-5 eV"}]
    }

    setting.convergence_settings = {"reproducibility": {"score": 0, "risk_level": "high"}}
    schema = service._dft_setting(setting)
    assert schema.convergence_settings.value is None
