from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    DFTAuditIssue,
    DFTAuditIssueSource,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
)
from app.migrations.dft_audit_issue_source_backfill_v1 import (
    DFTAuditIssueSourceBackfillError,
    analyze,
    upgrade,
)
from app.migrations.dft_identity_v2 import upgrade as install_dft_identity_v2


def _paper_and_run(session: Session, code: str) -> tuple[Paper, ExternalAnalysisRun]:
    paper = Paper(title=f"paper {code}", paper_code=code, pdf_path=f"{code}.pdf", authors=["A"])
    session.add(paper)
    session.flush()
    run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="legacy-source-test")
    session.add(run)
    session.flush()
    return paper, run


def _candidate(
    session: Session,
    paper: Paper,
    run: ExternalAnalysisRun,
) -> ExternalAnalysisCandidate:
    row = ExternalAnalysisCandidate(
        paper_id=paper.id,
        run_id=run.id,
        candidate_type="object_review_audit",
        normalized_payload={"value": str(uuid4())},
        status="materialized",
    )
    session.add(row)
    session.flush()
    return row


def _issue(
    session: Session,
    paper: Paper,
    source_candidate_ids: list[object],
    *,
    parent_issue_id: UUID | None = None,
) -> DFTAuditIssue:
    row = DFTAuditIssue(
        paper_id=paper.id,
        target_type="dft_results",
        target_id="new",
        issue_type="missing_dft_result",
        severity="high",
        status="closed",
        source_candidate_ids=source_candidate_ids,
        fingerprint=uuid4().hex,
        parent_issue_id=parent_issue_id,
    )
    session.add(row)
    session.flush()
    return row


def test_real_upgrade_order_backfills_multiple_sources_and_preserves_json(setup_test_db):
    with setup_test_db.begin() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        assert str(schema).startswith("pytest_")
        connection.execute(text(f'DROP TABLE "{schema}".dft_audit_issue_sources'))
    with Session(setup_test_db) as session:
        paper, run = _paper_and_run(session, "B9101")
        candidates = [_candidate(session, paper, run), _candidate(session, paper, run)]
        original = [str(row.id) for row in candidates]
        issue = _issue(session, paper, original)
        paper_id, issue_id = paper.id, issue.id
        session.commit()

    with setup_test_db.begin() as connection:
        install_dft_identity_v2(connection)
        report = upgrade(connection, paper_id=paper_id)

    assert report["migration_version"] == "007_dft_audit_issue_source_backfill_v1"
    assert report["expected_source_relations"] == 2
    assert report["inserted_relations"] == 2
    with Session(setup_test_db) as session:
        assert session.get(DFTAuditIssue, issue_id).source_candidate_ids == original
        assert session.query(DFTAuditIssueSource).count() == 2


def test_identity_split_parent_and_children_are_all_backfilled(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_and_run(session, "B9102")
        first = _candidate(session, paper, run)
        second = _candidate(session, paper, run)
        parent = _issue(session, paper, [str(first.id), str(second.id)])
        first_child = _issue(session, paper, [str(first.id)], parent_issue_id=parent.id)
        second_child = _issue(session, paper, [str(second.id)], parent_issue_id=parent.id)
        paper_id = paper.id
        expected = {
            (parent.id, first.id),
            (parent.id, second.id),
            (first_child.id, first.id),
            (second_child.id, second.id),
        }
        session.commit()

    with setup_test_db.begin() as connection:
        report = upgrade(connection, paper_id=paper_id)
    assert report["inserted_relations"] == 4
    with Session(setup_test_db) as session:
        actual = set(session.execute(select(
            DFTAuditIssueSource.issue_id,
            DFTAuditIssueSource.candidate_id,
        )).all())
    assert actual == expected


def test_partial_relations_only_fill_missing_and_second_apply_is_zero_write(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_and_run(session, "B9103")
        first = _candidate(session, paper, run)
        second = _candidate(session, paper, run)
        issue = _issue(session, paper, [str(first.id), str(second.id)])
        session.add(DFTAuditIssueSource(issue_id=issue.id, candidate_id=first.id))
        other_paper, other_run = _paper_and_run(session, "B9104")
        other_candidate = _candidate(session, other_paper, other_run)
        _issue(session, other_paper, [str(other_candidate.id)])
        paper_id = paper.id
        other_paper_id = other_paper.id
        session.commit()

    with setup_test_db.begin() as connection:
        first_report = upgrade(connection, paper_id=paper_id)
    with setup_test_db.begin() as connection:
        second_report = upgrade(connection, paper_id=paper_id)

    assert first_report["existing_relations"] == 2
    assert first_report["inserted_relations"] == 1
    assert second_report["inserted_relations"] == 0
    assert second_report["database_writes"] == 0
    with Session(setup_test_db) as session:
        assert session.query(DFTAuditIssueSource).count() == 2
        assert session.scalar(
            select(DFTAuditIssueSource).join(DFTAuditIssue).where(
                DFTAuditIssue.paper_id == other_paper_id
            )
        ) is None


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("invalid_uuid", "invalid_uuid"),
        ("missing", "candidate_not_found"),
        ("cross_paper", "cross_paper_candidate"),
    ],
)
def test_invalid_references_are_rejected_with_structured_report(
    setup_test_db,
    case,
    reason,
):
    with Session(setup_test_db) as session:
        paper, run = _paper_and_run(session, f"B92{case[:2]}")
        if case == "invalid_uuid":
            source_ids = ["not-a-uuid"]
        elif case == "missing":
            source_ids = [str(uuid4())]
        else:
            other_paper, other_run = _paper_and_run(session, f"B93{case[:2]}")
            source_ids = [str(_candidate(session, other_paper, other_run).id)]
        issue = _issue(session, paper, source_ids)
        original = deepcopy(issue.source_candidate_ids)
        paper_id, issue_id = paper.id, issue.id
        session.commit()

    with setup_test_db.begin() as connection:
        dry_run = analyze(connection, paper_id=paper_id)
    assert dry_run["status"] == "blocked"
    assert len(dry_run["errors"]) == 1
    assert dry_run["errors"][0]["issue_id"] == str(issue_id)
    assert dry_run["errors"][0]["reason"] == reason
    with pytest.raises(DFTAuditIssueSourceBackfillError) as caught:
        with setup_test_db.begin() as connection:
            upgrade(connection, paper_id=paper_id)
    assert caught.value.report["errors"][0]["reason"] == reason
    with Session(setup_test_db) as session:
        assert session.query(DFTAuditIssueSource).count() == 0
        assert session.get(DFTAuditIssue, issue_id).source_candidate_ids == original


def test_global_error_blocks_all_writes_but_valid_paper_can_be_isolated(setup_test_db):
    with Session(setup_test_db) as session:
        valid_paper, valid_run = _paper_and_run(session, "B9105")
        valid_candidate = _candidate(session, valid_paper, valid_run)
        _issue(session, valid_paper, [str(valid_candidate.id)])
        invalid_paper, _invalid_run = _paper_and_run(session, "B9106")
        _issue(session, invalid_paper, [str(uuid4())])
        valid_paper_id = valid_paper.id
        session.commit()

    with setup_test_db.begin() as connection:
        blocked = upgrade(connection, block_on_errors=False)
    assert blocked["status"] == "blocked"
    assert blocked["database_writes"] == 0
    with Session(setup_test_db) as session:
        assert session.query(DFTAuditIssueSource).count() == 0

    with setup_test_db.begin() as connection:
        isolated = upgrade(connection, paper_id=valid_paper_id)
    assert isolated["inserted_relations"] == 1
    with Session(setup_test_db) as session:
        assert session.query(DFTAuditIssueSource).count() == 1


def test_injected_failure_rolls_back_every_relation(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_and_run(session, "B9107")
        candidates = [_candidate(session, paper, run), _candidate(session, paper, run)]
        _issue(session, paper, [str(row.id) for row in candidates])
        paper_id = paper.id
        session.commit()

    with pytest.raises(RuntimeError, match="injected_fault:after_source_relation_insert"):
        with setup_test_db.begin() as connection:
            upgrade(connection, paper_id=paper_id, fault_after_insert=True)
    with Session(setup_test_db) as session:
        assert session.query(DFTAuditIssueSource).count() == 0
