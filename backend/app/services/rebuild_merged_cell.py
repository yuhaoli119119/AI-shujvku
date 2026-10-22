"""Audited JSON extension for sources; no ORM columns or production migration."""
from app.schemas.merged_cell import MergedCell, validate_cell_location


def stored_merged_cell(source):
    from sqlalchemy.orm import object_session
    from app.db.models import RebuildDataValue
    session = object_session(source)
    value = session.get(RebuildDataValue, source.value_id) if session else None
    return ((value.conflict or {}).get("source_merged_cells") or {}).get(str(source.id)) if value else None


def store_merged_cell(value, source, data):
    cells = dict((value.conflict or {}).get("source_merged_cells") or {})
    if data.get("merged_cell") is None:
        if str(source.id) not in cells:
            return
        cells.pop(str(source.id))
    else:
        cells[str(source.id)] = dict(data["merged_cell"])
    value.conflict = {**(value.conflict or {}), "source_merged_cells": cells}


def validate_source_cell(data, asset):
    if data.get("merged_cell") is None:
        return
    if data.get("source_kind") != "table" or asset is None or asset.asset_type != "table":
        raise ValueError("merged_cell_requires_table_asset")
    from app.services.rebuild_table_content import TableData
    raw = (asset.provenance or {}).get("table_data")
    if raw is None:
        raise ValueError("merged_cell_requires_structured_table_bounds")
    table = TableData.model_validate(raw)
    cell = validate_cell_location(data["merged_cell"], data.get("table_row"), data.get("table_column"),
                                 rows=len(table.rows), columns=len(table.columns))
    content = table.rows[data["table_row"]][data["table_column"]]
    if cell.cell_content == "blank" and content not in (None, ""):
        raise ValueError("merged_blank_cell_contains_value")
    if cell.cell_content == "printed" and content in (None, ""):
        raise ValueError("merged_printed_cell_is_blank")


def validate_merged_value(payload):
    for source in payload.get("sources") or []:
        cell = source.get("merged_cell")
        if cell is not None:
            cell = MergedCell.model_validate(cell)
        if cell is not None and cell.cell_content == "blank" and (
                payload.get("raw_value") is not None or payload.get("numeric_value") is not None):
            raise ValueError("merged_blank_cell_cannot_supply_value")
