import re
from typing import Any, Dict, List, Union

class CatalystExtractor:
    """
    Extracts Catalyst structure and synthesis details from paper documents using rule-based/regex techniques.
    """
    def __init__(self):
        # Priority section names for Catalyst extraction (case-insensitive substring match)
        self.priority_sections = [
            "synthesis",
            "preparation",
            "experimental",
            "characterization",
            "results and discussion",
            "methods",
            "supporting information",
            "computational details",
            "dft calculations",
            "catalyst preparation"
        ]

        # Initialize regular expressions for each catalyst-related target field
        self._init_regexes()

    def _init_regexes(self):
        # 1. Single Atom / Dual Atom
        self.atomicity_pattern = re.compile(
            r"\b(SACs?|DACs?|single-atom|dual-atom|single\s+atom|dual\s+atom|single-metal\s+atom|isolated\s+single\s+atom|isolated\s+metal\s+atom)\b",
            re.IGNORECASE
        )

        # 2. Metal Centers (Fe, Co, Ni, Cu, Pt, Ru, Ir, Zn, Mn, Pd, Mo, W, V)
        # Match case-sensitively for chemical symbols to avoid matching "We", "I", etc.
        self.metal_case_sensitive = re.compile(
            r"\b(Fe|Co|Ni|Cu|Pt|Ru|Ir|Zn|Mn|Pd|Mo|W|V|Au|Ag|Ti|Zr|Cr|Ce)\b"
        )
        # Match case-insensitively with context (e.g. Fe-based, Co single atom)
        self.metal_context_pattern = re.compile(
            r"\b(Fe|Co|Ni|Cu|Pt|Ru|Ir|Zn|Mn|Pd|Mo|W|V|Au|Ag|Ti|Zr|Cr|Ce)[- ](?:based|atom|metal|center|site|doped|single)s?\b",
            re.IGNORECASE
        )

        # 3. Coordination
        # Fixed character class bug and wrapped full match in group 1
        self.coordination_pattern1 = re.compile(
            r"\b((?:Fe|Co|Ni|Cu|Pt|Ru|Ir|Zn|Mn|Pd|Mo|W|V)-N[1-6])\b"
        )
        self.coordination_pattern2 = re.compile(
            r"\b(coordination\s+environment|coordination\s+number|Fe-N-C|M-N-C|metal-nitrogen|Fe-O|Co-O|Ni-N)\b",
            re.IGNORECASE
        )

        # 4. Support
        self.support_pattern = re.compile(
            r"\b(nitrogen-doped\s+carbon|N-doped\s+carbon|graphene|carbon\s+nanotubes|CNTs?|g-C3N4|C3N4|TiO2|CeO2|SiO2|Al2O3|carbon\s+support|mesoporous\s+carbon|carbon\s+black)\b",
            re.IGNORECASE
        )

        # 5. Synthesis Method
        self.synthesis_pattern = re.compile(
            r"\b(pyrolysis|hydrothermal|wet\s+impregnation|impregnation|atomic\s+layer\s+deposition|ALD|ball\s+milling|electrodeposition|chemical\s+vapor\s+deposition|CVD|sol-gel|calcination)\b",
            re.IGNORECASE
        )

        # 6. Structural Evidence: HAADF-STEM / XANES / EXAFS / XPS
        self.evidence_pattern = re.compile(
            r"\b(HAADF-STEM|XANES|EXAFS|XPS|STEM|aberration-corrected\s+STEM|X-ray\s+absorption\s+near-edge\s+structure|extended\s+X-ray\s+absorption\s+fine\s+structure|X-ray\s+photoelectron\s+spectroscopy|XAS)\b",
            re.IGNORECASE
        )

    def extract(self, unified_document: Any) -> Dict[str, List[Dict[str, Any]]]:
        """
        Main entry point for extraction.
        
        Args:
            unified_document: Can be a UnifiedPaperDocument, a list of sections, or a dict.
            
        Returns:
            A dictionary where keys are the 6 catalyst target fields, and values are lists of extracted records.
        """
        # Initialize output structure matching user requirements exactly
        results = {
            "single atom / dual atom": {},
            "metal centers": {},
            "coordination": {},
            "support": {},
            "synthesis method": {},
            "structural evidence: HAADF-STEM / XANES / EXAFS / XPS": {}
        }

        # 1. Normalize input into a list of sections, list of tables, and list of figures
        sections = []
        tables = []
        figures = []
        abstract = ""

        if isinstance(unified_document, list):
            sections = unified_document
        elif isinstance(unified_document, dict):
            sections = unified_document.get("sections", [])
            tables = unified_document.get("tables", [])
            figures = unified_document.get("figures", [])
            abstract = unified_document.get("abstract", "")
        else:
            # UnifiedPaperDocument object
            if hasattr(unified_document, "sections"):
                sections = unified_document.sections
            if hasattr(unified_document, "tables"):
                tables = unified_document.tables
            if hasattr(unified_document, "figures"):
                figures = unified_document.figures
            if hasattr(unified_document, "abstract"):
                abstract = unified_document.abstract

        # 2. Extract from Abstract
        if abstract:
            self._extract_from_text(abstract, "Abstract", None, results, is_abstract=True)

        # 3. Extract from Sections
        for sec in sections:
            sec_data = self._get_section_data(sec)
            title = sec_data.get("section_title") or sec_data.get("section_type") or "Unknown Section"
            text = sec_data.get("text", "")
            page_start = sec_data.get("page_start")
            page_end = sec_data.get("page_end")
            page = page_start if page_start is not None else page_end

            if text:
                self._extract_from_text(text, title, page, results, is_abstract=False)

        # 4. Extract from Tables (Captions and Markdown content)
        for tbl in tables:
            tbl_data = self._get_section_data(tbl)
            caption = tbl_data.get("caption") or ""
            markdown = tbl_data.get("markdown_content") or ""
            page = tbl_data.get("page")
            
            source_loc = {
                "section": None,
                "page": page,
                "figure": None,
                "table": caption or "Table"
            }
            
            # Extract from table caption
            if caption:
                self._extract_from_text(caption, None, page, results, source_loc_override=source_loc)
            # Extract from table markdown rows
            if markdown:
                self._extract_from_text(markdown, None, page, results, source_loc_override=source_loc, is_table_body=True)

        # 5. Extract from Figures (Captions)
        for fig in figures:
            fig_data = self._get_section_data(fig)
            caption = fig_data.get("caption") or ""
            page = fig_data.get("page")
            
            source_loc = {
                "section": None,
                "page": page,
                "figure": caption or "Figure",
                "table": None
            }
            if caption:
                self._extract_from_text(caption, None, page, results, source_loc_override=source_loc)

        # 6. Convert dictionaries to final list of sorted matches
        final_results = {}
        for field, matches in results.items():
            # Sort matches by confidence descending
            sorted_list = sorted(matches.values(), key=lambda x: x["confidence"], reverse=True)
            final_results[field] = sorted_list

        return final_results

    def _get_section_data(self, sec: Any) -> Dict[str, Any]:
        """Safely extracts dictionary data from a section, table, or figure object."""
        if hasattr(sec, "model_dump"):
            return sec.model_dump()
        elif hasattr(sec, "__dict__"):
            return {k: v for k, v in sec.__dict__.items() if not k.startswith('_')}
        elif isinstance(sec, dict):
            return sec
        return {}

    def _split_sentences(self, text: str) -> List[str]:
        """Splits a body of text into sentences using simple, robust rules."""
        # Clean text
        text = re.sub(r'\s+', ' ', text)
        sentence_end = re.compile(r'(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?)\s')
        return [s.strip() for s in sentence_end.split(text) if s.strip()]

    def _check_priority(self, title: str) -> bool:
        """Checks if a section title matches any of the prioritized extraction sections."""
        if not title:
            return False
        title_lower = title.lower()
        return any(kw in title_lower for kw in self.priority_sections)

    def _extract_from_text(
        self, 
        text: str, 
        section_title: str | None, 
        page: int | None, 
        results: Dict[str, Dict[str, Any]], 
        is_abstract: bool = False,
        source_loc_override: Dict[str, Any] = None,
        is_table_body: bool = False
    ):
        """Helper to scan a block of text sentence-by-sentence for catalyst parameters."""
        sentences = self._split_sentences(text)
        is_priority = self._check_priority(section_title) if section_title else False

        # Set default base confidence
        if is_priority:
            base_conf = 0.90
        elif is_abstract:
            base_conf = 0.80
        elif is_table_body:
            base_conf = 0.70
        else:
            base_conf = 0.60

        for sentence in sentences:
            evidence = sentence
            
            # Generate default source location if not overridden
            if source_loc_override:
                loc = source_loc_override.copy()
            else:
                loc = {
                    "section": section_title,
                    "page": page,
                    "figure": None,
                    "table": None
                }

            # 1. Single Atom / Dual Atom
            for match in self.atomicity_pattern.finditer(sentence):
                raw_val = match.group(1).strip()
                # Normalize values
                val = raw_val
                if raw_val.lower() in ["sac", "sacs", "single atom", "single-atom", "isolated single atom", "isolated metal atom", "single-metal atom"]:
                    val = "Single-Atom Catalyst (SAC)"
                elif raw_val.lower() in ["dac", "dacs", "dual atom", "dual-atom"]:
                    val = "Dual-Atom Catalyst (DAC)"
                self._add_match(results["single atom / dual atom"], val, None, evidence, loc, base_conf + 0.05)

            # 2. Metal Centers
            # Check case-sensitive chemical symbols
            for match in self.metal_case_sensitive.finditer(sentence):
                val = match.group(1).strip()
                # Slightly lower confidence for single case-sensitive symbols to avoid false positives
                self._add_match(results["metal centers"], val, None, evidence, loc, base_conf)
            # Check contextual phrases
            for match in self.metal_context_pattern.finditer(sentence):
                val = match.group(1).strip()
                # Contextual match is very reliable, boost confidence!
                self._add_match(results["metal centers"], val, None, evidence, loc, base_conf + 0.08)

            # 3. Coordination
            for match in self.coordination_pattern1.finditer(sentence):
                val = match.group(1).strip()
                self._add_match(results["coordination"], val, None, evidence, loc, base_conf + 0.08)
            for match in self.coordination_pattern2.finditer(sentence):
                val = match.group(1).strip()
                self._add_match(results["coordination"], val, None, evidence, loc, base_conf)

            # 4. Support
            for match in self.support_pattern.finditer(sentence):
                val = match.group(1).strip()
                # Normalize common supports
                if val.lower() in ["nitrogen-doped carbon", "n-doped carbon"]:
                    val = "N-doped carbon"
                elif val.lower() in ["g-c3n4", "c3n4"]:
                    val = "g-C3N4"
                elif val.lower() in ["cnts", "cnt", "carbon nanotubes"]:
                    val = "Carbon Nanotubes (CNTs)"
                self._add_match(results["support"], val, None, evidence, loc, base_conf + 0.05)

            # 5. Synthesis Method
            for match in self.synthesis_pattern.finditer(sentence):
                val = match.group(1).strip()
                # Normalize names
                if val.lower() == "ald":
                    val = "Atomic Layer Deposition (ALD)"
                elif val.lower() == "cvd":
                    val = "Chemical Vapor Deposition (CVD)"
                self._add_match(results["synthesis method"], val, None, evidence, loc, base_conf + 0.05)

            # 6. Structural Evidence: HAADF-STEM / XANES / EXAFS / XPS
            for match in self.evidence_pattern.finditer(sentence):
                raw_val = match.group(1).strip()
                val = raw_val.upper()
                if "HAADF" in val:
                    val = "HAADF-STEM"
                elif "NEAR-EDGE" in raw_val.lower():
                    val = "XANES"
                elif "EXTENDED" in raw_val.lower():
                    val = "EXAFS"
                elif "PHOTOELECTRON" in raw_val.lower():
                    val = "XPS"
                self._add_match(results["structural evidence: HAADF-STEM / XANES / EXAFS / XPS"], val, None, evidence, loc, base_conf + 0.08)

    def _add_match(
        self, 
        field_matches: Dict[str, Dict[str, Any]], 
        value: str, 
        unit: str | None, 
        evidence: str, 
        location: Dict[str, Any], 
        confidence: float
    ):
        """Adds a match to the dictionary, keeping the one with higher confidence."""
        # Ensure confidence is capped at 0.99
        confidence = min(0.99, max(0.1, round(confidence, 2)))
        
        val_key = value.strip().lower()
        if not val_key:
            return

        # If already exists, keep the one with higher confidence
        if val_key in field_matches:
            if confidence > field_matches[val_key]["confidence"]:
                field_matches[val_key] = {
                    "value": value,
                    "unit": unit,
                    "evidence_text": evidence,
                    "source_location": location,
                    "confidence": confidence
                }
        else:
            field_matches[val_key] = {
                "value": value,
                "unit": unit,
                "evidence_text": evidence,
                "source_location": location,
                "confidence": confidence
            }
