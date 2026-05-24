import os
import httpx
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
from ..config import settings

class GrobidParser:
    def __init__(self):
        self.url = settings.GROBID_URL + "/api/processFulltextDocument"

    async def parse_pdf(self, pdf_path: str) -> List[Dict[str, str]]:
        """
        Parses PDF using GROBID and returns a list of sections.
        Each section is a dict with 'title' and 'text'.
        """
        if not os.path.exists(pdf_path):
            return []

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(pdf_path, "rb") as f:
                files = {"input": f}
                response = await client.post(self.url, files=files)
                if response.status_code != 200:
                    return []
                tei_xml = response.text

        return self.parse_tei(tei_xml)

    def parse_tei(self, tei_xml: str) -> List[Dict[str, str]]:
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}
        root = ET.fromstring(tei_xml)
        
        sections = []
        
        # Extract Abstract
        abstract_node = root.find(".//tei:profileDesc/tei:abstract", ns)
        if abstract_node is not None:
            text = "".join(abstract_node.itertext()).strip()
            if text:
                sections.append({"title": "Abstract", "text": text})

        # Extract Body Sections
        body = root.find(".//tei:body", ns)
        if body is not None:
            for div in body.findall(".//tei:div", ns):
                head = div.find("tei:head", ns)
                title = head.text.strip() if head is not None and head.text else "Section"
                
                # Get all paragraphs
                paragraphs = []
                for p in div.findall("tei:p", ns):
                    paragraphs.append("".join(p.itertext()).strip())
                
                text = "\n\n".join(paragraphs)
                if text:
                    sections.append({"title": title, "text": text})

        return sections
