"""Released bundles must not ship CPython's test suites or any PEM private key."""
import ast
import subprocess
import sys
import io
from pathlib import Path
import zipfile

import pytest

from test_release_signing import REPO, bundle, write_zip

# Assembled at runtime so this file itself never contains a key header.
KEY = b"-----BEGIN " + b"PRIVATE KEY-----\nMIIfake\n-----END " + b"PRIVATE KEY-----\n"


def clean_app(tmp_path):
    app = tmp_path / "AI Guest List.app"
    lib = app / "Contents/Resources/lib"
    (lib / "python3.11/acctsw").mkdir(parents=True)
    (lib / "python3.11/acctsw/__init__.py").write_text("")
    write_zip(lib / "python311.zip", {"encodings/__init__.pyc": b"x", "unittest/case.pyc": b"x",
                                      "ssl.pyc": b"-----BEGIN CERTIFICATE-----"})
    return app


@pytest.mark.parametrize("name, expected", [
    ("test/certdata/keycert.pem", True), ("test/__init__.pyc", True),
    ("unittest/test/test_case.pyc", True), ("lib2to3/tests/data/x.py", True),
    ("idlelib/idle_test/htest.pyc", True), ("test.pyc", False), ("unittest/case.pyc", False),
    ("contest/x.pyc", False), ("acctsw/testing.pyc", False), ("tests", False)])
def test_stdlib_test_member(name, expected):
    assert bundle.stdlib_test_member(name) is expected


def test_clean_bundle_passes(tmp_path):
    bundle.scan_bundle(clean_app(tmp_path))  # a certificate header alone is fine


def test_test_package_in_stdlib_zip_is_refused(tmp_path):
    app = clean_app(tmp_path)
    write_zip(app / "Contents/Resources/lib/python311.zip", {"test/support/__init__.pyc": b"x"})
    with pytest.raises(ValueError, match="stdlib test suite"):
        bundle.scan_bundle(app)


def test_loose_test_package_is_refused(tmp_path):
    app = clean_app(tmp_path)
    (app / "Contents/Resources/lib/python3.11/test").mkdir()
    with pytest.raises(ValueError, match="stdlib test suite"):
        bundle.scan_bundle(app)


@pytest.mark.parametrize("where", ["file", "zip", "nested-zip"])
def test_private_key_anywhere_is_refused(tmp_path, where):
    app = clean_app(tmp_path)
    resources = app / "Contents/Resources"
    if where == "file":
        (resources / "web").mkdir()
        (resources / "web/demo.pem").write_bytes(KEY)
    elif where == "zip":
        write_zip(resources / "lib/python311.zip", {"vendor/key.pem": KEY})
    else:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("deep/key.pem", KEY)
        write_zip(resources / "lib/python311.zip", {"vendor/inner.zip": inner.getvalue()})
    with pytest.raises(ValueError, match="private key block"):
        bundle.scan_bundle(app)


def test_setup_excludes_stdlib_test_suites():
    tree = ast.parse((REPO / "setup.py").read_text())
    packages = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "STDLIB_TEST_PACKAGES" for t in node.targets))
    assert {"test", "unittest.test", "lib2to3.tests", "idlelib.idle_test"} <= set(packages)
    assert '"excludes": STDLIB_TEST_PACKAGES' in (REPO / "setup.py").read_text()


def test_pr_ci_scans_the_built_bundle():
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    build = ci.index("python setup.py py2app\n")
    assert ci.index('scripts/release-bundle.py scan "dist/AI Guest List.app"') > build


CASK = '''cask "ai-guest-list" do
  version "1.4.0"
  sha256 "{sha}"

  url "https://github.com/fheinfling/ai-guest-list/releases/download/v#{{version}}/AI-Guest-List-v#{{version}}.zip"

  app "AI Guest List.app"

  caveats <<~EOS
    AI Guest List is not signed or notarized, so macOS Gatekeeper blocks the first
    launch. Pick one of these:

      xattr -dr com.apple.quarantine "/Applications/AI Guest List.app"
  EOS
end
'''.format(sha="0" * 64)


def tap_cask_script():
    """The exact Python the tap job runs, de-indented as GitHub's YAML block scalar does."""
    lines = (REPO / ".github/workflows/release.yml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'CASKEOF'" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "CASKEOF")
    indent = len(lines[start]) - len(lines[start].lstrip())
    assert all(not line.strip() or line[:indent].isspace() for line in lines[start:end + 1])
    assert len(lines[end]) - len(lines[end].lstrip()) == indent  # heredoc ends at column 0
    return "\n".join(line[indent:] for line in lines[start + 1:end])


def run_tap_script(tmp_path, text):
    path = tmp_path / "ai-guest-list.rb"
    path.write_text(text)
    subprocess.run([sys.executable, "-", str(path)], input=tap_cask_script(), text=True, check=True)
    return path.read_text()


def test_tap_job_drops_unsigned_notice(tmp_path):
    out = run_tap_script(tmp_path, CASK)
    assert "not signed or notarized" not in out and "caveats" not in out and "xattr" not in out
    assert 'app "AI Guest List.app"\nend\n' in out and "\n\n\n" not in out
    assert run_tap_script(tmp_path, out) == out  # idempotent on an already-clean cask


def test_tap_job_keeps_other_caveats(tmp_path):
    other = CASK.replace("is not signed or notarized", "needs Full Disk Access")
    assert run_tap_script(tmp_path, other) == other


def test_tap_job_fails_if_notice_survives():
    workflow = (REPO / ".github/workflows/release.yml").read_text()
    assert 'if grep -Fq "not signed or notarized" "$CASK"; then' in workflow
