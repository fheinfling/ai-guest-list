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

# Keychain service that holds our per-account credential snapshots.
KEYCHAIN_SERVICE = "acct-switcher"  # accounts named "codex:<email>" / "claude:<email>"

# --- canonical locations the official tools read ---------------------------------------------
# Codex stores the active account here (honours $CODEX_HOME).
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
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
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"


def ensure_data_dirs() -> None:
    """Create the store directories if missing (0700)."""
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
