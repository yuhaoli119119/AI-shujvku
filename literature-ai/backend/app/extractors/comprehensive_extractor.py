import logging
import re
from typing import Any

from app.config import Settings
from app.services.llm_service import LLMService
from app.schemas.comprehensive_analysis import ComprehensivePaperAnalysisModel

logger = logging.getLogger(__name__)

class ComprehensiveExtractor:
    """
    通用、多领域的文献解析大模型提取器.
    基于 Pydantic Structured Outputs 提取:
    - Layman Summary
    - 写作逻辑 (供 AI 学习)
    - 实验执行细节与结果
    - 计算执行细节与结果 (替代原 DFTResultsExtractor 部分功能)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.llm = LLMService(settings) if settings else None

    @staticmethod
    def _coerce_input(unified_document: Any) -> Any:
        if isinstance(unified_document, list):
            return type("_NS", (), {"sections": unified_document,
                                      "tables": [], "figures": [], "abstract": "",
                                      "markdown": ""})()
        if isinstance(unified_document, dict):
            ns = type("_NS", (),
                       {"sections": unified_document.get("sections", []),
                        "tables": unified_document.get("tables", []),
                        "figures": unified_document.get("figures", []),
                        "abstract": unified_document.get("abstract", ""),
                        "markdown": unified_document.get("markdown", ""),
                        **{k: v for k, v in unified_document.items()
                           if k not in ("sections", "tables", "figures",
                                        "abstract", "markdown")}})()
            return ns
        return unified_document

    def extract(self, unified_document: Any) -> dict | None:
        doc = self._coerce_input(unified_document)
        markdown = getattr(doc, "markdown", "") or ""
        abstract = getattr(doc, "abstract", "") or ""

        if not self.llm or not self.llm.is_configured():
            logger.warning("LLMService is not configured. Comprehensive extraction requires an LLM.")
            return None

        if not markdown and not abstract:
            logger.warning("No markdown or abstract found in the document to extract comprehensive analysis.")
            return None

        # Paper type classification mapping (10 types + Review)
        type_options = (
            "A1 (纯计算-催化机理), A2 (纯计算-电子结构), A3 (纯计算-高通量筛选), A4 (纯计算-分子动力学), "
            "B1 (计算+实验-电催化), B2 (计算+实验-储能材料), B3 (计算+实验-热催化), "
            "C1 (纯实验-新材料合成), C2 (纯实验-器件性能), C3 (纯实验-原位表征), "
            "R (综述), Unknown"
        )
        
        system_prompt = (
            "你是一位专业的催化化学与计算化学学术审稿人。\n"
            f"分析提供的学术论文文本。\n"
            "首先，根据摘要和全文内容判断论文类型，从以下选项中选择一个：\n"
            f"{type_options}\n\n"
            "分类标准：\n"
            "- A1: 纯计算论文，研究催化机理（SAC/DAC单原子/双原子催化剂、反应路径、过渡态计算）\n"
            "- A2: 纯计算论文，研究电子结构（态密度DOS、d带中心、Bader电荷分析）\n"
            "- A3: 纯计算论文，进行高通量筛选或机器学习材料设计\n"
            "- A4: 纯计算论文，进行分子动力学模拟（AIMD、离子输运、界面动力学）\n"
            "- B1: 包含计算和实验的电催化论文（ORR/OER/HER等）\n"
            "- B2: 包含计算和实验的储能材料论文（锂硫电池、钠离子电池等）\n"
            "- B3: 包含计算和实验的热催化论文（CO2RR、N2还原等）\n"
            "- C1: 纯实验论文，聚焦新材料合成与表征\n"
            "- C2: 纯实验论文，聚焦器件性能研究\n"
            "- C3: 纯实验论文，聚焦原位或 operando 机理表征\n"
            "- R: 综述论文\n\n"
            "然后，按以下要求提取信息：\n"
            "1. 【小白总结】用通俗易懂的语言（大一新生能理解）总结论文核心发现，并用中文撰写。\n"
            "2. 【写作逻辑】分析作者如何提出研究gap、如何提出假设、如何构建论证链条。这对训练AI写作至关重要。\n"
            "3. 【实验细节】如果论文包含实验工作，提取合成步骤、表征方法、性能测试。\n"
            "4. 【计算细节】如果论文包含DFT计算，提取软件、泛函、k点设置，以及关键计算数值（吸附能、反应能垒、Bader电荷等）。\n\n"
            "注意：\n"
            "- 所有文本内容请用中文输出\n"
            "- 严格按照JSON schema格式输出\n"
            "- 对于不适用的字段使用null\n"
            "- 为分类结果给出置信度分数（0.0-1.0）"
        )

        text_to_process = self._build_focus_text(doc, markdown, abstract)

        logger.info("Executing LLM Comprehensive Extraction...")
        try:
            llm_output = self.llm.structured_extract(system_prompt, text_to_process, ComprehensivePaperAnalysisModel)
            if llm_output:
                return llm_output.to_dict()
        except Exception as e:
            logger.error(f"Comprehensive LLM extraction failed: {e}")
            return None
        
        return None

    def extract_quick_classification(self, unified_document: Any) -> dict | None:
        doc = self._coerce_input(unified_document)
        markdown = getattr(doc, "markdown", "") or ""
        abstract = getattr(doc, "abstract", "") or ""

        if not self.llm or not self.llm.is_configured():
            return None

        if not markdown and not abstract:
            return None

        type_options = (
            "A1 (纯计算-催化机理), A2 (纯计算-电子结构), A3 (纯计算-高通量筛选), A4 (纯计算-分子动力学), "
            "B1 (计算+实验-电催化), B2 (计算+实验-储能材料), B3 (计算+实验-热催化), "
            "C1 (纯实验-新材料合成), C2 (纯实验-器件性能), C3 (纯实验-原位表征), "
            "R (综述), Unknown"
        )
        
        system_prompt = (
            "你是一位专业的催化化学与计算化学学术审稿人。\n"
            "基于摘要和引言内容判断论文类型，从以下选项中选择一个：\n"
            f"{type_options}\n\n"
            "分类标准：\n"
            "- A1: 纯计算论文，研究催化机理（SAC/DAC单原子/双原子催化剂、反应路径、过渡态计算）\n"
            "- A2: 纯计算论文，研究电子结构（态密度DOS、d带中心、Bader电荷分析）\n"
            "- A3: 纯计算论文，进行高通量筛选或机器学习材料设计\n"
            "- A4: 纯计算论文，进行分子动力学模拟（AIMD、离子输运、界面动力学）\n"
            "- B1: 包含计算和实验的电催化论文（ORR/OER/HER等）\n"
            "- B2: 包含计算和实验的储能材料论文（锂硫电池、钠离子电池等）\n"
            "- B3: 包含计算和实验的热催化论文（CO2RR、N2还原等）\n"
            "- C1: 纯实验论文，聚焦新材料合成与表征\n"
            "- C2: 纯实验论文，聚焦器件性能研究\n"
            "- C3: 纯实验论文，聚焦原位机理表征\n"
            "- R: 综述论文\n\n"
            "注意：\n"
            "- 严格按照JSON schema格式输出\n"
            "- 为分类结果给出置信度分数（0.0-1.0）"
        )

        text_to_process = self._build_focus_text(doc, markdown, abstract, max_chars=8000)

        logger.info("Executing Quick Classification...")
        try:
            from app.schemas.comprehensive_analysis import QuickClassificationModel
            llm_output = self.llm.structured_extract(system_prompt, text_to_process, QuickClassificationModel)
            if llm_output:
                res = llm_output.to_dict()
                res["classification_source"] = "quick"
                return res
        except Exception as e:
            logger.error(f"Quick classification failed: {e}")
            return None
        
        return None

    def _build_focus_text(self, doc: Any, markdown: str, abstract: str, max_chars: int = 50000) -> str:
        sections = getattr(doc, "sections", []) or []
        tables = getattr(doc, "tables", []) or []
        figures = getattr(doc, "figures", []) or []
        section_regex = re.compile(
            r"(abstract|intro|background|method|experiment|comput|result|discuss|conclusion|mechan|electro|dft|dos|band|barrier|adsor)",
            re.IGNORECASE,
        )
        parts: list[str] = []
        if abstract:
            parts.append("## Abstract\n" + abstract[:5000])
        for sec in sections:
            title = getattr(sec, "section_title", "") or ""
            text = getattr(sec, "text", "") or ""
            if not text:
                continue
            if section_regex.search(title) or section_regex.search(text[:1000]):
                parts.append(f"## Section: {title or 'Untitled'}\n{text[:5000]}")
        for tbl in tables[:12]:
            caption = getattr(tbl, "caption", "") or "Table"
            content = getattr(tbl, "markdown_content", "") or ""
            if content or caption:
                parts.append(f"## Table: {caption}\n{content[:2500]}")
        for fig in figures[:12]:
            caption = getattr(fig, "caption", "") or ""
            if caption:
                parts.append(f"## Figure Caption\n{caption[:1200]}")
        if not parts and markdown:
            parts.append(markdown[:max_chars])
        return "\n\n".join(parts)[:max_chars]
