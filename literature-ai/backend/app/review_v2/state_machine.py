VALID_STATES = {"in_progress", "completed", "completed_with_issues", "blocked", "stale", "not_required"}
def final_state(*, item_count: int, held: int, rejected: int, requested: str) -> str:
    if item_count == 0: return "not_required"
    if requested == "blocked": return "blocked"
    return "completed_with_issues" if held + rejected else "completed"
def assert_contract(stage_status: str, unresolved_count: int) -> None:
    if stage_status not in VALID_STATES: raise ValueError(f"invalid_stage_status:{stage_status}")
    if stage_status == "completed" and unresolved_count != 0: raise ValueError("completed_requires_unresolved_count_zero")
