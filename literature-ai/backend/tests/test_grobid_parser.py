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
