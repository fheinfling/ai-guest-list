"""The ``Context`` bundles every external dependency the engine touches.

A real context (``Context.default()``) wires up the macOS keychain + real file locations. Tests
build a context pointing at a temp dir + an in-memory keychain, so no unit test ever reads or
writes real credentials.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pwd
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from . import paths as P
from .credlocations import ClaudeCredLocation, CodexCredLocation, CredLocation
from .credlocations import codex_jwt_matches
from .errors import CredentialIdentityMismatch, UnsafeCodexMirror
from .keychain import InMemoryKeychain, KeychainBackend, KeychainError, SecurityKeychain
from .state import State
from .util import chmod_dir


# A GUI app (Finder/launchd-launched) inherits only launchd's minimal PATH (/usr/bin:/bin:…), so the
# Homebrew/npm/~-local dirs where `claude`, `codex` and their `node` runtime live are missing. Then
# shutil.which() returns None and the app can't run the CLIs — so a completed `claude auth login`
# can't be identified into a seat, and usage never polls. Prepend the usual locations so both
# resolution AND running the tool (which itself needs `node` on PATH) work.
_COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                    str(Path.home() / ".local" / "bin"), str(Path.home() / ".npm-global" / "bin"),
                    str(Path.home() / "bin"))


def hydrate_path() -> None:
    """Ensure os.environ['PATH'] includes the common bin dirs a GUI-launched app's PATH omits, so the
    app can find AND run claude/codex (→ node). Idempotent; call once at app startup, before the
    Context/binaries are resolved. No subprocess — cheap and safe on the main thread."""
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [d for d in _COMMON_BIN_DIRS if d and d not in have]
    if extra:
        os.environ["PATH"] = os.pathsep.join([*have, *extra])


def which_tool(name: str) -> str | None:
    """shutil.which that also searches the common bin dirs a GUI app's PATH omits — belt-and-suspenders
    in case a Context is built before `hydrate_path` ran."""
    return shutil.which(name) or shutil.which(
        name, path=os.pathsep.join([os.environ.get("PATH", ""), *_COMMON_BIN_DIRS]))


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
    claude_real: Path | None = None  # shared Claude settings/transcripts, never a seat home
    claude_config_explicit: bool = True  # unset CLAUDE_CONFIG_DIR uses ~/.claude.json instead
    claude_config_value: str | None = None  # exact stock spelling matters to Claude's service hash

    @property
    def _homes_root(self) -> Path:
        return self.homes_root or P.CODEX_HOMES

    @property
    def _codex_real(self) -> Path:
        return self.codex_real or P.CODEX_HOME

    # --- factory: real system ----------------------------------------------------------------
    @classmethod
    def default(cls) -> "Context":
        from .claudehome import service_name
        inherited_claude = os.environ.get("CLAUDE_CONFIG_DIR")
        external_claude = bool(inherited_claude and Path(inherited_claude) == P.CLAUDE_CONFIG_DIR)
        explicit_claude = os.environ.get("ACCTSW_CLAUDE_SHARED_EXPLICIT",
                                         str(int(external_claude))) == "1"
        stock_config = (os.environ.get("ACCTSW_CLAUDE_STOCK_CONFIG",
                                       inherited_claude or str(P.CLAUDE_CONFIG_DIR))
                        if explicit_claude else None)
        keychain = SecurityKeychain()
        # macOS short username from the passwd db (robust under sudo/launchd, unlike $USER).
        username = pwd.getpwuid(os.getuid()).pw_name
        cred = {
            "codex": CodexCredLocation(P.CODEX_AUTH),
            "claude": ClaudeCredLocation(keychain, service_name(
                os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR",
                               stock_config)
            ), username),
        }
        return cls(
            data_dir=P.DATA_DIR,
            state_file=P.STATE_FILE,
            backup_dir=P.BACKUP_DIR,
            keychain=keychain,
            keychain_service=P.KEYCHAIN_SERVICE,
            cred=cred,
            claude_bin=which_tool("claude"),
            codex_bin=which_tool("codex"),
            homes_root=P.CODEX_HOMES,
            codex_real=P.CODEX_HOME,
            claude_real=P.CLAUDE_CONFIG_DIR,
            claude_config_explicit=explicit_claude,
            claude_config_value=stock_config,
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
            homes_root=root / ".account-switcher" / "ch",
            codex_real=root / "codex",
            claude_real=root / "claude",
        )

    # --- helpers -----------------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        chmod_dir(self.data_dir, 0o700)
        chmod_dir(self.backup_dir, 0o700)

    def load_state(self) -> State:
        return State.load(self.state_file)

    def _store_lock_held(self):
        return bool(getattr(self.__dict__.get("_lock_local"), "depth", 0))

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
            local = self.__dict__.setdefault("_lock_local", threading.local())
            local.depth = getattr(local, "depth", 0) + 1
            try:
                yield
            finally:
                local.depth -= 1
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    def snapshot_key(self, tool: str, email: str) -> str:
        """Keychain account name for our snapshot of a seat: ``<tool>:<email>``."""
        return f"{tool}:{email}"

    # --- per-seat homes and snapshots -------------------------------------------------------
    def codex_home(self, email: str) -> Path:
        """The CODEX_HOME directory for a codex seat (used by the launcher)."""
        from . import codexhome
        return codexhome.home_dir(email, self._homes_root)

    @property
    def _claude_real(self) -> Path:
        return (self.claude_real or P.CLAUDE_CONFIG_DIR).absolute()

    @property
    def _claude_config_value(self) -> str | None:
        if not self.claude_config_explicit:
            return None
        return self.claude_config_value if self.claude_config_value is not None else str(self._claude_real)

    def claude_home(self, email: str) -> Path:
        from . import claudehome
        return claudehome.home_dir(email, self.data_dir / "clh")

    def claude_cred(self, email: str) -> ClaudeCredLocation:
        from .claudehome import service_name
        return ClaudeCredLocation(self.keychain, service_name(str(self.claude_home(email))),
                                  self.cred["claude"].account)

    def _codex_mirror_is_private_home(self) -> bool:
        """Whether the canonical Codex writer was accidentally aimed at a seat home.

        Both roots count: a seat still on the pre-1.0.2 path (or reached through the by-address
        index) is just as private as one under the current homes root.
        """
        from . import codexhome
        path = getattr(self.cred["codex"], "auth_path", None)
        if path is None:
            return False
        roots = (self._homes_root, codexhome.index_root(self._homes_root))
        for root in roots:
            try:
                Path(path).resolve().relative_to(root.resolve())
            except ValueError:
                continue
            return True
        return False

    def credential_writer_block(self) -> str | None:
        """Why a newer engine's store forbids this engine to move credentials, or None.

        Reads state.json directly (no defaults, no lock) so even a hot path can afford it. A
        missing or unreadable store is not a newer engine's claim; its own readers report that.
        """
        from .state import writer_block
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return writer_block(data)

    def require_credential_writer(self) -> None:
        """Raise ``EngineTooOld`` instead of writing credentials over a newer engine's store."""
        from .errors import EngineTooOld
        reason = self.credential_writer_block()
        if reason:
            raise EngineTooOld(reason)

    def set_live(self, tool: str, blob: str) -> None:
        """Write canonical credentials, refusing a managed private home as the mirror target."""
        self.require_credential_writer()
        if tool == "codex" and self._codex_mirror_is_private_home():
            raise UnsafeCodexMirror(
                "refusing to write the shared Codex mirror through a private account home; "
                "restart acctsw outside a managed CODEX_HOME"
            )
        self.cred[tool].set_live(blob)

    def snapshot_get(self, tool: str, email: str) -> str | None:
        if tool == "codex":
            from . import codexhome
            blob = codexhome.load(email, root=self._homes_root)
            # Treat an old/corrupted snapshot as unavailable rather than ever installing it under
            # its label.  The caller can re-authenticate that seat without harming another one.
            return blob if blob and codex_jwt_matches(email, blob) else None
        try:
            return self.keychain.get(self.keychain_service, self.snapshot_key(tool, email))
        except Exception:
            raise KeychainError("Claude snapshot could not be read") from None

    def snapshot_set(self, tool: str, email: str, blob: str) -> None:
        if tool == "codex":
            if not codex_jwt_matches(email, blob):
                raise CredentialIdentityMismatch(
                    f"refusing to save Codex credentials for {email}: JWT identity does not match"
                )
            from . import codexhome
            codexhome.save(email, blob, codex_home=self._codex_real, root=self._homes_root)
        else:
            try:
                self.keychain.set(self.keychain_service, self.snapshot_key(tool, email), blob)
            except Exception:
                raise KeychainError("Claude snapshot could not be saved") from None

    def snapshot_delete(self, tool: str, email: str) -> bool:
        if tool == "codex":
            from . import codexhome
            return codexhome.delete(email, root=self._homes_root)
        from . import claudehome
        claudehome.delete(self, email)
        return self.keychain.delete(self.keychain_service, self.snapshot_key(tool, email))
