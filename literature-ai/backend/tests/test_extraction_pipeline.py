from pathlib import Path
from tempfile import TemporaryDirectory

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
from app.schemas.documents import UnifiedPaperDocument, UnifiedSection
from app.services.extraction_pipeline import ExtractionPipelineService


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
                                "The cutoff energy was set to 400 eV. A 3 x 3 x 1 k-point grid was used. "
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
