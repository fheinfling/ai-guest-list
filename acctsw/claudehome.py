"""Claude subscription homes: private OAuth item and identity, shared user workspace.

clh/<hashed email> is separate from Codex/BYOK's ch/<id>. Claude hashes the NFC config-dir
STRING (no realpath, slash stripping or expansion) for its Keychain service; always pass the exact
absolute home spelling we hash. Only Claude spends refresh tokens. Existing items always win over
migration snapshots, including after a crash; a launch never reinstalls an older refresh token.

Settings, skills, agents, plugins, hooks, CLAUDE.md, projects/transcripts and other ordinary entries
are symlinked to the user's real config dir. Reuse Codex's promotion/parking for atomic-renamed
shared files, only when no supervised Claude child is alive. Missing shared directories are created
before linking so a first child cannot grow a private transcript/skills tree.

.claude.json (and legacy .config.json), credentials, account/policy caches and daemon/job runtime
are PRIVATE. A global-config symlink would share oauthAccount and undo credential isolation.
Instead three-way merge an explicit set of user configuration fields at launch/exit: MCP servers,
project intent (trust/permissions/MCP), onboarding and UI preferences. Session metrics, tip counters
and unrecognised fields stay private. A private baseline records shared intent each home last saw.
Unrelated concurrent edits merge recursively; canonical changes win conflicts for that key only,
without parking or replacing identity. Unrecognised fields stay private, so new identity
fields cannot leak between seats. Config labels are UI hints and NEVER identity proof. Identity is copied only from a matching captured login; migrated
snapshots get an email hint until Claude obtains the full profile. No account UUID is invented.

Migration verifies the snapshot's token owner via the OAuth profile endpoint before seeding.
A proven mismatch flags auth_error and quarantines the seat without installing those bytes.
An inconclusive (often expired) snapshot may seed once, marked unverified; after the first child
use the profile is retried, and a mismatch quarantines the home and flags re-add. Never refresh to
learn an owner. A proven same-seat stock item competes with a matching/unknown snapshot by finite
expiresAt; use stock only when it is provably newer (or byte-identical). Existing home items win.
Resolve migration plans before taking the store lock; recheck snapshot/stock bytes before seeding.
Same-account rotation coordination lives in claudesync; .claude.json is only for Claude's UI.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
import unicodedata

from . import codexhome
from .errors import AcctswError, CredentialIdentityMismatch, MissingSnapshot
from .util import atomic_write_text

PRIVATE = frozenset({
    ".claude.json", ".config.json", ".claude.json.backup", ".credentials.json",
    ".acctsw-shared.json", "backups", "statsig", "remote-settings.json", "policy-limits.json",
    ".acctsw-seeded",
    "managed-settings.json", "hfi-auth.json", ".session_ingress_token", "daemon", "daemon.json",
    "jobs", "teams", "usage-data",
})
SHARED_CONFIG = frozenset({
    "projects", "mcpServers", "hasCompletedOnboarding", "lastOnboardingVersion", "theme",
    "preferredNotifChannel", "editorMode", "verbose", "model", "autoUpdates",
    "shiftEnterKeyBindingInstalled", "hasCompletedProjectOnboarding",
})
# Only explicit user choices cross project boundaries. In particular last*, exampleFiles*,
# tipsHistory and future/unknown caches are not shared, even when two sessions use the same cwd.
# Lists are intent values (not sets): concurrent differing edits resolve canonical-wins.
SHARED_PROJECT_CONFIG = frozenset({
    "allowedTools", "hasTrustDialogAccepted", "mcpServers", "enabledMcpjsonServers",
    "disabledMcpjsonServers", "disabledMcpServers", "mcpContextUris",
    "hasCompletedProjectOnboarding", "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
})
SHARED_DIRS = ("projects", "skills", "agents", "plugins", "commands", "hooks", "tasks", "todos",
               "plans", "file-history", "session-env", "shell-snapshots", "debug", "ide")


def _private(name: str) -> bool:
    return (name in PRIVATE or name.startswith((".claude.json", ".config.json", ".credentials",
                                               ".acctsw-", "remote-settings", "policy-limits")))


def service_name(config_dir: str | None = None) -> str:
    """Verified in Claude 2.1.309's vj(): SHA256(NFC(path)), first eight hex, raw spelling."""
    suffix = ("-" + hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode("utf-8"))
              .hexdigest()[:8]) if config_dir else ""
    return "Claude Code-credentials" + suffix


def home_dir(email: str, root: Path) -> Path:
    return codexhome.home_dir(email, root).absolute()


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Claude configuration is not an object: {path}")
    return value


def config_path(real: Path, *, explicit: bool = True) -> Path:
    """Claude still prefers a nonempty legacy .config.json to .claude.json."""
    legacy = real / ".config.json"
    if legacy.is_file() and legacy.stat().st_size:
        return legacy
    return real / ".claude.json" if explicit else real.parent / ".claude.json"


def _shared(value: dict) -> dict:
    result = {k: v for k, v in value.items() if k in SHARED_CONFIG and k != "projects"}
    if isinstance(value.get("projects"), dict):
        result["projects"] = {
            name: intent for name, entry in value["projects"].items()
            if isinstance(entry, dict)
            and (intent := {k: v for k, v in entry.items() if k in SHARED_PROJECT_CONFIG})
        }
    return result


def _apply_shared(value: dict, shared: dict) -> dict:
    """Replace shared intent, preserving this file's private top-level and project fields."""
    result = {k: v for k, v in value.items() if k not in SHARED_CONFIG}
    result.update(shared)
    projects = value.get("projects")
    if isinstance(projects, dict):
        private = {name: {k: v for k, v in entry.items() if k not in SHARED_PROJECT_CONFIG}
                   if isinstance(entry, dict) else entry for name, entry in projects.items()}
        for name, intent in shared.get("projects", {}).items():
            entry = private.get(name)
            private[name] = {**(entry if isinstance(entry, dict) else {}), **intent}
        result["projects"] = private
    return result


@contextmanager
def _config_lock(source: Path):
    """Claude 2.1.309 proper-lockfile: mkdir, stale >10s, heartbeat every 5s, rmdir.

    Callers hold acctsw's store lock: never wait for Claude here. Contention defers the entire
    merge, including its baseline, so a later launch/exit retries the seat's pending intent.
    """
    lock = Path(str(source) + ".lock")
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            if lock.stat().st_mtime >= time.time() - 10:
                yield False
                return
            lock.rmdir()
            lock.mkdir()
        except (FileNotFoundError, FileExistsError):
            yield False
            return
    stopped = threading.Event()
    compromised = threading.Event()
    owned = lock.stat()

    def still_owned():
        try:
            now = lock.stat()
            return (now.st_dev, now.st_ino) == (owned.st_dev, owned.st_ino)
        except FileNotFoundError:
            return False

    def refresh():
        while not stopped.wait(5):
            try:
                if not still_owned():
                    compromised.set()
                    return
                os.utime(lock, None)
            except OSError:
                compromised.set()
                return

    heartbeat = threading.Thread(target=refresh, daemon=True)
    try:
        heartbeat.start()
        yield lambda: not compromised.is_set() and still_owned()
    finally:
        stopped.set()
        heartbeat.join()
        if still_owned():
            lock.rmdir()


def _merge(base: dict, local: dict, current: dict) -> tuple[dict, bool]:
    """Apply only a home's changes since its baseline; preserve concurrent canonical edits."""
    result = dict(current)
    conflict = False
    absent = object()
    for key in base.keys() | local.keys():
        before, ours, theirs = base.get(key, absent), local.get(key, absent), current.get(key, absent)
        if ours == before:
            continue
        if ((before is absent or isinstance(before, dict))
                and isinstance(ours, dict) and isinstance(theirs, dict)):
            result[key], collided = _merge({} if before is absent else before, ours, theirs)
            conflict |= collided
        elif theirs == before or theirs == ours:
            if ours is absent:
                result.pop(key, None)
            else:
                result[key] = ours
        else:
            conflict = True
    return result, conflict


def merge_config(ctx, email: str, *, capture: bool = False) -> None:
    """Caller holds the store lock; defer on Claude lock contention without advancing baseline."""
    home = ctx.claude_home(email)
    path, baseline = home / ".claude.json", home / ".acctsw-shared.json"
    source = config_path(ctx._claude_real, explicit=ctx.claude_config_explicit)
    local, base = _read(path), _read(baseline)
    with _config_lock(source) as owned:
        # Even on the first launch a busy canonical config can seed identity/intent read-only.
        # Existing homes stay byte-for-byte intact; no pending edits or baseline can be lost.
        if not owned:
            if path.exists():
                return
            canonical = _read(source)
            shared = _shared(canonical)
        else:
            canonical = _read(source)  # mandatory re-read AFTER mkdir, before read-modify-write
            shared, _ = _merge(base, _shared(local), _shared(canonical))
            if not owned():
                return
            if shared != _shared(canonical):
                canonical = _apply_shared(canonical, shared)
                atomic_write_text(source, json.dumps(canonical, indent=2), mode=0o600)
    local = _apply_shared(local, shared)
    account = canonical.get("oauthAccount") or {}
    if (capture or not path.exists()) and account.get("emailAddress", "").casefold() == email.casefold():
        local["oauthAccount"] = account
    elif capture or (not path.exists() and "oauthAccount" not in local):
        local["oauthAccount"] = {"emailAddress": email}
    text = json.dumps(local, indent=2)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        atomic_write_text(path, text, mode=0o600)
    text = json.dumps(shared)
    if not baseline.exists() or baseline.read_text(encoding="utf-8") != text:
        atomic_write_text(baseline, text, mode=0o600)


def prepare(ctx, email: str) -> None:
    """Resolve a migration plan with no store lock; unchanged inputs reuse their exact-byte proof."""
    from . import identity
    from .claudesync import newest
    if ctx.claude_cred(email).get_live() is not None:
        return
    if ctx._store_lock_held():
        raise RuntimeError("Claude migration requires an unlocked store")
    snapshot, stock = ctx.snapshot_get("claude", email), ctx.cred["claude"].get_live()
    plans = ctx.__dict__.setdefault("_claude_migrations", {})
    old = plans.get(email)
    if old and old[:2] == (snapshot, stock):
        return
    owner = identity.claude_profile_email(snapshot, ctx.claude_bin) if snapshot else None
    from .claudesync import cached_identity
    stock_answer = cached_identity(ctx) if stock else None
    stock_owner = stock_answer.email if stock_answer and stock_answer.blob == stock else None
    seed, proven = snapshot, bool(owner)
    mismatch = bool(owner and owner.casefold() != email.casefold())
    if not mismatch and stock_owner and stock_owner.casefold() == email.casefold():
        candidate = newest([snapshot, stock]) if snapshot else stock
        if candidate == stock:
            seed, proven = stock, True
    plans[email] = (snapshot, stock, seed, proven, mismatch)


def prepare_all(ctx) -> None:
    for email in ctx.load_state().accounts("claude"):
        prepare(ctx, email)


def ensure_home(ctx, email: str, *, promote: bool = False, capture: bool = False) -> Path:
    home, real = ctx.claude_home(email), ctx._claude_real
    if not capture and not ctx._store_lock_held():
        prepare(ctx, email)
    if not capture and (home / ".acctsw-quarantined").exists():
        raise CredentialIdentityMismatch("Claude seat needs re-add")
    if home.parent.is_symlink() or home.is_symlink():
        raise CredentialIdentityMismatch("Claude seat home must be a private directory")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    # A former/manual symlink must never make the identity merge overwrite another home's config.
    for name in PRIVATE:
        entry = home / name
        if entry.is_symlink():
            entry.unlink()
    if promote:
        codexhome._heal(home, real, private=_private)
    for name in SHARED_DIRS:
        (real / name).mkdir(parents=True, exist_ok=True)
    for entry in real.iterdir():
        if _private(entry.name) or codexhome.ORPHAN_MARK in entry.name:
            continue
        link = home / entry.name
        if link.is_symlink() and link.resolve() != entry.resolve():
            link.unlink()
        if not link.exists() and not link.is_symlink():
            link.symlink_to(entry)
    merge_config(ctx, email, capture=capture)
    item = ctx.claude_cred(email)
    if item.get_live() is None:
        if (home / ".acctsw-seeded").exists() and not capture:
            raise MissingSnapshot(f"credentials removed from claude:{email} home — re-add this seat")
        blob = ctx.snapshot_get("claude", email)
        if not capture:
            plan = ctx.__dict__.get("_claude_migrations", {}).get(email)
            if (not plan or plan[:2] != (blob, ctx.cred["claude"].get_live())):
                raise MissingSnapshot("Claude migration changed — retry outside the store lock")
            _, _, blob, proven, mismatch = plan
            if mismatch:
                from .claudesync import quarantine
                quarantine(ctx, ctx.load_state(), email)
                raise CredentialIdentityMismatch("Claude snapshot owner differs — re-add this seat")
            if not proven:
                atomic_write_text(home / ".acctsw-unverified", "1", mode=0o600)
        if blob is None:
            raise MissingSnapshot(f"stored creds for claude:{email} not found — re-add this seat")
        # A child or another supervisor may have installed credentials during preparation.
        if item.get_live() is None:
            item.set_live(blob)
            if not capture and proven and ctx.snapshot_get("claude", email) == plan[0]:
                ctx.snapshot_set("claude", email, blob)
    atomic_write_text(home / ".acctsw-seeded", "1", mode=0o600)
    return home


def save(ctx, email: str, blob: str) -> None:
    """An identified fresh login explicitly replaces this seat's credentials, never another's."""
    # The snapshot is saved by the caller first, so a crash cannot lose the captured login.
    ensure_home(ctx, email, capture=True)
    ctx.claude_cred(email).set_live(blob)
    for marker in (".acctsw-quarantined", ".acctsw-unverified"):
        (ctx.claude_home(email) / marker).unlink(missing_ok=True)


def mirror_identity(ctx, email: str) -> None:
    """A deliberate stock switch must move the profile with the tokens, just like /login does."""
    path = config_path(ctx._claude_real, explicit=ctx.claude_config_explicit)
    with _config_lock(path) as owned:
        if not owned or not owned():
            raise AcctswError("Claude config is busy — retry the switch")
        config = _read(path)
        config["oauthAccount"] = _read(ctx.claude_home(email) / ".claude.json")["oauthAccount"]
        atomic_write_text(path, json.dumps(config, indent=2), mode=0o600)


def load(ctx, email: str) -> str | None:
    """Fresh per-seat bytes; a different /login identity is never attributed to the old seat."""
    home = ctx.claude_home(email)
    if not home.exists():
        return ctx.snapshot_get("claude", email)  # pre-migration
    if (home / ".acctsw-quarantined").exists():
        return None
    return ctx.claude_cred(email).get_live()


def sync_back(ctx, state, email: str, *, live_identity=None) -> bool:
    """Capture only this home's proven identity and exact bytes; never adopt a foreign login."""
    from . import identity, accounts
    resolved = live_identity or identity.claude_live_identity(ctx, email=email)
    if state.get_seat("claude", email) is None:
        return False
    if (resolved.blob and resolved.email and resolved.email.casefold() != email.casefold()
            and ctx.claude_cred(email).get_live() == resolved.blob):
        from .claudesync import quarantine
        quarantine(ctx, state, email)
        return False
    if (not resolved.blob or not resolved.email or resolved.email.casefold() != email.casefold()
            or ctx.claude_cred(email).get_live() != resolved.blob
            or state.get_seat("claude", email) is None):
        return False
    (ctx.claude_home(email) / ".acctsw-unverified").unlink(missing_ok=True)
    if ctx.snapshot_get("claude", email) != resolved.blob:
        ctx.snapshot_set("claude", email, resolved.blob)
        accounts._creds_refreshed(state, "claude", email)
        state.save()
    return True


def delete(ctx, email: str) -> None:
    item = ctx.claude_cred(email)
    item.keychain.delete(item.service, item.account)
    codexhome._remove(ctx.claude_home(email))
