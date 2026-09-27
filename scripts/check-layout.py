#!/usr/bin/env python3
"""Check the shipping UI in headless Chrome at the real 376x600 viewport.

Run: python3 scripts/check-layout.py
Before/after: python3 scripts/check-layout.py --compare-ref HEAD
  (Use the commit before these UI changes; reads its web files into a temp dir.)
Requires: pip install playwright; Google Chrome (or AGL_CHROME=/path/to/chrome).
No downloads, provider calls, real accounts, or gallery/build writes. Uses the
gallery's eight states and cached catalogue, plus realistic long identities.
The real bundle handles navigation, disclosures and model-picker replies.

Only decorative SVGs and reachable content INSIDE a working vertical scroller
may exceed the viewport. Scroller frames themselves must fit, and scrolling to
the end must expose their last content; overflow:hidden cannot hide a failure.
Roster seat rows have a 72px budget (90px for last-known readings), independently
of the stylesheet. An unavailable browser is an ERROR (exit 2), never a pass.
"""
from __future__ import annotations

import copy
import argparse
import functools
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"


def gallery_states():
    spec = importlib.util.spec_from_file_location("layout_gallery", ROOT / "scripts/design-gallery.py")
    gallery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gallery)
    # Read the cache explicitly: the gallery's fallback would fetch and write it.
    catalog = json.loads((ROOT / "design/fixtures/openrouter-catalog.json").read_text())
    states = gallery.build_states(gallery._screenshots_module().sample_payload(), catalog)
    long = copy.deepcopy(states[0])
    long["id"] = "long-identities"
    for tool in long["state"]["tools"].values():
        for seat in tool["seats"]:
            seat["name"] = "Wilhelmina Featherstone +codex / a realistically long account name"
            seat["plan"] = "Self_Serve_Business_Prolite"
    long["state"]["keys"][0].update(label="test / a long API key seat name", model="gpt-6-luna")
    states.append(long)
    return states


MEASURE = r"""() => {
  const errors = [], tolerance = 1;
  const check = (ok, message) => { if (!ok) errors.push(message); };
  const name = el => el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).trim().replaceAll(' ', '.') : '');
  const visible = el => {
    if (!el.checkVisibility() || el.closest('svg, [hidden]')) return false;
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
  const intended = '.roster-glance, .main-body, .ambient-verdict, .set-body:not(.picker-body), .model-list, .key-prompts:not(:empty), .paid-session-list';
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
    if (el.clientWidth && style.display !== 'inline' && style.textOverflow !== 'ellipsis'
        && !ornament && !clips) {
      check(el.scrollWidth <= el.clientWidth + tolerance, `${name(el)} horizontal content ${el.scrollWidth} > ${el.clientWidth}`);
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
  const rows = [...document.querySelectorAll('.roster tbody tr')].filter(row => row.querySelector('th[scope="row"]'));
  let columns;
  for (const row of rows) {
    const height = row.getBoundingClientRect().height;
    // A key row carries an action that subscription rows do not — "use in new terminal" sits
    // under its terms, because the two window columns leave this cell only 48% of the width.
    // That is a deliberate 12px, not drift; everything else still holds at 72.
    const budget = row.querySelector('.roster-freshness') ? 90
      : row.classList.contains('roster-key') ? 84 : 72;
    check(height <= budget, `roster row ${row.textContent.trim().slice(0, 70)}: ${height.toFixed(1)}px > ${budget}px`);
    const cells = [...row.querySelectorAll('.roster-value')];
    if (!cells.length) continue;
    const positions = cells.map(cell => cell.getBoundingClientRect().right);
    if (!columns) columns = positions;
    check(positions.every((v, i) => Math.abs(v - columns[i]) <= tolerance), 'roster window columns do not align across tools');
  }
  return [...new Set(errors)];
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
        page.locator('.header-menu').last.locator('summary').click()
        page.locator('[data-action="settings"]').click()
    elif entry["screen"] == "add-key":
        flow = entry["flow"]
        page.locator('.header-menu').first.locator('summary').click()
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
                            report(label, page.evaluate(MEASURE))
                            if entry["screen"] != "main":
                                continue
                            page.locator('.guest-drawer > summary').click()
                            page.locator('.seat--key .seat-disclosure > summary').first.click()
                            errors = page.evaluate(MEASURE)
                            # Browser keyboard focus must bring both formerly clipped actions into view.
                            for action in ('key-validate', 'key-remove'):
                                button = page.locator(f'.guest-drawer [data-action="{action}"]').first
                                button.focus()
                                bounds = button.bounding_box()
                                if not bounds or bounds['y'] < 228 or bounds['y'] + bounds['height'] > 601:
                                    errors.append(f'{action} is clipped after focus: {bounds}')
                            report(label + '/drawer-key-expanded', errors)
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
