"""Debug-symbol stripping preserves runtime files and the fail-closed signing gate."""
import importlib.util
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest


REPO = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


strip = load_script("strip-bundle")
bundle = load_script("release-bundle")
DSYM = "objc/_objc.cpython-311-darwin.so.dSYM/"
DWARF = b"\xcf\xfa\xed\xfe" + b"\0" * 8 + b"\x0a\0\0\0"


def app_lib(tmp_path):
    app = tmp_path / "AI Guest List.app"
    lib = app / "Contents/Resources/lib"
    lib.mkdir(parents=True)
    return app, lib


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                         zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_strips_only_dsym_and_preserves_zip_metadata_order_content(tmp_path, compression, capsys):
    app, lib = app_lib(tmp_path)
    path = lib / "python311.zip"
    names = ["objc/first.pyc", DSYM, DSYM + "Contents/Resources/DWARF/_objc.so",
             "objc/second.pyc", "symbols.dSYM", "symbols.dSYM.keep/keep.pyc",
             "other.dSYM/Contents/Info.plist", "objc/last.pyc"]
    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = b"archive comment\x00\xff"
        for index, name in enumerate(names):
            entry = zipfile.ZipInfo(name, (2024, 2, 3, 4, 5, 6 + index * 2))
            entry.compress_type = compression
            entry.comment = f"member {index}".encode()
            entry.extra = b"\xfe\xca\x02\x00hi"
            entry.external_attr = (0o40755 if name.endswith("/") else 0o100640) << 16
            archive.writestr(entry, DWARF if "DWARF" in name else f"content {index}".encode())
    path.chmod(0o640)
    with zipfile.ZipFile(path) as archive:
        expected = [(entry, archive.read(entry)) for entry in archive.infolist()
                    if entry.filename in [names[i] for i in (0, 3, 4, 5, 7)]]

    # Exercise the public CLI as CI does.
    result = subprocess.run([sys.executable, str(REPO / "scripts/strip-bundle.py"), str(app)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    for name in (names[1], names[2], names[6]):
        assert f"Removed: {path}!{name}\n" in result.stdout
    assert len(result.stdout.splitlines()) == 3
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    with zipfile.ZipFile(path) as archive:
        assert archive.comment == b"archive comment\x00\xff"
        assert archive.namelist() == [entry.filename for entry, data in expected]
        for actual, (original, data) in zip(archive.infolist(), expected):
            assert archive.read(actual) == data
            for field in ("date_time", "compress_type", "comment", "extra", "external_attr",
                          "internal_attr", "create_system", "CRC", "file_size"):
                assert getattr(actual, field) == getattr(original, field), field
    bundle.scan_bundle(app)

    once = path.read_bytes()
    strip.strip_bundle(app)
    assert path.read_bytes() == once
    assert capsys.readouterr().out == ""


def test_all_lib_zips_and_dsym_directories_only(tmp_path, capsys):
    app, lib = app_lib(tmp_path)
    for relative in ("python311.zip", "nested/dependency.zip"):
        path = lib / relative
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(DSYM + "Contents/Info.plist", b"debug plist")
            archive.writestr("keep.pyc", b"runtime bytecode")
    for relative in ("Contents/Frameworks/Python.dSYM", "Contents/Resources/other.dSYM"):
        directory = app / relative
        directory.mkdir(parents=True)
        (directory / "symbols").write_bytes(DWARF)
    untouched = [app / "Contents/Resources/unrelated.zip", lib / "unchanged.zip"]
    for path in untouched:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("keep.pyc" if path.parent == lib else DSYM + "keep", b"keep")
    untouched += [lib / "file.dSYM", lib / "keep.dSYM.txt", lib / "look.dSYM.keep/data"]
    for path in untouched[2:]:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"keep")
    before = {path: path.read_bytes() for path in untouched}

    strip.strip_bundle(app)

    assert not list(app.rglob("*.dSYM/"))
    assert all(path.read_bytes() == contents for path, contents in before.items())
    for relative in ("python311.zip", "nested/dependency.zip"):
        with zipfile.ZipFile(lib / relative) as archive:
            assert archive.namelist() == ["keep.pyc"]
    assert len(capsys.readouterr().out.splitlines()) == 4
    strip.strip_bundle(app)
    assert capsys.readouterr().out == ""


def test_native_runtime_member_survives_and_signer_still_refuses(tmp_path, monkeypatch):
    app, lib = app_lib(tmp_path)
    path = lib / "python311.zip"
    native = b"\xcf\xfa\xed\xfe" + b"runtime native code"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(DSYM + "Contents/Resources/DWARF/_objc.so", DWARF)
        archive.writestr("objc/_objc.so", native)
        archive.writestr("objc/__init__.pyc", b"bytecode")
    strip.strip_bundle(app)
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["objc/_objc.so", "objc/__init__.pyc"]
        assert archive.read("objc/_objc.so") == native
    stripped = path.read_bytes()
    monkeypatch.setattr(bundle.subprocess, "run", lambda *a, **kw: pytest.fail("signed before scanning"))
    with pytest.raises(ValueError, match=r"scripts/strip-bundle.py") as error:
        bundle.sign_bundle(app, "identity", REPO / "app/entitlements.plist")
    assert "python311.zip!objc/_objc.so" in str(error.value)
    assert path.read_bytes() == stripped


def test_failed_zip_rewrite_keeps_original_and_cleans_tempfile(tmp_path, monkeypatch):
    app, lib = app_lib(tmp_path)
    path = lib / "python311.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("keep.pyc", b"bytecode")
        archive.writestr(DSYM + "symbols", DWARF)
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(strip.shutil, "copyfileobj", fail)
    with pytest.raises(OSError, match="write failed"):
        strip.strip_bundle(app)
    assert path.read_bytes() == original
    assert list(lib.iterdir()) == [path]


def test_symlinks_never_modify_external_files(tmp_path):
    app, lib = app_lib(tmp_path)
    external = tmp_path / "external.dSYM"
    external.mkdir()
    path = external / "symbols.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(DSYM + "symbols", DWARF)
    original = path.read_bytes()
    (lib / "linked.dSYM").symlink_to(external, target_is_directory=True)
    (lib / "linked.zip").symlink_to(path)
    strip.strip_bundle(app)
    assert path.read_bytes() == original
    assert (lib / "linked.dSYM").is_symlink()
    assert (lib / "linked.zip").is_symlink()


def test_workflow_strips_immediately_after_build_then_scans_before_smoke():
    workflow = (REPO / ".github/workflows/release.yml").read_text()
    assert ('python setup.py py2app\n'
            '          python scripts/strip-bundle.py "dist/AI Guest List.app"') in workflow
    commands = ["python setup.py py2app", "python scripts/strip-bundle.py",
                "python scripts/release-bundle.py scan", "python scripts/smoke-bundle.py",
                "ditto -c -k --norsrc --keepParent"]
    positions = [workflow.index(command) for command in commands]
    assert positions == sorted(positions)
