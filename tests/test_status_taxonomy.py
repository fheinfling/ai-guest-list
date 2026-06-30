"""Tests for the one-status-per-seat taxonomy (spec §5) computed in accounts._assign_statuses."""
from datetime import timedelta

from acctsw import accounts as acct
from acctsw.util import now, iso


def _seats(specs):
    """specs: list of (email, active, limited_in_min_or_None, needs_login)."""
    at = now()
    out = []
    for email, active, lim, nl in specs:
        out.append({
            "email": email, "name": email.split("@")[0], "plan": None,
            "active": active, "limited": lim is not None,
            "limited_until": iso(at + timedelta(minutes=lim)) if lim is not None else None,
            "needs_login": nl, "usage5h": None, "usageWeek": None, "usage": None,
        })
    acct._assign_statuses(out)
    return {s["email"]: s["status"] for s in out}


def test_active_ready_resting():
    st = _seats([("a", True, None, False), ("b", False, None, False), ("c", False, 60, False)])
    assert st == {"a": "active", "b": "ready", "c": "resting"}


def test_queued_when_all_capped():
    # both capped, none active → soonest-reset is 'queued' (up next), the other 'resting'
    st = _seats([("late", False, 300, False), ("soon", False, 10, False)])
    assert st == {"soon": "queued", "late": "resting"}


def test_active_beats_queued_when_active_is_capped():
    # active seat is also capped; it stays 'active', queued falls to the soonest non-active capped
    st = _seats([("act", True, 200, False), ("soon", False, 10, False)])
    assert st["act"] == "active"
    assert st["soon"] == "queued"


def test_needs_login_excluded_from_queued_math():
    # a needs-login seat doesn't count as 'usable'; remaining single capped seat → queued
    st = _seats([("dead", False, None, True), ("capped", False, 30, False)])
    assert st["dead"] == "needs-login"
    assert st["capped"] == "queued"


def test_all_needs_login_no_queued():
    st = _seats([("x", False, None, True), ("y", False, None, True)])
    assert set(st.values()) == {"needs-login"}
