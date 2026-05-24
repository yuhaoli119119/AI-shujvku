from app.normalizers.dft_normalizer import DFTNormalizer


def test_full_reproducibility_score_is_low_risk():
    normalizer = DFTNormalizer()
    text = """
    All calculations were performed using VASP with PAW pseudopotentials and the PBE functional.
    The plane-wave cutoff energy was set to 500 eV. A 4 x 4 x 1 k-point mesh was used.
    EDIFF = 1e-5 eV. A vacuum layer of 15 A was added. DFT-D3 correction was applied.
    The adsorption free energy was calculated using Delta G = Delta E + ZPE - TS.
    Atomic coordinates are provided in the Supplementary Information.
    """

    score = normalizer.calculate_reproducibility_score(text)

    assert score.score >= 9
    assert score.risk_level == "low"


def test_partial_reproducibility_is_detected():
    normalizer = DFTNormalizer()
    text = "VASP with the PBE functional was used. The cutoff energy was 400 eV and a 3 x 3 x 1 k-point grid was applied."

    score = normalizer.calculate_reproducibility_score(text)

    assert 4 <= score.score <= 6
    assert score.risk_level in {"medium", "high"}


def test_normalize_exposes_cleaned_subfields():
    normalizer = DFTNormalizer()
    result = normalizer.normalize(
        {
            "text": "The calculations used VASP, PBE, cutoff energy 450 eV, a 5 x 5 x 1 k-point mesh, and vacuum of 20 A.",
        }
    )

    assert result["dft_reproducibility_score"] >= 4
    assert result["_normalized"]["software"] == "VASP"
    assert result["_normalized"]["cutoff"]["value"] == 450.0
    assert result["_normalized"]["kpoints"] == {"kx": 5, "ky": 5, "kz": 1}
    assert result["_normalized"]["vacuum"] == {"value": 20.0, "unit": "A"}
