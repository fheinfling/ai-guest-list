#!/usr/bin/env python3
"""Smoke-test the built macOS app through its bundled Python and terminal wrapper.

This is intentionally a standalone stdlib script.  It must not import ``acctsw`` from the checkout:
the point is to catch packaging omissions after py2app has produced the release artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import ssl
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_BUNDLE = Path("dist/AI Guest List.app")
CORE_COMMANDS = {"install", "uninstall", "add", "remove", "list", "status", "usage", "switch", "run"}

_GENERATE = r"""
import json
import sys
from pathlib import Path
import acctsw
from acctsw.install import _wrapper_script

print(json.dumps({
    "module_file": acctsw.__file__,
    "version": acctsw.__version__,
    "build": acctsw.build_number(),
    "wrapper": _wrapper_script("acctsw", sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])),
}))
"""

_PROBE = r"""
import json
import ssl
import acctsw
from acctsw import paths
from acctsw.cli import build_parser
from acctsw.usage import Usage, _confirmed_healthy, parse_codex

parser = build_parser()
subcommands = next(action.choices for action in parser._actions if action.dest == "command")
payload = {
    "plan_type": "self_serve_business_prolite",
    "rate_limit": {
        "allowed": True,
        "primary_window": {
            "used_percent": 3,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 602784,
        },
        "secondary_window": None,
    },
}
windows = parse_codex(payload)
verify_context = ssl.create_default_context()
verify_paths = ssl.get_default_verify_paths()
print(json.dumps({
    "module_file": acctsw.__file__,
    "version": acctsw.__version__,
    "build": acctsw.build_number(),
    "cafile": verify_paths.cafile,
    "capath": verify_paths.capath,
    "verify_mode": verify_context.verify_mode,
    "cert_store_stats": verify_context.cert_store_stats(),
    "canonical_codex_home": str(paths.CODEX_HOME),
    "commands": sorted(subcommands),
    "five_hour_pct": windows["5h"].used_pct,
    "weekly_pct": windows["weekly"].used_pct,
    "healthy": _confirmed_healthy(Usage(ok=True, allowed=True, windows=windows)),
}))
"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _inside(path: str, directory: Path) -> bool:
    """Lexically test a possibly zip-internal module path against a real directory."""
    try:
        Path(path).resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Keep the child's diagnostics readable even under the locale-free bundle environment.
    return subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, check=True)


def smoke(bundle_arg: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    bundle = bundle_arg.resolve()
    contents = bundle / "Contents"
    resources = contents / "Resources"
    bundle_python = contents / "MacOS" / "python"
    info_path = contents / "Info.plist"
    ca_file = resources / "openssl.ca" / "cert.pem"
    ca_dir = resources / "openssl.ca" / "no-such-file"

    for path, description in (
        (bundle_python, "bundled Python interpreter"),
        (info_path, "bundle Info.plist"),
        (ca_file, "packaged CA file"),
    ):
        _check(path.exists(), f"missing {description}: {path}")
    _check(ca_file.is_file() and ca_file.stat().st_size > 0, f"packaged CA file is empty: {ca_file}")

    info = plistlib.loads(info_path.read_bytes())
    plist_version = str(info["CFBundleShortVersionString"])
    plist_build = str(info["CFBundleVersion"])
    _check(info.get("CFBundleIdentifier") == "com.fheinfling.aiguestlist",
           "bundle has the wrong application identity")
    icon = resources / str(info.get("CFBundleIconFile", ""))
    _check(icon.is_file(), f"missing app icon: {icon}")
    _check(hashlib.sha256(icon.read_bytes()).digest()
           == hashlib.sha256((repo / "app" / "icon.icns").read_bytes()).digest(),
           "packaged app icon differs from the approved icon")

    with tempfile.TemporaryDirectory(prefix="acctsw-bundle-smoke-") as raw_tmp:
        tmp = Path(raw_tmp)
        home = tmp / "home"
        work = tmp / "work"
        bindir = tmp / "bin"
        home.mkdir()
        work.mkdir()
        bindir.mkdir()
        private_codex_home = home / ".account-switcher" / "codex-homes" / "poison-seat"

        clean_env = dict(os.environ)
        for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
            clean_env.pop(key, None)
        clean_env.update({
            "HOME": str(home),
            "CODEX_HOME": str(private_codex_home),
            # Direct bundle-Python bootstrap needed to ask the packaged installer for its wrapper.
            "PYTHONHOME": str(resources),
        })
        app_check = json.loads(_run(
            [str(contents / "MacOS" / info["CFBundleExecutable"]), "--check-app"],
            cwd=work, env=clean_env,
        ).stdout)
        _check(app_check["bundle_identifier"] == info["CFBundleIdentifier"],
               "native process did not load the application identity")
        _check(app_check["icon_valid"] is True, "AppKit could not load the application icon")
        generated = json.loads(_run(
            [str(bundle_python), "-c", _GENERATE, str(bundle_python), str(resources), str(bindir)],
            cwd=work,
            env=clean_env,
        ).stdout)

        module_file = generated["module_file"]
        _check(_inside(module_file, bundle), f"acctsw imported outside the app bundle: {module_file}")
        _check(not _inside(module_file, repo / "acctsw"), f"acctsw leaked from checkout: {module_file}")
        _check(generated["version"] == plist_version,
               f"package version {generated['version']} != Info.plist {plist_version}")
        _check(generated["build"] == plist_build,
               f"package build {generated['build']} != Info.plist {plist_build}")

        wrapper = bindir / "acctsw"
        wrapper_body = generated["wrapper"]
        wrapper.write_text(wrapper_body, encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)

        poisoned_env = dict(os.environ)
        poisoned_env.update({
            "HOME": str(home),
            "PWD": str(work),
            "CODEX_HOME": str(private_codex_home),
            "PYTHONHOME": "/Library/Frameworks/Python.framework/build-machine",
            "PYTHONPATH": str(repo),
            "PYTHONEXECUTABLE": "/build-machine/python",
            "__PYVENV_LAUNCHER__": "/build-machine/venv-launcher",
            "SSL_CERT_FILE": "/Library/Frameworks/Python.framework/build-machine/cert.pem",
            "SSL_CERT_DIR": "/Library/Frameworks/Python.framework/build-machine/certs",
        })

        version_run = _run([str(wrapper), "--version"], cwd=work, env=poisoned_env)
        _check(version_run.stdout.strip() == f"acctsw {plist_version}",
               f"generated wrapper returned unexpected version: {version_run.stdout!r}")

        needle = '-m acctsw "$@"'
        _check(wrapper_body.count(needle) == 1, "generated wrapper command has an unexpected shape")
        probe_wrapper = bindir / "acctsw-bundle-probe"
        probe_wrapper.write_text(wrapper_body.replace(needle, f"-c {shlex.quote(_PROBE)}"), encoding="utf-8")
        probe_wrapper.chmod(probe_wrapper.stat().st_mode | stat.S_IXUSR)
        # A checkout in the user's cwd must not override the packaged engine. This caused
        # installed launchers to import new code with old, missing stdlib modules (uuid).
        shadow_package = work / "acctsw"
        shadow_package.mkdir()
        (shadow_package / "__init__.py").write_text(
            'raise RuntimeError("launcher imported acctsw from the working directory")\n', encoding="utf-8")
        _run([str(wrapper), "--version"], cwd=work, env=poisoned_env)
        probe = json.loads(_run([str(probe_wrapper)], cwd=work, env=poisoned_env).stdout)

        expected_home = home / ".codex"
        _check(_inside(probe["module_file"], bundle),
               f"wrapper probe imported acctsw outside bundle: {probe['module_file']}")
        _check(not _inside(probe["module_file"], repo / "acctsw"),
               f"wrapper probe leaked acctsw from checkout: {probe['module_file']}")
        _check(probe["version"] == plist_version and probe["build"] == plist_build,
               "wrapper probe package version/build does not match Info.plist")
        _check(probe["cafile"] == str(ca_file),
               f"ssl CA file is not bundle-relative: {probe['cafile']!r}")
        _check(probe["capath"] is None and not ca_dir.exists(),
               f"ssl unexpectedly uses a CA directory: {probe['capath']!r}")
        _check(probe["verify_mode"] == ssl.CERT_REQUIRED,
               f"TLS certificate verification is not required: mode={probe['verify_mode']}")
        _check(probe["cert_store_stats"]["x509_ca"] > 0,
               f"bundled CA file loaded no trust roots: {probe['cert_store_stats']!r}")
        _check(probe["canonical_codex_home"] == str(expected_home),
               f"private inherited CODEX_HOME became canonical: {probe['canonical_codex_home']!r}")
        _check(CORE_COMMANDS <= set(probe["commands"]),
               f"packaged CLI is missing commands: {sorted(CORE_COMMANDS - set(probe['commands']))}")
        _check(probe["five_hour_pct"] is None and probe["weekly_pct"] == 3.0 and probe["healthy"] is True,
               f"packaged weekly-window parser/health result is wrong: {probe!r}")

    print(f"bundle smoke passed: {bundle} (version {plist_version}, build {plist_build})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", type=Path, default=DEFAULT_BUNDLE,
                        help=f"app bundle to inspect (default: {DEFAULT_BUNDLE})")
    args = parser.parse_args()
    try:
        smoke(args.bundle)
    except (KeyError, OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, file=sys.stderr, end="")
            if exc.stderr:
                print(exc.stderr, file=sys.stderr, end="")
        print(f"bundle smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
