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
CODEX_HOMES = DATA_DIR / "codex-homes"

# Keychain service that holds our per-account credential snapshots.
KEYCHAIN_SERVICE = "acct-switcher"  # accounts named "codex:<email>" / "claude:<email>"

# --- canonical locations the official tools read ---------------------------------------------
def _canonical_codex_home() -> Path:
    """Return the shared Codex home, never one of acctsw's private seat homes.

    ``launcher.run`` points its own process at a seat-specific ``CODEX_HOME`` while Codex runs.
    A child process can inherit that value and later construct a fresh ``Context.default()`` (for
    example, ``cx --version``).  Treating the inherited private home as the canonical mirror lets a
    normal switch overwrite that seat with another account's valid snapshot.  External custom
    ``CODEX_HOME`` values remain supported; only our managed per-seat subtree is rejected.
    """
    inherited = os.environ.get("CODEX_HOME")
    if not inherited:
        return HOME / ".codex"
    candidate = Path(inherited).expanduser()
    try:
        candidate.resolve().relative_to(CODEX_HOMES.resolve())
    except ValueError:
        return candidate
    return HOME / ".codex"


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
