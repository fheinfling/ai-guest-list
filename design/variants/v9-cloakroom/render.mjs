// v9-cloakroom: tactile claim tags. Forked from app/web/render.mjs.
// Pure render logic for the "ai guest list" popover. No DOM or bridge side effects.
// Round-two hierarchy: verdict, paid consent/control, then pick-up seat tags.
// System rounded type; native buttons use the existing card dispatcher for keyboard access.

export const TOOL_META = {
  codex: { label: "Codex", plan: "CHATGPT BUSINESS", accent: "var(--codex)" },   // teal
  claude: { label: "Claude", plan: "CLAUDE CODE", accent: "var(--claude)" },     // coral
};

// menu-bar aggregate dot (spec §9): rose=needs a hello · gold=just switched · amber=a seat
// resting · green=everyone fresh.
const DOT_COPY = {
  hello: { label: "needs a hello" }, switched: { label: "just switched you" },
  amber: { label: "a seat's resting" }, green: { label: "everyone's fresh" },
};

export function dotState(state) {
  const key = state?.dot || dotKey(state);
  return { key, ...DOT_COPY[key] };
}

export function dotKey(state) {
  const seats = ["codex", "claude"].flatMap((t) => state?.tools?.[t]?.seats || []);
  if (seats.some((s) => (s.status || "") === "needs-login")) return "hello";
  if (state?.recently_switched) return "switched";
  // Mirror acctsw.web_dot (shared golden fixtures): a terminal cannot free quota, so use status
  // just like the header counts.
  if (seats.some((s) => ["resting", "queued"].includes(s.status))) return "amber";
  return "green";
}

// Door open/shut — mirror of acctsw.web_dot.door_for (golden fixture keeps them in lockstep).
export function doorKey(state) {
  if (state?.door === "open" || state?.door === "shut") return state.door;
  const seats = ["codex", "claude"].flatMap((t) => state?.tools?.[t]?.seats || []);
  const free = seats.some((s) => ["ready", "active"].includes(s.status));
  return free || seats.length === 0 ? "open" : "shut";
}

// The header door mark (same glyph the menu bar swaps), matching the handoff icon-states prototype:
// open = warm room with a spinning disco ball + twinkles and the door swung ajar; shut = a closed
// cream door with a gold knob. Animated purely in CSS; aria-label carries the meaning.
export function doorMark(state) {
  const key = doorKey(state);
  const label = key === "open" ? "a model's free — come on in" : "every seat's resting";
  const inner = key === "open"
    ? `<span class="door-room"><span class="door-string"></span><span class="door-ball"></span>` +
      `<span class="tw tw1"></span><span class="tw tw2"></span>` +
      `<span class="tw tw3"></span><span class="tw tw4"></span></span>` +
      `<span class="door-leaf"></span>`
    : `<span class="door-room"></span><span class="door-panel"></span><span class="door-knob"></span>`;
  return `<span class="avatar door door--${key}" role="img" aria-label="${label}">${inner}</span>`;
}

export function needsHello(seat) {
  return (seat.status || "") === "needs-login" || (seat.usage || {}).error === "unauthorized";
}

export function pct(seat, win) {
  const v = win === "5h" ? seat?.usage5h : seat?.usageWeek;
  const raw = v != null ? v : (seat?.usage?.windows?.[win]?.used_pct);
  return typeof raw === "number" ? Math.max(0, Math.min(100, raw)) : null;
}

export function creditLeft(seat) {
  if (seat?.usage_unknown) return null;
  const used = ["5h", "weekly"].map((w) => pct(seat, w)).filter((v) => v !== null);
  return used.length ? Math.round(100 - Math.max(...used)) : null;
}

export function fmtCountdown(iso, now = Date.now()) {
  if (!iso) return "";
  const ms = new Date(iso).getTime() - now;
  if (isNaN(ms) || ms <= 0) return "now";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins}m`;
  if (mins > 24 * 60) {
    const totalHours = Math.floor(mins / 60);
    const days = Math.floor(totalHours / 24);
    const hours = totalHours % 24;
    return `${days}d${hours ? `${hours}h` : ""}`;
  }
  const hrs = Math.floor(mins / 60);
  return `${hrs}h${mins % 60 ? ` ${mins % 60}m` : ""}`;
}

export function fmtUsageAge(iso, now = Date.now()) {
  const fetched = iso ? new Date(iso).getTime() : NaN;
  if (!Number.isFinite(fetched)) return "waiting for first reading";
  const seconds = Math.max(0, Math.floor((now - fetched) / 1000));
  if (seconds < 60) return `updated ${seconds}s ago`;
  if (seconds < 3600) return `updated ${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `updated ${Math.floor(seconds / 3600)}h ago`;
  return `updated ${Math.floor(seconds / 86400)}d ago`;
}

export function fmtSessionAge(iso, now = Date.now()) {
  const started = iso ? new Date(iso).getTime() : NaN;
  if (!Number.isFinite(started)) return "";
  const minutes = Math.max(0, Math.floor((now - started) / 60000));
  if (minutes < 60) return `, ${minutes}m`;
  if (minutes < 1440) return `, ${Math.floor(minutes / 60)}h`;
  return `, ${Math.floor(minutes / 1440)}d`;
}

// Clock ticks change text only: leave focused buttons, expanded cards, and scroll position intact.
export function updateClockText(root, now = Date.now()) {
  for (const node of root.querySelectorAll("[data-expires-at]")) {
    node.textContent = consentExpiry(node.dataset.expiresAt, now);
  }
  for (const node of root.querySelectorAll("[data-usage-at]")) {
    node.textContent = fmtUsageAge(node.dataset.usageAt, now);
  }
  for (const node of root.querySelectorAll("[data-reset-at]")) {
    node.textContent = `${node.dataset.clockPrefix || "resets in"} ${fmtCountdown(node.dataset.resetAt, now)}`;
  }
  for (const node of root.querySelectorAll("[data-session-at]")) {
    node.textContent = fmtSessionAge(node.dataset.sessionAt, now);
  }
}

function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  let h = d.getHours(); const m = String(d.getMinutes()).padStart(2, "0");
  const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
  return `${h}:${m} ${ap}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

const PLAN_CHIPS = {
  business: "Business", team: "Team", enterprise: "Enterprise", pro: "Pro",
  plus: "Plus", max: "Max", free: "Free",
};

function planChip(plan) {
  const key = typeof plan === "string" ? plan.trim().toLowerCase() : "";
  const label = Object.prototype.hasOwnProperty.call(PLAN_CHIPS, key) ? PLAN_CHIPS[key] : null;
  return label ? `<span class="mono chip">${label}</span>` : "";
}

// --- seat card --------------------------------------------------------------------------------

function statusBit(tool, seat) {
  switch (seat.status) {
    case "active": return seat.in_session ? "on the floor" : "selected";
    case "queued": return "up next";
    case "resting": return `<span data-reset-at="${esc(seat.limited_until)}" data-clock-prefix="back in" title="${esc(fmtClock(seat.limited_until))}">back in ${fmtCountdown(seat.limited_until)}</span>`;
    case "needs-login": return "sign-in needed";
    default: return "ready";
  }
}

function bar(seat, win, label) {
  const used = pct(seat, win);
  const left = used === null ? null : Math.round(100 - used);
  const windows = seat.usage?.windows || {};
  const reset = windows[win]?.resets_at;
  const distinct = windows['5h']?.resets_at && windows.weekly?.resets_at
    && windows['5h'].resets_at !== windows.weekly.resets_at;
  // The resting return belongs to the tag heading; window resets are only a distinct detail.
  const showReset = reset && (seat.status !== "resting" || (distinct && reset !== seat.limited_until));
  return `<div class="window"><div class="usage"><span>${label} window</span><strong>${left === null ? "reading unavailable" : left === 0 ? "exhausted" : `${left}% left`}</strong></div>
    <div class="track${left === 0 ? " exhausted" : ""}" aria-hidden="true"><span class="fill" style="width:${left ?? 0}%"></span></div>
    ${showReset ? `<span class="usage-reset" data-reset-at="${esc(reset)}" data-clock-prefix="window resets in" title="${esc(fmtClock(reset))}">window resets in ${fmtCountdown(reset)}</span>` : ""}</div>`;
}

// Ordinals indicate position within the tool group, not account IDs.
function seatCard(tool, seat, ordinal = 1) {
  const detailId = `seat-${tool}-${encodeURIComponent(seat.email || "")}`;
  const left = creditLeft(seat);
  const reported = seat.usage?.reported_windows;
  const weeklyOnly = tool === "codex" && Array.isArray(reported) && reported.includes("weekly") && !reported.includes("5h");
  const at = seat.usage_fetched_at || seat.usage?.fetched_at || "";
  const stale = seat.usage_stale || seat.usage_unknown;
  const issue = ({rate_limited:"usage updates throttled; retrying automatically", network:"connection unavailable; retrying automatically",
    token_expired:`open ${TOOL_META[tool].label} to refresh usage`, unauthorized:"sign in to refresh usage",
    forbidden:"check your subscription to refresh usage", no_token:"sign in to refresh usage"})[seat.usage?.error];
  const login = needsHello(seat);
  const action = login ? `<button data-action="add" data-tool="${tool}">${seat.entitlement_revoked ? "subscription ended; sign in again" : "sign in"}</button>`
    : !["active","resting","queued"].includes(seat.status) ? `<button data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch to this seat</button>` : "";
  return `<article class="seat seat--${esc(seat.status)} seat--${tool}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <span class="tag-stub" aria-hidden="true"><span class="punch"></span><span class="tag-number">${String(ordinal).padStart(2,"0")}</span><span class="tool-shape ${tool}"></span></span>
    <button type="button" class="seat-row" aria-controls="${esc(detailId)}"><span class="seat-name">${esc(seat.name)}</span><span class="chevron" aria-hidden="true">⌄</span>
      <span class="seat-status">${statusBit(tool,seat)}</span>${seat.status === "resting" || login ? "" : `<span class="headroom">${left === null ? "usage unknown" : `${left}% left${stale ? " (last reading)" : ""}`}</span>`}<span class="sr-only disclosure-show">show details</span><span class="sr-only disclosure-hide">hide details</span></button>
      <div class="expand" id="${esc(detailId)}"><div class="seat-email">${esc(seat.email)} ${planChip(seat.plan)}</div>
      ${weeklyOnly ? "" : bar(seat,"5h","5h")}${bar(seat,"weekly","7d")}
      <p class="usage-age"><span data-usage-at="${esc(at)}">${fmtUsageAge(at)}</span>${stale ? "; last known reading" : ""}</p>
      ${issue ? `<p class="usage-error" role="status">${issue}</p>` : ""}
      ${!seat.active && seat.in_session ? `<p>also in a terminal<span data-session-at="${esc(seat.session_started_at)}">${fmtSessionAge(seat.session_started_at)}</span></p>` : ""}
      <div class="seat-detail-actions">${action}<button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out</button></div></div>
    </article>`;
}

function toolGroup(tool, t, keys = []) {
  const seats = t?.seats || [];
  if (!seats.length && !keys.length) return "";
  return `<section class="group group--${tool}" style="--accent:${TOOL_META[tool].accent}">
    <h2 class="g-head"><span class="tool-shape ${tool}" aria-hidden="true"></span>${TOOL_META[tool].label}<span class="g-count">${seats.length + keys.length} seats</span></h2>
    <div class="tag-stack">${seats.map((s,i)=>seatCard(tool,s,i+1)).join("")}${keys.map(keySeatCard).join("")}</div></section>`;
}

const CLAIM_ART = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="92 92 840 840" class="claim-art" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="cloak-tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffe6ad"/>
      <stop offset="55%" stop-color="#f7c668"/>
      <stop offset="100%" stop-color="#e7a23f"/>
    </linearGradient>
    <radialGradient id="cloak-glow" cx="50%" cy="40%" r="62%">
      <stop offset="0%" stop-color="#fff6df" stop-opacity=".95"/>
      <stop offset="100%" stop-color="#fff6df" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="cloak-ball" cx="38%" cy="30%" r="78%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="40%" stop-color="#bfe9df"/>
      <stop offset="100%" stop-color="#2f8a78"/>
    </radialGradient>
    <linearGradient id="cloak-leaf" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#f5eedd"/>
      <stop offset="100%" stop-color="#d8c8a8"/>
    </linearGradient>
    <clipPath id="cloak-ballClip"><circle cx="512" cy="470" r="172"/></clipPath>
    <clipPath id="cloak-tileClip"><rect x="92" y="92" width="840" height="840" rx="200"/></clipPath>
  </defs>

  <!-- squircle tile (macOS-style) -->
  <rect x="92" y="92" width="840" height="840" rx="200" fill="url(#cloak-tile)"/>
  <g clip-path="url(#cloak-tileClip)">
    <rect x="92" y="92" width="840" height="840" fill="url(#cloak-glow)"/>

    <!-- door swung open on the left -->
    <g transform="rotate(-7 250 520)">
      <rect x="150" y="150" width="120" height="740" rx="26" fill="url(#cloak-leaf)"/>
      <circle cx="244" cy="540" r="13" fill="#cf9b2e"/>
    </g>

    <!-- disco ball: string, glow, body, facets, highlight -->
    <line x1="512" y1="120" x2="512" y2="300" stroke="#b5905a" stroke-width="9"/>
    <circle cx="512" cy="470" r="200" fill="#fff2cf" opacity=".5"/>
    <circle cx="512" cy="470" r="172" fill="url(#cloak-ball)"/>
    <g clip-path="url(#cloak-ballClip)" stroke="#2c7e6d" stroke-width="7" opacity=".5" fill="none">
      <line x1="392" y1="470" x2="632" y2="470"/>
      <path d="M360 388 q152 70 304 0"/>
      <path d="M360 552 q152 -70 304 0"/>
      <line x1="452" y1="306" x2="452" y2="634"/>
      <line x1="512" y1="298" x2="512" y2="642"/>
      <line x1="572" y1="306" x2="572" y2="634"/>
    </g>
    <ellipse cx="455" cy="408" rx="46" ry="30" fill="#ffffff" opacity=".92"/>

    <!-- twinkles -->
    <circle cx="300" cy="300" r="17" fill="#46c2a8"/>
    <circle cx="745" cy="300" r="14" fill="#e0795a"/>
    <circle cx="735" cy="690" r="18" fill="#f3c969"/>
    <circle cx="690" cy="780" r="12" fill="#7c6cf0"/>
    <circle cx="360" cy="760" r="13" fill="#e0795a"/>
  </g>
</svg>`;
function claimArt(index = 0) { return `<div class="art-stub">${CLAIM_ART.replaceAll("cloak-", `cloak-${index}-`)}<span class="stub-caption">ai guest list</span></div>`; }

// Terminal supervision is infrastructure, not a seat-health signal. Keep this decision pure so a
// missing rc block/wrapper can never be hidden by an otherwise healthy usage snapshot.
export function supervisionBanner(state) {
  if (state?.settings?.supervise_shell === false || !state?.supervision) return "";
  const supervision = state.supervision;
  // A failed read leaves the block unknown. Reinstalling cannot fix unreadable settings;
  // show the path/reason from the probe so we don't claim their existing setup is off.
  if (supervision.error) {
    return `<div class="supervision-banner supervision-banner--error" role="alert">
      <span>${esc(supervision.error)}</span>
    </div>`;
  }
  if (!supervision.active) {
    return `<div class="supervision-banner supervision-banner--error" role="alert">
      <span>terminal supervision is off — <span class="mono">codex/claude</span> won't auto-switch</span>
      <button data-action="supervision-on">turn it on</button>
    </div>`;
  }
  if (!supervision.on_path) {
    return `<div class="supervision-banner supervision-banner--info" role="status">
      open a new terminal to finish setup
    </div>`;
  }
  return "";
}

// --- add-a-seat sub-view (spec §9) — a pushed screen like settings, NOT a modal ----------------
// Four steps: provider → details → connecting → done. The provider accent (teal Codex / coral
// Claude) rides a single `--accent` CSS var on the root, so step markup never branches on tool.
// `add` is the transient flow state owned by app.mjs: {step, provider, name, method, token}.

// Sign-in methods per provider:
//   codex  — browser sign-in, OR paste an auth.json blob (a textarea the engine installs directly).
//   claude — browser sign-in ONLY. A `claude setup-token` is a long-lived token for the
//            CLAUDE_CODE_OAUTH_TOKEN env var; it does NOT write the Keychain login our snapshot
//            pipeline reads, so there is no working paste/Terminal no-browser path for Claude today.
const CODEX_AUTH_PATH = "~/.codex/auth.json";
const ADD_COPY = {
  codex: {
    row: "ChatGPT sign-in, Business seat",
    chip: "Codex CLI, ChatGPT sign-in or auth.json",
    tokenHint: `paste the contents of ${CODEX_AUTH_PATH} — handy for a headless or shared box.`,
    tokenPh: "paste auth.json contents",
  },
  claude: {
    row: "Claude.ai sign-in, Max or Pro seat",
    chip: "Claude Code, Claude.ai sign-in",
  },
};
const BROWSER_HINT = "i'll pop open the official sign-in — nothing leaves your Mac, i just save the seat.";

// Which providers offer a no-browser method (a method choice at all). Only codex, via auth.json.
function addHasMethods(provider) { return provider === "codex"; }
// A codex "token" is the only in-app paste; everything else is an official flow launched in a window.
// Exported so app.mjs shares the single definition instead of re-deriving the predicate.
export function addUsesPaste(add) { return add.method === "token" && add.provider === "codex"; }

// PURE reducer for a bridge reply → next UI state + effects. All the async-correlation logic that
// kept regressing (stale replies, poll ordering, save-in-flight, login-launch failure) lives here so
// it can be unit-tested without a DOM. app.mjs owns the effects (render/flash/celebrate/timer).
//   ui  = { screen, add, lastRev, state }   (add may be null; add is mutated in place and returned)
//   res = the bridge result object
// returns { screen, add, lastRev, state, render, flash, celebrate, closeFlow }
//   closeFlow: the add object whose "done" screen should auto-close after a delay, or null.
export function reduceReply(ui, res) {
  let { screen, add, lastRev, state } = ui;
  let render = false, flash = null, celebrate = false, closeFlow = null;

  // Native "open settings" — but never interrupt an in-flight save (its reply would land off-screen).
  if (res.settings_panel && !(add && add.pending)) screen = "settings";
  // Drop a stale snapshot: a poll that read older state then arrived after a newer mutation would
  // clobber the fresh view (the new seat would vanish). rev is monotonic across saves.
  if (res.state && (res.state.rev ?? 0) >= lastRev) { state = res.state; lastRev = res.state.rev ?? 0; }

  let addChanged = false;
  const inAdd = screen === "add" && add;
  // An add-op reply applies to the CURRENT flow only if it's for the same tool — a late reply from a
  // login the user abandoned (possibly for the other provider) must not steer the flow they restarted.
  // (res.tool may be absent on a generic failure; then fall back to "current flow".)
  const forThisFlow = inAdd && res.add_op && (res.tool == null || res.tool === add.provider);
  if (res.added && forThisFlow && add.pending) { // OUR paste/snapshot/import succeeded → celebrate then done
    add.pending = false; add.importing = false; add.step = "done";
    addChanged = true; closeFlow = add;        // caller schedules the auto-close, scoped to this flow
  }
  if (res.error && forThisFlow) {               // OUR add op failed (not an unrelated / other-tool one)
    if (add.pending) {                          // a save was in flight
      add.pending = false;
      // An IN-APP save (codex paste or one-tap import) → back to the form (auth.json preserved).
      // A browser save (tapped before login finished) → stay on connecting so they can complete
      // sign-in and tap "save my seat" again.
      if (addUsesPaste(add) || add.importing) add.step = "details";
      add.importing = false;
      addChanged = true;
    } else if (add.step === "connecting") {     // the login LAUNCH failed (window never opened)
      add.step = "details"; addChanged = true;  // back to the form to retry
    }
  }

  if (screen === "add") render = addChanged;    // else swallow the poll, keep the DOM (and focus)
  else if (res.state || res.settings_panel) render = true;

  // Toast a user-action error, but not a background poll blip, nor an add-op error that isn't for the
  // current flow (a stale/other-tool one would pop over main/settings or the wrong add out of nowhere).
  if (res.error && !res.background && (!res.add_op || forThisFlow)) flash = res.error;
  else if (res.message && !res.background) flash = res.message;
  if (res.celebrate) celebrate = true;

  return { screen, add, lastRev, state, render, flash, celebrate, closeFlow };
}

function addProviderStep() {
  const row = (tool) => `<button class="add-prov" data-action="add-provider" data-tool="${tool}"
      style="--accent:${TOOL_META[tool].accent}">
      <span class="add-chip"><span class="add-chip-dot"></span></span>
      <span class="add-prov-tx"><span class="add-prov-name">${TOOL_META[tool].label}</span>
        <span class="add-prov-sub">${ADD_COPY[tool].row}</span></span>
      <span class="add-chev">›</span></button>`;
  return `<section class="set-sec">
    <span class="set-label">who's joining the list?</span>
    <div class="set-card">${row("codex")}${row("claude")}</div>
    <div class="add-foot">nothing leaves your Mac — i just save the seat's credentials so you can hop between them.</div>
    <button class="add-row" data-action="key-start">＋ bring an api key instead</button>
  </section>`;
}

function addDetailsStep(add, state) {
  const c = ADD_COPY[add.provider];
  const paste = addUsesPaste(add);
  const cta = paste ? "save the seat" : "open sign-in";
  // One-tap "use the login you already have": when codex is signed in on this Mac to an account that
  // isn't a seat yet, offer to import it directly — the easiest path, no browser dance, no paste.
  const liveEmail = add.provider === "codex" ? state?.codex_live_unregistered?.email : null;
  const importCard = liveEmail
    ? `<section class="set-sec">
        <span class="set-label">already signed in on this Mac</span>
        <div class="set-card">
          <button class="add-import" data-action="add-import">
            <span class="add-chip add-chip--sm"><span class="add-chip-dot"></span></span>
            <span class="add-prov-tx"><span class="add-provcard-t">use ${esc(liveEmail)}</span>
              <span class="add-provcard-s">one tap — no auth.json needed</span></span>
            <span class="add-chev">＋</span></button>
        </div>
        <div class="add-hint">or add a different account below.</div>
      </section>`
    : "";
  // The method chooser shows only for a provider that has a no-browser option (codex). Claude is
  // browser-only, so it renders name + a single "open sign-in" CTA with no segmented control.
  let methodSection = "";
  if (addHasMethods(add.provider)) {
    const seg = (v, label) =>
      `<button class="sopt ${add.method === v ? "on" : ""}" data-action="add-method" data-value="${v}">${label}</button>`;
    const hint = add.method === "token" ? c.tokenHint : BROWSER_HINT;
    // paste flow: show WHERE the file lives + a Finder shortcut, so the user isn't left guessing.
    const tokenWrap = paste
      ? `<div class="add-tokenwrap"><textarea id="add-token" class="add-token mono"
           placeholder="${esc(c.tokenPh)}">${esc(add.token)}</textarea></div>
         <div class="add-pathrow"><code class="add-path">${esc(CODEX_AUTH_PATH)}</code>
           <button class="add-reveal" data-action="add-reveal">reveal in Finder</button></div>`
      : "";
    methodSection = `<section class="set-sec">
      <span class="set-label">how should i sign you in?</span>
      <div class="set-card">
        <div class="add-method">
          <div class="set-seg">${seg("browser", "open browser")}${seg("token", "paste auth.json")}</div>
          <div class="add-hint">${hint}</div>
        </div>
        ${tokenWrap}
      </div>
    </section>`;
  }
  return `<div class="add-provcard">
      <span class="add-chip add-chip--sm"><span class="add-chip-dot"></span></span>
      <span class="add-prov-tx"><span class="add-provcard-t">new ${TOOL_META[add.provider].label} seat</span>
        <span class="add-provcard-s">${c.chip}</span></span>
      <button class="add-change" data-action="add-change">change</button>
    </div>
    ${importCard}
    <section class="set-sec">
      <span class="set-label">name this seat</span>
      <div class="set-card">
        <input id="add-name" class="add-input" placeholder="Work, Personal, Late-night" value="${esc(add.name)}">
      </div>
    </section>
    ${methodSection}
    <button class="add-cta" data-action="add-cta">${cta}</button>`;
}

function addConnectingStep(add) {
  // A browser sign-in first WAITS for the user to finish in the browser and tap "save my seat"; only
  // then (add.pending) is a snapshot in flight. A codex auth.json paste is always actively saving.
  // Spinner + "saving…" copy show only when something is really in
  // flight — a lone spinner while we wait on the user would read as "hung".
  const terminalFlow = !addUsesPaste(add) && !add.importing;  // a browser sign-in waits on the user
  const saving = !terminalFlow || add.pending;
  const title = saving ? "saving your seat…" : "we opened your browser…";
  const sub = saving ? "tucking it away safely 💛" : "say hi over there and you're on the list 💛";
  const spin = saving ? `<div class="add-spin"></div>` : "";
  // The "save my seat" handshake is only for the browser flow; a paste/import is already saving.
  const cta = terminalFlow
    ? `<button class="add-cta" data-action="add-save"${add.pending ? " disabled" : ""}>save my seat 💛</button>`
    : "";
  return `<div class="add-center">${spin}
    <div class="add-h">${title}</div><div class="add-sub">${sub}</div>${cta}</div>`;
}

function addDoneStep(add) {
  return `<div class="add-center add-center--done"><div class="add-heart">💛</div>
    <div class="add-welcome">welcome, ${esc(add.name.trim() || "new seat")}</div>
    <div class="add-sub">your seat's saved — i'll keep it warm</div></div>`;
}

// A pushed sub-view (§9): renders into #root in place of the popover. Reuses the settings chrome
// (.set-app / .set-head / .set-body) so header + scroll metrics match exactly.
export function buildAddSeat(state, add) {
  const theme = state?.settings?.theme === "dark" ? "dark" : "light";
  const step = add?.step || "provider";
  const accent = add?.provider ? ` style="--accent:${TOOL_META[add.provider].accent}"` : "";
  const cancel = step === "provider" || step === "details"
    ? `<button class="add-cancel" data-action="add-cancel">cancel</button>` : "";
  const body = step === "details" ? addDetailsStep(add, state)
    : step === "connecting" ? addConnectingStep(add)
    : step === "done" ? addDoneStep(add)
    : addProviderStep();
  return `<div class="app set-app add-app theme-${theme}"${accent}>
    <header class="set-head">
      <button class="set-back" data-action="add-back" title="back">‹</button>
      <span class="set-title">add a seat</span>
      ${cancel}
    </header>
    <div class="set-body">${body}</div>
  </div>`;
}

// Settings sub-view building blocks (spec §9.1): grouped iOS-style cards, every row a subtitle,
// segmented controls full-width on their own line. Friendly labels are display-only — data-value
// carries the real persisted value the bridge validates.
const STRATEGY_OPTS = [
  { v: "most_headroom", label: "most headroom" },
  { v: "soonest_back", label: "soonest back" },
];
const THEME_OPTS = [{ v: "light", label: "light" }, { v: "dark", label: "dark" }];

function strategyHint(strat) {
  return strat === "most_headroom" ? "pick the seat with room left" : "wait for the first seat to return";
}
function toggleRow(key,title,subtitle,on) {
  return `<label class="set-toggle-row"><span class="set-tx"><span class="set-t">${title}</span><span class="set-s">${subtitle}</span></span>
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}><span class="toggle-state" aria-hidden="true"><span class="when-on">on</span><span class="when-off">off</span></span><span class="sw" aria-hidden="true"></span></label>`;
}
function segBlock(label,hint,action,current,options) {
  return `<div class="set-seg-row"><span class="set-t">${label}</span>${hint ? `<span class="set-s">${hint}</span>` : ""}<div class="set-seg">${options.map(o=>`<button class="sopt ${current===o.v ? "on" : ""}" data-action="${action}" data-value="${o.v}" aria-pressed="${current===o.v}">${o.label}</button>`).join("")}</div></div>`;
}
export function buildSettings(state) {
  const s=state?.settings || {}, theme=s.theme === "dark" ? "dark" : "light";
  const strat=s.strategy === "most_headroom" ? "most_headroom" : "soonest_back";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="settings-back" aria-label="back">‹</button><span class="set-title">settings</span><button data-action="settings-back">done</button></header>
    <div class="set-body">
      <section class="set-sec settings-sheet"><h2 class="set-label">switching</h2>
      ${toggleRow("auto_switch","auto-switch","move when a seat runs out",s.auto_switch)}
      ${segBlock("choose the next seat",strategyHint(strat),"set_strategy",strat,STRATEGY_OPTS)}
      ${toggleRow("supervise_shell","supervise terminals","let codex / claude switch seats",s.supervise_shell !== false)}
      ${toggleRow("same_tool_only","stay on the same tool","Codex to Codex; Claude to Claude",s.same_tool_only)}
      ${toggleRow("notify","notify when switched","show the seat you're using",s.notify)}
      ${toggleRow("restart_app","restart the Codex app","use the new seat in the app",s.restart_app)}
      <p class="support">with supervision off, only cx / cl auto-switch. terminals switch without restarting.</p></section>
      <section class="set-sec paid-settings"><h2 class="set-label">paid keys</h2>
      ${toggleRow("key_fallback","allow paid key use","off stops paid sessions too",s.key_fallback===true)}
      ${toggleRow("confirm_key_switch","ask before using","approve each paid switch",s.confirm_key_switch!==false)}
      <p class="support">paid use permits metered terminals and automatic fallback. sent turns may still bill after stopping. turning asking off lets eligible keys spend without another approval. this app does not cap spend.</p></section>
      <section class="set-sec settings-sheet"><h2 class="set-label">appearance</h2>${segBlock("theme","","set_theme",theme,THEME_OPTS)}
      <div class="set-legend"><p>the menu bar door</p><div>${doorMark({door:"open"})}<span>a model is ready</span></div><div>${doorMark({door:"shut"})}<span>every seat is resting</span></div></div></section>
      <p class="set-ver">ai guest list ${state?.app ? `v${esc(state.app.version)}` : ""}</p>
    </div></div>`;
}

// --- popover ----------------------------------------------------------------------------------

function verdict(state) {
  const seats=["codex","claude"].flatMap(tool=>(state?.tools?.[tool]?.seats || []).map(s=>({...s,tool})));
  const ready=seats.filter(s=>["ready","active"].includes(s.status));
  const active=ready.find(s=>s.status==="active" && s.in_session) || ready.find(s=>s.status==="active") || ready[0];
  const key=(state?.keys || [])[0];
  let title,sub="",action="";
  if (active) {
    title=`${esc(active.name)} ${active.in_session ? "is on the floor" : "is ready"}`;
    const left=creditLeft(active);
    sub=`you can keep going${left===null ? "; usage unknown" : ` with ${left}% left${active.usage_stale ? " (last reading)" : ""}`}`;
  } else if (!seats.length && !key) {
    title="your first seat is waiting"; sub="add a subscription or an API key with ＋";
    // The single global add control remains in the header.
  } else {
    const login=seats.find(needsHello);
    title=seats.length && seats.every(s=>s.status==="resting" || s.status==="queued") ? "everyone’s resting" : "no seat is ready";
    if (key) { sub=`${esc(key.label)} can use a paid key; this app does not cap spend.`;
      action=`<button class="primary" data-action="key-terminal" data-id="${esc(key.id)}">use ${esc(key.label)}</button>`;
    } else if(login) {sub="sign in to get back to work";action=`<button class="primary" data-action="add" data-tool="${login.tool}">sign in to ${TOOL_META[login.tool].label}</button>`;}
    else sub="your seats return at the times below";
  }
  return `<section class="claim-ticket verdict${!active && seats.length ? " verdict--blocked" : ""}" role="status"><div class="claim-copy"><h1>${title}</h1><p>${sub}</p>${action}</div>${claimArt()}</section>`;
}
export function buildHTML(state) {
  const theme=state?.settings?.theme === "dark" ? "dark" : "light";
  const pending=(state?.pending_key_switches || []).some(r=>r.status==="pending" && Date.parse(r.expires_at)>Date.now());
  const paid=paidUseControl(state);
  const running=new Set([...(state?.running_key_seats || []),...(state?.pinned_sessions || []).map(s=>s.key_seat?.id || s.email)]);
  const keys=(state?.keys || []).filter(k=>!running.has(k.id) && !running.has(`key:${k.id}`));
  return `<div class="app theme-${theme}"><header class="top"><span class="brand">ai guest list<span class="brand-note">the cloakroom</span></span>
    <span class="top-actions"><button class="ibtn" data-action="settings" aria-label="settings" title="settings">⋯</button><details class="add-menu"><summary class="ibtn" aria-label="add a seat or API key" title="add a seat or API key">＋</summary><div class="menu-panel"><button data-action="add">add a subscription seat</button><button data-action="key-start">add an API key</button></div></details></span></header>
    <div class="priority${pending ? " priority--asking" : ""}${paid ? " priority--paid" : ""}">${paid}${keyConfirmations(state)}${!paid && !pending ? verdict(state) : ""}</div>
    <div class="main-body">${toolGroup("codex",state?.tools?.codex,keys.filter(k=>k.harness==="codex"))}${toolGroup("claude",state?.tools?.claude,keys.filter(k=>k.harness==="claude"))}
      ${supervisionBanner(state)}${state?.moved_note ? `<section class="event"><h2>last switch</h2><p>${esc(state.moved_note).replaceAll(" · ","; ")}</p></section>` : ""}
      <footer class="foot"><span>your seats, kept together</span><button class="link" data-action="quit">quit</button></footer>
    </div></div>`;
}
function paidUseControl(state) {
  const pinned=state?.pinned_sessions || [], running=state?.running_key_seats || [];
  if(!pinned.length && !running.length) return "";
  const stopping=state?.settings?.key_fallback===false;
  const others=running.filter(id=>!pinned.some(s=>(s.key_seat?.id || s.email)===id || s.email===`key:${id}`));
  return `<section class="paid-use-control claim-ticket" role="status"><div class="paid-heading"><h1>${stopping ? "paid use is stopping…" : "a paid key is running"}</h1><span class="paid-stamp" aria-hidden="true">paid</span></div>
    <div class="paid-session-list">${pinnedSessions(state)}${others.map(id=>{
      const seat=state?.keys?.find(k=>k.id===id);
      return `<div class="paid-session"><strong>${esc(seat?.label || id)}</strong><span>${esc(seat?.harness || "")} automatic fallback</span><span class="key-model">${esc(seat?.model || "")}</span><button data-action="key-stop"${stopping ? " disabled" : ""}>stop paid use</button></div>`;
    }).join("")}</div><button class="stop-all" data-action="key-stop"${stopping ? " disabled" : ""}>${stopping ? "stopping…" : "stop all paid use"}</button><p class="support">stops sessions and new requests. sent turns may still bill.</p></section>`;
}
export function pinnedSessions(state) {
  return (state?.pinned_sessions || []).map(s=>{
    const seat=s.key_seat || {};
    return `<div class="paid-session"><strong>${esc(seat.label || s.email)}</strong><span>${esc(providerName(seat))}, ${esc(s.tool)}</span><span>terminal ${esc(s.pid)}</span><span class="key-model">${esc(seat.model)}</span>
      <button class="session-end" data-action="end-pinned-session" data-tool="${esc(s.tool)}" data-pin="${esc(s.pin)}"${s.end_requested ? " disabled" : ""}>${s.end_requested ? "ending…" : "end session"}</button></div>`;
  }).join("");
}

// Key providers mirror providers.py's harness gate: chat-only catalogs are not usable seats.
// No price table lives here. Every amount comes from a bridge price envelope.
export const KEY_PROVIDERS = {
  openai: { name: "openai", harness: "codex", pricing: "https://openai.com/api/pricing/" },
  anthropic: { name: "anthropic", harness: "claude", pricing: "https://www.anthropic.com/pricing" },
  openrouter: { name: "openrouter", harness: "codex", priced: true, pricing: "https://openrouter.ai/models" },
  langdock: { name: "langdock, openai models", harness: "codex", regional: true, pricing: "https://www.langdock.com/pricing" },
  langdock_anthropic: { name: "langdock, claude models", harness: "claude", regional: true, pricing: "https://www.langdock.com/pricing" },
  deepseek: { name: "deepseek", harness: "codex", pricing: "https://api-docs.deepseek.com/quick_start/pricing" },
  xai: { name: "xai", harness: "codex", priced: true, pricing: "https://docs.x.ai/docs/models" },
  groq: { name: "groq", harness: "codex", unverified: true, pricing: "https://groq.com/pricing" },
  openai_compatible: { name: "openai-compatible", harness: "codex", unverified: true },
};

function providerName(seat) {
  return `${KEY_PROVIDERS[seat.provider]?.name || seat.provider || "key"}${seat.region ? `, ${seat.region}` : ""}`;
}

// Reject missing/blank/boolean values before numeric coercion: Number(null) is NOT a free model.
// Preserve decimal strings until formatting, including tiny nonzero rates.
function amount(value) {
  if (!["string", "number"].includes(typeof value) || String(value).trim() === "") return null;
  return Number.isFinite(Number(value)) && Number(value) >= 0 ? String(value) : null;
}
export function formatPrice(value) {
  if (amount(value) === null) return "price unavailable";
  const number = Number(value);
  return `$${number.toLocaleString("en-US", { useGrouping: false,
    ...(number >= .01 ? { minimumFractionDigits: 2, maximumFractionDigits: 4 } : { maximumSignificantDigits: 3 }) })}`;
}
function priceRate(price, name, seen = []) {
  if (price?.source !== "live" || price.currency !== "USD" || price.token_unit !== "per_million_tokens") return null;
  const rate = price.rates?.[name];
  if (seen.includes(name)) return null;
  if (rate?.status === "same_as") return priceRate(price, rate.same_as, [...seen, name]);
  return rate?.status === "known" ? amount(rate.value) : null;
}
function priceAge(price, now = Date.now()) {
  const fetched = price?.verified_at;
  const age = fetched && Number.isFinite(Date.parse(fetched))
    ? fmtUsageAge(fetched, now) : amount(price?.age_seconds) !== null
      ? `fetched ${Math.floor(Number(price.age_seconds) / 60)}m ago` : "fetch age unavailable";
  return `live price estimate; ${age}${price?.potentially_stale ? "; over 24h old" : ""}`;
}
function priceText(price) {
  const input = priceRate(price, "input"), output = priceRate(price, "output");
  if (input === null && output === null) return "price unavailable";
  return `input ${formatPrice(input)}${input === null ? "" : "/Mtok"}; output ${formatPrice(output)}${output === null ? "" : "/Mtok"}`;
}
function priceHTML(price) {
  const text = priceText(price);
  return `<div class="key-price mono">${esc(text)}</div>${text === "price unavailable" ? "" : `<div class="add-hint">${esc(priceAge(price))}</div>`}`;
}

export function keyProofStatus(seat) {
  if (seat.harness === "claude") return "";
  const proof = seat.last_proof;
  let text = "";
  if (proof?.outcome === "proven") text = "proven — a Responses turn completed";
  else if (proof?.outcome === "incompatible") text = "incompatible — this endpoint does not support Responses; this seat will not work";
  else if (proof?.outcome === "refused") {
    const reason = { invalid_key: "authentication", insufficient_quota: "quota", rate_limited: "rate limit" }[proof.error] || "access or billing";
    text = `refused — provider rejected the request (${reason}); Responses support is undetermined`;
  } else if (proof?.outcome === "inconclusive") {
    text = `inconclusive — ${proof.error === "timeout" ? "check timed out" : "no completed turn or definitive provider reply"}; Responses support is undetermined`;
  }
  return `${text ? `<div class="key-proof" role="status">${text}</div><div class="add-hint">checked ${esc(proof.checked_at)}; ${esc(proof.model)}</div>` : ""}
    ${seat.responses_verified === false && proof?.outcome !== "incompatible" && proof?.outcome !== "proven" ? `<div class="key-unproven">responses support unproven — this endpoint may not work</div>` : ""}`;
}

export function keySeatCard(seat) {
  const unproven=(seat.responses_verified===false || seat.last_proof?.outcome==="incompatible") && seat.harness!=="claude";
  const validation=seat.last_validation;
  const detailId=`key-${encodeURIComponent(seat.id || "")}`;
  return `<article class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}"><span class="tag-stub" aria-hidden="true"><span class="punch"></span><span class="tag-number">k</span></span>
    <button type="button" class="seat-row" aria-controls="${esc(detailId)}"><span class="seat-name">${esc(seat.label)}</span><span class="chevron" aria-hidden="true">⌄</span><span class="seat-status">API key</span><span class="headroom">paid per token</span><span class="sr-only disclosure-show">show details</span><span class="sr-only disclosure-hide">hide details</span></button>
      <div class="expand" id="${esc(detailId)}"><p>${esc(providerName(seat))}</p><p class="key-model">${esc(seat.model)}</p><p class="support">this app does not cap spend.</p>
      <button data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button>${keyProofStatus(seat)}
      ${validation ? `<p class="key-check-status" role="status">${validation.operation_permitted ? "key check passed; account access confirmed" : "key check wasn't permitted; check access with your provider"}</p>` : ""}
      ${unproven ? `<div class="proof-gate"><p class="support">checking this endpoint sends a paid request. cost unknown.</p><button data-action="key-prove" data-id="${esc(seat.id)}">send paid endpoint check</button></div>` : ""}
      <div class="seat-detail-actions"><button data-action="key-validate" data-id="${esc(seat.id)}">check key</button><button data-action="key-remove" data-id="${esc(seat.id)}">remove key</button></div></div></article>`;
}
function priceSentence(price,provider) {
  const input=priceRate(price,"input"),output=priceRate(price,"output");
  if(input===null && output===null) return `<p class="price-note">${esc(KEY_PROVIDERS[provider]?.name || provider || "this provider")} has no prices in this catalog. ${KEY_PROVIDERS[provider]?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(provider)}">check provider pricing</button>` : "check prices with your provider."}</p>`;
  return `<p class="price-note">input ${esc(formatPrice(input))} / output ${esc(formatPrice(output))}<br>per million tokens (USD)</p>`;
}
function consentExpiry(iso, now) {
  const seconds = Math.max(0, Math.ceil((Date.parse(iso) - now) / 1000));
  if (!Number.isFinite(seconds) || seconds === 0) return "request expired";
  return `expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
export function keyConfirmations(state,answering=new Set(),now=Date.now()) {
  const requests=(state?.pending_key_switches || []).filter(r=>r.status==="pending" && Date.parse(r.expires_at)>now);
  return `<div class="key-prompts" aria-live="polite">${requests.map((r,index)=>{
    const disabled=answering.has(r.id) ? " disabled" : "";
    return `<section class="key-confirm claim-ticket" aria-label="paid key confirmation"><div class="decision-top"><div><h1>${r.pinned ? "use a paid key in this terminal?" : "keep going with a paid key?"}</h1></div>${claimArt(index+1)}</div>
      <div class="decision-facts"><p class="decision-seat">${esc(r.key_seat?.label)} <span>${esc(providerName(r.key_seat || {}))}</span></p><p class="k-model">${esc(r.key_seat?.model)}</p>
      ${priceSentence(r.price,r.key_seat?.provider)}<p class="support">${r.pinned ? "this terminal uses the key." : "your subscription seat is resting."} this app does not cap spend.</p>
      <p class="expiry" aria-live="off" data-expires-at="${esc(r.expires_at)}">${consentExpiry(r.expires_at,now)}</p></div>
      <div class="k-acts"><button class="k-no" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${disabled}>${disabled ? "answering…" : "not now"}</button><button class="k-go" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${disabled}>${disabled ? "answering…" : "use the key"}</button></div></section>`;
  }).join("")}</div>`;
}

export function buildPaidKeyGate(state, flow) {
  const seat = state.keys?.find((key) => key.id === flow.id);
  const theme = state.settings?.theme === "dark" ? "dark" : "light";
  const disabled = flow.pending ? " disabled" : "";
  return `<div class="app set-app add-app theme-${theme}" style="--accent:${TOOL_META[seat?.harness || "codex"].accent}">
    <header class="set-head"><button class="set-back" data-action="paid-key-back" title="back"${disabled}>‹</button><span class="set-title">allow paid key use</span></header>
    <div class="set-body"><section class="set-sec"><span class="set-label">paid use is off</span>
      <div class="set-card add-method"><span class="set-t">use ${esc(seat?.label || flow.label)} in a new terminal?</span>
        <div class="add-hint">real money can be spent. this turns on paid key use for pinned terminals and automatic fallback when subscription seats rest.</div>
        <div class="add-hint">turning paid use off stops a running paid session; the turn already sent may still bill.</div></div>
      ${flow.error ? `<div class="usage-error" role="alert">${esc(flow.error)}</div>` : ""}
      <div class="k-acts"><button class="k-go" data-action="paid-key-enable"${disabled}>${flow.pending ? "opening your terminal…" : "allow paid use and open terminal"}</button>
      <button class="k-no" data-action="paid-key-back"${disabled}>not now</button></div>
    </section></div></div>`;
}

// Render only these results on input: the filter field itself keeps focus and its caret.
function isFree(model) { return priceRate(model.price,"input")!==null && priceRate(model.price,"output")!==null && Number(priceRate(model.price,"input"))===0 && Number(priceRate(model.price,"output"))===0; }
function contextText(value) {
  const n=Number(value);
  if(!Number.isFinite(n) || n<=0) return "";
  return `${n>=1e6 ? `${Number((n/1e6).toFixed(2))}M` : n>=1e3 ? `${Number((n/1e3).toFixed(1))}k` : n} context`;
}
function decimalRate(value) {
  if(value===null) return `<span>unknown</span>`;
  const parts=formatPrice(value).slice(1).split(".");
  return `<span class="decimal"><span>$${parts[0]}</span><span>.${parts[1] || "00"}</span></span>`;
}
export function buildModelResults(flow) {
  const catalog=flow.catalog || {}, priced=catalog.sort_key==="input_usd_per_million_tokens";
  const models=[...(catalog.models || [])].sort((a,b)=>{
    if(!priced) return a.id.localeCompare(b.id);
    const av=priceRate(a.price,"input"),bv=priceRate(b.price,"input");
    return (av===null ? Infinity : Number(av))-(bv===null ? Infinity : Number(bv));
  });
  const query=(flow.modelFilter || "").toLowerCase();
  const visible=models.filter(m=>m.id.toLowerCase().includes(query) || (m.display_name || "").toLowerCase().includes(query));
  const paid=visible.filter(m=>!isFree(m));
  return `<p class="model-count" role="status"><span class="free-hidden">${paid.length} matches; ${visible.length-paid.length} free hidden</span><span class="free-shown">${visible.length} matches, including free</span></p>
    <div class="model-columns"><span>model / context</span>${priced ? "<span>input / output<br>USD per million</span>" : ""}</div>
    <div class="model-list${!priced ? " model-list--unpriced" : ""}">${visible.map(m=>`<button class="key-model-option${isFree(m) ? " is-free" : ""}" data-action="key-model" data-model="${esc(m.id)}"><span class="model-identity"><span class="model-id">${esc(m.id)}</span><span class="model-context">${esc(contextText(m.context_window))}</span></span>${priced ? `<span class="model-rates">${isFree(m) ? "free" : `${decimalRate(priceRate(m.price,"input"))}${decimalRate(priceRate(m.price,"output"))}`}</span>` : ""}</button>`).join("")}
    ${!visible.length ? `<p class="empty">${query ? `no models match “${esc(flow.modelFilter)}”. try another name.` : "no models returned; go back and check this key and endpoint."}</p>` : !paid.length ? `<p class="empty free-hidden">only free models match; show free models to see them.</p>` : ""}</div>`;
}
export function buildModelPicker(flow) {
  const catalog=flow.catalog || {}, provider=KEY_PROVIDERS[flow.provider], priced=catalog.sort_key==="input_usd_per_million_tokens";
  return `<section class="model-picker"><div class="search-region"><label class="set-label" for="key-model-filter">find your model</label><input class="add-input" id="key-model-filter" type="search" autofocus placeholder="search by name or model id" autocomplete="off" spellcheck="false" value="${esc(flow.modelFilter || "")}">
    <label class="free-control"><input id="show-free-models" type="checkbox"><span class="free-hidden">free models hidden <b>show</b></span><span class="free-shown">free models shown <b>hide</b></span></label>
    <p class="support">${priced ? "live price estimates, lowest input first" : "this catalog has no prices; sorted by model id"}${catalog.source==="cache" ? "; cached catalog" : ""}</p><p class="support">${esc(fmtUsageAge(catalog.fetched_at))}${catalog.potentially_stale ? "; over 24h old" : ""}</p>
    ${catalog.error ? `<p class="usage-error" role="status">refresh failed; showing the last reading</p>` : ""}</div>
    <div id="key-model-results">${buildModelResults(flow)}</div><footer class="model-footer">${provider?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(flow.provider)}">provider pricing and budget controls</button>` : `<p>check prices with your endpoint's operator.</p>`}<p>prices are estimates. this app does not cap spend.</p></footer></section>`;
}

export function keyRequest(flow) {
  return { provider: flow.provider, secret: flow.secret.trim(), allow_unverified: flow.allow_unverified === true,
    ...(KEY_PROVIDERS[flow.provider]?.regional ? { region: flow.region } : {}),
    ...(flow.provider === "openai_compatible" ? { base_url: flow.base_url.trim() } : {}) };
}

// Native echoes only our request id/action, never the secret. Polls and abandoned-flow replies
// cannot advance a later flow, even when both use the same provider.
export function reduceKeyReply(flow, res) {
  if (!flow?.pending || flow.pending !== res.key_request_id) return false;
  flow.pending = null;
  if (res.key_action === "models_list") {
    if (res.ok && Array.isArray(res.models)) { flow.catalog = res; flow.step = "models"; delete flow.modelFilter; }
    else { flow.step = "details"; flow.error = res.error || "couldn't load models — try again"; }
  } else if (res.key_action === "key_add") {
    if (res.added) { flow.step = "done"; flow.secret = ""; flow.savedSeat = res.seat; }
    else { flow.step = "review"; flow.error = res.error || "couldn't save this key — try again"; }
  }
  return true;
}

export function buildAddKey(state, flow) {
  const theme = state?.settings?.theme === "dark" ? "dark" : "light";
  const provider = KEY_PROVIDERS[flow.provider];
  const accent = TOOL_META[provider?.harness || "codex"].accent;
  let body;
  if (flow.step === "provider") {
    body = `<section class="set-sec"><span class="set-label">who's bringing a key?</span><div class="set-card">${Object.entries(KEY_PROVIDERS).map(([id, p]) =>
      `<button class="add-prov" data-action="key-provider" data-provider="${id}"><span class="add-chip"><span class="add-chip-dot"></span></span>
      <span class="add-prov-tx"><span class="add-prov-name">${p.name}</span><span class="add-prov-sub">${p.harness === "claude" ? "claude code, messages" : "codex cli, responses"}${p.unverified ? "; unproven" : ""}</span></span><span class="add-chev">›</span></button>`).join("")}</div>
      <div class="add-foot">the same langdock key works for both routes: openai models through codex, claude models through claude code.</div>
      <div class="add-foot">openrouter offers live model prices across providers. direct openai, anthropic and langdock catalogs don't publish prices.</div></section>`;
  } else if (flow.step === "details") {
    body = `<div class="add-provcard"><span class="add-prov-tx"><span class="add-provcard-t">new ${esc(provider.name)} key seat</span><span class="add-provcard-s">${provider.harness === "claude" ? "claude code" : "codex cli"} sessions</span></span><button class="add-change" data-action="key-back">change</button></div>
      ${provider.regional ? `<section class="set-sec"><label class="set-label" for="key-region">region</label><div class="set-card"><select class="add-input" id="key-region">${["eu", "us", "global"].map((r) => `<option value="${r}"${flow.region === r ? " selected" : ""}>${r}</option>`).join("")}</select></div></section>` : ""}
      ${flow.provider === "openai_compatible" ? `<section class="set-sec"><label class="set-label" for="key-base-url">your endpoint's base url</label><div class="set-card"><input class="add-input" id="key-base-url" type="url" placeholder="https://your-host/v1" value="${esc(flow.base_url)}"></div><div class="add-hint">must support responses; chat completions alone won't work.</div></section>` : ""}
      <section class="set-sec"><label class="set-label" for="key-label">name this seat</label><div class="set-card"><input class="add-input" id="key-label" placeholder="work, late-night" value="${esc(flow.label)}"></div></section>
      <section class="set-sec"><label class="set-label" for="key-secret">paste your api key</label><div class="set-card"><input class="add-input mono" id="key-secret" type="password" autocomplete="off" spellcheck="false" value="${esc(flow.secret)}"></div><div class="add-hint">sent only to the provider you chose; saved in your Mac's keychain.</div></section>
      ${provider.unverified ? `<section class="set-sec"><span class="set-t">this endpoint is unproven</span><div class="add-hint">we cannot promise its responses endpoint works with codex, even if it lists models. requests may still cost money.</div><label class="key-ack"><input id="key-ack" type="checkbox"${flow.allow_unverified ? " checked" : ""}> i understand it may not work, and want to try this endpoint</label></section>` : ""}
      <button class="add-cta" data-action="key-discover">pick a model</button>`;
  } else if (flow.step === "connecting") {
    body = `<div class="add-center"><div class="add-spin"></div><div class="add-h">${flow.operation === "key_add" ? "saving your key seat…" : "looking up models…"}</div><div class="add-sub">${flow.operation === "key_add" ? "tucking the key away safely 💛" : "asking only the provider you chose"}</div></div>`;
  } else if (flow.step === "models") {
    body = buildModelPicker(flow);
  } else if (flow.step === "review") {
    const model = flow.catalog?.models?.find((m) => m.id === flow.model);
    body = `<section class="set-sec"><span class="set-label">a seat for ${esc(flow.label)}</span><div class="set-card add-method"><span class="set-t">${esc(providerName(flow))}</span><div class="key-model mono">${esc(flow.model)}</div>${priceHTML(model?.price)}</div>
      <div class="add-foot">saving a key does not start a paid session. choose “use in new terminal” when you're ready.</div>
      <div class="add-foot">use your provider's budget controls for limits — this app does not cap spend.</div></section><button class="add-cta" data-action="key-save">save the key seat</button>`;
  } else {
    body = `<div class="add-center add-center--done"><div class="add-heart">💛</div><div class="add-welcome">welcome, ${esc(flow.label)}</div><div class="add-sub">your key seat's saved</div>
      ${flow.savedSeat?.last_validation?.operation_permitted === false ? `<div class="usage-error">the key check wasn't permitted. check access with your provider before using it.</div>` : ""}
      <button class="add-cta" data-action="key-cancel">back to the guest list</button></div>`;
  }
  return `<div class="app set-app add-app theme-${theme}" style="--accent:${accent}"><header class="set-head">
    <button class="set-back" data-action="key-back" title="back"${flow.pending ? " disabled" : ""}>‹</button><span class="set-title">add a key</span>
    ${flow.pending ? "" : `<button class="add-cancel" data-action="key-cancel">cancel</button>`}</header>
    ${keyConfirmations(state)}<div class="set-body${flow.step === "models" ? " picker-body" : ""}">${flow.error ? `<div class="usage-error" role="alert">${esc(flow.error)}</div>` : ""}${body}</div></div>`;
}
