from datetime import timedelta

import pytest

from acctsw.selection import choose, choose_key
from acctsw.state import State
from acctsw.util import now, iso


def _state_with(tmp_path, seats):
    s = State.load(tmp_path / "state.json")
    for email, limited_until in seats:
        s.upsert_seat("codex", email)
        if limited_until is not None:
            s.set_limited_until("codex", email, limited_until)
    return s


def test_no_seats(tmp_path):
    sel = choose(_state_with(tmp_path, []), "codex")
    assert sel.email is None and sel.all_limited is False


def test_prefers_active_when_available(tmp_path):
    s = _state_with(tmp_path, [("a@x", None), ("b@x", None)])
    s.set_active("codex", "b@x")
    sel = choose(s, "codex")
    assert sel.email == "b@x" and sel.available is True


def test_skips_limited_active_to_available(tmp_path):
    at = now()
    future = iso(at + timedelta(hours=2))
    s = _state_with(tmp_path, [("a@x", None), ("b@x", future)])
    s.set_active("codex", "b@x")  # active but limited
    sel = choose(s, "codex", at=at)
    assert sel.email == "a@x" and sel.available is True


def test_past_limit_counts_as_available(tmp_path):
    at = now()
    past = iso(at - timedelta(minutes=1))
    s = _state_with(tmp_path, [("a@x", past)])
    sel = choose(s, "codex", at=at)
    assert sel.email == "a@x" and sel.available is True


def test_all_limited_picks_soonest_unlock(tmp_path):
    at = now()
    soon = iso(at + timedelta(minutes=10))
    later = iso(at + timedelta(hours=5))
    s = _state_with(tmp_path, [("late@x", later), ("soon@x", soon)])
    sel = choose(s, "codex", at=at)
    assert sel.all_limited is True
    assert sel.available is False
    assert sel.email == "soon@x"
    assert sel.unlocks_at is not None


def test_all_limited_tie_break_is_deterministic(tmp_path):
    at = now()
    same = iso(at + timedelta(minutes=30))
    s = _state_with(tmp_path, [("a@x", same), ("b@x", same)])
    # equal unlock times → stable pick (first by insertion order), no crash
    assert choose(s, "codex", at=at).email == "a@x"


def test_exclude_skips_seat(tmp_path):
    s = _state_with(tmp_path, [("a@x", None), ("b@x", None)])
    s.set_active("codex", "a@x")
    sel = choose(s, "codex", exclude={"a@x"})   # launcher excluding a just-failed seat
    assert sel.email == "b@x" and sel.available is True


def test_exclude_all_returns_none(tmp_path):
    s = _state_with(tmp_path, [("a@x", None)])
    sel = choose(s, "codex", exclude={"a@x"})
    assert sel.email is None and sel.available is False


def test_unauthorized_seat_is_still_selectable(tmp_path):
    # a non-active seat routinely shows usage.error=unauthorized (stale access token); selection
    # must NOT treat that as unusable, or autoswitch would skip healthy backups.
    s = _state_with(tmp_path, [("a@x", None), ("b@x", None)])
    s.get_seat("codex", "b@x")["usage"] = {"error": "unauthorized"}
    s.set_active("codex", "a@x")
    sel = choose(s, "codex", exclude={"a@x"})
    assert sel.email == "b@x" and sel.available is True


def test_forbidden_seat_is_not_selectable(tmp_path):
    # 403 is the ONE auth error that is real evidence: the endpoint answered "this account is not
    # entitled" (cancelled/terminated subscription). Unlike a 401 a refresh would fix, switching
    # onto it can only fail — the field bug was auto-switch hopping onto a dead subscription.
    s = _state_with(tmp_path, [("a@x", None), ("b@x", None)])
    s.get_seat("codex", "b@x")["usage"] = {"error": "forbidden"}
    s.set_active("codex", "a@x")
    sel = choose(s, "codex", exclude={"a@x"})
    assert sel.email is None and sel.available is False


def test_forbidden_seat_is_skipped_but_healthy_sibling_wins(tmp_path):
    s = _state_with(tmp_path, [("a@x", None), ("b@x", None), ("c@x", None)])
    s.get_seat("codex", "b@x")["usage"] = {"error": "forbidden"}
    s.set_active("codex", "a@x")
    sel = choose(s, "codex", exclude={"a@x"})
    assert sel.email == "c@x" and sel.available is True


def test_naive_reset_timestamp_does_not_crash(tmp_path):
    at = now()
    naive = (at + timedelta(hours=1)).replace(tzinfo=None).isoformat()  # no tz suffix
    s = _state_with(tmp_path, [("a@x", naive)])
    sel = choose(s, "codex", at=at)
    assert sel.all_limited is True and sel.email == "a@x"


def _key(state, id="key1", harness="codex", error=None):
    seat = {"id": id, "harness": harness}
    if error is not None:
        seat["last_validation"] = {"error": error}
    state.data["keys"][id] = seat


@pytest.mark.parametrize("seats", [[], [("a@x", "2999-01-01T00:00:00+00:00")]])
def test_key_fallback_requires_opt_in(tmp_path, seats):
    s = _state_with(tmp_path, seats)
    _key(s)
    result = choose_key(s, "codex")
    assert result.seat_id is None and result.reason == "key_fallback_disabled"
    s.set_setting("key_fallback", True)
    result = choose_key(s, "codex")
    assert result.seat_id == "key1" and result.reason == "selected"


@pytest.mark.parametrize("error", [None, "unauthorized"])
def test_key_never_preempts_healthy_subscription(tmp_path, error):
    at = now()
    s = _state_with(tmp_path, [("resting@x", iso(at + timedelta(hours=1))), ("healthy@x", None)])
    s.set_active("codex", "resting@x")
    s.get_seat("codex", "healthy@x")["usage"] = {"error": error}
    s.set_setting("key_fallback", True)
    _key(s)
    result = choose_key(s, "codex", at=at)
    assert result.seat_id is None and result.reason == "subscription_available"
    s.set_limited_until("codex", "healthy@x", iso(at - timedelta(seconds=1)))
    assert choose_key(s, "codex", at=at).reason == "subscription_available"


def test_no_keys_and_wrong_harness_have_distinct_reasons(tmp_path):
    s = _state_with(tmp_path, [])
    s.set_setting("key_fallback", True)
    assert choose_key(s, "codex").reason == "no_key_seats"
    _key(s, harness="claude")
    result = choose_key(s, "codex")
    assert result.seat_id is None and result.reason == "harness_mismatch"
    assert choose_key(s, "claude").seat_id == "key1"


@pytest.mark.parametrize("error", ["invalid_key", "insufficient_quota"])
def test_key_validation_can_exclude_candidate(tmp_path, error):
    s = _state_with(tmp_path, [])
    s.set_setting("key_fallback", True)
    _key(s, error=error)
    result = choose_key(s, "codex")
    assert result.seat_id is None and result.reason == "ineligible_validation"
    _key(s, id="usable", error="unknown")
    assert choose_key(s, "codex").seat_id == "usable"


@pytest.mark.parametrize("error", [None, "unknown", "rate_limited"])
def test_eligible_keys_use_store_order(tmp_path, error):
    s = _state_with(tmp_path, [])
    s.set_setting("key_fallback", True)
    _key(s, id="z-first", error=error)
    _key(s, id="a-second", error=error)
    assert choose_key(s, "codex").seat_id == "z-first"


def test_filtered_subscription_is_not_proof_all_seats_rest(tmp_path):
    s = _state_with(tmp_path, [("a@x", None)])
    s.get_seat("codex", "a@x")["usage"] = {"error": "forbidden"}
    s.set_setting("key_fallback", True)
    _key(s)
    result = choose_key(s, "codex")
    assert result.seat_id is None and result.reason == "subscriptions_not_resting"
