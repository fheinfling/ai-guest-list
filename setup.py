"""py2app packaging for the 'ai guest list' menubar app.

Build a self-contained .app (bundles Python + pyobjc + the engine):

    .venv/bin/python setup.py py2app

Result: dist/AI Guest List.app  →  drag into /Applications.

The engine itself is stdlib-only; only the menubar shell needs pyobjc. Web assets
(app/web) are placed at Contents/Resources/web so menubar.py's
``Path(__file__).parent / "web"`` resolves inside the bundle (the main script lives
in Contents/Resources under py2app), and WKWebView can load index.html via file://.
"""
import os
import re
import subprocess
from glob import glob
from pathlib import Path

from setuptools import setup

APP = ["app/menubar.py"]

# Single source of truth for the marketing version (acctsw/__init__.py); the build number is the
# git commit count — monotonic and automatic, so every tagged build gets a fresh CFBundleVersion.
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"',
                    Path("acctsw/__init__.py").read_text()).group(1)
try:
    BUILD = subprocess.check_output(["git", "rev-list", "--count", "HEAD"],
                                    text=True, stderr=subprocess.DEVNULL).strip() or "0"
except Exception:
    BUILD = "0"

def _shippable(f: str) -> bool:
    """Keep dev-only files out of the shipped popover assets.

    The app loads Resources/web/index.html, which references exactly styles.css + bundle.js (WEB_DIR
    is relative to the py2app MAIN SCRIPT, which lands in Resources/ — not the lib/.../app/ package
    copy). A denylist, not an allowlist: a genuinely new runtime asset should ship by default, since
    the dev run reads the source tree and would never catch the omission — only the frozen app would.

      *.test.mjs      node --test files. They assert on the RETIRED save-credit strings, so a grep of
                      the shipped app hits "COMPRESSES CONTEXT" and reads as a contaminated build.
      _*              scratch (scripts/screenshots.py writes app/web/_shot.{html,js} while it runs;
                      a build racing it would ship them).
      preview.html    dev preview page — referenced by nothing.
      package.json    node test-runner config (`node --test`).
      *.mjs           the ES sources bundle.js is BUILT from. WKWebView can't load module scripts
                      over file://, which is why bundle.js exists; the sources are never fetched.
    """
    name = os.path.basename(f)
    return not (
        name.endswith(".test.mjs")
        or name.startswith("_")
        or name in ("preview.html", "package.json")
        or name.endswith(".mjs")
    )


_web = [f for f in glob("app/web/*") if os.path.isfile(f) and _shippable(f)]
DATA_FILES = [
    ("web", _web),
    ("web/fonts", glob("app/web/fonts/*")),
]

OPTIONS = {
    "argv_emulation": False,            # menubar agent: no CLI args, and avoids the Carbon dependency
    "iconfile": "app/icon.icns",        # the disco-door app icon (source: app/icon.svg)
    "packages": ["acctsw", "app"],      # engine + shell (py2app follows imports, this is belt+braces)
    "plist": {
        "CFBundleName": "AI Guest List",
        "CFBundleDisplayName": "AI Guest List",
        "CFBundleIdentifier": "com.fheinfling.aiguestlist",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": BUILD,
        "LSUIElement": True,            # status-bar only: no dock icon, no window
        "LSMinimumSystemVersion": "12.0",
    },
}

setup(
    name="AI Guest List",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
