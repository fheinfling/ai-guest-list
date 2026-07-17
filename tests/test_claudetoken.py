"""Unit tests for setup-token validation + keychain merge (acctsw/claudetoken.py, credlocations).

Wire-free: `claude_identity` takes an injected `get=`, so nothing hits the network.
"""
import json

import pytest

from acctsw import claudetoken as ct
from acctsw import paths as P
from acctsw.credlocations import ClaudeCredLocation

TOK = "sk-ant-oat01-" + "a" * 40


def _get(status, body):
    return lambda url, headers, timeout: (status, body)


# --- looks_like_setup_token ---------------------------------------------------------------------

def test_looks_like_setup_token_accepts_and_strips():
    assert ct.looks_like_setup_token(f"  {TOK}\n") == TOK


@pytest.mark.parametrize("bad", ["nope", "sk-ant-api03-" + "x" * 40, "sk-ant-oat", ""])
def test_looks_like_setup_token_rejects(bad):
    assert ct.looks_like_setup_token(bad) is None


# --- claude_identity ----------------------------------------------------------------------------

def test_claude_identity_ok_max():
    body = json.dumps({"account": {"email": "e@x.com", "has_claude_max": True}})
    assert ct.claude_identity(TOK, user_agent="ua", get=_get(200, body)) == ("e@x.com", "max")


def test_claude_identity_ok_pro_and_unknown_plan():
    pro = json.dumps({"account": {"email": "e@x.com", "has_claude_pro": True}})
    assert ct.claude_identity(TOK, get=_get(200, pro)) == ("e@x.com", "pro")
    none = json.dumps({"account": {"email": "e@x.com"}})
    assert ct.claude_identity(TOK, get=_get(200, none)) == ("e@x.com", None)


def test_claude_identity_sends_the_right_request():
    seen = {}

    def get(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        return 200, json.dumps({"account": {"email": "e@x.com", "has_claude_max": True}})

    ct.claude_identity(TOK, user_agent="claude-code/9", get=get)
    assert seen["url"] == P.CLAUDE_PROFILE_URL
    assert seen["headers"]["Authorization"] == f"Bearer {TOK}"
    assert seen["headers"]["anthropic-beta"] == P.CLAUDE_OAUTH_BETA
    assert seen["headers"]["User-Agent"] == "claude-code/9"


@pytest.mark.parametrize("status,body", [(401, ""), (0, ""), (200, "not json"),
                                         (200, "{}"), (200, json.dumps({"account": {}}))])
def test_claude_identity_rejects(status, body):
    assert ct.claude_identity(TOK, get=_get(status, body)) is None


# --- ClaudeCredLocation.merge_token -------------------------------------------------------------

def _shared_item():
    return json.dumps({
        "mcpOAuth": {"plugin:github:x": {"accessToken": "gh", "expiresAt": 1}},
        "claudeAiOauth": {"accessToken": "old", "refreshToken": "r", "expiresAt": 2,
                          "refreshTokenExpiresAt": 3, "scopes": ["user:inference"],
                          "subscriptionType": "max", "rateLimitTier": "t1"}})


def test_merge_token_from_empty():
    blob = json.loads(ClaudeCredLocation.merge_token(None, TOK, "max"))
    o = blob["claudeAiOauth"]
    assert o["accessToken"] == TOK and o["subscriptionType"] == "max" and o["scopes"] == []
    assert "refreshToken" not in o and "refreshTokenExpiresAt" not in o
    assert o["expiresAt"] > 0 and "mcpOAuth" not in blob     # don't invent keys


def test_merge_token_preserves_the_shared_item():
    blob = json.loads(ClaudeCredLocation.merge_token(_shared_item(), TOK, "pro"))
    # the sibling MCP logins survive verbatim
    assert blob["mcpOAuth"] == {"plugin:github:x": {"accessToken": "gh", "expiresAt": 1}}
    o = blob["claudeAiOauth"]
    assert o["accessToken"] == TOK                           # our field replaced
    assert o["scopes"] == ["user:inference"] and o["rateLimitTier"] == "t1"   # unrelated kept
    assert "refreshToken" not in o and "refreshTokenExpiresAt" not in o       # dropped, not nulled
    assert o["subscriptionType"] == "pro"                    # replaced with the profile's plan


def test_merge_token_drops_stale_subscription_when_plan_unknown():
    blob = json.loads(ClaudeCredLocation.merge_token(_shared_item(), TOK, None))
    assert "subscriptionType" not in blob["claudeAiOauth"]   # old tier must not linger


def test_merge_token_tolerates_corrupt_existing():
    blob = json.loads(ClaudeCredLocation.merge_token("not json", TOK, "max"))
    assert blob["claudeAiOauth"]["accessToken"] == TOK
