# Design brief, round two — commit to a look

Read `DESIGN-BRIEF.md` first. It still holds: the audience, the popover's job, the severity-rated
findings, the information architecture (IA-1…IA-11), the voice chart, the ethics rules and the
technical constraints are all unchanged.

This document exists because **round one failed**, and it failed in a specific way that was my
fault. Round two (`v6`–`v10`) is a correction, not a continuation.

---

## 1. What round one produced, measured

Five variants were built from `DESIGN-BRIEF.md`. The user's verdict on seeing them: *"designs are
all very similar and wireframy."* That is not a matter of taste. Measured across the five
stylesheets:

| | v1 | v2 | v3 | v4 | v5 |
|---|---|---|---|---|---|
| typeface | Hanken | Hanken + Outfit | Hanken | Hanken | Hanken + Outfit |
| gradients | 0 | 0 | 0 | 0 | 0 |
| inline SVG / imagery | 0 | 0 | 0 | 0 | 0 |
| border radii | 12/16px | 12px | 16/28px | 12px | 12px |

Every variant used the same body typeface. None used a gradient, an image, a texture or any
drawn element. The radii are interchangeable. The typographic freedom the brief explicitly offered
went entirely unused, and several variants listed the app's own door mark under *"deliberately
absent"*. The result is one wireframe in five palettes.

## 2. Why — and the rule that caused it

§7 of the first brief forbids the generic defaults: the SaaS-card kit, the same soft grey shadow
under everything, gradient washes as decoration. That rule was read as **"no visual richness"**,
and with the browser blocked in the sandbox there was no feedback to correct the over-reading.

**The correction: §7 forbids reaching for a default. It does not ask for absence.** Restraint means
spending boldness in one place and keeping the rest quiet — it does not mean spending it nowhere. A
page with no depth, no texture, no drawn element and one system-ish sans is not restrained, it is
unfinished.

Two further unlocks that round one left on the table:

- **Inline `<svg>` is not restricted by the CSP at all.** `img-src 'self' data:` governs `<img>`
  and CSS `url()`; markup-level SVG is just markup. Drawn elements, textures and illustration are
  all available, at zero network cost.
- **macOS ships usable faces that need no vendoring:** `ui-serif` (New York), `ui-monospace`
  (SF Mono), `ui-rounded` (SF Rounded), `-apple-system` (SF Pro). Zero bytes, no licence, no CSP
  issue, and genuinely different from each other.

## 3. Reuse the icon — it is the identity

`app/icon.svg` is the app's mark and **the brand round one discarded**: a warm gold squircle, a
cream door swung open on the left, a faceted teal disco ball on a string, and four coloured
twinkles. Its palette is the product's:

| role | hex |
|---|---|
| tile gold | `#ffe6ad` → `#f7c668` → `#e7a23f` |
| warm glow | `#fff6df` |
| door leaf | `#f5eedd` → `#d8c8a8`, knob `#cf9b2e` |
| ball | `#ffffff` → `#bfe9df` → `#2f8a78`, facets `#2c7e6d` |
| twinkles | `#46c2a8` `#e0795a` `#f3c969` `#7c6cf0` |

**Every round-two variant must use this artwork, not merely allude to it.** Inline it, scale it,
crop it, recolour it, use one element of it as a structural device — but the mark is a starting
asset, not a logo to be shrunk into a corner and apologised for. `--codex: #46C2A8` and
`--claude: #E0795A` are already two of its twinkles; that is not a coincidence to be designed away.

## 4. The squint test

Ten variants will sit side by side at thumbnail size. **Each must be identifiable at a glance,
with the text unreadable, purely from colour, density, shape and rhythm.** If two thumbnails could
be swapped without anyone noticing, both have failed. Check your own variant against the round-one
screenshots in `design/.build` before declaring it done.

## 5. Your assigned direction

Each run builds exactly one. These are deliberately different in *kind*, not in palette. Take the
premise seriously and push it further than feels safe — a timid version of these is round one
again.

**`v6-after-hours`** — commit fully to the icon's world. The warm gold, the cream door, the teal
ball, the twinkles. Depth, glow and a real sense of a lit room at night. The door is structure, not
ornament: it can frame the verdict, divide the panel, or open as state changes. The risk to avoid
is kitsch — this is a warm, confident, well-lit interior, not a party flyer.

**`v7-programme`** — type is the interface. No cards. A printed programme or a title page: a strong
display face, real typographic hierarchy, generous rag, rules only where they carry meaning. The
guest list is a list of names set properly. `ui-serif` (New York) is available and unused; so is a
vendored display face. The risk to avoid is the broadsheet default in §7 — this is a programme, not
a newspaper.

**`v8-instrument`** — a dense technical instrument for someone who likes reading a system monitor.
Dark-first, `ui-monospace`, tabular alignment, high information density, everything visible at
once, no disclosure. Numbers are the design. The risk to avoid is the terminal-skin cliché: no fake
CRT, no scanlines, no green-on-black.

**`v9-cloakroom`** — physical and tactile. Ticket stubs, cloakroom tags, paper, layering. Real
depth: overlap, rotation, torn or perforated edges, stock texture, a cast shadow that means
something. Seats are objects you could pick up. The risk to avoid is fake realism — suggest the
material, don't photograph it.

**`v10-ambient`** — almost no chrome. The whole surface *is* the state: colour field, one number,
one sentence. Everything else appears only on demand. Closer to a weather app or a thermostat than
a dashboard. The risk to avoid is emptiness that hides the answer — IA-1's verdict must be the most
legible thing on screen, and every action must remain reachable.

## 6. What still binds you

Unchanged from `DESIGN-BRIEF.md`, and not negotiable:

- The IA (IA-1…IA-11). If your concept genuinely requires deviating from one, do it and say so in
  your README with the reasoning — do not deviate silently.
- The behaviour contract: every `data-action` and the other `data-*` attributes survive; the pure
  functions are untouched. `scripts/check-variant.mjs` enforces it.
- 376×600, the page itself never scrolls, light **and** dark both first-class, WCAG 2.2 AA
  (4.5:1 body text, 3:1 UI components, nothing carried by colour alone, visible focus,
  `prefers-reduced-motion` honoured).
- The CSP: no remote anything. Vendored fonts go in `app/web/fonts/` with an OFL entry; system
  faces cost nothing; inline SVG is free.
- The ethics rules in §5 — symmetric consent exits above all. A bolder look must not make the
  spend button prettier than the decline button.
- No invented prices, no fabricated "popular" models, no curated defaults.

One thing that is now *removed*: the checker previously demanded every action per state, which
forced a main-screen auto-switch shortcut. That bug is fixed — actions are checked as a union
across states. **Put auto-switch in settings where IA-5 says it belongs.**

## 7. Deliverables

`design/variants/v<N>-<name>/` — `render.mjs`, `styles.css`, `README.md` (palette as named hex,
typefaces and roles, the layout concept, the one memorable element, what you deliberately did not
do, and one sentence on how the icon is used).

Verify with:

```sh
export AGL_GALLERY_BUILD=/tmp/gal-<your-variant>
python3 scripts/design-gallery.py --no-serve
node scripts/check-variant.mjs <your-variant>
```

It must print `ok`. Then state plainly, in your final message, what a viewer would remember about
your variant that they would not remember about any of the other nine.
