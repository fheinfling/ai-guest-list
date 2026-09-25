"""Canonical paths, identifiers, and endpoints used across the engine.

Centralised so every milestone references the same constants (no magic strings scattered around).
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

# --- our store -------------------------------------------------------------------------------
DATA_DIR = HOME / ".account-switcher"
STATE_FILE = DATA_DIR / "state.json"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_MANIFEST = BACKUP_DIR / "manifest.json"
APP_SRC_DIR = DATA_DIR / "app"
# Per-account Codex homes (each a CODEX_HOME with its own auth.json; shared state symlinked to the
# real ~/.codex). Isolation so codex maintains each account's token lifecycle independently.
# Two letters, and a hashed directory per seat, because a CODEX_HOME must stay short enough for the
# app-server daemon's unix socket to fit in SUN_LEN — see codexhome.MAX_HOME_LEN.
CODEX_HOMES = DATA_DIR / "ch"
# Pre-1.0.2 homes lived here, one directory per address. Now the by-address index: a symlink per
# seat into CODEX_HOMES, so `ls ~/.account-switcher/codex-homes/` still answers "which seats?".
CODEX_HOMES_LEGACY = DATA_DIR / "codex-homes"

# Keychain service that holds our per-account credential snapshots.
KEYCHAIN_SERVICE = "acct-switcher"  # accounts named "codex:<email>" / "claude:<email>"

# --- canonical locations the official tools read ---------------------------------------------
def _contains(root: Path, path: Path) -> bool:
    """Whether ``path`` lies inside ``root`` — purely by name, so a path that no longer exists (or
    never did) is still recognised as one of ours."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical_codex_home() -> Path:
    """Return the shared Codex home, never one of acctsw's private seat homes.

    ``launcher.run`` points its own process at a seat-specific ``CODEX_HOME`` while Codex runs.
    A child process can inherit that value and later construct a fresh ``Context.default()`` (for
    example, ``cx --version``).  Treating the inherited private home as the canonical mirror lets a
    normal switch overwrite that seat with another account's valid snapshot.  External custom
    ``CODEX_HOME`` values remain supported; only our managed per-seat subtrees are rejected — both
    of them, and both lexically and resolved. An inherited value can name a home in the pre-1.0.2
    layout, or reach the current one through its by-address symlink, or name a path that no longer
    exists at all (``resolve`` then resolves nothing); none of those is a user's own custom home.
    The test stays anchored on the two managed roots rather than the whole store, so a custom home a
    developer parks elsewhere under ``~/.account-switcher`` is still honoured.
    """
    inherited = os.environ.get("CODEX_HOME")
    if not inherited:
        return HOME / ".codex"
    candidate = Path(inherited).expanduser()
    for root in (CODEX_HOMES, CODEX_HOMES_LEGACY):
        if _contains(root, candidate) or _contains(root.resolve(), candidate.resolve()):
            return HOME / ".codex"
    return candidate


# Codex normally stores the active account here (and may honour an external $CODEX_HOME).
CODEX_HOME = _canonical_codex_home()
CODEX_AUTH = CODEX_HOME / "auth.json"
CODEX_SESSIONS = CODEX_HOME / "sessions"

# Claude stores the active account in the macOS Keychain under this generic-password service.
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))

# --- usage endpoints (see docs/PLAN.md for header requirements) ------------------------------
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_ACCOUNTS_URL = "https://chatgpt.com/backend-api/accounts"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Required Claude headers — without these the endpoint returns 401 / aggressive 429s.
#   anthropic-beta: oauth-2025-04-20      (else 401)
#   User-Agent: claude-code/<version>     (else aggressive 429 bucket)
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_USER_AGENT_FALLBACK = "claude-code/2.1.0"  # used if `claude --version` can't be read
ANTHROPIC_VERSION = "2023-06-01"

# Codex usage requires identifying the ChatGPT account via this header.
CODEX_ACCOUNT_ID_HEADER = "ChatGPT-Account-Id"

# Usage caching.  The active seats are the values a person is watching in the popover, so they may
# refresh on the visible 30-second cadence.  Parked seats retain the gentler 2.5-minute cadence.
# Failed requests always use the parked-seat base for exponential backoff; opening the popover must
# never turn an Anthropic 429 into a retry storm.
USAGE_ACTIVE_REFRESH_SECONDS = 30
USAGE_MIN_REFRESH_SECONDS = 150  # parked seats / error-backoff base (~2.5 min)


def ensure_data_dirs() -> None:
    """Create the store directories if missing (0700)."""
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
