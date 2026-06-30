"""The ``Context`` bundles every external dependency the engine touches.

A real context (``Context.default()``) wires up the macOS keychain + real file locations. Tests
build a context pointing at a temp dir + an in-memory keychain, so no unit test ever reads or
writes real credentials.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import pwd
import shutil
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
    claude_bin: str | None = None  # path to the official `claude` CLI (for identity/usage)
    codex_bin: str | None = None   # path to the official `codex` CLI (for the launcher)
    homes_root: Path | None = None  # per-account Codex homes root (None → P.CODEX_HOMES)
    codex_real: Path | None = None  # the real shared ~/.codex (None → P.CODEX_HOME)

    @property
    def _homes_root(self) -> Path:
        return self.homes_root or P.CODEX_HOMES

    @property
    def _codex_real(self) -> Path:
        return self.codex_real or P.CODEX_HOME

    # --- factory: real system ----------------------------------------------------------------
    @classmethod
    def default(cls) -> "Context":
        keychain = SecurityKeychain()
        # macOS short username from the passwd db (robust under sudo/launchd, unlike $USER).
        username = pwd.getpwuid(os.getuid()).pw_name
        cred = {
            "codex": CodexCredLocation(P.CODEX_AUTH),
            "claude": ClaudeCredLocation(keychain, P.CLAUDE_KEYCHAIN_SERVICE, username),
        }
        return cls(
            data_dir=P.DATA_DIR,
            state_file=P.STATE_FILE,
            backup_dir=P.BACKUP_DIR,
            keychain=keychain,
            keychain_service=P.KEYCHAIN_SERVICE,
            cred=cred,
            claude_bin=shutil.which("claude"),
            codex_bin=shutil.which("codex"),
            homes_root=P.CODEX_HOMES,
            codex_real=P.CODEX_HOME,
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
            homes_root=root / "codex-homes",
            codex_real=root / "codex",
        )

    # --- helpers -----------------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        chmod_dir(self.data_dir, 0o700)
        chmod_dir(self.backup_dir, 0o700)

    def load_state(self) -> State:
        return State.load(self.state_file)

    @contextlib.contextmanager
    def locked(self):
        """Exclusive cross-process lock for read-modify-write of state.json.

        The menubar (a long-running writer) and ``acctsw run``/CLI both mutate state; an flock
        around load→mutate→save prevents lost updates (atomic_write alone only prevents torn files).
        Hold it ONLY around quick state mutations — never across a spawned agent session.
        """
        self.ensure_dirs()
        f = open(self.data_dir / ".lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    def snapshot_key(self, tool: str, email: str) -> str:
        """Keychain account name for our snapshot of a seat: ``<tool>:<email>``."""
        return f"{tool}:{email}"

    # --- per-seat credential store (codex → on-disk per-account home; claude → keychain) -------
    def codex_home(self, email: str) -> Path:
        """The CODEX_HOME directory for a codex seat (used by the launcher)."""
        from . import codexhome
        return codexhome.home_dir(email, self._homes_root)

    def snapshot_get(self, tool: str, email: str) -> str | None:
        if tool == "codex":
            from . import codexhome
            return codexhome.load(email, root=self._homes_root)
        return self.keychain.get(self.keychain_service, self.snapshot_key(tool, email))

    def snapshot_set(self, tool: str, email: str, blob: str) -> None:
        if tool == "codex":
            from . import codexhome
            codexhome.save(email, blob, codex_home=self._codex_real, root=self._homes_root)
        else:
            self.keychain.set(self.keychain_service, self.snapshot_key(tool, email), blob)

    def snapshot_delete(self, tool: str, email: str) -> bool:
        if tool == "codex":
            from . import codexhome
            return codexhome.delete(email, root=self._homes_root)
        return self.keychain.delete(self.keychain_service, self.snapshot_key(tool, email))
