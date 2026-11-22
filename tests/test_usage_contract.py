"""The popover's usage view is tested against app/web/fixtures/usage-history.sample.json; the engine
produces the payload in acctsw/tokenlog.history(). This pins the two to one shape, so neither side
can drift from the contract while its own tests keep passing."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from acctsw import tokenlog

SAMPLE = Path(__file__).resolve().parents[1] / "app" / "web" / "fixtures" / "usage-history.sample.json"
# A frozen clock (tokenlog._now is patched below): a Wednesday, mid calendar week.
_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
AT = (_NOW - timedelta(seconds=30)).isoformat()
BOUND = (_NOW - timedelta(seconds=60)).isoformat()


def _write(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _shape(value):
    """Keys and JSON types, recursively; maps keyed by data (seat keys, model ids) collapse to one entry."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _engine_payload(ctx):
    state = ctx.load_state()
    state.data["tools"]["claude"]["accounts"]["biz@example.com"] = {
        "email": "biz@example.com", "name": "Business", "plan": "team",
        "usage": {"windows": {"weekly": {"used_pct": 40, "resets_at": (_NOW + timedelta(days=3)).isoformat()}}}}
    state.save()
    # The launcher binds a session before it produces any usage: stamp the bind before the event.
    _write(ctx.data_dir / "seat-ledger.jsonl", {"v": 1, "at": BOUND, "tool": "claude",
           "email": "biz@example.com", "pid": 1, "thread": "t1", "event": "bind"})
    _write(ctx._claude_real / "projects" / "p" / "t1.jsonl",
           {"type": "assistant", "sessionId": "t1", "requestId": "r1", "timestamp": AT,
            "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
                "output_tokens": 5}}},
           {"type": "assistant", "sessionId": "other", "requestId": "r2", "timestamp": AT,
            "message": {"id": "m2", "model": "claude-sonnet-5-5", "usage": {
                "input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "output_tokens": 1}}})
    tokenlog.refresh(ctx, budget_s=30)
    return tokenlog.history(ctx)


def test_engine_payload_matches_the_ui_fixture_shape(ctx, monkeypatch):
    monkeypatch.setattr(tokenlog, "_now", lambda: _NOW.astimezone())
    sample = json.loads(SAMPLE.read_text())
    payload = _engine_payload(ctx)

    assert set(payload) == set(sample)
    assert _shape(payload["index"]) == _shape(sample["index"])
    assert set(payload["tracking"]) == set(sample["tracking"])
    assert set(payload["models"][0]) == set(sample["models"][0])
    seat_keys = {k for s in sample["seats"] for k in s}
    assert all(set(s) <= seat_keys for s in payload["seats"])
    assert {s["kind"] for s in payload["seats"]} <= {"sub", "key", "removed", "unattributed"}
    for mode in ("week", "month"):
        mine, theirs = payload["calendar"][mode], sample["calendar"][mode]
        assert set(mine[0]) == set(theirs[0])
        assert len(mine) == len(theirs)
        assert mine[0]["current"] is True and mine[1]["prev_same_point"] is None
        assert all(len(counts) == 4 for by in mine[0]["by"].values() for counts in by.values())
    own_mine = payload["own"]["claude:biz@example.com"]
    own_theirs = next(iter(sample["own"].values()))
    assert set(own_mine) == set(own_theirs)
    assert set(own_mine["week"][0]) == set(own_theirs["week"][0])
    # The engine's Claude month anchors need a profile file; with none it reports null, which the UI
    # must accept (its fallback note). Any non-null month must match the sample's keys.
    month = own_mine["month"]
    assert month is None or set(month) == set(next(o["month"] for o in sample["own"].values() if o["month"]))
    # Attribution reached the payload: the ledger-bound session lands on the seat, the other is unknown.
    week = payload["calendar"]["week"][0]["by"]
    assert set(week) == {"claude:biz@example.com", "claude:?"}
