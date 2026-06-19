from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable


@dataclass(frozen=True)
class BoundaryCleanupPlan:
    """Exact line signatures confirmed from repeated PDF page boundaries."""

    repeated_signatures: frozenset[str] = field(default_factory=frozenset)
    page_number_signatures: frozenset[str] = field(default_factory=frozenset)

    @property
    def removable_signatures(self) -> frozenset[str]:
        return self.repeated_signatures | self.page_number_signatures

    def to_metadata(self) -> dict[str, list[str]]:
        return {
            "repeated_signatures": sorted(self.repeated_signatures),
            "page_number_signatures": sorted(self.page_number_signatures),
        }

    @classmethod
    def from_metadata(cls, value: Any) -> "BoundaryCleanupPlan":
        if not isinstance(value, dict):
            return cls()
        return cls(
            repeated_signatures=frozenset(str(item) for item in value.get("repeated_signatures") or []),
            page_number_signatures=frozenset(str(item) for item in value.get("page_number_signatures") or []),
        )


class BodyBoundaryCleaner:
    """Conservatively remove page-boundary boilerplate by exact line match."""

    BOUNDARY_LINE_LIMIT = 3
    MAX_BOUNDARY_LINE_CHARS = 160

    @classmethod
    def analyze(cls, page_blocks: Iterable[dict[str, Any]]) -> BoundaryCleanupPlan:
        blocks = list(page_blocks)
        if len(blocks) < 2:
            return BoundaryCleanupPlan()

        by_zone: dict[tuple[str, str], set[int]] = {}
        page_number_candidates: list[tuple[int, str, int]] = []
        for page_index, block in enumerate(blocks):
            lines = [(index, line.strip()) for index, line in enumerate((block.get("text") or "").splitlines()) if line.strip()]
            top_indexes = {index for index, _ in lines[: cls.BOUNDARY_LINE_LIMIT]}
            bottom_indexes = {index for index, _ in lines[-cls.BOUNDARY_LINE_LIMIT :]}
            for index, line in lines:
                if len(line) > cls.MAX_BOUNDARY_LINE_CHARS:
                    continue
                zones = []
                if index in top_indexes:
                    zones.append("top")
                if index in bottom_indexes:
                    zones.append("bottom")
                if not zones:
                    continue
                signature = cls.signature(line)
                for zone in zones:
                    by_zone.setdefault((zone, signature), set()).add(page_index)
                number = cls._page_number(line)
                if number is not None:
                    page_number_candidates.append((page_index, signature, number))

        minimum_pages = max(2, (len(blocks) + 1) // 2)
        repeated = {
            signature
            for (_zone, signature), pages in by_zone.items()
            if signature and len(pages) >= minimum_pages and cls._page_number(signature) is None
        }

        page_numbers: set[str] = set()
        ordered_candidates = sorted(page_number_candidates)
        if len({page for page, _, _ in ordered_candidates}) >= 2:
            values_by_page: dict[int, int] = {}
            signatures_by_page: dict[int, str] = {}
            for page, signature, number in ordered_candidates:
                values_by_page.setdefault(page, number)
                signatures_by_page.setdefault(page, signature)
            ordered_values = [values_by_page[page] for page in sorted(values_by_page)]
            if len(ordered_values) >= 2 and all(right == left + 1 for left, right in zip(ordered_values, ordered_values[1:])):
                page_numbers.update(signatures_by_page.values())

        return BoundaryCleanupPlan(
            repeated_signatures=frozenset(repeated),
            page_number_signatures=frozenset(page_numbers),
        )

    @classmethod
    def clean_page_blocks(
        cls,
        page_blocks: Iterable[dict[str, Any]],
        plan: BoundaryCleanupPlan,
    ) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for block in page_blocks:
            updated = dict(block)
            updated["text"] = cls.clean_text(block.get("text") or "", plan)
            cleaned.append(updated)
        return cleaned

    @classmethod
    def clean_text(cls, text: str, plan: BoundaryCleanupPlan) -> str:
        if not text or not plan.removable_signatures:
            return text.strip()
        kept = [line for line in text.splitlines() if cls.signature(line) not in plan.removable_signatures]
        return "\n".join(kept).strip()

    @classmethod
    def clean_sections(
        cls,
        sections: Iterable[dict[str, Any]],
        plan: BoundaryCleanupPlan,
    ) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for section in sections:
            updated = dict(section)
            updated["text"] = cls.clean_text(str(section.get("text") or ""), plan)
            if updated["text"]:
                cleaned.append(updated)
        return cleaned

    @staticmethod
    def signature(line: str) -> str:
        value = re.sub(r"^\s{0,3}#{1,6}\s+", "", str(line or "").strip())
        return re.sub(r"\s+", " ", value).strip().casefold()

    @classmethod
    def _page_number(cls, line: str) -> int | None:
        value = cls.signature(line)
        match = re.fullmatch(r"(?:page\s+)?(\d{1,4})", value, re.IGNORECASE)
        if not match:
            match = re.fullmatch(r"[-–—\s]*([0-9]{1,4})[-–—\s]*", value)
        return int(match.group(1)) if match else None
