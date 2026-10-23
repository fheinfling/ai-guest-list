"""Identity resolution tests — Codex via JWT, Claude via an injected profile transport."""
import pytest

from acctsw.identity import claude_profile_email, live_email
from acctsw.credlocations import codex_jwt_email
from tests.conftest import make_claude_blob, make_codex_blob


def test_codex_live_email_from_jwt(ctx):
    ctx.cred["codex"].set_live(make_codex_blob("a@x.com"))
    assert live_email(ctx, "codex") == "a@x.com"


def test_codex_live_email_none_without_creds(ctx):
    assert live_email(ctx, "codex") is None


@pytest.mark.parametrize("blob", [
    "[]",
    '"not an auth object"',
    '{"tokens": []}',
    '{"tokens": "not an object"}',
    '{"tokens": {"id_token": []}}',
    '{"tokens": {"id_token": "header.W10.sig"}}',
    '{"tokens": {"id_token": "header.W1siaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9hdXRoIl0.sig"}}',
])
def test_codex_jwt_email_rejects_valid_json_with_a_wrong_shape(blob):
    assert codex_jwt_email(blob) is None


def test_claude_profile_email_parses_json():
    get = lambda *_args: (200, '{"account": {"email": "c@x.com"}}')
    assert claude_profile_email(make_claude_blob(), get=get) == "c@x.com"


def test_claude_profile_email_logged_out():
    assert claude_profile_email(make_claude_blob(), get=lambda *_: (401, "")) is None


def test_claude_profile_email_no_binary(monkeypatch):
    from acctsw import usage
    monkeypatch.setattr(usage.shutil, "which", lambda _: None)
    usage._claude_user_agent_for_exe.cache_clear()
    assert claude_profile_email(make_claude_blob(), get=lambda *_:
                                (200, '{"account": {"email": "c@x.com"}}')) == "c@x.com"


def test_claude_profile_email_bad_json():
    assert claude_profile_email(make_claude_blob(), get=lambda *_: (200, "not json")) is None
