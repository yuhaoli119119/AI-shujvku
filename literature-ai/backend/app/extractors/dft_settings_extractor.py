import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Union

class DFTSettingsExtractor:
    """
    Extracts DFT computational settings from paper documents using rule-based/regex techniques.
    """
    def __init__(self, config_path: str = None):
        self.rules = {}
        # Try to load rules from YAML configuration
        if not config_path:
            possible_paths = [
                Path(__file__).resolve().parents[3] / "prompts" / "dft_settings.yaml",
                Path(__file__).resolve().parents[2] / "prompts" / "dft_settings.yaml",
                Path("prompts/dft_settings.yaml"),
                Path("../prompts/dft_settings.yaml"),
            ]
            for p in possible_paths:
                if p.exists():
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            self.rules = yaml.safe_load(f) or {}
                        break
                    except Exception:
                        pass
        else:
            p = Path(config_path)
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        self.rules = yaml.safe_load(f) or {}
                except Exception:
                    pass

        # Priority section names for extraction (case-insensitive substring match)
        self.priority_sections = [
            "computational details",
            "methods",
            "supporting information",
            "dft calculations",
            "theoretical calculations",
            "computational methods",
            "theoretical methods",
            "experimental section",
            "computational study"
        ]

        # Initialize regular expressions for each target field
        self._init_regexes()

    def _init_regexes(self):
        # 1. Software
        self.software_pattern = re.compile(
            r"\b(VASP|Vienna\s+Ab\s+initio\s+Simulation\s+Package|Gaussian(?:\s*(?:09|16|98|03|05|10))?|G09|G16|ORCA|CP2K|Quantum\s+ESPRESSO|QE\b|DMol3|Materials\s+Studio|Q-Chem|NWChem|CASTEP|GPAW|ABINIT|SIESTA)\b",
            re.IGNORECASE
        )

        # 2. Functional
        self.functional_pattern = re.compile(
            r"\b(PBE\+U|PBEsol|PBE|B3LYP|HSE06|HSE|PW91|RPBE|revPBE|LDA|GGA|meta-GGA|TPSS|M06-2X|B97D)\b",
            re.IGNORECASE
        )

        # 3. Dispersion Correction
        self.dispersion_pattern = re.compile(
            r"\b(DFT-D3\(BJ\)|DFT-D3|D3\(BJ\)|D3|DFT-D2|D2|DFT-D|Grimme(?:\'s)?\s*(?:D3|D2|dispersion)|Tkatchenko-Scheffler|TS\s+dispersion|vdW-DF2|vdW-DF)\b",
            re.IGNORECASE
        )

        # 4. Pseudopotential / Basis Set
        self.basis_pattern = re.compile(
            r"\b(PAW|Projector\s+Augmented\s+Wave|USPP|ultrasoft\s+pseudopotential|norm-conserving|6-31G\(d\)|6-31G\*|6-31\+G\*|6-311\+G\(d,p\)|6-31\+G\(d,p\)|6-311G\*\*|def2-SVP|def2-TZVP|def2SVP|def2TZVP|LANL2DZ|double-zeta|triple-zeta|DZVP|TZVP|cc-pVDZ|cc-pVTZ)\b",
            re.IGNORECASE
        )

        # 5. Cutoff Energy
        # Matches e.g. "kinetic energy cutoff was set to 400 eV", "cutoff: 400 eV", "cutoff 400 eV"
        self.cutoff_pattern1 = re.compile(
            r"\b(?:cut-off|cutoff|kinetic\s+energy|energy\s+cutoff|plane-wave\s+cutoff)\s*(?:energy|cutoff)?(?:\s*(?:is|was|of|set|to|at|be|value|valued|below|within|above|:|=))*\s*(\d+(?:\.\d+)?)\s*(eV|Ry|Rydberg)\b",
            re.IGNORECASE
        )
        self.cutoff_pattern2 = re.compile(
            r"(\d+(?:\.\d+)?)\s*(eV|Ry|Rydberg)\s*(?:for\s+)?(?:the\s+)?(?:cut-off|cutoff|energy\s+cutoff)",
            re.IGNORECASE
        )

        # 6. K-points
        self.kpoint_pattern = re.compile(
            r"\b(\d+)\s*(?:x|×|\\times|\*)\s*(\d+)\s*(?:x|×|\\times|\*)\s*(\d+)\b",
            re.IGNORECASE
        )
        self.kpoint_keywords = re.compile(
            r"\b(Monkhorst-Pack|Gamma-centered|gamma\s+centered|k-point\s+grid|k-point\s+mesh|k-points)\b",
            re.IGNORECASE
        )

        # 7. Convergence Criteria
        # Electronic energy convergence (matches e.g. "electronic convergence criterion was set to 1e-5 eV", "convergence: 1e-5 eV")
        self.energy_conv_pattern = re.compile(
            r"(?:electronic\s+)?(?:energy\s+)?(?:convergence|tolerance|threshold)\s*(?:criterion|criteria|threshold)?(?:\s*(?:is|was|of|set|to|at|be|below|within|value|valued|:|=))*\s*(10\s*(?:\^|^-)?\s*-?[4-8]|1[eE]-?[4-8]|\d+(?:\.\d+)?\s*(?:x|×)\s*10\s*(?:\^|^-)?\s*-?[4-8])\s*(eV)",
            re.IGNORECASE
        )
        # Force/relaxation convergence
        self.force_conv_pattern = re.compile(
            r"(?:force|ionic|relaxation)\s+(?:convergence|tolerance|threshold|criteria|criterion)?(?:\s*(?:is|was|of|set|to|at|be|below|within|value|valued|:|=))*\s*([0-9\.]+)\s*(eV\s*/\s*(?:Å|A|nm|Angstrom)|eV\s*Å\s*-1|eV\s*A\s*-1)",
            re.IGNORECASE
        )

        # 8. Vacuum Thickness
        self.vacuum_pattern1 = re.compile(
            r"(?:vacuum|vacuum\s+layer|vacuum\s+space|vacuum\s+thickness|vacuum\s+size)\s*(?:of|is|was|set\s+to|about)?\s*(\d+(?:\.\d+)?)\s*(Å|A|nm)\b",
            re.IGNORECASE
        )
        self.vacuum_pattern2 = re.compile(
            r"(\d+(?:\.\d+)?)\s*(Å|A|nm)\s*(?:vacuum\s+layer|vacuum\s+space|vacuum\s+thickness|vacuum)",
            re.IGNORECASE
        )

        # 9. Spin Polarization
        self.spin_pattern = re.compile(
            r"\b(spin-polarized|spin\s+polarization|ISPIN\s*=\s*2|non-spin-polarized|spin-unpolarized)\b",
            re.IGNORECASE
        )

        # 10. Solvation Model
        self.solvation_pattern = re.compile(
            r"\b(implicit\s+solvation|SMD|PCM|COSMO|VASPSOL|VASPsol|solvent\s+effect|polarizable\s+continuum\s+model|solvation\s+model|implicit\s+solvent)\b",
            re.IGNORECASE
        )

    def extract(self, unified_document: Any) -> Dict[str, List[Dict[str, Any]]]:
        """
        Main entry point for extraction.
        
        Args:
            unified_document: Can be a UnifiedPaperDocument, a list of sections, or a dict.
            
        Returns:
            A dictionary where keys are the 10 target fields, and values are lists of extracted records.
        """
        # Initialize output structure
        results = {
            "software": {},
            "functional": {},
            "dispersion correction": {},
            "pseudopotential / basis set": {},
            "cutoff energy": {},
            "k-points": {},
            "convergence criteria": {},
            "vacuum thickness": {},
            "spin polarization": {},
            "solvation model": {}
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
        """Helper to scan a block of text sentence-by-sentence for settings."""
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

            # 1. Software
            for match in self.software_pattern.finditer(sentence):
                raw_val = match.group(1)
                # Normalize values
                val = raw_val.strip()
                if "Vienna" in val:
                    val = "VASP"
                elif val.upper() == "QE":
                    val = "Quantum ESPRESSO"
                elif val.upper() in ["G09", "G16"]:
                    val = "Gaussian"
                
                # Check for version in proximity, avoiding single dots
                version_match = re.search(rf"{raw_val}\s*(?:v(?:ersion)?\s*)?([0-9]+(?:\.[0-9]+)*)", sentence, re.IGNORECASE)
                if version_match:
                    val = f"{val} {version_match.group(1)}"

                self._add_match(results["software"], val, None, evidence, loc, base_conf)

            # 2. Functional
            for match in self.functional_pattern.finditer(sentence):
                val = match.group(1).strip()
                conf_bonus = 0.05 if val in ["PBE+U", "HSE06"] else 0.0
                self._add_match(results["functional"], val, None, evidence, loc, base_conf + conf_bonus)

            # 3. Dispersion Correction
            for match in self.dispersion_pattern.finditer(sentence):
                val = match.group(1).strip()
                # Normalize Grimme D3/D2
                if "D3" in val:
                    val = "DFT-D3"
                elif "D2" in val:
                    val = "DFT-D2"
                elif "TS" in val or "Tkatchenko" in val:
                    val = "Tkatchenko-Scheffler (TS)"
                self._add_match(results["dispersion correction"], val, None, evidence, loc, base_conf + 0.05)

            # 4. Pseudopotential / Basis Set
            for match in self.basis_pattern.finditer(sentence):
                val = match.group(1).strip()
                # Normalize PAW
                if val.upper() == "PAW" or "Projector" in val:
                    val = "PAW"
                self._add_match(results["pseudopotential / basis set"], val, None, evidence, loc, base_conf)

            # 5. Cutoff Energy
            # Try Pattern 1 first
            for match in self.cutoff_pattern1.finditer(sentence):
                val = match.group(1).strip()
                unit = match.group(2).strip()
                self._add_match(results["cutoff energy"], val, unit, evidence, loc, base_conf + 0.08)
            # Try Pattern 2
            for match in self.cutoff_pattern2.finditer(sentence):
                val = match.group(1).strip()
                unit = match.group(2).strip()
                self._add_match(results["cutoff energy"], val, unit, evidence, loc, base_conf + 0.08)

            # 6. K-points
            # Extract grid first
            grid_match = self.kpoint_pattern.search(sentence)
            has_kw = self.kpoint_keywords.search(sentence)
            if grid_match:
                grid_val = f"{grid_match.group(1)}×{grid_match.group(2)}×{grid_match.group(3)}"
                conf = base_conf + 0.08 if has_kw else base_conf
                self._add_match(results["k-points"], grid_val, None, evidence, loc, conf)
            elif has_kw:
                # E.g. "Monkhorst-Pack grid"
                kw = has_kw.group(1).strip()
                if kw.lower() in ["monkhorst-pack", "gamma-centered", "gamma centered"]:
                    self._add_match(results["k-points"], kw, None, evidence, loc, base_conf)

            # 7. Convergence Criteria
            # Electronic energy convergence
            for match in self.energy_conv_pattern.finditer(sentence):
                val = match.group(1).strip()
                unit = match.group(2).strip()
                self._add_match(results["convergence criteria"], f"Energy: {val}", unit, evidence, loc, base_conf + 0.05)
            # Force convergence
            for match in self.force_conv_pattern.finditer(sentence):
                val = match.group(1).strip()
                unit = match.group(2).strip()
                self._add_match(results["convergence criteria"], f"Force: {val}", unit, evidence, loc, base_conf + 0.05)

            # 8. Vacuum Thickness
            for match in self.vacuum_pattern1.finditer(sentence):
                val = match.group(1).strip()
                unit = match.group(2).strip()
                self._add_match(results["vacuum thickness"], val, unit, evidence, loc, base_conf + 0.05)
            for match in self.vacuum_pattern2.finditer(sentence):
                val = match.group(1).strip()
                unit = match.group(2).strip()
                self._add_match(results["vacuum thickness"], val, unit, evidence, loc, base_conf + 0.05)

            # 9. Spin Polarization
            for match in self.spin_pattern.finditer(sentence):
                raw_val = match.group(1).strip()
                val = "Spin-polarized"
                if "non-spin" in raw_val.lower() or "unpolarized" in raw_val.lower():
                    val = "Non-spin-polarized"
                self._add_match(results["spin polarization"], val, None, evidence, loc, base_conf + 0.05)

            # 10. Solvation Model
            for match in self.solvation_pattern.finditer(sentence):
                val = match.group(1).strip()
                # Normalize names
                if val.upper() in ["VASPSOL", "VASPSOL"]:
                    val = "VASPsol"
                elif val.upper() == "SMD":
                    val = "SMD (solvation model density)"
                elif val.upper() == "PCM":
                    val = "PCM (polarizable continuum model)"
                elif val.upper() == "COSMO":
                    val = "COSMO"
                self._add_match(results["solvation model"], val, None, evidence, loc, base_conf + 0.05)

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
