from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ElementProperty


ELEMENT_PROPERTY_DATA_SOURCE = "literature_ai_builtin_pubchem_mendeleev_seed"
ELEMENT_PROPERTY_DATA_VERSION = "periodic_table_118_v1"
ELEMENT_PROPERTY_LICENSE = "PubChem public domain metadata; mendeleev-data MIT-compatible schema"
ELEMENT_PROPERTY_SOURCE_URL = "https://pubchem.ncbi.nlm.nih.gov/docs/elements"


@dataclass(frozen=True)
class ElementSeed:
    atomic_number: int
    symbol: str
    name: str
    electronegativity_pauling: float | None = None
    valence_electron_count: int | None = None


_ELEMENTS: tuple[tuple[int, str, str], ...] = (
    (1, "H", "Hydrogen"),
    (2, "He", "Helium"),
    (3, "Li", "Lithium"),
    (4, "Be", "Beryllium"),
    (5, "B", "Boron"),
    (6, "C", "Carbon"),
    (7, "N", "Nitrogen"),
    (8, "O", "Oxygen"),
    (9, "F", "Fluorine"),
    (10, "Ne", "Neon"),
    (11, "Na", "Sodium"),
    (12, "Mg", "Magnesium"),
    (13, "Al", "Aluminium"),
    (14, "Si", "Silicon"),
    (15, "P", "Phosphorus"),
    (16, "S", "Sulfur"),
    (17, "Cl", "Chlorine"),
    (18, "Ar", "Argon"),
    (19, "K", "Potassium"),
    (20, "Ca", "Calcium"),
    (21, "Sc", "Scandium"),
    (22, "Ti", "Titanium"),
    (23, "V", "Vanadium"),
    (24, "Cr", "Chromium"),
    (25, "Mn", "Manganese"),
    (26, "Fe", "Iron"),
    (27, "Co", "Cobalt"),
    (28, "Ni", "Nickel"),
    (29, "Cu", "Copper"),
    (30, "Zn", "Zinc"),
    (31, "Ga", "Gallium"),
    (32, "Ge", "Germanium"),
    (33, "As", "Arsenic"),
    (34, "Se", "Selenium"),
    (35, "Br", "Bromine"),
    (36, "Kr", "Krypton"),
    (37, "Rb", "Rubidium"),
    (38, "Sr", "Strontium"),
    (39, "Y", "Yttrium"),
    (40, "Zr", "Zirconium"),
    (41, "Nb", "Niobium"),
    (42, "Mo", "Molybdenum"),
    (43, "Tc", "Technetium"),
    (44, "Ru", "Ruthenium"),
    (45, "Rh", "Rhodium"),
    (46, "Pd", "Palladium"),
    (47, "Ag", "Silver"),
    (48, "Cd", "Cadmium"),
    (49, "In", "Indium"),
    (50, "Sn", "Tin"),
    (51, "Sb", "Antimony"),
    (52, "Te", "Tellurium"),
    (53, "I", "Iodine"),
    (54, "Xe", "Xenon"),
    (55, "Cs", "Caesium"),
    (56, "Ba", "Barium"),
    (57, "La", "Lanthanum"),
    (58, "Ce", "Cerium"),
    (59, "Pr", "Praseodymium"),
    (60, "Nd", "Neodymium"),
    (61, "Pm", "Promethium"),
    (62, "Sm", "Samarium"),
    (63, "Eu", "Europium"),
    (64, "Gd", "Gadolinium"),
    (65, "Tb", "Terbium"),
    (66, "Dy", "Dysprosium"),
    (67, "Ho", "Holmium"),
    (68, "Er", "Erbium"),
    (69, "Tm", "Thulium"),
    (70, "Yb", "Ytterbium"),
    (71, "Lu", "Lutetium"),
    (72, "Hf", "Hafnium"),
    (73, "Ta", "Tantalum"),
    (74, "W", "Tungsten"),
    (75, "Re", "Rhenium"),
    (76, "Os", "Osmium"),
    (77, "Ir", "Iridium"),
    (78, "Pt", "Platinum"),
    (79, "Au", "Gold"),
    (80, "Hg", "Mercury"),
    (81, "Tl", "Thallium"),
    (82, "Pb", "Lead"),
    (83, "Bi", "Bismuth"),
    (84, "Po", "Polonium"),
    (85, "At", "Astatine"),
    (86, "Rn", "Radon"),
    (87, "Fr", "Francium"),
    (88, "Ra", "Radium"),
    (89, "Ac", "Actinium"),
    (90, "Th", "Thorium"),
    (91, "Pa", "Protactinium"),
    (92, "U", "Uranium"),
    (93, "Np", "Neptunium"),
    (94, "Pu", "Plutonium"),
    (95, "Am", "Americium"),
    (96, "Cm", "Curium"),
    (97, "Bk", "Berkelium"),
    (98, "Cf", "Californium"),
    (99, "Es", "Einsteinium"),
    (100, "Fm", "Fermium"),
    (101, "Md", "Mendelevium"),
    (102, "No", "Nobelium"),
    (103, "Lr", "Lawrencium"),
    (104, "Rf", "Rutherfordium"),
    (105, "Db", "Dubnium"),
    (106, "Sg", "Seaborgium"),
    (107, "Bh", "Bohrium"),
    (108, "Hs", "Hassium"),
    (109, "Mt", "Meitnerium"),
    (110, "Ds", "Darmstadtium"),
    (111, "Rg", "Roentgenium"),
    (112, "Cn", "Copernicium"),
    (113, "Nh", "Nihonium"),
    (114, "Fl", "Flerovium"),
    (115, "Mc", "Moscovium"),
    (116, "Lv", "Livermorium"),
    (117, "Ts", "Tennessine"),
    (118, "Og", "Oganesson"),
)

_PAULING_EN: dict[str, float] = {
    "Sc": 1.36,
    "Ti": 1.54,
    "V": 1.63,
    "Cr": 1.66,
    "Mn": 1.55,
    "Fe": 1.83,
    "Co": 1.88,
    "Ni": 1.91,
    "Cu": 1.90,
    "Zn": 1.65,
    "Ge": 2.01,
    "Y": 1.22,
    "Zr": 1.33,
    "Nb": 1.60,
    "Mo": 2.16,
    "Tc": 1.90,
    "Ru": 2.20,
    "Rh": 2.28,
    "Pd": 2.20,
    "Ag": 1.93,
    "Hf": 1.30,
    "W": 2.36,
    "Ir": 2.20,
    "Pt": 2.28,
    "Au": 2.54,
}

_VALENCE_ELECTRONS: dict[str, int] = {
    "Sc": 3,
    "Ti": 4,
    "V": 5,
    "Cr": 6,
    "Mn": 7,
    "Fe": 8,
    "Co": 9,
    "Ni": 10,
    "Cu": 11,
    "Zn": 12,
    "Y": 3,
    "Zr": 4,
    "Nb": 5,
    "Mo": 6,
    "Tc": 7,
    "Ru": 8,
    "Rh": 9,
    "Pd": 10,
    "Ag": 11,
    "Hf": 4,
    "W": 6,
    "Ir": 9,
    "Pt": 10,
    "Au": 11,
}


def builtin_element_property_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for atomic_number, symbol, name in _ELEMENTS:
        row = {
            "symbol": symbol,
            "name": name,
            "atomic_number": atomic_number,
            "electronegativity_pauling": _PAULING_EN.get(symbol),
            "valence_electron_count": _VALENCE_ELECTRONS.get(symbol),
            "data_source": ELEMENT_PROPERTY_DATA_SOURCE,
            "data_version": ELEMENT_PROPERTY_DATA_VERSION,
            "license": ELEMENT_PROPERTY_LICENSE,
            "source_url": ELEMENT_PROPERTY_SOURCE_URL,
        }
        row["source_snapshot_hash"] = hashlib.sha256(
            json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        rows.append(row)
    return rows


class ElementPropertyImportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def import_builtin_snapshot(self, *, dry_run: bool = False) -> dict[str, Any]:
        rows = builtin_element_property_rows()
        existing = {
            row.symbol: row
            for row in self.session.scalars(select(ElementProperty)).all()
        }
        inserted = 0
        updated = 0
        for item in rows:
            row = existing.get(item["symbol"])
            if row is None:
                inserted += 1
                if not dry_run:
                    self.session.add(ElementProperty(**item))
                continue
            changed = False
            for key, value in item.items():
                if getattr(row, key) != value:
                    changed = True
                    if not dry_run:
                        setattr(row, key, value)
            updated += 1 if changed else 0
        if not dry_run:
            self.session.flush()
        return {
            "data_source": ELEMENT_PROPERTY_DATA_SOURCE,
            "data_version": ELEMENT_PROPERTY_DATA_VERSION,
            "row_count": len(rows),
            "inserted_count": inserted,
            "updated_count": updated,
            "dry_run": dry_run,
        }
