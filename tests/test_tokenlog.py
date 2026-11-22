import base64
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from acctsw import bridge, seatledger, tokenlog, usage

AT = "2026-11-21T10:00:00+00:00"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    real_now = tokenlog._now
    monkeypatch.setattr(tokenlog, "_now", lambda: datetime.fromisoformat("2026-11-21T15:30:00+00:00"))
    return real_now


def write(path, *rows, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab" if append else "wb") as stream:
        for row in rows:
            stream.write((json.dumps(row) + "\n").encode())
    return path


def claude(mid="m1", *, at=AT, thread="thread", model="claude-opus-5-5", n=10):
    return {"type": "assistant", "sessionId": thread, "requestId": "r" + mid, "timestamp": at,
            "message": {"id": mid, "model": model, "usage": {
                "input_tokens": n, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
                "output_tokens": 5, "output_tokens_details": {"thinking_tokens": 2}}}}


def meta(thread="thread", user=None, account=None, at=AT):
    return {"type": "session_meta", "timestamp": at,
            "payload": {"id": thread, "creator_user_id": user, "creator_account_id": account}}


def turn(model):
    return {"type": "turn_context", "payload": {"model": model}}


def count(n, *, cached=0, cache_write=0, output=5, at=AT, info=True, window=None):
    return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": n, "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write, "output_tokens": output,
        "reasoning_output_tokens": 2}, "last_token_usage": {"input_tokens": 999999}}
        if info else None, "rate_limits": {"secondary": window}}}


def cp(ctx, name="s.jsonl"):
    return ctx._claude_real / "projects" / "fake-project" / name


def xp(ctx, name="rollout-fake.jsonl"):
    return ctx._codex_real / "sessions" / "2000" / "01" / "01" / name


def rows(ctx, table="usage"):
    db = sqlite3.connect(ctx.data_dir / "usage.sqlite")
    try:
        return db.execute("SELECT * FROM " + table).fetchall()
    finally:
        db.close()


def totals(ctx):
    out = [0, 0, 0, 0]
    for row in rows(ctx):
        for i, n in enumerate(row[4:]):
            out[i] += n
    return out


def refresh(ctx, budget=30):
    tokenlog.refresh(ctx, budget_s=budget)


def bind(ctx, tool, email, thread="thread", at="2026-11-21T09:00:00+00:00"):
    row = dict(v=1, at=at, tool=tool, email=email, thread=thread, pid=42, event="bind")
    write(ctx.data_dir / "seat-ledger.jsonl", row, append=True)


def seats(ctx, tool="codex", **accounts):
    state = ctx.load_state()
    state.data["tools"][tool]["accounts"].update(accounts)
    state.save()


def test_claude_dedupes_globally_skips_synthetic_and_finds_subagents(ctx):
    write(cp(ctx), claude(), claude(), claude("skip", model="<synthetic>"), {},
          {"type": "assistant", "message": {"id": "no-usage"}})
    write(cp(ctx, "s/subagents/a.jsonl"), claude(), claude("m2"))
    refresh(ctx)
    refresh(ctx)
    assert totals(ctx) == [20, 8, 6, 10]
    assert len(rows(ctx, "claude_seen")) == 2
    assert tokenlog.index_progress(ctx) == {"done": True, "pct": 100}


def test_claude_dedupe_key_includes_request_id(ctx):
    first, second = claude(), claude()
    second["requestId"] = "different-request"
    write(cp(ctx), first, second)
    refresh(ctx)
    assert totals(ctx) == [20,8,6,10]


def test_rotation_with_larger_unrelated_contents_reads_from_zero(ctx):
    path = write(cp(ctx), claude("old1"), claude("old2"))
    refresh(ctx)
    path.rename(path.with_suffix(".old"))
    write(path, claude("new1"), claude("new2"), claude("new3"))
    refresh(ctx)
    assert totals(ctx) == [50,20,15,25]


def test_complete_lines_only_then_resume(ctx):
    path = write(cp(ctx), claude())
    partial = json.dumps(claude("m2")).encode()
    with path.open("ab") as stream:
        stream.write(partial[:80])
    refresh(ctx)
    assert totals(ctx) == [10, 4, 3, 5]
    offset = rows(ctx, "files")[0][4]
    assert offset == path.stat().st_size - 80
    with path.open("ab") as stream:
        stream.write(partial[80:] + b"\n")
    refresh(ctx)
    assert totals(ctx) == [20, 8, 6, 10]


@pytest.mark.parametrize("rotate", [False, True])
def test_truncation_rotation_preserves_deduplication(ctx, rotate):
    path = write(cp(ctx), claude(), claude("m2"))
    refresh(ctx)
    if rotate:
        path.rename(path.with_suffix(".old"))
    write(path, claude())
    refresh(ctx)
    write(path, claude("m3"), append=True)
    refresh(ctx)
    assert totals(ctx) == [30, 12, 9, 15]


def test_codex_deltas_copy_reset_normalisation_model_switch_and_null(ctx):
    events = [meta(), turn("gpt-a"), count(100, cached=20, cache_write=10, output=10),
              count(100, cached=20, cache_write=10, output=10), count(999, info=False),
              turn("gpt-b"), count(150, cached=30, cache_write=15, output=20,
                                   at="2026-11-21T11:00:00Z")]
    path = write(xp(ctx), *events)
    refresh(ctx)
    assert totals(ctx) == [105, 30, 15, 20]
    assert {r[2]: list(r[4:]) for r in rows(ctx)} == {"gpt-a": [70,20,10,10], "gpt-b": [35,10,5,10]}
    write(xp(ctx, "rollout-copy.jsonl"), *events)
    refresh(ctx)
    assert totals(ctx) == [105, 30, 15, 20]
    write(path, count(10, cached=2, cache_write=1, output=3, at="2026-11-21T12:00:00Z"), append=True)
    refresh(ctx)
    assert totals(ctx) == [112, 32, 16, 23]
    write(path, count(10, cached=2, cache_write=1, output=3, at="2026-11-21T12:01:00Z"), append=True)
    refresh(ctx)
    assert totals(ctx) == [112, 32, 16, 23]


def test_codex_nonnegative_fresh_input_and_persisted_file_context(ctx):
    path = write(xp(ctx), meta(), turn("persisted"), count(5, cached=4, cache_write=3))
    refresh(ctx)
    write(path, count(15, cached=8, cache_write=5, output=8, at="2026-11-21T11:00:00Z"), append=True)
    refresh(ctx)
    assert totals(ctx) == [4, 8, 5, 8]
    assert rows(ctx)[0][2] == "persisted"


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_ledger_attribution_and_hop_split(ctx, tool):
    seats(ctx, account={"account_id": "user-fake"})
    bind(ctx, tool, "first")
    bind(ctx, tool, "second", at="2026-11-21T10:30:00Z")
    if tool == "claude":
        write(cp(ctx), claude(), claude("m2", at="2026-11-21T11:00:00Z"))
    else:
        write(xp(ctx), meta(user="user-fake"), count(10),
              count(20, output=10, at="2026-11-21T11:00:00Z"))
    refresh(ctx)
    assert {r[3] for r in rows(ctx)} == {tool + ":first", tool + ":second"}
    history = tokenlog.history(ctx)
    assert {s["key"] for s in history["seats"] if s["kind"] == "removed"} == {
        tool + ":first", tool + ":second"}


def test_creator_identity_workspace_tiebreak_and_unknown(ctx):
    seats(ctx, z={"account_id": "user-fake", "workspace_id": "correct"},
          a={"account_id": "user-fake", "workspace_id": "other"})
    write(xp(ctx), meta("one", "user-fake", "correct"), count(10))
    write(xp(ctx, "rollout-two.jsonl"), meta("two", "user-fake", None), count(10))
    write(xp(ctx, "rollout-three.jsonl"), meta("three", "user-unknown"), count(10))
    write(cp(ctx), claude())
    refresh(ctx)
    assert {r[3] for r in rows(ctx)} == {"codex:z", "codex:a", "codex:?", "claude:?"}
    payload = tokenlog.history(ctx)
    assert [s["key"] for s in payload["seats"]][-2:] == ["codex:?", "claude:?"]
    assert set(payload["own"]) == {"codex:z", "codex:a"}
    assert payload["tracking"]["codex"] is not None
    assert payload["tracking"]["claude"] is None


def test_ledger_slack_and_equal_time_last_bind(ctx):
    bind(ctx, "claude", "first", at="2026-11-21T10:02:00Z")
    bind(ctx, "claude", "second", at="2026-11-21T10:02:00Z")
    bind(ctx, "claude", "too-late", at="2026-11-21T10:02:01Z")
    write(cp(ctx), claude())
    refresh(ctx)
    assert rows(ctx)[0][3] == "claude:second"
    assert tokenlog.history(ctx)["tracking"]["claude"] == "2026-11-21T10:02:00Z"


@pytest.fixture
def berlin(monkeypatch):
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_calendar_dst_and_month_boundaries(ctx, monkeypatch, berlin):
    monkeypatch.setattr(tokenlog, "_now", lambda: datetime.fromisoformat("2026-10-28T12:00:00+01:00"))
    payload = tokenlog.history(ctx)
    weeks = payload["calendar"]["week"]
    assert len(weeks) == 13
    assert weeks[0]["from"] == "2026-10-26T00:00:00+01:00"
    assert weeks[1]["from"] == "2026-10-19T00:00:00+02:00"
    assert weeks[1]["to"] == weeks[0]["from"]
    a, b = (datetime.fromisoformat(weeks[1][key]) for key in ("from", "to"))
    assert b.timestamp() - a.timestamp() == 169 * 3600
    assert weeks[1]["label"] == "Oct 19 – Oct 25"
    months = payload["calendar"]["month"]
    assert len(months) == 6 and months[0]["label"] == "October"
    assert months[0]["from"] == "2026-10-01T00:00:00+02:00"
    assert months[0]["to"] == "2026-11-01T00:00:00+01:00"


def test_prev_same_point_only_current_and_cutoff(ctx, berlin, monkeypatch):
    monkeypatch.setattr(tokenlog, "_now", lambda: datetime.fromisoformat("2026-10-28T12:00:00+01:00"))
    write(cp(ctx), claude("included", at="2026-10-21T09:00:00Z"),
          claude("outside", at="2026-10-21T10:00:00Z"),
          claude("current", at="2026-10-28T10:00:00Z"))
    refresh(ctx)
    periods = tokenlog.history(ctx)["calendar"]["week"]
    # 60 elapsed hours since Monday; previous Monday is still in summer time.
    assert periods[0]["prev_same_point"]["claude:?"]["claude-opus-5-5"] == [10,4,3,5]
    assert all(p["prev_same_point"] is None for p in periods[1:])


@pytest.mark.parametrize("year,expected", [(2026, "2026-02-28"), (2024, "2024-02-29")])
def test_billing_day_31_clamps_without_drifting(ctx, monkeypatch, berlin, year, expected):
    monkeypatch.setattr(tokenlog, "_now", lambda: datetime.fromisoformat(f"{year}-03-15T12:00:00+01:00"))
    seats(ctx, a={"billing": {"renews_at": f"{year}-01-31T19:45:00Z", "source": "codex_token"}})
    billing = tokenlog.history(ctx)["own"]["codex:a"]["month"]
    assert billing["renews_day"] == 31 and billing["source"] == "codex_token"
    assert billing["periods"][0]["from"].startswith(expected + "T00:00:00")
    assert billing["periods"][0]["to"].startswith(f"{year}-03-31T00:00:00")
    assert billing["periods"][1]["from"].startswith(f"{year}-01-31T00:00:00")


def test_claude_billing_verifies_email_and_key_calendar(ctx, berlin):
    seats(ctx, "claude", a={}, b={})
    for email, profile_email in (("a", "A"), ("b", "wrong")):
        path = ctx.claude_home(email) / ".claude.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"oauthAccount": {"emailAddress": profile_email,
                        "subscriptionCreatedAt": "2026-01-14T22:30:00Z"}}))
    state = ctx.load_state()
    state.data["keys"] = {"fake-key": {"id": "fake-key", "label": "Key", "harness": "codex"}}
    state.save()
    history = tokenlog.history(ctx)
    assert history["own"]["claude:a"]["month"]["source"] == "claude_profile"
    assert history["own"]["claude:a"]["month"]["renews_day"] == 14
    assert history["own"]["claude:b"]["month"] is None
    assert history["own"]["codex:fake-key"]["month"]["source"] == "calendar"


def test_own_windows_snap_extrapolate_merge_and_peak(ctx):
    end = datetime.fromisoformat("2026-11-22T10:00:00Z")
    seats(ctx, a={"usage": {"windows": {"weekly": {"used_pct": 65, "resets_at": end.isoformat()}}}})
    write(xp(ctx), meta(user="user-fake"), count(10, window={"window_minutes": 10080,
          "used_percent": 87, "resets_at": end.timestamp()}))
    bind(ctx, "codex", "a")
    refresh(ctx)
    with tokenlog._connect(ctx) as db:
        tokenlog._window(db, "codex:a", (end - timedelta(days=7, hours=2)).timestamp(), 40)
        tokenlog._window(db, "codex:a", (end - timedelta(minutes=10)).timestamp(), 90)
    periods = tokenlog.history(ctx)["own"]["codex:a"]["week"]
    assert periods[0]["current"] and periods[0]["approx"] is False
    assert periods[0]["limit_pct"] == 90
    assert datetime.fromisoformat(periods[0]["from"]).timestamp() == (end - timedelta(days=7, hours=2)).timestamp()
    assert periods[1]["approx"] is True and periods[1]["limit_pct"] == 40
    assert periods[2]["limit_pct"] is None
    assert periods[1]["to"] == periods[0]["from"]


def test_weekly_snapshot_keeps_peak_across_refreshes(ctx):
    end = "2026-11-22T10:00:00Z"
    seats(ctx, a={"usage": {"windows": {"weekly": {"used_pct": 87, "resets_at": end}}}})
    refresh(ctx)
    seats(ctx, a={"usage": {"windows": {"weekly": {"used_pct": 20, "resets_at": end}}}})
    refresh(ctx)
    assert rows(ctx, "windows")[0][2:] == (87, 1)
    assert tokenlog.history(ctx)["own"]["codex:a"]["week"][0]["limit_pct"] == 87


def test_key_windows_never_have_limit_percentage_and_api_homes_are_scanned(ctx):
    state = ctx.load_state()
    state.data["keys"] = {"key": {"id": "key", "label": "Key", "harness": "codex"}}
    state.save()
    bind(ctx, "codex", "key")
    path = ctx._homes_root / "private-pin" / "sessions" / "rollout-key.jsonl"
    write(path, meta(), count(10, window={"window_minutes": 10080, "used_percent": 50,
                                        "resets_at": 1795341600}))
    refresh(ctx)
    payload = tokenlog.history(ctx)
    assert totals(ctx) == [10,0,0,5]
    assert all(p["limit_pct"] is None for p in payload["own"]["codex:key"]["week"])


def test_models_labels_slots_fold_and_payload_shape(ctx):
    models = ["gpt-z", "claude-sonnet-4-5-20250929", "claude-opus-5-5",
              "claude-haiku-4-5", "claude-custom", "a", "b", "c", "d"]
    write(cp(ctx), *(claude(str(i), model=model) for i, model in enumerate(models)))
    bind(ctx, "claude", "seat")
    seats(ctx, "claude", seat={"name": "Fake", "plan": "team"})
    refresh(ctx)
    history = tokenlog.history(ctx)
    assert set(history) == {"v", "generated_at", "index", "tracking", "models", "seats", "calendar", "own"}
    assert history["v"] == 1 and datetime.fromisoformat(history["generated_at"]).utcoffset() is not None
    assert history["index"] == {"done": True, "pct": 100}
    assert history["models"][:2] == [dict(id="claude-opus-5-5", label="Opus 5.5", slot=1),
        dict(id="claude-sonnet-4-5-20250929", label="Sonnet 4.5", slot=2)]
    assert [m["slot"] for m in history["models"]] == [1,2,3,4,5,6,7,0]
    assert history["models"][-1] == dict(id="other", label="other models", slot=0)
    period = history["calendar"]["week"][0]
    assert set(period) == {"from", "to", "label", "current", "by", "prev_same_point"}
    assert period["by"]["claude:seat"]["other"] == [20,8,6,10]
    own = history["own"]["claude:seat"]["week"][0]
    assert set(own) == {"from", "to", "label", "current", "approx", "limit_pct", "by"}
    assert own["by"] == period["by"]["claude:seat"]
    assert isinstance(own["approx"], bool) and own["approx"]
    assert history["seats"] == [dict(key="claude:seat", name="Fake", plan="team", tool="claude", kind="sub")]
    json.dumps(history, allow_nan=False)


def test_budget_stops_and_resumes(ctx, monkeypatch):
    monkeypatch.setattr(tokenlog, "_CHUNK", 1024)
    monkeypatch.setattr(tokenlog, "_BACKLOG", 0)  # exact progress on a tiny fixture
    write(cp(ctx), *(claude(str(i)) for i in range(100)))
    refresh(ctx, 0)
    assert totals(ctx) == [0,0,0,0]
    assert tokenlog.index_progress(ctx) == {"done": False, "pct": 0}
    ticks = iter([0, 0, 0, 10])
    with monkeypatch.context() as patch:
        patch.setattr(tokenlog.time, "monotonic", lambda: next(ticks, 10))
        refresh(ctx, 1)
    progress = tokenlog.index_progress(ctx)
    assert not progress["done"] and 0 < progress["pct"] < 100
    assert 0 < totals(ctx)[0] < 1000
    assert tokenlog.index_progress(ctx) == progress
    refresh(ctx)
    assert totals(ctx) == [1000,400,300,500]


def test_schema_mismatch_rebuilds(ctx):
    write(cp(ctx), claude())
    refresh(ctx)
    db = sqlite3.connect(ctx.data_dir / "usage.sqlite")
    db.execute("UPDATE meta SET value='obsolete' WHERE key='schema_version'")
    db.commit()
    db.close()
    refresh(ctx)
    assert totals(ctx) == [10,4,3,5]
    with tokenlog._connect(ctx) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


@pytest.mark.parametrize("obj", [[], None, {}, {"type":"assistant", "message": []},
    {"type":"event_msg", "payload": None}, {"type":"session_meta", "payload":{"id":[]}},
    {"type":"turn_context", "payload":{"model":[]}},
    {"type":"event_msg", "timestamp": AT, "payload":{"type":"token_count", "info":[]}}])
def test_malformed_input_never_raises(ctx, obj):
    write(cp(ctx), obj, claude())
    write(xp(ctx), meta(), obj, count(1))
    refresh(ctx)
    assert totals(ctx) == [11,4,3,10]


def test_history_bridge_has_no_state_and_sanitizes_errors(ctx, monkeypatch):
    result = bridge.handle(ctx, {"action": "usage_history"})
    assert result["ok"] is True and "usage_history" in result and "state" not in result
    monkeypatch.setattr(tokenlog, "refresh", lambda *a, **kw: (_ for _ in ()).throw(OSError("/private/fake-secret")))
    assert bridge.handle(ctx, {"action": "usage_history"}) == {
        "ok": False, "usage_history_error": "Could not read usage history"}


def test_history_and_index_never_read_credentials(ctx, monkeypatch):
    seats(ctx, a={})
    seats(ctx, "claude", b={})
    def forbidden(*args, **kwargs):
        pytest.fail("history read credentials")
    monkeypatch.setattr(ctx, "snapshot_get", forbidden)
    monkeypatch.setattr(ctx.keychain, "get", forbidden)
    refresh(ctx)
    payload = tokenlog.history(ctx)
    assert list(payload["own"]) == ["codex:a", "claude:b"]


def test_poll_persists_billing_from_existing_blob(ctx):
    seats(ctx, a={})
    claim = {"https://api.openai.com/auth": {"chatgpt_subscription_active_start": "2026-10-14T12:00:00Z",
             "chatgpt_subscription_active_until": "2026-11-14T12:00:00Z"}}
    jwt = "." + base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("=") + "."
    blob = json.dumps({"tokens": {"id_token": jwt}})
    state = ctx.load_state()
    usage.store_fetch(state, "codex", "a", usage.Usage(ok=True), blob=blob)
    assert state.get_seat("codex", "a")["billing"] == {
        "renews_at": "2026-11-14T12:00:00Z", "source": "codex_token"}


def test_symlinked_seat_homes_do_not_read_shared_rollouts_twice(ctx):
    # Per-seat CODEX_HOMEs link `sessions` (and Claude homes link `projects`) to the shared dir.
    # Every file must be indexed under one path only: otherwise each seat adds a full extra read
    # and the progress bar's byte total grows by one copy of the whole history per seat.
    write(xp(ctx), meta(), count(10))
    write(cp(ctx), claude("m1"))
    for name in ("a", "b"):
        home = ctx._homes_root / name
        home.mkdir(parents=True)
        (home / "sessions").symlink_to(ctx._codex_real / "sessions")
        (home / "projects").symlink_to(ctx._claude_real / "projects")
    refresh(ctx)
    assert len(rows(ctx, "files")) == 2
    assert totals(ctx) == [20, 4, 3, 10]  # one codex event + one claude message, each once


def test_progress_is_done_while_live_sessions_keep_appending(ctx, monkeypatch):
    # An active session appends constantly. A small unread tail must not hold the view in its
    # "reading history" state; a real backlog must.
    write(cp(ctx), claude("m1"))
    refresh(ctx)
    write(cp(ctx), claude("m2"), append=True)
    tokenlog.refresh(ctx, budget_s=0)
    assert tokenlog.index_progress(ctx) == {"done": True, "pct": 100}
    monkeypatch.setattr(tokenlog, "_BACKLOG", 0)
    progress = tokenlog.index_progress(ctx)
    assert progress["done"] is False and 0 < progress["pct"] < 100



@pytest.mark.parametrize("ahead", [0, 7 * 86400])
def test_own_week_next_end_boundaries(ctx, monkeypatch, ahead):
    now = datetime.fromisoformat(AT)
    monkeypatch.setattr(tokenlog, "_now", lambda: now)
    seats(ctx, a={})
    with tokenlog._connect(ctx) as db:
        tokenlog._window(db, "codex:a", now.timestamp() + ahead, 41)
    periods = tokenlog.history(ctx)["own"]["codex:a"]["week"]
    assert len(periods) == 13  # fourteen boundaries enclose thirteen weeks
    assert datetime.fromisoformat(periods[0]["to"]).timestamp() == now.timestamp() + 7 * 86400
    assert periods[0]["current"] is True
    assert periods[1]["current"] is False
    assert periods[0]["approx"] is True
    assert periods[0]["limit_pct"] == (41 if ahead else None)


@pytest.mark.parametrize("gap,merged", [(1800, True), (1801, False)])
def test_own_week_merge_tolerance_keeps_latest_end_and_highest_peak(ctx, gap, merged):
    end = tokenlog._now().timestamp() + 86400
    seats(ctx, a={})
    with tokenlog._connect(ctx) as db:
        for stamp, peak in [(end - gap, 93), (end, 20), (end - 7 * 86400, 10)]:
            tokenlog._window(db, "codex:a", stamp, peak)
    period = tokenlog.history(ctx)["own"]["codex:a"]["week"][0]
    assert datetime.fromisoformat(period["to"]).timestamp() == (end if merged else end - gap)
    assert datetime.fromisoformat(period["from"]).timestamp() == end - 7 * 86400
    assert period["limit_pct"] == 93
    assert period["approx"] is False


@pytest.mark.parametrize("distance,snapped", [(43200, True), (43201, False), (46800, False)])
def test_own_week_snap_tolerance_and_approximation(ctx, distance, snapped):
    end = tokenlog._now().timestamp() + 86400
    previous = end - 7 * 86400 - distance
    seats(ctx, a={})
    with tokenlog._connect(ctx) as db:
        tokenlog._window(db, "codex:a", end, 30)
        tokenlog._window(db, "codex:a", previous, 70)
    periods = tokenlog.history(ctx)["own"]["codex:a"]["week"]
    assert datetime.fromisoformat(periods[0]["from"]).timestamp() == (
        previous if snapped else end - 7 * 86400)
    assert periods[0]["approx"] is (not snapped)
    assert periods[1]["limit_pct"] == (70 if snapped else None)
    assert all(p["approx"] for p in periods[1:])
    assert all(a["from"] == b["to"] for a, b in zip(periods, periods[1:]))


@pytest.mark.parametrize("anchors,expected", [
    ([(-10, 8), (-2, 99)], 5),  # nearest past observation, not highest peak or first row
    ([(-20, 99), (8.5, 1)], 1.5),  # nearest may be in the future beyond the next week
    ([(3, 5), (5, 99)], 3),  # earliest upcoming observation wins
])
def test_own_week_selects_or_extrapolates_nearest_observed_end(ctx, anchors, expected):
    at = tokenlog._now().timestamp()
    seats(ctx, a={})
    with tokenlog._connect(ctx) as db:
        for days, peak in anchors:
            tokenlog._window(db, "codex:a", at + days * 86400, peak)
    period = tokenlog.history(ctx)["own"]["codex:a"]["week"][0]
    assert datetime.fromisoformat(period["to"]).timestamp() == at + expected * 86400
    assert period["approx"] is True


@pytest.mark.parametrize("day", [14, 22])
def test_billing_midnight_today_and_future_anchor(ctx, monkeypatch, berlin, day):
    now = datetime.fromisoformat("2026-11-14T00:00:00+01:00")
    monkeypatch.setattr(tokenlog, "_now", lambda: now)
    seats(ctx, a={"billing": {"renews_at": f"2026-01-{day}T19:45:00Z"}})
    month = tokenlog.history(ctx)["own"]["codex:a"]["month"]
    assert month["source"] == "codex_token" and month["renews_day"] == day
    periods = month["periods"]
    assert len(periods) == 6
    expected_start = "2026-11-14T00:00:00+01:00" if day == 14 else "2026-10-22T00:00:00+02:00"
    assert periods[0]["from"] == expected_start
    assert periods[0]["current"] is True and periods[1]["current"] is False
    assert all(p["approx"] is False for p in periods)
    assert all(a["from"] == b["to"] for a, b in zip(periods, periods[1:]))


@pytest.mark.parametrize("anchor", [None, 42, True, "invalid", "2026-99-14T00:00:00Z"])
def test_billing_invalid_anchor_has_no_month(ctx, anchor):
    seats(ctx, a={"billing": {"renews_at": anchor}})
    assert tokenlog.history(ctx)["own"]["codex:a"]["month"] is None


@pytest.mark.parametrize("n", [7, 8])
def test_history_seven_models_and_other_cutoff(ctx, n):
    models = ["claude-opus-4-1", "claude-sonnet-4-5-20250929", "claude-haiku-4-5",
              "claude-custom", "a", "b", "c", "d"][:n]
    write(cp(ctx), *(claude(str(i), model=model) for i, model in enumerate(reversed(models))))
    refresh(ctx)
    payload = tokenlog.history(ctx)
    expected = [dict(id=model, label=label, slot=i + 1) for i, (model, label) in enumerate(zip(
        models[:7], ["Opus 4.1", "Sonnet 4.5", "Haiku 4.5", "claude-custom", "a", "b", "c"]))]
    if n == 8:
        expected.append(dict(id="other", label="other models", slot=0))
    assert payload["models"] == expected
    by = payload["calendar"]["week"][0]["by"]["claude:?"]
    assert set(by) == set(models[:7]) | ({"other"} if n == 8 else set())
    assert sum(counts[0] for counts in by.values()) == n * 10


def test_history_hour_boundaries_and_previous_month_cutoff(ctx, monkeypatch, berlin):
    # Whole-hour local midnight must include its bucket; the upper bound is exclusive.
    monkeypatch.setattr(tokenlog, "_now", lambda: datetime.fromisoformat("2026-11-02T02:00:00+01:00"))
    write(cp(ctx), claude("before", at="2026-11-01T22:00:00Z", n=1),
          claude("start", at="2026-11-01T23:00:00Z", n=2),
          claude("inside", at="2026-11-02T00:00:00Z", n=4),
          claude("end", at="2026-11-02T01:00:00Z", n=8),
          claude("prev-before", at="2026-10-01T23:00:00Z", n=16),
          claude("prev-cutoff", at="2026-10-02T00:00:00Z", n=32))
    refresh(ctx)
    history = tokenlog.history(ctx)
    week = history["calendar"]["week"][0]
    assert week["by"]["claude:?"]["claude-opus-5-5"][0] == 6
    assert history["calendar"]["month"][0]["prev_same_point"]["claude:?"]["claude-opus-5-5"][0] == 16


def test_key_calendar_months_and_seat_labels(ctx):
    state = ctx.load_state()
    state.data["keys"] = {
        "c": {"id": "c", "harness": "codex", "label": "Named"},
        "h": {"id": "h", "harness": "claude", "label": ""}}
    state.save()
    history = tokenlog.history(ctx)
    assert [(s["key"], s["name"]) for s in history["seats"]] == [("codex:c", "Named"), ("claude:h", "h")]
    for key in ("codex:c", "claude:h"):
        month = history["own"][key]["month"]
        assert month["source"] == "calendar" and month["renews_day"] == 1
        assert [(p["from"], p["to"]) for p in month["periods"]] == [
            (p["from"], p["to"]) for p in history["calendar"]["month"]]
        assert all(p["approx"] for p in month["periods"])


@pytest.mark.parametrize("size,offset,expected", [
    (16 << 20, 8 << 20, {"done": True, "pct": 100}),
    ((16 << 20) + 1, 8 << 20, {"done": False, "pct": 49}),
    (100_000_000, 30_000_000, {"done": False, "pct": 30}),
    (2_000_000_000, 1_991_000_000, {"done": False, "pct": 99}),
])
def test_index_progress_exact_backlog_and_percent(ctx, size, offset, expected):
    # Metadata models a large backlog without allocating transcript data.
    with tokenlog._connect(ctx) as db:
        tokenlog._save_file(db, dict(path="fake", dev=0, ino=0, size=size, offset=offset,
            mtime_ns=0, tool="codex", thread=None, model=None, creator_user=None, creator_account=None))
    assert tokenlog.index_progress(ctx) == expected


def test_removed_transcript_leaves_counts_but_not_backlog(ctx):
    path = write(cp(ctx), claude())
    refresh(ctx)
    path.unlink()
    refresh(ctx)
    assert rows(ctx, "files") == []
    assert totals(ctx) == [10, 4, 3, 5]
    assert tokenlog.index_progress(ctx) == {"done": True, "pct": 100}


def test_unchanged_file_keeps_codex_context_for_future_appends(ctx, monkeypatch):
    path = write(xp(ctx), meta(user="fake-user"), turn("persisted"), count(10))
    refresh(ctx)
    monkeypatch.setattr(tokenlog, "_BACKLOG", 0)
    refresh(ctx, 0)  # size == offset must retain progress, thread and model
    assert tokenlog.index_progress(ctx) == {"done": True, "pct": 100}
    write(path, count(20, output=10, at="2026-11-21T11:00:00Z"), append=True)
    refresh(ctx)
    assert totals(ctx) == [20, 0, 0, 10]
    assert {r[2] for r in rows(ctx)} == {"persisted"}


@pytest.mark.parametrize("ticks", [[0, 1, 0], [0, 0, 1, 0]])
def test_refresh_stops_at_exact_deadline(ctx, monkeypatch, ticks):
    write(cp(ctx), claude())
    clock = iter(ticks)
    monkeypatch.setattr(tokenlog.time, "monotonic", lambda: next(clock, 2))
    refresh(ctx, 1)
    assert totals(ctx) == [0, 0, 0, 0]
    assert rows(ctx, "files")[0][4] == 0


def test_large_partial_tail_waits_for_newline(ctx, monkeypatch):
    monkeypatch.setattr(tokenlog, "_CHUNK", 16)
    path = write(cp(ctx), claude())
    raw = json.dumps(claude("tail")).encode()
    with path.open("ab") as stream:
        stream.write(raw)
    ticks = iter([0] * 10)
    with monkeypatch.context() as patch:
        patch.setattr(tokenlog.time, "monotonic", lambda: next(ticks, 2))
        refresh(ctx, 1)
    assert totals(ctx) == [10, 4, 3, 5]
    assert rows(ctx, "files")[0][4] == path.stat().st_size - len(raw)
    with path.open("ab") as stream:
        stream.write(b"\n")
    refresh(ctx)
    assert totals(ctx) == [20, 8, 6, 10]


@pytest.mark.parametrize("first,second", [((100, 90), (110, 60)), ((100, 90), (110, 80))])
def test_codex_reset_compares_total_input_plus_output(ctx, first, second):
    write(xp(ctx), meta(), count(first[0], output=first[1]),
          count(second[0], output=second[1], at="2026-11-21T11:00:00Z"))
    refresh(ctx)
    expected = [210, 0, 0, 150] if second[1] == 60 else [110, 0, 0, 90]
    assert totals(ctx) == expected


@pytest.mark.parametrize("minutes", [10080, 10081, 300])
def test_codex_only_weekly_rate_window_is_captured(ctx, minutes):
    write(xp(ctx), meta(), count(1, window={"window_minutes": minutes,
        "used_percent": 88, "resets_at": "2026-11-23T10:00:00Z"}))
    refresh(ctx)
    assert len(rows(ctx, "windows")) == (1 if minutes == 10080 else 0)


def test_discover_private_claude_projects_skips_symlinked_homes_and_other_files(ctx):
    home = ctx._homes_root / "private"
    write(home / "projects" / "nested" / "valid.jsonl", claude("private"))
    write(home / "projects" / "ignored.txt", claude("extension"))
    (home / "projects" / "empty.jsonl").mkdir()
    outside_project = ctx.data_dir / "fake-outside-project"
    write(outside_project / "ignored.jsonl", claude("directory-link"))
    (home / "projects" / "directory-link").symlink_to(outside_project, target_is_directory=True)
    external = ctx.data_dir / "fake-external-home"
    write(external / "projects" / "linked.jsonl", claude("linked"))
    ctx._homes_root.mkdir(parents=True, exist_ok=True)
    (ctx._homes_root / "linked").symlink_to(external, target_is_directory=True)
    refresh(ctx)
    assert totals(ctx) == [10, 4, 3, 5]
    assert [r[0] for r in rows(ctx, "files")] == [str(home / "projects" / "nested" / "valid.jsonl")]


def test_ledger_ignores_end_nonstring_thread_and_invalid_times_and_sorts_binds(ctx):
    bind(ctx, "claude", "a-later@example.test", at="2026-11-21T11:00:00Z")
    bind(ctx, "claude", "z-earlier@example.test", at="2026-11-21T09:00:00Z")
    path = ctx.data_dir / "seat-ledger.jsonl"
    base = dict(v=1, tool="claude", email="bad@example.test", thread="thread", pid=42,
                at="2026-11-21T09:50:00Z", event="bind")
    write(path, {**base, "event": "end"}, {**base, "thread": 42},
          {**base, "thread": ""}, {**base, "at": "invalid"}, append=True)
    write(cp(ctx), claude(), claude("later", at="2026-11-21T12:00:00Z"))
    refresh(ctx)
    assert {r[3] for r in rows(ctx)} == {"claude:z-earlier@example.test", "claude:a-later@example.test"}


@pytest.mark.parametrize("value", [True, False, float("inf"), float("nan"), 10**100])
def test_timestamp_rejects_non_epoch_numbers(value):
    assert tokenlog._timestamp(value) is None


@pytest.mark.parametrize("bad", [True, -1, 1.5, "10"])
def test_invalid_token_counts_do_not_contribute(ctx, bad):
    write(cp(ctx), claude("invalid", n=bad), claude("valid"))
    write(xp(ctx), meta(), count(bad), count(20, at="2026-11-21T11:00:00Z"))
    refresh(ctx)
    assert totals(ctx) == [30, 4, 3, 10]


@pytest.mark.parametrize("end,peak", [(None, 4), (True, 4), (AT, True), (AT, "4"), (AT, float("nan"))])
def test_invalid_windows_are_ignored(ctx, end, peak):
    seats(ctx, a={"usage": {"windows": {"weekly": {"resets_at": end, "used_pct": peak}}}})
    refresh(ctx)
    assert rows(ctx, "windows") == []


def test_zero_counts_do_not_create_empty_seats_or_models(ctx):
    row = claude()
    row["message"]["usage"] = {}
    write(cp(ctx), row)
    refresh(ctx)
    payload = tokenlog.history(ctx)
    assert payload["seats"] == [] and payload["models"] == []


def test_connect_creates_private_database_and_rebuilds_only_unknown_schema(ctx, tmp_path):
    from acctsw.context import Context
    fresh = Context.for_test(tmp_path / "fresh" / "nested")
    db = tokenlog._connect(fresh)
    db.close()
    path = fresh.data_dir / "usage.sqlite"
    path.chmod(0o644)
    tokenlog._connect(fresh).close()
    assert path.stat().st_mode & 0o777 == 0o600
    refresh(ctx)
    assert ("schema_version", "1") in rows(ctx, "meta")
    # A mismatched cache must be discarded even when no log remains to recreate it.
    with sqlite3.connect(ctx.data_dir / "usage.sqlite") as db:
        db.execute("UPDATE meta SET value='obsolete' WHERE key='schema_version'")
        db.execute("INSERT INTO usage VALUES (1,'claude','old','claude:?',1,0,0,0)")
    refresh(ctx)
    assert rows(ctx) == []
    assert ("schema_version", "1") in rows(ctx, "meta")


def test_default_clock_is_timezone_aware_and_current(clock):
    before = datetime.now().astimezone()
    observed = clock()
    after = datetime.now().astimezone()
    assert observed.utcoffset() is not None
    assert before <= observed <= after


def test_concurrent_refresh_skips_without_waiting_for_owner(ctx):
    import threading
    completed = threading.Event()
    failures = []

    def attempt():
        try:
            refresh(ctx)
        except Exception as exc:
            failures.append(exc)
        finally:
            completed.set()

    tokenlog._LOCK.acquire()
    worker = threading.Thread(target=attempt)
    try:
        worker.start()
        skipped = completed.wait(1)
    finally:
        tokenlog._LOCK.release()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert skipped, "a second index request waited for the existing refresh"


def test_refresh_failed_connect_preserves_error_and_releases_lock(ctx, monkeypatch):
    def fail(_ctx):
        raise OSError("fake database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(tokenlog, "_connect", fail)
        with pytest.raises(OSError, match="fake database unavailable"):
            refresh(ctx)
    assert tokenlog._LOCK.acquire(blocking=False)
    tokenlog._LOCK.release()
    refresh(ctx)


def test_chunk_budget_defers_messages_beyond_first_mebibyte(ctx, monkeypatch):
    path = cp(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}\n" * (512 * 1024) + (json.dumps(claude()) + "\n").encode())
    ticks = iter([0, 0, 0, 2])
    with monkeypatch.context() as patch:
        patch.setattr(tokenlog.time, "monotonic", lambda: next(ticks, 2))
        refresh(ctx, 1)
    assert totals(ctx) == [0, 0, 0, 0]
    refresh(ctx)
    assert totals(ctx) == [10, 4, 3, 5]


def test_refresh_does_not_read_past_snapshotted_size_when_file_grows(ctx, monkeypatch):
    import builtins
    path = write(cp(ctx), claude())
    refresh(ctx)
    write(path, claude("second"), append=True)
    original_open = builtins.open

    def growing_open(name, mode="r", *args, **kwargs):
        if str(name) == str(path) and mode == "rb":
            write(path, claude("third"), append=True)
        return original_open(name, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(tokenlog, "open", growing_open, raising=False)
        refresh(ctx)
    assert totals(ctx) == [20, 8, 6, 10]
    refresh(ctx)
    assert totals(ctx) == [30, 12, 9, 15]


@pytest.mark.parametrize("tool,obj", [
    ("claude", ["usage"]), ("codex", ["token_count"]),
    ("codex", {"type": "event_msg", "payload": ["token_count"]}),
])
def test_malformed_records_with_fast_path_keys_are_ignored(ctx, tool, obj):
    if tool == "claude":
        write(cp(ctx), obj, claude())
    else:
        write(xp(ctx), meta(), obj, count(10))
    refresh(ctx)
    assert totals(ctx) == ([10, 4, 3, 5] if tool == "claude" else [10, 0, 0, 5])


def test_non_assistant_and_non_token_events_do_not_count(ctx):
    assistant = claude("user")
    assistant["type"] = "user"
    wrong_kind = count(100)
    wrong_kind["type"] = "other"
    wrong_payload = count(200)
    wrong_payload["payload"]["type"] = "other"
    write(cp(ctx), assistant, claude("valid"))
    write(xp(ctx), count(1000), meta(), wrong_kind, wrong_payload, count(10))
    refresh(ctx)
    assert totals(ctx) == [20, 4, 3, 10]


@pytest.mark.parametrize("user,at", [(None, AT), ("fake-user", None), (None, None)])
def test_codex_tracking_requires_creator_and_timestamp(ctx, user, at):
    write(xp(ctx), meta(user=user, at=at), count(10))
    refresh(ctx)
    assert tokenlog.history(ctx)["tracking"]["codex"] is None
    assert totals(ctx) == [10, 0, 0, 5]


def test_nonstring_account_ids_do_not_break_indexing(ctx):
    seats(ctx, **{"fake@example.test": {"account_id": {"invalid": "identity"}}})
    write(xp(ctx), meta(user="unknown"), count(10))
    refresh(ctx)
    assert rows(ctx)[0][3] == "codex:?"


def test_key_snapshot_windows_use_their_own_harness(ctx):
    state = ctx.load_state()
    state.data["keys"] = {tool: {"id": tool, "harness": tool, "usage": {"windows": {
        "weekly": {"resets_at": AT, "used_pct": 40}}}} for tool in ("codex", "claude")}
    state.save()
    refresh(ctx)
    assert {r[0] for r in rows(ctx, "windows")} == {"codex:codex", "claude:claude"}


def test_own_weeks_snap_to_nearest_among_multiple_older_observations(ctx):
    seats(ctx, a={})
    end = tokenlog._now().timestamp() + 86400
    with tokenlog._connect(ctx) as db:
        for days, peak in [(0, 20), (7 + 2 / 24, 30), (21, 40)]:
            tokenlog._window(db, "codex:a", end - days * 86400, peak)
    periods = tokenlog.history(ctx)["own"]["codex:a"]["week"]
    assert periods[0]["approx"] is False
    assert periods[1]["limit_pct"] == 30
    a = datetime.fromisoformat(periods[0]["from"])
    b = datetime.fromisoformat(periods[0]["to"])
    assert periods[0]["label"] == f"{a:%b} {a.day} – {b - timedelta(days=1):%b} {(b - timedelta(days=1)).day}"


def test_registered_codex_symlinked_home_is_still_discovered(ctx):
    # A registered account has an explicit root even if its home is a relocated directory.
    email = "fake@example.test"
    seats(ctx, **{email: {}})
    relocated = ctx.data_dir / "fake-relocated-home"
    write(relocated / "sessions" / "rollout-relocated.jsonl", meta(), count(10))
    home = ctx.codex_home(email)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.symlink_to(relocated, target_is_directory=True)
    refresh(ctx)
    assert totals(ctx) == [10, 0, 0, 5]


def test_removed_claude_seat_has_no_billing_despite_old_profile(ctx):
    email = "removed@example.test"
    bind(ctx, "claude", email)
    write(cp(ctx), claude())
    profile = ctx.claude_home(email) / ".claude.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"oauthAccount": {"emailAddress": email,
        "subscriptionCreatedAt": "2026-01-14T22:30:00Z"}}))
    refresh(ctx)
    history = tokenlog.history(ctx)
    assert history["seats"][0]["kind"] == "removed"
    assert history["own"]["claude:" + email]["month"] is None


def test_codex_event_can_mention_other_record_types_without_being_skipped(ctx):
    row = count(10)
    row["payload"]["extra"] = ["session_meta", "turn_context"]
    write(xp(ctx), meta(), row)
    refresh(ctx)
    assert totals(ctx) == [10, 0, 0, 5]


def test_calendar_periods_are_contiguous_with_seven_day_weeks(ctx, berlin):
    calendar = tokenlog.history(ctx)["calendar"]
    for kind, count in (("week", 13), ("month", 6)):
        periods = calendar[kind]
        assert len(periods) == count
        assert [p["current"] for p in periods] == [True] + [False] * (count - 1)
        assert all(a["from"] == b["to"] for a, b in zip(periods, periods[1:]))
        for p in periods:
            a, b = (datetime.fromisoformat(p[key]) for key in ("from", "to"))
            if kind == "week":
                assert (b.date() - a.date()).days == 7
            else:
                assert a.day == b.day == 1
                assert b.year * 12 + b.month - (a.year * 12 + a.month) == 1


@pytest.mark.parametrize("ahead", [3, 7])
def test_own_week_uses_upcoming_end_instead_of_nearer_past_anchor(ctx, ahead):
    at = tokenlog._now().timestamp()
    seats(ctx, a={})
    with tokenlog._connect(ctx) as db:
        tokenlog._window(db, "codex:a", at - 86400, 99)
        tokenlog._window(db, "codex:a", at + ahead * 86400, 20)
    p = tokenlog.history(ctx)["own"]["codex:a"]["week"][0]
    assert datetime.fromisoformat(p["to"]).timestamp() == at + ahead * 86400
    assert p["limit_pct"] == 20


def test_billing_oldest_period_extends_before_calendar_history(ctx):
    seats(ctx, **{"fake@example.test": {"account_id": "fake-user",
        "billing": {"renews_at": "2026-01-31T12:00:00Z"}}})
    write(xp(ctx), meta(user="fake-user", at="2026-05-31T10:00:00Z"), turn("older-model"),
          count(10, at="2026-05-31T10:00:00Z"))
    refresh(ctx)
    p = tokenlog.history(ctx)["own"]["codex:fake@example.test"]["month"]["periods"][-1]
    assert p["by"] == {"older-model": [10, 0, 0, 5]}


def test_models_exclude_usage_before_oldest_displayed_period(ctx):
    write(cp(ctx), claude("old", at="2026-05-30T10:00:00Z", model="old-model"),
          claude("recent", model="current-model"))
    refresh(ctx)
    history = tokenlog.history(ctx)
    assert history["models"] == [dict(id="current-model", label="current-model", slot=1)]


def test_schema_rebuild_remains_atomic_for_other_readers(ctx, monkeypatch):
    with tokenlog._connect(ctx) as db:
        db.execute("INSERT INTO usage VALUES (1,'claude','old','claude:?',10,0,0,0)")
        db.execute("UPDATE meta SET value='old-schema' WHERE key='schema_version'")
    connect = sqlite3.connect
    reader = connect(ctx.data_dir / "usage.sqlite")
    observations = []

    class RebuildConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql == "DROP TABLE IF EXISTS usage":
                observations.append(reader.execute("SELECT input FROM usage").fetchall())
            return result

    def connect_rebuild(*args, **kwargs):
        return connect(*args, factory=RebuildConnection, **kwargs)

    try:
        monkeypatch.setattr(tokenlog.sqlite3, "connect", connect_rebuild)
        db = tokenlog._connect(ctx)
        db.close()
        assert observations == [[(10,)]]
        assert reader.execute("SELECT input FROM usage").fetchall() == []
    finally:
        reader.close()


def test_history_uses_one_snapshot_while_refresh_updates_backlog(ctx, monkeypatch):
    monkeypatch.setattr(tokenlog, "_BACKLOG", 0)
    path = write(cp(ctx), claude())
    refresh(ctx)
    write(path, claude("appended"), append=True)
    original_ledger = tokenlog._ledger
    pending = True

    def refresh_during_history(context):
        nonlocal pending
        if pending:
            pending = False
            refresh(context, 0)  # publish a new file size, leaving its fresh tail unread
        return original_ledger(context)

    monkeypatch.setattr(tokenlog, "_ledger", refresh_during_history)
    history = tokenlog.history(ctx)
    assert history["index"] == {"done": True, "pct": 100}
    assert history["calendar"]["week"][0]["by"]["claude:?"]["claude-opus-5-5"] == [10, 4, 3, 5]
    assert tokenlog.index_progress(ctx)["done"] is False
