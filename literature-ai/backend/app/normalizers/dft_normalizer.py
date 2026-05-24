from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SOFTWARE_PATTERNS = [
    r"\bVASP\b",
    r"\bGaussian(?:0?9|16)?\b",
    r"\bQuantum\s+ESPRESSO\b",
    r"\bCP2K\b",
    r"\bORCA\b",
    r"\bCASTEP\b",
    r"\bABINIT\b",
    r"\bSIESTA\b",
]
FUNCTIONAL_PATTERNS = [
    r"\bPBE0?\b",
    r"\bRPBE\b",
    r"\brevPBE\b",
    r"\bHSE0?6\b",
    r"\bB3LYP\b",
    r"\bSCAN\b",
    r"\bTPSS\b",
    r"\bPW91\b",
    r"\bLDA\b",
    r"\bGGA\b",
]
PSEUDOPOTENTIAL_PATTERNS = [
    r"\bPAW\b",
    r"\bprojector\s+augmented\s+wave\b",
    r"\bultrasoft\b",
    r"\bnorm-?conserving\b",
    r"\bpseudopotential(?:s)?\b",
    r"\b6-31G(?:\(d,p\)|\*+)?\b",
    r"\bdef2-(?:SVP|TZVP)\b",
    r"\bcc-pV(?:D|T)Z\b",
    r"\bLANL2DZ\b",
]
DISPERSION_PATTERNS = [
    r"\bDFT-D2\b",
    r"\bDFT-D3(?:\(BJ\)|BJ)?\b",
    r"\bD3(?:BJ)?\b",
    r"\bD4\b",
    r"\bvdW-DF2?\b",
    r"\bvan\s+der\s+Waals\b",
    r"\bdispersion\s+correction\b",
    r"\bGrimme\b",
]
FORMULA_PATTERNS = [
    r"\bE_ads\s*=",
    r"\bE_bind\s*=",
    r"(?:Delta|\u0394)\s*G\s*=",
    r"(?:Delta|\u0394)\s*E\s*=",
    r"\bZPE\b",
    r"\bTS\b",
    r"\bG\s*=\s*H\s*-\s*T\s*S\b",
]
STRUCTURE_PATTERNS = [
    r"\bSupplementary\s+Information\b",
    r"\bSupporting\s+Information\b",
    r"\bPOSCAR\b",
    r"\bCIF\b",
    r"\bxyz\b",
    r"\batomic\s+coordinates?\b",
    r"\bstructure\s+coordinates?\b",
    r"\bTable\s+S\d+\b",
]


@dataclass
class ReproducibilityScore:
    score: int
    missing_items: list[str]
    risk_level: str
    details: dict[str, bool] = field(default_factory=dict)


class DFTNormalizer:
    """Normalize DFT metadata and compute a reproducibility score."""

    cutoff_pattern = re.compile(
        r"(?:ecut|encut|plane[- ]wave\s+cutoff|cutoff\s+energy)\s*(?:was\s+set\s+to|was|=|:|of|at|is)?\s*([\d.]+)\s*(eV|Ry|Ha)\b",
        re.IGNORECASE,
    )
    kpoint_pattern = re.compile(
        r"(\d+)\s*[xX*]\s*(\d+)\s*[xX*]\s*(\d+)\b",
        re.IGNORECASE,
    )
    kpoint_context_pattern = re.compile(
        r"(?:k[- ]points?|Monkhorst[- ]Pack|k[- ]mesh|k[- ]grid)",
        re.IGNORECASE,
    )
    convergence_pattern = re.compile(
        r"(?:EDIFF|EDIFFG|convergence(?:\s+criterion|\s+criteria|\s+threshold)?|force\s+tolerance|scf\s+convergence)\s*(?:=|:|was\s+set\s+to|of|at|is)?\s*([0-9.]+(?:e[-+]?\d+)?|10\^?-?\d+)\s*(eV|Ry|Ha|eV/A)?",
        re.IGNORECASE,
    )
    vacuum_pattern = re.compile(
        r"(?:vacuum(?:\s+layer|\s+thickness|\s+spacing)?)(?:\s+of|\s+was|\s+is|\s*=|\s*:|\s+set\s+to)?\s*([\d.]+)\s*(A|angstrom|Angstrom|nm|AA)\b",
        re.IGNORECASE,
    )

    def normalize(self, payload: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(payload, list):
            return [self._normalize_single(item) for item in payload]
        return self._normalize_single(payload)

    def _normalize_single(self, data: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(self._collect_text(data))
        score = self.calculate_reproducibility_score(text, data)
        return {
            **data,
            "dft_reproducibility_score": score.score,
            "missing_items": score.missing_items,
            "risk_level": score.risk_level,
            "_reproducibility_details": score.details,
            "_normalized": {
                "software": self._extract_software(text),
                "functional": self._extract_functional(text),
                "cutoff": self._extract_cutoff(text),
                "kpoints": self._extract_kpoints(text),
                "convergence": self._extract_convergence(text),
                "vacuum": self._extract_vacuum(text),
                "dispersion": self._has_dispersion(text),
            },
        }

    def calculate_reproducibility_score(
        self,
        text: str,
        raw_data: dict[str, Any] | None = None,
    ) -> ReproducibilityScore:
        haystack = " ".join(filter(None, [text, " ".join(self._collect_text(raw_data or {}))]))
        checks = {
            "dft_software": self._extract_software(haystack) is not None,
            "functional": self._extract_functional(haystack) is not None,
            "pseudopotential": self._search_patterns(PSEUDOPOTENTIAL_PATTERNS, haystack),
            "cutoff_energy": self._extract_cutoff(haystack) is not None,
            "kpoints": self._extract_kpoints(haystack) is not None,
            "convergence_criteria": self._extract_convergence(haystack) is not None,
            "vacuum_thickness": self._extract_vacuum(haystack) is not None,
            "dispersion_correction": self._has_dispersion(haystack),
            "adsorption_or_free_energy_formula": self._search_patterns(FORMULA_PATTERNS, haystack),
            "structure_coordinates": self._search_patterns(STRUCTURE_PATTERNS, haystack),
        }
        score = sum(1 for passed in checks.values() if passed)
        missing = [name for name, passed in checks.items() if not passed]
        if score >= 8:
            risk = "low"
        elif score >= 5:
            risk = "medium"
        else:
            risk = "high"
        return ReproducibilityScore(score=score, missing_items=missing, risk_level=risk, details={f"has_{k}": v for k, v in checks.items()})

    def _collect_text(self, data: Any) -> list[str]:
        texts: list[str] = []
        if isinstance(data, str):
            return [data]
        if isinstance(data, dict):
            for value in data.values():
                texts.extend(self._collect_text(value))
        elif isinstance(data, list):
            for value in data:
                texts.extend(self._collect_text(value))
        return texts

    def _extract_software(self, text: str) -> str | None:
        for pattern in SOFTWARE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        return None

    def _extract_functional(self, text: str) -> str | None:
        for pattern in FUNCTIONAL_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        return None

    def _extract_cutoff(self, text: str) -> dict[str, Any] | None:
        match = self.cutoff_pattern.search(text)
        if not match:
            return None
        return self.clean_cutoff_string(f"{match.group(1)} {match.group(2)}")

    def _extract_kpoints(self, text: str) -> dict[str, int] | None:
        if not self.kpoint_context_pattern.search(text):
            return None
        match = self.kpoint_pattern.search(text)
        if not match:
            return None
        return {
            "kx": int(match.group(1)),
            "ky": int(match.group(2)),
            "kz": int(match.group(3)),
        }

    def _extract_convergence(self, text: str) -> dict[str, Any] | None:
        match = self.convergence_pattern.search(text)
        if not match:
            return None
        return {
            "value": match.group(1),
            "unit": (match.group(2) or "").strip() or None,
        }

    def _extract_vacuum(self, text: str) -> dict[str, Any] | None:
        match = self.vacuum_pattern.search(text)
        if not match:
            return None
        value = float(match.group(1))
        unit = match.group(2)
        if unit.lower() == "nm":
            value *= 10.0
        return {"value": round(value, 4), "unit": "A"}

    def _has_dispersion(self, text: str) -> bool:
        return self._search_patterns(DISPERSION_PATTERNS, text)

    @staticmethod
    def clean_kpoint_string(raw: str) -> dict[str, int] | None:
        match = re.search(r"(\d+)\s*[xX*]\s*(\d+)\s*[xX*]\s*(\d+)", raw)
        if not match:
            return None
        return {"kx": int(match.group(1)), "ky": int(match.group(2)), "kz": int(match.group(3))}

    @staticmethod
    def clean_cutoff_string(raw: str) -> dict[str, Any] | None:
        match = re.search(r"([\d.]+)\s*(eV|Ry|Ha)", raw, re.IGNORECASE)
        if not match:
            return None
        return {"value": float(match.group(1)), "unit": match.group(2).lower()}

    @staticmethod
    def _search_patterns(patterns: list[str], text: str) -> bool:
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
