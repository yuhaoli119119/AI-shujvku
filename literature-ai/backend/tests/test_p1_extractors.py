import pytest
from app.extractors.dft_settings_extractor import DFTSettingsExtractor

def test_dft_settings_extractor_regex_boundaries():
    extractor = DFTSettingsExtractor()
    
    # Test dispersion corrections
    text = "We used the DFT-D3(BJ) method and also tested 6-31G* as well as 6-311+G(d,p) basis sets."
    results = extractor.extract([{"text": text, "section_title": "methods"}])
    
    dispersion = [res["value"] for res in results.get("dispersion correction", [])]
    basis = [res["value"] for res in results.get("pseudopotential / basis set", [])]
    
    assert "DFT-D3" in dispersion
    assert "6-31G*" in basis
    assert "6-311+G(d,p)" in basis
