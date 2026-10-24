"""Exercise the release shell flow using local stand-ins for GitHub and Apple services."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS ditto and preflight")

# Never invoke real gh/security/xcrun/codesign/spctl in these tests. ditto is real,
# exercising both app extraction and the final, manifest-hashed zip.
STAND_IN = r'''#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
root = Path(os.environ["RELEASE_TEST_ROOT"])
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
state_path = root / "state.json"
state = json.loads(state_path.read_text())
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
def save():
    state_path.write_text(json.dumps(state))
def option(name):
    return args[args.index(name) + 1]
if tool == "gh":
    if args[:3] == ["attestation", "verify", "--help"]:
        if state.get("attestation_unavailable"):
            sys.exit("unknown command: attestation")
        print("--repo --signer-workflow --source-ref --cert-identity --cert-oidc-issuer --deny-self-hosted-runners")
    elif args[:2] == ["attestation", "verify"]:
        sys.exit(state.get("attestation_exit", 0))
    elif args[:2] == ["auth", "status"]:
        pass
    elif args[:2] == ["release", "view"]:
        fields = option("--json").split(",")
        print(json.dumps({key: state[key] for key in fields}))
    elif args[:2] == ["release", "download"]:
        destination = Path(option("--dir"))
        destination.mkdir()
        name = option("--pattern")
        shutil.copyfile(root / "assets" / name, destination / name)
    elif args[:2] == ["release", "upload"]:
        for arg in args:
            if Path(arg).is_file():
                shutil.copyfile(arg, root / "assets" / Path(arg).name)
                if not any(a["name"] == Path(arg).name for a in state["assets"]):
                    state["assets"].append({"name": Path(arg).name})
        save()
    elif args[:2] == ["release", "delete-asset"]:
        (root / "assets" / args[3]).unlink()
        state["assets"] = [a for a in state["assets"] if a["name"] != args[3]]
        save()
    elif args[:2] == ["release", "edit"]:
        if "--notes-file" in args:
            state["body"] = Path(option("--notes-file")).read_text()
        if "--draft=false" in args:
            state["isDraft"] = False
        save()
    else:
        sys.exit("unexpected mock gh invocation")
elif tool == "security":
    print('  1) ' + 'A' * 40 + ' "Developer ID Application: Fixture One (TESTTEAM01)"')
    print('  2) ' + 'B' * 40 + ' "Developer ID Application: Fixture Two (TESTTEAM01)"')
elif tool == "xcrun":
    if args[:2] == ["notarytool", "submit"]:
        print(json.dumps({"status": state.get("notary_status", "Accepted"), "id": "test-submission"}))
        sys.exit(state.get("notary_exit", 0))
    elif args[:2] == ["notarytool", "log"]:
        print("mock rejection log")
elif tool == "codesign":
    if "--sign" in args:
        assert option("--sign") == os.environ.get("EXPECTED_IDENTITY", "B" * 40)
        assert "--timestamp" in args and "--deep" not in args
    else:
        assert "--verify" in args
        if "-R" in args:
            expected = (Path(os.environ["RELEASE_TEST_REPO"]) / "scripts/update-requirement.txt").read_text().strip()
            assert option("-R") == "=" + expected
elif tool != "spctl":
    sys.exit("unexpected stand-in tool")
'''


@pytest.fixture
def release_cli(tmp_path):
    app = tmp_path / "AI Guest List.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    # Recognizable MH_EXECUTE header; codesign is mocked for this service-flow test.
    (app / "Contents/MacOS/main").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 8 + b"\x02\0\0\0")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.fheinfling.aiguestlist", "CFBundleShortVersionString": "1.3.0",
        "CFBundleVersion": "412", "LSMinimumSystemVersion": "12.0",
        "CFBundleExecutable": "main", "CFBundlePackageType": "APPL"}))
    assets = tmp_path / "assets"
    assets.mkdir()
    unsigned = "AI-Guest-List-v1.3.0-unsigned.zip"
    subprocess.run(["ditto", "-c", "-k", "--norsrc", "--keepParent", str(app), str(assets / unsigned)], check=True)
    state = {"isDraft": True, "isPrerelease": False,
             "body": "Draft awaiting local Developer ID signing and notarization.\n\n"
                     "## What's Changed\n* Fixture release notes\n\n**Full Changelog**: fixture\n",
             "assets": [{"name": unsigned}]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("gh", "security", "xcrun", "codesign", "spctl"):
        command = bindir / tool
        command.write_text(STAND_IN)
        command.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {"AGL_SIGN_ID", "AGL_SKIP_NOTARIZE", "AGL_NOTARY_PROFILE", "CI", "GITHUB_ACTIONS"}}
    env.update(RELEASE_TEST_ROOT=str(tmp_path), RELEASE_TEST_REPO=str(REPO),
               PATH=str(bindir) + os.pathsep + env["PATH"], HOME=str(tmp_path / "home"),
               TMPDIR=str(tmp_path), PY=sys.executable)

    def run(*flags):
        return subprocess.run(["bash", str(REPO / "scripts/release-sign.sh"), "v1.3.0", *flags],
                              env=env, text=True, capture_output=True)
    return tmp_path, env, run


def read_state(root):
    return json.loads((root / "state.json").read_text())


def calls(root):
    return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]


def test_signed_draft_requires_attested_unsigned_input_on_rerun(release_cli):
    root, env, run = release_cli
    env.update(AGL_SIGN_ID="A" * 40, EXPECTED_IDENTITY="A" * 40, AGL_NOTARY_PROFILE="fixture-profile")
    result = run("--no-publish")
    assert result.returncode == 0, result.stdout + result.stderr
    state = read_state(root)
    assert state["isDraft"] is True
    assert {a["name"] for a in state["assets"]} == {"AI-Guest-List-v1.3.0.zip", "latest.json"}
    assert "## What's Changed\n* Fixture release notes\n\n**Full Changelog**: fixture\n" in state["body"]
    assert "Draft awaiting" not in state["body"]
    assert "Developer ID signed and notarized" in state["body"]
    assert "updates itself in-app" not in state["body"]
    manifest = json.loads((root / "assets/latest.json").read_text())
    archive = (root / "assets/AI-Guest-List-v1.3.0.zip").read_bytes()
    assert manifest["sha256"] == hashlib.sha256(archive).hexdigest()
    assert manifest["size"] == len(archive)
    assert manifest["sha256"] in result.stdout
    assert not list(root.glob("agl-release.*")), "temporary directory was not cleaned"
    assert not any("--draft=false" in command for command in calls(root))
    assert any(command[:3] == ["xcrun", "notarytool", "history"]
               and "fixture-profile" in command for command in calls(root))
    assert any(command[:3] == ["xcrun", "stapler", "validate"] for command in calls(root))
    assert any(command[:2] == ["spctl", "--assess"] for command in calls(root))

    result = run()
    assert result.returncode != 0
    assert "unsigned app zip" in result.stderr
    assert read_state(root)["isDraft"] is True


def test_default_identity_selects_last_fingerprint(release_cli):
    root, _, run = release_cli
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_state(root)["isDraft"] is False
    assert any(command[:3] == ["gh", "release", "edit"] and "--draft=false" in command
               and "--latest" in command for command in calls(root))


@pytest.mark.parametrize("failure", [{"attestation_exit": 1}, {"attestation_unavailable": True}])
def test_provenance_failure_refuses_before_signing_or_apple(release_cli, failure):
    root, _, run = release_cli
    state = read_state(root)
    state.update(failure)
    (root / "state.json").write_text(json.dumps(state))
    result = run()
    assert result.returncode != 0
    assert "attestation" in result.stderr
    commands = calls(root)
    assert not any(c[0] in {"codesign", "security", "xcrun", "spctl"} for c in commands)
    assert not any(c[:2] == ["gh", "release"] and c[2] in {"upload", "edit", "delete-asset"}
                   for c in commands)
    assert read_state(root)["isDraft"] is True
    assert not list(root.glob("agl-release.*"))


def test_provenance_is_constrained_and_precedes_first_codesign(release_cli):
    root, _, run = release_cli
    unsigned = root / "assets/AI-Guest-List-v1.3.0-unsigned.zip"
    digest = hashlib.sha256(unsigned.read_bytes()).hexdigest()
    result = run("--no-publish")
    assert result.returncode == 0, result.stdout + result.stderr
    commands = calls(root)
    verifies = [c for c in commands if c[:3] == ["gh", "attestation", "verify"] and "--help" not in c]
    assert len(verifies) == 1
    verify = verifies[0]
    assert Path(verify[3]).name == unsigned.name
    workflow = "fheinfling/ai-guest-list/.github/workflows/release.yml"
    assert verify[4:] == [
        "--repo", "fheinfling/ai-guest-list", "--signer-workflow", workflow,
        "--source-ref", "refs/tags/v1.3.0", "--cert-identity",
        f"https://github.com/{workflow}@refs/tags/v1.3.0",
        "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
        "--deny-self-hosted-runners"]
    assert commands.index(verify) < next(i for i, c in enumerate(commands) if c[0] == "codesign")
    assert f"Verified unsigned SHA-256: {digest}" in result.stdout


@pytest.mark.parametrize("setting", ["CI", "GITHUB_ACTIONS"])
def test_ci_cannot_invoke_signing(release_cli, setting):
    root, env, run = release_cli
    env[setting] = "true"
    result = run()
    assert result.returncode != 0
    assert "local" in result.stderr and "CI" in result.stderr
    assert not (root / "calls.jsonl").exists()


def test_offline_skips_attestation_and_never_calls_services(release_cli):
    root, env, run = release_cli
    # The codesign stand-in rejects ad-hoc signing. Reaching it proves that all
    # preflight/archive checks passed without invoking any network or Apple tool.
    env.update(AGL_SIGN_ID="-", AGL_SKIP_NOTARIZE="1")
    result = run("--from-zip", str(root / "assets/AI-Guest-List-v1.3.0-unsigned.zip"))
    assert result.returncode != 0
    assert "Skipping attestation verification" in result.stdout
    assert any(c[0] == "codesign" for c in calls(root))
    assert not any(c[0] in {"gh", "security", "xcrun", "spctl"} for c in calls(root))


@pytest.mark.parametrize(("status", "exit_code"), [("Invalid", 1), ("In Progress", 0), ("Rejected", 0)])
def test_notary_rejection_logs_and_never_uploads(release_cli, status, exit_code):
    root, _, run = release_cli
    state = read_state(root)
    state.update(notary_status=status, notary_exit=exit_code)
    (root / "state.json").write_text(json.dumps(state))
    result = run()
    assert result.returncode != 0
    assert "notarization was not Accepted" in result.stderr
    assert "xcrun notarytool log test-submission" in result.stderr
    assert "mock rejection log" in result.stdout
    assert not any(command[:3] == ["gh", "release", "upload"] for command in calls(root))
    assert not list(root.glob("agl-release.*"))


def test_published_release_is_refused_before_download(release_cli):
    root, _, run = release_cli
    state = read_state(root)
    state["isDraft"] = False
    (root / "state.json").write_text(json.dumps(state))
    result = run()
    assert result.returncode != 0
    assert "must exist as a non-prerelease draft" in result.stderr
    assert not any(command[:3] == ["gh", "release", "download"] for command in calls(root))
