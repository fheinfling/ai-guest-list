#!/usr/bin/env python3
"""Regenerate the README screenshots from the REAL app UI.

The popover and its settings view are rendered by `app/web` from one state payload. This seeds a
throwaway store with sample seats, runs it through the REAL engine (`bridge.snapshot_state`, the same
call the menubar makes), and shoots the actual web UI with headless Chrome. Nothing here mocks the
markup, so a screenshot can never advertise a feature the app no longer has — which is exactly how
the old ones came to show a "save credit" toggle for months after it was removed.

    .venv/bin/python scripts/screenshots.py

Writes docs/assets/screenshot.png (popover) and docs/assets/screenshot-settings.png (settings).
Framing (424 CSS px wide, 24px inset, 376px card, soft blue backdrop, 2x) matches the originals.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WEB = ROOT / "app" / "web"
OUT = ROOT / "docs" / "assets"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8917

# The framing measured from the original screenshots: 424 CSS px wide, 24px inset, a 376px card
# (the real popover width) on a soft blue backdrop, at 2x.
#
# In the app the popover is a fixed 376x600 window: `html,body,#root,.app{height:100%}` with
# `.main-body`/`.set-body` scrolling INSIDE it. A screenshot of that would just be the first 600px
# with the rest cut off, so we let the card grow to its full content height instead — which is what
# the originals show. This is the only deviation from how the app renders; everything else is the
# app's own CSS.
SHOT_CSS = """
  html { zoom: 2; }                    /* headless dpr is 1; zoom gives true 2x pixels */
  html, body { margin: 0; padding: 0; }
  html, body, #root, #root .app { height: auto !important; overflow: visible !important; }
  #root .main-body, #root .set-body { flex: none !important; min-height: 0 !important;
                                      overflow: visible !important; }
  body { width: 424px; padding: 24px; box-sizing: border-box;
         background: linear-gradient(180deg,#c3cfe5 0%,#bcc8dd 7%,#bcc8dd 93%,#c7d4ea 100%); }
  #root { width: 376px; margin: 0; }   /* 376 = the real popover width */
  #root .app { box-shadow: 0 18px 40px rgba(28,40,66,.28), 0 2px 8px rgba(28,40,66,.14); }
  #overlay { display: none; }
"""


def sample_payload() -> dict:
    """Sample seats, run through the real engine so the payload is exactly what the app renders."""
    from acctsw import bridge
    from acctsw.context import Context
    from acctsw.util import iso, now

    store = Path(tempfile.mkdtemp()) / "store"
    ctx = Context.for_test(store)
    ctx.ensure_dirs()
    t = now()

    def usage(p5, resets_in_min, pweek):
        return {"ok": True, "error": None, "error_streak": 0, "fetched_at": iso(t),
                "limit_reached": None,
                "windows": {"5h": {"used_pct": p5,
                                   "resets_at": iso(t + dt.timedelta(minutes=resets_in_min))},
                            "weekly": {"used_pct": pweek,
                                       "resets_at": iso(t + dt.timedelta(days=3))}}}

    def seat(email, name, plan, u, limited_until=None):
        return {"email": email, "name": name, "plan": plan, "added_at": iso(t),
                "last_on_floor": None, "limit_source": None, "limited_until": limited_until,
                "usage": u, "account_id": f"acct:{email}"}

    s = ctx.load_state()
    s.data["tools"]["codex"] = {"active": "personal@studio.dev", "accounts": {
        "work@studio.dev": seat("work@studio.dev", "Work", "Business", usage(100, 101, 74),
                                iso(t + dt.timedelta(minutes=101))),
        "personal@studio.dev": seat("personal@studio.dev", "Personal", "Business",
                                    usage(38, 180, 21)),
    }}
    s.data["tools"]["claude"] = {"active": "studio@studio.dev", "accounts": {
        "studio@studio.dev": seat("studio@studio.dev", "Studio", "Max", usage(20, 240, 16)),
        "personal@me.dev": seat("personal@me.dev", "Personal", "Pro", usage(5, 300, 4)),
    }}
    s.data["moved_note"] = "auto-moved Codex · Work → Personal — Work's resting"
    s.data["last_switch_at"] = iso(t)
    s.settings().update({"theme": "light", "auto_switch": True, "strategy": "most_headroom",
                         "same_tool_only": True, "notify": True, "restart_app": False,
                         "celebrations": True})
    s.save()
    return bridge.snapshot_state(ctx)


def shoot(name: str, settings_view: bool, payload: dict) -> Path:
    """Render one view and screenshot it, via a temporary page inside app/web (relative asset paths
    and the page's own CSP mean it has to be served from there)."""
    page = WEB / "_shot.html"
    boot = WEB / "_shot.js"
    call = ("window.AGL.result({state: P, settings_panel: true})" if settings_view
            else "window.AGL.update(P)")
    # Stamp the rendered height into the DOM so pass 1 can read it back: --screenshot only ever
    # captures the viewport, so the window has to be sized to the content before the real shot.
    boot.write_text(
        f"const P = {json.dumps(payload)};\n{call};\n"
        "document.title = 'H' + Math.ceil(document.documentElement.getBoundingClientRect().height);\n")
    page.write_text(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="styles.css">'
        f"<style>{SHOT_CSS}</style></head><body>"
        '<div id="root"></div><script src="bundle.js"></script>'
        '<script src="_shot.js"></script></body></html>')
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(PORT)],
                           cwd=WEB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp = Path(tempfile.mkdtemp())
    url = f"http://127.0.0.1:{PORT}/_shot.html"
    common = [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
              "--default-background-color=00000000"]
    try:
        # pass 1: measure
        dom = subprocess.run(common + ["--dump-dom", "--window-size=848,900", url],
                             check=True, capture_output=True, text=True, timeout=90, cwd=tmp).stdout
        m = re.search(r"<title>H(\d+)</title>", dom)
        if not m:
            raise RuntimeError("could not measure rendered height (did the UI render?)")
        height = int(m.group(1))
        # pass 2: shoot at exactly the content height
        subprocess.run(common + [f"--screenshot={tmp / 'out.png'}",
                                 f"--window-size=848,{height}", url],
                       check=True, capture_output=True, timeout=90, cwd=tmp)
        dest = OUT / name
        shutil.copy(tmp / "out.png", dest)
        return dest
    finally:
        srv.terminate()
        page.unlink(missing_ok=True)
        boot.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    payload = sample_payload()
    for name, settings_view in (("screenshot.png", False), ("screenshot-settings.png", True)):
        out = shoot(name, settings_view, payload)
        print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
