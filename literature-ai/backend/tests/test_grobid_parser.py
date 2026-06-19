from app.parsers.grobid_parser import GrobidParser


SAMPLE_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt>
        <title>Sample Paper</title>
        <author><persName>Alice Smith</persName></author>
      </titleStmt>
      <sourceDesc>
        <biblStruct>
          <monogr>
            <title level="j">Journal of Tests</title>
          </monogr>
          <idno type="DOI">10.1000/test</idno>
        </biblStruct>
      </sourceDesc>
    </fileDesc>
    <profileDesc>
      <abstract><p>Abstract text.</p></abstract>
    </profileDesc>
  </teiHeader>
  <text>
    <body>
      <div><head>Introduction</head><p>Intro text.</p></div>
      <div><head>Results and Discussion</head><p>Results text.</p></div>
    </body>
  </text>
</TEI>
"""


def test_parse_tei_extracts_metadata_and_sections():
    parser = GrobidParser("http://grobid:8070")
    result = parser._parse_tei(SAMPLE_TEI)

    assert result.metadata["title"] == "Sample Paper"
    assert result.metadata["doi"] == "10.1000/test"
    assert result.abstract == "Abstract text."
    assert len(result.sections) == 2
    assert result.sections[0]["section_type"] == "introduction"


def test_header_doi_wins_over_reference_dois():
    tei = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>Header DOI Paper</title></titleStmt>
      <sourceDesc>
        <biblStruct>
          <monogr>
            <title level="j">Header Journal</title>
            <imprint><date type="published" when="2024"/></imprint>
          </monogr>
          <idno type="DOI">https://doi.org/10.1000/HEADER.001</idno>
        </biblStruct>
      </sourceDesc>
    </fileDesc>
  </teiHeader>
  <text><body><div><p>Body.</p></div></body><back><listBibl>
    <biblStruct><analytic><title>Ref A</title></analytic><idno type="DOI">10.2000/ref-a</idno></biblStruct>
    <biblStruct><analytic><title>Ref B</title></analytic><idno type="DOI">10.3000/ref-b</idno></biblStruct>
  </listBibl></back></text>
</TEI>"""
    result = GrobidParser("http://grobid:8070")._parse_tei(tei)

    assert result.metadata["doi"] == "10.1000/header.001"
    assert result.metadata["journal"] == "Header Journal"
    assert result.metadata["year"] == 2024
    assert [ref["doi"] for ref in result.references] == ["10.2000/ref-a", "10.3000/ref-b"]


def test_reference_doi_is_not_promoted_when_header_doi_missing():
    tei = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>No Header DOI Paper</title></titleStmt>
      <sourceDesc><biblStruct><monogr><title level="j">Main Journal</title></monogr></biblStruct></sourceDesc>
    </fileDesc>
  </teiHeader>
  <text><body><div><p>Body.</p></div></body><back><listBibl>
    <biblStruct><analytic><title>Ref A</title></analytic><idno type="DOI">10.2000/ref-a</idno></biblStruct>
  </listBibl></back></text>
</TEI>"""
    result = GrobidParser("http://grobid:8070")._parse_tei(tei)

    assert result.metadata["doi"] is None
    assert result.metadata["journal"] == "Main Journal"
    assert result.references[0]["doi"] == "10.2000/ref-a"


def test_nested_sections_preserve_heading_path_and_level():
    tei = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>Nested</title></titleStmt>
<sourceDesc><biblStruct/></sourceDesc></fileDesc></teiHeader><text><body>
  <div><head>1 Results</head><p>Parent results text.</p>
    <div><head>1.1 Adsorption</head><p>Adsorption text.</p>
      <div><head>1.1.3 Kinetics</head><p>Kinetics text.</p></div>
    </div>
  </div>
</body></text></TEI>"""

    result = GrobidParser("http://grobid:8070")._parse_tei(tei)

    assert [section["level"] for section in result.sections] == [1, 2, 3]
    assert result.sections[2]["section_number"] == "1.1.3"
    assert result.sections[2]["parent_title"] == "1.1 Adsorption"
    assert result.sections[2]["heading_path"] == ["1 Results", "1.1 Adsorption", "1.1.3 Kinetics"]
    assert result.sections[2]["section_title"] == "1 Results > 1.1 Adsorption > 1.1.3 Kinetics"
    assert "Adsorption text" not in result.sections[0]["text"]


def test_nested_sections_prefer_head_n_and_keep_empty_parent_in_path():
    tei = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>Head N</title></titleStmt>
<sourceDesc><biblStruct/></sourceDesc></fileDesc></teiHeader><text><body>
  <div><head n="1">Results</head>
    <div><head n="1.1">Adsorption</head><p>Only adsorption body.</p>
      <div><head n="1.1.3">Kinetics</head><p>Only kinetics body.</p></div>
    </div>
  </div>
</body></text></TEI>"""

    result = GrobidParser("http://grobid:8070")._parse_tei(tei)

    assert len(result.sections) == 2
    assert [section["level"] for section in result.sections] == [2, 3]
    assert [section["section_number"] for section in result.sections] == ["1.1", "1.1.3"]
    assert result.sections[0]["parent_title"] == "Results"
    assert result.sections[1]["heading_path"] == ["Results", "Adsorption", "Kinetics"]
    assert result.sections[1]["section_title"] == "Results > Adsorption > Kinetics"
    assert "kinetics" not in result.sections[0]["text"].lower()
    assert "adsorption" not in result.sections[1]["text"].lower()


def test_head_n_preserves_non_numeric_raw_value_without_inventing_number():
    tei = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>Appendix</title></titleStmt>
<sourceDesc><biblStruct/></sourceDesc></fileDesc></teiHeader><text><body>
<div><head n="A">Supplementary methods</head><p>Method text.</p></div>
</body></text></TEI>"""

    result = GrobidParser("http://grobid:8070")._parse_tei(tei)

    assert result.sections[0]["section_number"] == "A"


def test_untitled_section_keeps_body_without_invented_section_number_or_title():
    tei = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>Untitled Intro</title></titleStmt>
<sourceDesc><biblStruct/></sourceDesc></fileDesc></teiHeader><text><body>
<div><p>Intro text without an explicit section heading.</p></div>
</body></text></TEI>"""

    result = GrobidParser("http://grobid:8070")._parse_tei(tei)

    assert result.sections[0]["section_title"] is None
    assert result.sections[0]["section_number"] is None
    assert result.sections[0]["heading_path"] == []
    assert "Intro text" in result.sections[0]["text"]
