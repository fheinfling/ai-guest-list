"""Feature tests for install/uninstall — factory image, restore round-trip, non-destructive."""
import json
import os
import stat
import sys

from acctsw import install as inst
from acctsw.install import install, uninstall, _backup_account
from tests.conftest import make_codex_blob, make_claude_blob


def _seed_live(ctx, codex_email="orig@x.com"):
    ctx.cred["codex"].set_live(make_codex_blob(codex_email))
    ctx.cred["claude"].set_live(make_claude_blob())


def test_install_captures_factory_image_and_registers(ctx, tmp_path):
    _seed_live(ctx)
    # claude identity isn't derivable from blob → register only codex here (no claude bin in tests)
    plan = install(ctx, bin_dir=tmp_path / "bin", register=True)
    # factory image stored in keychain for codex
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) is not None
    # manifest written (non-secret), with sha256
    manifest = json.loads((ctx.backup_dir / "manifest.json").read_text())
    assert manifest["entries"]["codex"]["present"] is True
    assert len(manifest["entries"]["codex"]["sha256"]) == 64
    # first codex seat registered
    assert "orig@x.com" in ctx.load_state().accounts("codex")


def test_install_writes_executable_wrappers_without_shadowing(ctx, tmp_path):
    _seed_live(ctx)
    bindir = tmp_path / "bin"
    install(ctx, bin_dir=bindir, register=False)
    for name in ("acctsw", "cx", "cl"):
        p = bindir / name
        assert p.exists()
        assert os.stat(p).st_mode & stat.S_IXUSR
    assert "run codex" in (bindir / "cx").read_text()
    assert "run claude" in (bindir / "cl").read_text()
    # we never create a file named codex/claude (no shadowing)
    assert not (bindir / "codex").exists() and not (bindir / "claude").exists()


def test_install_is_idempotent_keeps_original_factory_image(ctx, tmp_path):
    _seed_live(ctx, codex_email="orig@x.com")
    install(ctx, bin_dir=tmp_path / "bin", register=False)
    original = ctx.keychain.get(ctx.keychain_service, _backup_account("codex"))
    # user later logs in as a different account, then re-runs install
    ctx.cred["codex"].set_live(make_codex_blob("different@x.com"))
    install(ctx, bin_dir=tmp_path / "bin", register=False)
    # factory image must still be the ORIGINAL
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) == original


def test_dry_run_performs_nothing(ctx, tmp_path):
    _seed_live(ctx)
    plan = install(ctx, bin_dir=tmp_path / "bin", dry_run=True, register=True)
    assert plan.actions
    # dry-run must change NOTHING: no factory image, no seats, no wrappers
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) is None
    assert ctx.load_state().accounts("codex") == {}
    assert not (tmp_path / "bin" / "acctsw").exists()


def test_uninstall_restores_original_creds(ctx, tmp_path):
    _seed_live(ctx, codex_email="orig@x.com")
    bindir = tmp_path / "bin"
    install(ctx, bin_dir=bindir, register=True)
    # user switches around / live creds change
    ctx.cred["codex"].set_live(make_codex_blob("someone-else@x.com"))

    uninstall(ctx, bin_dir=bindir)
    # original codex creds restored to the canonical location
    import json as _j
    restored = _j.loads(ctx.cred["codex"].get_live())
    from acctsw.util import jwt_payload
    assert jwt_payload(restored["tokens"]["id_token"])["email"] == "orig@x.com"
    # wrappers removed
    assert not (bindir / "acctsw").exists()


def test_uninstall_skips_restore_on_sha_mismatch(ctx, tmp_path):
    _seed_live(ctx, codex_email="orig@x.com")
    install(ctx, bin_dir=tmp_path / "bin", register=False)  # no seat snapshot → factory is the only source
    # live is now a different account (so restore must fall back to the factory image, path (c))
    ctx.cred["codex"].set_live(make_codex_blob("someone-else@x.com"))
    # tamper the stored factory image so sha256 won't match the manifest
    ctx.keychain.set(ctx.keychain_service, _backup_account("codex"), make_codex_blob("tampered@x.com"))
    plan = uninstall(ctx, bin_dir=tmp_path / "bin")
    assert any("failed sha256" in a for a in plan.actions)


def test_uninstall_leaves_original_if_already_live(ctx, tmp_path):
    """M5-B1: if the original account is still live, never downgrade it to the frozen factory token."""
    _seed_live(ctx, codex_email="orig@x.com")
    install(ctx, bin_dir=tmp_path / "bin", register=True)
    # original is still live; rotate its live token (simulating normal use)
    rotated = make_codex_blob("orig@x.com").replace('"access_token": "a"', '"access_token": "FRESH"')
    ctx.cred["codex"].set_live(rotated)
    plan = uninstall(ctx, bin_dir=tmp_path / "bin")
    assert any("already on original" in a for a in plan.actions)
    # the freshest (rotated) original creds are preserved, not overwritten by the stale factory
    import json
    assert json.loads(ctx.cred["codex"].get_live())["tokens"]["access_token"] == "FRESH"


def test_uninstall_prefers_fresh_snapshot_over_factory(ctx, tmp_path):
    """When live is a different account, restore the original via its (fresher) seat snapshot."""
    _seed_live(ctx, codex_email="orig@x.com")
    install(ctx, bin_dir=tmp_path / "bin", register=True)
    # original's seat snapshot gets refreshed (rotated) during use
    rotated = make_codex_blob("orig@x.com").replace('"refresh_token": "r"', '"refresh_token": "ROT"')
    ctx.snapshot_set("codex", "orig@x.com", rotated)
    # live is now a different account
    ctx.cred["codex"].set_live(make_codex_blob("other@x.com"))
    uninstall(ctx, bin_dir=tmp_path / "bin")
    import json
    restored = json.loads(ctx.cred["codex"].get_live())
    assert restored["tokens"]["refresh_token"] == "ROT"  # fresh snapshot, not frozen factory


def test_install_keychain_guard_protects_original_when_manifest_lost(ctx, tmp_path):
    """If the manifest is lost but the keychain factory image survives, re-install must NOT
    overwrite the original factory image with current (non-original) creds."""
    _seed_live(ctx, codex_email="orig@x.com")
    install(ctx, bin_dir=tmp_path / "bin", register=False)
    original_factory = ctx.keychain.get(ctx.keychain_service, _backup_account("codex"))
    # simulate manifest loss
    (ctx.backup_dir / "manifest.json").unlink()
    # user has since logged into a different account
    ctx.cred["codex"].set_live(make_codex_blob("different@x.com"))
    install(ctx, bin_dir=tmp_path / "bin", register=False)
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) == original_factory


def test_ensure_shell_setup_adds_then_is_idempotent(tmp_path):
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    changed, _ = inst.ensure_shell_setup(bindir, rc)
    assert changed
    body = rc.read_text()
    assert inst.BLOCK_BEGIN in body and inst.BLOCK_END in body
    assert f'export PATH="{bindir}:$PATH"' in body
    assert "alias codex=cx" in body and "alias claude=cl" in body
    # second call is a no-op (block already present, identical)
    changed2, _ = inst.ensure_shell_setup(bindir, rc)
    assert not changed2
    assert rc.read_text().count(inst.BLOCK_BEGIN) == 1


def test_ensure_shell_setup_rewrites_block_in_place(tmp_path):
    rc = tmp_path / ".zshrc"
    inst.ensure_shell_setup(tmp_path / "bin", rc, aliases=True)
    # re-running with aliases off rewrites OUR block (still exactly one), dropping the alias lines
    changed, _ = inst.ensure_shell_setup(tmp_path / "bin", rc, aliases=False)
    assert changed
    body = rc.read_text()
    assert body.count(inst.BLOCK_BEGIN) == 1
    assert "alias codex=cx" not in body


def test_ensure_shell_setup_preserves_existing_content(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("alias ll='ls -la'")          # no trailing newline
    inst.ensure_shell_setup(tmp_path / "bin", rc)
    body = rc.read_text()
    assert body.startswith("alias ll='ls -la'\n")   # original kept, newline inserted before our block


def test_install_with_path_wires_rc(ctx, tmp_path, monkeypatch):
    _seed_live(ctx)
    rc = tmp_path / ".zshrc"
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    monkeypatch.setenv("PATH", "/usr/bin")       # bin_dir not on PATH
    bindir = tmp_path / "bin"
    install(ctx, bin_dir=bindir, register=False, with_path=True)
    body = rc.read_text()
    assert f'export PATH="{bindir}:$PATH"' in body
    assert "alias codex=cx" in body


def test_install_default_warns_and_never_edits_rc(ctx, tmp_path, monkeypatch):
    _seed_live(ctx)
    rc = tmp_path / ".zshrc"
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    monkeypatch.setenv("PATH", "/usr/bin")
    plan = install(ctx, bin_dir=tmp_path / "bin", register=False)  # no with_path
    assert not rc.exists()                                          # never edited silently
    assert any("NOT on PATH" in a for a in plan.actions)


def test_install_no_warning_when_already_on_path(ctx, tmp_path, monkeypatch):
    _seed_live(ctx)
    bindir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin")
    plan = install(ctx, bin_dir=bindir, register=False)
    assert any("already on PATH" in a for a in plan.actions)
    assert not any("NOT on PATH" in a for a in plan.actions)


def test_ensure_launchers_writes_wrappers_and_wires_rc(tmp_path, monkeypatch):
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    changed, _ = inst.ensure_launchers(bin_dir=bindir)
    assert changed
    assert (bindir / "cx").exists() and (bindir / "cl").exists()
    assert "alias codex=cx" in rc.read_text()
    # idempotent: nothing changes on a second call
    changed2, _ = inst.ensure_launchers(bin_dir=bindir)
    assert not changed2


def test_ensure_launchers_preserves_non_poisoned_wrapper(tmp_path, monkeypatch):
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    (bindir / "cx").write_text("#!/bin/sh\n# good wrapper from `acctsw install`\n")
    inst.ensure_launchers(bin_dir=bindir)
    # an existing wrapper that isn't the known-broken pattern (no frozen `python311.zip` on
    # PYTHONPATH) must be preserved — healing is scoped to the crash symptom, so a good hand-written
    # or `acctsw install` wrapper is never clobbered by the app bootstrap
    assert "good wrapper" in (bindir / "cx").read_text()


def test_shell_rc_path_bash_targets_bash_profile(tmp_path, monkeypatch):
    """On macOS, bash login shells source ~/.bash_profile, not ~/.bashrc."""
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(inst.Path, "home", classmethod(lambda cls: tmp_path))
    assert inst.shell_rc_path() == tmp_path / ".bash_profile"     # neither exists → login rc
    (tmp_path / ".bashrc").write_text("")                          # only ~/.bashrc exists → respect it
    assert inst.shell_rc_path() == tmp_path / ".bashrc"
    (tmp_path / ".bash_profile").write_text("")                    # prefer ~/.bash_profile once present
    assert inst.shell_rc_path() == tmp_path / ".bash_profile"


def test_ensure_shell_setup_handles_regex_metachars_in_path(tmp_path):
    """A bin_dir whose path contains re-replacement metachars (\\g, \\1, backslash) must be written
    LITERALLY when our block is rewritten in place — not interpreted by re.sub."""
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / r"w\g<0>ird\1"          # path with backslash + group-ref-looking sequences
    inst.ensure_shell_setup(bindir, rc)          # first write (append path)
    inst.ensure_shell_setup(bindir, rc, aliases=False)  # rewrite-in-place path → exercises sub()
    assert f'export PATH="{bindir}:$PATH"' in rc.read_text()


def test_ensure_launchers_uses_bundle_python_when_frozen(tmp_path, monkeypatch):
    """Called from the frozen app (no python passed), the acctsw wrapper must exec the py2app BUNDLE
    interpreter (sys.executable = …/Contents/MacOS/python) with PYTHONHOME pointed at the bundle's
    Contents/Resources — otherwise the bundle python can't find its own stdlib on a machine lacking a
    system Python.framework and dies with "can't find module 'encodings'". No PYTHONPATH assignment
    (the ≤0.2.3 crash cause)."""
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    inst.ensure_launchers(bin_dir=bindir)
    body = (bindir / "acctsw").read_text()
    assert "PYTHONHOME=/Bundle.app/Contents/Resources exec /Bundle.app/Contents/MacOS/python -m acctsw" in body
    assert "PYTHONPATH=" not in body           # no PYTHONPATH *assignment* (the ≤0.2.3 crash cause)
    assert "unset PYTHONHOME PYTHONPATH" in body   # clears any inherited leak before setting our own
    assert "python311.zip" not in body and "/usr/bin/python3" not in body


def test_ensure_launchers_heals_stale_wrapper(tmp_path, monkeypatch):
    """A wrapper an older build baked with the broken system-python3 + frozen-zip PYTHONPATH gets
    rewritten to the bundle-python form on the next (idempotent) bootstrap."""
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stale = bindir / "acctsw"
    stale.write_text('#!/bin/sh\nPYTHONPATH=/App.app/Contents/Resources/lib/python311.zip '
                     'exec /usr/bin/python3 -m acctsw "$@"\n')
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    changed, _ = inst.ensure_launchers(bin_dir=bindir)
    assert changed
    body = stale.read_text()
    assert "python311.zip" not in body and "/usr/bin/python3" not in body
    assert "PYTHONHOME=/Bundle.app/Contents/Resources" in body
    # idempotent: the healed wrapper matches desired, so a second pass leaves it untouched
    assert inst.ensure_launchers(bin_dir=bindir)[0] is False


def test_ensure_launchers_heals_bundle_wrapper_missing_pythonhome(tmp_path, monkeypatch):
    """The 0.2.4 breakage: a bundle-python wrapper with NO PYTHONHOME (it unset the vars but never set
    one) can't find its own stdlib on a clean machine. The bootstrap must heal it to set PYTHONHOME."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "acctsw").write_text(
        '#!/bin/sh\n# ai guest list engine\n'
        'unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE __PYVENV_LAUNCHER__\n'
        'exec /Bundle.app/Contents/MacOS/python -m acctsw "$@"\n')
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    changed, _ = inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    assert changed
    assert "PYTHONHOME=/Bundle.app/Contents/Resources" in (bindir / "acctsw").read_text()


def test_ensure_launchers_heals_wrapper_with_dead_interpreter(tmp_path, monkeypatch):
    """A wrapper whose baked interpreter no longer exists (app moved/renamed, or venv deleted) is dead
    and gets healed to the current interpreter."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "acctsw").write_text(
        '#!/bin/sh\n# ai guest list engine\n'
        'PYTHONHOME=/Old/Moved.app/Contents/Resources '
        'exec /Old/Moved.app/Contents/MacOS/python -m acctsw "$@"\n')
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    changed, _ = inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    assert changed
    assert "/Bundle.app/Contents/MacOS/python -m acctsw" in (bindir / "acctsw").read_text()


def test_ensure_launchers_preserves_wrapper_with_live_interpreter(tmp_path, monkeypatch):
    """A source-install wrapper that execs a real, existing interpreter is NOT stale — the app
    bootstrap must not clobber it just because it differs from the bundle form."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    good = ('#!/bin/sh\n# ai guest list engine\n'
            f'PYTHONPATH=/src/checkout exec {sys.executable} -m acctsw "$@"\n')
    (bindir / "acctsw").write_text(good)
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    assert (bindir / "acctsw").read_text() == good    # preserved verbatim


def test_ensure_launchers_heals_wrapper_after_python_version_bump(tmp_path, monkeypatch):
    """Healing matches the poison shape (python3NN.zip), not the pinned 311 — so an old broken wrapper
    still self-heals if a future bundle ships a newer Python."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "acctsw").write_text('#!/bin/sh\nPYTHONPATH=/App.app/Contents/Resources/lib/python312.zip '
                                   'exec /usr/bin/python3 -m acctsw "$@"\n')
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    changed, _ = inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    assert changed
    assert "python312.zip" not in (bindir / "acctsw").read_text()


def test_ensure_launchers_wire_rc_false_skips_rc(tmp_path, monkeypatch):
    """wire_rc=False heals wrappers but never touches the shell rc (so re-heal every launch doesn't
    re-add a block the user removed)."""
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    assert (bindir / "acctsw").exists()
    assert not rc.exists()


def test_uninstall_removes_only_our_block(ctx, tmp_path, monkeypatch):
    _seed_live(ctx)
    rc = tmp_path / ".zshrc"
    rc.write_text("alias ll='ls -la'\n")
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    bindir = tmp_path / "bin"
    inst.ensure_shell_setup(bindir, rc)
    assert inst.BLOCK_BEGIN in rc.read_text()
    uninstall(ctx, bin_dir=bindir)
    body = rc.read_text()
    assert inst.BLOCK_BEGIN not in body
    assert f'export PATH="{bindir}:$PATH"' not in body
    assert "alias ll='ls -la'" in body            # untouched user content survives


def test_uninstall_clears_bootstrap_sentinel(ctx, tmp_path, monkeypatch):
    """Uninstall removes the wrappers/rc block, so it must also clear the .cli-bootstrapped sentinel —
    otherwise reopening the app skips ensure_launchers() and never restores cx/cl."""
    _seed_live(ctx)
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    sentinel = ctx.data_dir / ".cli-bootstrapped"
    sentinel.write_text("")
    uninstall(ctx, bin_dir=tmp_path / "bin")
    assert not sentinel.exists()


def test_purge_removes_store_and_keychain(ctx, tmp_path):
    _seed_live(ctx)
    install(ctx, bin_dir=tmp_path / "bin", register=True)
    uninstall(ctx, bin_dir=tmp_path / "bin", purge=True)
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) is None
    assert ctx.snapshot_get("codex", "orig@x.com") is None
    assert not ctx.data_dir.exists()
