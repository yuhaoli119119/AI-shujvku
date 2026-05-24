from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceSpan,
    MechanismClaim,
    Paper,
    PaperFigure,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.services.paper_reprocessing import PaperReprocessingService


def test_rerun_stage2_replaces_existing_outputs():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "paper.md").write_text("Results markdown", encoding="utf-8")
        (tmp_path / "paper.tei.xml").write_text("<TEI/>", encoding="utf-8")
        (tmp_path / "paper.docling.json").write_text("{}", encoding="utf-8")

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(engine)

            with Session(engine) as session:
                paper = Paper(
                    title="Reprocess Me",
                    pdf_path=str(tmp_path / "paper.pdf"),
                    authors=[],
                    abstract="However, sluggish conversion remains a challenge. In this work, we propose Fe-N4 sites. Delta G = -0.45 eV.",
                    tei_path=str(tmp_path / "paper.tei.xml"),
                    docling_json_path=str(tmp_path / "paper.docling.json"),
                    markdown_path=str(tmp_path / "paper.md"),
                )
                session.add(paper)
                session.flush()
                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Results",
                        section_type="results",
                        text=(
                            "The adsorption energy of Li2S4 is -1.23 eV. "
                            "The Fe-N4 site can effectively adsorb polysulfides and suppress shuttle effect. "
                            "The sulfur loading was 3.8 mg/cm2 and the cell delivered 850 mAh/g at 1C after 300 cycles."
                        ),
                        page_start=3,
                        page_end=3,
                    )
                )
                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Computational details",
                        section_type="computational",
                        text=(
                            "The calculations were performed using VASP with PAW and the PBE functional. "
                            "The cutoff energy was set to 400 eV. A 3 x 3 x 1 k-point grid was used."
                        ),
                        page_start=2,
                        page_end=2,
                    )
                )
                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Experimental Section",
                        section_type="methods",
                        text="A single-atom Fe catalyst with Fe-N4 coordination on nitrogen-doped carbon was synthesized by pyrolysis. XANES and EXAFS confirmed the structure.",
                        page_start=1,
                        page_end=1,
                    )
                )
                session.add(
                    PaperTable(
                        paper_id=paper.id,
                        caption="Table 1",
                        markdown_content="| property | value |\n| --- | --- |\n| barrier | 0.75 eV |",
                        page=4,
                        extraction_source="docling",
                    )
                )
                session.add(
                    PaperFigure(
                        paper_id=paper.id,
                        caption="Figure 1. DOS overlap and charge density difference.",
                        image_path=None,
                        page=5,
                        figure_role="electronic_structure",
                    )
                )
                session.flush()

                session.add(DFTSetting(paper_id=paper.id, software="old"))
                session.add(CatalystSample(paper_id=paper.id, name="old"))
                session.add(DFTResult(paper_id=paper.id, property_type="old", evidence_text="old"))
                session.add(ElectrochemicalPerformance(paper_id=paper.id, capacity_value=1.0, evidence_text="old"))
                session.add(MechanismClaim(paper_id=paper.id, claim_type="old", claim_text="old"))
                session.add(WritingCard(paper_id=paper.id, paper_type="old"))
                session.add(EvidenceSpan(paper_id=paper.id, object_type="dft_setting", object_id="old", text="old"))
                session.add(EvidenceSpan(paper_id=paper.id, object_type="catalyst_sample", object_id="old", text="old"))
                session.add(EvidenceSpan(paper_id=paper.id, object_type="dft_result", object_id="old", text="old"))
                session.add(EvidenceSpan(paper_id=paper.id, object_type="electrochemical_performance", object_id="old", text="old"))
                session.commit()

                service = PaperReprocessingService(session=session, settings=Settings(storage_root=tmp_path))
                summary = service.rerun_stage2(paper.id)

                assert summary["dft_settings"] == 1
                assert summary["catalyst_samples"] == 1
                assert summary["dft_results"] >= 1
                assert summary["electrochemical_performance"] == 1
                assert summary["mechanism_claims"] >= 1
                assert summary["writing_cards"] == 1
                assert session.query(DFTSetting).filter(DFTSetting.paper_id == paper.id).count() == 1
                assert session.query(CatalystSample).filter(CatalystSample.paper_id == paper.id).count() == 1
                assert session.query(DFTResult).filter(DFTResult.paper_id == paper.id).count() >= 1
                assert session.query(ElectrochemicalPerformance).filter(ElectrochemicalPerformance.paper_id == paper.id).count() == 1
                assert session.query(MechanismClaim).filter(MechanismClaim.paper_id == paper.id).count() >= 1
                assert session.query(WritingCard).filter(WritingCard.paper_id == paper.id).count() == 1
                assert session.query(EvidenceSpan).filter(EvidenceSpan.paper_id == paper.id).count() >= 1
        finally:
            engine.dispose()


def test_classify_single_paper_fallback():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        engine = create_engine(f"sqlite:///{tmp_path / 'test_classify.db'}", future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                # 1. create a metadata_only paper
                paper = Paper(
                    title="DFT study of Fe-N4 single atom catalyst for lithium-sulfur batteries",
                    journal="Journal of Computational Chemistry",
                    pdf_path="",
                    oa_status="metadata_only",
                    authors=[],
                )
                session.add(paper)
                session.commit()
                session.refresh(paper)
                
                # Instantiate reprocessing service
                service = PaperReprocessingService(session=session, settings=Settings(storage_root=tmp_path))
                
                # Execute classification
                res = service.classify_single_paper(paper.id)
                assert res["paper_type"] == "A"
                assert res["classification_source"] == "rule_heuristic"
                assert res["type_confidence"] == 0.5
                
                # Verify database state
                db_paper = session.get(Paper, paper.id)
                assert db_paper.paper_type == "A"
                assert db_paper.classification_source == "rule_heuristic"
        finally:
            engine.dispose()

