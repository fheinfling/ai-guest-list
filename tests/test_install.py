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
    ctx.keychain.set(ctx.keychain_service, "codex:orig@x.com", rotated)
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


def test_purge_removes_store_and_keychain(ctx, tmp_path):
    _seed_live(ctx)
    install(ctx, bin_dir=tmp_path / "bin", register=True)
    uninstall(ctx, bin_dir=tmp_path / "bin", purge=True)
    assert ctx.keychain.get(ctx.keychain_service, _backup_account("codex")) is None
    assert ctx.keychain.get(ctx.keychain_service, "codex:orig@x.com") is None
    assert not ctx.data_dir.exists()
