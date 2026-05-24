import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# 添加 sys.path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from app.config import Settings
from app.db.models import Base, DFTResult, ElectrochemicalPerformance, MechanismClaim, Paper, PaperSection, WritingCard
from app.rag.retriever import Retriever
from app.rag.writer import Writer
from app.rag.citation_guard import CitationGuard

def run_debug():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'rag.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(engine)

            with Session(engine) as session:
                paper = Paper(title="RAG Paper", pdf_path="rag.pdf", authors=[])
                session.add(paper)
                session.flush()

                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Introduction",
                        section_type="introduction",
                        text="Sluggish LiPS conversion remains a challenge in lithium-sulfur batteries with single-atom catalysts.",
                        page_start=1,
                        page_end=1,
                    )
                )
                session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title="Discussion",
                        section_type="discussion",
                        text="These data indicate that stronger Li2S4 binding accelerates LiPS conversion and improves cycling stability.",
                        page_start=5,
                        page_end=5,
                    )
                )
                session.add(
                    DFTResult(
                        paper_id=paper.id,
                        adsorbate="Li2S4",
                        property_type="adsorption_energy",
                        value=-1.23,
                        unit="eV",
                        evidence_text="The adsorption energy of Li2S4 on Fe-N4 was -1.23 eV.",
                    )
                )
                session.add(
                    ElectrochemicalPerformance(
                        paper_id=paper.id,
                        sulfur_loading_mg_cm2=4.2,
                        capacity_value=900.0,
                        rate="0.5C",
                        cycle_number=200,
                        evidence_text="The cell delivered 900 mAh/g at 0.5C after 200 cycles.",
                    )
                )
                session.add(
                    MechanismClaim(
                        paper_id=paper.id,
                        claim_type="lips_conversion",
                        claim_text="Fe-N4 accelerates LiPS conversion by strengthening intermediate binding.",
                        evidence_types=["Li2S4", "DOS"],
                        evidence_text="The catalyst accelerates LiPS conversion through stronger Li2S4 binding.",
                    )
                )
                session.add(
                    WritingCard(
                        paper_id=paper.id,
                        paper_type="mixed",
                        research_gap="existing sulfur hosts still struggle to balance adsorption and conversion",
                        proposed_solution="Fe-N4 single-atom sites are introduced to regulate sulfur redox intermediates",
                        core_hypothesis="strong but not overly irreversible LiPS binding can improve bidirectional redox kinetics",
                        figure_logic='[{"fig_id":"Figure 1","purpose":"structure"},{"fig_id":"Figure 2","purpose":"DFT evidence"}]',
                    )
                )
                session.commit()

                # 执行 RAG 检索并打印
                retrieved = Retriever(session).retrieve("Fe-N4 Li2S4 adsorption conversion lithium sulfur", [paper.id], 3)
                print("\n=== RETRIEVED (Fe-N4 Li2S4 ...) ===")
                for k, v in retrieved.items():
                    print(f"- {k}: {[item['text'] for item in v]}")

                # 执行 Writer.write 并打印
                draft = Writer(session, settings=Settings(writer_backend="rule")).write(
                    topic="Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    paper_ids=[paper.id],
                )
                print("\n=== WRITER DRAFT DISCUSSION ===")
                print(draft["discussion"])
                print("\n=== GUARD ACTIONS ===")
                print(draft["guard_actions"])

                # 打印校验明细
                guard = CitationGuard()
                rule_disc = Writer(session, settings=Settings(writer_backend="rule"))._build_rule_sections(
                    "Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    retrieved,
                    ["discussion"],
                )["discussion"]
                print("\n=== ORIGINAL RULE DISCUSSION ===")
                print(rule_disc)
                print("\n=== TEXTUAL CLAIMS IN ORIGINAL RULE DISCUSSION ===")
                print(guard._extract_textual_claims(rule_disc))

                rule_intro = Writer(session, settings=Settings(writer_backend="rule"))._build_rule_sections(
                    "Fe-N4 single-atom catalysts for lithium-sulfur cathodes",
                    retrieved,
                    ["introduction"],
                )["introduction"]
                print("\n=== ORIGINAL RULE INTRODUCTION ===")
                print(rule_intro)
                print("\n=== TEXTUAL CLAIMS IN ORIGINAL RULE INTRODUCTION ===")
                print(guard._extract_textual_claims(rule_intro))

        finally:
            engine.dispose()

if __name__ == "__main__":
    run_debug()
