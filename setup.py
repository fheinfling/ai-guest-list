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
from glob import glob

from setuptools import setup

APP = ["app/menubar.py"]

_web = [f for f in glob("app/web/*") if os.path.isfile(f)]
DATA_FILES = [
    ("web", _web),
    ("web/fonts", glob("app/web/fonts/*")),
]

OPTIONS = {
    "argv_emulation": False,            # menubar agent: no CLI args, and avoids the Carbon dependency
    "packages": ["acctsw", "app"],      # engine + shell (py2app follows imports, this is belt+braces)
    "plist": {
        "CFBundleName": "AI Guest List",
        "CFBundleDisplayName": "AI Guest List",
        "CFBundleIdentifier": "com.fheinfling.aiguestlist",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
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
