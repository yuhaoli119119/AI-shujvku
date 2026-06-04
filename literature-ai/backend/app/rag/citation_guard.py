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
CLAIM_STATUSES = {
    "verified",
    "numerical_error",
    "unverified",
    "hallucination",
    "misleading",
    "citation_not_found",
    "not_in_source",
    "citation_mismatch",
}
CITATION_PATTERN = re.compile(r"\[(?:\d+(?:\s*,\s*\d+)*)\]|\([A-Z][A-Za-z-]+(?:\s+et\s+al\.)?,\s*\d{4}\)")
COMPARATIVE_PATTERN = re.compile(r"\b(better|higher|lower|superior|strongest|fastest|highest|lowest|outperform\w*)\b", re.IGNORECASE)
ATTRIBUTION_PATTERN = re.compile(r"\b(reported|proposed|proved|proven|discovered|found|demonstrated|showed|according to)\b", re.IGNORECASE)
CONSENSUS_PATTERN = re.compile(r"\b(widely accepted|generally recognized|most studies|consensus|well established)\b", re.IGNORECASE)
MECHANISM_PATTERN = re.compile(r"\b(pathway|mechanism|adsorption|conversion|cataly\w*|intermediate|reaction route)\b", re.IGNORECASE)


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
        claim_audit = self.audit_claims(text, facts)
        high_risk_failures = [
            item
            for item in claim_audit
            if item["status"] != "verified"
            and item.get("claim_type")
            in {
                "numerical_claim",
                "comparative_claim",
                "causal_claim",
                "attribution_claim",
                "consensus_claim",
                "mechanism_claim",
            }
        ]
        return {
            "ok": not missing and not missing_textual,
            "supported_values": supported,
            "missing_values": missing,
            "supported_fact_claims": supported_textual,
            "missing_fact_claims": missing_textual,
            "checked_count": len(numeric_claims),
            "checked_fact_count": len(textual_claims),
            "claim_audit": claim_audit,
            "status_counts": self._status_counts(claim_audit),
            "guard_failure": bool(high_risk_failures),
            "guard_failure_claims": high_risk_failures,
        }

    def audit_claims(
        self,
        text: str,
        facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        fact_claims = self._collect_fact_claims(facts)
        evidence_items = self._collect_textual_evidence_items(facts, include_synthetic=True)
        audit: list[dict[str, Any]] = []

        for claim in self._extract_numeric_claims(text):
            supported = self._find_supporting_claim(claim, fact_claims)
            mismatch = self._find_numeric_mismatch(claim, fact_claims)
            if supported is not None:
                status = "verified"
                evidence = [supported]
                warning = None
            elif mismatch is not None:
                status = "numerical_error"
                evidence = [mismatch]
                warning = "Exact numerical value/unit does not match retrieved evidence"
            else:
                status = "unverified"
                evidence = []
                warning = "No retrieved evidence supports this numerical claim"
            audit.append(
                {
                    "claim_text": claim["literal"],
                    "claim_type": "numerical_claim",
                    "status": status,
                    "evidence": evidence,
                    "warning": warning,
                    "context": claim.get("context") or [],
                }
            )

        for claim in self._extract_textual_claims(text, include_synthetic=True):
            supported = self._find_supporting_textual_claim(claim, facts, include_synthetic=True)
            claim_text = claim["sentence"]
            if supported is not None:
                status = "verified"
                evidence = [supported]
                warning = None
            elif self._has_citation_marker(claim_text) and evidence_items:
                status = "citation_mismatch"
                evidence = []
                warning = "Citation or retrieved evidence exists, but it does not support the claim"
            elif self._has_citation_marker(claim_text):
                status = "citation_not_found"
                evidence = []
                warning = "Claim includes a citation marker but no matching retrieved source was available"
            elif evidence_items:
                status = "unverified"
                evidence = []
                warning = "Retrieved evidence does not support this claim"
            else:
                status = "not_in_source"
                evidence = []
                warning = "Doc-only evidence set is empty for this claim"
            audit.append(
                {
                    "claim_text": claim_text,
                    "claim_type": self._classify_textual_claim(claim_text, claim),
                    "status": status,
                    "evidence": evidence,
                    "warning": warning,
                    "triggers": claim.get("triggers") or [],
                    "context": claim.get("context") or [],
                }
            )
        return audit

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

    def _find_numeric_mismatch(self, claim: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any] | None:
        for fact in facts:
            if fact["unit"] != claim["unit"]:
                continue
            if not self._context_matches(claim, fact):
                continue
            if abs(fact["value"] - claim["value"]) > self._tolerance(claim["unit"]):
                return fact
        return None

    def _extract_textual_claims(self, text: str, include_synthetic: bool = False) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for sentence in self._split_sentences(text):
            tokens = self._tokenize(sentence)
            trigger_set = set(self._extract_fact_triggers(tokens, sentence=sentence))
            if include_synthetic:
                trigger_set |= self._synthetic_claim_triggers(sentence)
            triggers = sorted(trigger_set)
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

    def _classify_textual_claim(self, sentence: str, claim: dict[str, Any]) -> str:
        triggers = set(claim.get("triggers") or [])
        if COMPARATIVE_PATTERN.search(sentence) or triggers & {"comparative", "superior", "strengthens", "weakens"}:
            return "comparative_claim"
        if triggers & {"accelerates", "suppresses", "causes", "infers_causality"}:
            return "causal_claim"
        if ATTRIBUTION_PATTERN.search(sentence) or "attribution" in triggers:
            return "attribution_claim"
        if CONSENSUS_PATTERN.search(sentence) or "consensus" in triggers:
            return "consensus_claim"
        if MECHANISM_PATTERN.search(sentence) or triggers & {"mechanism", "mediates"}:
            return "mechanism_claim"
        return "factual_claim"

    def _find_supporting_textual_claim(
        self,
        claim: dict[str, Any],
        facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
        include_synthetic: bool = False,
    ) -> dict[str, Any] | None:
        evidence_items = self._collect_textual_evidence_items(facts, include_synthetic=include_synthetic)
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
        self,
        facts: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
        include_synthetic: bool = False,
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
            trigger_set = set(self._extract_fact_triggers(tokens, sentence=text))
            if include_synthetic:
                trigger_set |= self._synthetic_claim_triggers(text)
            triggers = sorted(trigger_set)
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
    def _synthetic_claim_triggers(sentence: str) -> set[str]:
        triggers: set[str] = set()
        if COMPARATIVE_PATTERN.search(sentence):
            triggers.add("comparative")
        if ATTRIBUTION_PATTERN.search(sentence):
            triggers.add("attribution")
        if CONSENSUS_PATTERN.search(sentence):
            triggers.add("consensus")
        if MECHANISM_PATTERN.search(sentence):
            triggers.add("mechanism")
        return triggers

    @staticmethod
    def _has_citation_marker(text: str) -> bool:
        return bool(CITATION_PATTERN.search(text or ""))

    @staticmethod
    def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
        counts = {status: 0 for status in sorted(CLAIM_STATUSES)}
        for item in items:
            status = str(item.get("status") or "unverified")
            counts[status] = counts.get(status, 0) + 1
        return counts

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
