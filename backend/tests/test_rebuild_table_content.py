from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.rebuild_table_content import table_content, validate_table_provenance

pytestmark = pytest.mark.no_test_database


def asset(**kwargs):
    return SimpleNamespace(asset_type="table", file_id=uuid4(), page_numbers=[71, 72],
                           provenance=kwargs.get("provenance", {}), context_text=kwargs.get("context_text"))


def test_lossless_table_transport_and_cross_page_pointers():
    record = asset(context_text=" s  1.00s\nmissing — ")
    assert table_content(record)["text"] == record.context_text
    record.provenance = {"table_data": {"columns": ["s", "value"], "rows": [["1.00s", None], ["s", "0.0100"]]},
                         "table_sources": [{"file_id": str(record.file_id), "page_number": 71, "role": "caption", "locator": "S13 caption"},
                                           {"file_id": str(record.file_id), "page_number": 72, "role": "body", "locator": "S13 body"}]}
    validate_table_provenance(record.provenance, file_id=record.file_id, page_numbers=record.page_numbers)
    result = table_content(record)
    assert result["format"] == "structured"
    assert result["data"]["rows"] == [["1.00s", None], ["s", "0.0100"]]
    assert result["sources"] == record.provenance["table_sources"]


@pytest.mark.parametrize("provenance,error", [
    ({"table_data": {"columns": ["t"], "rows": [[1.0]]}}, "string_type"),
    ({"table_data": {"columns": ["t"], "rows": [["a", "b"]]}}, "table_row_width_mismatch"),
    ({"table_markdown": 12}, "table_markdown_must_be_text"),
    ({"table_markdown": "original"}, "table_body_sources_required"),
])
def test_reject_invalid_new_body_without_guessing(provenance, error):
    with pytest.raises(ValueError, match=error):
        validate_table_provenance(provenance, file_id=uuid4(), page_numbers=[71, 72])


def test_legacy_invalid_structured_remains_readable_and_wrong_source_rejected():
    record = asset(provenance={"table_data": {"rows": [[1.0]]}, "table_markdown": "|s|1.00s|"}, context_text="fallback")
    assert table_content(record)["format"] == "markdown"
    record.provenance = {"table_markdown": "body", "table_sources": [{"file_id": str(uuid4()), "page_number": 72, "locator": "top"}]}
    with pytest.raises(ValueError, match="table_source_wrong_file"):
        validate_table_provenance(record.provenance, file_id=record.file_id, page_numbers=[71, 72])
    record.provenance["table_sources"][0]["file_id"] = str(record.file_id)
    with pytest.raises(ValueError, match="table_source_page_not_in_asset"):
        validate_table_provenance(record.provenance, file_id=record.file_id, page_numbers=[71])
