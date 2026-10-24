"""Claude identity follows the token owner, even when the CLI's config label is stale."""
from email.message import Message
import io
import json
import subprocess
import types
import urllib.error
import urllib.request

import pytest

from acctsw import accounts, bridge, identity, usage
from acctsw.errors import CannotIdentify
from tests.conftest import make_claude_blob

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


def _blob(token="tok-g", refresh="tok-refresh-g"):
    data = json.loads(make_claude_blob())
    data["claudeAiOauth"].update(accessToken=token, refreshToken=refresh, expiresAt=1)
    return json.dumps(data)


@pytest.fixture
def profile(ctx, monkeypatch):
    ctx.claude_bin = "fake-claude"
    calls, cli_calls = [], []

    def stale_label(argv, **_kwargs):
        cli_calls.append(argv)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"loggedIn": True, "email": "b@x.com", "subscriptionType": "max"}))

    def get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return 200, json.dumps({"account": {"email": "g@x.com"}})

    monkeypatch.setattr(subprocess, "run", stale_label)
    monkeypatch.setattr(usage, "claude_user_agent", lambda _bin=None: "claude-code/test")
    monkeypatch.setattr(usage, "_default_get", get)
    monkeypatch.setattr(usage, "_default_post", lambda *_args: pytest.fail("no refresh allowed"))
    return calls, cli_calls


def _seats(ctx):
    for email, token in (("b@x.com", "tok-b"), ("g@x.com", "tok-g")):
        ctx.cred["claude"].set_live(_blob(token))
        accounts.add(ctx, ctx.load_state(), "claude", email=email)
    return ctx.load_state()


def test_reconcile_refresh_uses_token_owner_despite_stale_config_label(ctx, profile):
    state = _seats(ctx)
    before_b = ctx.snapshot_get("claude", "b@x.com")
    fresh = _blob("tok-g-rotated", "tok-refresh-g-rotated").replace('"expiresAt": 1', '"expiresAt": 2')
    ctx.cred["claude"].set_live(fresh)

    assert accounts.reconcile_claude(ctx, state) == "g@x.com"
    assert ctx.snapshot_get("claude", "g@x.com") == fresh
    assert ctx.snapshot_get("claude", "b@x.com") == before_b
    assert ctx.load_state().active("claude") == "g@x.com"
    assert len(profile[0]) == 4
    assert profile[1] == []


def test_add_uses_token_owner_despite_stale_config_label(ctx, profile):
    live = _blob()
    ctx.cred["claude"].set_live(live)
    seat = accounts.add(ctx, ctx.load_state(), "claude")
    assert seat["email"] == "g@x.com"
    assert ctx.snapshot_get("claude", "g@x.com") == live
    assert ctx.snapshot_get("claude", "b@x.com") is None
    assert ctx.load_state().active("claude") == "g@x.com"
    assert profile[1] == []


def test_profile_request_contract_and_live_email(ctx, profile):
    ctx.cred["claude"].set_live(_blob())
    assert identity.live_email(ctx, "claude") == "g@x.com"
    assert profile[0] == [(PROFILE_URL, {
        "Authorization": "Bearer tok-g", "Content-Type": "application/json",
        "User-Agent": "claude-code/test",
    }, 10.0)]
    assert profile[1] == []


@pytest.mark.parametrize("response", [
    (401, '{"account":{"email":"g@x.com"}}'),
    (403, '{"account":{"email":"g@x.com"}}'),
    (201, '{"account":{"email":"g@x.com"}}'),
    (0, ""),
    OSError("tok-g-rotated transport failure"),
    TimeoutError("tok-g-rotated timeout"),
    (200, "not json"), (200, None), (200, "[]"),
    (200, '{"account": []}'), (200, '{"account": null}'),
    (200, '{}'), (200, '{"account": {}}'),
    (200, '{"account": {"email": null}}'),
    (200, '{"account": {"email": 7}}'),
    (200, '{"account": {"email": ""}}'),
    (200, '{"account": {"email": "   "}}'),
], ids=["expired", "revoked", "not-200", "network", "exception", "timeout",
        "malformed", "no-body", "list", "account-list", "account-null", "no-account",
        "no-email", "email-null", "email-number", "email-empty", "email-blank"])
def test_unknown_profile_never_writes_any_seat(ctx, monkeypatch, profile, response):
    state = _seats(ctx)
    ctx.cred["claude"].set_live(_blob("tok-g-rotated"))
    before_store = dict(ctx.keychain._store)
    before_state = ctx.state_file.read_bytes()

    def get(*_args):
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(usage, "_default_get", get)
    assert identity.claude_live_identity(ctx).email is None
    assert accounts.reconcile_claude(ctx, state) is None
    with pytest.raises(CannotIdentify, match="could not determine"):
        accounts.add(ctx, state, "claude")
    assert ctx.keychain._store == before_store
    assert ctx.state_file.read_bytes() == before_state
    assert profile[1] == []


@pytest.mark.parametrize("blob", [None, "", "bad json", "[]", '{}',
    '{"claudeAiOauth": []}', '{"claudeAiOauth": {}}',
    '{"claudeAiOauth": {"accessToken": null}}',
    '{"claudeAiOauth": {"accessToken": 5}}',
    '{"claudeAiOauth": {"accessToken": ""}}',
    '{"claudeAiOauth": {"accessToken": "   ", "refreshToken": "tok-refresh"}}',
])
def test_missing_token_is_unknown_without_request(ctx, profile, blob):
    if blob is not None:
        ctx.cred["claude"].set_live(blob)
    resolved = identity.claude_live_identity(ctx)
    assert resolved.blob == blob
    assert resolved.email is None
    assert profile == ([], [])
    before = dict(ctx.keychain._store)
    if blob:
        with pytest.raises(CannotIdentify):
            accounts.add(ctx, ctx.load_state(), "claude")
    assert accounts.reconcile_claude(ctx, ctx.load_state()) is None
    assert ctx.keychain._store == before
    assert not ctx.state_file.exists()


@pytest.mark.parametrize("failure", ["http", "exception"])
def test_identity_failure_never_exposes_credentials(ctx, monkeypatch, profile, capsys, caplog,
                                                   failure):
    token = "tok-sensitive-credential-for-test"
    refresh = "tok-sensitive-refresh-for-test"
    body = json.dumps({"echo": token, "refresh": refresh, "account": {"name": "fake-name"}})
    ctx.cred["claude"].set_live(_blob(token, refresh))

    def get(*_args):
        if failure == "exception":
            raise urllib.error.URLError(body)
        return 401, body

    monkeypatch.setattr(usage, "_default_get", get)
    with pytest.raises(CannotIdentify) as error:
        accounts.add(ctx, ctx.load_state(), "claude")
    result = bridge.handle(ctx, {"action": "snapshot", "tool": "claude"})
    assert result == {"ok": False, "error": "could not determine the account email for claude"}
    output = str(error.value) + repr(result) + repr(capsys.readouterr()) + caplog.text
    for secret in (token, refresh, body, "fake-name"):
        assert secret not in output
    assert not ctx.state_file.exists()
    assert ctx.snapshot_get("claude", "b@x.com") is None


def test_profile_matches_existing_seats_case_insensitively(ctx, monkeypatch, profile):
    state = _seats(ctx)
    monkeypatch.setattr(usage, "_default_get", lambda *_args:
                        (200, '{"account": {"email": "G@X.COM"}}'))
    fresh = _blob("tok-g-new").replace('"expiresAt": 1', '"expiresAt": 2')
    ctx.cred["claude"].set_live(fresh)
    assert accounts.reconcile_claude(ctx, state) == "g@x.com"
    assert accounts.add(ctx, state, "claude")["email"] == "g@x.com"
    assert set(ctx.load_state().accounts("claude")) == {"b@x.com", "g@x.com"}
    assert ctx.snapshot_get("claude", "g@x.com") == fresh


def test_identity_pair_keeps_exact_blob_while_profile_is_in_flight(ctx, monkeypatch, profile):
    state = _seats(ctx)
    old = ctx.cred["claude"].get_live()
    before = dict(ctx.keychain._store)

    def get(*_args):
        ctx.cred["claude"].set_live(_blob("tok-b-new"))
        return 200, '{"account": {"email": "g@x.com"}}'

    monkeypatch.setattr(usage, "_default_get", get)
    resolved = identity.claude_live_identity(ctx)
    assert resolved == identity.ClaudeLiveIdentity(old, "g@x.com")
    assert accounts.reconcile_claude(ctx, state, live_identity=resolved) is None
    for key, value in before.items():
        if key[0] == ctx.keychain_service:
            assert ctx.keychain._store[key] == value


def test_profile_transport_never_follows_redirects(monkeypatch):
    requests = []

    class Redirect(urllib.request.HTTPSHandler):
        def https_open(self, req):
            requests.append(req)
            headers = Message()
            headers["Location"] = "https://other.test/stolen"
            return self.parent.error("http", req, io.BytesIO(b"fake redirect"), 302, "Found",
                                     headers)

    real_builder = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(usage.urllib.request, "build_opener", lambda *handlers: real_builder(
        *handlers, urllib.request.ProxyHandler({}), Redirect()))
    assert usage._default_get(PROFILE_URL, {"Authorization": "Bearer tok-g"}, 10) == (
        302, "fake redirect")
    assert len(requests) == 1
    assert requests[0].full_url == PROFILE_URL
    assert requests[0].get_header("Authorization") == "Bearer tok-g"


def test_add_identifies_exact_blob_it_captures(ctx, monkeypatch, profile):
    old, fresh = _blob("tok-g"), _blob("tok-b")
    ctx.cred["claude"].set_live(old)
    real_get_live = ctx.cred["claude"].get_live
    reads = []

    def racing_get_live():
        value = real_get_live()
        if not reads:
            ctx.cred["claude"].set_live(fresh)
        reads.append(value)
        return value

    def get(_url, headers, _timeout):
        email = "g@x.com" if headers["Authorization"] == "Bearer tok-g" else "b@x.com"
        return 200, json.dumps({"account": {"email": email}})

    monkeypatch.setattr(ctx.cred["claude"], "get_live", racing_get_live)
    monkeypatch.setattr(usage, "_default_get", get)
    assert accounts.add(ctx, ctx.load_state(), "claude")["email"] == "g@x.com"
    assert ctx.snapshot_get("claude", "g@x.com") == old


def test_profile_retains_only_email(ctx, monkeypatch, profile):
    body = {"account": {"email": "g@x.com", "name": "fake-name", "uuid": "fake-id"},
            "organization": {"name": "fake-org"}}
    monkeypatch.setattr(usage, "_default_get", lambda *_args: (200, json.dumps(body)))
    ctx.cred["claude"].set_live(_blob())
    accounts.add(ctx, ctx.load_state(), "claude")
    saved = ctx.state_file.read_text()
    for secret in ("fake-name", "fake-id", "fake-org", "tok-g", "tok-refresh-g"):
        assert secret not in saved
    assert "g@x.com" in saved


def test_install_manifest_identifies_saved_blob_instead_of_current_label(ctx, profile):
    from acctsw import install
    ctx.cred["claude"].set_live(_blob("tok-b"))
    entry = install._entry_from(ctx, "claude", _blob("tok-g"))
    assert entry["email"] == "g@x.com"
    assert profile[0][0][1]["Authorization"] == "Bearer tok-g"


def test_install_registers_exact_profiled_blob(ctx, monkeypatch, profile, tmp_path):
    from acctsw import install
    old = _blob()
    ctx.cred["claude"].set_live(old)

    real_get_live = ctx.cred["claude"].get_live

    def racing_get_live():
        live = real_get_live()
        ctx.cred["claude"].set_live(_blob("tok-b"))
        return live

    def get(_url, headers, _timeout):
        email = "g@x.com" if headers["Authorization"] == "Bearer tok-g" else "b@x.com"
        return 200, json.dumps({"account": {"email": email}})

    # Skip the factory probe so the credential race happens during first-seat registration.
    ctx.keychain.set(ctx.keychain_service, install._backup_account("claude"), old)
    manifest = {"entries": {"claude": {"email": "g@x.com"}}}
    (ctx.backup_dir / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(ctx.cred["claude"], "get_live", racing_get_live)
    monkeypatch.setattr(usage, "_default_get", get)
    install.install(ctx, bin_dir=tmp_path / "bin")
    assert ctx.snapshot_get("claude", "g@x.com") == old
    assert ctx.snapshot_get("claude", "b@x.com") is None


def test_live_identity_accepts_injected_transport_and_uses_cli_user_agent(ctx, monkeypatch):
    ctx.claude_bin = "fake-claude"
    blob = _blob()
    ctx.cred["claude"].set_live(blob)
    binaries, calls = [], []

    def user_agent(binary):
        binaries.append(binary)
        return "claude-code/fake-version"

    def get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return 200, '{"account": {"email": "g@x.com"}}'

    monkeypatch.setattr(usage, "claude_user_agent", user_agent)
    assert identity.claude_live_identity(ctx, get=get) == identity.ClaudeLiveIdentity(blob, "g@x.com")
    assert binaries == ["fake-claude"]
    assert calls == [(PROFILE_URL, {"Authorization": "Bearer tok-g",
                                  "Content-Type": "application/json",
                                  "User-Agent": "claude-code/fake-version"}, 10.0)]
