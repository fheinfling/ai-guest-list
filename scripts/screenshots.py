#!/usr/bin/env python3
"""Regenerate the README screenshots from the REAL app UI.

The popover and its settings view are rendered by `app/web` from one state payload. This seeds a
throwaway store with sample seats, runs it through the REAL engine (`bridge.snapshot_state`, the same
call the menubar makes), and shoots the actual web UI with headless Chrome. Nothing here mocks the
markup, so a screenshot can never advertise a feature the app no longer has — which is exactly how
the old ones came to show a "save credit" toggle for months after it was removed.

    .venv/bin/python scripts/screenshots.py

Writes docs/assets/screenshot.png (popover), docs/assets/screenshot-settings.png (settings) and
docs/assets/screenshot-usage.png (token usage history). The usage history comes from synthetic Claude
and Codex logs written into the same throwaway store and indexed by the REAL `tokenlog` engine.
Framing (424 CSS px wide, 24px inset, 376px card, soft blue backdrop, 2x) matches the originals.
"""
from __future__ import annotations

import datetime as dt
import json
import random
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
# In the app the popover is a 376px window capped at 600px with `.body`/`.set-body` scrolling
# INSIDE it. A screenshot of that would just be the first 600px
# with the rest cut off, so we let the card grow to its full content height instead — which is what
# the originals show. This is the only deviation from how the app renders; everything else is the
# app's own CSS.
SHOT_CSS = """
  html { zoom: 2; }                    /* headless dpr is 1; zoom gives true 2x pixels */
  html, body { margin: 0; padding: 0; }
  html, body, #root, #root .app { height: auto !important; max-height: none !important;
                                     overflow: visible !important; }
  #root .body, #root .set-body { flex: none !important; min-height: 0 !important;
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

    with tempfile.TemporaryDirectory(prefix="acctsw-shot-") as tmp:
        store = Path(tmp) / "store"
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
        # Reserved example domains keep every address unmistakably fictional.
        s.data["tools"]["codex"] = {"active": "personal@studio.example", "accounts": {
            "work@studio.example": seat("work@studio.example", "Work", "Business", usage(100, 101, 74),
                                    iso(t + dt.timedelta(minutes=101))),
            "personal@studio.example": seat("personal@studio.example", "Personal", "Business",
                                        usage(38, 180, 21)),
        }}
        s.data["tools"]["claude"] = {"active": "studio@studio.example", "accounts": {
            "studio@studio.example": seat("studio@studio.example", "Studio", "Max", usage(20, 240, 16)),
            "personal@me.example": seat("personal@me.example", "Personal", "Pro", usage(5, 300, 4)),
        }}
        # Metadata only: no API key, Keychain write, provider request or real fingerprint.
        # OpenAI's catalog has no machine-readable prices; the real key card shows no price line.
        s.data["keys"] = {"sample-openai-key": {
            "id": "sample-openai-key", "label": "Late-night", "provider": "openai",
            "harness": "codex", "model": "gpt-5.4", "fingerprint": "demo",
            "responses_verified": True, "created_at": iso(t),
        }}
        s.data["moved_note"] = "auto-moved Codex · Work → Personal — Work's resting"
        s.settings().update({"theme": "light", "auto_switch": True,
                             "notify": True, "restart_app": False,
                             "engine_restart": True, "key_fallback": False,
                             "confirm_key_switch": True})
        s.save()
        payload = bridge.snapshot_state(ctx)
        payload["usage_history"] = sample_history(ctx, t)
    # The shell-wiring probe is host-global even with Context.for_test. Show a sample completed
    # setup, without carrying this machine's rc path or installation status into the screenshot.
    payload["supervision"] = {"wrappers": True, "block": True, "on_path": True, "active": True}
    return payload


# Synthetic weekly volumes (all tokens, cache reads included) per seat and model. Rough shapes only:
# a heavy Max seat, a light Pro seat, a busy Codex work seat that hits its limit, and a key used now
# and then. Every log line is fictional; the seeded generator keeps the shapes the same between runs.
_HISTORY = {
    ("claude", "studio@studio.example"): {"claude-opus-5-5": 520e6, "claude-sonnet-5-5": 110e6,
                                          "claude-haiku-5-5": 25e6},
    ("claude", "personal@me.example"): {"claude-sonnet-5-5": 55e6, "claude-haiku-5-5": 8e6},
    ("codex", "work@studio.example"): {"gpt-6.1-sol": 430e6, "gpt-6.1-mini": 45e6},
    ("codex", "personal@studio.example"): {"gpt-6.1-sol": 160e6, "gpt-6.1-mini": 20e6},
    ("codex", "sample-openai-key"): {"gpt-5.4": 14e6},
}
_POLLED_WEEK = {"work@studio.example": 74.0, "personal@studio.example": 21.0}
_WEEKS = 13
_TRACKED_WEEKS = 9      # older Claude sessions predate the seat ledger and show as unattributed


def sample_history(ctx, t: dt.datetime) -> dict:
    """Write fictional transcripts, index them with the real engine, return the history payload."""
    from acctsw import tokenlog

    rng = random.Random(1795)
    ledger = []
    week_end = t + dt.timedelta(days=3)        # matches the seats' weekly reset in sample_payload
    for (tool, seat), models in _HISTORY.items():
        is_key = seat == "sample-openai-key"
        for week in range(_WEEKS):
            if is_key and week % 3 == 1:
                continue
            trend = 0.75 + 0.5 * rng.random()
            end = week_end - dt.timedelta(weeks=week)
            start = end - dt.timedelta(weeks=1)
            peak = 0.0
            for day in range(7):
                at = start + dt.timedelta(days=day, hours=9 + rng.randrange(8))
                if at > t or (is_key and rng.random() < 0.6):
                    continue
                thread = f"{tool}-{seat.split('@')[0]}-{week}-{day}"
                share = trend * (0.35 + rng.random()) / 4.9
                peak = min(100.0, peak + share * 100 * rng.uniform(0.55, 0.75))
                turns = {m: v * share for m, v in models.items()}
                if tool == "claude":
                    if week < _TRACKED_WEEKS:
                        ledger.append((at, tool, seat, thread))
                    _claude_session(ctx, thread, at, turns)
                else:
                    if is_key:
                        ledger.append((at, tool, seat, thread))
                    # This week's peak can't exceed the seat's polled reading in the popover shot.
                    cap = _POLLED_WEEK.get(seat, 100.0) if week == 0 else 100.0
                    _codex_session(ctx, thread, at, turns, f"acct:{seat}",
                                   None if is_key else (end, min(peak, cap)))
    # The seat ledger is how the app attributes Claude sessions (and key-seat Codex sessions).
    rows = [{"v": 1, "at": at.isoformat(), "tool": tool, "email": seat, "pid": 1,
             "thread": thread, "event": "bind"} for at, tool, seat, thread in sorted(ledger)]
    (ctx.data_dir / "seat-ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    for _ in range(20):
        tokenlog.refresh(ctx, budget_s=30)
        if tokenlog.index_progress(ctx)["done"]:
            return tokenlog.history(ctx)
    raise RuntimeError("sample usage logs did not finish indexing")


def _split(total: float, parts: int) -> list[int]:
    return [int(total / parts)] * parts


def _claude_session(ctx, thread: str, at: dt.datetime, turns: dict) -> None:
    lines = []
    for model, total in turns.items():
        for i, chunk in enumerate(_split(total, 12)):
            stamp = (at + dt.timedelta(minutes=len(lines) * 4)).isoformat()
            lines.append({"type": "assistant", "sessionId": thread, "requestId": f"r-{thread}-{model}-{i}",
                          "timestamp": stamp, "message": {
                              "id": f"m-{thread}-{model}-{i}", "model": model, "usage": {
                                  "input_tokens": int(chunk * 0.02),
                                  "cache_creation_input_tokens": int(chunk * 0.05),
                                  "cache_read_input_tokens": int(chunk * 0.91),
                                  "output_tokens": int(chunk * 0.02)}}})
    path = ctx._claude_real / "projects" / "sample-project" / f"{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _codex_session(ctx, thread: str, at: dt.datetime, turns: dict, creator: str,
                   window: tuple | None) -> None:
    lines = [{"type": "session_meta", "timestamp": at.isoformat(),
              "payload": {"id": thread, "creator_user_id": creator}}]
    total = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
             "output_tokens": 0, "reasoning_output_tokens": 0}
    for model, amount in turns.items():
        lines.append({"type": "turn_context", "timestamp": at.isoformat(), "payload": {"model": model}})
        for i, chunk in enumerate(_split(amount, 12)):
            output = int(chunk * 0.03)
            total["input_tokens"] += chunk - output
            total["cached_input_tokens"] += int(chunk * 0.88)
            total["output_tokens"] += output
            total["reasoning_output_tokens"] += output // 2
            limits = {}
            if window:
                end, peak = window
                limits["secondary"] = {"window_minutes": 10080, "resets_at": int(end.timestamp()),
                                       "used_percent": round(peak * (i + 1) / 12, 1)}
            stamp = (at + dt.timedelta(minutes=(len(lines)) * 3)).isoformat()
            lines.append({"type": "event_msg", "timestamp": stamp, "payload": {
                "type": "token_count", "info": {"total_token_usage": dict(total)},
                "rate_limits": limits}})
    day = ctx._codex_real / "sessions" / f"{at:%Y}" / f"{at:%m}" / f"{at:%d}"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-{at:%Y-%m-%dT%H-%M-%S}-{thread}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


_VIEWS = {
    "popover": "window.AGL.update(P);",
    "settings": "window.AGL.result({state: P, settings_panel: true});",
    # The same path the app takes: the header button asks for history, the bridge replies.
    "usage": ("window.AGL.update(P);\n"
              "document.querySelector('[data-action=\"usage\"]').click();\n"
              "window.AGL.result({ok: true, usage_history: H});"),
}


def shoot(name: str, view: str, payload: dict) -> Path:
    """Render one view and screenshot it, via a temporary page inside app/web (relative asset paths
    and the page's own CSP mean it has to be served from there)."""
    page = WEB / "_shot.html"
    boot = WEB / "_shot.js"
    state = {k: v for k, v in payload.items() if k != "usage_history"}
    call = _VIEWS[view]
    # Stamp the rendered height into the DOM so pass 1 can read it back: --screenshot only ever
    # captures the viewport, so the window has to be sized to the content before the real shot.
    boot.write_text(
        f"const P = {json.dumps(state)};\nconst H = {json.dumps(payload['usage_history'])};\n"
        f"{call}\n"
        "document.title = 'H' + Math.ceil(document.documentElement.getBoundingClientRect().height);\n",
        encoding="utf-8")
    page.write_text(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="styles.css">'
        f"<style>{SHOT_CSS}</style></head><body>"
        '<div id="root"></div><script src="bundle.js"></script>'
        '<script src="_shot.js"></script></body></html>', encoding="utf-8")
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(PORT)],
                           cwd=WEB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp = Path(tempfile.mkdtemp())
    url = f"http://127.0.0.1:{PORT}/_shot.html"
    common = [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
              "--default-background-color=00000000"]
    try:
        # pass 1: measure
        # A bad browser diagnostic byte must not prevent reading the measured height.
        dom = subprocess.run(common + ["--dump-dom", "--window-size=848,900", url],
                             check=True, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=90, cwd=tmp).stdout
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
    for name, view in (("screenshot.png", "popover"), ("screenshot-settings.png", "settings"),
                       ("screenshot-usage.png", "usage")):
        out = shoot(name, view, payload)
        print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
