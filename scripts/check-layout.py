#!/usr/bin/env python3
"""Check the shipping UI in headless Chrome at the real 376x600 viewport.

Run: python3 scripts/check-layout.py
Before/after: python3 scripts/check-layout.py --compare-ref HEAD
  (Use the commit before these UI changes; reads its web files into a temp dir.)
Requires: pip install playwright; Google Chrome (or AGL_CHROME=/path/to/chrome).
No downloads, provider calls, real accounts, or gallery/build writes. Uses
synthesized states from the real engine snapshot plus a self-contained model
catalogue and realistic long identities.
The real bundle handles navigation, disclosures and model-picker replies.

Only decorative SVGs and reachable content INSIDE a working vertical scroller
may exceed the viewport. Scroller frames themselves must fit, and scrolling to
the end must expose their last content; overflow:hidden cannot hide a failure.
Sheet seat rows have a single-line 44px height budget, the content column after
the 58px tool column aligns across tool blocks and sheet rows, and .row-action
buttons meet their 28x26px target and 2px focus-ring contract. An unavailable
browser is an ERROR (exit 2), never a pass.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"


def _iso(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _price_envelope(input_rate: str | None, output_rate: str | None) -> dict:
    known = input_rate is not None or output_rate is not None
    return {
        "status": "known" if known else "unknown",
        "estimate": True,
        "currency": "USD",
        "token_unit": "per_million_tokens",
        "request_unit": "per_request",
        "rates": {
            "input": {"status": "known" if input_rate is not None else "unknown",
                      "value": input_rate, "same_as": None},
            "output": {"status": "known" if output_rate is not None else "unknown",
                       "value": output_rate, "same_as": None},
            "cached_input": {"status": "unknown", "value": None, "same_as": None},
            "reasoning": {"status": "unknown", "value": None, "same_as": None},
            "request": {"status": "unknown", "value": None, "same_as": None},
        },
        "age_seconds": 180,
        "verified_at": _iso(-3),
        "potentially_stale": False,
        "source": "live",
    }


def sample_catalog() -> dict:
    """Self-contained OpenRouter-shaped catalogue with enough rows to scroll .model-list."""
    entries = [
        ("anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5", 200000, "3", "15"),
        ("anthropic/claude-opus-4.1", "Claude Opus 4.1", 200000, "15", "75"),
        ("anthropic/claude-3.5-haiku", "Claude 3.5 Haiku", 200000, "0.8", "4"),
        ("openai/gpt-5.4", "GPT-5.4", 400000, "2.5", "10"),
        ("openai/gpt-5.4-mini", "GPT-5.4 Mini", 400000, "0.25", "2"),
        ("openai/o4-mini-high", "o4 Mini High", 200000, "1.1", "4.4"),
        ("google/gemini-2.5-pro", "Gemini 2.5 Pro", 1048576, "1.25", "10"),
        ("google/gemini-2.5-flash", "Gemini 2.5 Flash", 1048576, "0.15", "0.6"),
        ("deepseek/deepseek-r1-0528", "DeepSeek R1", 163840, "0.55", "2.19"),
        ("deepseek/deepseek-chat-v3.1:free", "DeepSeek V3.1 (free)", 128000, "0", "0"),
        ("meta-llama/llama-4-maverick-17b-128e-instruct", "Llama 4 Maverick", 1000000, "0.18", "0.6"),
        ("qwen/qwen3-coder-480b-a35b-instruct-fp8", "Qwen3 Coder 480B", 262144, "0.4", "1.6"),
        ("mistralai/mistral-medium-3.1-long-context-preview", "Mistral Medium 3.1", 131072, "0.4", "2"),
        ("x-ai/grok-4-fast-reasoning-preview", "Grok 4 Fast", 256000, None, None),
    ]
    models = []
    for model_id, display_name, ctx_win, in_rate, out_rate in entries:
        item = {"id": model_id, "display_name": display_name, "context_window": ctx_win}
        if in_rate is not None or out_rate is not None:
            item["price"] = _price_envelope(in_rate, out_rate)
        models.append(item)
    return {
        "ok": True,
        "models": models,
        "sort_key": "input_usd_per_million_tokens",
        "source": "live",
        "fetched_at": _iso(-3),
        "potentially_stale": False,
    }


def build_states(base: dict, catalog: dict) -> list[dict]:
    """Representative UI states across main, spending, consent, trouble, settings and model picker."""
    states: list[dict] = []

    def add(id, label, note, screen, state, flow=None):
        states.append({"id": id, "label": label, "note": note, "screen": screen,
                       "state": state, "flow": flow})

    add("main", "the glance", "two tools, a resting seat, a key seat", "main", base)

    spending = copy.deepcopy(base)
    spending["settings"]["key_fallback"] = True
    spending["pinned_sessions"] = [{
        "tool": "codex", "pid": 20887, "pin": "pin-1", "email": "key:sample-openai-key",
        "end_requested": False, "key_seat": spending["keys"][0],
    }]
    spending["running_key_seats"] = ["sample-openai-key"]
    add("spending", "money running", "a pinned paid terminal", "main", spending)

    priced = next(m for m in catalog["models"] if m.get("price", {}).get("status") == "known"
                  and "claude" in m["id"])
    asking = copy.deepcopy(base)
    asking["keys"][0] = {**asking["keys"][0], "provider": "openrouter", "model": priced["id"]}
    asking["pending_key_switches"] = [{
        "id": "req-1", "status": "pending", "expires_at": _iso(2), "tool": "codex",
        "pinned": False,
        "from_seat": {"id": "work@studio.example", "label": "Work"},
        "key_seat": asking["keys"][0], "price": priced["price"],
    }]
    add("asking", "asking to spend", "consent, priced", "main", asking)

    unpriced = copy.deepcopy(base)
    unpriced["pending_key_switches"] = [{
        "id": "req-2", "status": "pending", "expires_at": _iso(2), "tool": "codex",
        "pinned": True,
        "from_seat": {"id": "work@studio.example", "label": "Work"},
        "key_seat": unpriced["keys"][0],
        "price": _price_envelope(None, None),
    }]
    add("asking-unpriced", "asking to spend · no price", "consent without published rate",
        "main", unpriced)

    trouble = copy.deepcopy(base)
    for tool in ("codex", "claude"):
        group = trouble["tools"][tool]
        group["active"] = None
        for seat in group["seats"]:
            seat["status"], seat["active"], seat["limited"] = "resting", False, True
            seat["usage5h"] = 100
            seat["limited_until"] = _iso(96)
    trouble["counts"] = {"resting": 4, "ready": 0}
    trouble["door"], trouble["dot"] = "shut", "amber"
    trouble["keys"][0]["responses_verified"] = False
    trouble["supervision"] = {"wrappers": True, "block": False, "on_path": False, "active": False}
    add("trouble", "nothing ready", "every seat resting, unproven key, supervision off",
        "main", trouble)

    add("settings", "settings", "settings sub-view", "settings", base)

    add("models", "pick a model", "priced OpenRouter model picker",
        "add-key", base,
        flow={"step": "models", "provider": "openrouter", "label": "Late-night",
              "modelFilter": "", "catalog": catalog})

    add("models-unpriced", "pick a model · no prices", "unpriced OpenAI model picker",
        "add-key", base,
        flow={"step": "models", "provider": "openai", "label": "Late-night", "modelFilter": "",
              "catalog": {"ok": True, "sort_key": "id", "source": "live",
                          "fetched_at": _iso(-3), "potentially_stale": False,
                          "models": [{"id": m, "display_name": None, "context_window": None}
                                     for m in ("gpt-5.4", "gpt-5.4-mini", "gpt-6-astra",
                                               "gpt-6-luna", "o5", "o5-mini",
                                               "text-embedding-3-large")]}})
    return states


def gallery_states():
    spec = importlib.util.spec_from_file_location("agl_screenshots", ROOT / "scripts/screenshots.py")
    screenshots = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(screenshots)
    states = build_states(screenshots.sample_payload(), sample_catalog())
    long = copy.deepcopy(states[0])
    long["id"] = "long-identities"
    for tool in long["state"]["tools"].values():
        for idx, seat in enumerate(tool["seats"]):
            seat["name"] = "Wilhelmina Featherstone +codex / a realistically long account name"
            seat["kind"] = "business" if idx == 0 else "personal"
            seat["plan"] = "Business" if idx == 0 else "Self_Serve_Business_Prolite"
    long["state"]["keys"][0].update(label="test / a long API key seat name", model="gpt-6-luna")
    states.append(long)
    # Enough seats to force .body to scroll in both spending and trouble states. Include a
    # stale reading so the stale reading copy is exercised alongside ordinary rows.
    for source in ("spending", "trouble"):
        overflow = copy.deepcopy(next(entry for entry in states if entry["id"] == source))
        overflow["id"] = source + "-overflow"
        for tool in overflow["state"]["tools"].values():
            originals = tool["seats"]
            tool["seats"] = []
            for index in range(4):
                for original in originals:
                    seat = copy.deepcopy(original)
                    seat["email"] = f'{index}-{seat["email"]}'
                    seat["usage_stale"] = index == 0
                    tool["seats"].append(seat)
        states.append(overflow)
    return states


MEASURE = r"""() => {
  const errors = [], tolerance = 1;
  const check = (ok, message) => { if (!ok) errors.push(message); };
  const name = el => el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).trim().replaceAll(' ', '.') : '');
  const visible = el => {
    if (!el.checkVisibility() || el.closest('svg, [hidden], .sr-only')) return false;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (p.matches('details:not([open])') && !p.querySelector(':scope > summary')?.contains(el)) return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const scrolls = el => /^(auto|scroll)$/.test(getComputedStyle(el).overflowY);
  check(innerWidth === 376 && innerHeight === 600, `viewport is ${innerWidth}x${innerHeight}`);
  for (const el of [document.documentElement, document.body, document.getElementById('root')]) {
    check(el.scrollHeight <= 600 && el.scrollWidth <= 376, `${name(el)} page overflows: ${el.scrollWidth}x${el.scrollHeight}`);
  }
  const elements = [...document.querySelectorAll('#root *')].filter(visible);
  const intended = '.body, .set-body:not(.picker-body), .model-list, .key-prompts:not(:empty)';
  for (const el of document.querySelectorAll(intended)) {
    if (visible(el)) check(el.scrollHeight <= el.clientHeight + tolerance || scrolls(el),
      `${name(el)} is an intended scroller with unreachable overflow`);
  }
  for (const el of elements) {
    const r = el.getBoundingClientRect();
    if (!el.closest('[aria-hidden="true"]')) {
      check(r.left >= -tolerance && r.right <= 376 + tolerance, `${name(el)} horizontal bounds ${r.left.toFixed(1)}..${r.right.toFixed(1)}`);
    }
    const style = getComputedStyle(el);
    // Text may exceed its box only when deliberately ellipsized. Wrapped text must fit.
    // scrollWidth on a clipping box reports its UNCLIPPED content, so a decorative bleed
    // reads as overflow even though nothing is reachable or scrollable. The page not scrolling
    // is the real contract; an aria-hidden ornament that the box clips is not a defect.
    const clips = style.overflowX === 'hidden' || style.overflowX === 'clip';
    const ornament = el.closest('[aria-hidden="true"]') !== null;
    const delegatesEllipsis = el.matches('.now-seat, .seatname')
      && [...el.querySelectorAll('.seat-identity, .seat-metadata')].every(
        child => getComputedStyle(child).textOverflow === 'ellipsis' && getComputedStyle(child).overflowX === 'hidden'
      );
    if (el.clientWidth && style.display !== 'inline' && style.textOverflow !== 'ellipsis'
        && !ornament && !clips && !delegatesEllipsis) {
      check(el.scrollWidth <= el.clientWidth + tolerance, `${name(el)} horizontal content ${el.scrollWidth} > ${el.clientWidth}`);
    }
    if (el.matches('.now, .row')) {
      const cells = [...el.children].filter(visible);
      for (let i = 1; i < cells.length; i++) {
        check(cells[i - 1].getBoundingClientRect().right <= cells[i].getBoundingClientRect().left + tolerance,
          `${name(el)} children overlap: ${name(cells[i - 1])} and ${name(cells[i])}`);
      }
    }
    let scroller = el.parentElement;
    while (scroller && !(scrolls(scroller) && scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
    if (!scroller) {
      check(r.top >= -tolerance && r.bottom <= 600 + tolerance,
        `${name(el)} vertical bounds ${r.top.toFixed(1)}..${r.bottom.toFixed(1)} (no scrolling ancestor)`);
    }
    if (!scrolls(el)) continue;
    check(r.height > 0 && r.top >= -tolerance && r.bottom <= 600 + tolerance,
      `${name(el)} scroll frame ${r.top.toFixed(1)}..${r.bottom.toFixed(1)}; clientHeight=${el.clientHeight}, scrollHeight=${el.scrollHeight}`);
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (/^(hidden|clip)$/.test(getComputedStyle(p).overflowY)) {
        const b = p.getBoundingClientRect();
        check(r.top >= b.top - tolerance && r.bottom <= b.bottom + tolerance, `${name(el)} scroll frame clipped by ${name(p)}`);
      }
    }
    const before = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    check(el.scrollHeight <= el.clientHeight + tolerance || el.scrollTop > 0,
      `${name(el)} content overflows but cannot scroll`);
    const end = el.getBoundingClientRect().bottom;
    for (const child of el.querySelectorAll('*')) {
      if (!visible(child)) continue;
      // An independently scrolling child owns its own reachability check.
      let p = child.parentElement;
      while (p !== el && !scrolls(p)) p = p.parentElement;
      if (p === el) check(child.getBoundingClientRect().bottom <= end + tolerance,
        `${name(el)} cannot reach ${name(child)} at scroll end`);
    }
    el.scrollTop = before;
  }
  const rows = [...document.querySelectorAll('.sheet .seat > .row')].filter(visible);
  for (const row of rows) {
    const height = row.getBoundingClientRect().height;
    check(height >= 36 && height <= 44, `sheet row ${row.textContent.trim().slice(0, 70)}: ${height.toFixed(1)}px outside 36..44px`);
  }
  const contentStarts = [
    ...[...document.querySelectorAll('.field .tool .now .seatname')].filter(visible),
    ...rows.map(row => row.querySelector(':scope > .seatname')).filter(Boolean),
  ].map(el => el.getBoundingClientRect().left);
  if (contentStarts.length > 1) {
    check(contentStarts.every(x => Math.abs(x - contentStarts[0]) <= tolerance),
      `tool/sheet content columns do not align: ${contentStarts.map(x => x.toFixed(1)).join(', ')}`);
  }
  const focused = document.activeElement;
  for (const action of document.querySelectorAll('.row-action')) {
    if (!visible(action) || action.disabled) continue;
    const r = action.getBoundingClientRect();
    check(r.width >= 28 && r.height >= 26 - tolerance, `row action target is ${r.width}x${r.height}`);
    check(action.tabIndex >= 0, 'row action is not keyboard focusable');
    action.focus({preventScroll: true});
    const ring = getComputedStyle(action);
    check(ring.outlineStyle !== 'none' && parseFloat(ring.outlineWidth) >= 2,
      'row action has no visible keyboard focus ring');
  }
  focused?.focus({preventScroll: true});
  for (const el of document.querySelectorAll('.sheet .seat > .row *')) {
    if (el.closest('.sr-only')) continue;
    if ([...el.childNodes].some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim())) {
      check(parseFloat(getComputedStyle(el).fontSize) >= 12, `${name(el)} sheet row text is below 12px`);
    }
  }
  return [...new Set(errors)];
}"""


BODY_SCROLL = r"""overflowExpected => {
  const body = document.querySelector('.body');
  if (!body) return ['main .body scroller is missing'];
  const top = document.querySelector('.top');
  const foot = document.querySelector('.foot');
  if (!top || !foot) return ['popover header or footer is missing'];
  const before = [top, foot].map(el => el.getBoundingClientRect().y);
  const errors = [];
  if (overflowExpected && body.scrollHeight <= body.clientHeight) errors.push('fixture does not overflow .body');
  body.scrollTop = body.scrollHeight;
  if (overflowExpected && body.scrollTop === 0) errors.push('.body cannot scroll');
  const lastSeat = body.querySelector('.sheet .seat:last-child');
  if (!lastSeat) {
    errors.push('sheet has no seats');
  } else {
    const last = lastSeat.getBoundingClientRect();
    const bounds = body.getBoundingClientRect();
    if (last.top < bounds.top - 1 || last.bottom > bounds.bottom + 1) errors.push('last seat is unreachable');
  }
  [top, foot].forEach((el, i) => {
    if (Math.abs(el.getBoundingClientRect().y - before[i]) > 1) errors.push('.body scrolling moved the header or footer');
  });
  const f = foot.getBoundingClientRect();
  if (f.top < 0 || f.bottom > 600 + 1) errors.push('footer is outside the viewport');
  body.scrollTop = 0;
  return errors;
}"""


def load_state(page, url, entry, theme):
    page.goto(url)
    snapshot = copy.deepcopy(entry["state"])
    snapshot["settings"]["theme"] = theme
    page.evaluate("""s => {
      for (const request of s.pending_key_switches || []) request.expires_at = new Date(Date.now() + 120000).toISOString();
      window.AGL.update(s);
    }""", snapshot)
    if entry["screen"] == "settings":
        page.locator('[data-action="menu"][data-menu="more"]').click()
        page.locator('[data-action="settings"]').click()
    elif entry["screen"] == "add-key":
        flow = entry["flow"]
        page.locator('[data-action="menu"][data-menu="add"]').click()
        page.locator('[data-action="key-start"]').click()
        page.locator(f'[data-action="key-provider"][data-provider="{flow["provider"]}"]').click()
        page.locator('#key-label').fill(flow["label"])
        page.locator('#key-secret').fill('layout-check-fake-secret')
        page.locator('[data-action="key-discover"]').click()
        page.evaluate("""catalog => {
          const request = window.layoutMessages.findLast(m => m.action === 'models_list');
          if (!request) throw Error('model discovery was not dispatched');
          window.AGL.result({...catalog, key_action: 'models_list', key_request_id: request.key_request_id});
        }""", flow["catalog"])
    page.evaluate("document.fonts.ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare-ref", help="also run against the shipping web files at this git ref")
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: install the browser driver with: pip install playwright", file=sys.stderr)
        return 2
    chrome = os.environ.get("AGL_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
    chrome = chrome or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome).exists():
        print(f"ERROR: Chrome/Chromium binary not found at {chrome!r} (set AGL_CHROME)", file=sys.stderr)
        return 2
    passed = failed = 0

    def report(label, errors):
        nonlocal passed, failed
        if errors:
            failed += 1
            print(f"FAIL {label}")
            for error in errors:
                print(f"  {error}")
        else:
            passed += 1
            print(f"PASS {label}")

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    def run_states(browser, states, web):
        handler = functools.partial(QuietHandler, directory=str(web))
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                context = browser.new_context(viewport={"width": 376, "height": 600}, reduced_motion="reduce")
                context.add_init_script("""window.layoutMessages = [];
                  window.webkit = {messageHandlers: {agl: {postMessage: m => window.layoutMessages.push(m)}}};""")
                page = context.new_page()
                page.set_default_timeout(5000)
                url = f"http://127.0.0.1:{server.server_port}/index.html"
                for theme in ("light", "dark"):
                    for entry in states:
                        label = f'{theme}/{entry["id"]}'
                        try:
                            load_state(page, url, entry, theme)
                            # Establish keyboard modality before measuring row-action focus rings.
                            page.keyboard.press("Tab")
                            errors = page.evaluate(MEASURE)
                            if entry["id"].startswith(("spending", "trouble")):
                                errors += page.evaluate(BODY_SCROLL, entry["id"].endswith("-overflow"))
                            report(label, errors)
                            if entry["screen"] != "main":
                                continue
                            page.locator('.seat.key-seat [data-action="seat-details"]').first.click()
                            page.keyboard.press("Tab")
                            errors = page.evaluate(MEASURE)
                            body_bounds = page.locator('.body').bounding_box()
                            # Browser keyboard focus must bring both key-seat management actions into view.
                            for action in ('key-validate', 'key-remove'):
                                button = page.locator(f'.seat.key-seat [data-action="{action}"]').first
                                button.focus()
                                bounds = button.bounding_box()
                                if (not bounds or not body_bounds
                                        or bounds['y'] < body_bounds['y'] - 1
                                        or bounds['y'] + bounds['height'] > min(601, body_bounds['y'] + body_bounds['height'] + 1)):
                                    errors.append(f'{action} is clipped after focus: {bounds}')
                            report(label + '/key-seat-expanded', errors)
                        except Exception as exc:
                            report(label + '/interaction', [str(exc)])
                context.close()
            finally:
                server.shutdown()
                thread.join()

    baseline_failed = None
    try:
        # Start the browser first so an unavailable runtime cannot be mistaken for a layout failure.
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            states = gallery_states()
            if args.compare_ref:
                with tempfile.TemporaryDirectory(prefix="agl-layout-before-") as directory:
                    for file in ("index.html", "styles.css", "bundle.js"):
                        source = subprocess.check_output(["git", "show", f"{args.compare_ref}:app/web/{file}"], cwd=ROOT)
                        (Path(directory) / file).write_bytes(source)
                    run_states(browser, states, Path(directory))
                print(f"Before ({args.compare_ref}): {passed} passed, {failed} failed")
                baseline_failed = failed
                passed = failed = 0
            run_states(browser, states, WEB)
            browser.close()
    except Exception as exc:
        print(f"ERROR: layout check could not run: {exc}", file=sys.stderr)
        print(f"Layout: {passed} passed, {failed} failed; browser/runtime error")
        return 2
    print(f"Layout: {passed} passed, {failed} failed (376x600, both themes)")
    if baseline_failed == 0:
        print("FAIL: the comparison ref did not reproduce a layout failure")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
