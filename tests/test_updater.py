import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from acctsw import updater as U

REPO = Path(__file__).resolve().parent.parent
REQUIREMENT_FILE = REPO / "scripts" / "update-requirement.txt"
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
APP = "AI Guest List.app"


def manifest(version="1.8.0", **over):
    tag = f"v{version}"
    m = {
        "schema": 1,
        "version": version,
        "build": "120",
        "published_at": "2026-11-07T10:00:00Z",
        "url": f"{U.REPO_URL}/releases/download/{tag}/AI-Guest-List-{tag}.zip",
        "sha256": "a" * 64,
        "size": 1234,
        "minimum_system_version": "12.0",
        "notes_url": f"{U.REPO_URL}/releases/tag/{tag}",
    }
    m.update(over)
    return m


def body(m):
    return json.dumps(m).encode()


def fetcher(payload, calls=None):
    def fetch(url, limit):
        if calls is not None:
            calls.append((url, limit))
        if isinstance(payload, BaseException):
            raise payload
        return payload
    return fetch


def do_check(tmp_path, m=None, *, raw=None, current="1.7.0", system="15.1", at=T0, force=True,
             calls=None):
    payload = raw if raw is not None else body(m or manifest())
    return U.check(tmp_path, force=force, fetch=fetcher(payload, calls), current=current,
                   system_version=system, at=at)


def stored(tmp_path):
    return json.loads((tmp_path / U.STATUS_FILE).read_text())


# --- manifest validation -------------------------------------------------------------------------

def test_valid_manifest_parses():
    m = manifest()
    assert U.parse_manifest(body(m)) == m


def test_real_published_manifest_shape_parses():
    # Copied from the published v1.6.1 latest.json.
    m = {"schema": 1, "version": "1.6.1", "build": "93", "published_at": "2026-11-01T19:51:57Z",
         "url": f"{U.REPO_URL}/releases/download/v1.6.1/AI-Guest-List-v1.6.1.zip",
         "sha256": "dd54b8e6ee64ccf07035a2e8f36189937ca659706831ad56b08441ac2cbeaa56",
         "size": 20826756, "minimum_system_version": "12.0",
         "notes_url": f"{U.REPO_URL}/releases/tag/v1.6.1"}
    assert U.parse_manifest((json.dumps(m, indent=2) + "\n").encode())["version"] == "1.6.1"


@pytest.mark.parametrize("over", [
    {"schema": 2}, {"schema": "1"}, {"schema": True}, {"schema": 1.0},
    {"version": "1.8"}, {"version": "v1.8.0"}, {"version": "1.8.0-beta"}, {"version": 180},
    {"version": "１.8.0"},
    {"build": 120}, {"build": "12a"}, {"build": ""},
    {"url": f"{U.REPO_URL}/releases/download/v1.7.9/AI-Guest-List-v1.7.9.zip"},
    {"url": "https://evil.example/releases/download/v1.8.0/AI-Guest-List-v1.8.0.zip"},
    {"url": f"http://github.com/fheinfling/ai-guest-list/releases/download/v1.8.0/AI-Guest-List-v1.8.0.zip"},
    {"url": f"{U.REPO_URL}/releases/download/v1.8.0/AI-Guest-List-v1.8.0.zip?x=1"},
    {"notes_url": f"{U.REPO_URL}/releases/tag/v1.7.0"},
    {"notes_url": "https://evil.example/"},
    {"sha256": "A" * 64}, {"sha256": "a" * 63}, {"sha256": "g" * 64}, {"sha256": None},
    {"size": 0}, {"size": -1}, {"size": 300_000_000}, {"size": "1234"}, {"size": 12.0},
    {"size": True},
    {"minimum_system_version": "12"}, {"minimum_system_version": 12.0},
    {"minimum_system_version": "12.0.1.2"},
    {"published_at": "2026-11-07 10:00:00Z"}, {"published_at": "2026-11-07T10:00:00+00:00"},
    {"published_at": "2026-13-07T10:00:00Z"}, {"published_at": None},
])
def test_manifest_rule_violations_rejected(over):
    with pytest.raises(U.UpdateError):
        U.parse_manifest(body(manifest(**over)))


def test_manifest_missing_or_extra_key_rejected():
    m = manifest()
    del m["build"]
    with pytest.raises(U.UpdateError, match="keys"):
        U.parse_manifest(body(m))
    with pytest.raises(U.UpdateError, match="keys"):
        U.parse_manifest(body(manifest(extra=1)))


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[]", b"null", "\xff".encode("latin-1"),
    b'{"schema": 1, "schema": 1}', b'{"size": NaN}',
])
def test_manifest_malformed_bytes_rejected(raw):
    with pytest.raises(U.UpdateError):
        U.parse_manifest(raw)


def test_manifest_size_cap():
    m = body(manifest())
    padded = m[:-1] + b" " * (U.MAX_MANIFEST_SIZE - len(m)) + b"}"
    assert len(padded) == U.MAX_MANIFEST_SIZE + 0
    assert U.parse_manifest(padded)["version"] == "1.8.0"
    with pytest.raises(U.UpdateError, match="too large"):
        U.parse_manifest(padded + b" ")


# --- check ---------------------------------------------------------------------------------------

def test_check_newer_is_available(tmp_path):
    calls = []
    r = do_check(tmp_path, calls=calls)
    assert r == {"ok": True, "checked": True, "up_to_date": False, "version": "1.8.0", "error": None}
    assert calls == [(U.MANIFEST_URL, U.MAX_MANIFEST_SIZE)]
    assert U.MANIFEST_URL == "https://github.com/fheinfling/ai-guest-list/releases/latest/download/latest.json"
    s = U.status(tmp_path, current="1.7.0")
    assert s == {"current": "1.7.0", "version": "1.8.0", "notes_url": manifest()["notes_url"],
                 "phase": "available", "error": None, "checked_at": U.iso(T0)}
    assert oct((tmp_path / U.STATUS_FILE).stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("version", ["1.7.0", "1.6.9", "0.9.0"])
def test_check_equal_or_older_is_up_to_date(tmp_path, version):
    r = do_check(tmp_path, manifest(version))
    assert r == {"ok": True, "checked": True, "up_to_date": True, "version": None, "error": None}
    assert U.status(tmp_path, current="1.7.0") is None


def test_check_compares_numerically(tmp_path):
    assert do_check(tmp_path, manifest("1.10.0"), current="1.9.3")["version"] == "1.10.0"
    assert do_check(tmp_path, manifest("1.7.1"), current="1.7.0")["version"] == "1.7.1"


def test_check_unknown_current_offers_nothing(tmp_path):
    assert do_check(tmp_path, current="dev")["up_to_date"] is True


@pytest.mark.parametrize("system,ok", [("12.0", True), ("12", True), ("11.7.10", False),
                                       ("13.0.1", True), ("", False)])
def test_check_minimum_macos(tmp_path, system, ok):
    r = do_check(tmp_path, manifest(minimum_system_version="12.0"), system=system)
    assert r["up_to_date"] is (not ok)
    assert (U.status(tmp_path, current="1.7.0") is not None) is ok


def test_check_system_version_callable(tmp_path):
    r = U.check(tmp_path, force=True, fetch=fetcher(body(manifest(minimum_system_version="26.0"))),
                current="1.7.0", system_version=lambda: "26.1", at=T0)
    assert r["version"] == "1.8.0"


def test_check_is_throttled(tmp_path):
    calls = []
    do_check(tmp_path, calls=calls)
    r = do_check(tmp_path, calls=calls, force=False, at=T0 + timedelta(hours=19))
    assert r == {"ok": True, "checked": False, "up_to_date": False, "version": "1.8.0", "error": None}
    assert len(calls) == 1
    do_check(tmp_path, calls=calls, force=True, at=T0 + timedelta(hours=1))
    assert len(calls) == 2
    # interval counts from the last check
    do_check(tmp_path, calls=calls, force=False, at=T0 + timedelta(hours=20, minutes=59))
    assert len(calls) == 2
    do_check(tmp_path, calls=calls, force=False, at=T0 + timedelta(hours=21, seconds=1))
    assert len(calls) == 3


def test_check_first_time_and_clock_backwards_not_throttled(tmp_path):
    calls = []
    do_check(tmp_path, calls=calls, force=False)
    do_check(tmp_path, calls=calls, force=False, at=T0 - timedelta(days=1))
    assert len(calls) == 2


def test_throttled_check_when_up_to_date(tmp_path):
    do_check(tmp_path, manifest("1.7.0"))
    r = do_check(tmp_path, force=False, at=T0 + timedelta(minutes=5))
    assert r == {"ok": True, "checked": False, "up_to_date": True, "version": None, "error": None}


def test_failed_check_records_failures_too(tmp_path):
    calls = []
    U.check(tmp_path, fetch=fetcher(OSError("down"), calls), current="1.7.0", system_version="15",
            at=T0)
    U.check(tmp_path, fetch=fetcher(OSError("down"), calls), current="1.7.0", system_version="15",
            at=T0 + timedelta(hours=1))
    assert len(calls) == 1  # failures are throttled too: no hammering while offline


@pytest.mark.parametrize("failure", [OSError("offline"), TimeoutError(),
                                     U.UpdateError("refused a download from an unexpected address"),
                                     body(manifest(sha256="nope")), b"garbage"])
def test_failed_check_keeps_known_update(tmp_path, failure):
    do_check(tmp_path)
    r = U.check(tmp_path, force=True, fetch=fetcher(failure), current="1.7.0", system_version="15",
                at=T0 + timedelta(days=1))
    assert r["ok"] is False and r["checked"] is True
    assert r["version"] == "1.8.0" and r["up_to_date"] is False
    assert r["error"] and len(r["error"]) < 120
    assert stored(tmp_path)["check_error"] == r["error"]
    assert U.status(tmp_path, current="1.7.0")["version"] == "1.8.0"


def test_failed_check_without_known_update(tmp_path):
    r = U.check(tmp_path, force=True, fetch=fetcher(OSError("x")), current="1.7.0",
                system_version="15", at=T0)
    assert r == {"ok": False, "checked": True, "up_to_date": True, "version": None,
                 "error": "couldn't reach GitHub"}
    assert U.status(tmp_path, current="1.7.0") is None


def test_successful_check_clears_check_error(tmp_path):
    U.check(tmp_path, force=True, fetch=fetcher(OSError("x")), current="1.7.0",
            system_version="15", at=T0)
    do_check(tmp_path)
    assert stored(tmp_path)["check_error"] is None


def test_newer_release_resets_phase_same_release_keeps_it(tmp_path):
    do_check(tmp_path)
    U.set_phase(tmp_path, "failed", "the download failed")
    do_check(tmp_path)  # same release: the failure stays visible
    assert U.status(tmp_path, current="1.7.0")["phase"] == "failed"
    do_check(tmp_path, manifest("1.8.1"))
    s = U.status(tmp_path, current="1.7.0")
    assert (s["version"], s["phase"], s["error"]) == ("1.8.1", "available", None)


def test_release_no_longer_latest_is_forgotten(tmp_path):
    do_check(tmp_path, manifest("1.8.0"))
    do_check(tmp_path, manifest("1.7.0"))
    assert U.status(tmp_path, current="1.7.0") is None


# --- status --------------------------------------------------------------------------------------

def test_status_none_without_file_or_garbage(tmp_path):
    assert U.status(tmp_path) is None
    (tmp_path / U.STATUS_FILE).write_text("{oops")
    assert U.status(tmp_path) is None
    (tmp_path / U.STATUS_FILE).write_text(json.dumps({"manifest": {"version": "9.9.9"}}))
    assert U.status(tmp_path) is None


def test_status_none_once_current_catches_up(tmp_path):
    do_check(tmp_path)
    assert U.status(tmp_path, current="1.7.0") is not None
    assert U.status(tmp_path, current="1.8.0") is None
    assert U.status(tmp_path, current="1.9.0") is None


def test_status_defaults_to_real_version(tmp_path):
    from acctsw import __version__
    do_check(tmp_path, manifest("999.0.0"), current=__version__)
    assert U.status(tmp_path)["current"] == __version__


def test_set_phase(tmp_path):
    do_check(tmp_path)
    U.set_phase(tmp_path, "downloading", "ignored")
    assert U.status(tmp_path, current="1.7.0")["error"] is None
    U.set_phase(tmp_path, "failed", "boom")
    s = U.status(tmp_path, current="1.7.0")
    assert (s["phase"], s["error"]) == ("failed", "boom")
    with pytest.raises(ValueError):
        U.set_phase(tmp_path, "bogus")


# --- network guard -------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://github.com/x", "https://release-assets.githubusercontent.com/a?b=c",
    "https://objects.githubusercontent.com/a", "https://GitHub.com/x", "https://github.com:443/x",
])
def test_allowed_urls(url):
    U._check_url(url)


@pytest.mark.parametrize("url", [
    "http://github.com/x", "https://evil.com/x", "https://github.com.evil.com/x",
    "https://raw.githubusercontent.com/x", "https://user:pw@github.com/x",
    "https://github.com:8443/x", "file:///etc/passwd", "ftp://github.com/x", "https://[::1/x",
])
def test_refused_urls(url):
    with pytest.raises(U.UpdateError):
        U._check_url(url)


def test_redirect_handler_refuses_bad_hop():
    h = U._AllowlistRedirects()
    with pytest.raises(U.UpdateError):
        h.redirect_request(None, None, 302, "Found", {}, "http://release-assets.githubusercontent.com/x")


def test_default_fetch_caps_bytes(monkeypatch):
    class Resp(io.BytesIO):
        def geturl(self):
            return "https://release-assets.githubusercontent.com/x"
    monkeypatch.setattr(U, "_open", lambda url: Resp(b"x" * 20))
    assert U._default_fetch(U.MANIFEST_URL, 20) == b"x" * 20
    with pytest.raises(U.UpdateError):
        U._default_fetch(U.MANIFEST_URL, 19)


def test_default_fetch_stream_caps_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_open", lambda url: io.BytesIO(b"y" * 50))
    U._default_fetch_stream("https://github.com/x", tmp_path / "a", 50)
    assert (tmp_path / "a").read_bytes() == b"y" * 50
    with pytest.raises(U.UpdateError):
        U._default_fetch_stream("https://github.com/x", tmp_path / "b", 49)


def test_open_refuses_bad_start_url():
    with pytest.raises(U.UpdateError):
        U._open("http://github.com/x")


# --- prepare -------------------------------------------------------------------------------------

def make_zip(names=(f"{APP}/", f"{APP}/Contents/", f"{APP}/Contents/Info.plist")) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"" if n.endswith("/") else b"payload")
    return buf.getvalue()


def info_plist(bundle_id=U.BUNDLE_ID, version="1.8.0", build="120", minimum="12.0") -> bytes:
    d = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version,
         "CFBundleVersion": build, "LSMinimumSystemVersion": minimum}
    return plistlib.dumps({k: v for k, v in d.items() if v is not None})


def good_tree(dest: Path, **plist):
    app = dest / APP
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(info_plist(**plist))
    (app / "Contents" / "MacOS").mkdir()
    (app / "Contents" / "MacOS" / "AI Guest List").write_bytes(b"bin")
    os.symlink("MacOS/AI Guest List", app / "Contents" / "inside-link")  # internal links are fine


class FakeRun:
    def __init__(self, tree=good_tree, fail=()):
        self.tree, self.fail, self.calls = tree, set(fail), []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        tool = Path(argv[0]).name
        if tool == "ditto":
            if "ditto" in self.fail:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            self.tree(Path(argv[-1]))
        return SimpleNamespace(returncode=1 if tool in self.fail else 0, stdout="", stderr="")


def setup_prepare(tmp_path, zip_bytes=None, **over):
    data = tmp_path / "data"
    zb = zip_bytes if zip_bytes is not None else make_zip()
    m = manifest(sha256=hashlib.sha256(zb).hexdigest(), size=len(zb), **over)
    do_check(data, m)
    return data, zb, m


def stream_of(payload, seen=None):
    def fetch_stream(url, dest, limit):
        if seen is not None:
            seen.append((url, limit, U.status(dest.parent.parent.parent / "data",
                                              current="1.7.0")["phase"]))
        Path(dest).write_bytes(payload)
    return fetch_stream


def run_prepare(tmp_path, data, payload, run=None, seen=None, **kw):
    kw.setdefault("system_version", "15.1")
    return U.prepare(data, workdir=tmp_path / "work", requirement="anchor apple generic",
                     fetch_stream=stream_of(payload, seen), run=run or FakeRun(), current="1.7.0",
                     **kw)


def assert_failed(tmp_path, data, match):
    s = U.status(data, current="1.7.0")
    assert s["phase"] == "failed" and match in s["error"]
    assert list((tmp_path / "work").iterdir()) == []  # nothing staged is left behind


def test_prepare_success(tmp_path):
    data, zb, m = setup_prepare(tmp_path)
    run, seen = FakeRun(), []
    app = run_prepare(tmp_path, data, zb, run, seen)
    assert app.name == APP and app.is_dir() and app.parent.parent.parent == tmp_path / "work"
    assert seen == [(m["url"], m["size"], "downloading")]
    assert U.status(data, current="1.7.0")["phase"] == "verifying"
    argvs = [c[0] for c in run.calls]
    assert argvs[0][:3] == ["/usr/bin/ditto", "-x", "-k"]
    assert argvs[1] == ["/usr/bin/codesign", "--verify", "--deep", "--strict",
                        "-R", "=anchor apple generic", str(app)]
    assert argvs[2] == ["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)]
    assert all("PYTHONHOME" not in c[1]["env"] for c in run.calls)
    assert not list(app.parent.parent.glob("*.zip"))  # zip removed after extraction


def test_prepare_without_known_update(tmp_path):
    with pytest.raises(U.UpdateError, match="no update"):
        run_prepare(tmp_path, tmp_path / "data", b"")
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="no update"):
        U.prepare(data, workdir=tmp_path / "work", requirement="r", fetch_stream=stream_of(zb),
                  run=FakeRun(), current="1.8.0")


def test_prepare_wrong_size(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="size"):
        run_prepare(tmp_path, data, zb + b"x")
    assert_failed(tmp_path, data, "size")


def test_prepare_wrong_hash(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    bad = bytearray(zb)
    bad[-1] ^= 1
    with pytest.raises(U.UpdateError, match="checksum"):
        run_prepare(tmp_path, data, bytes(bad))
    assert_failed(tmp_path, data, "checksum")


def test_prepare_download_error(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)

    def boom(url, dest, limit):
        raise OSError("reset")
    with pytest.raises(U.UpdateError, match="download failed"):
        U.prepare(data, workdir=tmp_path / "work", requirement="r", fetch_stream=boom,
                  run=FakeRun(), current="1.7.0", system_version="15.1")
    assert_failed(tmp_path, data, "download failed")


@pytest.mark.parametrize("names", [
    ["Other.app/", "Other.app/x"],
    [f"{APP}/x", "extra.txt"],
    [f"{APP}/../../x"],
    ["/abs/x"],
])
def test_prepare_zip_member_names_rejected_before_ditto(tmp_path, names):
    zb = make_zip(names)
    data, zb, _ = setup_prepare(tmp_path, zb)
    run = FakeRun()
    with pytest.raises(U.UpdateError, match="layout"):
        run_prepare(tmp_path, data, zb, run)
    assert run.calls == []
    assert_failed(tmp_path, data, "layout")


def test_prepare_not_a_zip(tmp_path):
    data, zb, _ = setup_prepare(tmp_path, b"PK not really")
    with pytest.raises(U.UpdateError, match="valid zip"):
        run_prepare(tmp_path, data, zb)


def test_prepare_ditto_failure(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="unpack"):
        run_prepare(tmp_path, data, zb, FakeRun(fail={"ditto"}))
    assert_failed(tmp_path, data, "unpack")


def _two_apps(dest):
    good_tree(dest)
    (dest / "Second.app").mkdir()


def _app_is_file(dest):
    (dest / APP).write_bytes(b"x")


def _app_is_link(dest):
    good_tree(dest.parent / "elsewhere")
    os.symlink(dest.parent / "elsewhere" / APP, dest / APP)


def _wrong_name(dest):
    good_tree(dest)
    (dest / APP).rename(dest / "AI Guest List 2.app")


def _empty(dest):
    pass


@pytest.mark.parametrize("tree", [_two_apps, _app_is_file, _app_is_link, _wrong_name, _empty])
def test_prepare_extracted_layout_rejected(tmp_path, tree):
    data, zb, _ = setup_prepare(tmp_path)
    run = FakeRun(tree=tree)
    with pytest.raises(U.UpdateError, match="layout"):
        run_prepare(tmp_path, data, zb, run)
    assert [Path(c[0][0]).name for c in run.calls] == ["ditto"]  # never reached codesign


@pytest.mark.parametrize("link", ["/etc/passwd", "../../../outside", "../../..",
                                  "../../../AI Guest List.app-evil/x"])
def test_prepare_symlink_escape_rejected(tmp_path, link):
    def tree(dest):
        good_tree(dest)
        os.symlink(link, dest / APP / "Contents" / "MacOS" / "evil")
    data, zb, _ = setup_prepare(tmp_path)
    run = FakeRun(tree=tree)
    with pytest.raises(U.UpdateError, match="link outside"):
        run_prepare(tmp_path, data, zb, run)
    assert_failed(tmp_path, data, "link outside")
    assert [Path(c[0][0]).name for c in run.calls] == ["ditto"]


def test_prepare_internal_relative_links_allowed(tmp_path):
    def tree(dest):
        good_tree(dest)
        os.symlink("../..", dest / APP / "Contents" / "MacOS" / "root")
        os.symlink("../Info.plist", dest / APP / "Contents" / "MacOS" / "plist")
    data, zb, _ = setup_prepare(tmp_path)
    assert run_prepare(tmp_path, data, zb, FakeRun(tree=tree)).is_dir()


def test_prepare_fifo_rejected(tmp_path):
    def tree(dest):
        good_tree(dest)
        os.mkfifo(dest / APP / "Contents" / "pipe")
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="unexpected file"):
        run_prepare(tmp_path, data, zb, FakeRun(tree=tree))


def test_prepare_codesign_failure(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    run = FakeRun(fail={"codesign"})
    with pytest.raises(U.UpdateError, match="isn't signed"):
        run_prepare(tmp_path, data, zb, run)
    assert [Path(c[0][0]).name for c in run.calls] == ["ditto", "codesign"]
    assert_failed(tmp_path, data, "isn't signed")


def test_prepare_spctl_failure(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="Gatekeeper"):
        run_prepare(tmp_path, data, zb, FakeRun(fail={"spctl"}))
    assert_failed(tmp_path, data, "Gatekeeper")


def test_prepare_tool_crash_or_timeout_fails_closed(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)

    def run(argv, **kw):
        if "codesign" in argv[0]:
            raise subprocess.TimeoutExpired(argv, 1)
        return FakeRun()(argv, **kw)
    with pytest.raises(U.UpdateError, match="isn't signed"):
        run_prepare(tmp_path, data, zb, run)


def test_prepare_wrong_bundle_id(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="different app"):
        run_prepare(tmp_path, data, zb,
                    FakeRun(tree=lambda d: good_tree(d, bundle_id="com.evil.app")))
    assert_failed(tmp_path, data, "different app")


def test_prepare_wrong_version(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="version"):
        run_prepare(tmp_path, data, zb, FakeRun(tree=lambda d: good_tree(d, version="1.7.0")))
    assert_failed(tmp_path, data, "version")


def test_prepare_unreadable_plist(tmp_path):
    def tree(dest):
        good_tree(dest)
        (dest / APP / "Contents" / "Info.plist").write_bytes(b"\x00garbage")
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="Info.plist"):
        run_prepare(tmp_path, data, zb, FakeRun(tree=tree))


def test_prepare_revalidates_stored_manifest(tmp_path):
    data, zb, m = setup_prepare(tmp_path)
    s = stored(data)
    s["manifest"]["url"] = "https://evil.example/x.zip"
    (data / U.STATUS_FILE).write_text(json.dumps(s))
    with pytest.raises(U.UpdateError, match="no update"):
        run_prepare(tmp_path, data, zb)


def test_prepare_wrong_build(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="build"):
        run_prepare(tmp_path, data, zb, FakeRun(tree=lambda d: good_tree(d, build="119")))
    assert_failed(tmp_path, data, "build")


@pytest.mark.parametrize("minimum", ["13.0", None])
def test_prepare_bundle_minimum_must_match_manifest(tmp_path, minimum):
    data, zb, _ = setup_prepare(tmp_path)
    with pytest.raises(U.UpdateError, match="minimum macOS"):
        run_prepare(tmp_path, data, zb, FakeRun(tree=lambda d: good_tree(d, minimum=minimum)))


def test_prepare_refuses_unsupported_macos_before_downloading(tmp_path):
    data, zb, _ = setup_prepare(tmp_path, minimum_system_version="14.0")

    def never(url, dest, limit):
        raise AssertionError("downloaded")
    with pytest.raises(U.UpdateError, match="needs macOS 14.0"):
        U.prepare(data, workdir=tmp_path / "work", requirement="r", fetch_stream=never,
                  run=FakeRun(), current="1.7.0", system_version="13.6")
    with pytest.raises(U.UpdateError, match="needs macOS"):
        U.prepare(data, workdir=tmp_path / "work", requirement="r", fetch_stream=never,
                  run=FakeRun(), current="1.7.0", system_version=lambda: "")


def test_verify_app_checks_running_macos(tmp_path):
    good_tree(tmp_path, minimum="14.0")
    app = tmp_path / APP
    kw = dict(requirement="r", version="1.8.0", build="120", minimum_system_version="14.0",
              run=FakeRun())
    U.verify_app(app, system_version="14.0", **kw)
    with pytest.raises(U.UpdateError, match="needs macOS 14.0"):
        U.verify_app(app, system_version="13.9", **kw)


def test_prepare_staging_dir_is_private(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    app = run_prepare(tmp_path, data, zb)
    stage = app.parent.parent
    assert stage.parent == tmp_path / "work"
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700


def test_prepare_creates_private_workdir(tmp_path):
    data, zb, _ = setup_prepare(tmp_path)
    run_prepare(tmp_path, data, zb)
    assert stat.S_IMODE((tmp_path / "work").stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757])
def test_prepare_refuses_shared_workdir(tmp_path, mode):
    data, zb, _ = setup_prepare(tmp_path)
    (tmp_path / "work").mkdir()
    os.chmod(tmp_path / "work", mode)
    with pytest.raises(U.UpdateError, match="writable by other users"):
        run_prepare(tmp_path, data, zb)
    assert U.status(data, current="1.7.0")["phase"] == "failed"


def test_prepare_refuses_symlinked_or_foreign_workdir(tmp_path, monkeypatch):
    data, zb, _ = setup_prepare(tmp_path)
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "work")
    with pytest.raises(U.UpdateError, match="plain folder"):
        run_prepare(tmp_path, data, zb)
    os.unlink(tmp_path / "work")
    uid = os.getuid()
    monkeypatch.setattr(U.os, "getuid", lambda: uid + 1)
    with pytest.raises(U.UpdateError, match="another user"):
        run_prepare(tmp_path, data, zb)


def _zip_entries(n):
    return [f"{APP}/"] + [f"{APP}/f{i}" for i in range(n - 1)]


def test_prepare_caps_zip_entry_count(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "MAX_ZIP_ENTRIES", 10)
    data, zb, _ = setup_prepare(tmp_path, make_zip(_zip_entries(10)))
    run_prepare(tmp_path, data, zb)
    data, zb, _ = setup_prepare(tmp_path / "b", make_zip(_zip_entries(11)))
    run = FakeRun()
    with pytest.raises(U.UpdateError, match="too many files"):
        run_prepare(tmp_path / "b", data, zb, run)
    assert run.calls == []


def test_prepare_caps_unpacked_size(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{APP}/big", b"\0" * 5000)  # compresses to a few bytes
    zb = buf.getvalue()
    monkeypatch.setattr(U, "MAX_UNPACKED_SIZE", 4999)
    data, zb, _ = setup_prepare(tmp_path, zb)
    run = FakeRun()
    with pytest.raises(U.UpdateError, match="unpacks too large"):
        run_prepare(tmp_path, data, zb, run)
    assert run.calls == []


def _disk(free_by_path):
    def usage(path):
        for prefix, free in free_by_path.items():
            if str(path).startswith(str(prefix)):
                return SimpleNamespace(total=10**12, used=0, free=free)
        return SimpleNamespace(total=10**12, used=0, free=10**12)
    return usage


def test_prepare_needs_space_to_download_and_unpack(tmp_path, monkeypatch):
    data, zb, _ = setup_prepare(tmp_path)
    monkeypatch.setattr(U.shutil, "disk_usage", _disk({tmp_path / "work": U._SPACE_MARGIN}))
    with pytest.raises(U.UpdateError, match="free disk space to download"):
        run_prepare(tmp_path, data, zb)


def test_prepare_needs_space_next_to_target(tmp_path, monkeypatch):
    zb = make_zip([f"{APP}/", f"{APP}/Contents/", f"{APP}/Contents/Info.plist"])
    data, zb, _ = setup_prepare(tmp_path, zb)
    apps = tmp_path / "Applications"
    apps.mkdir()
    unpacked = len(b"payload")
    monkeypatch.setattr(U.shutil, "disk_usage",
                        _disk({apps: U._SPACE_MARGIN + 2 * unpacked - 1}))
    run = FakeRun()
    with pytest.raises(U.UpdateError, match="free disk space to install"):
        run_prepare(tmp_path, data, zb, run, target_parent=apps)
    assert run.calls == []
    monkeypatch.setattr(U.shutil, "disk_usage", _disk({apps: U._SPACE_MARGIN + 2 * unpacked}))
    assert run_prepare(tmp_path, data, zb, target_parent=apps).is_dir()


# --- load_requirement ----------------------------------------------------------------------------

def test_load_requirement(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n")
    req = U.load_requirement([tmp_path / "missing.txt", empty, REQUIREMENT_FILE])
    assert req == REQUIREMENT_FILE.read_text().strip()
    assert req.startswith("anchor apple generic") and f'identifier "{U.BUNDLE_ID}"' in req
    with pytest.raises(U.UpdateError):
        U.load_requirement([tmp_path / "missing.txt", empty])
    with pytest.raises(U.UpdateError):
        U.load_requirement([])


# --- acknowledge / install_command -----------------------------------------------------------

def test_acknowledge(tmp_path):
    path = U.acknowledge(tmp_path, current="1.8.0")
    assert path == tmp_path / U.ACK_FILE == tmp_path / "update-ack"
    assert path.read_text().strip() == "1.8.0"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    from acctsw import __version__
    assert U.acknowledge(tmp_path).read_text().strip() == __version__


def write_app(app: Path, version: str, marker: str):
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": U.BUNDLE_ID, "CFBundleShortVersionString": version,
        "CFBundleExecutable": "AI Guest List", "CFBundlePackageType": "APPL"}))
    exe = app / "Contents" / "MacOS" / "AI Guest List"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    (app / "Contents" / "marker").write_text(marker)


def install_dirs(root):
    apps = root / "Applications"
    target = apps / APP
    write_app(target, "1.7.0", "old")
    staged = root / "work" / "update-x" / "app" / APP
    write_app(staged, "1.8.0", "new")
    data = root / "data"
    data.mkdir()
    return staged, target, apps / ".AI Guest List.app.backup", data / U.ACK_FILE


def marker(app):
    return (app / "Contents" / "marker").read_text()


def tool(tmp_path, name, body):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


class Hooks:
    """Fake opener/pkill/pgrep/codesign that log their argv to tmp_path/bin/<name>.log."""

    def __init__(self, tmp_path, *, ack_with=None, open_rc=0, codesign_rc=0):
        self.dir = tmp_path / "bin"
        log = 'printf "%s\\n" "$*" >> "$0.log"\n'
        ack = ""
        if ack_with is not None:
            # the "new app" acknowledges, but only when the new version is the one opened
            ack = (f'if grep -q new "$1/Contents/marker"; then '
                   f'printf "{ack_with}\\n" > "$UPDATER_TEST_ACK"; fi\n')
        self.kw = dict(
            opener=tool(tmp_path, "open", log + ack + f"exit {open_rc}\n"),
            pkill=tool(tmp_path, "pkill", log),
            pgrep=tool(tmp_path, "pgrep", log + "exit 1\n"),
            codesign=tool(tmp_path, "codesign", log + f"exit {codesign_rc}\n"),
            quit_wait_s=1, ack_wait_s=1)

    def log(self, name):
        path = self.dir / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []


def command(staged, target, backup, ack, hooks=None, pid=None, **kw):
    kw = {**(hooks.kw if hooks else {}), "ack": ack, "expected_current": "1.7.0",
          "requirement": "anchor apple generic", **kw}
    return U.install_command(staged, target, pid or _exited_pid(), backup, **kw)


def run_helper(argv):
    # the fake "new app" learns where to acknowledge from the env, so no path is baked into it
    env = {**os.environ, "UPDATER_TEST_ACK": argv[9]}
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)


def leftovers(target):
    return sorted(p.name for p in target.parent.iterdir() if p.name != APP)


def test_install_command_passes_values_as_args(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    argv = command(staged, target, backup, ack, pid=4242)
    assert argv[:2] == ["/bin/sh", "-c"] and argv[3] == "ai-guest-list-update"
    candidate = target.parent / ".AI Guest List.app.new-4242"
    assert argv[4:13] == [str(staged), str(target), "4242", str(backup), str(candidate), str(ack),
                          "1.7.0", "1.8.0", "anchor apple generic"]
    assert argv[13:17] == ["/usr/bin/open", "/usr/bin/codesign", "/usr/bin/pkill",
                           "/usr/bin/pgrep"]
    assert argv[17:19] == ["120", "240"]
    assert argv[19] == "^" + U._ere_escape(f"{target}/Contents/MacOS/")
    for value in argv[4:]:
        assert value not in argv[2]


def test_pkill_pattern_is_exact(tmp_path):
    import re
    weird = "/Users/a.b/Apps (x)/AI Guest List.app"
    pattern = "^" + U._ere_escape(weird + "/Contents/MacOS/")
    assert re.match(pattern, weird + "/Contents/MacOS/AI Guest List")
    assert not re.match(pattern, "/Users/aXb/Apps (x)/AI Guest List.app/Contents/MacOS/x")
    assert not re.match(pattern, "/bin/sh " + weird + "/Contents/MacOS/x")
    r = subprocess.run(["/usr/bin/grep", "-Eq", pattern],
                       input=weird + "/Contents/MacOS/AI Guest List", text=True)
    assert r.returncode == 0
    r = subprocess.run(["/usr/bin/grep", "-Eq", pattern],
                       input="/Users/aXb/Apps (x)/AI Guest List.app/Contents/MacOS/", text=True)
    assert r.returncode == 1


def test_install_command_validation(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    ok = dict(staged=staged, target=target, pid=1234, backup=backup, ack=ack,
              expected_current="1.7.0", requirement="anchor apple generic")

    def refused(why="", **over):
        with pytest.raises(U.UpdateError, match="^can't update in place: .*" + why):
            U.install_command(**{**ok, **over})

    refused(target=Path("Applications") / APP)
    refused(ack=Path("data") / "update-ack")
    refused(target=tmp_path / "Applications" / "AI Guest List")
    refused(target=tmp_path / "Applications" / "Other.app")  # missing
    refused("temporary location",
            target=Path("/private/var/folders/xx/T/AppTranslocation/ABC/d") / APP)
    refused(staged=tmp_path / "nope" / APP)
    refused("next to the app", backup=tmp_path / "elsewhere.backup")
    refused("data folder", ack=tmp_path / "nodata" / "update-ack")
    refused(pid=0)
    refused(pid=-5)
    refused(pid="123")
    refused("requirement", requirement=" ")
    refused("installed version", expected_current="dev")
    refused("isn't newer", expected_current="1.8.0")
    refused("isn't newer", expected_current="1.9.0")
    backup.mkdir()
    refused("backup")
    backup.rmdir()
    (target.parent / ".AI Guest List.app.new-1234").mkdir()
    refused("copy is in the way")
    (target.parent / ".AI Guest List.app.new-1234").rmdir()
    apps = target.parent
    os.chmod(apps, 0o500)
    try:
        if not os.access(apps, os.W_OK):  # root ignores modes
            refused("permission")
    finally:
        os.chmod(apps, 0o700)
    assert U.install_command(**ok)


def _exited_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


def test_helper_installs_when_new_app_acknowledges(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    ack.write_text("1.8.0\n")  # stale ack from an earlier attempt must not count...
    hooks = Hooks(tmp_path, ack_with="1.8.0")
    argv = command(staged, target, backup, ack, hooks, ack_wait_s=10)
    r = run_helper(argv)
    assert r.returncode == 0, r.stderr
    assert marker(target) == "new"
    assert leftovers(target) == []  # backup and candidate gone
    assert not staged.exists() and not (tmp_path / "work" / "update-x").exists()
    assert (tmp_path / "work").is_dir()
    assert hooks.log("open") == [str(target)]
    assert hooks.log("codesign") == [
        f"--verify --deep --strict -R =anchor apple generic {argv[8]}"]
    assert hooks.log("pkill") == []


def test_helper_removes_stale_ack_first(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    ack.write_text("1.8.0\n")  # ...because the helper deletes it before the swap
    hooks = Hooks(tmp_path)  # the new app never acknowledges
    r = run_helper(command(staged, target, backup, ack, hooks))
    assert r.returncode == 4
    assert marker(target) == "old"


def test_helper_wrong_ack_version_rolls_back(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path, ack_with="1.7.0")
    r = run_helper(command(staged, target, backup, ack, hooks))
    assert r.returncode == 4, r.stderr
    assert marker(target) == "old"


def test_helper_rolls_back_without_ack(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    argv = command(staged, target, backup, ack, hooks)
    r = run_helper(argv)
    assert r.returncode == 4, r.stderr
    assert "didn't confirm" in r.stderr
    assert marker(target) == "old"
    assert leftovers(target) == []
    assert hooks.log("open") == [str(target), str(target)]  # new app, then the old one again
    assert hooks.log("pkill") == [f"-f {argv[19]}"]
    assert argv[19] == "^" + U._ere_escape(f"{target}/Contents/MacOS/")


def test_helper_rolls_back_when_open_fails(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path, open_rc=1)
    r = run_helper(command(staged, target, backup, ack, hooks, ack_wait_s=30))
    assert r.returncode == 4
    assert "couldn't open" in r.stderr
    assert marker(target) == "old" and leftovers(target) == []
    assert len(hooks.log("open")) == 2 and len(hooks.log("pkill")) == 1


def test_helper_leaves_newer_install_alone(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    argv = command(staged, target, backup, ack, hooks)
    # Homebrew upgraded the app between the click and the helper running
    info = target / "Contents" / "Info.plist"
    info.write_bytes(info.read_bytes().replace(b"1.7.0", b"1.9.0"))
    before = sorted(str(p.relative_to(tmp_path)) for p in target.rglob("*"))
    r = run_helper(argv)
    assert r.returncode == 3 and "expected '1.7.0'" in r.stderr
    assert sorted(str(p.relative_to(tmp_path)) for p in target.rglob("*")) == before
    assert b"1.9.0" in info.read_bytes() and marker(target) == "old"
    assert leftovers(target) == []
    assert hooks.log("codesign") == [] and hooks.log("pkill") == []


def test_helper_leaves_an_upgrade_that_landed_during_the_copy_alone(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    info = target / "Contents" / "Info.plist"
    # Verifying the copy takes a while; Homebrew upgrades the installed app right then.
    hooks.kw["codesign"] = tool(tmp_path, "codesign-slow",
                                f'/usr/bin/sed -i "" "s/1\\.7\\.0/1.9.0/" "{info}"\nexit 0\n')
    r = run_helper(command(staged, target, backup, ack, hooks))
    assert r.returncode == 3 and "became '1.9.0' during the copy" in r.stderr
    assert b"1.9.0" in info.read_bytes() and marker(target) == "old"
    assert leftovers(target) == [] and hooks.log("pkill") == []


def test_reconcile_unsticks_a_phase_nobody_is_working_on(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for phase in ("downloading", "verifying", "installing"):
        U.set_phase(data, phase)
        U.reconcile(data)
        raw = U._load(data)
        assert raw["phase"] == "failed" and "didn't finish" in raw["error"]
    U.set_phase(data, "available")
    U.reconcile(data)
    assert U._load(data)["phase"] == "available"


def test_helper_candidate_verification_failure_leaves_target_untouched(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path, codesign_rc=1)
    r = run_helper(command(staged, target, backup, ack, hooks))
    assert r.returncode == 1 and "signature" in r.stderr
    assert marker(target) == "old" and leftovers(target) == []
    assert hooks.log("open") == [str(target)]  # only the old app is reopened
    assert hooks.log("pkill") == []


def test_helper_candidate_wrong_version_leaves_target_untouched(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    argv = command(staged, target, backup, ack, hooks)
    info = staged / "Contents" / "Info.plist"
    info.write_bytes(info.read_bytes().replace(b"1.8.0", b"1.8.1"))
    r = run_helper(argv)
    assert r.returncode == 1 and "wrong version" in r.stderr
    assert marker(target) == "old" and leftovers(target) == []


@pytest.mark.skipif(not Path("/usr/bin/codesign").exists(), reason="codesign missing")
def test_helper_real_codesign_rejects_unsigned_candidate(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    kw = {**hooks.kw, "codesign": "/usr/bin/codesign"}
    r = run_helper(U.install_command(staged, target, _exited_pid(), backup, ack=ack,
                                     expected_current="1.7.0",
                                     requirement=U.load_requirement([REQUIREMENT_FILE]), **kw))
    assert r.returncode == 1
    assert marker(target) == "old" and leftovers(target) == []


def test_helper_copy_failure_leaves_target_untouched(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    argv = command(staged, target, backup, ack, hooks)
    shutil.rmtree(staged)
    r = run_helper(argv)
    assert r.returncode == 1 and "copy failed" in r.stderr
    assert marker(target) == "old" and leftovers(target) == []
    assert hooks.log("open") == [str(target)]


def test_helper_reports_failed_restore_loudly(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    # an "open" that sabotages the rollback: it puts a file where the old app must go back
    opener = tool(tmp_path, "open", f'mkdir -p "{target}.blocker"; exit 1\n')
    hooks = Hooks(tmp_path)
    kw = {**hooks.kw, "opener": opener,
          "pkill": tool(tmp_path, "pkill2", f'chmod 555 "{target.parent}"\n')}
    try:
        r = run_helper(command(staged, target, backup, ack, **kw))
    finally:
        os.chmod(target.parent, 0o700)
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    assert r.returncode == 70
    assert "RESTORE FAILED" in r.stderr and str(backup) in r.stderr
    assert marker(backup) == "old"  # the old app still exists, at the logged path


def test_helper_hostile_names(tmp_path):
    weird = tmp_path / "a'b \"$(touch pwned)` x;"
    staged, target, backup, ack = install_dirs(weird)
    hooks = Hooks(tmp_path, ack_with="1.8.0")
    r = run_helper(command(staged, target, backup, ack, hooks, ack_wait_s=10))
    assert r.returncode == 0, r.stderr
    assert marker(target) == "new"
    assert not list(Path.cwd().glob("pwned")) and not list(tmp_path.rglob("pwned"))


def test_helper_leaves_running_app_alone(tmp_path):
    staged, target, backup, ack = install_dirs(tmp_path)
    hooks = Hooks(tmp_path)
    r = run_helper(command(staged, target, backup, ack, hooks, pid=os.getpid()))
    assert r.returncode == 2
    assert marker(target) == "old" and staged.exists() and leftovers(target) == []
    assert hooks.log("open") == []


# --- real codesign: fail closed --------------------------------------------------------------------

def _dummy_app(root: Path) -> Path:
    app = root / APP
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": U.BUNDLE_ID, "CFBundleShortVersionString": "1.8.0",
        "CFBundleExecutable": "AI Guest List", "CFBundlePackageType": "APPL"}))
    exe = app / "Contents" / "MacOS" / "AI Guest List"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return app


@pytest.mark.skipif(not Path("/usr/bin/codesign").exists(), reason="codesign missing")
def test_real_codesign_rejects_unsigned_and_adhoc_app(tmp_path):
    requirement = U.load_requirement([REQUIREMENT_FILE])
    app = _dummy_app(tmp_path)
    with pytest.raises(U.UpdateError, match="isn't signed"):
        U.verify_app(app, requirement=requirement, version="1.8.0")
    # An ad-hoc signature is a valid signature, just not ours: still rejected.
    signed = subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
                            capture_output=True)
    if signed.returncode == 0:
        assert subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                              capture_output=True).returncode == 0
        with pytest.raises(U.UpdateError, match="isn't signed"):
            U.verify_app(app, requirement=requirement, version="1.8.0")
