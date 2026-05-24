"""ai_extractor.py — 全文分批 DFT 专项信息抽取器

针对单原子/双原子催化剂第一性原理计算论文设计：
- 读取论文全部 chunk，不再 limit(12)
- 带章节标题拼接成结构化全文
- 分批（每批 ~3000 词）调用 AI，最后合并所有批次结果
- 专业 DFT schema：计算参数、催化剂结构、能量数值、写作骨架
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from openai import OpenAI
from sqlmodel import Session, select

from ..core.models import Chunk, ExtractedRecord, ExtractionJob

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 系统 Prompt（保持不变，每批次都使用）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
你是一位专业的催化化学与计算化学学术审稿人。请对提供的学术论文片段进行高精度数据抽取。

【抽取要求】
1. 只输出合法 JSON，不加任何解释。
2. 没有明确证据的字段填 null；列表无内容填 []。
3. 数值必须带单位，evidence_quote 必须是原文。
4. layman_summary 要求用大一新生能懂的话写（中文）。
5. 所有的分析文本（如撰写逻辑、小白总结、实验细节等）必须用中文输出。

【返回 JSON 结构】
{
  "paper_type": "A1 | A2 | A3 | A4 | B1 | B2 | B3 | C1 | C2 | C3 | R | Unknown",
  "type_confidence": 0.0,
  "layman_summary": {
    "one_sentence_takeaway": null,
    "real_world_impact": null
  },
  "writing_logic": {
    "research_gap_framing": null,
    "core_hypothesis": null,
    "evidence_chain": [
      {
        "step_description": "First, they demonstrated X using Y..."
      }
    ],
    "conclusion_mapping": null
  },
  "experimental_details": {
    "synthesis_steps": null,
    "characterization_methods": [],
    "performance_tests": []
  },
  "computational_details": {
    "software_and_functional": null,
    "cutoff_energy_and_kpoints": null,
    "solvation_model": null
  },
  "experimental_results": {
    "key_performance_metrics": null,
    "characterization_findings": null
  },
  "computational_results": [
    {
      "category": "adsorption_energy | reaction_barrier | bader_charge | etc.",
      "species": null,
      "reaction_step": null,
      "value": null,
      "unit": null,
      "evidence_quote": null,
      "source": null
    }
  ]
}
"""

_MERGE_PROMPT = """\
你是数据合并助手。给定若干个 JSON 对象（均来自同一篇论文的不同段落），请合并成一个完整的 JSON 对象。

合并规则：
1. paper_type：如果有多批次，取置信度 (type_confidence) 最高的那个分类。
2. 简单文本字段 (如 layman_summary, experimental_results 等)：优先取非 null 且信息量最大（字数最多）的。
3. 列表字段 (如 characterization_methods, evidence_chain 等)：取所有批次的并集并去重。
4. computational_results：直接合并列表并去重（category+species+value 相同才去重）。

只输出一个合法 JSON 对象，不加任何解释。

要合并的 JSON 列表：
"""

# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

_WORDS_PER_BATCH = 2800  # 每批约 2800 词，约 3500~4000 token，留足输出空间


class AIExtractor:
    def __init__(self, api_key: str, base_url: str | None = None, model: str = "gpt-4o-mini"):
        if not api_key:
            raise ValueError("缺少兼容 API Key")
        self.client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def extract_paper_info(self, session: Session, paper_id: str, job_id: str, schema_name: str) -> dict:
        """读取全文，分批抽取，合并后保存。"""
        chunks = session.exec(
            select(Chunk).where(Chunk.paper_id == paper_id).order_by(Chunk.chunk_index)
        ).all()

        if not chunks:
            raise ValueError("当前论文还没有可供抽取的文本块，请先解析 PDF。")

        # ---- 1. 拼接全文（保留章节标题） ----
        full_text = self._build_full_text(chunks)
        logger.info(f"Full text length: {len(full_text)} chars, ~{len(full_text.split())} words")

        # ---- 2. 分批调用 AI ----
        batches = self._split_into_batches(full_text, words_per_batch=_WORDS_PER_BATCH)
        logger.info(f"Split into {len(batches)} batches for extraction")

        partial_results: list[dict] = []
        for i, batch_text in enumerate(batches):
            logger.info(f"Extracting batch {i+1}/{len(batches)} ...")
            result = self._extract_batch(batch_text, batch_index=i, total_batches=len(batches))
            if result:
                partial_results.append(result)

        if not partial_results:
            raise ValueError("所有批次的 AI 抽取均失败，请检查 API Key 和网络连接。")

        # ---- 3. 合并多批次结果 ----
        if len(partial_results) == 1:
            merged = partial_results[0]
        else:
            merged = self._merge_results(partial_results)

        # ---- 4. 保存到数据库 ----
        record = ExtractedRecord(
            paper_id=paper_id,
            job_id=job_id,
            schema_name=schema_name,
            data_json=json.dumps(merged, ensure_ascii=False, indent=2),
            confidence_score=0.9,
            needs_review=1,
            review_status="pending",
            updated_at=datetime.now().isoformat(),
        )
        session.add(record)

        job = session.get(ExtractionJob, job_id)
        if job:
            job.status = "success"
            job.completed_at = datetime.now().isoformat()
            job.error_message = None
            session.add(job)

        session.commit()
        return merged

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _build_full_text(chunks: list[Chunk]) -> str:
        """把所有 chunk 拼成带章节标题的全文。"""
        parts: list[str] = []
        last_section = ""
        for chunk in chunks:
            section = chunk.section_title or ""
            if section and section != last_section:
                parts.append(f"\n\n=== {section} ===\n")
                last_section = section
            text = (chunk.text or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _split_into_batches(text: str, words_per_batch: int) -> list[str]:
        """按段落边界把全文切成批次。"""
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        batches: list[str] = []
        current_parts: list[str] = []
        current_words = 0

        for para in paragraphs:
            word_count = len(para.split())
            if current_words + word_count > words_per_batch and current_parts:
                batches.append("\n\n".join(current_parts))
                current_parts = []
                current_words = 0
            current_parts.append(para)
            current_words += word_count

        if current_parts:
            batches.append("\n\n".join(current_parts))

        return batches if batches else [text]

    def _extract_batch(self, text: str, batch_index: int, total_batches: int) -> dict | None:
        """调用 LLM 对单个批次做抽取，返回解析后的 dict，失败返回 None。"""
        user_content = (
            f"[论文片段 {batch_index + 1}/{total_batches}]\n\n"
            f"{text}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.05,
                timeout=120.0,
            )
            content = response.choices[0].message.content
            if not content:
                logger.warning(f"Batch {batch_index+1}: empty response")
                return None
            return json.loads(content)
        except Exception as e:
            logger.error(f"Batch {batch_index+1} extraction failed: {e}")
            return None

    def _merge_results(self, results: list[dict]) -> dict:
        """把多批次抽取结果合并为一个完整 JSON。先尝试 AI 合并，失败则做本地合并。"""
        try:
            merge_input = json.dumps(results, ensure_ascii=False, indent=2)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": _MERGE_PROMPT + merge_input},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                timeout=120.0,
            )
            content = response.choices[0].message.content
            if content:
                return json.loads(content)
        except Exception as e:
            logger.warning(f"AI merge failed, falling back to local merge: {e}")

        return self._local_merge(results)

        # Fallback local merge logic simplified for the new schema
        # In reality, the AI merge handles the new structure best.
        # Here we just blindly merge what we can if AI fails.
        merged: dict = {"dft_results": []}
        
        def merge_str(key, subkey=None):
            best = None
            for r in results:
                val = r.get(key)
                if subkey and isinstance(val, dict):
                    val = val.get(subkey)
                if isinstance(val, str) and len(val) > len(best or ""):
                    best = val
            return best
            
        def merge_list(key, subkey=None):
            seen = []
            for r in results:
                val = r.get(key)
                if subkey and isinstance(val, dict):
                    val = val.get(subkey)
                for item in (val or []):
                    if item not in seen:
                        seen.append(item)
            return seen

        merged["paper_type"] = merge_str("paper_type") or "Unknown"
        merged["type_confidence"] = 0.5
        
        merged["layman_summary"] = {
            "one_sentence_takeaway": merge_str("layman_summary", "one_sentence_takeaway"),
            "real_world_impact": merge_str("layman_summary", "real_world_impact")
        }
        
        merged["writing_logic"] = {
            "research_gap_framing": merge_str("writing_logic", "research_gap_framing"),
            "core_hypothesis": merge_str("writing_logic", "core_hypothesis"),
            "evidence_chain": merge_list("writing_logic", "evidence_chain"),
            "conclusion_mapping": merge_str("writing_logic", "conclusion_mapping")
        }
        
        merged["experimental_details"] = {
            "synthesis_steps": merge_str("experimental_details", "synthesis_steps"),
            "characterization_methods": merge_list("experimental_details", "characterization_methods"),
            "performance_tests": merge_list("experimental_details", "performance_tests")
        }
        
        merged["computational_details"] = {
            "software_and_functional": merge_str("computational_details", "software_and_functional"),
            "cutoff_energy_and_kpoints": merge_str("computational_details", "cutoff_energy_and_kpoints"),
            "solvation_model": merge_str("computational_details", "solvation_model")
        }
        
        merged["experimental_results"] = {
            "key_performance_metrics": merge_str("experimental_results", "key_performance_metrics"),
            "characterization_findings": merge_str("experimental_results", "characterization_findings")
        }
        
        all_comp = []
        seen_keys = set()
        for r in results:
            for item in r.get("computational_results") or []:
                dedup_key = f"{item.get('category')}|{item.get('species')}|{item.get('value')}"
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_comp.append(item)
        merged["computational_results"] = all_comp
        
        return merged
