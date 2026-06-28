"""Headroom integration — the optional "save credit" toggle.

When enabled, the supervised launcher routes the agent through Headroom
(https://github.com/headroomlabs-ai/headroom), which compresses what the agent reads → fewer
tokens → usage limits are hit more slowly. Headroom is purely a data-path wrapper: it never
touches credentials or the keychain, so toggling it can't affect account state.

It is OPTIONAL: if it isn't installed, the launcher just runs the agent directly and the toggle
shows an install hint in the UI.
"""
from __future__ import annotations

import shutil

# `headroom wrap <command…>` runs the command with Headroom's compression in the data path.
WRAP_PREFIX = ("headroom", "wrap")
INSTALL_COMMAND = 'pip install "headroom-ai[all]"'


def available() -> bool:
    return shutil.which("headroom") is not None


def wrap(argv: list, *, enabled: bool, is_available=None) -> list:
    """Return ``argv`` wrapped with Headroom when the toggle is on AND Headroom is installed.

    ``is_available`` is injectable for tests; defaults to a real ``which`` check.
    """
    ok = available() if is_available is None else is_available
    if enabled and ok:
        return [*WRAP_PREFIX, *argv]
    return argv
