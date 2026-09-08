from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.ai_verification_service import AIVerificationService


pytestmark = pytest.mark.no_test_database


def _checks(*, value, unit, evidence, value_upper=None, value_kind=None):
    target = SimpleNamespace(
        value=value,
        value_upper=value_upper,
        value_kind=value_kind,
        unit=unit,
        catalyst_sample_id=None,
        evidence_payload={},
    )
    return AIVerificationService(None)._content_checks("dft_results", target, "value", value, unit, evidence)


@pytest.mark.parametrize("evidence", [
    "config 1: -1.75 eV",
    "value 1 seven",
    "value: 1\nunit: eV",
])
def test_value_unit_parser_rejects_numbering_words_and_cross_line_pairing(evidence):
    assert _checks(value=1, unit="eV", evidence=evidence)["value_unit_same_evidence_item"] is False


def test_value_unit_parser_accepts_signed_same_item_value():
    checks = _checks(value=-1.75, unit="eV", evidence="Binding energy = -1.75 eV")
    assert checks["numeric_value_matches"] is True
    assert checks["value_unit_same_evidence_item"] is True


def test_range_parser_requires_one_range_item_with_both_bounds_and_unit():
    assert _checks(
        value=-1.75, value_upper=-1.20, value_kind="range", unit="eV",
        evidence="row 1: -1.75 eV; row 2: -1.20 eV",
    )["value_unit_same_evidence_item"] is False
    assert _checks(
        value=-1.75, value_upper=-1.20, value_kind="range", unit="eV",
        evidence="Binding-energy range: -1.75 to -1.20 eV",
    )["value_unit_same_evidence_item"] is True
