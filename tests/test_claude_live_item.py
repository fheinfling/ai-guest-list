"""Claude Code reads its Keychain item verbatim: acctsw must write raw JSON there, never the base64
it uses for its own snapshots. Up to 1.2.0 every Claude seat switch base64-encoded the item, and
every Claude Code started afterwards reported "Not logged in · Please run /login"."""
import json

import pytest

from acctsw import accounts as acct
from acctsw import keychain as kc_mod
from acctsw.credlocations import ClaudeCredLocation
from acctsw.keychain import InMemoryKeychain, KeychainError, SecurityKeychain, _encode
from acctsw.switch import switch
from tests.conftest import make_claude_blob

SVC, ACCT = "Claude Code-credentials", "tester"


def _raw(ctx):
    """What Claude Code itself reads from its item."""
    return ctx.keychain.get_raw(SVC, ACCT)


def test_switching_claude_seats_leaves_raw_json_claude_can_read(ctx, monkeypatch):
    ctx.cred["claude"].set_live(make_claude_blob("max"))
    acct.add(ctx, ctx.load_state(), "claude", email="a@x.com")
    state = ctx.load_state()
    state.upsert_seat("claude", "b@x.com")
    ctx.snapshot_set("claude", "b@x.com", make_claude_blob("team"))
    state.save()
    monkeypatch.setattr(acct.identity, "claude_profile_email", lambda *_args, **_kwargs: "a@x.com")

    switch(ctx, state, "claude", "b@x.com")

    assert json.loads(_raw(ctx))["claudeAiOauth"]["subscriptionType"] == "team"
    # our own snapshots keep their encoding
    assert ctx.keychain.get_raw(ctx.keychain_service, "claude:b@x.com") == _encode(
        make_claude_blob("team"))


def test_security_backend_passes_claude_json_verbatim(monkeypatch):
    calls = []

    class _R:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(argv, **_kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(kc_mod.subprocess, "run", fake_run)
    blob = make_claude_blob("team")
    ClaudeCredLocation(SecurityKeychain(), SVC, ACCT).set_live(blob)
    assert calls[-1][1] == "add-generic-password"
    assert calls[-1][-2:] == ["-w", blob]


def test_get_live_heals_an_item_left_base64_encoded():
    kc = InMemoryKeychain()
    blob = make_claude_blob("team")
    kc.set(SVC, ACCT, blob)  # what 1.2.0's switch left behind
    assert kc.get_raw(SVC, ACCT) != blob

    assert ClaudeCredLocation(kc, SVC, ACCT).get_live() == blob
    assert kc.get_raw(SVC, ACCT) == blob


def test_get_live_leaves_raw_json_untouched():
    kc = InMemoryKeychain()
    blob = make_claude_blob("max")
    kc.set_raw(SVC, ACCT, blob)
    writes = []
    kc.set_raw = lambda *a: writes.append(a)  # type: ignore[method-assign]
    assert ClaudeCredLocation(kc, SVC, ACCT).get_live() == blob
    assert writes == []


def test_get_live_missing_item_is_none():
    assert ClaudeCredLocation(InMemoryKeychain(), SVC, ACCT).get_live() is None


@pytest.mark.parametrize("stored", ["blob-A", _encode("not json"), _encode('["a list"]')])
def test_get_live_never_reinterprets_values_that_are_not_encoded_json(stored):
    kc = InMemoryKeychain()
    kc.set_raw(SVC, ACCT, stored)
    assert ClaudeCredLocation(kc, SVC, ACCT).get_live() == stored
    assert kc.get_raw(SVC, ACCT) == stored


def test_heal_never_overwrites_a_login_that_landed_meanwhile():
    """Claude Code (or a /login) may rewrite the item between our two reads; fresh bytes win."""
    kc = InMemoryKeychain()
    old, fresh = make_claude_blob("max"), make_claude_blob("team")
    kc.set(SVC, ACCT, old)
    real_get_raw = kc.get_raw
    reads = []

    def racing_get_raw(service, account):
        reads.append(1)
        if len(reads) == 2:
            kc.set_raw(SVC, ACCT, fresh)  # the login lands right before the re-check
        return real_get_raw(service, account)

    kc.get_raw = racing_get_raw  # type: ignore[method-assign]
    ClaudeCredLocation(kc, SVC, ACCT).get_live()
    assert real_get_raw(SVC, ACCT) == fresh


def test_heal_failure_still_returns_the_credentials():
    kc = InMemoryKeychain()
    blob = make_claude_blob("team")
    kc.set(SVC, ACCT, blob)

    def refuse(*_a):
        raise KeychainError("locked")

    kc.set_raw = refuse  # type: ignore[method-assign]
    assert ClaudeCredLocation(kc, SVC, ACCT).get_live() == blob


def test_in_memory_keychain_stores_what_security_would():
    kc = InMemoryKeychain()
    kc.set("svc", "acct", "secret")
    assert kc.get_raw("svc", "acct") == _encode("secret")
    assert kc.get("svc", "acct") == "secret"
    kc.set_raw("svc", "acct", '{"a": 1}')
    assert kc.get_raw("svc", "acct") == '{"a": 1}'
    assert kc.get("svc", "acct") == '{"a": 1}'


class _Security:
    """Records `security` invocations and answers `find-generic-password -w` like the real CLI."""

    def __init__(self, monkeypatch, *, stdout="", returncode=0):
        self.calls = []
        self.stdout, self.returncode = stdout, returncode
        monkeypatch.setattr(kc_mod.subprocess, "run", self)

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        return type("R", (), {"returncode": self.returncode, "stdout": self.stdout,
                              "stderr": "denied"})()


def test_security_set_raw_updates_the_named_item_in_place(monkeypatch):
    sec = _Security(monkeypatch)
    SecurityKeychain("/usr/bin/security").set_raw(SVC, ACCT, '{"a": 1}')
    assert sec.calls == [["/usr/bin/security", "add-generic-password", "-U",
                          "-s", SVC, "-a", ACCT, "-w", '{"a": 1}']]


def test_security_set_raw_failure_raises(monkeypatch):
    _Security(monkeypatch, returncode=1)
    with pytest.raises(KeychainError):
        SecurityKeychain().set_raw(SVC, ACCT, "{}")


def test_security_get_raw_returns_stored_bytes_and_get_decodes(monkeypatch):
    blob = make_claude_blob("team")
    sec = _Security(monkeypatch, stdout=_encode(blob) + "\n")
    kc = SecurityKeychain("/usr/bin/security")
    assert kc.get_raw(SVC, ACCT) == _encode(blob)
    assert sec.calls[-1] == ["/usr/bin/security", "find-generic-password",
                             "-s", SVC, "-a", ACCT, "-w"]
    assert kc.get(SVC, ACCT) == blob


def test_security_get_of_a_missing_item_is_none(monkeypatch):
    _Security(monkeypatch, returncode=44)
    assert SecurityKeychain().get_raw(SVC, ACCT) is None
    assert SecurityKeychain().get(SVC, ACCT) is None


def test_get_live_decodes_but_never_rewrites_a_hex_item_it_did_not_write():
    """`security -w` prints multi-line data as hex; rewriting it raw would just loop."""
    import binascii
    kc = InMemoryKeychain()
    blob = '{\n "claudeAiOauth": {"accessToken": "x"}\n}'
    hexed = binascii.hexlify(blob.encode()).decode()
    kc.set_raw(SVC, ACCT, hexed)
    assert ClaudeCredLocation(kc, SVC, ACCT).get_live() == blob
    assert kc.get_raw(SVC, ACCT) == hexed
