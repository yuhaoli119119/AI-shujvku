import pytest
from pathlib import Path
from app.parsers.docling_parser import DoclingParser
from app.config import Settings
import httpx
from lxml import etree
from app.parsers.grobid_parser import GrobidParser

def test_docling_parser_fallback_missing_file():
    settings = Settings(docling_enabled=False)
    parser = DoclingParser(settings)
    
    # Passing a non-existent file should raise an Exception, not return a scanned PDF warning
    with pytest.raises(Exception):
        parser.parse_pdf_sync(Path("does_not_exist.pdf"))

def test_grobid_parser_network_error():
    parser = GrobidParser(base_url="http://invalid_url_that_does_not_exist")
    with pytest.raises(Exception):
        parser.parse_pdf_sync(Path(__file__))
