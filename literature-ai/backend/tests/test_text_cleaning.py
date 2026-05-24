from pathlib import Path

from app.config import Settings
from app.parsers.docling_parser import DoclingParser
from app.services.discovery_service import DiscoveryService
from app.utils.text_cleaning import normalize_text_tree, repair_mojibake_text


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
    assert "Warning" in result.markdown
