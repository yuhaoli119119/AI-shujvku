from datetime import datetime, timedelta

import pytest

from app.services.rebuild_progress_guard import initial_guard, observe, retry_guidance

pytestmark = pytest.mark.no_test_database
NOW = datetime(2026, 10, 17)
BASE = {"sha256": "a" * 64, "entities": 3}


def test_empty_completed_items_with_goal_active_stops_after_bounded_observations():
    state = initial_guard(BASE, now=NOW)
    native = {"thread_id": "review", "turn_id": "same-turn", "turn_status": "completed", "goal_status": "active", "item_count": 0}
    for index in range(3):
        state = observe(state, BASE, native, now=NOW + timedelta(seconds=60 * index))
    assert state["needs_attention"] and not state["automatic_retry"]
    assert state["empty_completed_observations"] == 3
    assert state["diagnostics"][-1]["root_cause"] == "undetermined"


def test_fast_poll_does_not_exhaust_budget_and_repeated_checkpoint_is_no_progress():
    state = initial_guard(BASE, now=NOW)
    native = {"turn_status": "completed", "goal_status": "active", "item_count": 0}
    for index in range(20):
        state = observe(state, BASE, native, now=NOW + timedelta(seconds=index))
    assert not state["needs_attention"] and state["empty_completed_observations"] == 1
    assert not state["business_advanced"]


def test_actual_new_evidence_resets_counters_but_counts_and_turn_completed_do_not():
    state = initial_guard(BASE, now=NOW)
    state = observe(state, BASE, {"turn_status": "completed", "goal_status": "complete", "item_count": 10}, now=NOW)
    assert not state["business_advanced"]
    advanced = {"sha256": "b" * 64, "entities": 3}  # new source at same entity count
    state = observe(state, advanced, {"turn_status": "completed", "item_count": 1}, now=NOW + timedelta(seconds=60))
    assert state["business_advanced"] and not state["needs_attention"]
    state = observe(state, BASE, {}, now=NOW + timedelta(seconds=16 * 60))
    assert state["stalled_observations"] == 1  # going back to an old checkpoint is not new


def test_stalled_nonempty_returns_are_bounded_and_stopped_is_sticky():
    state = initial_guard(BASE, now=NOW)
    for index in range(3):
        state = observe(state, BASE, {"turn_status": "completed", "item_count": 2}, now=NOW + timedelta(minutes=16 + index))
    assert state["needs_attention"]
    state = observe(state, {"sha256": "c" * 64, "entities": 4}, {}, now=NOW + timedelta(minutes=20))
    assert state["needs_attention"]  # no automatic resume after late evidence


def test_413_classifies_payload_and_429_has_finite_bounded_backoff():
    assert retry_guidance(413, payload_kind="images")["action"] == "split_images"
    assert retry_guidance(413, payload_kind="context")["action"] == "split_context"
    assert retry_guidance(413)["action"] == "classify_context_vs_images"
    assert [retry_guidance(429, attempt=i)["delay_seconds"] for i in range(3)] == [5, 10, 20]
    assert retry_guidance(429, retry_after=1000)["delay_seconds"] == 120
    assert retry_guidance(429, attempt=3)["action"] == "needs_attention"
    assert retry_guidance(429)["requires_known_rejection_before_retry"]
    assert retry_guidance(429)["model_change"] is False
