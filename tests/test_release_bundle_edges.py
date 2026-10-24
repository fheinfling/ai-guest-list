"""Binary format boundaries and public bundle CLI failure behavior."""
import io
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import zipfile

import pytest

from test_release_signing import bundle, thin, write_zip

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("magic", "endian"), [
    (b"\xfe\xed\xfa\xce", ">"), (b"\xce\xfa\xed\xfe", "<"),
    (b"\xfe\xed\xfa\xcf", ">"), (b"\xcf\xfa\xed\xfe", "<")])
@pytest.mark.parametrize("filetype", [2, 6])
def test_thin_formats_seek_to_requested_offset(magic, endian, filetype):
    stream = io.BytesIO(b"prefix!" + magic + struct.pack(endian + "III", 0, 0, filetype) + b"trailing")
    stream.seek(0, 2)
    assert bundle.macho_executable(stream, 7) is (filetype == 2)


def fat_binary(magic, endian, wide, slices):
    count = len(slices)
    size = 32 if wide else 20
    header = magic + struct.pack(endian + "I", count)
    table, contents = b"", b""
    for index, data in enumerate(slices):
        offset = 8 + count * size + len(contents)
        table += struct.pack(endian + ("IIQQII" if wide else "IIIII"),
                             0x100000C, index, offset, len(data), 0, *([0] if wide else []))
        contents += data
    return header + table + contents


@pytest.mark.parametrize(("magic", "endian", "wide"), [
    (b"\xca\xfe\xba\xbe", ">", False), (b"\xbe\xba\xfe\xca", "<", False),
    (b"\xca\xfe\xba\xbf", ">", True), (b"\xbf\xba\xfe\xca", "<", True)])
@pytest.mark.parametrize("last", [2, 6])
def test_universal_multi_slice_formats(magic, endian, wide, last):
    data = fat_binary(magic, endian, wide, [thin(6), thin(last)])
    stream = io.BytesIO(b"offset!" + data + b"trailing")
    assert bundle.macho_executable(stream, 7) is (last == 2)


def test_unknown_binary_and_non_native_universal_slice_are_false():
    assert bundle.macho_executable(io.BytesIO(b"unknown binary header")) is False
    data = fat_binary(b"\xca\xfe\xba\xbe", ">", False, [b"non-native slice"])
    assert bundle.macho_executable(io.BytesIO(data)) is False


def test_universal_architecture_limit():
    data = fat_binary(b"\xca\xfe\xba\xbe", ">", False, [thin(6)] * 64)
    assert bundle.macho_executable(io.BytesIO(data)) is False
    with pytest.raises(ValueError, match="architecture count"):
        bundle.macho_executable(io.BytesIO(b"\xca\xfe\xba\xbe" + struct.pack(">I", 65) + b"\0" * 8))


@pytest.mark.parametrize("relative", ["Contents/Info.plist", "Resources/Info.plist", "Info.plist"])
@pytest.mark.parametrize("kind", ["APPL", "XPC!", "FMWK"])
def test_nested_bundle_package_types(tmp_path, relative, kind):
    path = tmp_path / "helper.bundle" / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"CFBundlePackageType": kind}))
    assert bundle.bundle_executable(tmp_path / "helper.bundle") is (kind != "FMWK")
    assert bundle.bundle_executable(tmp_path / "no-plist.bundle") is False


def test_every_nested_container_is_signed(tmp_path, monkeypatch):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    (app / "main").write_bytes(thin(2))
    nested = [app / ("helper" + suffix) for suffix in (".app", ".framework", ".bundle", ".xpc", ".appex", ".plugin")]
    for path in nested:
        path.mkdir()
    commands = []
    monkeypatch.setattr(bundle.subprocess, "run", lambda command, **kw: commands.append(command))
    bundle.sign_bundle(app, "fixture", REPO / "app/entitlements.plist")
    assert {Path(c[-1]) for c in commands} == {app, app / "main", *nested}


def test_failed_codesign_stops_before_next_target(tmp_path, monkeypatch):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    (app / "main").write_bytes(thin(2))
    commands = []

    def fail(command, *, check):
        commands.append(command)
        if check:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(bundle.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        bundle.sign_bundle(app, "fixture", REPO / "app/entitlements.plist")
    assert len(commands) == 1


def test_archive_nesting_boundary(tmp_path):
    data = b"plain python"
    for _ in range(9):
        archive = io.BytesIO()
        write_zip(archive, {"member": data})
        data = archive.getvalue()
    assert bundle.archive_machos(io.BytesIO(data), "eight-levels") == []
    archive = io.BytesIO()
    write_zip(archive, {"member": data})
    with pytest.raises(ValueError, match="nesting too deep"):
        bundle.archive_machos(io.BytesIO(archive.getvalue()), "nine-levels")


def test_nested_archive_limit_is_inclusive(tmp_path, monkeypatch):
    path = tmp_path / "outer.zip"
    nested = io.BytesIO()
    write_zip(nested, {"code.py": b"python"})
    write_zip(path, {"nested.zip": nested.getvalue()})
    original = bundle.zipfile.ZipFile.infolist

    def entries(archive):
        result = original(archive)
        for entry in result:
            if entry.filename == "nested.zip":
                entry.file_size = 300_000_000
        return result

    monkeypatch.setattr(bundle.zipfile.ZipFile, "infolist", entries)
    with pytest.raises(ValueError, match="nested archive too large"):
        bundle.archive_machos(path, "outer")


def test_nested_zip_extension_fails_closed_and_probe_may_consume_stream(tmp_path, monkeypatch):
    path = tmp_path / "outer.zip"
    write_zip(path, {"broken.ZIP": b"invalid"})
    with pytest.raises(zipfile.BadZipFile):
        bundle.archive_machos(path, "outer")
    nested = io.BytesIO(b"stub")
    nested.seek(0, 2)
    write_zip(nested, {"native": thin(2)})
    write_zip(path, {"nested": nested.getvalue()})
    original = bundle.zipfile.is_zipfile

    def consuming_probe(stream):
        result = original(stream)
        stream.seek(0, 2)  # Match Python versions whose ZIP probe leaves the cursor at EOF.
        return result

    monkeypatch.setattr(bundle.zipfile, "is_zipfile", consuming_probe)
    assert bundle.archive_machos(path, "outer") == ["outer!nested!native"]


def test_empty_and_backslash_archives_refused(tmp_path):
    path = tmp_path / "input.zip"
    for members in ({}, {"AI Guest List.app/bad\\name": b"x"}):
        write_zip(path, members)
        with pytest.raises(ValueError):
            bundle.check_zip(path)


@pytest.mark.parametrize("command", ["check-zip", "scan", "sign"])
def test_bundle_cli_dispatch_and_exit_status(tmp_path, monkeypatch, capsys, command):
    target = tmp_path / "target"
    argv = ["release-bundle.py", command, str(target)]
    if command == "sign":
        argv += ["--identity", "fixture", "--entitlements", "fixture.plist", "--adhoc"]
    monkeypatch.setattr(sys, "argv", argv)
    calls = []

    def handler(*args, **kwargs):
        calls.append((args, kwargs))

    name = {"check-zip": "check_zip", "scan": "scan_bundle", "sign": "sign_bundle"}[command]
    monkeypatch.setattr(bundle, name, handler)
    assert bundle.main() == 0
    assert calls == [((target, "fixture", Path("fixture.plist")), {"adhoc": True})] if command == "sign" else calls == [((target,), {})]

    def fail(*args, **kwargs):
        raise ValueError("bad input")

    monkeypatch.setattr(bundle, name, fail)
    assert bundle.main() == 1
    assert "bad input" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["sign", "app", "--entitlements", "e.plist"],
                                 ["sign", "app", "--identity", "fixture"]])
def test_bundle_cli_requires_command_and_signing_arguments(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["release-bundle.py", *args])
    with pytest.raises(SystemExit) as error:
        bundle.main()
    assert error.value.code == 2
