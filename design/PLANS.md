# Five directions for ai guest list

Design plans only. The authority is [DESIGN-BRIEF.md](../docs/DESIGN-BRIEF.md); IA-1–IA-11 apply to every direction. No variant implementation is included.

## Evidence and limits of this pass

Read, in the requested order: the brief; the frontend-design skill (all seven installed copies have the same hash); the UX index and its interaction/visual design, design systems, content/service design and accessibility topics; all 347 lines of `styles.css` and all 869 lines of `render.mjs`. Also inspected the dispatcher in `app.mjs`, font files/licences, the gallery generator and its catalogue fixture.

`python3 scripts/design-gallery.py --no-serve` succeeded. The sandbox denied the localhost server's socket bind. Headless Chrome then aborted with exit 134 for both screenshot and `--dump-dom` commands. As a fallback, I executed the actual render exports with the generated gallery payload for **all eight states in both themes**, and inspected their generated content. Consent was evaluated at the gallery's creation time so its real two-minute expiry did not remove the cards during inspection. I also viewed the repository's existing main/settings screenshots. Those tall screenshots are visual references, **not evidence of fit at 376×600**. No new browser layout or dark-theme screenshots were available; the dimensions below are explicit design budgets requiring later visual verification. No test suites were run.

What that inspection established:

| State, inspected in light and dark | Evidence that governs these plans |
| --- | --- |
| `main` | Auto-switch and history precede the list. Five entries comprise four subscriptions and one key. Personal/Codex and Studio/Claude are selected, but neither is reported in a session: calling either “on the floor” would overstate the payload. |
| `spending` | Late-night, `gpt-5.4` and terminal 20887 appear in both the paid-use strip and pinned-session block. The ordinary key entry appears again below. |
| `asking` | The real fixture uses `anthropic/claude-haiku-4.5:batch`, with input $0.50 and output $2.50 per million tokens. The current largest text is the input rate. |
| `asking-unpriced` | The hero is literally “price unavailable”; a unit caption remains beneath it. This request pins one terminal, unlike the automatic fallback in `asking`. |
| `trouble` | Supervision and history lead; the unproven key follows two resting Codex entries. Their seat-return and window-reset values differ. The fixture says four resting/zero ready but leaves both Claude entries ready/selected; the verdict must identify the affected tool rather than invent consistent global state. |
| `settings` | Paid permission and consent preference are embedded in switching preferences. The permission explanation alone is 48 words. |
| `models` | 458 buttons and 2,751 text fragments. The first entry is `stealth/space-bunny-alpha`; provenance repeats. The fixture contains 21 explicitly free/zero-input-and-output entries, leaving 437 when hidden. The longest ID is 56 characters. |
| `models-unpriced` | Seven IDs, no prices and no context values. A generic “prices are estimates” footer wrongly survives even though no estimate exists. |

Counts and rates in these plans come from this fixture, not a claim about today's live catalogue. Countdown examples are illustrative; shipping text must use actual timestamps.

## Shared design contract

These are part of **each** direction, not optional common advice. Cooper's transient-interface principle puts the answer before inspection; the UX skill's content-first and real-content principles govern the actual copy and dimensions below. Functional components remain consistent across the set; perceptual choices differ.

### Meaning, money and navigation

- **IA-1:** Header first, verdict immediately below. For the actual `main` fixture: “Personal is ready for Codex.” For a confirmed live session: “Personal is on the floor with Codex.” Do not infer a running session from selected credentials. The default verdict has no unnecessary action. When blocked, exactly one corrective action occupies the verdict area. For `trouble`, use “your Codex seats are resting; Late-night is unproven”, followed by “paid per use. price unknown. no spend cap.” and the existing **use in new terminal** action. The label must describe that real action, not promise a review step the dispatcher may skip. Disclose the unproven endpoint before the action; keep the existing paid permission/consent gates.
- **IA-2:** The paid-session section itself becomes the verdict area while money is moving. One session: “Late-night is spending in terminal 20887.” The section then supplies provider, tool and model once, an obvious **end session** button, and the section-level **stop paid use** button. Multiple sessions: aggregate verdict, one row per session and one end control per row. No separate `pinnedSessions` cards further down. Key configuration may remain in the seat list, but must not repeat running-session identity or activity. Two terminals sharing a key remain two sessions; a fallback already represented by a pin is not listed again.
- **IA-3:** Consent leads with “use Late-night's paid key to continue?”; pinned consent instead says “pin this terminal to Late-night's paid key?”. Under it: provider and unmodified model ID, then the price fact, “no spend cap. sent turns still bill.”, the real remaining time and two equal answers. The priced example is about 30 words including provider, model, expiry, Esc hint and buttons, excluding price/provenance. The unpriced equivalent stays below approximately 35 excluding its price explanation. There is no predicted hourly bill: the app has no evidence for one.
- **F1/ethics:** **not now** and **use the key** have identical width, height, border, fill, type weight and interaction states. Both start unselected. Initial focus belongs to the decision heading, not a spend action; Tab reaches decline first. Esc declines the relevant pending request, including when it arrives over another screen. Expiry disables approval, leaves a persistent “request expired; no paid switch approved” result and does not silently approve or bounce focus. Money and credential outcomes persist on their owning surface.
- **F3/F7:** Price is 13–14px supporting text, never display type. Show “input $0.50 / output $2.50 per million tokens”, with its live-estimate provenance once. With no machine-readable price: “openai doesn't publish machine-readable prices; check its pricing page.” The final phrase is a labelled provider-pricing action. Do not claim the provider has no public prices. No empty unit caption or fabricated zero. Keep all supplied rate precision; pad ordinary amounts to at least two decimal places without rounding down tiny amounts such as $0.017.
- **IA-5:** Settings consists of **switching**, **paid keys**, **appearance**, in that order. Switching holds auto-switch, strategy, supervision, same-tool, notifications and restart-app. The existing main auto-switch control moves here. Paid keys holds allow-paid-use and ask-before-using. Appearance holds theme and legend. Example one-line subtitles: “switch when a seat rests”, “use codex or claude directly”, “stay with the current tool”, “show who takes over”, “use the new desktop login”, “permits billed requests”, “approve each paid switch”. Long explanations move to section footers, including: “paid use starts off. enabling it permits fallback and pinned terminals. turning it off stops paid sessions and new requests. sent turns may still bill.” Disabling confirmation explicitly explains that eligible keys can spend without asking. On/off uses the same control and target, with readable state text and thumb position; off is never recessed.
- **IA-6:** Local actions change their own label immediately. Background actions also announce completion via toast. Consequential actions retain a status on their own entry/card as well as immediate feedback. Example: **check key → checking… → account access checked**; persistent qualification: “inference access is still unproven.” Failure example: “the provider rejected this key. add a replacement with +.” Never convert an account-access check into a claim that inference works. Keep status readable without reopening a disclosure. An endpoint proof that spends money states “this check sends a billed request”; it receives the same informed, symmetric consent treatment, not the shipping phrase “a small amount”.
- **IA-7/9:** Collapsed seats show headroom and one return countdown. Bars represent **remaining** capacity, labelled “62% left”; exhaustion is an empty track with an end-stop mark and “0% left / resting”, not a full bar recoloured. Unknown is “usage unavailable”, not zero. Stale readings say “last known”. Detailed window rows appear on expansion, retain separate resets only when different, and identify “5h window” and “weekly window”. The seat-return fact is not repeated there. Absolute return time is available on hover and keyboard focus; `title` alone is insufficient for keyboard access.
- **IA-8/11:** Header **＋** is the sole global add entry. It opens a chooser with “a subscription seat” and “an API key”; the subscription branch retains the two tool choices. Existing sign-in repair remains contextual. Empty first run says “add a seat so you can keep working.” Its sole action is the header add control, temporarily expanded to **add a seat**; no duplicate bottom button or invented onboarding page.
- **IA-10:** History sits after the complete seat list. Without a reliable event timestamp, label it “last switch”; do not invent “just now”. Quit lives at the quiet end of the same internal body, not in a competing bottom navigation bar.

### One search contract for all five pickers

The model step always focuses the labelled search field. Its placeholder, “try claude, gpt or a model id”, teaches that a remembered family/name works; matching retains both IDs and display names. Model IDs themselves remain verbatim, in a proportional sans face. No paid model is preselected by focus or by being first.

A visible 28px-high chip reads **free models hidden [show]**. One activation changes it to **free models shown [hide]**, preserving the query; neither state is hidden in settings. Classify explicitly free variants and genuinely zero input-and-output rates as free; never coerce unknown, blank or missing prices to zero. The fixture's default count is **437 of 458 models**, with 21 hidden. A zero-result state says whether hidden free matches exist and retains the show control. Display the word **free** when revealed and a section note that free OpenRouter models are rate-limited previews.

Use stable model-ID alphabetical order within the visible set, with the order stated beside the count. This deliberately ends cheapest-first as a recommendation proxy. No invented “popular”, compatibility scores, favourites or curated defaults. Search is primary: alphabetization alone does not make 437 entries useful. The wireframes show the real first three paid IDs and their fixture prices.

Each row has exactly three ranked facts: **model ID**, **price**, **context**. At 344px usable width, allocate 200px to identity/context, 12px gap and 132px to price. Price has input above output, common column labels/units, tabular proportional figures, right alignment and aligned decimal separators. Reserve fractional space rather than quantizing precision to force alignment. Context is compact, e.g. `131K context`, `1M context`; exact token count is available on focus/hover. IDs wrap at safe visual break opportunities without inserting characters into their values; the 56-character case may grow to three lines. Never truncate the only distinguishable model suffix or shrink it below 13px to hit a row count.

One fixed footer gives provenance/cache state and fetch age, plus the provider-pricing action and spend-cap reminder. The fixture requires “live price estimate; fetch time unavailable”, not an invented freshness value. Stale/error language replaces the ordinary source sentence. With entirely unpriced data, use “openai doesn't publish machine-readable prices. check its pricing page.” once at section level; remove the price-column heading and estimate wording, and give identity the freed width. The seven unknown contexts stay absent. In mixed results, an unknown rate is explicitly “unknown” in that row; a missing input rate never hides a known output rate.

### Dimensions and accessibility

All measurements are CSS pixels at **376×600**, with 16px side gutters and a **344px content width**. Diagram borders describe regions, not a requirement to draw those borders. Brackets indicate controls. Repeated short labels inside diagrams stand for the full accessible label given in prose. All alignment is left except numeric prices, headroom columns and paired actions. Wireframes describe content order inside the labelled scroll regions; they do not shrink rows to make the entire list visible. At the specified row heights, the slips and note show about four complete seat entries before scrolling; history follows the list below the initial fold. The register, doorway and console can expose all five compact entries, with history still allowed below the fold.

- Fixed 52px header. The document/page never scrolls. Main, settings and add flows have their own shrinking, keyboard-scrollable body. Normal text is 14px/20px; supporting text is at least 13px/18px. A shared spacing scale is 4, 8, 12, 16 and 24px. No viewport calculation assumes a wider window.
- Main allocations below total exactly 600px. Heights are budgets for ordinary content, not fixed clipping boxes. Long verdicts or identifiers gain height by reducing the internal detail viewport; they do not overlap the header or disappear behind actions. A blocked verdict with its paid/unproven warning and 44px action may use 188px, leaving 360px below the 52px header.
- Consent uses **52 header + 64 verdict + 328 decision region + 156 detail viewport**. The 328px region includes both 44px-high answers. At 344px, each answer is **166px wide with a 12px gap**. Direction 2 adds 16px to the decision region for its ticket edge and takes that from details. Longer proof/error text scrolls within the decision region, with both choices held together at its edge and warnings preceding them in reading order. No decoration consumes a separate panel.
- Picker uses **52 header + 160 search/filter/column-heading region + 300 results + 88 provenance/footer = 600**. Search is 40px high. The 300px result pane alone scrolls; focus and caret survive result changes. Typical rows show four to five results, with the next row discoverable through a scrollbar. This is a search interface, not a claim that all 458 choices fit. Longer IDs increase row height.
- Spending uses one integrated money region below the header, capped at 240px under normal conditions: verdict and stop-everything remain reachable; session rows scroll there if needed. Details use the remainder. A single session uses less. If consent arrives while spending, the same urgency region allocates internal scrolling to requests/session details and keeps **stop paid use** and the active consent answers reachable; neither is put behind the guest list. Do not stack two unbounded fixed panels.
- Settings has **52 header + 548 internal body**. Section heading 20px, toggle row about 52px, strategy about 84px. Paid keys begins within the initial body viewport; appearance may require internal scrolling. Subtitles are edited to one line at normal size, allowed to wrap when text is enlarged rather than clipped. Section footers carry all necessary qualifications.
- Controls are at least 28×28px here, exceeding the 24×24 brief minimum; primary, stop and consent controls are 44px high. Native keyboard-operable buttons, inputs and disclosures get visible focus, meaningful accessible names and state. Focus uses a 2px signal outline separated by a 2px ground-coloured gap so it remains distinct from filled controls. Scroll padding keeps focus out from under pinned regions. Tab order follows reading order.
- Text contrast is calculated from the hex pairs below. Body target is **at least 7:1** in each theme, supporting text at least 4.5:1, control boundaries and focus at least 3:1 on both ground and surface. No opacity is applied to meaningful secondary text. These are token calculations, not a claim of audited WCAG conformance. [W3C contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
- Retain `--codex: #46C2A8` and `--claude: #E0795A` as small identity details, never essential light-theme text or the sole status cue. Codex has a square marker and its name; Claude a round marker and its name. Markers receive a contrasting outline. “ready”, “selected”, “resting”, “spending” and “needs sign-in” remain text. Status uses an appropriate polite live region; errors are announced. Do not announce a countdown every second or reread 458 results after each keypress; announce the result count once after input settles.
- No idle disco twinkle, spinning background or urgency pulse. Optional response transitions last at most 120ms and become immediate under reduced motion. Progress always has words; a stopped decorative spinner cannot imply a stalled operation.
- At 200% text size, reflow multi-column rows into vertical facts and stack the two equal consent buttons at equal full widths. Condense the header to accessible icon controls, keep it pinned, and put oversized verdict/search content into the internal body rather than consuming the entire viewport. Maintain a usable scroll area and all actions. Reflow to 320px content width with no horizontal scrolling is also a later acceptance check. [W3C reflow guidance](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html).

### Fonts and behaviour boundary

No direction requires a new typeface or remote resource. All use the existing local OFL Hanken Grotesk, with some using existing Outfit 700; no small metadata uses Space Mono. Hanken's current woff2 is 50,796 bytes; Outfit's is 14,064 bytes. Their combined use is about 65 KB; the current four-file bundle is 101,916 bytes. Unused files may remain during a later variant experiment; these plans add zero font bytes. The existing licence inventory omits Outfit; a future implementation that uses it must add its [upstream OFL attribution](https://github.com/Outfitio/Outfit-Fonts/blob/main/OFL.txt). No font files or inventory are changed now.

Keep all exports, bridge payloads, action meanings and dispatch values. Preserve `data-card`, `data-key`, `data-model`, `data-tool`, `data-pin`, `data-value`, `data-provider`, `data-email`, `data-id`, `data-approved`, and clock attributes `data-usage-at`, `data-reset-at`, `data-clock-prefix`, `data-session-at`. Keep the input IDs and result container used by `app.mjs`; retain compatible `.seat.expanded`, `.main-body`, `.set-body` and `.key-prompts` hooks. The listed pure functions (`dotState`, `doorKey`, `doorMark`, `fmtCountdown`, `fmtUsageAge`, `pct`, `creditLeft`, `reduceReply`, `reduceKeyReply`) are untouched. Door styling can be quieted around the unchanged `doorMark` output; none of these concepts needs a rewritten icon function.

The source inventory is broader than the brief's “33”: 34 literal rendered actions, dynamic `set_strategy` and `set_theme`, and the runtime `key-remove-confirm`, for **37 distinct action values**. Preserve all of them:

- `add`, `add-back`, `add-cancel`, `add-change`, `add-cta`, `add-import`, `add-method`, `add-provider`, `add-reveal`, `add-save`.
- `key-start`, `key-provider`, `key-back`, `key-cancel`, `key-discover`, `key-model`, `key-save`, `key-answer`, `key-stop`, `key-terminal`, `key-validate`, `key-prove`, `key-remove`, `key-remove-confirm`, `key-pricing`.
- `paid-key-back`, `paid-key-enable`, `end-pinned-session`, `switch`, `remove`, `toggle`, `supervision-on`, `settings`, `settings-back`, `set_theme`, `set_strategy`, `quit`.

The paid-key-enable gate receives symmetric exits too; its longer enable label may use two lines, with both buttons the same height. A neutral visual label never changes the underlying approval value. Interaction dependencies that cannot honestly be delivered by markup/CSS alone are listed in **Objections**; no direction silently invents a new bridge action.

## v1-door-register — A working guest list with one unmistakable place to look

**Concept.** Treat the panel as the register held by the person at the door: names line up, the current answer has a bracket in the margin, and the rest is available without a ceremony. The developer is checking whether their name still gets them in, not admiring a club poster. The organizing device is a continuous list with a status gutter, so active, resting and paid entries share a readable scan path without becoming a pile of cards. The door vocabulary carries the subject; the bracket carries attention.

**Palette.** Six core roles; fixed tool markers are additional identity constants from the shared contract.

| Role | Light | Dark |
| --- | --- | --- |
| ground | `#FFFFFF` | `#162D40` |
| grouped surface | `#EAF0F7` | `#203E54` |
| body ink | `#000000` | `#FFFFFF` |
| supporting ink | `#485569` | `#C7D6E3` |
| control edge | `#6B7B8E` | `#8AA2B6` |
| attention/focus | `#214BB2` | `#AFC9FF` |

Body-on-ground target ≥7:1; calculated **21.00:1 light, 14.16:1 dark**. Supporting ink's worst pairing across ground/surface is 6.59:1 light, 7.54:1 dark. Control edges reach at least 3.78:1 and 4.22:1 respectively. Attention colours belong to the bracket and focus; consent remains two neutral outlined buttons.

**Type.** Existing Hanken Grotesk throughout: 700 at 22px/26px for the verdict, 600 at 14px for names, 400 at 14px/20px for body and 13px/18px for support. Tabular figures for price/headroom. The wordmark uses the same family at 700, not its current yellow/Outfit split. No Space Mono, new face or new subset; about 51 KB of fonts used.

**Layout.** Main budget: **52 header + 88 verdict + 460 list viewport**. A 20px noninteractive margin gutter contains one 3px bracket, exclusively around the verdict. Seat rows have 64px minimum height, 4px focus rounding and no individual surface boxes. Tool groups separate by 16px space; state and action sit on the trailing edge. Disclosure exposes window detail in the same list. Controls have 8px corners; the panel retains 16px outer rounding.

```text
main                         376 x 600
+----------------------------------------+
| ai guest list                 [+] [...]| 52 fixed
| [ Personal is ready for Codex.         | 88 verdict
| [                                      |
|                                        |
| Codex                                  | list scrolls
|   Personal               selected [v]  |
|   62% left                             |
|   Work                    resting [v]  |
|   back in 1h 36m                       |
|   Late-night                 key [v]   |
|   openai                               |
| Claude                                 |
|   Personal                  ready [v]  |
|   Studio                 selected [v]  |
|                                        |
| last switch                            |
| Work gave way to Personal              |
|                                 [quit] |
+----------------------------------------+
```

```text
asking
+----------------------------------------+
| ai guest list                 [+] [...]|
| [ Work is resting; a paid key          | verdict
| [ needs your answer.                   |
|                                        |
| use Late-night's paid key              | 22px decision
| to continue?                           |
| openrouter                             |
| anthropic/claude-haiku-4.5:batch         |
| input $0.50 / output $2.50              | supporting
| per million tokens                     |
| live estimate; fetch time unavailable  |
| no spend cap. sent turns still bill.   |
| expires in 1m 42s. Esc declines.        |
| +----------------+  +----------------+ |
| |    not now     |  |  use the key   | | equal 166 x 44
| +----------------+  +----------------+ |
| Codex                                  | details scroll
|   Personal                  selected   |
+----------------------------------------+
```

```text
models
+----------------------------------------+
| [back] add a key              [cancel] |
| search models                          | fixed search
| [try claude, gpt or a model id        ] |
| [free models hidden             show] |
| 437 of 458 models; model id order      |
| model / context         input / output|
|                         $ per 1M tokens|
| aion-labs/aion-2.0                $0.80| results scroll
| 131K context                     $1.60|
|                                        |
| aion-labs/aion-3.0                $3.00|
| 131K context                     $6.00|
|                                        |
| aion-labs/aion-3.0-mini           $0.70|
| 131K context                     $1.40|
| ...                                    |
| live price estimate                    | fixed footer
| fetch time unavailable                 |
| [check provider pricing]               |
| this app doesn't cap spend.            |
+----------------------------------------+
```

Picker rows are 60px minimum, approximately five visible. The margin bracket does not migrate onto every search result. `asking-unpriced` replaces the three price lines with the ordinary provider sentence. `models-unpriced` becomes a full-width ID register under that section sentence. `spending` uses the bracketed verdict as the heading of its sole money section; end controls have the same visible outline as start controls. In `trouble`, the bracket contains the shared Codex/unproven-key verdict, paid warning and **use in new terminal** before any list detail. `settings` uses open row groups, with paid keys alone enclosed by a 2px control-edge boundary and a plain heading.

**The one memorable element.** The register's margin bracket holding the live verdict.

**Deliberately not doing.** No coloured row rails, pills for every status, shaded card per seat, paper texture, hand-drawn tick marks or animated door. The bracket is structural emphasis, not another clickable object.

**How it answers the severity-4 findings.**

- **F1:** Two neutral 166×44px framed answers with identical stroke, type and focus treatment; neither is filled alone.
- **F2:** Focused name/ID search, 21 free entries initially hidden behind the visible reversible chip, 60px rows, one provenance footer and aligned rates.
- **F3:** The action sentence stays the heading; unavailable machine-readable pricing is 13px supporting prose and an explicit pricing-page action.

**Anti-generic self-check, §7.**

1. The light combination is white/black/blue (`#FFFFFF`, `#000000`, `#214BB2`), with Hanken sans; no cream, high-contrast serif or terracotta. Claude's fixed small coral marker is identity, not the palette accent.
2. Dark uses visibly blue `#162D40` and pale blue attention, not near-black with acid green/vermilion. Tool colours cannot spread into fills.
3. A register is a near neighbour of a broadsheet. Here it is one vertical list, separated by space, without hairline ruling or newspaper columns. Controls and outer shell are rounded; the actual bracket is 3px, not a hairline grid.
4. The five seats are unboxed rows; only the consequential settings group gets an enclosure. There are no repeated card shadows, universal radius or washes.
5. The proposed labels are “Codex”, “search models”, “last switch”; normal case and normal tracking. Provider/model occupy separate lines. No middle-dot strings, spaced-em-dash labels, near-black substitute ink, mono metadata or arrow-suffixed buttons appear in the wireframes. `#000000` is the light text black.

## v2-admission-slips — Distinct seats as flat admission slips, with the decision on top

**Concept.** The developer has several ways through the same door, like a handful of admission slips. Each seat is a thin, legible object with identity on its broad side and state at its stub; the top slip states whether work can continue. This makes switching accounts feel concrete without gamifying paid access. The primary organizing device is a shallow vertical stack of flat slips, not a dashboard grid. The one physical reference is a small clipped ticket edge; no fake ticket number, price of entry or VIP hierarchy is invented.

**Palette.**

| Role | Light | Dark |
| --- | --- | --- |
| ground | `#F0EDF7` | `#352544` |
| slip surface | `#FFFFFF` | `#463253` |
| body ink | `#000000` | `#FFFFFF` |
| supporting ink | `#5B4C68` | `#DFCEEA` |
| control edge | `#84708E` | `#B39ABE` |
| attention/focus | `#74378E` | `#E2B7F4` |

Body-on-ground target ≥7:1; calculated **18.16:1 light, 14.00:1 dark**. Supporting ink stays ≥6.78:1 / 7.69:1 across both surfaces; edges ≥3.87:1 / 4.50:1. Ticket surfaces carry body ink. The attention colour marks the top slip's notch and focus; it is not a special fill behind “use the key”.

**Type.** Existing Outfit 700 for the complete verdict/decision sentence at 22px/26px and seat names at 16px/20px; Hanken 400/600 at 14px/20px for instructions and controls, 13px/18px for supporting facts. Prices use Hanken tabular figures. No mono serial-number motif. About 65 KB used from existing woff2 files; no new face or download, with the Outfit attribution task noted above.

**Layout.** Main budget: **52 header + 104 verdict slip + 444 stack viewport**. All slips stay horizontal; no rotation, overlapping text or swipe/drag interaction. A 6px edge notch and 8px ends suggest the object; 72px seat strips are separated by 8px. The verdict has more depth than the seat strips. The right stub is 100px wide and uses words, never just a coloured dot. Long countdowns move into the broad area. Controls are 8px-rounded rectangles, independent of the ticket silhouette.

```text
main                         376 x 600
+----------------------------------------+
| ai guest list                 [+] [...]|
|  /----------------------------------\  |
| < Personal is ready for Codex.        > | verdict slip
|  \----------------------------------/  |
| Codex                                  | stack scrolls
|  /-----------------------+----------\  |
| < Personal               | selected > |
|  \ 62% left              |      [v]/  |
|  /-----------------------+----------\  |
| < Work                   | resting  > |
|  \ back in 1h 36m         |      [v]/  |
|  /-----------------------+----------\  |
| < Late-night             | key      > |
|  \ openai                |      [v]/  |
| Claude                                 |
|  / Personal                  ready  \ |
|  \ Studio                 selected  / |
| last switch                            |
| Work gave way to Personal       [quit] |
+----------------------------------------+
```

Claude has two separate 72px slips; the compact diagram abbreviates their outlines. A state label never functions as an unlabeled disclosure: the actual button name includes the seat and tool.

```text
asking
+----------------------------------------+
| ai guest list                 [+] [...]|
| Work is resting; a paid key            | verdict
| needs your answer.                     |
|  /----------------------------------\  |
| < use Late-night's paid key           > | one decision slip
| | to continue?                       | |
| | openrouter                         | |
| | anthropic/claude-haiku-4.5:batch     | |
| | input $0.50 / output $2.50          | |
| | per million tokens                 | |
| | live estimate; fetch time unknown  | |
| | no spend cap. sent turns still bill.| |
| | expires in 1m 42s. Esc declines.    | |
|  \----------------------------------/  |
| [     not now    ]  [   use the key   ] | equal outside edge
|                                        |
| Codex                                  | details scroll
|   Personal                   selected  |
+----------------------------------------+
```

Answers sit on the same uninterrupted baseline below the slip, within the decision region. Neither answer occupies a detachable-looking stub. This avoids implying that one choice is admission and the other a discarded ticket.

```text
models
+----------------------------------------+
| [back] add a key              [cancel] |
| search models                          |
| [try claude, gpt or a model id        ] |
| [free models hidden             show] |
| 437 of 458 models; model id order      |
| model / context         input / output|
|                         $ per 1M tokens|
| aion-labs/aion-2.0                $0.80| results scroll
| 131K context                     $1.60|
|                                        |
| aion-labs/aion-3.0                $3.00|
| 131K context                     $6.00|
|                                        |
| aion-labs/aion-3.0-mini           $0.70|
| 131K context                     $1.40|
| ...                                    |
| live price estimate                    |
| fetch time unavailable                 |
| [check provider pricing]               |
| this app doesn't cap spend.            |
+----------------------------------------+
```

The catalogue is an open list of 68px rows, approximately four visible; 458 decorative tickets would defeat search. Ticket form belongs only to actual seats and their decision. In `spending`, a single larger slip owns the live-session facts, with end controls on its broad face and stop-everything at the verdict edge; it is not duplicated in the stack. `asking-unpriced` uses the same slip size with normal-weight price prose. In `trouble`, the top slip names the unproven Codex fallback, includes the shared paid warning and offers **use in new terminal**; resting subscriptions never cover it. Settings is three flat open sections; paid keys alone has a clipped-edge enclosure, with identical on/off treatment. `models-unpriced` drops the numerical stub/column entirely and states the limitation once.

**The one memorable element.** The small admission-slip notch, identifying a seat as something you can use at the door.

**Deliberately not doing.** No perforation pattern, barcode, serial numbers, drop shadows, rotated pile, wristband colours, glossy paper or tactile drag. This costs some row density: the main stack scrolls sooner than the register.

**How it answers the severity-4 findings.**

- **F1:** Identical 166×44px answers outside the ticket silhouette; no spend-only stub, fill or ticket metaphor implying a preferred answer.
- **F2:** Model search stays conventional and focused, with the 21 free entries hidden/revealable and compact open rows instead of 458 physical objects.
- **F3:** The ticket headline describes the paid action; its price absence is ordinary Hanken prose with a pricing link, not a giant amount or stamp.

**Anti-generic self-check, §7.**

1. Lavender ground `#F0EDF7`, white slips and purple attention are not cream/terracotta. Outfit is geometric sans, with no serif display.
2. Dark is visibly plum `#352544`; attention is pale violet. Neither acid green nor vermilion is a theme accent.
3. The broad single-column slips have curved ends, not dense newspaper columns or hairline article divisions.
4. **Closest risk: the SaaS-card kit.** Real seats do repeat an object shape. They are shallow strips with a state stub, not identically padded cards containing whole mini-dashboards; the verdict has a different depth, picker rows are unboxed, settings is open, and no element has a shadow or gradient. If the later rendering loses the stub hierarchy and becomes ordinary rounded cards with notches pasted on, reject this direction.
5. Names occupy the broad face; states read “selected”, “resting”, “key” at normal tracking. No uppercase ticket stamp, dot-joined metadata, dash label, mono serial or trailing action arrow. Light ink is literal black. The notch is the only decorative convention borrowed from print.

## v3-doorway — The answer occupies the doorway; details wait below it

**Concept.** On opening the popover, you look through one broad doorway and read whether work can continue. The framing makes the status answer the destination of the glance, as the venue entrance would be, while the guest list beneath becomes quiet reference. The primary organizing device is a single status aperture with compact disclosure rows below it. This is a departure from the current tiny animated door logo: the memorable doorway is the region containing useful words, not a separate illustration asking for attention.

**Palette.**

| Role | Light | Dark |
| --- | --- | --- |
| ground | `#EAF7F4` | `#163A38` |
| doorway surface | `#FFFFFF` | `#244C48` |
| body ink | `#000000` | `#FFFFFF` |
| supporting ink | `#3C5D56` | `#C4DDD5` |
| control edge | `#62877C` | `#89B6A8` |
| attention/focus | `#006857` | `#95E1CB` |

Body-on-ground target ≥7:1; calculated **19.11:1 light, 12.38:1 dark**. Supporting ink stays ≥6.61:1 / 6.65:1; edges ≥3.62:1 / 4.23:1. The doorway frame uses attention; text on its surface uses body ink. No white text on the light Codex teal.

**Type.** Existing Hanken only: 700 at 24px/28px for the complete verdict, 600 at 14px for seat summaries and controls, 400 at 14px/20px for prose and 13px/18px for support. No display numeral or mono data. About 51 KB used; no new face/subset. The 24px size applies to a short sentence, not to a rate or “unknown”.

**Layout.** Main budget: **52 header + 144 aperture + 404 detail viewport**. One 344px-wide frame has 28px top corners and 8px bottom corners, with a 2px stroke; nothing else gets an arch. Its sentence is left-aligned, not centred like a marketing hero. Below are 52–60px disclosure rows with tool, seat and a concise state. Window bars live in expanded detail. The doorway is a container, not an open/shut gauge: its geometry never contradicts the actual state text or `doorKey`.

```text
main                         376 x 600
+----------------------------------------+
| ai guest list                 [+] [...]|
|       __________________________       |
|     /                            \     |
|    | Personal is ready            |    | verdict aperture
|    | for Codex.                   |    |
|    |                              |    |
|    +------------------------------+    |
| Codex                                  | details scroll
| Personal                   selected [v]|
| Work              back in 1h 36m [v]   |
| Late-night                      key [v]|
| Claude                                 |
| Personal                      ready [v]|
| Studio                     selected [v]|
|                                        |
| last switch                            |
| Work gave way to Personal       [quit] |
+----------------------------------------+
```

```text
asking
+----------------------------------------+
| ai guest list                 [+] [...]|
| Work is resting; a paid key            | compact verdict
| needs your answer.                     |
|       __________________________       |
|     /                            \     |
|    | use Late-night's paid key    |    | decision aperture
|    | to continue?                 |    |
|    | openrouter                   |    |
|    | anthropic/claude-haiku-      |    |
|    | 4.5:batch                    |    |
|    | input $0.50 / output $2.50   |    |
|    | per million tokens          |    |
|    | live estimate; age unknown  |    |
|    | no spend cap.               |    |
|    | sent turns still bill.      |    |
|    +------------------------------+    |
| expires in 1m 42s. Esc declines.        |
| [     not now    ]  [   use the key   ] | equal, squarely aligned
| Codex                                  | details scroll
+----------------------------------------+
```

Only one aperture exists in a state: in asking, it moves to frame the decision while the verdict above becomes plain. The diagram's ID line break is visual only. The actual frame spans the full 344px; the drawing's curve is schematic, not a narrow 260px card. In a longer decision, its border follows the whole decision region rather than adding decorative height.

```text
models
+----------------------------------------+
| [back] add a key              [cancel] |
| search models                          | no arch on inputs
| [try claude, gpt or a model id        ] |
| [free models hidden             show] |
| 437 of 458 models; model id order      |
| model / context         input / output|
|                         $ per 1M tokens|
| aion-labs/aion-2.0                $0.80| results scroll
| 131K context                     $1.60|
| aion-labs/aion-3.0                $3.00|
| 131K context                     $6.00|
| aion-labs/aion-3.0-mini           $0.70|
| 131K context                     $1.40|
| ...                                    |
|                                        |
| live price estimate                    |
| fetch time unavailable                 |
| [check provider pricing]               |
| this app doesn't cap spend.            |
+----------------------------------------+
```

Picker rows are 60px minimum, approximately five visible. `spending` puts the sole money section in the aperture, with the stop action immediately visible and session endings below; the frame never glows or pulses for money. In `trouble`, the text says “your Codex seats are resting; Late-night is unproven”, with the shared paid warning and **use in new terminal** in the aperture, and supervision repair in details. `asking-unpriced` keeps the decision type size and swaps in normal price prose. `models-unpriced` uses the shared full-width list. Settings stays rectilinear; paid keys has a 2px outlined section, not a second doorway.

**The one memorable element.** A single broad doorway framing the answer.

**Deliberately not doing.** No disco ball animation, large separate door icon, coloured spotlight, doorway around every control, headroom gauge in the aperture or decorative second headline. Some glanceable seat detail is surrendered to make the verdict dominant.

**How it answers the severity-4 findings.**

- **F1:** The two 166×44px answers share a neutral baseline and treatment; the arch encloses the decision facts, never only the paid answer.
- **F2:** A dedicated focused search step replaces the large aperture, preserving room for five compact results and the one-click free-model control.
- **F3:** The aperture frames what you are deciding; unknown pricing has the same 13px supporting treatment as known pricing.

**Anti-generic self-check, §7.**

1. Mint ground `#EAF7F4`, black ink and deep green attention have no cream/serif/terracotta combination. The type is Hanken sans.
2. **Near neighbour: a dark green developer tool.** `#163A38` is visibly teal rather than near-black, with pale mint `#95E1CB`, not acid green. There is no luminous terminal text, black canvas or neon effect. The visual identity comes from the aperture, not an accent-on-black theme.
3. One large curved frame and spaced disclosure rows cannot read as a broadsheet. No columns or hairline ruling.
4. Only the answer has the arched enclosure. Seat/model rows are open; controls use 8px corners. No repeated card grid, shadow system or gradient fill.
5. The whole verdict is one weight and colour. Search/provider/model labels use normal case, separate lines and proportional type. Light ink is `#000000`; no dot chains, dash labels, uppercase eyebrows or arrow suffixes. “doorway” is the design rationale, not an extra UI label.

## v4-booth-console — A compact operating board for seats and remaining capacity

**Concept.** A venue's booth needs to know which channel can carry the next part of the set. Here the channels are subscriptions and paid keys: their availability must be compared without reading several miniature reports. The organizing device is a compact operational matrix below the verdict, with aligned remaining-capacity cells. Its one distinctive move is the short segmented headroom strip, useful data rather than a fake audio meter. It serves the developer who opens the panel to see what is about to run out, while the verdict still supplies the immediate answer.

**Palette.**

| Role | Light | Dark |
| --- | --- | --- |
| ground | `#E3E8ED` | `#303236` |
| console surface | `#F8FAFC` | `#41464C` |
| body ink | `#000000` | `#FFFFFF` |
| supporting ink | `#495561` | `#D0D8E0` |
| control edge | `#70808E` | `#9DACBC` |
| attention/focus | `#27539E` | `#B8CDFF` |

Body-on-ground target ≥7:1; calculated **17.03:1 light, 12.84:1 dark**. Supporting ink ≥6.18:1 / 6.61:1; control edges ≥3.30:1 / 4.11:1 across the two backgrounds. Dark is middle charcoal, not nearly black. Capacity segments use body/supporting ink; attention belongs to focus and the verdict's action, not a rainbow of meters.

**Type.** Existing Hanken only, with 700 at 19px/24px for verdict and decision, 600 at 14px for names, 400 at 14px/20px for prose, 13px/18px for column headings and data. Tabular figures, not Space Mono, align quantities. About 51 KB used; no new font or subset. Less display-size contrast buys more comparable information without shrinking text.

**Layout.** Main budget: **52 header + 76 verdict + 472 matrix viewport**. The 344px matrix is one continuous surface. Allocate 132px to the seat, 60px to each capacity window, 68px to the action, and three 8px gutters. Column headings explicitly say capacity is left. Names can wrap; at 200% the rows become vertical descriptions. A row is at least 64px. Each strip has five coarse segments plus its exact percentage; the segments are a redundant overview, not a measurement scale. Resting rows show an empty strip with an end-stop and a labelled countdown below the name. Controls have 6px corners; the single console enclosure has 12px corners.

```text
main                         376 x 600
+----------------------------------------+
| ai guest list                 [+] [...]|
| Personal is ready for Codex.           | verdict
|                                        |
| Codex          5h left  week left      | matrix scrolls
| Personal       [###..]  [####.]    [v]  |
| selected          62%      79%         |
| Work           [|....]  [#....]    [v]  |
| back in 1h 36m     0%      26%         |
| Late-night                        [v]  |
| api key; openai                        |
|                                        |
| Claude         5h left  week left      |
| Personal       [#####]  [#####]    [v]  |
| ready             95%      96%         |
| Studio         [####.]  [####.]    [v]  |
| selected          80%      84%         |
|                                        |
| last switch                            |
| Work gave way to Personal       [quit] |
+----------------------------------------+
```

```text
asking
+----------------------------------------+
| ai guest list                 [+] [...]|
| Work is resting; a paid key            | verdict
| needs your answer.                     |
|                                        |
| use Late-night's paid key              | decision, not meter
| to continue?                           |
| openrouter                             |
| anthropic/claude-haiku-4.5:batch         |
| input $0.50 / output $2.50              |
| per million tokens                     |
| live estimate; fetch time unavailable  |
| no spend cap. sent turns still bill.   |
| expires in 1m 42s. Esc declines.        |
|                                        |
| [     not now    ]  [   use the key   ] | equal 166 x 44
|                                        |
| Codex          5h left  week left      | detail matrix scrolls
| Personal          62%      79%    [v]  |
+----------------------------------------+
```

```text
models
+----------------------------------------+
| [back] add a key              [cancel] |
| search models                          |
| [try claude, gpt or a model id        ] |
| [free models hidden             show] |
| 437 of 458 models; model id order      |
| model / context         input / output|
|                         $ per 1M tokens|
| aion-labs/aion-2.0                $0.80| results scroll
| 131K context                     $1.60|
| aion-labs/aion-3.0                $3.00|
| 131K context                     $6.00|
| aion-labs/aion-3.0-mini           $0.70|
| 131K context                     $1.40|
| aion-labs/aion-3.5                $3.00|
| 262K context                     $6.00|
| ...                                    |
| live price estimate                    |
| fetch time unavailable                 |
| [check provider pricing]               |
| this app doesn't cap spend.            |
+----------------------------------------+
```

Picker rows are 52px minimum, five complete short-ID rows plus part of a sixth; long IDs grow. The two fact columns are a list, not a spreadsheet with tiny interactive cells. In `spending`, the money section replaces the verdict region with session rows and obvious end/stop controls; it shows no invented live cost counter. In `trouble`, the shared unproven-key verdict, paid warning and **use in new terminal** sit above the empty Codex strips. The actual Claude values are not falsely zeroed to match the fixture aggregate. `asking-unpriced` is prose, never an empty numeric readout. `models-unpriced` removes price cells and the entire price heading. Settings aligns controls in three sections; paid keys gets a 2px boundary and “real money” in its footer, without styling spending as a red alarm light.

**The one memorable element.** The five-segment headroom strip aligned across seats.

**Deliberately not doing.** No animated VU meters, faders, knobs, fake waveform, monospaced terminal skin, colour-coded severity heatmap or aggregate score. The matrix sacrifices the warmth of object-like seats for comparison speed.

**How it answers the severity-4 findings.**

- **F1:** Consent exits use the same 166×44px control shell, with no green “go” versus unframed “no”.
- **F2:** Focused search, reversible free filtering and the densest readable rows in the set; decimal alignment helps comparison, not price-as-recommendation.
- **F3:** No “unknown” readout or giant rate; ordinary supporting text states the absence and points to provider pricing.

**Anti-generic self-check, §7.**

1. Cool grey `#E3E8ED`, literal black and blue attention, all in sans: no warm cream/serif/terracotta trio.
2. `#303236` is a visibly mid-dark console, with pale blue focus, not near-black and acid green/vermilion. There are no luminous segments.
3. **Closest risk: dense broadsheet columns.** This is one labelled comparison matrix, not several columns of prose. It uses whitespace and surface grouping, no hairline grid, with 6px controls and a 12px shell. If implementation adds boxed cells, ruling everywhere or compressed 10px metadata, it fails the direction.
4. One shared matrix replaces individual seat cards. The consent decision is open typography, settings groups are unequal in treatment, and there are no repeated shadows or washes.
5. Column labels are “5h left” and “week left”, not uppercase instrument captions. Terminal/provider/model facts occupy separate lines. Hanken tabular numbers remain proportional type; there are no mono labels, middle-dot strings, dash headings, near-black ink substitute or arrow-suffixed actions.

## v5-handover-note — A short note from the door, organized by sentences

**Concept.** Imagine the person at the door leaving the next person a clear handover: who can go on, who needs a break, and whether someone is spending money. The developer receives a short, typeset note instead of navigating a machine's inventory. Its primary organizing device is a hierarchy of brief sentences on a single surface; the verdict is the first and most emphatic sentence, and each seat is one compact paragraph with an attached disclosure. The door-and-disco language earns its place by explaining continuity, not by introducing decoration.

**Palette.**

| Role | Light | Dark |
| --- | --- | --- |
| ground | `#F6F6F6` | `#3D2831` |
| decision surface | `#FFFFFF` | `#503640` |
| body ink | `#000000` | `#FFFFFF` |
| supporting ink | `#5F4B52` | `#E8CCD7` |
| control edge | `#88717A` | `#BD96A5` |
| attention/focus | `#91365C` | `#FFC0D7` |

Body-on-ground target ≥7:1; calculated **19.43:1 light, 13.58:1 dark**. Supporting ink ≥7.43:1 / 7.23:1; edges ≥4.14:1 / 4.15:1. Attention is reserved for focus and actionable text decoration. The verdict stays black/white as a complete sentence, without one rose-coloured keyword.

**Type.** Existing Outfit 700 at 26px/30px for the complete verdict and 22px/26px for the consent question. Hanken 400 at 14px/21px for short paragraphs, 600 for names and controls, 13px/18px for supporting facts. Sentence spacing supplies hierarchy instead of metadata labels. Prices and IDs stay Hanken; no serif or mono. About 65 KB used from current local files; no new subset and the same future Outfit attribution requirement.

**Layout.** Main budget: **52 header + 116 opening sentence + 432 note viewport**. The verdict is a two-line hanging paragraph with a 12px inset after its first line; the same whole-sentence shape is the only typographic gesture. Subsequent seat paragraphs have no hanging indent, 72px minimum blocks and 16px between tool groups. Each paragraph contains one idea per line. Controls retain 8px corners; the note is not an invitation to replace buttons with inline text links. Headroom bars are secondary inside disclosed detail; collapsed prose states “62% left”.

```text
main                         376 x 600
+----------------------------------------+
| ai guest list                 [+] [...]|
| Personal is ready                     | opening verdict
|   for Codex.                           |
|                                        |
| Codex                                  | note body scrolls
| Personal is selected.                  |
| you have 62% left.                 [v] |
|                                        |
| Work is resting.                       |
| back in 1h 36m.                   [v] |
|                                        |
| Late-night is an openai key.       [v] |
|                                        |
| Claude                                 |
| Personal is ready.                [v] |
| Studio is selected.               [v] |
|                                        |
| last switch                            |
| Work gave way to Personal.      [quit] |
+----------------------------------------+
```

```text
asking
+----------------------------------------+
| ai guest list                 [+] [...]|
| Work is resting; a paid key            | compact verdict
| needs your answer.                     |
|                                        |
| use Late-night's paid key              | decision sentence
|   to continue?                         |
|                                        |
| openrouter                             |
| anthropic/claude-haiku-4.5:batch         |
| input $0.50 / output $2.50              |
| per million tokens                     |
| live estimate; fetch time unavailable  |
|                                        |
| no spend cap. sent turns still bill.   |
| expires in 1m 42s. Esc declines.        |
| [     not now    ]  [   use the key   ] | equal outlined buttons
|                                        |
| Codex                                  | details scroll
+----------------------------------------+
```

```text
models
+----------------------------------------+
| [back] add a key              [cancel] |
| search models                          |
| [try claude, gpt or a model id        ] |
| [free models hidden             show] |
| 437 of 458 models; model id order      |
| model / context         input / output|
|                         $ per 1M tokens|
| aion-labs/aion-2.0                $0.80| results scroll
| 131K context                     $1.60|
|                                        |
| aion-labs/aion-3.0                $3.00|
| 131K context                     $6.00|
|                                        |
| aion-labs/aion-3.0-mini           $0.70|
| 131K context                     $1.40|
| ...                                    |
| live price estimate                    |
| fetch time unavailable                 |
| [check provider pricing]               |
| this app doesn't cap spend.            |
+----------------------------------------+
```

Picker rows are 64px minimum, four complete rows with a continuation. Search stays utilitarian: turning IDs and prices into sentences would destroy scanning. In `spending`, the opening sentence is owned by the single money section; the model and end control follow as short lines, with stop-everything at the top. No paid story appears again in the note. `trouble` leads with the shared Codex limitation, paid warning and **use in new terminal**; supervision and historical prose follow the live detail. `asking-unpriced` replaces price lines with the ordinary provider sentence, not an oversized paragraph about absence. `models-unpriced` drops the price column and estimate footer. Settings is a short document with three headings; paid keys has a solid 2px side bracket, readable “paid keys” heading and regular buttons/toggles. This small delimiter distinguishes consequence without becoming a second display motif.

**The one memorable element.** The opening verdict as a large, slightly hanging sentence, like the first line of a handover.

**Deliberately not doing.** No salutation, signature, serif editorial styling, paper grain, ornamental quotation mark, prose paragraph for every field or chat transcript. No bolded single “important” word. The note trades compact numerical comparison for immediate comprehension.

**How it answers the severity-4 findings.**

- **F1:** Even in a text-led direction, decline and approve remain equal 166×44px buttons; neither is an inline link or sentence ending.
- **F2:** The model step deliberately returns to search and aligned compact facts, with free entries hidden/revealable; it does not make 458 prose paragraphs.
- **F3:** The large sentence asks about the paid action. Missing rates are a normal-weight 13px explanation with the provider-pricing action.

**Anti-generic self-check, §7.**

1. **Near neighbour: an editorial document.** The ground is neutral `#F6F6F6`, not cream, and both families are sans. Rose `#91365C` is used for focus rather than terracotta decoration. No high-contrast serif appears.
2. Dark uses visibly wine-coloured `#3D2831` and pale rose, not near-black plus vermilion. Meaningful text is white, not a coloured terminal display.
3. Despite the document metaphor, there are no hairline rules, newspaper columns or zero-radius control system. One column, paragraph spacing and rounded controls keep it a note.
4. Seat paragraphs share the ground; they are not cards. The decision surface is a single consequential region, with no shadow or wash. Different radii follow control versus panel roles.
5. “Codex”, “paid keys” and complete sentences replace tracked eyebrows and `WORD — fragment` labels. Provider and model are separate lines; no dot chains, mono metadata or arrow-suffixed buttons. `#000000` is actual black. The hanging sentence keeps one weight/colour across all words, avoiding the generated-page trick of accenting one phrase.

## Comparison and later acceptance

| Direction | Primary organizing device | The one remembered thing | Deliberate tradeoff |
| --- | --- | --- | --- |
| v1-door-register | Continuous list with status gutter | Bracket at the live verdict | Less object-like separation |
| v2-admission-slips | Stack of shallow seat objects | Admission notch | Fewer seats visible at once |
| v3-doorway | Large status region, compact disclosures | Doorway containing the answer | More detail is one disclosure away |
| v4-booth-console | Capacity comparison matrix | Segmented headroom strips | More operational, less conversational |
| v5-handover-note | Hierarchy of short sentences | Hanging opening verdict | Slower cross-seat numerical comparison |

The model-step fundamentals intentionally converge: an unfamiliar catalogue of 458 IDs is the wrong place to spend the visual novelty. The home panel carries each direction; shared search and ethical controls carry comprehension.

A later build must render all eight states in both themes at the real size and verify keyboard/VoiceOver behavior, long IDs, long seat names, multiple paid sessions, simultaneous consent, failure acknowledgement, unknown/stale readings, empty first run and enlarged text. Compare time to identify the verdict and stop paid use, not just screenshots. This plan run does not claim those checks have passed.

## Objections

**No objection to IA-1–IA-11.** These plans preserve them. There are material implementation and evidence constraints to resolve before treating any direction as build-ready:

1. **The promised behavior exceeds the current markup-only boundary.** `app.mjs` does not decline pending consent on Esc; its clock refresh removes expired requests but does not update a visible consent countdown; it does not focus model search on opening; its filter state has no free-model visibility flag; several actions expose only a toast or no immediate pending state. Native markup can supply some local disclosure behavior, but it cannot by itself deliver query-aware free counts, durable asynchronous feedback and correct Esc routing. IA-3/4/6 remain requirements. A later implementation needs narrowly scoped UI-state/event work while preserving the bridge actions and protected reducers. These plans do not authorize or disguise that work as CSS.
2. **Per-session ending is not uniformly available in the payload/dispatch contract.** Pins have `tool` and `pin` for `end-pinned-session`; `running_key_seats` may expose only key IDs, and `key-stop` stops everything. IA-2 still requires a real end control for every session, but a non-pinned fallback needs an addressable session/stop contract confirmed by the app owner. Do not wire an apparently individual “end session” button to stop every session, fabricate a pin or hide the unresolved case.
3. **The trouble fixture and selected-session semantics need an agreed verdict source.** The generated `trouble` aggregate conflicts with its Claude rows, and `main` has selected rather than running sessions. The plans use honest tool-scoped text and preserve the rows as received. A later gallery fix should produce a truly all-resting case as well. An unproven key cannot be promised to “keep you going”. These plans therefore use the truthful “use in new terminal” label with the paid/unproven/unknown-price warning already visible. They do not rename `key-terminal` to “review paid key”: its flow depends on existing permission state and cannot guarantee an extra review step.
4. **A real countdown is not automatically an accessibility exception.** The brief requires expiry, and all designs display it without invented urgency. WCAG's timing requirement still needs an adjustable/extendable limit or a documented essential-time-limit basis. The sources inspected here do not establish that exception. Keep safe expiry and no automatic approval, but resolve timing accessibility before claiming full AA; it cannot be solved by changing type or colour. [W3C timing guidance](https://www.w3.org/WAI/WCAG22/Understanding/timing-adjustable.html).
5. **Verification remains bounded.** Browser execution was blocked, so the per-state content inspection and calculated palettes support these plans but cannot prove text wrapping, focus preservation or WKWebView fit. The font inventory also needs Outfit attribution if retained. These are explicit next-pass requirements, not reasons to silently relax the brief or change application code in this run.
