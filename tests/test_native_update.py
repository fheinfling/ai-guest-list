"""The menubar's side of an in-app update: where the running bundle is, and the hand-off."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from acctsw import updater
from acctsw.context import Context
from app import menubar

pytestmark = pytest.mark.skipif(menubar.objc is None, reason="native callbacks require PyObjC")


def _receiver(tmp_path, monkeypatch, app_path):
    ctx = Context.for_test(tmp_path / "store")
    ctx.ensure_dirs()
    resources = tmp_path / "Resources"
    resources.mkdir()
    (resources / "update-requirement.txt").write_text("anchor apple generic\n")
    bundle = SimpleNamespace(bundlePath=lambda: str(app_path), resourcePath=lambda: str(resources))
    monkeypatch.setattr(menubar, "NSBundle", SimpleNamespace(mainBundle=lambda: bundle))
    seen = {"pushes": [], "main": [], "spawned": []}
    receiver = SimpleNamespace(
        ctx=ctx, _update_inflight=True,
        _pushUpdateState=lambda: seen["pushes"].append((updater.status(ctx.data_dir, current="0.0.1") or {}).get("phase")),
        quitForUpdate_=lambda _s: None,
        performSelectorOnMainThread_withObject_waitUntilDone_=lambda sel, obj, wait: seen["main"].append(sel))
    monkeypatch.setattr(menubar.subprocess, "Popen", lambda argv, **kw: seen["spawned"].append((argv, kw)))
    return receiver, seen


@pytest.mark.parametrize("where,why", [
    ("/private/var/folders/x/AppTranslocation/abc/d/AI Guest List.app", "open the app from Applications"),
    ("/tmp/not-an-app", "open the app from Applications"),
])
def test_a_copy_that_cant_be_replaced_says_so_before_downloading(tmp_path, monkeypatch, where, why):
    receiver, seen = _receiver(tmp_path, monkeypatch, Path(where))
    monkeypatch.setattr(updater, "prepare", lambda *a, **k: pytest.fail("downloaded anyway"))
    menubar.AGLDelegate.updateInstallBg_.callable(receiver, None)
    assert seen["spawned"] == [] and receiver._update_inflight is False
    raw = (Path(receiver.ctx.data_dir) / updater.STATUS_FILE)
    assert why in raw.read_text()


def test_a_read_only_folder_says_so_before_downloading(tmp_path, monkeypatch):
    folder = tmp_path / "Applications"
    (folder / "AI Guest List.app").mkdir(parents=True)
    folder.chmod(0o555)
    try:
        receiver, seen = _receiver(tmp_path, monkeypatch, folder / "AI Guest List.app")
        monkeypatch.setattr(updater, "prepare", lambda *a, **k: pytest.fail("downloaded anyway"))
        menubar.AGLDelegate.updateInstallBg_.callable(receiver, None)
        assert seen["spawned"] == [] and receiver._update_inflight is False
        assert "isn't writable" in (Path(receiver.ctx.data_dir) / updater.STATUS_FILE).read_text()
    finally:
        folder.chmod(0o755)


def test_a_verified_update_hands_over_to_the_helper_and_quits(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "AI Guest List.app"
    app.mkdir(parents=True)
    receiver, seen = _receiver(tmp_path, monkeypatch, app)
    staged = tmp_path / "staged" / "AI Guest List.app"
    calls = {}

    def prepare(data_dir, *, workdir, requirement, target_parent=None, **_k):
        calls["requirement"] = requirement
        calls["prepare_target_parent"] = target_parent
        return staged

    def install_command(s, target, pid, backup, *, ack, expected_current, requirement, **_k):
        calls["install"] = (s, target, backup)
        calls["install_kw"] = (ack, expected_current, requirement)
        return ["/bin/sh", "-c", "true"]
    monkeypatch.setattr(updater, "prepare", prepare)
    monkeypatch.setattr(updater, "install_command", install_command)
    menubar.AGLDelegate.updateInstallBg_.callable(receiver, None)
    assert calls["requirement"] == "anchor apple generic"   # the bundled copy, not a default
    assert calls["install"][0] == staged and calls["install"][1] == app
    assert calls["install"][2].parent == app.parent and calls["install"][2].name.startswith(".")
    from acctsw import __version__
    assert calls["prepare_target_parent"] == app.parent        # space checked where it lands
    ack, expected, requirement = calls["install_kw"]
    assert ack == Path(receiver.ctx.data_dir) / updater.ACK_FILE
    assert expected == __version__ and requirement == "anchor apple generic"
    (argv, kw), = seen["spawned"]
    assert argv == ["/bin/sh", "-c", "true"] and kw["start_new_session"] is True
    assert kw["stderr"] is not None and kw["stderr"] != menubar.subprocess.DEVNULL
    assert len(seen["main"]) == 1                         # quit, on the main thread
    assert receiver._update_inflight is True              # never reopened: the app is quitting


def test_a_failed_download_or_check_shows_failed_and_allows_another_try(tmp_path, monkeypatch):
    app = tmp_path / "Applications" / "AI Guest List.app"
    app.mkdir(parents=True)
    receiver, seen = _receiver(tmp_path, monkeypatch, app)

    def prepare(*a, **k):
        raise updater.UpdateError("the download didn't match its checksum")
    monkeypatch.setattr(updater, "prepare", prepare)
    menubar.AGLDelegate.updateInstallBg_.callable(receiver, None)
    assert seen["spawned"] == [] and seen["main"] == [] and receiver._update_inflight is False
    raw = (Path(receiver.ctx.data_dir) / updater.STATUS_FILE).read_text()
    assert '"failed"' in raw and "checksum" in raw
