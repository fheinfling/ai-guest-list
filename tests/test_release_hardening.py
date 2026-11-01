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


APPLE_SECRETS = ("secrets.MACOS_CERT_P12", "secrets.MACOS_CERT_PASSWORD", "secrets.NOTARY_API_KEY",
                 "secrets.NOTARY_API_KEY_ID", "secrets.NOTARY_API_ISSUER")


def test_signing_secrets_and_attestation_are_confined():
    from test_release_signing import release_jobs

    workflow, jobs = release_jobs()
    prefix = workflow.split("jobs:\n", 1)[0]
    assert "permissions:\n  contents: read" in prefix
    sign = jobs["sign"]
    # Apple credentials exist only in the sign job, which GitHub runs only after the owner approves it.
    assert "    environment: release\n" in sign
    for secret in APPLE_SECRETS:
        assert secret in sign, secret
    for name, job in jobs.items():
        if name != "sign":
            assert not any(secret in job for secret in APPLE_SECRETS), name
            assert "environment:" not in job, name
    assert "permissions:" not in sign and "id-token:" not in prefix + sign
    # Only publish (no secrets) may write releases and attestations.
    publish = jobs["publish"]
    assert "    permissions:\n      contents: write        # create the draft release\n      id-token: write" in publish
    assert "      attestations: write\n" in publish
    for name in ("build", "sign", "update-tap"):
        assert "id-token:" not in jobs[name] and "attestations:" not in jobs[name], name
    assert re.search(r"uses: actions/attest-build-provenance@[0-9a-f]{40}\s+# v", publish)
    assert "subject-path: AI-Guest-List-${{ github.ref_name }}.zip" in publish
    assert publish.index("uses: actions/attest-build-provenance@") < publish.index("uses: softprops/action-gh-release@")
    # The signing secrets never reach the build, its tests or any third-party action.
    assert "notarytool" not in jobs["build"] and "--sign" not in jobs["build"]
    for action in re.findall(r"uses: ([\w./-]+)@", sign):
        assert action.startswith("actions/"), action


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
    workflow = (REPO / ".github/workflows/release.yml").read_text()
    assert "Any updater MUST enforce" in docs and "SecStaticCode" in docs
    assert "manifest SHA-256" in docs and "codesign -R" in docs
    assert "Any updater MUST enforce" in workflow
    assert 'requirement="$(cat scripts/update-requirement.txt)"' in workflow
    assert 'codesign --verify --deep --strict -R "=$requirement"' in workflow


def test_version_and_install_release_flow():
    import acctsw
    import tomllib

    # Signed builds start at 1.4.0; the current version only has to be consistent and at least that.
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert acctsw.__version__ == version
    assert tuple(map(int, version.split("."))) >= (1, 4, 0)
    readme = (REPO / "README.md").read_text()
    assert "1.4.0" in readme and "signed and notarized" in readme
    assert "older builds" in readme and "Open Anyway" in readme
    assert "update themselves in-app" not in readme
    docs = (REPO / "docs/RELEASING.md").read_text()
    assert "Review deployments" in docs
    assert docs.index("Review deployments") < docs.index("test the signed app") < docs.index("gh release edit vX.Y.Z")
    assert "gh attestation verify" in docs
