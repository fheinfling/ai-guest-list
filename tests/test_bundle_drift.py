"""Guard: the committed app/web/bundle.js must equal a fresh build from the .mjs sources.

Prevents shipping stale UI when render.mjs/app.mjs are edited without re-running build-web.py.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _build_module():
    spec = importlib.util.spec_from_file_location("build_web", REPO / "scripts" / "build-web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_in_sync_with_sources():
    committed = (REPO / "app" / "web" / "bundle.js").read_text()
    fresh = _build_module().build()
    assert committed == fresh, "app/web/bundle.js is stale — run `python scripts/build-web.py`"
