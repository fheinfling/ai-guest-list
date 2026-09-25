"""Feature tests for install/uninstall — factory image, restore round-trip, non-destructive."""
import json
import locale
import os
import shlex
import ssl
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.fixture
def ascii_rc(tmp_path, monkeypatch):
    rc = tmp_path / ".zshrc"
    # Modern Python can bypass getpreferredencoding() for open(). Force the rc's default
    # codec too, so this reproduces LaunchServices even on a UTF-8 developer machine.
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args: "ascii")
    original_open = Path.open

    def open_rc(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if path == rc and "b" not in mode and encoding in (None, "locale"):
            encoding = "ascii"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_rc)
    prefix = b"# " + b"x" * 33 + "— user's settings\n".encode("utf-8")
    assert prefix[35] == 0xe2  # the exact byte/offset in the reported failure
    rc.write_bytes(prefix)
    return rc


def test_ensure_shell_setup_with_ascii_locale(ascii_rc, tmp_path):
    original = ascii_rc.read_bytes()
    changed, _ = inst.ensure_shell_setup(tmp_path / "bin", ascii_rc)
    assert changed
    assert ascii_rc.read_bytes() == original + b"\n" + inst.shell_block(tmp_path / "bin").encode("utf-8")
    assert not inst.ensure_shell_setup(tmp_path / "bin", ascii_rc)[0]


def test_supervision_status_with_ascii_locale(ascii_rc, tmp_path):
    bindir = tmp_path / "bin"
    inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    ascii_rc.write_bytes(ascii_rc.read_bytes() + inst.shell_block(bindir).encode("utf-8"))
    status = inst.supervision_status(bindir, ascii_rc)
    assert status["block"] is True
    assert status["active"] is True
    assert "error" not in status


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_shell_setup_preserves_foreign_bytes_and_mode(tmp_path, monkeypatch, newline, mode):
    atomic_write_text = inst.atomic_write_text

    def checked_write(*args, **kwargs):
        # fchmod may silently mask file-type bits, hiding a non-portable mode argument.
        assert kwargs["mode"] == mode
        return atomic_write_text(*args, **kwargs)

    monkeypatch.setattr(inst, "atomic_write_text", checked_write)
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    original = b"# caf\xe9" + newline + b"export EDITOR=vi" + newline
    rc.write_bytes(original)
    rc.chmod(mode)
    inst.ensure_shell_setup(bindir, rc)
    separator = b"\n" if original.endswith(b"\n") else b"\n\n"
    assert rc.read_bytes() == original + separator + inst.shell_block(bindir).encode("utf-8")
    assert stat.S_IMODE(rc.stat().st_mode) == mode
    # Exercise in-place replacement and removal with foreign bytes on BOTH sides.
    suffix = b"# apr\xe8s" + newline
    rc.write_bytes(rc.read_bytes() + suffix)
    inst.ensure_shell_setup(bindir, rc, aliases=False)
    assert rc.read_bytes() == original + separator + inst.shell_block(bindir, aliases=False).encode("utf-8") + suffix
    assert inst.remove_shell_setup(rc)
    assert rc.read_bytes() == original + separator + suffix
    assert stat.S_IMODE(rc.stat().st_mode) == mode


def test_new_shell_rc_mode(tmp_path):
    rc = tmp_path / ".zshrc"
    inst.ensure_shell_setup(tmp_path / "bin", rc)
    assert stat.S_IMODE(rc.stat().st_mode) == 0o644


@pytest.mark.parametrize("chain_length", [1, 3])
@pytest.mark.parametrize("target_exists", [False, True])
def test_shell_setup_preserves_symlinks(tmp_path, monkeypatch, chain_length, target_exists):
    real = tmp_path / "dotfiles" / "zshrc"
    real.parent.mkdir()
    original = b"# my dotfiles\nexport A=1\n" if target_exists else b""
    if target_exists:
        real.write_bytes(original)
    links = []
    target = real
    for index in range(chain_length):
        link = tmp_path / f".zshrc-{index}"
        link.symlink_to(target.relative_to(tmp_path))
        links.append((link, link.readlink()))
        target = link
    rc = target
    atomic_write_text = inst.atomic_write_text

    def checked_write(path, *args, **kwargs):
        # The writer derives its temp directory from this path; it must be beside the target.
        assert path == Path(os.path.realpath(real))
        return atomic_write_text(path, *args, **kwargs)

    monkeypatch.setattr(inst, "atomic_write_text", checked_write)
    if not target_exists:
        assert not inst.remove_shell_setup(rc)
        assert not real.exists()
        assert all(link.is_symlink() and link.readlink() == dest for link, dest in links)
    bindir = tmp_path / "bin"
    assert inst.ensure_shell_setup(bindir, rc)[0]
    assert all(link.is_symlink() and link.readlink() == dest for link, dest in links)
    assert real.read_bytes() == original + b"\n" + inst.shell_block(bindir).encode("utf-8")
    assert not inst.ensure_shell_setup(bindir, rc)[0]
    assert inst.remove_shell_setup(rc)
    assert all(link.is_symlink() and link.readlink() == dest for link, dest in links)
    assert real.read_bytes() == original + b"\n"


@pytest.mark.parametrize("chain_length", [1, 3])
def test_remove_shell_setup_preserves_symlinks(tmp_path, chain_length):
    real = tmp_path / "dotfiles" / "zshrc"
    real.parent.mkdir()
    original = b"# my dotfiles\nexport A=1\n"
    real.write_bytes(original + inst.shell_block(tmp_path / "bin").encode("utf-8"))
    links = []
    target = real
    for index in range(chain_length):
        link = tmp_path / f".zshrc-{index}"
        link.symlink_to(target.relative_to(tmp_path))
        links.append((link, link.readlink()))
        target = link
    assert inst.remove_shell_setup(target)
    assert all(link.is_symlink() and link.readlink() == dest for link, dest in links)
    assert real.read_bytes() == original


@pytest.mark.parametrize("packaged", [False, True], ids=["source", "bundle"])
def test_wrapper_without_utf8_is_repaired(tmp_path, packaged):
    interpreter = tmp_path / "App.app" / "Contents" / "MacOS" / "python" if packaged else Path(sys.executable)
    if packaged:
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = inst._wrapper_script("acctsw", str(interpreter), tmp_path, bindir)
    assert "PYTHONUTF8=1" in body
    assert not inst._wrapper_stale(body)
    old = body.replace("PYTHONUTF8=1 ", "")
    assert inst._wrapper_stale(old)
    (bindir / "acctsw").write_text(old, encoding="utf-8")
    inst.ensure_launchers(bin_dir=bindir, python=str(interpreter), pkg_root=tmp_path, wire_rc=False)
    assert (bindir / "acctsw").read_text(encoding="utf-8") == body


@pytest.mark.parametrize(
    ("have_wrappers", "have_block", "path_live", "expected_active"),
    [
        (True, True, True, True),       # fully effective in this terminal
        (True, False, False, False),    # maintainer's field failure: wrappers only
        (False, True, False, False),    # rc block only
        (False, False, False, False),   # neither half installed
        (True, True, False, True),      # correctly wired; this terminal predates the rc edit
    ],
    ids=["wrappers-and-block", "wrappers-only", "block-only", "neither", "new-terminal-needed"],
)
def test_supervision_status_matrix(
    tmp_path, monkeypatch, have_wrappers, have_block, path_live, expected_active,
):
    bindir = tmp_path / "bin"
    rc = tmp_path / ".zshrc"
    if have_wrappers:
        bindir.mkdir()
        for name in inst.BIN_NAMES:
            wrapper = bindir / name
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o755)
    if have_block:
        inst.ensure_shell_setup(bindir, rc)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin" if path_live else "/usr/bin:/bin")

    status = inst.supervision_status(bindir, rc)

    assert status == {
        "wrappers": have_wrappers,
        "block": have_block,
        "rc_path": str(rc),
        "on_path": path_live,
        "active": expected_active,
    }


def test_supervision_status_requires_executable_wrappers(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in inst.BIN_NAMES:
        wrapper = bindir / name
        wrapper.write_text("#!/bin/sh\n")
        wrapper.chmod(0o755)
    (bindir / "cl").chmod(0o644)
    rc = tmp_path / ".zshrc"
    inst.ensure_shell_setup(bindir, rc)
    monkeypatch.setenv("PATH", str(bindir))
    status = inst.supervision_status(bindir, rc)
    assert status["wrappers"] is False
    assert status["block"] is True
    assert status["active"] is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_supervision_status_survives_an_unreadable_rc(tmp_path, monkeypatch):
    """An rc we are not allowed to read must degrade, not explode: the caller still gets a status
    dict, `block` falls back to False, and the reason travels with it so the UI can say it."""
    bindir = tmp_path / "bin"
    inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    rc = tmp_path / ".zshrc"
    inst.ensure_shell_setup(bindir, rc)          # the block IS there — we simply cannot look
    rc.chmod(0o000)
    monkeypatch.setenv("PATH", str(bindir))
    try:
        status = inst.supervision_status(bindir, rc)
    finally:
        rc.chmod(0o600)                          # keep tmp_path teardown clean

    assert status["error"].startswith(f"couldn't read {rc}: ")
    assert "Permission denied" in status["error"]
    assert status["block"] is False and status["active"] is False
    assert status["wrappers"] is True            # the half we could check is still reported
    assert status["rc_path"] == str(rc)
    assert status["on_path"] is True


def test_supervision_status_survives_an_undecodable_rc(tmp_path, monkeypatch):
    """A stray latin-1 line does not prevent us from recognizing our ASCII block."""
    bindir = tmp_path / "bin"
    rc = tmp_path / ".zshrc"
    rc.write_bytes(b"export EDITOR=vi\n# caf\xe9 au lait (latin-1, not utf-8)\n"
                   + inst.shell_block(bindir).encode("utf-8"))
    monkeypatch.setenv("PATH", "/usr/bin")

    status = inst.supervision_status(bindir, rc)

    assert status == {
        "wrappers": False,
        "block": True,
        "rc_path": str(rc),
        "on_path": False,
        "active": False,
    }


def test_supervision_status_omits_error_when_the_rc_reads_fine(tmp_path, monkeypatch):
    """The error key is the exception, not the rule — a healthy probe must not carry one."""
    rc = tmp_path / ".zshrc"
    rc.write_text("alias ll='ls -la'\n")
    monkeypatch.setenv("PATH", "/usr/bin")
    assert "error" not in inst.supervision_status(tmp_path / "bin", rc)


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


def test_ensure_launchers_rewires_deleted_block_once(tmp_path, monkeypatch):
    """The exact field failure self-heals: intact wrappers + a missing managed block."""
    rc = tmp_path / ".zshrc"
    bindir = tmp_path / "bin"
    monkeypatch.setattr(inst, "shell_rc_path", lambda: rc)
    inst.ensure_launchers(bin_dir=bindir)
    assert inst.remove_shell_setup(rc) is True
    assert inst.supervision_status(bindir, rc)["block"] is False

    changed, _ = inst.ensure_launchers(bin_dir=bindir)
    assert changed is True
    assert inst.supervision_status(bindir, rc)["active"] is True
    assert rc.read_text().count(inst.BLOCK_BEGIN) == 1

    changed_again, _ = inst.ensure_launchers(bin_dir=bindir)
    assert changed_again is False
    assert rc.read_text().count(inst.BLOCK_BEGIN) == 1


def test_ensure_launchers_repairs_non_executable_wrapper(tmp_path):
    bindir = tmp_path / "bin"
    inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    wrapper = bindir / "cl"
    wrapper.chmod(0o644)
    assert inst.supervision_status(bindir, tmp_path / ".zshrc")["wrappers"] is False

    changed, messages = inst.ensure_launchers(bin_dir=bindir, wire_rc=False)

    assert changed is True
    assert any("made executable" in message for message in messages)
    assert os.access(wrapper, os.X_OK)
    assert inst.ensure_launchers(bin_dir=bindir, wire_rc=False)[0] is False


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
    assert "PYTHONHOME=/Bundle.app/Contents/Resources" in body
    assert "exec /Bundle.app/Contents/MacOS/python -P -m acctsw" in body
    assert "SSL_CERT_FILE=/Bundle.app/Contents/Resources/openssl.ca/cert.pem" in body
    assert "SSL_CERT_DIR=/Bundle.app/Contents/Resources/openssl.ca/no-such-file" in body
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


def test_ensure_launchers_heals_bundle_wrapper_missing_portable_ca(tmp_path, monkeypatch):
    """The 0.8.3 wrapper found the bundled stdlib but skipped py2app's TLS setup, leaving ssl to use
    the build machine's compiled-in CA path. Bootstrap must replace it with a relocatable CA path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "acctsw").write_text(
        '#!/bin/sh\n# ai guest list engine\n'
        'unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE __PYVENV_LAUNCHER__\n'
        'PYTHONHOME=/Bundle.app/Contents/Resources '
        'exec /Bundle.app/Contents/MacOS/python -m acctsw "$@"\n')
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")

    changed, _ = inst.ensure_launchers(bin_dir=bindir, wire_rc=False)

    body = (bindir / "acctsw").read_text()
    assert changed
    assert "SSL_CERT_FILE=/Bundle.app/Contents/Resources/openssl.ca/cert.pem" in body


def test_bundle_wrapper_gives_ssl_a_relocatable_verified_ca(tmp_path):
    """Execute an installed wrapper in an isolated bundle-shaped tree. The interpreter shim hands
    off to real Python after recording the bundle PYTHONHOME, so real ``ssl`` proves that a stale
    inherited build-machine path is replaced and certificate verification remains required."""
    bundle = tmp_path / "Some Other Machine" / "AI Guest List.app" / "Contents"
    resources = bundle / "Resources"
    ca_file = resources / "openssl.ca" / "cert.pem"
    ca_dir = resources / "openssl.ca" / "no-such-file"
    ca_file.parent.mkdir(parents=True)
    ca_file.write_text("simulated packaged CA bundle\n")

    bundle_python = bundle / "MacOS" / "python"
    bundle_python.parent.mkdir(parents=True)
    probe = (
        "import os, ssl; "
        "paths = ssl.get_default_verify_paths(); "
        "context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); "
        "print(os.environ['BUNDLE_TEST_PYTHONHOME']); "
        "print(paths.cafile); print(paths.capath); print(context.verify_mode)"
    )
    bundle_python.write_text(
        "#!/bin/sh\n"
        "BUNDLE_TEST_PYTHONHOME=$PYTHONHOME\n"
        "export BUNDLE_TEST_PYTHONHOME\n"
        "unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE __PYVENV_LAUNCHER__\n"
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(probe)}\n"
    )
    bundle_python.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "acctsw"
    wrapper.write_text(inst._wrapper_script("acctsw", str(bundle_python), tmp_path, bindir))
    wrapper.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "SSL_CERT_FILE": "/Library/Frameworks/Python.framework/build-machine/cert.pem",
        "SSL_CERT_DIR": "/Library/Frameworks/Python.framework/build-machine/certs",
    })

    result = subprocess.run([str(wrapper)], env=env, capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == [
        str(resources),
        str(ca_file),
        "None",
        str(ssl.CERT_REQUIRED),
    ]
    assert not ca_dir.exists()


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
    assert "/Bundle.app/Contents/MacOS/python -P -m acctsw" in (bindir / "acctsw").read_text()


def test_ensure_launchers_preserves_wrapper_with_live_interpreter(tmp_path, monkeypatch):
    """A source-install wrapper that execs a real, existing interpreter is NOT stale — the app
    bootstrap must not clobber it just because it differs from the bundle form."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    good = ('#!/bin/sh\n# ai guest list engine\n'
            f'PYTHONUTF8=1 PYTHONPATH=/src/checkout exec {sys.executable} -m acctsw "$@"\n')
    (bindir / "acctsw").write_text(good)
    monkeypatch.setattr(inst, "shell_rc_path", lambda: tmp_path / ".zshrc")
    monkeypatch.setattr(inst.sys, "executable", "/Bundle.app/Contents/MacOS/python")
    inst.ensure_launchers(bin_dir=bindir, wire_rc=False)
    assert (bindir / "acctsw").read_text() == good    # preserved verbatim


@pytest.mark.parametrize("packaged", [False, True], ids=["source", "packaged"])
def test_wrapper_uses_its_installation_from_a_conflicting_checkout(tmp_path, packaged):
    """Execute generated wrappers with foreign Python settings and a same-named cwd package."""
    installation = tmp_path / "installation with spaces"
    work = tmp_path / "unrelated checkout"
    for root, body in (
        (installation, 'print("correct installation")\n'),
        (work, 'raise RuntimeError("imported the working directory")\n'),
    ):
        package = root / "acctsw"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text(body)

    python = sys.executable
    if packaged:
        # Stand in for a bundled runtime with its own package search path, preserving all
        # command-line flags so dropping -P genuinely reproduces the reported cwd collision.
        interpreter = tmp_path / "Renamed App.app" / "Contents" / "MacOS" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(
            "#!/bin/sh\nunset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE __PYVENV_LAUNCHER__\n"
            f"PYTHONPATH={shlex.quote(str(installation))} exec {shlex.quote(sys.executable)} \"$@\"\n"
        )
        interpreter.chmod(0o755)
        python = str(interpreter)
    wrapper = tmp_path / "acctsw-wrapper"
    wrapper.write_text(inst._wrapper_script("acctsw", python, installation, tmp_path))
    wrapper.chmod(0o755)
    env = dict(os.environ, PYTHONHOME="/missing/foreign/python", PYTHONPATH=str(work),
               PYTHONEXECUTABLE="/missing/python", __PYVENV_LAUNCHER__="/missing/venv")
    result = subprocess.run([str(wrapper)], cwd=work, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "correct installation"


def test_bundle_wrapper_without_safe_path_is_repaired(tmp_path):
    interpreter = tmp_path / "Relocated.app" / "Contents" / "MacOS" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = inst._wrapper_script("acctsw", str(interpreter), tmp_path, bindir)
    (bindir / "acctsw").write_text(body.replace(" -P -m ", " -m "))
    assert inst._wrapper_stale((bindir / "acctsw").read_text())
    inst.ensure_launchers(bin_dir=bindir, python=str(interpreter), wire_rc=False)
    assert (bindir / "acctsw").read_text() == body
    assert not inst._wrapper_stale(body)
    interpreter.unlink()
    assert inst._wrapper_stale(body)  # -P must not hide a subsequently moved/deleted interpreter


def test_alias_bundle_wrapper_uses_the_source_interpreter(tmp_path):
    import plistlib
    bundle = tmp_path / "Development.app" / "Contents"
    executable = bundle / "MacOS" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    (bundle / "Info.plist").write_bytes(plistlib.dumps({
        "PythonInfoDict": {"py2app": {"alias": True}},
    }))
    source = tmp_path / "checkout"
    assert inst._wrapper_script("acctsw", str(executable), source, tmp_path) == (
        inst._wrapper_script("acctsw", sys.executable, source, tmp_path)
    )


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
    """A reinstall may show the one-time ready notification again; wiring never uses the sentinel."""
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
