"""Unit tests for the UI↔engine bridge dispatch (no pyobjc)."""
from acctsw import accounts as acct
from acctsw import bridge
from tests.conftest import make_codex_blob


def _add(ctx, email):
    ctx.cred["codex"].set_live(make_codex_blob(email))
    st = ctx.load_state()
    acct.add(ctx, st, "codex", email=email)


def test_status_returns_state_with_headroom_flag(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "ready"})
    assert r["ok"] is True
    assert "headroom_available" in r["state"]
    assert r["state"]["tools"]["codex"]["active"] == "a@x.com"


def test_shared_account_warning_rides_the_nested_state(ctx):
    """The shared-account warning must land at result['state']['warnings'] — the level the menubar
    reads. Two codex seats on ONE ChatGPT account (same account_id) → exactly one warning there."""
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com", account_id="dup"))
    acct.add(ctx, ctx.load_state(), "codex", email="a@x.com")
    ctx.cred["codex"].set_live(make_codex_blob("a+codex@x.com", account_id="dup"))
    acct.add(ctx, ctx.load_state(), "codex", email="a+codex@x.com")
    r = bridge.handle(ctx, {"action": "usage"})
    assert "warnings" not in r                      # NOT at the top level (the bug we fixed)
    assert len(r["state"]["warnings"]) == 1 and "same account" in r["state"]["warnings"][0]


def test_state_carries_app_version_and_build(ctx):
    _add(ctx, "a@x.com")
    import acctsw
    app = bridge.handle(ctx, {"action": "ready"})["state"]["app"]
    assert app["version"] == acctsw.__version__
    assert app["build"] == "dev"           # source checkout → not a packaged build


def test_build_number_reads_bundle_info_plist(tmp_path, monkeypatch):
    """From inside a packaged *.app, build_number() reads CFBundleVersion from Info.plist."""
    import plistlib
    import acctsw
    appdir = tmp_path / "AI Guest List.app"
    fake_module_file = appdir / "Contents" / "Resources" / "lib" / "acctsw" / "__init__.py"
    fake_module_file.parent.mkdir(parents=True)
    (appdir / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "142"}))
    monkeypatch.setattr(acctsw, "_BUILD_CACHE", None)
    monkeypatch.setattr(acctsw, "__file__", str(fake_module_file))
    assert acctsw.build_number() == "142"


def test_toggle_setting_persists(ctx):
    r = bridge.handle(ctx, {"action": "toggle", "key": "auto_switch", "value": False})
    assert r["ok"] and r["state"]["settings"]["auto_switch"] is False
    assert ctx.load_state().settings()["auto_switch"] is False


def test_switch_action(ctx):
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "a@x.com"})
    assert r["ok"] and r["celebrate"] is True
    assert r["state"]["tools"]["codex"]["active"] == "a@x.com"


def test_switch_unknown_seat_is_friendly_error(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "ghost@x.com"})
    assert r["ok"] is False and "ghost@x.com" in r["error"]


def test_remove_action(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "remove", "tool": "codex", "email": "a@x.com"})
    assert r["ok"] and r["state"]["tools"]["codex"]["seats"] == []


def test_add_returns_login_plan(ctx):
    r = bridge.handle(ctx, {"action": "add", "tool": "claude"})
    assert r["ok"] and r["login"]["tool"] == "claude"
    ids = {m["id"] for m in r["login"]["methods"]}
    assert ids == {"browser", "token"}


def test_snapshot_after_login_adds_seat(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("new@x.com"))
    r = bridge.handle(ctx, {"action": "snapshot", "tool": "codex", "email": "new@x.com"})
    assert r["ok"] and r["added"] == "new@x.com"
    assert "new@x.com" in ctx.load_state().accounts("codex")


def test_missing_field_error(ctx):
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex"})  # no email
    assert r["ok"] is False and "email" in r["error"]


def test_unknown_action(ctx):
    r = bridge.handle(ctx, {"action": "frobnicate"})
    assert r["ok"] is False and "unknown action" in r["error"]


def test_toggle_rejects_non_whitelisted_key(ctx):
    r = bridge.handle(ctx, {"action": "toggle", "key": "theme", "value": True})
    assert r["ok"] is False and "not a toggle" in r["error"]
    # theme remains its default string, not clobbered to a bool
    assert ctx.load_state().settings()["theme"] == "light"


def test_set_theme(ctx):
    assert bridge.handle(ctx, {"action": "set_theme", "value": "dark"})["state"]["settings"]["theme"] == "dark"
    assert bridge.handle(ctx, {"action": "set_theme", "value": "bogus"})["ok"] is False


def test_state_includes_dot_and_recently_switched(ctx):
    _add(ctx, "a@x.com")
    r = bridge.handle(ctx, {"action": "status"})
    assert r["state"]["dot"] in {"green", "amber", "hello", "switched"}
    assert r["state"]["recently_switched"] is False


def test_set_strategy(ctx):
    assert bridge.handle(ctx, {"action": "set_strategy", "value": "most_headroom"})["state"]["settings"]["strategy"] == "most_headroom"
    assert bridge.handle(ctx, {"action": "set_strategy", "value": "bogus"})["ok"] is False


def test_set_savings_level(ctx):
    # persists a valid level (headroom off by default → no proxy restart side effect)
    r = bridge.handle(ctx, {"action": "set_savings_level", "value": "aggressive"})
    assert r["ok"] and r["state"]["settings"]["savings_level"] == "aggressive"
    # rejects an unknown level and leaves the stored value untouched
    assert bridge.handle(ctx, {"action": "set_savings_level", "value": "bogus"})["ok"] is False
    assert ctx.load_state().settings()["savings_level"] == "aggressive"


def test_set_savings_level_kicks_proxy_restart(ctx, monkeypatch):
    """Changing the level always asks restart_proxy to re-apply it (env is read at proxy boot). We do
    NOT gate on the headroom toggle here: restart_proxy self-guards on whether a proxy is actually
    running, so a retained graceful-OFF proxy still picks up the new level."""
    from acctsw import headroom
    calls = []
    monkeypatch.setattr(headroom, "restart_proxy", lambda store=None: calls.append(store) or True)
    # run the background restart inline so the assertion is deterministic
    monkeypatch.setattr(bridge, "_run_async", lambda fn: fn())

    bridge.handle(ctx, {"action": "set_savings_level", "value": "moderate"})
    assert len(calls) == 1
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    bridge.handle(ctx, {"action": "set_savings_level", "value": "conservative"})
    assert len(calls) == 2


def test_savings_level_restart_failure_turns_headroom_off(ctx, monkeypatch):
    """A hard restart failure (False) has already unrouted to avoid a dead port. We first try to roll
    back to the previously-working level (global_enable); only if THAT also fails do we mark the setting
    off to match reality (the health-check's own restarts are for a proxy that DIED, not one that
    won't boot at this level). A no-op (None) leaves it untouched."""
    from acctsw import headroom
    monkeypatch.setattr(bridge, "_run_async", lambda fn: fn())
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()

    monkeypatch.setattr(headroom, "restart_proxy", lambda store=None: None)   # no-op → leave setting on
    bridge.handle(ctx, {"action": "set_savings_level", "value": "moderate"})
    assert ctx.load_state().settings()["headroom"] is True

    # hard failure AND rollback also fails → setting goes off
    monkeypatch.setattr(headroom, "restart_proxy", lambda store=None: False)
    monkeypatch.setattr(headroom, "global_enable", lambda store=None: (False, "nope"))
    bridge.handle(ctx, {"action": "set_savings_level", "value": "aggressive"})
    assert ctx.load_state().settings()["headroom"] is False


def test_savings_level_restart_failure_rolls_back_and_stays_on(ctx, monkeypatch):
    """A failed tier switch must not silently kill save-credit: if the replacement proxy won't boot we
    revert to the previously-working level and re-establish routing (global_enable), keeping the setting
    on. The persisted savings_level is rolled back to the prior value."""
    from acctsw import headroom
    monkeypatch.setattr(bridge, "_run_async", lambda fn: fn())
    st = ctx.load_state()
    st.set_setting("headroom", True); st.set_setting("savings_level", "conservative"); st.save()

    monkeypatch.setattr(headroom, "restart_proxy", lambda store=None: False)
    monkeypatch.setattr(headroom, "global_enable", lambda store=None: (True, "back up"))
    bridge.handle(ctx, {"action": "set_savings_level", "value": "aggressive"})

    settings = ctx.load_state().settings()
    assert settings["headroom"] is True                 # save-credit survived the failed switch
    assert settings["savings_level"] == "conservative"  # rolled back to the prior working level


def test_parse_output_savings_measured_vs_estimated():
    est = "Method:    ESTIMATED (synthetic control)\n  Reduction: 35% (95% CI ...)"
    meas = "Method:    MEASURED (holdout)\n  Reduction: 28% "
    assert bridge._parse_output_savings(est) == (35, False)
    assert bridge._parse_output_savings(meas) == (28, True)
    assert bridge._parse_output_savings("no percent here") == (None, False)
    # anchors to the Reduction line — must NOT surface the holdout fraction / CI bound printed first
    holdout_first = "Holdout: 10% of traffic\nMethod: MEASURED\nReduction: 28% (95% CI 24%-32%)"
    assert bridge._parse_output_savings(holdout_first) == (28, True)
    # decimal reductions round to a whole percent (must not fall through to the trailing "3%")
    assert bridge._parse_output_savings("Method: ESTIMATED\nReduction: 42.3%") == (42, False)


def test_snapshot_flags_proxy_down_when_headroom_on_but_proxy_dead(ctx, monkeypatch):
    """headroom_proxy_down is an honest health signal: setting on + proxy not actually running."""
    from acctsw import headroom
    monkeypatch.setattr(bridge, "headroom_available", lambda: True)
    monkeypatch.setattr(headroom, "proxy_maybe_running", lambda store: False)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    assert bridge.snapshot_state(ctx)["headroom_proxy_down"] is True
    # proxy alive → not down
    monkeypatch.setattr(headroom, "proxy_maybe_running", lambda store: True)
    assert bridge.snapshot_state(ctx)["headroom_proxy_down"] is False
    # headroom off → never "down" regardless of proxy liveness
    st = ctx.load_state(); st.set_setting("headroom", False); st.save()
    monkeypatch.setattr(headroom, "proxy_maybe_running", lambda store: False)
    assert bridge.snapshot_state(ctx)["headroom_proxy_down"] is False


def test_headroom_report_is_cached_and_nonblocking(monkeypatch):
    """snapshot runs on every UI action, so _headroom_report must return last-known values without
    fetching inline: the first call kicks a background refresh, later calls reuse the cache."""
    fetches = {"savings": 0, "stats": 0}
    monkeypatch.setattr(bridge, "headroom_savings", lambda: (fetches.__setitem__("savings", fetches["savings"] + 1) or (30, True)))
    monkeypatch.setattr(bridge, "headroom_stats", lambda: (fetches.__setitem__("stats", fetches["stats"] + 1) or {"tokens_saved": 1}))
    monkeypatch.setattr(bridge, "_run_async", lambda fn: fn())  # run the refresh inline, deterministically
    bridge._HR_CACHE.update(at=None, savings=(None, False), stats=None, refreshing=False)

    # first call returns the last-known (empty) value WITHOUT blocking, and kicks one background
    # refresh that populates the cache — the UI polls again and picks the fresh value up next time.
    assert bridge._headroom_report() == ((None, False), None)
    assert fetches == {"savings": 1, "stats": 1}
    # second call within the TTL: served from the now-populated cache, no new fetch
    assert bridge._headroom_report() == ((30, True), {"tokens_saved": 1})
    assert fetches == {"savings": 1, "stats": 1}


def test_parse_stats_extracts_lifetime_totals():
    data = {"summary": {"compression": {"total_tokens_saved_with_rtk": 12748404},
                        "cost": {"total_saved_usd": 63.74}}}
    assert bridge._parse_stats(data) == {"tokens_saved": 12748404, "usd_saved": 63.74}
    assert bridge._parse_stats({}) is None
    assert bridge._parse_stats({"summary": {}}) is None
    # /stats is untrusted (a foreign process could squat the port) and the values are rendered into
    # the WebView — non-numeric fields must be dropped, never passed through toward innerHTML.
    evil = {"summary": {"compression": {"total_tokens_saved_with_rtk": "<img src=x onerror=alert(1)>"},
                        "cost": {"total_saved_usd": True}}}
    assert bridge._parse_stats(evil) is None
    mixed = {"summary": {"compression": {"total_tokens_saved_with_rtk": 500},
                         "cost": {"total_saved_usd": "nope"}}}
    assert bridge._parse_stats(mixed) == {"tokens_saved": 500, "usd_saved": None}
    # valid-but-wrong-shaped JSON (nested value not an object) must read as "no stats", never raise
    assert bridge._parse_stats({"summary": "ok"}) is None
    assert bridge._parse_stats({"summary": {"compression": "x", "cost": 3}}) is None
    # top-level not even an object (older/foreign process returns a list or string) → None, never raise
    assert bridge._parse_stats(["not", "an", "object"]) is None
    assert bridge._parse_stats("nope") is None


def test_headroom_report_refresh_clears_refreshing_even_on_error(monkeypatch):
    """A raising fetch must not wedge the cache: `refreshing` has to clear so later snapshots retry."""
    monkeypatch.setattr(bridge, "headroom_savings", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bridge, "headroom_stats", lambda: None)
    bridge._HR_CACHE.update(at=None, savings=(1, True), stats={"tokens_saved": 1}, refreshing=True)
    bridge._refresh_headroom_report()
    assert bridge._HR_CACHE["refreshing"] is False
    assert bridge._HR_CACHE["at"] is not None            # stamped → backs off for a TTL, no hammering


def test_switch_sets_recently_switched_dot(ctx):
    _add(ctx, "a@x.com")
    _add(ctx, "b@x.com")
    r = bridge.handle(ctx, {"action": "switch", "tool": "codex", "email": "a@x.com"})
    assert r["state"]["recently_switched"] is True
    assert r["state"]["dot"] == "switched"


def test_paste_installs_and_registers_codex(ctx):
    blob = make_codex_blob("pasted@x.com")
    r = bridge.handle(ctx, {"action": "paste", "tool": "codex", "blob": blob})
    assert r["ok"] and r["added"] == "pasted@x.com"
    assert "pasted@x.com" in ctx.load_state().accounts("codex")
    import json
    assert json.loads(ctx.cred["codex"].get_live())  # live creds installed


def test_is_native_routing():
    assert bridge.is_native("quit") and bridge.is_native("login") and bridge.is_native("settings")
    assert not bridge.is_native("switch") and not bridge.is_native("status")
    assert not bridge.is_native("headroom_install")  # engine-routed (returns command)


def test_toggle_headroom_off_keeps_proxy_alive_for_open_sessions(ctx, monkeypatch):
    """Toggle-OFF is graceful: unroute new sessions but NEVER reap the proxy — open agents pinned to
    its port must keep working (requirement: 'off' must not disconnect running agents)."""
    from acctsw import headroom
    seen = {}
    monkeypatch.setattr(headroom, "global_disable",
                        lambda store=None, **k: seen.update(k) or (True, "ok"))
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    res = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": False})
    assert res["ok"] is True
    assert seen.get("reap_proxy") is False
    assert ctx.load_state().settings()["headroom"] is False


def test_toggle_headroom_on_clears_auto_off_event(ctx, monkeypatch):
    """Re-enabling save-credit makes the auto-off banner stale — it must go in the same write."""
    from acctsw import headroom
    monkeypatch.setattr(headroom, "global_enable", lambda store=None: (True, "up"))
    st = ctx.load_state()
    st.data["headroom_event"] = {"at": "2026-07-02T00:00:00+00:00", "reason": "x"}
    st.set_setting("headroom", False); st.save()
    res = bridge.handle(ctx, {"action": "toggle", "key": "headroom", "value": True})
    assert res["ok"] is True
    assert ctx.load_state().data.get("headroom_event") is None


def test_headroom_event_dismiss_action(ctx):
    st = ctx.load_state()
    st.data["headroom_event"] = {"at": "2026-07-02T00:00:00+00:00", "reason": "r"}
    st.save()
    res = bridge.handle(ctx, {"action": "headroom_event_dismiss"})
    assert res["ok"] is True
    assert ctx.load_state().data.get("headroom_event") is None
    assert res["state"]["headroom_event"] is None


def test_snapshot_exposes_headroom_event(ctx):
    st = ctx.load_state()
    st.data["headroom_event"] = {"at": "2026-07-02T00:00:00+00:00", "reason": "the proxy kept crashing"}
    st.save()
    assert bridge.snapshot_state(ctx)["headroom_event"]["reason"] == "the proxy kept crashing"


def test_savings_level_double_failure_records_auto_off_event(ctx, monkeypatch):
    """The level-change rollback path runs on a silent background thread — when it flips the setting
    off it MUST leave the persistent banner, or the user finds out hours later."""
    from acctsw import headroom
    monkeypatch.setattr(bridge, "_run_async", lambda fn: fn())
    monkeypatch.setattr(headroom, "restart_proxy", lambda store=None: False)
    monkeypatch.setattr(headroom, "global_enable", lambda store=None: (False, "nope"))
    reconciled = []
    monkeypatch.setattr(headroom, "reconcile", lambda c, blocking=True: reconciled.append(c) or (False, ""))
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    bridge.handle(ctx, {"action": "set_savings_level", "value": "aggressive"})
    s = ctx.load_state()
    assert s.settings()["headroom"] is False
    assert "savings level" in s.data["headroom_event"]["reason"]
    assert reconciled  # a partially-failed rollback must not leave routing injected unverified
