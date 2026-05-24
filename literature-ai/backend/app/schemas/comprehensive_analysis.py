from typing import Optional, List
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Layman Summary (小白通俗版)
# ---------------------------------------------------------------------------
class LaymanSummaryModel(BaseModel):
    one_sentence_takeaway: str = Field(..., description="A one-sentence, highly accessible summary of the paper's core finding, understandable by an undergraduate student.")
    real_world_impact: str = Field(..., description="The potential real-world application or industrial impact of this research.")

# ---------------------------------------------------------------------------
# Writing Logic & Storyline (高阶写作逻辑解析)
# ---------------------------------------------------------------------------
class EvidenceStepModel(BaseModel):
    step_description: str = Field(..., description="A logical step in the evidence chain (e.g., 'First, they demonstrated X using Y...')")

class WritingLogicModel(BaseModel):
    research_gap_framing: str = Field(..., description="How the authors framed the problem and introduced the research gap in the introduction.")
    core_hypothesis: str = Field(..., description="The core hypothesis or the proposed solution to bridge the gap.")
    evidence_chain: List[EvidenceStepModel] = Field(..., description="The step-by-step logical progression of how the paper proves its point.")
    conclusion_mapping: str = Field(..., description="How the conclusion circles back to perfectly address the initial research gap.")

# ---------------------------------------------------------------------------
# Methodology (实验与计算执行细节)
# ---------------------------------------------------------------------------
class ExperimentalDetailsModel(BaseModel):
    synthesis_steps: Optional[str] = Field(None, description="Detailed steps for material synthesis or preparation, if applicable.")
    characterization_methods: Optional[List[str]] = Field(None, description="List of characterization techniques used (e.g., XRD, XPS, TEM) and what they aimed to prove.")
    performance_tests: Optional[List[str]] = Field(None, description="Performance metrics evaluated (e.g., battery cycling life, specific capacity).")

class ComputationalDetailsModel(BaseModel):
    software_and_functional: Optional[str] = Field(None, description="DFT software and exchange-correlation functional used (e.g., VASP, PBE).")
    cutoff_energy_and_kpoints: Optional[str] = Field(None, description="Cutoff energy and K-point mesh details.")
    solvation_model: Optional[str] = Field(None, description="Implicit or explicit solvation models used, if any.")

# ---------------------------------------------------------------------------
# Key Findings (核心数据与结果)
# ---------------------------------------------------------------------------
class ExperimentalResultsModel(BaseModel):
    key_performance_metrics: Optional[str] = Field(None, description="The best performance numbers achieved in experiments.")
    characterization_findings: Optional[str] = Field(None, description="Key structural or chemical findings from characterization.")

# We will reuse the DFTResultItemModel from dft_results_extractor for computational_results 
# to keep compatibility with the ML database.
from app.extractors.dft_results_extractor import DFTResultItemModel

# ---------------------------------------------------------------------------
# Paper Type Classification (10 types)
# ---------------------------------------------------------------------------
# A1: Pure Computational - Catalytic Mechanism (SAC/DAC, reaction paths, transition states)
# A2: Pure Computational - Electronic Structure (DOS, d-band center, Bader charge)
# A3: Pure Computational - High-throughput Screening (descriptors, volcano plots, ML material design)
# A4: Pure Computational - Molecular Dynamics (AIMD, ion transport, interface dynamics)
# B1: Computational + Experimental - Electrocatalysis (ORR/OER/HER)
# B2: Computational + Experimental - Energy Storage (Li-S, Na-ion)
# B3: Computational + Experimental - Thermal Catalysis (CO2RR, N2 fixation)
# C1: Pure Experimental - New Material Synthesis & Characterization
# C2: Pure Experimental - Device Performance Study
# C3: Pure Experimental - In-situ Mechanism Characterization
# R: Review
# Unknown: Fallback when classification fails
PAPER_TYPE_OPTIONS = [
    "A1", "A2", "A3", "A4",
    "B1", "B2", "B3",
    "C1", "C2", "C3",
    "R", "Unknown",
]

class QuickClassificationModel(BaseModel):
    paper_type: str = Field(..., description=f"One of: {PAPER_TYPE_OPTIONS}")
    type_confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Classification confidence: 0.0-1.0")

    def to_dict(self) -> dict:
        return self.model_dump()

# ---------------------------------------------------------------------------
# Unified Root Schema (大一统模型)
# ---------------------------------------------------------------------------
class ComprehensivePaperAnalysisModel(BaseModel):
    paper_type: str = Field(..., description=f"One of: {PAPER_TYPE_OPTIONS}")
    type_confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Classification confidence: 0.0-1.0")
    
    layman_summary: LaymanSummaryModel
    writing_logic: WritingLogicModel
    
    # Optional sections based on paper type
    experimental_details: Optional[ExperimentalDetailsModel] = Field(None, description="Only fill this if the paper contains experimental work.")
    computational_details: Optional[ComputationalDetailsModel] = Field(None, description="Only fill this if the paper contains computational (e.g., DFT) work.")
    
    experimental_results: Optional[ExperimentalResultsModel] = Field(None, description="Only fill this if the paper has experimental results.")
    computational_results: Optional[List[DFTResultItemModel]] = Field(None, description="Strictly extracted computational values for the ML database. Only fill if the paper has computational results.")

    def to_dict(self) -> dict:
        return self.model_dump()
