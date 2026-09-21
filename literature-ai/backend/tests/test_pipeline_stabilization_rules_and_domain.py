"""Regression tests for the rule extractor and the domain model.

Split out from ``test_pipeline_stabilization_chart_and_dft.py`` so that the
"baseline code + candidate tests" matrix cell can collect: this half only
imports modules that already exist on the baseline, while the chart/ML-readiness
half imports the new candidate modules.

C. the system rule extractor must never emit the tail digits of a longer decimal
   and must bind a number to the physical quantity it belongs to;
D. domain model: atomic sulfur ``S_atom`` is not molecular ``S8``, and materials
   such as graphdiyne are a material dimension, not a reaction type.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.reaction_taxonomy import (
    normalize_intermediate,
    normalize_reaction_type,
    validate_reaction_record,
)
from app.extractors import dft_results_extractor
from app.extractors.dft_results_extractor import (
    DFTResultsExtractor,
    _normalize_numeric_text,
    _resolve_adsorbate,
)

# Candidate-only helper.  Imported by name so the "baseline code + candidate
# tests" matrix cell can still collect this module and report a real per-test
# result instead of an import-time collection error.
_number_is_fragment = getattr(dft_results_extractor, "_number_is_fragment", None)


# --------------------------------------------------------------------------- #
# C. system rule numeric extraction
# --------------------------------------------------------------------------- #


def _scan(text: str) -> list[Any]:
    extractor = DFTResultsExtractor(None)
    normalized = _normalize_numeric_text(text)
    items: list[Any] = []
    for category in extractor.categories:
        items.extend(extractor._scan_text(normalized, category, {}, []))
    return extractor._deduplicate(items)


def test_decimal_tail_digits_are_never_extracted_as_separate_values():
    text = (
        "the desorption process of a single sulfur atom only needs to conquer a low "
        "energy barrier of 2.13 eV, ensuring the regeneration of the catalysts. "
        "As seen in Figure 6, the desorption process of a single sulfur atom from the "
        "surface of Ti-N4-based SACs has an extremely high energy barrier of 4.99 eV."
    )
    values = sorted({item.value for item in _scan(text) if item.value is not None})
    assert 3.0 not in values
    assert 9.0 not in values
    assert 2.13 in values
    assert 4.99 in values


@pytest.mark.parametrize(
    "text,forbidden",
    [
        ("The activation barrier is 2.13 eV in total.", {3.0}),
        ("The activation barrier is 4.99 eV in total.", {9.0}),
        ("The diffusion barrier is 0.85 eV.", {5.0, 85.0}),
        ("The value is 1.5e-3 eV as reported.", {3.0, 5.0, 1.0}),
        ("The barrier is 1e3 eV as reported.", {3.0}),
        ("The barrier is 1.25 x 10^3 eV nominal.", {3.0}),
        ("the barrier equals 12.5 eV for the cited system", {2.0, 5.0, 12.0}),
    ],
)
def test_numbers_are_complete_tokens_not_fragments(text, forbidden):
    values = {item.value for item in _scan(text) if item.value is not None}
    assert not (values & forbidden), f"{values} contains a fragment of a longer number"


def test_number_is_fragment_helper_contract():
    if _number_is_fragment is None:
        pytest.fail("_number_is_fragment is missing from the extractor on this tree")
    text = "barrier of 2.13 eV"
    # 2.13 -> whole token
    assert _number_is_fragment(text, text.index("2"), text.index("2") + 4) is False
    # the trailing 3 is a fragment
    assert _number_is_fragment(text, text.index("3"), text.index("3") + 1) is True
    # scientific notation exponent tail
    exp = "1.5e-3 eV"
    assert _number_is_fragment(exp, exp.index("-3"), exp.index("-3") + 2) is True


def test_sulfur_desorption_criterion_is_not_adsorption_energy():
    text = (
        "M-N4 SAC models with E b values of less than 4.53 eV for the binding of a "
        "single S atom can break away from the M-S bonding during the charging process."
    )
    items = _scan(text)
    criterion = [item for item in items if item.value == 4.53]
    assert criterion, "4.53 eV must still be extracted"
    assert {item.category for item in criterion} == {"sulfur_desorption_criterion"}
    assert all(item.category != "adsorption_energy" for item in criterion)
    assert all(item.adsorbate == "S_atom" for item in criterion)


def test_atomic_sulfur_desorption_barrier_has_its_own_property():
    text = (
        "while in Co-N4-based SACs the desorption process of a single sulfur atom only "
        "needs to conquer a low energy barrier of 2.13 eV."
    )
    items = _scan(text)
    assert {item.category for item in items if item.value == 2.13} == {"sulfur_desorption_barrier"}
    assert all(item.adsorbate == "S_atom" for item in items if item.value == 2.13)


def test_single_sulfur_atom_is_not_normalised_to_s8():
    assert _resolve_adsorbate("the desorption of a single sulfur atom from the surface") == "S_atom"
    assert _resolve_adsorbate("atomic sulfur binds strongly") == "S_atom"
    assert _resolve_adsorbate("molecular S8 ring is stable") == "S8"
    assert _resolve_adsorbate("cyclo-S8 molecule") == "S8"


def test_measured_adsorption_energy_stays_adsorption_energy():
    text = "The calculated adsorption energy of Li2S4 on FeCo-NC is -1.85 eV."
    items = [item for item in _scan(text) if item.value is not None]
    assert items
    assert {item.category for item in items} <= {"adsorption_energy", "binding_energy", "metal_support_binding_energy_Eb"}


# --------------------------------------------------------------------------- #
# D. domain model
# --------------------------------------------------------------------------- #


def test_srr_lis_distinguishes_atomic_sulfur_from_molecular_s8():
    assert normalize_intermediate("SRR_LiS", "sulfur") == "S_atom"
    assert normalize_intermediate("SRR_LiS", "single sulfur atom") == "S_atom"
    assert normalize_intermediate("SRR_LiS", "S_atom") == "S_atom"
    assert normalize_intermediate("SRR_LiS", "S8") == "S8"
    assert normalize_intermediate("SRR_LiS", "sulfur") != normalize_intermediate("SRR_LiS", "S8")


@pytest.mark.parametrize(
    "property_type,adsorbate",
    [
        ("sulfur_desorption_barrier", "S_atom"),
        ("sulfur_desorption_criterion", "S_atom"),
    ],
)
def test_new_sulfur_properties_are_in_scope_for_srr(property_type, adsorbate):
    verdict = validate_reaction_record(
        "SRR_LiS",
        {"adsorbate": adsorbate, "property_type": property_type, "evidence_text": "single sulfur atom"},
    )
    assert verdict["valid"] is True, verdict
    assert verdict["status"] == "valid"


def test_claim_categories_without_capture_groups_still_scan():
    """dos_claim / CDD-claim patterns declare a value group they do not have.

    The decimal-tail guard must not turn those into a crash, and it must not
    change the legacy outcome either: a match whose pattern has no numeric group
    carries no value by design, so the guard has to report "not a fragment" and
    let the original ``except IndexError -> value=None`` path run.
    """
    import re

    text = (
        "The DOS is enhanced near the Fermi level upon Li coordination, and the "
        "charge density difference confirms electron transfer from the metal. "
        "The projected DOS hybridizes strongly with the metal d states."
    )
    matcher = getattr(dft_results_extractor, "_match_number_is_fragment", None)
    if matcher is None:
        pytest.fail("_match_number_is_fragment is missing from the extractor on this tree")
    extractor = DFTResultsExtractor(None)
    zero_group_seen = 0
    for category in ("dos_claim", "charge_density_difference_claim"):
        patterns = dft_results_extractor.CATEGORY_RULES.get(category) or []
        assert patterns, f"{category} must stay registered"
        for pattern in patterns:
            compiled = re.compile(pattern, re.IGNORECASE)
            for match in compiled.finditer(text):
                verdict = matcher(text, match, 1)
                assert isinstance(verdict, bool)
                if compiled.groups == 0:
                    zero_group_seen += 1
                    assert verdict is False, "a pattern with no value group has no fragment to guard"
        # the whole scan must not raise, and must keep the legacy "no value" shape
        items = extractor._scan_text(_normalize_numeric_text(text), category, {}, [])
        assert all(item.value is None for item in items)
    assert zero_group_seen > 0, "the zero-group claim patterns are no longer exercised"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("The adsorption energy of Li2S4 is -1.85 eV.", -1.85),
        ("The adsorption energy of Li2S4 is \u22121.85 eV.", -1.85),
        ("A barrier of 1.25\u00d710^-3 eV was reported.", None),
        ("The E_ads for Li2S on the site is 0.45 eV, while for Li2S4 it is 1.10 eV.", None),
    ],
)
def test_signs_scientific_notation_and_neighbouring_values(text, expected):
    items = [item for item in _scan(text) if item.value is not None]
    if expected is not None:
        assert expected in {item.value for item in items}, [item.value for item in items]
    # neighbouring values must each keep their own magnitude, never a tail digit
    for item in items:
        assert item.value not in {85.0, 45.0, 10.0, 25.0}, [i.value for i in items]


def test_reference_title_and_formula_digits_are_not_bond_lengths():
    text = (
        "[61] R. Thapa, N. Barman, Electronic descriptor for e-NRR and effect of "
        "BF3 as electrolyte ion, ChemSusChem (2024), doi:10.1002/cssc.202400902."
    )
    items = [item for item in _scan(text) if item.value is not None]
    assert not [
        item for item in items
        if item.value == 3.0 and item.category in {"bond_length_M-N", "bond_length_M-S", "bond_length_M-M"}
    ]


def test_figure_table_and_reference_numbers_are_not_values():
    text = (
        "As seen in Figure 6, the reference 65 and Table S3 report the same trend "
        "(see Ref. 12). The desorption barrier of a single sulfur atom is 4.99 eV."
    )
    items = [item for item in _scan(text) if item.value is not None]
    assert 4.99 in {item.value for item in items}
    assert 6.0 not in {item.value for item in items}
    assert 12.0 not in {item.value for item in items}
    assert 3.0 not in {item.value for item in items}
