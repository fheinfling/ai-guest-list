"""Local Codex access-token expiry is not evidence of a signed-out session."""
import base64
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest

from acctsw import accounts, codexhome, session, usage
from acctsw.util import iso, now
from tests.conftest import make_codex_blob
from tests.test_usage import codex_ok_body


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _blob(expiry, *, refresh="refresh-credential"):
    value = json.loads(make_codex_blob("person@example.test"))
    value["tokens"].update(access_token=_jwt({"exp": expiry}), refresh_token=refresh)
    return json.dumps(value)


@pytest.mark.parametrize("blob", [
    None, "invalid", "null", "[]", "{}", '{"tokens": []}',
    '{"tokens": {"access_token": 1}}', '{"tokens": {"access_token": "broken"}}',
    json.dumps({"tokens": {"access_token": _jwt([])}}),
    json.dumps({"tokens": {"access_token": _jwt({})}}),
    *[_blob(expiry) for expiry in [None, True, "1", 0, -1, float("inf"), float("nan")]],
])
def test_unknown_expiry_is_not_proof(blob):
    assert usage._codex_access_token_expired(blob) is False


def test_expiry_boundary_matches_claude(monkeypatch):
    at = now()
    monkeypatch.setattr(usage, "now", lambda: at)
    assert usage._codex_access_token_expired(_blob(at.timestamp())) is True
    assert usage._codex_access_token_expired(_blob(at.timestamp() + 1)) is False


@pytest.mark.parametrize("refresh", [None, "", "refresh-credential"])
def test_expiry_classification_does_not_require_refresh_credential(refresh):
    result = usage._fetch_for("codex", _blob(1, refresh=refresh), lambda *_: (401, "{}"), None)
    assert result.error == "token_expired"


def test_unexpired_rejection_does_not_trust_provider_prose():
    blob = _blob((now() + timedelta(hours=1)).timestamp())
    result = usage._fetch_for("codex", blob, lambda *_: (401, '{"error":"token expired"}'), None)
    assert result.error == "unauthorized"


@pytest.mark.parametrize("status", [200, 403, 429, 0])
def test_expiry_does_not_override_other_results(status):
    result = usage._fetch_for("codex", _blob(1), lambda *_: (status, codex_ok_body()), None)
    assert result.error != "token_expired"


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("case", ["active", "supervised", "no_refresh", "valid"])
def test_usage_preserves_credentials_when_rotation_is_forbidden(ctx, monkeypatch, live, case):
    email = "person@example.test"
    expiry = (now() + timedelta(hours=1)).timestamp() if case == "valid" else 1
    blob = _blob(expiry, refresh=None if case == "no_refresh" else "refresh-credential")
    state = ctx.load_state()
    seat = state.upsert_seat("codex", email)
    state.set_active("codex", email if case == "active" else None)
    ctx.snapshot_set("codex", email, blob)
    canonical = blob if case == "active" else make_codex_blob("other@example.test")
    ctx.cred["codex"].set_live(canonical)
    state.save()
    monkeypatch.setattr(session, "active_session", lambda *a, **kw:
                        {"email": email} if case == "supervised" else None)

    def no_rotation(*args, **kwargs):
        pytest.fail("usage must not rotate a credential owned by a tool or lacking expiry proof")

    monkeypatch.setattr(usage, "refresh_codex_blob", no_rotation)
    canonical_path = ctx._codex_real / "auth.json"
    canonical_stat = canonical_path.stat()
    monkeypatch.setattr(ctx, "set_live", no_rotation)
    monkeypatch.setattr(ctx.cred["codex"], "set_live", no_rotation)
    get = lambda *_: (401, "{}")
    if live:
        result = usage.refresh_live(ctx, "codex", force=True, get=get)
        state = ctx.load_state()
        seat = state.get_seat("codex", email)
    else:
        result = usage.refresh(ctx, state, "codex", force=True, get=get, post=no_rotation)
    assert result["codex"][email] == ("unauthorized" if case == "valid" else "token_expired")
    assert not seat.get("auth_error")
    assert accounts._seat_view(seat, active=case == "active", at=now())["needs_login"] is False
    assert ctx.snapshot_get("codex", email) == blob
    assert ctx.cred["codex"].get_live() == canonical
    assert canonical_path.stat().st_mtime_ns == canonical_stat.st_mtime_ns


def test_fresh_usage_clears_expiry_error(ctx):
    state = ctx.load_state()
    email = "person@example.test"
    seat = state.upsert_seat("codex", email)
    expired = usage._fetch_for("codex", _blob(1), lambda *_: (401, "{}"), None)
    usage.store_fetch(state, "codex", email, expired)
    renewed = _blob((now() + timedelta(hours=1)).timestamp())
    fresh = usage._fetch_for("codex", renewed, lambda *_: (200, codex_ok_body()), None)
    usage.store_fetch(state, "codex", email, fresh, blob=renewed)
    assert seat["usage"]["ok"] is True
    assert seat["usage"]["error"] is None
    assert seat["usage"]["error_streak"] == 0


@pytest.fixture
def parked_seat(ctx, monkeypatch):
    email = "person@example.test"
    state = ctx.load_state()
    state.upsert_seat("codex", email)
    state.set_active("codex", None)
    ctx.snapshot_set("codex", email, _blob(1))
    ctx.cred["codex"].set_live(make_codex_blob("other@example.test"))
    state.save()
    canonical = ctx._codex_real / "auth.json"
    before = canonical.read_bytes(), canonical.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        pytest.fail("parked recovery must never write the shared ~/.codex mirror")

    monkeypatch.setattr(ctx, "set_live", forbidden)
    monkeypatch.setattr(ctx.cred["codex"], "set_live", forbidden)
    real_write = codexhome.atomic_write_text

    def private_write(path, *args, **kwargs):
        assert not path.resolve().is_relative_to(ctx._codex_real.resolve())
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(codexhome, "atomic_write_text", private_write)
    yield email
    assert (canonical.read_bytes(), canonical.stat().st_mtime_ns) == before


def _renewal_response():
    return 200, json.dumps({"access_token": _jwt({"exp": now().timestamp() + 3600}),
                            "refresh_token": "renewed-refresh"})


def test_parked_recovery_writes_once_and_fetches_new_token_outside_lock(ctx, monkeypatch,
                                                                    parked_seat):
    email = parked_seat
    held = False
    writes = []
    posts = []
    real_locked, real_save = ctx.locked, codexhome.save

    @contextmanager
    def locked():
        nonlocal held
        with real_locked():
            held = True
            try:
                yield
            finally:
                held = False

    def save(*args, **kwargs):
        assert held
        writes.append(args[1])
        return real_save(*args, **kwargs)

    def post(url, payload, timeout):
        assert not held
        assert payload["refresh_token"] == "refresh-credential"
        posts.append(url)
        return _renewal_response()

    def get(url, headers, timeout):
        assert not held
        assert headers["Authorization"] == "Bearer " + json.loads(writes[0])["tokens"]["access_token"]
        return 200, codex_ok_body()

    monkeypatch.setattr(ctx, "locked", locked)
    monkeypatch.setattr(codexhome, "save", save)
    result = usage.refresh_live(ctx, "codex", post=post, get=get)
    assert result["codex"][email] == "ok"
    assert len(posts) == len(writes) == 1
    assert ctx.snapshot_get("codex", email) == writes[0]
    stored = ctx.load_state().get_seat("codex", email)["usage"]
    assert stored["ok"] is True
    assert stored["credential_digest"] == usage._credential_digest(writes[0])
    assert stored["error_streak"] == 0


def test_invalidated_current_refresh_token_requires_login_and_backs_off(ctx, parked_seat):
    email = parked_seat
    before = ctx.snapshot_get("codex", email)
    calls = []

    def post(*args):
        calls.append("post")
        return 401, "{}"

    def get(*args):
        calls.append("get")
        return 401, "{}"

    result = usage.refresh_live(ctx, "codex", post=post, get=get)
    assert result["codex"][email] == "token_expired"
    seat = ctx.load_state().get_seat("codex", email)
    assert seat["auth_error"] == "refresh_token_revoked"
    assert accounts._seat_view(seat, active=False, at=now())["needs_login"] is True
    assert seat["usage"]["error_streak"] == 1
    assert ctx.snapshot_get("codex", email) == before
    assert usage.refresh_live(ctx, "codex", force=True, post=post, get=get)["codex"][email] == "cached"
    assert calls == ["post", "get"]


@pytest.mark.parametrize("outcome", ["success", "invalidated"])
@pytest.mark.parametrize("changed_refresh", [True, False])
def test_refresh_race_preserves_other_writer_and_healthy_state(ctx, parked_seat,
                                                             outcome, changed_refresh):
    email = parked_seat
    other_blob = _blob(now().timestamp() + 7200,
                       refresh="other-refresh" if changed_refresh else "refresh-credential")
    after = {}

    def post(*args):
        ctx.snapshot_set("codex", email, other_blob)
        current = ctx.load_state()
        usage.store_fetch(current, "codex", email,
                          usage.Usage(ok=True, fetched_at=iso(now())), blob=other_blob)
        current.save()
        after["state"] = ctx.state_file.read_bytes()
        return _renewal_response() if outcome == "success" else (401, "{}")

    def no_get(*args):
        pytest.fail("losing a refresh race must skip the stale usage GET")

    result = usage.refresh_live(ctx, "codex", post=post, get=no_get)
    assert result["codex"][email] == "refresh_raced"
    assert ctx.snapshot_get("codex", email) == other_blob
    assert (ctx.codex_home(email) / "auth.json").read_bytes() == other_blob.encode()
    assert ctx.state_file.read_bytes() == after["state"]
    seat = ctx.load_state().get_seat("codex", email)
    assert not seat.get("auth_error")
    assert seat["usage"]["ok"] is True
    assert accounts._seat_view(seat, active=False, at=now())["needs_login"] is False


@pytest.mark.parametrize("outcome", ["success", "invalidated"])
@pytest.mark.parametrize("change", ["session", "active", "readded", "removed"])
def test_recovery_discards_result_when_seat_ownership_changes(ctx, monkeypatch, parked_seat,
                                                            outcome, change):
    email = parked_seat
    before = ctx.snapshot_get("codex", email)
    running = None
    monkeypatch.setattr(session, "active_session", lambda *a, **kw: running)
    gets = []

    def post(*args):
        nonlocal running
        current = ctx.load_state()
        if change == "session":
            running = {"email": email}
        elif change == "active":
            current.set_active("codex", email)
        else:
            current.remove_seat("codex", email)
            if change == "readded":
                current.upsert_seat("codex", email)["added_at"] = "new-incarnation"
        current.save()
        return _renewal_response() if outcome == "success" else (401, "{}")

    def get(url, headers, timeout):
        gets.append(headers["Authorization"])
        assert headers["Authorization"] == "Bearer " + json.loads(before)["tokens"]["access_token"]
        return 401, "{}"

    result = usage.refresh_live(ctx, "codex", post=post, get=get)
    assert result["codex"][email] == ("stale" if change == "removed" else "token_expired")
    assert len(gets) == (0 if change == "removed" else 1)
    assert ctx.snapshot_get("codex", email) == before
    seat = ctx.load_state().get_seat("codex", email)
    assert not (seat or {}).get("auth_error")


@pytest.mark.parametrize("status,body", [(500, "{}"), (0, ""), (200, "not json")])
def test_recovery_errors_use_existing_usage_backoff(ctx, parked_seat, status, body):
    before = ctx.snapshot_get("codex", parked_seat)
    result = usage.refresh_live(ctx, "codex", post=lambda *_: (status, body),
                                get=lambda *_: (401, "{}"))
    assert result["codex"][parked_seat] == "token_expired"
    seat = ctx.load_state().get_seat("codex", parked_seat)
    assert seat["usage"]["error_streak"] == 1
    assert not seat.get("auth_error")
    assert ctx.snapshot_get("codex", parked_seat) == before


@pytest.mark.parametrize("midflight", [False, True])
def test_recovery_obeys_due_even_when_forced_and_rechecks_at_commit(ctx, parked_seat, midflight):
    email = parked_seat
    before = ctx.snapshot_get("codex", email)
    posts = []

    def recent_usage():
        current = ctx.load_state()
        usage.store_fetch(current, "codex", email, usage.Usage(ok=True, fetched_at=iso(now())),
                          blob=before)
        current.save()

    def post(*args):
        assert midflight, "force must not bypass the parked recovery cadence"
        posts.append(True)
        recent_usage()
        return _renewal_response()

    if not midflight:
        recent_usage()
    result = usage.refresh_live(ctx, "codex", force=True, post=post,
                                get=lambda *_: (401, "{}"))
    assert result["codex"][email] == ("cached" if midflight else "token_expired")
    assert len(posts) == int(midflight)
    assert ctx.snapshot_get("codex", email) == before
    assert not ctx.load_state().get_seat("codex", email).get("auth_error")
