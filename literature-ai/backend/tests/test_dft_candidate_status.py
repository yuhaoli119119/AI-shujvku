import pytest

from app.utils.dft_candidate_status import is_status_ready, is_terminal, normalize


@pytest.mark.parametrize(
    ("raw_status", "normalized"),
    [
        (" ML_Ready ", "ml_ready"),
        ("AI_Verified_ML_Ready", "ai_verified_ml_ready"),
        ("Rejected", "rejected"),
        ("human_reviewed_needs_evidence", "human_reviewed_needs_evidence"),
    ],
)
def test_normalize_is_case_insensitive(raw_status, normalized):
    assert normalize(raw_status) == normalized


@pytest.mark.parametrize(
    "status",
    [
        "ML_Ready",
        "AI_Verified_ML_Ready",
        "Rejected",
        "AI_Rejected",
        "REJECTED_BY_LOCAL_AI",
        "human_reviewed_needs_evidence",
        "Gemini_Verified",
        "Human_Confirmed",
        "Citation_Ready",
        "verified",
        "human_verified",
    ],
)
def test_terminal_statuses_are_case_insensitive(status):
    assert is_terminal(status) is True


def test_human_reviewed_needs_evidence_is_terminal_but_not_ready():
    assert is_terminal("Human_Reviewed_Needs_Evidence") is True
    assert is_status_ready("Human_Reviewed_Needs_Evidence") is False


def test_system_candidate_remains_active_and_not_ready():
    assert is_terminal("system_candidate") is False
    assert is_status_ready("system_candidate") is False
