from datetime import timedelta

from acctsw.selection import choose
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


def test_naive_reset_timestamp_does_not_crash(tmp_path):
    at = now()
    naive = (at + timedelta(hours=1)).replace(tzinfo=None).isoformat()  # no tz suffix
    s = _state_with(tmp_path, [("a@x", naive)])
    sel = choose(s, "codex", at=at)
    assert sel.all_limited is True and sel.email == "a@x"
