"""The bridge must return the fields app.mjs correlates replies by.

This is the gap that let "check key" look broken: app.mjs branches on `res.key_action` in four
places, the bridge never sent it, and both sides were tested in isolation — the node tests build
fake replies that already contain it, the bridge tests assert the bridge's own shape. Neither
suite could see the seam between them. These tests read the field names straight out of app.mjs
so the contract cannot drift again without failing here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from acctsw import bridge, keyseats, pricing, providers

APP_MJS = Path(__file__).resolve().parent.parent / "app" / "web" / "app.mjs"


def _actions_the_ui_correlates() -> set[str]:
    source = APP_MJS.read_text(encoding="utf-8")
    return set(re.findall(r'res\.key_action === "([a-z_]+)"', source))


@pytest.fixture
def seat(ctx, monkeypatch):
    monkeypatch.setattr(pricing, "_default_get", lambda *a, **k: (200, '{"data":[]}'))
    return keyseats.add(ctx, providers.get_provider("openrouter"), "sk-contract-1111",
                        label="t", model="m", get=lambda *_: (200, "{}"))


def test_every_key_reply_names_its_action(ctx, seat, monkeypatch):
    monkeypatch.setattr(pricing, "_default_get", lambda *a, **k: (200, '{"data":[]}'))
    result = bridge.handle(ctx, {"action": "key_validate", "id": seat["id"]})
    assert result["key_action"] == "key_validate"
    assert result["key_target_id"] == seat["id"]


def test_the_ui_never_waits_on_an_action_the_bridge_cannot_name(ctx):
    """Anything app.mjs correlates must be an action the bridge actually stamps."""
    correlated = _actions_the_ui_correlates()
    assert correlated, "expected app.mjs to correlate replies by key_action"
    unknown = correlated - bridge.KEY_ACTIONS
    assert not unknown, f"app.mjs waits on replies the bridge never produces: {sorted(unknown)}"


@pytest.mark.parametrize("action", sorted(_actions_the_ui_correlates()))
def test_each_correlated_action_round_trips_its_name(ctx, seat, action, monkeypatch):
    monkeypatch.setattr(pricing, "_default_get", lambda *a, **k: (200, '{"data":[]}'))
    message = {"action": action, "id": seat["id"]}
    if action == "answer_key_switch":
        message["approved"] = False
    result = bridge.handle(ctx, message)
    # It may legitimately fail (no pending confirmation, wrong harness). What it may never do is
    # come back anonymous, because then the UI cannot tell whose reply it is.
    assert result.get("key_action") == action, f"{action} reply is unattributable: {json.dumps(result, default=str)[:160]}"
