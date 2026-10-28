"""Archive safety, signing order, the ad-hoc requirement check and workflow event guards."""
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_bundle", REPO / "scripts/release-bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def write_zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)


@pytest.mark.parametrize("magic", sorted(bundle.MACHO_MAGICS))
def test_archive_native_magic_is_rejected(tmp_path, magic):
    app = tmp_path / "AI Guest List.app"
    lib = app / "Contents/Resources/lib"
    lib.mkdir(parents=True)
    archive = lib / "python311.zip"
    write_zip(archive, {"native/no-extension": magic + b"native code"})
    with pytest.raises(ValueError, match=r"Mach-O hidden inside zip.*|python311.zip!native/no-extension"):
        bundle.scan_bundle(app)
    assert bundle.archive_machos(archive, "archive") == ["archive!native/no-extension"]


def test_plain_python_and_pyc_are_not_native(tmp_path):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    write_zip(app / "python311.zip", {"code.py": b"print('hello')\n", "code.pyc": b"\xa7\r\r\npyc"})
    bundle.scan_bundle(app)


def test_nested_archive_even_without_zip_extension(tmp_path):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    nested = io.BytesIO()
    write_zip(nested, {"native.so": b"\xca\xfe\xba\xbe"})
    write_zip(app / "archive.dat", {"nested.dat": nested.getvalue()})
    with pytest.raises(ValueError, match="archive.dat!nested.dat!native.so"):
        bundle.scan_bundle(app)


def test_zip_with_leading_stub_is_still_inspected(tmp_path):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    nested = io.BytesIO()
    nested.write(b"leading data")
    write_zip(nested, {"native": thin(6)})
    outer = app / "data"
    outer.write_bytes(b"outer leading data")
    with zipfile.ZipFile(outer, "a") as archive:
        archive.writestr("nested", nested.getvalue())
    with pytest.raises(ValueError, match="data!nested!native"):
        bundle.scan_bundle(app)


def test_invalid_zip_fails_closed(tmp_path):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    (app / "broken.zip").write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        bundle.scan_bundle(app)


@pytest.mark.parametrize("name", ["Other.app/Contents/Info.plist", "../escape", "/absolute",
                                  "AI Guest List.app/../../escape", "__MACOSX/sidecar"])
def test_bad_archive_layout(tmp_path, name):
    archive = tmp_path / "input.zip"
    write_zip(archive, {"AI Guest List.app/Contents/Info.plist": b"plist", name: b"bad"})
    with pytest.raises(ValueError, match="exactly one top-level"):
        bundle.check_zip(archive)


def test_external_symlink_rejected(tmp_path):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    (app / "outside").symlink_to(tmp_path / "external")
    with pytest.raises(ValueError, match="symlink escapes"):
        bundle.scan_bundle(app)


def thin(filetype):
    return b"\xcf\xfa\xed\xfe" + struct.pack("<III", 0x100000C, 0, filetype)


def test_signs_every_binary_then_containers_without_deep(tmp_path, monkeypatch):
    app = tmp_path / "AI Guest List.app"
    binary_paths = ["Contents/MacOS/main", "Contents/Frameworks/Python.framework/Versions/A/Python",
                    "Contents/Resources/lib/odd-name", "Contents/Resources/lib/module.so",
                    "Contents/Frameworks/helper.app/Contents/MacOS/helper"]
    for index, name in enumerate(binary_paths):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(thin(2 if index in {0, 4} else 6))
    # Normal framework aliases must neither cause duplicate signing nor be followed.
    framework = app / "Contents/Frameworks/Python.framework"
    (framework / "Versions/Current").symlink_to("A")
    (framework / "Python").symlink_to("Versions/Current/Python")
    helper = app / "Contents/Frameworks/helper.app"
    (helper / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundlePackageType": "APPL"}))
    commands = []
    monkeypatch.setattr(bundle.subprocess, "run", lambda command, **kwargs: commands.append(command))
    entitlements = REPO / "app/entitlements.plist"
    bundle.sign_bundle(app, "A" * 40, entitlements)
    targets = [Path(command[-1]) for command in commands]
    binaries = {app / path for path in binary_paths}
    assert set(targets[:len(binaries)]) == binaries
    assert targets[-1] == app
    assert set(targets[len(binaries):-1]) == {framework, helper}
    assert [len(path.parts) for path in targets[:len(binaries)]] == sorted(
        [len(path.parts) for path in binaries], reverse=True)
    for command in commands:
        assert "--deep" not in command
        assert "--force" in command and "--timestamp" in command
        assert command[command.index("--options") + 1] == "runtime"
        has_entitlements = "--entitlements" in command
        assert has_entitlements == (Path(command[-1]) in {app, helper, app / binary_paths[0], app / binary_paths[4]})


def test_universal_executable_detection():
    header = b"\xca\xfe\xba\xbe" + struct.pack(">I", 1)
    arch = struct.pack(">IIIII", 0x100000C, 0, 28, 16, 0)
    assert bundle.macho_executable(io.BytesIO(header + arch + thin(2)))
    assert not bundle.macho_executable(io.BytesIO(thin(6)))


def test_signing_refuses_hidden_native_before_codesign(tmp_path, monkeypatch):
    app = tmp_path / "AI Guest List.app"
    app.mkdir()
    write_zip(app / "python311.zip", {"native.so": thin(6)})
    monkeypatch.setattr(bundle.subprocess, "run", lambda *a, **kw: pytest.fail("signed before scanning"))
    with pytest.raises(ValueError, match="Mach-O hidden"):
        bundle.sign_bundle(app, "identity", REPO / "app/entitlements.plist")


def release_jobs():
    workflow = (REPO / ".github/workflows/release.yml").read_text()
    jobs = dict(re.findall(r"^  ([a-z][a-z-]+):\n(.*?)(?=^  [a-z][a-z-]+:|\Z)",
                           workflow.split("jobs:\n", 1)[1], re.M | re.S))
    return workflow, jobs


def test_workflow_drafts_on_push_and_tap_only_on_publication():
    workflow, jobs = release_jobs()
    assert list(jobs) == ["build", "sign", "publish", "update-tap"]
    assert "tags: [\"v*\"]" in workflow
    assert "release:\n    types: [published]" in workflow
    build = jobs["build"]
    assert "if: github.event_name == 'push'" in build
    assert "runs-on: macos-14" in build and 'python-version: "3.11"' in build
    assert "needs: build" in jobs["sign"] and "needs: sign" in jobs["publish"]
    publish = jobs["publish"]
    assert "draft: true" in publish
    assert "AI-Guest-List-${{ github.ref_name }}.zip\n            latest.json\n" in publish
    assert "softprops/action-gh-release@efb35369e0ad2afab669f228072c1b0d510eae64" in publish
    assert "unsigned" not in publish
    tap = jobs["update-tap"]
    assert "if: github.event_name == 'release' && github.event.action == 'published'" in tap
    assert "needs:" not in tap
    assert "gh release download" in tap and "shasum -a 256" in tap
    assert 'manifest["sha256"] != sys.argv[2]' in tap
    assert "sed -i -E '/^[[:space:]]*auto_updates[[:space:]]/d'" in tap
    assert workflow.count("secrets.TAP_PUSH_TOKEN") == 1
    assert "token: ${{ secrets.TAP_PUSH_TOKEN }}" in tap


def test_sign_job_checks_the_build_before_signing_and_after_stapling():
    sign = release_jobs()[1]["sign"]
    order = ['shasum -a 256 -c', 'release-bundle.py check-zip "$RUNNER_TEMP/in/unsigned.zip"', '["read_app"]',
             'release-bundle.py sign "$APP" --identity="$IDENTITY" --entitlements app/entitlements.plist',
             'codesign --verify --deep --strict -R "=$requirement" "$APP"', "xcrun notarytool submit",
             "xcrun stapler staple", "spctl --assess", 'release-bundle.py check-zip "$ASSET"',
             'codesign --verify --deep --strict -R "=$requirement" "$RUNNER_TEMP/final-check/AI Guest List.app"',
             'xcrun stapler validate "$RUNNER_TEMP/final-check/AI Guest List.app"', "scripts/update-manifest.py --zip"]
    positions = [sign.index(step) for step in order]
    assert positions == sorted(positions)
    # The identity is chosen by SHA-1 from the throwaway keychain, never by display name.
    assert "awk '/\"Developer ID Application:/ { print $2; exit }'" in sign
    cleanup = sign.index("- name: Delete the keychain and key files")
    assert "if: always()" in sign[cleanup:cleanup + 120]
    assert 'security delete-keychain "$KC"' in sign[cleanup:]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS native signing tools")
def test_adhoc_signing_rejects_developer_id_requirement(tmp_path):
    import _struct  # A real copied extension, never modify the installed original.

    app = tmp_path / "AI Guest List.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources/lib").mkdir(parents=True)
    # Copy bytes/mode, not SIP flags or extended attributes from the system executable.
    shutil.copy("/bin/echo", app / "Contents/MacOS/main")
    shutil.copy(_struct.__file__, app / "Contents/Resources/lib/copied.so")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.fheinfling.aiguestlist", "CFBundleShortVersionString": "1.3.0",
        "CFBundleVersion": "412", "LSMinimumSystemVersion": "12.0",
        "CFBundleExecutable": "main", "CFBundlePackageType": "APPL"}))
    result = subprocess.run([sys.executable, str(REPO / "scripts/release-bundle.py"), "sign", str(app),
                             "--identity=-", "--entitlements", str(REPO / "app/entitlements.plist"), "--adhoc"],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "copied.so" in result.stdout
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    # The release job's designated-requirement check must refuse anything but the Developer ID team.
    requirement = (REPO / "scripts/update-requirement.txt").read_text().strip()
    check = subprocess.run(["codesign", "--verify", "--deep", "--strict", "-R", "=" + requirement, str(app)],
                           text=True, capture_output=True)
    assert check.returncode != 0
    assert "code failed to satisfy specified code requirement" in check.stderr, check.stderr
