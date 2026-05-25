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
