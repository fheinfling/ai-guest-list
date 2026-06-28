"""The ``Context`` bundles every external dependency the engine touches.

A real context (``Context.default()``) wires up the macOS keychain + real file locations. Tests
build a context pointing at a temp dir + an in-memory keychain, so no unit test ever reads or
writes real credentials.
"""
from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path

from . import paths as P
from .credlocations import ClaudeCredLocation, CodexCredLocation, CredLocation
from .keychain import InMemoryKeychain, KeychainBackend, SecurityKeychain
from .state import State
from .util import chmod_dir


@dataclass
class Context:
    data_dir: Path
    state_file: Path
    backup_dir: Path
    keychain: KeychainBackend
    keychain_service: str          # service for OUR snapshots
    cred: dict[str, CredLocation]  # tool -> canonical location

    # --- factory: real system ----------------------------------------------------------------
    @classmethod
    def default(cls) -> "Context":
        keychain = SecurityKeychain()
        cred = {
            "codex": CodexCredLocation(P.CODEX_AUTH),
            "claude": ClaudeCredLocation(
                keychain, P.CLAUDE_KEYCHAIN_SERVICE, getpass.getuser()
            ),
        }
        return cls(
            data_dir=P.DATA_DIR,
            state_file=P.STATE_FILE,
            backup_dir=P.BACKUP_DIR,
            keychain=keychain,
            keychain_service=P.KEYCHAIN_SERVICE,
            cred=cred,
        )

    # --- factory: tests ----------------------------------------------------------------------
    @classmethod
    def for_test(cls, root: Path) -> "Context":
        root = Path(root)
        keychain = InMemoryKeychain()
        cred = {
            "codex": CodexCredLocation(root / "codex" / "auth.json"),
            "claude": ClaudeCredLocation(keychain, "Claude Code-credentials", "tester"),
        }
        return cls(
            data_dir=root / ".account-switcher",
            state_file=root / ".account-switcher" / "state.json",
            backup_dir=root / ".account-switcher" / "backups",
            keychain=keychain,
            keychain_service="acct-switcher-test",
            cred=cred,
        )

    # --- helpers -----------------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        chmod_dir(self.data_dir, 0o700)
        chmod_dir(self.backup_dir, 0o700)

    def load_state(self) -> State:
        return State.load(self.state_file)

    def snapshot_key(self, tool: str, email: str) -> str:
        """Keychain account name for our snapshot of a seat: ``<tool>:<email>``."""
        return f"{tool}:{email}"
