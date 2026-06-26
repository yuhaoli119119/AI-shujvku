from pathlib import Path

from app.config import Settings
from app.parsers.docling_parser import DoclingParser
from app.services.discovery_service import DiscoveryService
from app.services.paper_query import PaperQueryService
from app.utils.text_cleaning import normalize_text_tree, repair_mojibake_text, repair_repeated_journal_title


def test_repair_mojibake_text_fixes_common_utf8_latin1_damage():
    assert repair_mojibake_text("Guangâ\x80\x90Jie Xia") == "Guang‐Jie Xia"
    assert repair_mojibake_text("CafÃ©") == "Café"


def test_normalize_text_tree_repairs_nested_values():
    payload = {
        "title": "CafÃ©",
        "authors": ["Guangâ\x80\x90Jie Xia"],
        "nested": {"abstract": "FranÃ§ois"},
    }

    normalized = normalize_text_tree(payload)

    assert normalized["title"] == "Café"
    assert normalized["authors"] == ["Guang‐Jie Xia"]
    assert normalized["nested"]["abstract"] == "François"


def test_repair_repeated_journal_title_collapses_exact_adjacent_duplicate():
    assert repair_repeated_journal_title("ACS Omega ACS Omega") == "ACS Omega"
    assert repair_repeated_journal_title("  Journal   of Energy Storage   Journal of Energy Storage  ") == "Journal of Energy Storage"


def test_normalize_text_tree_only_collapses_repeated_journal_field():
    payload = {
        "title": "Small Small catalyst",
        "journal": "Small Small",
        "nested": {"journal": "Energy Storage Materials Energy Storage Materials"},
    }

    normalized = normalize_text_tree(payload)

    assert normalized["title"] == "Small Small catalyst"
    assert normalized["journal"] == "Small"
    assert normalized["nested"]["journal"] == "Energy Storage Materials"


def test_repair_mojibake_text_repairs_shifted_digit_block():
    shifted = "doi s" + "".join(chr(0x0376 + i) for i in [4, 1]) + "598-024-67393-z"
    assert repair_mojibake_text(shifted) == "doi s41598-024-67393-z"


def test_repair_mojibake_text_repairs_latin1_decoded_greek_symbols():
    assert repair_mojibake_text("\u00ce\u00b1 absorption") == "\u03b1 absorption"
    assert repair_mojibake_text("\u00ce\u00b5 dielectric") == "\u03b5 dielectric"


def test_paper_query_clean_pdf_text_repairs_existing_stored_mojibake():
    shifted = "s" + "".join(chr(0x0376 + i) for i in [4, 1]) + "598-024-67393-z"
    assert PaperQueryService._clean_pdf_text(shifted) == "s41598-024-67393-z"


def test_discovery_service_serialize_repairs_text_fields():
    class Author:
        def __init__(self, name: str) -> None:
            self.name = name

    class Source:
        def __init__(self, title: str) -> None:
            self.title = title

    class Paper:
        title = "CafÃ© catalyst"
        doi = "10.1000/test"
        publication_date = None
        source = Source("Revista FranÃ§aise")
        authors = [Author("Guangâ\x80\x90Jie Xia")]
        abstract = "FranÃ§ois studied CafÃ© adsorption."
        url = None
        pdf_url = None
        is_open_access = True
        databases = ["openalex"]

    payload = DiscoveryService._serialize_paper(Paper())

    assert payload["title"] == "Café catalyst"
    assert payload["journal"] == "Revista Française"
    assert payload["authors"] == ["Guang‐Jie Xia"]
    assert payload["abstract"] == "François studied Café adsorption."


def test_docling_parser_respects_disabled_flag_and_falls_back(tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    settings = Settings(docling_enabled=False)
    parser = DoclingParser(settings)

    result = parser.parse_pdf_sync(pdf_path)

    assert result.json_payload["fallback"] is True
    assert result.json_payload["parse_blocked"] is True
    assert result.json_payload["parse_warning"].startswith("[Warning]")
    assert result.markdown == ""
    assert result.page_blocks == []


def test_docling_source_path_uses_ascii_temp_copy_for_unicode_absolute_path(tmp_path: Path):
    unicode_dir = tmp_path / "测试目录"
    unicode_dir.mkdir()
    pdf_path = unicode_dir / "样本文档.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    with DoclingParser._docling_source_path(pdf_path.resolve()) as source_path:
        assert source_path.exists()
        assert source_path.suffix == ".pdf"
        assert str(source_path).isascii()
        assert source_path.read_bytes() == pdf_path.read_bytes()
