"""Feature tests for install/uninstall — factory image, restore round-trip, non-destructive."""
import json
import os
import stat

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
    assert plan.actions and all(a.startswith(("DRY", "keep", "no ", "NOTE", "skip")) or "factory" in a
                                for a in plan.actions)
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) is None
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
    install(ctx, bin_dir=tmp_path / "bin", register=False)
    # tamper the stored factory image so sha256 won't match the manifest
    ctx.keychain.set(ctx.keychain_service, _backup_account("codex"), make_codex_blob("tampered@x.com"))
    plan = uninstall(ctx, bin_dir=tmp_path / "bin")
    assert any("failed sha256" in a for a in plan.actions)


def test_purge_removes_store_and_keychain(ctx, tmp_path):
    _seed_live(ctx)
    install(ctx, bin_dir=tmp_path / "bin", register=True)
    uninstall(ctx, bin_dir=tmp_path / "bin", purge=True)
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) is None
    assert ctx.keychain.get(ctx.keychain_service, "codex:orig@x.com") is None
    assert not ctx.data_dir.exists()
