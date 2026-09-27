#!/usr/bin/env python3
"""Browse design variants of the popover side by side, in a real browser, on real data.

Each variant is a fork of the web UI living in `design/variants/<id>/` — its own `render.mjs`
and `styles.css`, nothing else. This builds a gallery that renders every variant across the
same set of screens and both themes, and serves it on localhost:

    python3 scripts/design-gallery.py            # build + serve + open
    python3 scripts/design-gallery.py --no-serve # build only

`v0-today` is generated from the shipping `app/web/` sources, so the current design is always in
the line-up as the baseline to beat.

Why it renders the real thing rather than mockups: every state below comes from
`scripts/screenshots.py:sample_payload()`, which runs a seeded store through the real engine
(`bridge.snapshot_state`) — the same call the menubar makes. The model picker is fed the real
OpenRouter catalogue (458 models, public, no key), because a picker that looks fine with eight
rows is not the picker we ship. A design that only works on invented data is not evidence.

Nothing here touches `app/web/`, so the render tests and the bundle-drift guard stay green while
the variants are explored. The frames import `render.mjs` as ES modules, which needs a real
origin — hence the localhost server rather than opening files directly.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WEB = ROOT / "app" / "web"
DESIGN = ROOT / "design"
VARIANTS = DESIGN / "variants"
# Parallel variant builds would otherwise clobber each other's output: the build starts by
# removing the directory. AGL_GALLERY_BUILD gives each concurrent run its own.
BUILD = Path(os.environ.get("AGL_GALLERY_BUILD") or DESIGN / ".build")
FIXTURES = DESIGN / "fixtures"
PORT = 8918

POPOVER_W, POPOVER_H = 376, 600   # the real WKWebView popover, menubar.py


def _screenshots_module():
    """Reuse the sample payload rather than copying it — one definition of "sample seats"."""
    spec = importlib.util.spec_from_file_location("agl_screenshots", ROOT / "scripts" / "screenshots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the real model catalogue -------------------------------------------------------------------

def openrouter_catalog(refresh: bool = False) -> dict:
    """The live OpenRouter catalogue, shaped exactly as bridge.py hands it to the picker.

    Cached to design/fixtures/ so the gallery works offline and every variant is judged on the
    same list. OpenRouter's catalogue is public; this sends no key and no account data.
    """
    cache = FIXTURES / "openrouter-catalog.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())

    from acctsw import handoff as handoff_mod
    from acctsw import pricing as pricing_mod
    from acctsw import providers as providers_mod

    provider = providers_mod.get_provider("openrouter")
    status, body = pricing_mod._default_get(provider.models_endpoint, {}, 30)
    if status != 200:
        raise SystemExit(f"OpenRouter catalogue fetch failed: HTTP {status}")
    models_parsed = pricing_mod.parse_catalog(provider, body)
    checked = pricing_mod.now() if hasattr(pricing_mod, "now") else dt.datetime.now(dt.timezone.utc)

    models = []
    for model in pricing_mod.sort_models(models_parsed):
        item = {"id": model.id, "display_name": model.display_name,
                "context_window": model.context_window}
        if any(model.rate(name) is not None for name in pricing_mod.RATE_NAMES):
            item["price"] = handoff_mod._price(model, checked)
        models.append(item)
    catalog = {"ok": True, "models": models,
               "sort_key": pricing_mod.model_sort_key(models_parsed),
               "source": "live", "fetched_at": checked.isoformat(), "potentially_stale": False}
    FIXTURES.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(catalog, indent=1, default=str))
    return json.loads(cache.read_text())


# --- the states every variant must render ---------------------------------------------------------

def _iso(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def build_states(base: dict, catalog: dict) -> list[dict]:
    """The comparison set. Same states, same data, every variant — so the look is what differs."""
    states: list[dict] = []

    def add(id, label, note, screen, state, flow=None):
        states.append({"id": id, "label": label, "note": note, "screen": screen,
                       "state": state, "flow": flow})

    # 1 — the everyday glance: two tools, a resting seat, a key seat.
    add("main", "the glance", "what opens 95% of the time: is anyone free, and who's on now",
        "main", base)

    # 2 — money is being spent right now. The pinned row, the paid-use strip and the stop button
    #     all appear together; today they say the same thing twice.
    spending = copy.deepcopy(base)
    spending["settings"]["key_fallback"] = True     # the steady state: paid use is genuinely on
    spending["pinned_sessions"] = [{
        "tool": "codex", "pid": 20887, "pin": "pin-1", "email": "key:sample-openai-key",
        "end_requested": False, "key_seat": spending["keys"][0]}]
    spending["running_key_seats"] = ["sample-openai-key"]
    add("spending", "money running", "a pinned paid terminal — the state where a mistake costs money",
        "main", spending)

    # 3 — the decision screen, priced. A real catalogue entry, so the amounts and their age are
    #     the provider's own rather than numbers picked to flatter the layout.
    priced = next((m for m in catalog["models"] if m.get("price", {}).get("status") == "known"
                   and "claude" in m["id"]), None) or next(
                      m for m in catalog["models"] if m.get("price", {}).get("status") == "known")
    asking = copy.deepcopy(base)
    asking["keys"][0] = {**asking["keys"][0], "provider": "openrouter", "model": priced["id"]}
    asking["pending_key_switches"] = [{
        "id": "req-1", "status": "pending", "expires_at": _iso(2), "tool": "codex",
        "pinned": False,
        "from_seat": {"id": "work@studio.example", "label": "Work"},
        "key_seat": asking["keys"][0], "price": priced["price"],
    }]
    add("asking", "asking to spend", "consent, priced: the seat, the model, the rate, and a way out",
        "main", asking)

    # 4 — the same consent for a provider that publishes no price. OpenAI, Anthropic and Langdock
    #     all land here, so it is the common case, not the edge case.
    unpriced = copy.deepcopy(base)
    unpriced["pending_key_switches"] = [{
        "id": "req-2", "status": "pending", "expires_at": _iso(2), "tool": "codex",
        "pinned": True,
        "from_seat": {"id": "work@studio.example", "label": "Work"},
        "key_seat": unpriced["keys"][0],
        "price": {"status": "unknown", "estimate": True, "currency": "USD",
                  "token_unit": "per_million_tokens", "request_unit": "per_request",
                  "rates": {n: {"status": "unknown", "value": None, "same_as": None}
                            for n in ("input", "output", "cached_input", "reasoning", "request")},
                  "age_seconds": None, "source": "live"},
    }]
    add("asking-unpriced", "asking to spend · no price",
        "the same consent when the provider publishes no rate — what carries the decision then?",
        "main", unpriced)

    # 4 — the unhappy list: nothing ready, a key that may not work, the wiring incomplete.
    trouble = copy.deepcopy(base)
    # Every tool has to rest, or the header's "0 ready" contradicts the Claude rows right under
    # it — and a design shaped around an incoherent fixture inherits the incoherence.
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
    add("trouble", "nothing ready", "every seat resting, an unproven key, the wiring incomplete",
        "main", trouble)

    # 5 — settings: seven toggles, one of which decides whether real money may be spent.
    add("settings", "settings", "seven rows mixing 'how switching works' with 'may you spend money'",
        "settings", base)

    # 6 — the density test. 458 real models; prices only where the provider publishes them.
    add("models", "pick a model", "458 real OpenRouter models — the density the picker must survive",
        "add-key", base,
        flow={"step": "models", "provider": "openrouter", "label": "Late-night",
              "modelFilter": "", "catalog": catalog})

    # 7 — the same picker for a provider that publishes no prices at all. The honest empty column.
    add("models-unpriced", "pick a model · no prices",
        "openai publishes no machine-readable prices — the picker must not imply it does",
        "add-key", base,
        flow={"step": "models", "provider": "openai", "label": "Late-night", "modelFilter": "",
              "catalog": {"ok": True, "sort_key": "id", "source": "live",
                          "fetched_at": _iso(-3), "potentially_stale": False,
                          "models": [{"id": m, "display_name": None, "context_window": None}
                                     for m in ("gpt-5.4", "gpt-5.4-mini", "gpt-6-astra",
                                               "gpt-6-luna", "o5", "o5-mini",
                                               "text-embedding-3-large")]}})
    return states


# --- the gallery ----------------------------------------------------------------------------------

FRAME_JS = """
import * as R from "./render.mjs";
const q = new URLSearchParams(location.search);
const data = await (await fetch("../states.json")).json();
const entry = data.find((s) => s.id === q.get("s")) || data[0];
const state = structuredClone(entry.state);
state.settings = { ...state.settings, theme: q.get("t") === "dark" ? "dark" : "light" };
const root = document.getElementById("root");
root.innerHTML =
    entry.screen === "settings" ? R.buildSettings(state)
  : entry.screen === "add-key" ? R.buildAddKey(state, entry.flow)
  : R.buildHTML(state);
// Gallery-only shims: enough interaction to judge the design, none of the bridge.
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-action]")) { e.preventDefault(); return; }
  const card = e.target.closest("[data-card]");
  if (card) card.classList.toggle("expanded");
});
const filter = document.getElementById("key-model-filter");
if (filter) filter.addEventListener("input", () => {
  document.getElementById("key-model-results").innerHTML =
    R.buildModelResults({ ...entry.flow, modelFilter: filter.value });
});
"""

FRAME_CSS = f"""
  html, body {{ margin:0; padding:0; background:transparent; }}
  html, body, #root {{ width:{POPOVER_W}px; height:{POPOVER_H}px; overflow:hidden; }}
"""


def frame_html(variant: str) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{variant}</title><link rel="stylesheet" href="styles.css">'
            f"<style>{FRAME_CSS}</style></head><body><div id=\"root\"></div>"
            f'<script type="module" src="frame.js"></script></body></html>')


def discover_variants() -> list[tuple[str, Path]]:
    """v0-today is generated from the shipping sources; the rest come from design/variants/."""
    found = []
    if VARIANTS.is_dir():
        for path in sorted(VARIANTS.iterdir()):
            if path.is_dir() and (path / "render.mjs").exists() and (path / "styles.css").exists():
                found.append((path.name, path))
    return found


def read_rationale(path: Path | None) -> str:
    if path is None:
        return ("The design as it ships today, generated from app/web/. Everything else in this "
                "gallery has to be better than this to be worth the churn.")
    readme = path / "README.md"
    return readme.read_text().strip() if readme.exists() else "(no README.md in this variant)"


def build(refresh_catalog: bool = False) -> list[dict]:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)

    states = build_states(_screenshots_module().sample_payload(), openrouter_catalog(refresh_catalog))
    (BUILD / "states.json").write_text(json.dumps(states, default=str))

    variants = [("v0-today", None)] + discover_variants()
    for name, src in variants:
        out = BUILD / name
        out.mkdir()
        render_src = (src / "render.mjs") if src else (WEB / "render.mjs")
        styles_src = (src / "styles.css") if src else (WEB / "styles.css")
        shutil.copy(render_src, out / "render.mjs")
        shutil.copy(styles_src, out / "styles.css")
        (out / "frame.js").write_text(FRAME_JS)
        (out / "index.html").write_text(frame_html(name))
    shutil.copytree(WEB / "fonts", BUILD / "fonts")
    for name, src in variants:                 # each frame resolves fonts/ relative to itself
        (BUILD / name / "fonts").symlink_to("../fonts")

    (BUILD / "index.html").write_text(gallery_html(
        [{"id": n, "rationale": read_rationale(p)} for n, p in variants], states))
    return states


def gallery_html(variants: list[dict], states: list[dict]) -> str:
    """One page: pick a screen, see every variant side by side, flip the theme."""
    tabs = "".join(
        f'<button class="tab" data-state="{s["id"]}">{s["label"]}</button>' for s in states)
    notes = "".join(
        f'<p class="note" data-state="{s["id"]}" hidden>{s["note"]}</p>' for s in states)
    # The rationale is a full README; showing it inline pushed every popover below the fold.
    # Lead with one sentence, keep the rest one click away.
    def caption(v):
        text = " ".join(v["rationale"].split())
        head, _, tail = text.partition(". ")
        return (f'<figcaption><b>{v["id"]}</b><span class="why">{head}.</span>'
                + (f'<details><summary>full rationale</summary><p>{tail}</p></details>' if tail else "")
                + "</figcaption>")
    cols = "".join(
        f'<figure class="col">{caption(v)}'
        f'<iframe data-variant="{v["id"]}" width="{POPOVER_W}" height="{POPOVER_H}" '
        f'loading="lazy" title="{v["id"]}"></iframe></figure>' for v in variants)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>ai guest list — design gallery</title><style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; font:14px/1.5 -apple-system, system-ui, sans-serif;
         background:#b9c4d8; color:#161b24; }}
  body.dark {{ background:#141820; color:#e9edf4; }}
  header {{ position:sticky; top:0; z-index:2; padding:12px 20px 10px;
           background:inherit; border-bottom:1px solid rgba(128,140,160,.35); }}
  h1 {{ font-size:15px; margin:0 0 8px; font-weight:700; }}
  .bar {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
  button {{ font:inherit; padding:5px 11px; border-radius:8px; cursor:pointer;
           border:1px solid rgba(128,140,160,.5); background:rgba(255,255,255,.55); color:inherit; }}
  body.dark button {{ background:rgba(255,255,255,.08); }}
  button.on {{ background:#2f6f5e; border-color:#2f6f5e; color:#fff; }}
  .spacer {{ flex:1; }}
  .note {{ margin:9px 0 0; font-size:12.5px; opacity:.8; max-width:80ch; }}
  .rail {{ display:flex; gap:22px; padding:22px 20px 40px; overflow-x:auto; align-items:flex-start; }}
  .col {{ margin:0; flex:none; width:{POPOVER_W}px; }}
  figcaption {{ display:flex; flex-direction:column; gap:3px; margin-bottom:9px; height:104px; }}
  figcaption b {{ font-size:13px; }}
  .why {{ font-size:11.5px; opacity:.75; line-height:1.45; overflow:hidden; }}
  details {{ margin-top:auto; font-size:11px; }}
  summary {{ cursor:pointer; opacity:.7; }}
  details[open] {{ position:absolute; z-index:3; width:{POPOVER_W}px; max-height:60vh;
                   overflow:auto; background:#fff; color:#161b24; padding:10px 12px;
                   border-radius:10px; box-shadow:0 10px 30px rgba(20,30,50,.3); }}
  iframe {{ border:0; border-radius:16px; display:block;
           box-shadow:0 18px 40px rgba(20,30,50,.28), 0 2px 8px rgba(20,30,50,.16); }}
</style></head><body>
<header>
  <h1>ai guest list — design gallery <span style="font-weight:400;opacity:.7">·
    real engine payload · {POPOVER_W}×{POPOVER_H}, the real popover size</span></h1>
  <div class="bar">{tabs}<span class="spacer"></span>
    <button id="theme">dark</button></div>
  {notes}
</header>
<div class="rail">{cols}</div>
<script>
  let stateId = {json.dumps(states[0]["id"])}, theme = "light";
  const paint = () => {{
    document.body.classList.toggle("dark", theme === "dark");
    document.getElementById("theme").textContent = theme === "light" ? "dark" : "light";
    for (const t of document.querySelectorAll(".tab")) t.classList.toggle("on", t.dataset.state === stateId);
    for (const n of document.querySelectorAll(".note")) n.hidden = n.dataset.state !== stateId;
    for (const f of document.querySelectorAll("iframe"))
      f.src = `${{f.dataset.variant}}/index.html?s=${{stateId}}&t=${{theme}}`;
  }};
  document.addEventListener("click", (e) => {{
    const tab = e.target.closest(".tab");
    if (tab) {{ stateId = tab.dataset.state; paint(); }}
    else if (e.target.id === "theme") {{ theme = theme === "light" ? "dark" : "light"; paint(); }}
  }});
  paint();
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-serve", action="store_true")
    ap.add_argument("--refresh-catalog", action="store_true",
                    help="re-fetch the public OpenRouter catalogue instead of using the fixture")
    args = ap.parse_args()

    states = build(args.refresh_catalog)
    variants = ["v0-today"] + [n for n, _ in discover_variants()]
    # AGL_GALLERY_BUILD may point outside the repo, so relative_to() is not safe here.
    where = BUILD.relative_to(ROOT) if BUILD.is_relative_to(ROOT) else BUILD
    print(f"built {where} — {len(variants)} variant(s), {len(states)} states")
    for name in variants:
        print(f"  · {name}")
    if args.no_serve:
        return 0

    url = f"http://127.0.0.1:{args.port}/index.html"
    print(f"\nserving {url}   (ctrl-c to stop)")
    webbrowser.open(url)
    try:
        subprocess.run([sys.executable, "-m", "http.server", str(args.port)], cwd=BUILD)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
