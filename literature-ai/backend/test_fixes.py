import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))

from app.parsers.grobid_parser import GrobidParser
from app.services.extraction_pipeline import ExtractionPipelineService
from app.normalizers.chemistry_normalizer import ChemistryNormalizer, _extract_metal_from_name
from app.extractors.catalyst_extractor import CatalystExtractor

def run_tests():
    print("Testing _safe_year...")
    assert GrobidParser._safe_year("Oct 2022") == 2022, "Year extraction failed"
    assert GrobidParser._safe_year("1999/12/31") == 1999, "Year extraction failed"
    assert GrobidParser._safe_year(None) is None, "None handling failed"
    print("[OK] _safe_year works")

    print("Testing _safe_float...")
    assert ExtractionPipelineService._safe_float("~450") == 450.0, "Float approx failed"
    assert ExtractionPipelineService._safe_float("400-500") == 400.0, "Float range failed"
    assert ExtractionPipelineService._safe_float("  -12.5 eV") == -12.5, "Float negative failed"
    assert ExtractionPipelineService._safe_float(None) is None, "None handling failed"
    print("[OK] _safe_float works")

    print("Testing ChemistryNormalizer metals...")
    assert _extract_metal_from_name("Au-SAC") == "Au", "Au missing"
    assert _extract_metal_from_name("Ag-N4") == "Ag", "Ag missing"
    assert _extract_metal_from_name("Ti-based") == "Ti", "Ti missing"
    print("[OK] ChemistryNormalizer works")

    print("Testing CatalystExtractor metals...")
    extractor = CatalystExtractor()
    assert extractor.metal_case_sensitive.search("Au").group(0) == "Au", "Au regex failed"
    assert extractor.metal_context_pattern.search("Ag-based").group(0) == "Ag-based", "Ag context failed"
    print("[OK] CatalystExtractor regexes work")

if __name__ == "__main__":
    run_tests()
    print("\nAll unit tests for the pipeline fixes passed successfully!")
