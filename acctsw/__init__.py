"""ai guest list — engine for switching between Codex/Claude accounts on usage limits.

The engine is intentionally stdlib-only so it can be vendored as a single directory and run
without a virtualenv. The menubar app (``app/``) is a thin UI layer on top of this package.
"""

__version__ = "0.2.2"     # marketing version — the SINGLE source of truth (setup.py reads this)
APP_NAME = "ai guest list"

# Tools this engine knows how to manage.
TOOLS = ("codex", "claude")

_BUILD_CACHE: str | None = None


def build_number() -> str:
    """The packaged build number (CFBundleVersion) when running from the .app, else "dev".

    Read from the bundle's Info.plist by locating the enclosing ``*.app`` — pyobjc-free so the CLI
    and tests can call it too. Cached: the value is invariant for the process lifetime."""
    global _BUILD_CACHE
    if _BUILD_CACHE is not None:
        return _BUILD_CACHE
    import plistlib
    from pathlib import Path
    build = "dev"
    for parent in Path(__file__).resolve().parents:
        if parent.suffix == ".app":
            try:
                info = plistlib.loads((parent / "Contents" / "Info.plist").read_bytes())
                build = str(info.get("CFBundleVersion", "dev"))
            except OSError:
                pass
            break
    _BUILD_CACHE = build
    return build
