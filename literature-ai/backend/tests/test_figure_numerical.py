from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Base,
    Paper,
    PaperFigure,
    FigureDataPoint,
    EvidenceSpan,
)
from app.schemas.documents import UnifiedFigure, UnifiedPaperDocument, UnifiedSection
from app.services.paper_ingestion import PaperIngestionService
from app.rag.retriever import Retriever
from app.rag.citation_guard import CitationGuard


def test_vlm_level3_extraction_and_rag_integration():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # 1. 模拟数据库初始化
        engine = create_engine(f"sqlite:///{tmp_path / 'test_numerical.db'}", future=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(engine)

            # 2. 构造 Ingestion 需要的 UnifiedPaperDocument 传输对象
            test_pdf = tmp_path / "test_paper.pdf"
            test_pdf.write_bytes(b"%PDF-1.4...")  # mock pdf file

            fig = UnifiedFigure(
                caption="Figure 3. Capacity voltage profiles.",
                image_path="fig3.png",
                page=3,
                figure_role="electrochemistry",
                role_confidence=0.95,
                content_summary="Specific capacity of Fe-N4 catalysts.",
                key_elements=["capacity", "Fe-N4"],
                numerical_data_points=[
                    {
                        "metric_name": "capacity",
                        "metric_value": 1200.0,
                        "unit": "mAh/g",
                        "sample_label": "Fe-N4/C",
                        "conditions": {"current_density": "0.1C"},
                        "confidence": 0.90
                    },
                    {
                        "metric_name": "adsorption_energy",
                        "metric_value": -0.85,
                        "unit": "eV",
                        "sample_label": "Fe-N4/C",
                        "conditions": {"adsorbate": "Li2S4"},
                        "confidence": 0.85
                    }
                ]
            )

            doc = UnifiedPaperDocument(
                metadata={"title": "Single Atom Catalyst Study", "authors": ["John Doe"]},
                abstract="Fe-N4 catalysts show great performance.",
                sections=[
                    UnifiedSection(section_title="Introduction", section_type="intro", text="Single atom catalysts are outstanding.", page_start=1, page_end=1)
                ],
                tables=[],
                figures=[fig],
                source_pdf_path=test_pdf
            )

            # 3. 持久化落库
            with Session(engine) as session:
                # 准备 settings
                settings = Settings()
                settings.writer_api_key = "mock_key"
                
                # 使用 mock 掉 run_stage2 的 IngestionService
                with patch("app.services.extraction_pipeline.ExtractionPipelineService.run_stage2") as mock_stage2:
                    ingestion = PaperIngestionService(session, settings)
                    paper = ingestion._persist(doc)
                    
                # 确认 `FigureDataPoint` 已经自愈建表并成功插入
                db_dps = session.scalars(
                    select(FigureDataPoint).where(FigureDataPoint.paper_id == paper.id)
                ).all()
                
                assert len(db_dps) == 2
                
                cap_dp = next(x for x in db_dps if x.metric_name == "capacity")
                assert cap_dp.metric_value == 1200.0
                assert cap_dp.unit == "mAh/g"
                assert cap_dp.sample_label == "Fe-N4/C"
                assert cap_dp.conditions == {"current_density": "0.1C"}
                assert cap_dp.confidence == 0.90

                # 确认 `EvidenceSpan` 是否已经针对数值点自动生成
                db_evs = session.scalars(
                    select(EvidenceSpan).where(EvidenceSpan.paper_id == paper.id)
                ).all()
                
                assert len(db_evs) == 2
                cap_ev = next(x for x in db_evs if "capacity" in x.text)
                assert cap_ev.object_type == "figure_data"
                assert cap_ev.figure == "Figure 3. Capacity voltage profiles."
                assert "current_density" in cap_ev.text
                assert "0.1C" in cap_ev.text
                
                # 4. 验证 Retriever 混合检索 FigureDataPoint 的召回与打分
                retriever = Retriever(session)
                # 用 "capacity" 作为 query，应该能精确匹配召回
                retrieved = retriever.retrieve("capacity", paper_ids=[paper.id])
                
                assert "figure_data_points" in retrieved
                fig_dps = retrieved["figure_data_points"]
                assert len(fig_dps) > 0
                
                retrieved_cap = next(x for x in fig_dps if x["metric_name"] == "capacity")
                assert retrieved_cap["value"] == 1200.0
                assert retrieved_cap["unit"] == "mAh/g"
                assert retrieved_cap["sample_label"] == "Fe-N4/C"
                assert "Figure 3" in retrieved_cap["text"]
                
                # 5. 验证 CitationGuard 数值护栏能够正常工作
                guard = CitationGuard()
                generated_text = "The synthesized Fe-N4/C catalyst exhibits a specific capacity of 1200 mAh/g."
                
                # 应该校验通过，因为 1200 mAh/g 在 retrieved 数据里得到了支持
                verdict = guard.validate(generated_text, retrieved)
                assert verdict["ok"] is True
                assert verdict["checked_count"] == 1
                assert len(verdict["supported_values"]) == 1
                assert verdict["supported_values"][0]["claim"]["value"] == 1200.0
                
                # 验证错误的数值是否会被拦截
                bad_text = "The synthesized Fe-N4/C catalyst exhibits a capacity of 850 mAh/g."
                bad_verdict = guard.validate(bad_text, retrieved)
                assert bad_verdict["ok"] is False
                assert len(bad_verdict["missing_values"]) == 1
                assert bad_verdict["missing_values"][0]["value"] == 850.0
        finally:
            engine.dispose()
