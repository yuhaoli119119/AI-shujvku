from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import create_engine, text
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
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'dedup_existing.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
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
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'test.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
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
                settings.embedding_dimension = 8
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


def test_extraction_pipeline_merges_computational_views_and_adds_quality_checks():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'merge_test.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
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
                settings.embedding_dimension = 8
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
