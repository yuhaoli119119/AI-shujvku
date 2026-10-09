import pytest
from app.utils.paper_type import normalize_paper_type_filter

def test_normalize_paper_type_filter():
    assert normalize_paper_type_filter("A-1") == ["A"]
    assert normalize_paper_type_filter("C") == ["C"]
    assert normalize_paper_type_filter(None) is None
    assert normalize_paper_type_filter("   r-foo ") == ["R"]
    assert normalize_paper_type_filter("") is None
    assert normalize_paper_type_filter("   ") is None
    assert normalize_paper_type_filter("\t\n") is None
