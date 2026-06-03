from __future__ import annotations

from app.utils.library_names import DEFAULT_LIBRARY_NAME, library_name_variants, normalize_library_name


def test_default_library_name_is_real_utf8_text() -> None:
    assert DEFAULT_LIBRARY_NAME == "默认文献库"
    assert normalize_library_name(None) == "默认文献库"
    assert normalize_library_name("榛樿鏂囩尞搴?") == "默认文献库"


def test_non_ascii_library_name_matches_legacy_question_mark_damage() -> None:
    variants = library_name_variants("石墨炔第一性原理计算")

    assert "石墨炔第一性原理计算" in variants
    assert "??????????" in variants
