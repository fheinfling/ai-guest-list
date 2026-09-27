# Design brief — the popover, second pass

For whoever designs or builds a variant. Read this before writing any CSS.

Everything in §2 is **observed**, not asserted: each finding was reproduced by rendering the
shipping UI from a real engine payload and looking at it. Reproduce them yourself with

```sh
python3 scripts/design-gallery.py          # builds, serves and opens the gallery
```

`v0-today` in that gallery is the current design, generated from `app/web/`. It is the baseline
every variant has to beat.

---

## 1. What this is and who it is for

**ai guest list** is a macOS menubar app for people who pay for more than one AI-coding
subscription. When the seat they are working on hits its usage limit, the app moves them to another
seat and resumes the session. With BYOK it can also fall back to a pay-per-token API key, or pin
one terminal to a key deliberately.

**The audience is one person: a working developer, mid-task, interrupted.** They did not open the
popover to browse. They opened it because something stopped, or because they want to know whether
something is about to stop. They will look at it for two or three seconds and close it again.

**The popover's job, in order:**

1. Say whether work can continue.
2. If it cannot, offer the one action that fixes it.
3. If real money is being spent, say so, and make stopping easy.
4. Everything else — per-seat detail, settings, adding things — is secondary and may be one level
   down.

Anything that does not serve 1–3 on open is detail, and detail must not outrank the answer.

**This app spends the user's money.** That is not a feature to be smoothed over. Every screen that
leads to a charge is held to the ethics rules in §5, which override visual preference.

---

## 2. What is wrong today — heuristic evaluation

Rated on Nielsen's 0–4 severity scale (frequency × impact × persistence). The heuristic number in
brackets is from Nielsen's ten. Each finding names the gallery state that shows it.

### Severity 4 — fix before anything else

**F1 · The decline button is visually recessive next to the spend button.** *(state: `asking`,
heuristics 3, 5; Brignull: interface interference / friction asymmetry)*
On the consent card, "use the key" is a full-width filled accent button; "not now" is borderless
text at body weight beside it. The action that costs money has roughly four times the visual pull
of the action that does not. This is the deceptive-pattern taxonomy's *visual interference*, in the
one place in the app where informed choice actually matters. **It must be fixed regardless of which
direction wins.**

**F2 · The model picker's default view shows the models nobody wants.** *(state: `models`,
heuristics 6, 8)*
458 real OpenRouter models, sorted cheapest first, so the list opens on
`stealth/space-bunny-alpha`, `inclusionai/ling-3.0-flash-sante:free`, `qwen/…:free`. The sort is
correct and useless. At roughly five lines per row that is ~2,300 lines of scroll in a 600px
window, and the only workable path — the filter — requires the user to *recall* an exact model id.
Recognition over recall fails completely.

**F3 · "price unavailable" is the display hero of the consent card.** *(state: `asking-unpriced`,
heuristics 2, 8)*
OpenAI, Anthropic and Langdock publish no machine-readable prices, so for the most likely providers
the largest element on the money-spending screen is the phrase *price unavailable* at ~40px, with
"input / output per million tokens" underneath labelling a number that is not there. An absence is
rendered as the headline.

### Severity 3 — fix in this pass

**F4 · Running paid sessions are listed twice, in different words.** *(state: `spending`,
heuristics 4, 8)*
`paidUseControl` (`render.mjs:602`) names each session above the scroll line; `pinnedSessions`
(`render.mjs:625`) renders a card per session below it with the same four facts in a different
order. Two owners for one truth.

**F5 · In the state where the key seat is the answer, it is below the fold.** *(state: `trouble`,
heuristic 1)*
When every subscription seat is resting, the body leads with a supervision banner, then the
auto-switch toggle, then a stale "auto-moved" history note, then two full resting seat cards. The
key seat — the only thing that can keep the user working — is last. The conclusion ("nothing is
ready; here is what you can do") is never stated; the user assembles it from parts.

**F6 · A resting seat states its return three times, in three formats, and two of them disagree.**
*(state: `trouble`, heuristics 4, 8)*
One card shows "back in 1h 35m" (header), "resets in 2h 59m" (5h row) and "taking a breather —
back 5:31 PM" (footer). The first is the soonest window, the second is that row's window; adjacent,
unexplained, and read as a contradiction.

**F7 · A rate is presented as if it answered "what will this cost me?"** *(state: `asking`,
heuristic 2)*
`$0.5 / $2.5` per million tokens is the hero. A developer mid-task cannot convert that into an
expected cost, because they have no idea how many tokens the next hour uses. The biggest number on
screen answers a question nobody asked. (It is also formatted `$0.5`, not `$0.50`.)

**F8 · Settings mixes "how switching works" with "may you spend real money".** *(state: `settings`,
heuristics 4, 8)*
Seven toggle rows in one card under one label, `auto-switch`. *allow paid key use* — the master
money switch — sits fourth, styled exactly like *restart the Codex app after a swap*, with a
five-line subtitle that is the longest text in the app.

**F9 · Actions complete with no visible change.** *(heuristic 1)*
Observed in real use: "check key" appeared to do nothing, and "use in new terminal" failed with
"please try again". Both were patched reactively; there is still no rule saying every action must
visibly acknowledge itself, so the next one will do it again.

### Severity 2 — worth fixing while here

**F10 · A 100 %-full bar means "exhausted".** *(state: `trouble`, heuristics 2, 4; WCAG 1.4.1)*
A filled progress bar conventionally means *complete/plenty*. Here it means the opposite, and only
colour distinguishes it — which also fails "never rely on colour alone".

**F11 · Per-row provenance noise.** *(state: `models`)*
"live price estimate · fetch age unavailable" repeats on all 458 rows; "1000000 token context" is
unformatted; a free model reads `$0/Mtok`, which looks like missing data rather than *free*.

**F12 · Stale history outranks live state.** *(states: `main`, `trouble`)*
"↪ auto-moved Codex · Work → Personal — Work's resting" sits above the seat list in low-contrast
mono, and stays there after it stops being news.

**F13 · The pinned session's `end` button has no button chrome.** *(state: `spending`)*
`.btn.switch` inside `.seat--key.pinned-session` renders as bare bold text. It does not look
clickable, and it is the control that stops one paid session.

**F14 · The generic tells are already in the shipping design.** *(all states)*
Tracked-out ALL-CAPS eyebrows (`PICK A MODEL`, `AUTO-SWITCH`, `APPEARANCE`), meta strings joined
with middle dots (`openai · codex · terminal 20887`), and a `→` appended to button text
(`pick a model →`). See §6 — these are exactly the defaults a variant must not reach for, and they
are already here.

---

## 3. The information architecture — decided once, shared by all five variants

Variants differ in **look**, not in structure. These decisions are settled; a variant that wants to
depart from one must say why in its README.

**IA-1 · The popover opens with a verdict, not with materials.**
The first thing below the header is one sentence stating the current situation, and — when
something needs doing — exactly one action. Examples of the three cases:
*"Personal is on the floor · 38% used"* (nothing to do) ·
*"everyone's resting until 5:31pm — Late-night can keep you going"* + one action ·
*"Late-night is spending · terminal 20887"* + stop.
The seat list stays below as the detail, always available, never the headline.

**IA-2 · One owner for "money is moving".**
`paidUseControl` and `pinnedSessions` merge into a single section that lists each paid session
once, each with its own end control, plus one section-level stop-everything. Nothing about a paid
session is stated in two places (fixes F4).

**IA-3 · The consent card is decision-first, with symmetric exits.**
- The hero is what is about to happen, in words — not a rate, and never the absence of a rate.
- **The two answers carry equal visual weight.** Same size, same shape, same prominence; they may
  differ in colour. Declining is never a text link (fixes F1).
- The price is a secondary supporting fact with its unit. When the provider publishes none, that is
  a normal-weight sentence — *"openai doesn't publish prices — check their pricing page"* — never a
  display-size phrase (fixes F3, F7).
- The request expires; show the remaining time, because the user is on a clock.
- `Esc` declines. Nothing is pre-selected.

**IA-4 · The model picker is search-first, and hides noise by default.**
- The search field takes focus when the step opens; it is the primary path, not a fallback.
- **Free/$0 models are excluded from the default list**, behind a visible, one-click, reversible
  chip (*"free models hidden · show"*). A free OpenRouter model is a rate-limited preview, not a
  coding seat, and burying the real choices under it is the actual defect (fixes F2).
- A row shows three things with clear rank: **model id** (primary), **price** (secondary,
  right-aligned, decimal-aligned), **context** (tertiary, formatted `1M`, not `1000000`).
- Provenance ("live price estimate", fetch age, cache state) appears **once** at section level,
  never per row (fixes F11).
- `free` is written as the word *free*, not `$0/Mtok`.
- The honest no-prices state stays a section-level sentence. Nothing may imply a price exists.

**IA-5 · Settings splits into three sections with distinct jobs.**
`switching` (strategy, supervise, same-tool, notify, restart-app) · `paid keys` (allow paid use,
ask before using) · `appearance` (theme, legend). Every row subtitle is **one line**; the long
explanation moves to a section footer. The money section is visually distinct from preferences
(fixes F8). Turning paid use **off** must be at least as easy and as prominent as turning it on.

**IA-6 · Every action acknowledges itself. Three tiers, no exceptions.**
- *Instant and local* → the control's own label changes (`check key` → `checking…` → result).
- *Background, under ~5s* → label change plus a toast on completion.
- *Consequential* (money, credentials, anything irreversible) → a persistent status line on the
  card it belongs to. Never a toast alone: a toast that is missed is a result that never happened.
Failures name the reason in the user's words and say what to do next. No "please try again"
(fixes F9).

**IA-7 · One fact, one representation.**
A resting seat states its return **once**, as a relative countdown. The absolute time is available
on hover/`title`, not as a third line. Per-window rows keep their own resets only where both
windows genuinely differ, and then they are labelled as windows (fixes F6).

**IA-8 · One way in.**
The header `＋` is the single global entry for adding anything, and opens a chooser: *a
subscription seat* or *an API key*. The bottom `＋ add a key` row and the per-group `＋ add a seat`
rows are removed. Global navigation lives in one place.

**IA-9 · Usage bars read correctly without colour.**
A bar must not say "exhausted" only by being full and orange. Show headroom rather than
consumption, or mark the exhausted state with a shape/label as well as a hue (fixes F10, WCAG
1.4.1).

**IA-10 · Live state outranks history.**
The "auto-moved" note is either demoted below the seat list or given an explicit recency that makes
it read as history (fixes F12).

**IA-11 · An empty guest list says what to do.**
First run, no seats: one sentence and one action. Not a blank panel.

---

## 4. Voice

Unchanged in character — **warm, lowercase, second person, never cute about money.**

| Dimension | Rule |
|---|---|
| Concepts | seats on a guest list; the active seat is *on the floor*; a limited seat is *resting* |
| Vocabulary | plain developer English; provider and model ids verbatim, never prettified |
| Verbosity | one line per idea; the whole consent card under ~35 words excluding the price |
| Grammar | lowercase sentence starts, second person, active voice, verbs lead buttons |
| Punctuation | no exclamation marks; no `→` appended to button text; no `·`-joined meta strings |

Two hard rules:

- **Never make spending sound smaller than it is.** No "just", no "only a few cents", no rounding
  down, no omitting that a sent turn still bills.
- **Errors say what happened and what to do**, in the interface's voice, without blame or apology.

---

## 5. Ethics — non-negotiable, overrides visual preference

Checked against Brignull's deceptive-pattern taxonomy, because this app spends real money:

1. **No friction asymmetry.** Declining, stopping and turning paid use off are always at least as
   easy and as prominent as the opposite. (F1 is the live violation.)
2. **No preselection** of anything that spends money. Paid use is off by default and stays off
   until an explicit act.
3. **No hidden cost.** Where a price is unknown, say it is unknown. Never omit the disclaimer to
   make a card tidier.
4. **No urgency theatre.** The consent expiry is real and may be shown; nothing may invent time
   pressure.
5. **No confirmshaming.** "not now" is a neutral, first-class choice, never phrased as a loss.

---

## 6. Hard constraints

Technical, and not negotiable by a design:

- **CSP** (`app/web/index.html`): `default-src 'none'; script-src 'self'; style-src 'self'
  'unsafe-inline'; font-src 'self'`. No CDN, no remote fonts, no network of any kind.
- **Fixed 376×600 WKWebView.** The page itself must never scroll (`styles.css:31`): each screen
  pins its header and scrolls its own body, or a rubber-band drag drags the header with it.
- **Light and dark are both first-class**, driven by the token block at `styles.css:12-28`.
- `prefers-reduced-motion` honoured.
- **Behaviour contract:** every `data-action` (33 of them), plus `data-card`, `data-key`,
  `data-model`, `data-tool`, `data-pin`, `data-value`, `data-provider`, `data-email`, `data-id`,
  `data-approved`, survives unchanged — `app/web/app.mjs:182` dispatches on them. The pure
  functions (`dotState`, `doorKey`, `doorMark`, `fmtCountdown`, `fmtUsageAge`, `pct`, `creditLeft`,
  `reduceReply`, `reduceKeyReply`) are not markup and must not change.
- **Typefaces:** new ones allowed if **OFL or equivalent**, vendored and subset into
  `app/web/fonts/`, listed in `app/web/fonts/LICENSE.md`. Today's four total 102 KB; stay in that
  neighbourhood.
- **Accessibility, WCAG 2.2 AA:** body-text contrast ≥ 4.5:1 and UI-component contrast ≥ 3:1 **in
  both themes**; visible keyboard focus on every control; nothing carried by colour alone; target
  size ≥ 24×24; status changes announced (`role="status"` / `aria-live`).
- `--codex: #46c2a8` and `--claude: #e0795a` identify the two tools. A variant may restyle them,
  but the two tools must stay distinguishable **without relying on colour alone**.

---

## 7. Anti-generic guardrail

Current AI-generated design clusters hard around a few looks. A variant that lands on one of these
has spent its freedom on a default, and will be rejected:

- warm cream ground (~`#F4F1EA`) + high-contrast serif display + terracotta accent (~`#D97757`);
- near-black ground + a single acid-green or vermilion accent;
- broadsheet layout: hairline rules, zero radius, dense newspaper columns;
- the SaaS-card kit: everything chopped into identical rounded cards, one radius for every element
  regardless of hierarchy, the same soft grey shadow under each, gradient washes as decoration;
- template chrome: tracked-out ALL-CAPS eyebrows above every heading, `A · B · C` meta strings,
  `WORD — fragment` labels with a spaced em dash, tinted near-black (`#0B0B0B`) standing in for
  black, monospace for small data labels, `→` appended to button text.

Note that **the shipping design already commits three of these** (F14), so "like today but tidier"
is not a direction — it is the thing being replaced.

Each variant must be a deliberate response to *this* subject: a door-and-disco guest list for a
developer who is mid-task and mildly annoyed. Spend the boldness in **one** place and keep
everything around it quiet.

---

## 8. What each variant must deliver

`design/variants/v<N>-<name>/`:

- `render.mjs` — a fork of `app/web/render.mjs`, same exports, same behaviour contract.
- `styles.css` — a fork of `app/web/styles.css`.
- `README.md` — one paragraph: the palette as 4–6 named hex values, the typefaces and their roles,
  the layout concept, and **what the one memorable element is**. Say what was deliberately not
  done.

It must render all eight gallery states, in both themes:

| state | what it is testing |
|---|---|
| `main` | the everyday glance |
| `spending` | money running: one owner for the fact, an obvious stop |
| `asking` | priced consent — symmetric exits (F1) |
| `asking-unpriced` | consent with no price — the common case (F3) |
| `trouble` | nothing ready: does the verdict lead? is the key seat findable? (F5) |
| `settings` | three sections, one-line subtitles, money set apart (F8) |
| `models` | 458 real models — the density test (F2) |
| `models-unpriced` | the honest no-price column |

Add variants and re-run `python3 scripts/design-gallery.py`; they are picked up automatically.
