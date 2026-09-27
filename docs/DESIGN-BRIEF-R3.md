# Design brief, round three — iterate v10-ambient's glance

`v10-ambient` won. Round three explores **one screen**: the glance (`main`). Everything else in
v10 — consent, spending, settings, the picker — stays exactly as it is.

Read `DESIGN-BRIEF.md` (findings, IA-1…IA-11, voice, ethics, constraints) and
`DESIGN-BRIEF-R2.md` (why round one was wireframes, the icon as identity) first. Then
`design/variants/v10-ambient/` in full: you are forking it, not restarting.

---

## 1. What the user asked for, verbatim

> *"on the glance i want to be able to see right away which model is active and how much headroom
> we still have left weekly and 5 hourly (ideally in one line and as minimal as the screen
> currently is) … allowing me to see everything right away. every subscription is resting should
> show the closed door in the back, not the disco ball."*

Four requirements, and the tension between them is the whole design problem:

1. **The active seat, immediately.** Not after a scroll, not after a disclosure.
2. **Both headroom windows — 5-hour and weekly — ideally on one line.**
3. **Everything visible right away.** No progressive disclosure for the primary facts.
4. **As minimal as v10 is now.** v10's restraint is why it won. More information, same calm.

## 2. A data truth you must not design around

**Subscription seats have no model.** The payload gives `name`, `email`, `plan`, `status`,
`usage5h`, `usageWeek`, `limited_until`, `usage_stale`, `usage_unknown` — and nothing about which
model Codex or Claude is running, because that is chosen inside the CLI per session and never
reaches the app. Only a **key seat** carries a real `model` field (e.g. `gpt-5.4`).

So "which model is active" resolves, honestly, to:

- **the tool and the seat** — *Codex · Personal (Business)* — for a subscription, and
- **the literal model id** when a key seat is pinned or running.

Show the tool and seat as the active identity. Show a model id **only** where one genuinely
exists. Do not print a placeholder model, do not infer one from the harness, and do not label a
seat name as a "model". Inventing this would be the same failure as inventing a price.

## 3. Headroom: both windows, one line

`usage5h` and `usageWeek` are percentages **used**; IA-9 requires the display to show what is
**left**, and never to rely on colour alone. `usage_unknown` means say so rather than print a
zero; `usage_stale` means mark the reading as last-known.

The line must answer, at a glance: *how much can I do in the next few hours*, and *how much is
left this week*. Whether it is two figures, one glyph carrying two tracks, or a sentence with the
numbers set large is your direction's job (§5). What it may not be is two separate progress bars
stacked with labels — that is `v0-today`, and it is what the redesign is replacing.

## 4. The closed door

The mechanism already exists: `doorKey(state)` returns `"open"` when any seat is ready or active
and `"shut"` when every seat rests; `doorMark(state)` draws a disco ball for open and a closed
cream door with a gold knob for shut. **Keep both pure functions untouched.**

What is new: when every subscription is resting, the **closed door becomes a large background
presence** behind the glance — not a 36px badge in the header. The field already turns gold in
that state; the door belongs in it, at scale, quiet, behind the words. When a seat is ready, it is
the lit room and the ball, as now.

Use the real artwork from `app/icon.svg` — the cream leaf `#F5EEDD → #D8C8A8` with its `#CF9B2E`
knob, the gold tile, the warm glow. Inline SVG is not restricted by the CSP. It must sit *behind*
the content and must never reduce text contrast below 4.5:1 — test both themes.

## 5. Your assigned direction

Fork `v10-ambient`. Change **`buildHTML` and the styles the glance needs**, nothing else. Keep
v10's colour-field system (mint ready, gold resting, coral spending, violet consent), its palette,
its `ui-rounded` type, and its other seven screens intact.

**`v11-pair`** — the two figures are the hero, side by side, one line: the 5-hour reading and the
weekly reading given equal typographic weight, with the seat named above them. The whole screen is
one sentence and two numbers. The risk: two big numbers with tiny labels read as a scoreboard —
make clear at a glance which is which without a legend.

**`v12-arc`** — one drawn glyph carries both windows: concentric arcs, the inner for 5-hour, the
outer for weekly, the seat named at its centre. A single thing to look at. The risk: a donut
chart. It must be legible at 376px, readable without colour, and not a dashboard widget.

**`v13-band`** — literally one line: a single full-width band split into two hairline tracks, 5-hour
above, weekly below, the seat name at its left and the two percentages right-aligned at its end.
Everything else on the screen is quiet. The risk: it becomes a progress bar. Give the band a
reason to be a band.

**`v14-sentence`** — no chart at all. The reading is prose with the numbers set large inline:
*"Personal has 62% left now, 79% this week."* Typography does all the work. The risk: numbers
buried in running text are slower to scan than numerals — set them so they are not.

**`v15-roster`** — abandon the single hero. Every seat gets one line: name, 5-hour, weekly, with
the active one clearly first and emphasised. Nothing is hidden and nothing is summarised. The
risk: this is the easiest to turn back into `v0-today` — it must stay as airy as v10.

## 6. What still binds you

- IA-1 (verdict first), IA-9 (headroom not consumption, never colour alone), IA-10 (history last),
  and the rest of IA-1…IA-11.
- The behaviour contract; `scripts/check-variant.mjs` enforces it, including that each state still
  contains the control it exists to exercise.
- 376×600, page never scrolls, light **and** dark, WCAG 2.2 AA, `prefers-reduced-motion`.
- The ethics rules — untouched, and the consent screen is inherited from v10 unchanged.
- The four states that are not `main` must still render correctly. You are not redesigning them,
  but you must not break them.

## 7. Deliverable

`design/variants/v<N>-<name>/` with `render.mjs`, `styles.css`, `README.md`. The README states what
changed **relative to v10** — not a fresh manifesto — and one sentence on how the closed door
reads at scale.

```sh
export AGL_GALLERY_BUILD=/tmp/gal-<your-variant>
python3 scripts/design-gallery.py --no-serve
node scripts/check-variant.mjs <your-variant>
```

Must print `ok`. Then say, in one sentence, how someone opening this screen learns the active seat
and both headroom figures without reading twice.
