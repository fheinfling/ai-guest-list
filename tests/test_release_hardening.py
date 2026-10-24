"""Release provenance, action pinning, privacy and documented updater boundaries."""
import io
from pathlib import Path
import re

import pytest

from test_release_signing import bundle, write_zip

REPO = Path(__file__).resolve().parents[1]


def test_archive_probe_starts_at_zero(tmp_path, monkeypatch):
    nested = io.BytesIO(b"leading stub")
    nested.seek(0, 2)
    write_zip(nested, {"native": b"\xcf\xfa\xed\xfe"})
    outer = tmp_path / "outer.zip"
    write_zip(outer, {"nested-without-extension": nested.getvalue()})
    original = bundle.zipfile.is_zipfile
    positions = []

    def probe(stream):
        positions.append(stream.tell())
        assert stream.tell() == 0, "archive probe consumed the magic without rewinding"
        return original(stream)

    monkeypatch.setattr(bundle.zipfile, "is_zipfile", probe)
    assert bundle.archive_machos(outer, "outer") == ["outer!nested-without-extension!native"]
    assert positions == [0]


@pytest.mark.parametrize("workflow", sorted((REPO / ".github/workflows").glob("*.y*ml")),
                         ids=lambda path: path.name)
def test_every_action_is_sha_pinned(workflow):
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(.+)$", workflow.read_text(), re.M)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}\s+# v[0-9][^\n]*", action), action


def test_attestation_only_has_build_job_permissions():
    workflow = (REPO / ".github/workflows/release.yml").read_text()
    prefix, jobs = workflow.split("jobs:\n", 1)
    build, tap = jobs.split("  update-tap:\n", 1)
    assert "permissions:\n  contents: read" in prefix
    assert "id-token:" not in prefix + tap and "attestations:" not in prefix + tap
    assert "    permissions:\n      contents: write\n      id-token: write\n      attestations: write\n" in build
    assert "if: github.event_name == 'push'" in build
    assert re.search(r"uses: actions/attest-build-provenance@[0-9a-f]{40}\s+# v", build)
    assert "subject-path: AI-Guest-List-${{ github.ref_name }}-unsigned.zip" in build
    assert 'shasum -a 256 "AI-Guest-List-${GITHUB_REF_NAME}-unsigned.zip"' in build
    assert build.index("ditto -c -k") < build.index("shasum -a 256") < build.index("uses: actions/attest-build-provenance@") < build.index("uses: softprops/action-gh-release@")
    assert "release-sign.sh" not in build
    assert "notarytool" not in build and "--sign" not in build


def test_dependabot_maintains_action_pins():
    config = (REPO / ".github/dependabot.yml").read_text()
    assert re.search(r"package-ecosystem:\s*[\"']?github-actions[\"']?\s*\n\s+directory: [\"']/[\"']", config)


def test_team_identifier_only_in_mechanical_requirement():
    requirement = (REPO / "scripts/update-requirement.txt").read_text()
    team = re.search(r'subject\.OU\] = "([A-Z0-9]+)"', requirement)[1]
    paths = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]
    for path in paths:
        assert team not in path.read_text(), path.relative_to(REPO)
    for path in (REPO / "scripts").iterdir():
        if path.suffix in {".py", ".sh"}:
            for line in path.read_text().splitlines():
                assert not (line.lstrip().startswith("#") and team in line), path.name
    assert "<TEAM_ID>" in (REPO / "docs/RELEASING.md").read_text()


def test_updater_contract_requires_signature_verification():
    docs = (REPO / "docs/RELEASING.md").read_text()
    script = (REPO / "scripts/release-sign.sh").read_text()
    assert "Any updater MUST enforce" in docs and "SecStaticCode" in docs
    assert "manifest SHA-256" in docs and "codesign -R" in docs
    assert "Any updater MUST enforce" in script
    assert 'requirement="$(cat "$SCRIPT_DIR/update-requirement.txt")"' in script
    assert 'codesign --verify --deep --strict -R "=$requirement"' in script


def test_version_and_install_release_flow():
    import acctsw
    import tomllib

    assert acctsw.__version__ == "1.4.0"
    assert tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"] == "1.4.0"
    readme = (REPO / "README.md").read_text()
    assert "1.4.0" in readme and "signed and notarized" in readme
    assert "older builds" in readme and "Open Anyway" in readme
    assert "update themselves in-app" not in readme
    docs = (REPO / "docs/RELEASING.md").read_text()
    assert "scripts/release-sign.sh vX.Y.Z --no-publish" in docs
    assert docs.index("--no-publish") < docs.index("gh release edit vX.Y.Z")
    assert "attestation" in docs and "test the signed app" in docs
