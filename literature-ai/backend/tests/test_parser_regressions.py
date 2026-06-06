import pytest
import re
from app.parsers.docling_parser import DoclingParser
from app.parsers.grobid_parser import GrobidParser
from app.utils.figure_filtering import is_decorative_figure
from lxml import etree

def test_bare_figure_label_is_kept_for_vlm_review():
    assert not is_decorative_figure("Fig. 1", [{"page_no": 1}])
    assert not is_decorative_figure("Figure 2.", [{"page_no": 2}])

def test_short_caption_figure_not_dropped():
    class MockFig:
        def __init__(self, cap):
            self.caption = cap
            self.figure_role = "figure"
            self.image_path = ""
            self.page = 1
            
        def model_copy(self, **kwargs):
            return self

    figs = [MockFig("Fig. 1")]
    _short_caption_re = re.compile(r'^fig\.?\s*\d+\.?\s*$', re.IGNORECASE)
    filtered_figures = []
    for fig in figs:
        if not fig.caption or not fig.caption.strip():
            continue
        if _short_caption_re.match(fig.caption.strip()):
            fig.figure_role = "caption_incomplete"
        filtered_figures.append(fig)
        
    assert len(filtered_figures) == 1
    assert filtered_figures[0].figure_role == "caption_incomplete"

def test_table_caption_not_truncated_by_header_mention():
    caption = "Table 1. Experimental details including Reaction Temperature and Pressure."
    markdown = "| Reaction Temperature | Pressure | Time |\n|---|---|---|\n| 100 | 200 | 300 |"
    
    res = DoclingParser._strip_table_body_from_caption(caption, markdown)
    assert "Reaction Temperature and Pressure" in res

    caption2 = "Table 1. Details. | Reaction Temperature | Pressure | Time | 100 | 200 |"
    res2 = DoclingParser._strip_table_body_from_caption(caption2, markdown)
    assert "|" not in res2

def test_grobid_extracts_list_formula_figdesc():
    xml = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
        <text><body><div>
            <head>Section 1</head>
            <p>Para 1</p>
            <list><item>Item 1</item></list>
            <formula>E=mc2</formula>
            <figure><figDesc>Fig desc 1</figDesc></figure>
        </div></body></text>
    </TEI>"""
    parser = GrobidParser(base_url="http://dummy:8070")
    root = etree.fromstring(xml.encode("utf-8"))
    
    NS = {"tei": "http://www.tei-c.org/ns/1.0"}
    sections = []
    body_divs = root.xpath("//tei:text/tei:body/tei:div", namespaces=NS)
    for index, div in enumerate(body_divs, start=1):
        head = parser._join_text(div.xpath("./tei:head//text()", namespaces=NS)).strip()
        elements = div.xpath(".//tei:p | .//tei:item | .//tei:formula | .//tei:figDesc | .//tei:note", namespaces=NS)
        parts = []
        for el in elements:
            parts.append(parser._join_text(el.xpath(".//text()", namespaces=NS)))
        text = "\n\n".join(part.strip() for part in parts if part.strip())
        sections.append(text)
        
    assert "Para 1" in sections[0]
    assert "Item 1" in sections[0]
    assert "E=mc2" in sections[0]
    assert "Fig desc 1" in sections[0]

def test_dedupe_caption_odd_length():
    # Length needs to be >= 48 so half >= 24
    s1 = "Table 1. This is a very very long test data string "
    s2 = "Table 1. This is a very very long test data string"
    res = DoclingParser._dedupe_caption_text(s1 + s2 + " ")
    assert res == s1.strip()

    s3 = "Table 1. This is a very very long test data string"
    s4 = "Table 1. This is a very very long test data string"
    res2 = DoclingParser._dedupe_caption_text(s3 + s4)
    assert res2 == s3
