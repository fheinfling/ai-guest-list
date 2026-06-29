"""Non-destructive install / uninstall.

Guarantees:
- The stock ``codex`` / ``claude`` binaries are NEVER shadowed (we install new names: acctsw/cx/cl).
- Before we ever touch the canonical credential locations, the ORIGINAL live creds are captured as
  a "factory image" — stored in the Keychain (per the all-keychain choice), with only a non-secret
  sha256 manifest written to disk. Uninstall restores from that image (sha256-verified).
- Idempotent: re-running install never overwrites an existing factory image or a newer snapshot.
- ``dry_run`` reports every action without performing it.
"""
from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import TOOLS, __version__
from . import accounts as acct
from .context import Context
from .identity import live_email
from .switch import sync_back
from .util import now, iso, sha256_text, write_json

BIN_DIR = Path.home() / ".local" / "bin"
BIN_NAMES = ("acctsw", "cx", "cl")

# We manage a single, clearly-delimited block in the user's shell rc (conda-style begin/end markers)
# so it can be rewritten in place and removed cleanly without touching anything else they wrote.
BLOCK_BEGIN = "# >>> ai guest list (acctsw) >>>"
BLOCK_END = "# <<< ai guest list (acctsw) <<<"
_BLOCK_RE = re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)


def shell_rc_path() -> Path:
    """The shell rc to manage, inferred from $SHELL. zsh (macOS default) → ~/.zshrc;
    bash → ~/.bashrc; anything else → ~/.profile."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if shell.endswith("zsh"):
        return home / ".zshrc"
    if shell.endswith("bash"):
        return home / ".bashrc"
    return home / ".profile"


def on_path(bin_dir: Path) -> bool:
    """True if bin_dir is already on the current PATH."""
    return str(bin_dir) in os.environ.get("PATH", "").split(os.pathsep)


def shell_block(bin_dir: Path, *, aliases: bool = True) -> str:
    """The exact rc block we manage: put cx/cl on PATH and (optionally) alias codex/claude → cx/cl
    so a plain ``codex``/``claude`` is supervised (auto-switch + resume on limits)."""
    lines = [
        BLOCK_BEGIN,
        "# cx/cl supervise codex/claude: auto-switch seats + resume your work on usage limits.",
        f'export PATH="{bin_dir}:$PATH"',
    ]
    if aliases:
        lines += ["alias codex=cx", "alias claude=cl"]
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def ensure_shell_setup(bin_dir: Path | None = None, rc_path: Path | None = None,
                       *, aliases: bool = True) -> tuple[bool, str]:
    """Idempotently install our managed block into the shell rc so cx/cl are runnable (and, with
    aliases, so plain codex/claude are supervised). Rewrites our block in place if its contents
    changed; never touches the user's other lines. Returns (changed, message)."""
    bin_dir = bin_dir or BIN_DIR
    rc_path = rc_path or shell_rc_path()
    block = shell_block(bin_dir, aliases=aliases)
    existing = rc_path.read_text() if rc_path.exists() else ""
    if BLOCK_BEGIN in existing:
        new = _BLOCK_RE.sub(block, existing, count=1)
    else:
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        new = existing + prefix + "\n" + block
    if new == existing:
        return False, f"{rc_path} already set up for cx/cl"
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(new)
    return True, f"wired cx/cl into {rc_path} — open a NEW terminal (or `source {rc_path}`)"


def remove_shell_setup(rc_path: Path | None = None) -> bool:
    """Remove our managed block (only ours). Returns True if something was removed."""
    rc_path = rc_path or shell_rc_path()
    if not rc_path.exists():
        return False
    text = rc_path.read_text()
    if BLOCK_BEGIN not in text:
        return False
    rc_path.write_text(_BLOCK_RE.sub("", text, count=1))
    return True


def _backup_account(tool: str) -> str:
    return f"__factory__:{tool}"


def _entry_from(ctx: Context, tool: str, blob: str) -> dict:
    """Build a manifest entry (records the original account email so uninstall can find the
    freshest copy of that identity rather than blindly restoring the frozen token)."""
    email = ctx.cred[tool].email_of(blob)
    if not email and tool == "claude":
        email = live_email(ctx, "claude")  # at capture time the live claude IS the original
    return {"present": True, "sha256": sha256_text(blob), "captured_at": iso(now()), "email": email}


@dataclass
class Plan:
    actions: list[str] = field(default_factory=list)
    dry_run: bool = False

    def do(self, desc: str, fn=None):
        self.actions.append(("DRY " if self.dry_run else "") + desc)
        if fn and not self.dry_run:
            fn()


# --- install ----------------------------------------------------------------------------------

def install(ctx: Context, *, dry_run: bool = False, register: bool = True,
            bin_dir: Path | None = None, python: str | None = None,
            pkg_root: Path | None = None, with_path: bool = False) -> Plan:
    plan = Plan(dry_run=dry_run)
    bin_dir = bin_dir or BIN_DIR
    python = python or sys.executable
    pkg_root = pkg_root or Path(__file__).resolve().parent.parent

    plan.do("ensure store dirs (0700)", ctx.ensure_dirs)

    # 1. factory image of the ORIGINAL live creds (keychain) + non-secret manifest (disk).
    manifest_path = ctx.backup_dir / "manifest.json"
    manifest = {"created_at": iso(now()), "version": __version__, "entries": {}}
    if manifest_path.exists():
        import json
        manifest = json.loads(manifest_path.read_text())
        manifest.setdefault("entries", {})
    for tool in TOOLS:
        # The Keychain factory image is authoritative — guard on it (not just the manifest) so a
        # lost/corrupt manifest can't cause us to overwrite the irreplaceable original.
        existing = ctx.keychain.get(ctx.keychain_service, _backup_account(tool))
        if existing is not None:
            if tool not in manifest["entries"]:
                manifest["entries"][tool] = _entry_from(ctx, tool, existing)  # rebuild lost manifest
            plan.actions.append(f"keep existing factory image for {tool}")
            continue
        blob = ctx.cred[tool].get_live()
        if not blob:
            manifest["entries"][tool] = {"present": False}
            plan.actions.append(f"no live {tool} creds to back up")
            continue
        entry = _entry_from(ctx, tool, blob)
        def _save(t=tool, b=blob):
            ctx.keychain.set(ctx.keychain_service, _backup_account(t), b)
        plan.do(f"capture factory image: {tool}:{entry.get('email') or '?'} ({entry['sha256'][:12]}…)", _save)
        manifest["entries"][tool] = entry
    plan.do("write backup manifest", lambda: write_json(manifest_path, manifest, mode=0o600))

    # 2. register the currently-logged-in account as the first seat.
    if register:
        for tool in TOOLS:
            blob = ctx.cred[tool].get_live()
            if not blob:
                continue
            email = live_email(ctx, tool)
            if not email:
                plan.actions.append(f"skip register {tool}: could not identify email")
                continue
            def _reg(t=tool, e=email):
                st = ctx.load_state()
                acct.add(ctx, st, t, email=e)
            plan.do(f"register first seat: {tool}:{email}", _reg)

    # 3. install the bin wrappers (never shadow stock codex/claude).
    for name in BIN_NAMES:
        target = bin_dir / name
        script = _wrapper_script(name, python, pkg_root, bin_dir)
        def _write(t=target, s=script):
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(s)
            t.chmod(0o755)
        plan.do(f"install {target}", _write)

    # 4. bundle Headroom (save-credit) into this venv so the toggle just works — no separate install.
    from . import headroom as hr
    if hr.available():
        plan.actions.append("headroom already installed (save-credit ready)")
    else:
        plan.do("install headroom (save-credit) into the app venv", hr.ensure_installed)

    # 5. shell setup: cx/cl are useless (autoswitch never fires) unless bin_dir is on PATH. With
    #    --path we wire it (PATH + codex/claude aliases) so it "just works"; otherwise WARN loudly
    #    (not a quiet NOTE) so the gap is never silent.
    if with_path:
        rc = shell_rc_path()
        def _setup(b=bin_dir, r=rc):
            ensure_shell_setup(b, r)
        plan.do(f"wire cx/cl into {rc} (PATH + codex/claude aliases; open a new terminal after)", _setup)
    elif on_path(bin_dir):
        plan.actions.append(f"{bin_dir} already on PATH — cx/cl are ready")
    else:
        plan.actions.append(
            f"WARNING: {bin_dir} is NOT on PATH, so cx/cl won't run and autoswitch can't work. "
            f"Fix it with `acctsw path`, or add:  export PATH=\"{bin_dir}:$PATH\"")

    return plan


def ensure_launchers(*, bin_dir: Path | None = None, python: str | None = None,
                     pkg_root: Path | None = None, aliases: bool = True) -> tuple[bool, list[str]]:
    """Make cx/cl usable end-to-end with zero manual steps: (re)write the bin wrappers if missing or
    stale, and wire the shell rc (PATH + codex/claude aliases). Idempotent and cheap — safe to call
    on every app launch so a fresh install "just works" and a deleted rc block self-heals. Returns
    (changed, messages)."""
    bin_dir = bin_dir or BIN_DIR
    python = python or sys.executable
    pkg_root = pkg_root or Path(__file__).resolve().parent.parent
    changed, msgs = False, []
    for name in BIN_NAMES:
        target = bin_dir / name
        script = _wrapper_script(name, python, pkg_root, bin_dir)
        if not target.exists() or target.read_text() != script:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(script)
            target.chmod(0o755)
            changed = True
            msgs.append(f"installed {target}")
    did, msg = ensure_shell_setup(bin_dir, aliases=aliases)
    msgs.append(msg)
    return (changed or did), msgs


def _wrapper_script(name: str, python: str, pkg_root: Path, bin_dir: Path) -> str:
    if name == "acctsw":
        return (f"#!/bin/sh\n# ai guest list engine\n"
                f'PYTHONPATH="{pkg_root}:$PYTHONPATH" exec "{python}" -m acctsw "$@"\n')
    tool = "codex" if name == "cx" else "claude"
    return (f"#!/bin/sh\n# supervised {tool} launcher (stock {tool} is not shadowed)\n"
            f'exec "{bin_dir}/acctsw" run {tool} "$@"\n')


# --- uninstall --------------------------------------------------------------------------------

def uninstall(ctx: Context, *, purge: bool = False, dry_run: bool = False,
              bin_dir: Path | None = None) -> Plan:
    plan = Plan(dry_run=dry_run)
    bin_dir = bin_dir or BIN_DIR

    # 1. don't lose the active seat's freshest creds.
    state = ctx.load_state()
    for tool in TOOLS:
        if state.active(tool):
            plan.do(f"sync-back active {tool} creds", lambda t=tool: sync_back(ctx, ctx.load_state(), t))

    # 2. restore the ORIGINAL identity — preferring the FRESHEST copy, not the frozen factory token
    #    (refresh tokens rotate; restoring stale bytes would log the original account out).
    manifest_path = ctx.backup_dir / "manifest.json"
    if manifest_path.exists():
        import json
        manifest = json.loads(manifest_path.read_text())
        for tool, entry in manifest.get("entries", {}).items():
            if not entry.get("present"):
                continue
            orig_email = entry.get("email")
            live = ctx.cred[tool].get_live()
            cur_email = ctx.cred[tool].email_of(live) if live else None

            # (a) the original identity is already live → it's the freshest; leave it untouched.
            if orig_email and cur_email == orig_email:
                plan.actions.append(f"{tool}: already on original {orig_email}; left as-is")
                continue
            # (b) prefer the original identity's seat snapshot (kept fresh via sync-back).
            snap = (ctx.snapshot_get(tool, orig_email) if orig_email else None)
            if snap is not None:
                plan.do(f"restore original {tool}:{orig_email} (freshest snapshot)",
                        lambda t=tool, b=snap: ctx.cred[t].set_live(b))
                continue
            # (c) last resort: the frozen factory image, sha256-verified.
            factory = ctx.keychain.get(ctx.keychain_service, _backup_account(tool))
            if factory is None:
                plan.actions.append(f"WARN: factory image for {tool} missing; cannot restore")
                continue
            if sha256_text(factory) != entry.get("sha256"):
                plan.actions.append(f"WARN: factory image for {tool} failed sha256; skipping restore")
                continue
            plan.do(f"restore original {tool} creds (factory image)",
                    lambda t=tool, b=factory: ctx.cred[t].set_live(b))
    else:
        plan.actions.append("no backup manifest found; nothing to restore")

    # 3. remove the bin wrappers + app.
    for name in BIN_NAMES:
        target = bin_dir / name
        if target.exists():
            plan.do(f"remove {target}", lambda t=target: t.unlink())
    app = ctx.data_dir / "ai guest list.app"
    if app.exists():
        plan.actions.append(f"NOTE: remove the menubar app at {app} (drag to Trash)")

    # remove the managed block we added (only ours — delimited by our begin/end markers).
    rc = shell_rc_path()
    if rc.exists() and BLOCK_BEGIN in rc.read_text():
        plan.do(f"remove our cx/cl block from {rc}", lambda r=rc: remove_shell_setup(r))
    else:
        plan.actions.append(f"NOTE: if you added it manually, remove the cx/cl block for {bin_dir} from your shell rc")

    # 4. purge: delete store + all our keychain items.
    if purge:
        st = ctx.load_state()
        for tool in TOOLS:
            for email in list(st.accounts(tool)):
                plan.do(f"delete stored creds {tool}:{email}",
                        lambda t=tool, e=email: ctx.snapshot_delete(t, e))
            plan.do(f"delete factory image {tool}",
                    lambda t=tool: ctx.keychain.delete(ctx.keychain_service, _backup_account(t)))

        def _rmtree():
            import shutil
            shutil.rmtree(ctx.data_dir, ignore_errors=True)
        plan.do(f"delete store {ctx.data_dir}", _rmtree)

    return plan
