"""Tests for the Headroom save-credit integration."""
from acctsw import accounts as acct
from acctsw import bridge
from acctsw import headroom
from acctsw import launcher as L
from tests.conftest import make_codex_blob
from tests.test_usage import fake_get


def test_wrap_when_enabled_and_available():
    assert L  # ensure import
    out = headroom.wrap(["codex", "exec"], enabled=True, is_available=True)
    assert out == ["headroom", "wrap", "codex", "exec"]


def test_no_wrap_when_disabled():
    assert headroom.wrap(["codex"], enabled=False, is_available=True) == ["codex"]


def test_no_wrap_when_unavailable():
    assert headroom.wrap(["codex"], enabled=True, is_available=False) == ["codex"]


def _two_codex(ctx):
    for em in ("a@x.com", "b@x.com"):
        ctx.cred["codex"].set_live(make_codex_blob(em))
        acct.add(ctx, ctx.load_state(), "codex", email=em)
    from acctsw.switch import switch
    switch(ctx, ctx.load_state(), "codex", "a@x.com")


class _Spawn:
    def __init__(self, status=0):
        self.calls = []
        self.status = status
    def __call__(self, argv, on_output):
        self.calls.append(list(argv))
        on_output(b"done\n")
        return self.status


def test_launcher_wraps_with_headroom_when_enabled(ctx, monkeypatch):
    _two_codex(ctx)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    monkeypatch.setattr(L.headroom_mod, "available", lambda: True)
    spawn = _Spawn()
    L.run(ctx, "codex", ["--foo"], spawn=spawn, get=fake_get({}), notify=lambda m: None)
    assert spawn.calls[0][:2] == ["headroom", "wrap"]
    assert spawn.calls[0][-1] == "--foo"


def test_launcher_skips_headroom_when_unavailable(ctx, monkeypatch):
    _two_codex(ctx)
    st = ctx.load_state(); st.set_setting("headroom", True); st.save()
    monkeypatch.setattr(L.headroom_mod, "available", lambda: False)
    msgs = []
    spawn = _Spawn()
    L.run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=msgs.append)
    assert spawn.calls[0][:2] != ["headroom", "wrap"]
    assert any("headroom isn't installed" in m for m in msgs)


def test_launcher_no_headroom_when_toggle_off(ctx, monkeypatch):
    _two_codex(ctx)  # headroom defaults off
    monkeypatch.setattr(L.headroom_mod, "available", lambda: True)
    spawn = _Spawn()
    L.run(ctx, "codex", [], spawn=spawn, get=fake_get({}), notify=lambda m: None)
    assert spawn.calls[0][:2] != ["headroom", "wrap"]


def test_bridge_headroom_install_returns_command(ctx):
    r = bridge.handle(ctx, {"action": "headroom_install"})
    assert r["ok"] and r["command"].startswith("pip install")
    assert "available" in r
