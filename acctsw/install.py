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


def _backup_account(tool: str) -> str:
    return f"__factory__:{tool}"


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
            pkg_root: Path | None = None) -> Plan:
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
        if tool in manifest["entries"]:
            plan.actions.append(f"keep existing factory image for {tool}")
            continue
        blob = ctx.cred[tool].get_live()
        if not blob:
            manifest["entries"][tool] = {"present": False}
            plan.actions.append(f"no live {tool} creds to back up")
            continue
        entry = {"present": True, "sha256": sha256_text(blob), "captured_at": iso(now())}
        def _save(t=tool, b=blob):
            ctx.keychain.set(ctx.keychain_service, _backup_account(t), b)
        plan.do(f"capture factory image: {tool} ({entry['sha256'][:12]}…)", _save)
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

    # 4. PATH note (never edit rc silently).
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        plan.actions.append(f"NOTE: add to PATH →  export PATH=\"{bin_dir}:$PATH\"")

    return plan


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

    # 2. restore the factory image (sha256-verified) to the canonical locations.
    manifest_path = ctx.backup_dir / "manifest.json"
    if manifest_path.exists():
        import json
        manifest = json.loads(manifest_path.read_text())
        for tool, entry in manifest.get("entries", {}).items():
            if not entry.get("present"):
                continue
            blob = ctx.keychain.get(ctx.keychain_service, _backup_account(tool))
            if blob is None:
                plan.actions.append(f"WARN: factory image for {tool} missing; cannot restore")
                continue
            if sha256_text(blob) != entry.get("sha256"):
                plan.actions.append(f"WARN: factory image for {tool} failed sha256; skipping restore")
                continue
            plan.do(f"restore original {tool} creds", lambda t=tool, b=blob: ctx.cred[t].set_live(b))
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

    plan.actions.append(f"NOTE: if you added it, remove the PATH line for {bin_dir} from your shell rc")

    # 4. purge: delete store + all our keychain items.
    if purge:
        st = ctx.load_state()
        for tool in TOOLS:
            for email in list(st.accounts(tool)):
                plan.do(f"delete keychain snapshot {tool}:{email}",
                        lambda t=tool, e=email: ctx.keychain.delete(ctx.keychain_service,
                                                                    ctx.snapshot_key(t, e)))
            plan.do(f"delete factory image {tool}",
                    lambda t=tool: ctx.keychain.delete(ctx.keychain_service, _backup_account(t)))

        def _rmtree():
            import shutil
            shutil.rmtree(ctx.data_dir, ignore_errors=True)
        plan.do(f"delete store {ctx.data_dir}", _rmtree)

    return plan
