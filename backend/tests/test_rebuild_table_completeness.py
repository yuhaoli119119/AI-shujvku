"""HTTP/physical PDF/real PostgreSQL fixtures; never production science writes."""
import hashlib
from uuid import uuid4

import fitz
import pytest
from sqlalchemy import select

from app.db.models import Paper, PaperRelationship, PaperTable, RebuildPaperFile, RebuildVisualAsset
from test_rebuild_mutation_audit import audit_env, assets
from test_rebuild_acceptance import base, freeze_body, review_body

MD = "| Object | Time |\n| --- | --- |\n| s | 1.00s |\n| missing | |"


def add_asset(env, label="1", pages=None, file_id=None, context="original text 1.00s"):
    response = env["client"].post(assets(env), json={"asset_key": "table-" + uuid4().hex,
        "asset_type": "table", "file_id": str(file_id or env["file"]), "figure_label": label,
        "caption": "Table " + label + ". Original caption", "page_numbers": pages or [1], "context_text": context})
    assert response.status_code == 200, response.text
    return response.json()["asset"]


def legacy_main(env, *, count=1):
    with env["factory"]() as session:
        session.get(RebuildPaperFile, env["file"]).role = "main"
        records = [PaperTable(paper_id=env["paper"], caption=f"Table {index}. Original caption",
                              page=index, markdown_content=MD) for index in range(1, count + 1)]
        session.add_all(records); session.commit()
        return [row.id for row in records]


def freeze_result(env, request="freeze-1"):
    payload = freeze_body(env); payload["request_id"] = request
    response = env["client"].post(base(env) + "/freeze", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_context_only_is_saved_but_partial_and_independent_accept_is_blocked(audit_env):
    env = audit_env
    for index in range(1, 19):
        add_asset(env, label=f"S{index}")
    frozen = freeze_result(env)
    assert frozen["status"] == "partial"
    issues = [issue for issue in frozen["progress"]["rules"]["issues"] if issue["code"] == "table_representation_incomplete"]
    assert len(issues) == 18 and all(issue["format"] == "text" for issue in issues)
    with env["factory"]() as session:
        assert all(row.context_text == "original text 1.00s" for row in session.scalars(select(RebuildVisualAsset)))
        assert not session.scalars(select(PaperTable)).all()
    other = env["client_for"]("other")
    try:
        response = other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen))
        assert response.status_code == 409 and response.json()["detail"] == "acceptance_gates_not_satisfied"
    finally:
        other.close()


def test_b006_style_legacy_markdown_passes_and_full_consumed_rows_are_frozen(audit_env):
    env = audit_env; legacy_ids = legacy_main(env, count=2)
    add_asset(env, "1", [1]); add_asset(env, "2", [2])
    frozen = freeze_result(env)
    assert frozen["status"] == "rules_checked"
    assert {entry["id"] for entry in frozen["output_manifest"] if entry["kind"] == "paper_tables"} == set(map(str, legacy_ids))
    other = env["client_for"]("other")
    try:
        response = other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen))
        assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    finally:
        other.close()
    with env["factory"]() as session:
        row = session.get(PaperTable, legacy_ids[0]); row.markdown_content = MD.replace("1.00s", "2.00s"); session.commit()
    status = env["client"].get(base(env)).json()["acceptance"]
    assert status["stale"] and status["status"] == "review_incomplete"


@pytest.mark.parametrize("mismatch", ["other_file", "other_paper", "page", "label", "physical_pdf", "ambiguous"])
def test_legacy_matching_never_borrows_wrong_identity(audit_env, mismatch):
    env = audit_env; ids = legacy_main(env)
    file_id = env["file"]
    with env["factory"]() as session:
        row = session.get(PaperTable, ids[0])
        if mismatch == "page": row.page = 2
        elif mismatch == "label": row.caption = "Table 2. Other table"
        elif mismatch == "ambiguous":
            session.add(PaperTable(paper_id=env["paper"], caption=row.caption, page=1, markdown_content=MD))
        else:
            path = env["settings"].storage_root / "different.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((30, 40), "Different physical source")
                document.save(path)
            if mismatch == "physical_pdf": session.get(Paper, env["paper"]).pdf_path = "different.pdf"
            elif mismatch == "other_file":
                file = RebuildPaperFile(paper_id=env["paper"], role="main", storage_path="different.pdf",
                    original_filename="different.pdf", sha256=hashlib.sha256(path.read_bytes()).hexdigest(), file_size=path.stat().st_size)
                session.add(file); session.flush(); file_id = file.id
            else:
                other = Paper(title="Unrelated paper", library_name="默认文献库", pdf_path="fixture.pdf")
                session.add(other); session.flush(); row.paper_id = other.id
        session.commit()
    add_asset(env, file_id=file_id)
    if mismatch == "other_file":
        # Two legacy mains have no declared preferred source. Neither UUID nor
        # insertion order can authorize borrowing one edition's legacy tables.
        state = env["client"].get(f'/api/rebuild/papers/{env["paper"]}/files').json()
        assert state["ambiguous_main"] and state["preferred_main_id"] is None
        assert state["active_file_ids"] == []
        response = env["client"].post(base(env) + "/freeze", json=freeze_body(env))
        assert response.status_code == 400, response.text
        with env["factory"]() as session:
            from app.services.rebuild_table_reading import table_representations
            readings = table_representations(session, env["paper"], env["settings"])
            assert readings["tables"] and not any(table["complete"] for table in readings["tables"])
        return
    else: frozen = freeze_result(env)
    assert frozen["status"] == "partial"
    assert any(issue["code"] == "table_representation_incomplete" for issue in frozen["progress"]["rules"]["issues"])


def test_linked_si_requires_explicit_scope_same_pdf_and_freezes_relation(audit_env):
    env = audit_env
    with env["factory"]() as session:
        child = Paper(title="Linked SI", library_name="默认文献库", pdf_path="fixture.pdf")
        session.add(child); session.flush()
        session.add_all([PaperTable(paper_id=child.id, caption="Table S13. Original caption", page=page,
                                   markdown_content=MD) for page in [1, 2]])
        child_id = child.id; session.commit()
    add_asset(env, "S13", [1, 2])
    assert freeze_result(env)["status"] == "partial"  # Same bytes alone never authorize another paper.
    with env["factory"]() as session:
        link = PaperRelationship(source_paper_id=env["paper"], target_paper_id=child_id, relationship_type="supplementary")
        session.add(link); session.commit(); link_id = link.id
    frozen = freeze_result(env, "legal-si")
    assert frozen["status"] == "rules_checked"
    assert len([entry for entry in frozen["output_manifest"] if entry["kind"] == "paper_tables"]) == 2
    with env["factory"]() as session:
        session.get(PaperRelationship, link_id).relationship_type = "reference"; session.commit()
    assert env["client"].get(base(env)).json()["acceptance"]["stale"]


def test_remaining_legacy_without_assets_is_bound_and_its_mutation_invalidates_freeze(audit_env):
    env = audit_env; ids = legacy_main(env)
    frozen = freeze_result(env)
    assert frozen["status"] == "rules_checked"
    assert any(entry["kind"] == "paper_tables" for entry in frozen["output_manifest"])
    with env["factory"]() as session:
        session.get(PaperTable, ids[0]).caption = "Table 1. Changed original caption"; session.commit()
    assert env["client"].get(base(env)).json()["acceptance"]["stale"]
    other = env["client_for"]("other")
    try:
        result = other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen))
        assert result.status_code == 409 and result.json()["detail"] == "acceptance_frozen_revision_changed"
    finally:
        other.close()


@pytest.mark.parametrize("body,expected", [
    ({"table_data": {"columns": ["Object", "Time"], "rows": [["s", "1.00s"], ["missing", None]]}}, "rules_checked"),
    ({"table_markdown": MD}, "rules_checked"),
    ({"table_markdown": "original prose 1.00s, not a table"}, "partial"),
    ({"table_markdown": "| A | B |\n| --- | --- |"}, "partial"),
    ({"table_data": {"columns": ["A", "B"], "rows": []}}, "partial"),
])
def test_formal_representation_is_a_real_shape_gate_without_inventing_cells(audit_env, body, expected):
    env = audit_env
    # Existing valid legacy must not mask a malformed higher-priority body.
    ids = legacy_main(env)
    if expected == "rules_checked":
        # A malformed historical body must not demote the formal representation
        # which actually owns the current card's body.
        with env["factory"]() as session:
            session.get(PaperTable, ids[0]).markdown_content = "old unstructured body"; session.commit()
    provenance = {**body, "table_sources": [
        {"file_id": str(env["file"]), "page_number": 1, "role": "caption", "locator": "caption"},
        {"file_id": str(env["file"]), "page_number": 1, "role": "body", "locator": "body"}]}
    response = env["client"].post(assets(env), json={"asset_key": "formal", "asset_type": "table",
        "file_id": str(env["file"]), "figure_label": "1", "caption": "Table 1. Original caption",
        "page_numbers": [1], "context_text": "unchanged raw 1.00s", "provenance": provenance})
    assert response.status_code == 200, response.text
    assert freeze_result(env)["status"] == expected


@pytest.mark.parametrize("formal", ["structured", "markdown"])
@pytest.mark.parametrize("damage", ["missing_file", "foreign_file", "empty_pages", "negative_page", "boolean_page", "string_page", "scalar_pages", "mapping_pages", "outside_pdf"])
def test_formal_shape_cannot_bypass_its_own_source_identity_or_page_bounds(audit_env, formal, damage):
    env = audit_env
    with env["factory"]() as session:
        file_id = env["file"]
        pages = [1]
        if damage == "missing_file": file_id = None
        elif damage == "foreign_file":
            other = Paper(title="Other owner", library_name="默认文献库", pdf_path="fixture.pdf")
            session.add(other); session.flush()
            own = session.get(RebuildPaperFile, env["file"])
            foreign = RebuildPaperFile(paper_id=other.id, role="main", storage_path=own.storage_path,
                original_filename=own.original_filename, sha256=own.sha256, file_size=own.file_size)
            session.add(foreign); session.flush(); file_id = foreign.id
        else:
            pages = {"empty_pages": [], "negative_page": [-1], "boolean_page": [True],
                     "string_page": ["1"], "scalar_pages": 1, "mapping_pages": {"page": 1}, "outside_pdf": [3]}[damage]
        provenance = {"table_data": {"columns": ["A", "B"], "rows": [["s", "1.00s"]]}} if formal == "structured" else {"table_markdown": MD}
        # Deliberately model damaged/historical persisted records, bypassing
        # write validation in this isolated fixture. All own files are covered.
        asset = RebuildVisualAsset(paper_id=env["paper"], file_id=file_id, asset_key="damaged-formal",
            asset_type="table", figure_label=None, caption="Unnumbered table", page_numbers=pages,
            provenance=provenance, context_text="original unchanged")
        session.add(asset); session.commit(); asset_id = str(asset.id)
    frozen = freeze_result(env)
    assert frozen["status"] == "partial" and not frozen["progress"]["rules"]["passed"]
    issue = next(issue for issue in frozen["progress"]["rules"]["issues"] if issue.get("asset_id") == asset_id)
    assert issue["code"] == "table_representation_incomplete" and issue["reason"] == "table_source_unverified"
    assert issue["complete"] is False
    other = env["client_for"]("other")
    try:
        result = other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen))
        assert result.status_code == 409 and result.json()["detail"] == "acceptance_gates_not_satisfied"
    finally:
        other.close()


@pytest.mark.parametrize("formal", ["structured", "markdown"])
def test_valid_formal_source_does_not_require_a_numbered_label(audit_env, formal):
    env = audit_env
    provenance = {"table_data": {"columns": ["A", "B"], "rows": [["s", "1.00s"]]}} if formal == "structured" else {"table_markdown": MD}
    with env["factory"]() as session:
        session.add(RebuildVisualAsset(paper_id=env["paper"], file_id=env["file"], asset_key="unnumbered-formal",
            asset_type="table", caption="Unnumbered table", page_numbers=[1, 2], provenance=provenance))
        session.commit()
    frozen = freeze_result(env)
    assert frozen["status"] == "rules_checked"
    other = env["client_for"]("other")
    try:
        result = other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen))
        assert result.status_code == 200 and result.json()["status"] == "accepted", result.text
    finally:
        other.close()
