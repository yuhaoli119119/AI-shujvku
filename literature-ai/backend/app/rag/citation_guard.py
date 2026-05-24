from __future__ import annotations

import re
from typing import Any


NUMERIC_PATTERN = re.compile(
    r"(?P<value>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>eV|meV|mAh/g|Ah/kg|mg/cm2|wt%|uL/mg|%/cycle|C)\b",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(r"[a-z0-9_+-]+", re.IGNORECASE)
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")
CONTEXT_KEYWORDS: dict[str, set[str]] = {
    "ev": {"adsorption", "energy", "barrier", "free", "reaction", "intermediate", "lips", "li2s4", "descriptor", "dft"},
    "mev": {"adsorption", "energy", "barrier", "free", "reaction", "descriptor", "dft"},
    "mah/g": {"capacity", "discharge", "charge", "specific", "retention", "cell", "delivered"},
    "ah/kg": {"capacity", "energy", "specific"},
    "mg/cm2": {"loading", "sulfur", "areal"},
    "wt%": {"content", "loading", "sulfur", "electrolyte"},
    "ul/mg": {"electrolyte", "ratio"},
    "%/cycle": {"decay", "fade", "retention", "cycle"},
    "c": {"rate", "current", "cycling", "cell"},
}
FACT_TRIGGER_SYNONYMS: dict[str, set[str]] = {
    "accelerates": {"accelerates", "accelerate", "promotes", "facilitates", "speeds"},
    "suppresses": {"suppresses", "suppress", "inhibits", "hinders", "retards"},
    "strengthens": {"strengthens", "strengthen", "strengthening", "stronger", "enhances", "improves", "boosts"},
    "weakens": {"weakens", "weaken", "weakening", "weaker", "reduces", "decreases", "diminishes", "lower", "lowers", "lowering"},
    "stabilizes": {"stabilizes", "stabilize", "stabilizing", "anchors"},
    "superior": {"superior", "outperform", "outperforms", "better", "best", "highest", "lowest", "most", "least"},
    "evidences": {"demonstrates", "demonstrate", "demonstrated", "indicates", "indicate", "indicated", "confirms", "confirm", "confirmed", "reveals", "reveal", "revealed", "proves", "prove", "proven", "establishes", "establish", "established"},
    "causes": {"causes", "cause", "caused", "drives", "drive", "driven", "yields", "yield", "therefore", "thus", "hence", "consequently", "attributed"},
    "mediates": {"mediates", "mediate", "via", "through", "coordinated", "coordinating", "anchored", "docked"},
    "infers_causality": {"as a result", "because", "owing to", "due to", "resulting in", "leads to", "lead to"},
}
FACT_CONTEXT_KEYWORDS = {
    "conversion",
    "binding",
    "adsorption",
    "kinetics",
    "barrier",
    "energy",
    "capacity",
    "retention",
    "stability",
    "sulfur",
    "lips",
    "li2s4",
    "polysulfide",
    "catalyst",
    "cathode",
}
STRICT_FACT_CONTEXT_KEYWORDS = {
    "barrier",
    "adsorption",
    "binding",
    "capacity",
    "retention",
    "stability",
    "cyclability",
    "coordination",
    "mechanism",
}


class CitationGuard:
    """Validate that key numeric and high-risk fact claims exist in retrieved evidence."""

    def validate(self, text: str, facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]]) -> dict[str, Any]:
        numeric_claims = self._extract_numeric_claims(text)
        fact_claims = self._collect_fact_claims(facts)
        textual_claims = self._extract_textual_claims(text)

        supported: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for claim in numeric_claims:
            match = self._find_supporting_claim(claim, fact_claims)
            if match is None:
                missing.append(claim)
            else:
                supported.append({"claim": claim, "supported_by": match})

        supported_textual: list[dict[str, Any]] = []
        missing_textual: list[dict[str, Any]] = []
        for claim in textual_claims:
            match = self._find_supporting_textual_claim(claim, facts)
            if match is None:
                missing_textual.append(claim)
            else:
                supported_textual.append({"claim": claim, "supported_by": match})
        return {
            "ok": not missing and not missing_textual,
            "supported_values": supported,
            "missing_values": missing,
            "supported_fact_claims": supported_textual,
            "missing_fact_claims": missing_textual,
            "checked_count": len(numeric_claims),
            "checked_fact_count": len(textual_claims),
        }

    def _extract_numeric_claims(self, text: str) -> list[dict[str, Any]]:
        claims = []
        for match in NUMERIC_PATTERN.finditer(text or ""):
            claims.append(
                {
                    "value": float(match.group("value")),
                    "unit": self._normalize_unit(match.group("unit")),
                    "literal": match.group(0),
                    "context": self._extract_context(text, match.start(), match.end()),
                }
            )
        return claims

    def _collect_fact_claims(self, facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(facts, dict):
            for group in facts.values():
                if isinstance(group, list):
                    items.extend(group)
        elif isinstance(facts, list):
            items.extend(facts)

        claims: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            unit = item.get("unit")
            if value is not None and unit:
                claims.append(
                    {
                        "value": float(value),
                        "unit": self._normalize_unit(str(unit)),
                        "source": item.get("evidence_text") or item.get("text") or "",
                        "context": self._infer_fact_context(item),
                    }
                )
            text = " ".join(filter(None, [item.get("text"), item.get("evidence_text")]))
            for match in NUMERIC_PATTERN.finditer(text):
                claims.append(
                    {
                        "value": float(match.group("value")),
                        "unit": self._normalize_unit(match.group("unit")),
                        "source": text,
                        "context": self._extract_context(text, match.start(), match.end()),
                    }
                )
        return claims

    def _find_supporting_claim(self, claim: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any] | None:
        for fact in facts:
            if fact["unit"] != claim["unit"]:
                continue
            if abs(fact["value"] - claim["value"]) <= self._tolerance(claim["unit"]):
                if self._context_matches(claim, fact):
                    return fact
        return None

    def _extract_textual_claims(self, text: str) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for sentence in self._split_sentences(text):
            tokens = self._tokenize(sentence)
            triggers = self._extract_fact_triggers(tokens, sentence=sentence)
            if not triggers:
                continue
            context = sorted(tokens & FACT_CONTEXT_KEYWORDS)
            claims.append(
                {
                    "sentence": sentence,
                    "triggers": triggers,
                    "context": context,
                }
            )
        return claims

    def _find_supporting_textual_claim(
        self,
        claim: dict[str, Any],
        facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        evidence_items = self._collect_textual_evidence_items(facts)
        supported_by: list[dict[str, Any]] = []
        for trigger in claim.get("triggers") or []:
            match = self._find_support_for_trigger(trigger, claim, evidence_items)
            if match is None:
                return None
            supported_by.append({"trigger": trigger, "evidence": match})
        return {"supports": supported_by}

    def _find_support_for_trigger(
        self,
        trigger: str,
        claim: dict[str, Any],
        evidence_items: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for item in evidence_items:
            if trigger not in set(item.get("triggers") or []):
                continue
            claim_context = set(claim.get("context") or [])
            evidence_context = set(item.get("context") or [])
            if claim_context and evidence_context and not (claim_context & evidence_context):
                continue
            strict_context = claim_context & STRICT_FACT_CONTEXT_KEYWORDS
            if strict_context and not strict_context.issubset(evidence_context):
                continue
            return item
        return None

    def _context_matches(self, claim: dict[str, Any], fact: dict[str, Any]) -> bool:
        claim_context = set(claim.get("context") or [])
        fact_context = set(fact.get("context") or [])
        if not claim_context or not fact_context:
            return True
        keywords = CONTEXT_KEYWORDS.get(claim["unit"], set())
        scoped_claim = claim_context & keywords if keywords else claim_context
        scoped_fact = fact_context & keywords if keywords else fact_context
        if not scoped_claim or not scoped_fact:
            return True
        return bool(scoped_claim & scoped_fact)

    def _infer_fact_context(self, item: dict[str, Any]) -> list[str]:
        tokens = set()
        for key in ["property_type", "adsorbate", "reaction_step", "claim_type", "rate", "text", "evidence_text"]:
            value = item.get(key)
            if value:
                tokens.update(self._tokenize(str(value)))
        if item.get("capacity_value") is not None:
            tokens.add("capacity")
        if item.get("cycle_number") is not None:
            tokens.add("cycle")
        return sorted(tokens)

    def _extract_context(self, text: str, start: int, end: int) -> list[str]:
        window = text[max(0, start - 80): min(len(text), end + 80)]
        return sorted(self._tokenize(window))

    def _collect_textual_evidence_items(
        self, facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(facts, dict):
            for group in facts.values():
                if isinstance(group, list):
                    items.extend(group)
        elif isinstance(facts, list):
            items.extend(facts)

        evidence: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = " ".join(
                filter(
                    None,
                    [
                        item.get("text"),
                        item.get("claim_text"),
                        item.get("evidence_text"),
                        item.get("research_gap"),
                        item.get("proposed_solution"),
                        item.get("core_hypothesis"),
                    ],
                )
            ).strip()
            if not text:
                continue
            tokens = self._tokenize(text)
            triggers = self._extract_fact_triggers(tokens, sentence=text)
            if not triggers:
                continue
            evidence.append(
                {
                    "text": text,
                    "triggers": triggers,
                    "context": sorted(tokens & FACT_CONTEXT_KEYWORDS),
                }
            )
        return evidence

    def _extract_fact_triggers(self, tokens: set[str], sentence: str = "") -> list[str]:
        triggers = []
        lowered_sentence = sentence.lower()
        for canonical, variants in FACT_TRIGGER_SYNONYMS.items():
            if canonical == "infers_causality":
                # Phrase-level matching for multi-word variants
                if any(phrase in lowered_sentence for phrase in variants):
                    triggers.append(canonical)
            else:
                # Word-level matching (existing logic)
                if tokens & variants:
                    triggers.append(canonical)
        return sorted(triggers)

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        return [part.strip() for part in SENTENCE_SPLIT_PATTERN.split(text or "") if part and part.strip()]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {token.lower() for token in WORD_PATTERN.findall(text or "") if len(token) > 1}

    @staticmethod
    def _normalize_unit(unit: str) -> str:
        return unit.replace(" ", "").replace("uL", "ul").replace("碌", "u").replace("渭", "u").lower()

    @staticmethod
    def _tolerance(unit: str) -> float:
        if unit in {"ev", "mev"}:
            return 1e-3 if unit == "ev" else 1.0
        if unit in {"mah/g", "ah/kg"}:
            return 1e-2
        if unit in {"mg/cm2", "wt%", "ul/mg", "%/cycle"}:
            return 1e-3
        if unit == "c":
            return 1e-6
        return 1e-6
