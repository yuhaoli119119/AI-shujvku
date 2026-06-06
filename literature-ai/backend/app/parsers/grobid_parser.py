from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

import httpx
from lxml import etree


NS = {"tei": "http://www.tei-c.org/ns/1.0"}
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


@dataclass
class GrobidParseResult:
    metadata: dict[str, Any]
    abstract: str
    sections: list[dict[str, Any]]
    references: list[dict[str, Any]]
    tei_xml: str


class GrobidParser:
    def __init__(self, base_url: str, timeout: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def parse_pdf(self, pdf_path: Path) -> GrobidParseResult:
        endpoint = f"{self.base_url}/api/processFulltextDocument"
        data = {
            "includeRawCitations": "1",
            "includeRawAffiliations": "1",
            "consolidateHeader": "1",
            "consolidateCitations": "1",
            "segmentSentences": "0",
            "teiCoordinates": "head,ref,biblStruct,figure,formula",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                with pdf_path.open("rb") as handle:
                    response = await client.post(
                        endpoint,
                        data=data,
                        files={"input": (pdf_path.name, handle, "application/pdf")},
                    )
                response.raise_for_status()
        except httpx.RequestError as e:
            raise RuntimeError(f"Grobid network request failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Grobid returned HTTP error: {e.response.status_code}") from e
        tei_xml = response.text
        return self._parse_tei(tei_xml)

    def _parse_tei(self, tei_xml: str) -> GrobidParseResult:
        try:
            root = etree.fromstring(tei_xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            raise RuntimeError(f"Failed to parse TEI XML: {e}") from e

        title = self._join_text(root.xpath("//tei:titleStmt/tei:title/text()", namespaces=NS))
        abstract = self._join_text(
            root.xpath("//tei:profileDesc/tei:abstract//text()", namespaces=NS)
        ).strip()
        header = root.xpath("//tei:teiHeader/tei:fileDesc/tei:sourceDesc", namespaces=NS)
        header_node = header[0] if header else root
        doi = self._first_valid_doi(
            header_node.xpath(".//tei:idno[@type='DOI' or @type='doi']/text()", namespaces=NS)
        )
        year = self._safe_year(
            self._join_text(
                header_node.xpath(
                    ".//tei:date[@type='published']/@when | .//tei:date[@type='published']//text() | .//tei:imprint/tei:date/@when | .//tei:imprint/tei:date//text()",
                    namespaces=NS,
                )
            )
        )
        journal = self._join_text(
            header_node.xpath(".//tei:monogr/tei:title[@level='j']//text()", namespaces=NS)
        ).strip()
        authors = [
            self._join_text(author.xpath(".//tei:persName//text()", namespaces=NS)).strip()
            for author in root.xpath("//tei:titleStmt/tei:author", namespaces=NS)
        ]

        sections: list[dict[str, Any]] = []
        body_divs = root.xpath("//tei:text/tei:body/tei:div", namespaces=NS)
        for index, div in enumerate(body_divs, start=1):
            head = self._join_text(div.xpath("./tei:head//text()", namespaces=NS)).strip()
            
            elements = div.xpath(".//tei:p | .//tei:item | .//tei:formula | .//tei:figDesc | .//tei:note", namespaces=NS)
            parts = []
            for el in elements:
                parts.append(self._join_text(el.xpath(".//text()", namespaces=NS)))
            text = "\n\n".join(part.strip() for part in parts if part.strip())
            
            if not text:
                continue
            sections.append(
                {
                    "section_title": head or f"Section {index}",
                    "section_type": self._infer_section_type(head),
                    "text": text,
                    "page_start": None,
                    "page_end": None,
                }
            )

        references = []
        for ref in root.xpath("//tei:listBibl/tei:biblStruct", namespaces=NS):
            references.append(
                {
                    "title": self._join_text(ref.xpath(".//tei:title//text()", namespaces=NS)).strip(),
                    "doi": self._first_valid_doi(
                        ref.xpath(".//tei:idno[@type='DOI' or @type='doi']/text()", namespaces=NS)
                    ),
                    "raw_text": self._join_text(ref.xpath(".//text()", namespaces=NS)).strip(),
                }
            )

        metadata = {
            "doi": doi or None,
            "title": title or None,
            "year": year,
            "journal": journal or None,
            "authors": [author for author in authors if author],
        }
        return GrobidParseResult(
            metadata=metadata,
            abstract=abstract,
            sections=sections,
            references=references,
            tei_xml=tei_xml,
        )

    @staticmethod
    def _join_text(parts: list[str]) -> str:
        return " ".join(part.strip() for part in parts if part and part.strip())

    @staticmethod
    def _normalize_doi(raw: str | None) -> str | None:
        if not raw:
            return None
        cleaned = raw.strip()
        cleaned = re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", cleaned, flags=re.IGNORECASE)
        match = DOI_RE.search(cleaned)
        if not match:
            return None
        return match.group(0).rstrip(".,;:)").lower()

    @classmethod
    def _first_valid_doi(cls, values: list[str]) -> str | None:
        for value in values:
            normalized = cls._normalize_doi(value)
            if normalized:
                return normalized
        return None

    @staticmethod
    def _safe_year(raw: str) -> int | None:
        if not raw:
            return None
        import re
        match = re.search(r"\b(19|20)\d{2}\b", raw)
        if match:
            return int(match.group(0))
        return None

    @staticmethod
    def _infer_section_type(title: str) -> str:
        normalized = (title or "").strip().lower()
        mapping = {
            "introduction": "introduction",
            "experimental": "methods",
            "methods": "methods",
            "results": "results",
            "discussion": "discussion",
            "conclusion": "conclusion",
            "computational": "computational",
        }
        for key, value in mapping.items():
            if key in normalized:
                return value
        return "body"
