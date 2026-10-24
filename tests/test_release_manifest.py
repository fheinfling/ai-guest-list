"""The release writer must produce exactly what the updater contract accepts."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/update-manifest.py"
spec = importlib.util.spec_from_file_location("release_manifest", SCRIPT)
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


@pytest.fixture
def artifact(tmp_path):
    app = tmp_path / "AI Guest List.app"
    (app / "Contents").mkdir(parents=True)
    info = {"CFBundleIdentifier": "com.fheinfling.aiguestlist",
            "CFBundleShortVersionString": "1.3.0", "CFBundleVersion": "412",
            "LSMinimumSystemVersion": "12.0"}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    archive = tmp_path / "AI-Guest-List-v1.3.0.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.write(app / "Contents/Info.plist", "AI Guest List.app/Contents/Info.plist")
    return app, info, archive


def test_manifest_cli_happy_path(artifact, tmp_path):
    app, info, archive = artifact
    output = tmp_path / "latest.json"
    result = subprocess.run([sys.executable, str(SCRIPT), "--zip", str(archive),
                             "--app", str(app), "--tag", "v1.3.0", "--out", str(output)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(output.read_bytes())
    assert set(value) == manifest.KEYS
    assert value == {
        "schema": 1, "version": "1.3.0", "build": "412",
        "published_at": value["published_at"],
        "url": "https://github.com/fheinfling/ai-guest-list/releases/download/v1.3.0/AI-Guest-List-v1.3.0.zip",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size": archive.stat().st_size, "minimum_system_version": "12.0",
        "notes_url": "https://github.com/fheinfling/ai-guest-list/releases/tag/v1.3.0",
    }
    assert len(output.read_bytes()) < 16 * 1024
    manifest.validate_manifest(value, tag="v1.3.0", info=info, zip_path=archive)


@pytest.mark.parametrize("tag", ["", "1.3.0", "v1.3", "v1.3.0-rc1", "v1.3.0\n",
                                "v1.3.0/extra", "v١.3.0"])
def test_bad_tag(artifact, tag):
    app, _, archive = artifact
    with pytest.raises(ValueError, match="tag"):
        manifest.make_manifest(archive, app, tag)


@pytest.mark.parametrize("key", ["CFBundleIdentifier", "CFBundleShortVersionString",
                                 "CFBundleVersion", "LSMinimumSystemVersion"])
def test_missing_plist_keys(artifact, key):
    app, info, archive = artifact
    del info[key]
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    with pytest.raises(ValueError, match=key):
        manifest.make_manifest(archive, app, "v1.3.0")


@pytest.mark.parametrize(("key", "value"), [
    ("CFBundleIdentifier", "com.example.other"),
    ("CFBundleShortVersionString", "1.2.0"),
    ("CFBundleShortVersionString", 130),
    ("CFBundleVersion", 412), ("CFBundleVersion", ""),
    ("CFBundleVersion", "4.12"), ("CFBundleVersion", "٤١٢"),
    ("LSMinimumSystemVersion", ""), ("LSMinimumSystemVersion", 12),
    ("LSMinimumSystemVersion", "twelve"),
])
def test_invalid_plist_values(artifact, key, value):
    app, info, archive = artifact
    info[key] = value
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    with pytest.raises(ValueError, match=key):
        manifest.make_manifest(archive, app, "v1.3.0")


@pytest.mark.parametrize(("key", "value"), [
    ("schema", True), ("schema", 1.0), ("schema", 2),
    ("version", "1.2.0"), ("version", "1.3.0-beta"), ("version", 130),
    ("build", "413"), ("build", ""), ("build", 412),
    ("url", "https://evil.example/AI-Guest-List-v1.3.0.zip"),
    ("url", "http://github.com/fheinfling/ai-guest-list/releases/download/v1.3.0/AI-Guest-List-v1.3.0.zip"),
    ("url", "https://github.com/fheinfling/ai-guest-list/releases/download/v1.2.0/AI-Guest-List-v1.3.0.zip"),
    ("url", "https://github.com/fheinfling/ai-guest-list/releases/download/v1.3.0/other.zip"),
    ("notes_url", "https://evil.example/releases/tag/v1.3.0"),
    ("notes_url", "https://github.com/fheinfling/ai-guest-list/releases/tag/v1.2.0"),
    ("sha256", "A" * 64), ("sha256", "0" * 63), ("sha256", "g" * 64),
    ("sha256", "0" * 64), ("sha256", 123),
    ("size", 0), ("size", -1), ("size", 300_000_000), ("size", True),
    ("size", 1.0), ("size", "100"), ("size", 1),
    ("minimum_system_version", "13.0"),
    ("published_at", "2026-10-23"), ("published_at", "2026-99-23T12:00:00Z"),
])
def test_contract_rejections(artifact, key, value):
    app, info, archive = artifact
    result = manifest.make_manifest(archive, app, "v1.3.0")
    result[key] = value
    with pytest.raises(ValueError):
        manifest.validate_manifest(result, tag="v1.3.0", info=info, zip_path=archive)


def test_hash_catches_changed_zip(artifact):
    app, info, archive = artifact
    result = manifest.make_manifest(archive, app, "v1.3.0")
    archive.write_bytes(b"x" * archive.stat().st_size)
    with pytest.raises(ValueError, match="sha256/size mismatch"):
        manifest.validate_manifest(result, tag="v1.3.0", info=info, zip_path=archive)


def test_writer_missing_and_extra_keys(artifact):
    app, info, archive = artifact
    original = manifest.make_manifest(archive, app, "v1.3.0")
    for key in original:
        value = copy.copy(original)
        del value[key]
        with pytest.raises(ValueError, match="exactly"):
            manifest.validate_manifest(value, tag="v1.3.0", info=info, zip_path=archive)
    original["extra"] = True
    with pytest.raises(ValueError, match="exactly"):
        manifest.validate_manifest(original, tag="v1.3.0", info=info, zip_path=archive)


def test_size_limits(artifact, monkeypatch):
    app, info, archive = artifact
    archive.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        manifest.make_manifest(archive, app, "v1.3.0")
    # Exercise the streaming size guard at its exact boundary without a 300 MB fixture.
    monkeypatch.setattr(manifest, "MAX_ZIP_SIZE", 10)
    archive.write_bytes(b"x" * 10)
    with pytest.raises(ValueError, match="smaller than 300 MB"):
        manifest.make_manifest(archive, app, "v1.3.0")
    archive.write_bytes(b"x")
    info["CFBundleVersion"] = "1" * (16 * 1024)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    with pytest.raises(ValueError, match="16 KB"):
        manifest.make_manifest(archive, app, "v1.3.0")


def test_cli_fails_without_writing(artifact, tmp_path):
    app, _, archive = artifact
    output = tmp_path / "latest.json"
    result = subprocess.run([sys.executable, str(SCRIPT), "--zip", str(archive),
                             "--app", str(app), "--tag", "v2.0.0", "--out", str(output)],
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "CFBundleShortVersionString" in result.stderr
    assert not output.exists()
