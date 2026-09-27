// v7-programme: a typeset programme. Forked from app/web/render.mjs.
// Bridge contracts and state reducers are retained; presentation is local and network-free.

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
  if (minutes < 60) return ` · ${minutes}m`;
  if (minutes < 1440) return ` · ${Math.floor(minutes / 60)}h`;
  return ` · ${Math.floor(minutes / 1440)}d`;
}

// Clock ticks change text only: leave focused buttons, expanded cards, and scroll position intact.
export function updateClockText(root, now = Date.now()) {
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

const PROGRAMME_ART = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="92 92 840 840" class="programme-art" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="programme-tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffe6ad"/>
      <stop offset="55%" stop-color="#f7c668"/>
      <stop offset="100%" stop-color="#e7a23f"/>
    </linearGradient>
    <radialGradient id="programme-glow" cx="50%" cy="40%" r="62%">
      <stop offset="0%" stop-color="#fff6df" stop-opacity=".95"/>
      <stop offset="100%" stop-color="#fff6df" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="programme-ball" cx="38%" cy="30%" r="78%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="40%" stop-color="#bfe9df"/>
      <stop offset="100%" stop-color="#2f8a78"/>
    </radialGradient>
    <linearGradient id="programme-leaf" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#f5eedd"/>
      <stop offset="100%" stop-color="#d8c8a8"/>
    </linearGradient>
    <clipPath id="programme-ballClip"><circle cx="512" cy="470" r="172"/></clipPath>
    <clipPath id="programme-tileClip"><rect x="92" y="92" width="840" height="840" rx="200"/></clipPath>
  </defs>

  <!-- squircle tile (macOS-style) -->
  <rect x="92" y="92" width="840" height="840" rx="200" fill="url(#programme-tile)"/>
  <g clip-path="url(#programme-tileClip)">
    <rect x="92" y="92" width="840" height="840" fill="url(#programme-glow)"/>

    <!-- door swung open on the left -->
    <g transform="rotate(-7 250 520)">
      <rect x="150" y="150" width="120" height="740" rx="26" fill="url(#programme-leaf)"/>
      <circle cx="244" cy="540" r="13" fill="#cf9b2e"/>
    </g>

    <!-- disco ball: string, glow, body, facets, highlight -->
    <line x1="512" y1="120" x2="512" y2="300" stroke="#b5905a" stroke-width="9"/>
    <circle cx="512" cy="470" r="200" fill="#fff2cf" opacity=".5"/>
    <circle cx="512" cy="470" r="172" fill="url(#programme-ball)"/>
    <g clip-path="url(#programme-ballClip)" stroke="#2c7e6d" stroke-width="7" opacity=".5" fill="none">
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

// --- seat card --------------------------------------------------------------------------------


function restText(seat) {
  return `<span class="rest-count" data-reset-at="${esc(seat.limited_until)}" data-clock-prefix="back in" title="${esc(fmtClock(seat.limited_until))}">back in ${fmtCountdown(seat.limited_until)}</span>`;
}

function statusBit(tool, seat) {
  switch (seat.status) {
    case "active": return seat.in_session ? "on the floor" : "selected";
    case "queued": return "up next";
    case "resting": return restText(seat);
    case "needs-login": return "sign-in needed";
    default: return "ready";
  }
}

function bar(seat, win, label) {
  const used = pct(seat, win);
  const left = used === null ? null : Math.round(100 - used);
  const reset = seat?.usage?.windows?.[win]?.resets_at;
  const windows = seat?.usage?.windows || {};
  const distinct = windows['5h']?.resets_at && windows.weekly?.resets_at && windows['5h'].resets_at !== windows.weekly.resets_at;
  const timer = reset && Number.isFinite(Date.parse(reset)) && (seat.status !== 'resting' || distinct && reset !== seat.limited_until)
    ? `<span class="usage-reset" data-reset-at="${esc(reset)}" data-clock-prefix="${label} window resets in" title="${esc(fmtClock(reset))}">${label} window resets in ${fmtCountdown(reset)}</span>` : '';
  return `<div class="window"><div class="usage"><span>${label} window</span><span>${left === null ? 'usage unknown' : left === 0 ? 'no headroom' : `${left}% left`}</span></div>
    <div class="track${left === 0 ? ' exhausted' : ''}" aria-hidden="true"><span class="fill" style="width:${left ?? 0}%"></span></div>${timer}</div>`;
}

function seatCard(tool, seat) {
  const reported = seat?.usage?.reported_windows;
  const weeklyOnly = tool === 'codex' && Array.isArray(reported) && reported.includes('weekly') && !reported.includes('5h');
  const credit = creditLeft(seat);
  const action = needsHello(seat)
    ? `<button data-action="add" data-tool="${tool}">${seat.entitlement_revoked ? 'subscription ended; sign in' : 'sign in'}</button>`
    : !['active', 'resting', 'queued'].includes(seat.status)
      ? `<button data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch to this seat</button>` : '';
  const issue = ({ rate_limited:'usage updates throttled; retrying automatically', network:'connection unavailable; retrying automatically',
    token_expired:`open ${TOOL_META[tool].label} to refresh usage`, unauthorized:'sign in to refresh usage', forbidden:'check your subscription to refresh usage', no_token:'sign in to refresh usage' })[seat.usage?.error];
  return `<article class="seat seat--${esc(seat.status)}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <details class="seat-disclosure"><summary class="seat-row">
      <span class="seat-name">${esc(seat.name)}</span>
      <span class="seat-summary"><span>${statusBit(tool, seat)}</span>${credit !== null && seat.status !== 'resting' ? `<span>${credit}% left${seat.usage_stale || seat.usage_unknown ? ', last known' : ''}</span>` : ''}</span>
      <span class="chevron" aria-hidden="true">⌄</span></summary>
      <div class="expand"><p class="seat-email">${esc(seat.email)} ${planChip(seat.plan)}</p>
        ${weeklyOnly ? '' : bar(seat, '5h', '5h')}${bar(seat, 'weekly', '7d')}
        ${seat.status !== 'resting' ? `<p class="usage-age" data-usage-at="${esc(seat.usage_fetched_at || seat.usage?.fetched_at || '')}">${fmtUsageAge(seat.usage_fetched_at || seat.usage?.fetched_at)}</p>` : ''}
        ${issue ? `<p class="usage-error" role="status">${issue}</p>` : ''}
        ${seat.in_session && !seat.active ? '<p class="support">a terminal is attached to this seat</p>' : ''}
        ${seat.session_started_at ? `<p class="support">session started ${esc(new Date(seat.session_started_at).toLocaleString())}</p>` : ''}
        <div class="seat-detail-actions">${action}<button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out</button></div>
      </div></details></article>`;
}

function toolGroup(tool, t, keys = []) {
  const seats = t?.seats || [];
  if (!seats.length && !keys.length) return '';
  return `<section class="group" style="--accent:${TOOL_META[tool].accent}">
    <h2 class="g-head"><span class="tool-marker tool-marker--${tool}" aria-hidden="true"></span><span>${TOOL_META[tool].label}</span><span class="g-count">${seats.length + keys.length} seats</span></h2>
    ${seats.map(seat => seatCard(tool, seat)).join('')}${keys.map(keySeatCard).join('')}</section>`;
}

const REFRESH = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="none"
  stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
  d="M12.5 4.5a5 5 0 1 0 1.2 3.3"/><path fill="currentColor" d="M13.5 2.2l.6 2.8-2.8.2z"/></svg>`;

function controlBar(opts) {
  const { icon, title, chip, sub, key, on, accentClass } = opts;
  return `<label class="ctl">
    <span class="ctl-ic ${accentClass}">${icon}</span>
    <span class="ctl-tx"><span class="ctl-t">${title}${chip ? ` <span class="mono ctl-chip">${chip}</span>` : ""}</span>
      <span class="ctl-s">${sub}</span></span>
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}><span class="sw"></span>
  </label>`;
}

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
    row: "ChatGPT sign-in; Business seat",
    chip: "Codex CLI; ChatGPT sign-in or auth.json",
    tokenHint: `paste the contents of ${CODEX_AUTH_PATH} — handy for a headless or shared box.`,
    tokenPh: "paste auth.json contents",
  },
  claude: {
    row: "Claude.ai sign-in; Max or Pro seat",
    chip: "Claude Code; Claude.ai sign-in",
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
  return strat === "most_headroom"
    ? "i jump to whoever's got the most room left to breathe"
    : "if everyone's capped, i hold the seat that wakes up first — shortest wait wins";
}
// A toggle row: title + subtitle on the left, 42×24 switch on the right.

function toggleRow(key, title, subtitle, on) {
  return `<label class="set-toggle-row"><span class="set-tx"><span class="set-t">${title}</span><span class="set-s">${subtitle}</span></span>
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? 'checked' : ''}>
    <span class="toggle-state" aria-hidden="true"><span class="when-on">on</span><span class="when-off">off</span></span><span class="sw" aria-hidden="true"></span></label>`;
}

// A segmented block: label + optional hint stacked, then the full-width control on its own line.
function segBlock(label, hint, action, current, options) {
  const segs = options.map((o) =>
    `<button class="sopt ${current === o.v ? "on" : ""}" data-action="${action}" data-value="${o.v}">${o.label}</button>`).join("");
  return `<div class="set-seg-row">
    <div class="set-tx"><span class="set-t">${label}</span>${hint ? `<span class="set-s">${hint}</span>` : ""}</div>
    <div class="set-seg">${segs}</div></div>`;
}

// Settings sub-view (spec §9.1) — a full-panel pushed screen, NOT a modal. Renders into #root in
// place of the popover; back chevron / done / Esc pop back to main. Every change persists live.

export function buildSettings(state) {
  const s = state?.settings || {};
  const theme = s.theme === 'dark' ? 'dark' : 'light';
  const strat = s.strategy === 'most_headroom' ? 'most_headroom' : 'soonest_back';
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="settings-back" aria-label="back">‹</button><span class="set-title">settings</span><button data-action="settings-back">done</button></header>
    <div class="set-body">
      <section class="set-sec switching-settings"><h1 class="set-label"><em>i.</em> switching</h1>
        ${toggleRow('auto_switch','auto-switch','move when your seat runs out',s.auto_switch)}
        ${segBlock('choose the next seat', '', 'set_strategy', strat, STRATEGY_OPTS)}
        ${toggleRow('supervise_shell','supervise terminals','follow codex and claude commands',s.supervise_shell !== false)}
        ${toggleRow('same_tool_only','stay on the same tool','Codex to Codex; Claude to Claude',s.same_tool_only)}
        ${toggleRow('notify','tell me when it switches','show a notification',s.notify)}
        ${toggleRow('restart_app','restart Codex after a swap','refresh the desktop account',s.restart_app)}
        <p class="support">${strat === 'most_headroom' ? 'choose the seat with the most room left.' : 'if every seat is resting, choose the first to return.'} without supervision, use cx/cl to switch automatically.</p>
      </section>
      <section class="set-sec paid-settings"><h2 class="set-label"><em>ii.</em> paid keys</h2>
        ${toggleRow('key_fallback','allow paid key use','real money can be spent',s.key_fallback === true)}
        ${toggleRow('confirm_key_switch','ask before using a key','approve each paid switch',s.confirm_key_switch !== false)}
        <p class="support">paid use permits fallback and pinned terminals. turning it off stops paid sessions and new requests; sent turns may still bill. without asking, eligible keys can spend automatically. this app does not cap spend.</p>
      </section>
      <section class="set-sec"><h2 class="set-label"><em>iii.</em> appearance</h2>
        ${segBlock('theme', '', 'set_theme', theme, THEME_OPTS)}
        <div class="set-legend"><p class="set-t">the menu bar door</p><div class="set-legend-row">${doorMark({door:'open'})}<span>a model is free</span></div><div class="set-legend-row">${doorMark({door:'shut'})}<span>every seat is resting</span></div><p>gold dot: just switched you<br>rose dot: a seat needs sign-in</p></div>
      </section><p class="set-ver">ai guest list${state?.app?.version ? ` v${esc(state.app.version)}` : ''}</p>
    </div></div>`;
}

function verdict(state) {
  const seats = ['codex','claude'].flatMap(tool => (state?.tools?.[tool]?.seats || []).map(seat => ({...seat, tool})));
  const active = seats.find(seat => seat.status === 'active' && seat.in_session) || seats.find(seat => seat.status === 'active');
  const available = active || seats.find(seat => seat.status === 'ready');
  let title, sub = '', action = '';
  if (available) {
    title = `<em>${esc(available.name)},</em><br>${active ? active.in_session ? 'on the floor.' : 'ready to work.' : 'ready to work.'}`;
    const left = creditLeft(available);
    sub = `${TOOL_META[available.tool].label}${left !== null ? ` has ${left}% headroom${available.usage_stale || available.usage_unknown ? ' (last known)' : ''}.` : ' is available.'}`;
  } else if (!seats.length && !(state?.keys || []).length) {
    title = 'your name<br><em>on the list.</em>';
    sub = 'add a subscription seat or an api key to begin.';
    action = `<button class="primary" data-action="add">add your first seat</button>`;
  } else {
    const key = state?.keys?.find(k => k.last_proof?.outcome !== 'incompatible');
    const login = seats.find(needsHello);
    title = seats.length && seats.every(seat => ['resting','queued'].includes(seat.status)) ? 'everyone<br>is <em>resting.</em>' : 'work is<br><em>paused.</em>';
    if (key) {
      sub = `${esc(key.label)} is a paid option${key.responses_verified === false ? '; endpoint unproven' : ''}.`;
      action = `<button class="primary" data-action="key-terminal" data-id="${esc(key.id)}">use this paid key</button>`;
    } else if (login) {
      sub = `${esc(login.name)} needs you to sign in.`;
      action = `<button class="primary" data-action="add" data-tool="${login.tool}">sign in</button>`;
    } else {
      const next = seats.filter(seat => Number.isFinite(Date.parse(seat.limited_until))).sort((a,b) => Date.parse(a.limited_until)-Date.parse(b.limited_until))[0];
      sub = next ? `the next seat returns in ${fmtCountdown(next.limited_until)}.` : 'check the seats below for their return.';
    }
  }
  return `<section class="verdict" aria-label="current situation" role="status"><div class="frontispiece">${PROGRAMME_ART}</div><h1>${title}</h1><p class="verdict-sub">${sub}</p>${action}</section>`;
}

export function buildHTML(state) {
  const theme = state?.settings?.theme === 'dark' ? 'dark' : 'light';
  const paid = (state?.pinned_sessions || []).length || (state?.running_key_seats || []).length;
  const asking = (state?.pending_key_switches || []).some(r => r.status === 'pending' && Date.parse(r.expires_at) > Date.now());
  return `<div class="app theme-${theme}"><header class="top"><span class="brand">ai guest list<span class="brand-sub">the programme</span></span><div class="top-actions"><button class="ibtn" data-action="settings" title="settings" aria-label="settings">⋯</button><details class="header-menu"><summary class="ibtn" title="add a seat or key" aria-label="add a seat or key">＋</summary><div class="menu-panel"><button data-action="add">a subscription seat</button><button data-action="key-start">an api key</button></div></details></div></header>
    <div class="main-body${asking ? ' has-consent' : ''}">
      ${keyConfirmations(state)}${paid ? paidUseControl(state) : asking ? '' : verdict(state)}
      <div class="guest-list"><h2 class="list-heading">the guest list</h2>
        ${toolGroup('codex',state?.tools?.codex,(state?.keys || []).filter(k => k.harness === 'codex'))}
        ${toolGroup('claude',state?.tools?.claude,(state?.keys || []).filter(k => k.harness === 'claude'))}
      </div>${supervisionBanner(state)}
      ${state?.moved_note ? `<details class="history"><summary>last switch</summary><p>${esc(state.moved_note)}</p></details>` : ''}
      <footer class="foot"><span>ai guest list</span><button class="link" data-action="quit">quit</button></footer>
    </div></div>`;
}

function paidUseControl(state) {
  const running = state?.running_key_seats || [];
  const pinned = state?.pinned_sessions || [];
  if (!running.length && !pinned.length) return '';
  const stopping = state?.settings?.key_fallback === false;
  const automatic = running.filter(id => !pinned.some(p => (p.key_seat?.id || p.email) === id));
  return `<section class="paid-use-control" aria-label="paid sessions" role="status"><div class="frontispiece">${PROGRAMME_ART}</div>
    <h1>${stopping ? 'paid use<br>is <em>stopping.</em>' : 'a key is<br><em>spending.</em>'}</h1>
    <button class="primary" data-action="key-stop"${stopping ? ' disabled' : ''}>${stopping ? 'stopping paid use…' : 'stop all paid use'}</button>
    <div class="paid-session-list">${pinnedSessions(state)}${automatic.map(id => {
      const seat = state?.keys?.find(key => key.id === id);
      return `<div class="paid-session"><span class="session-title">${esc(seat?.label || id)}</span><p class="support">${esc(seat?.harness || 'automatic fallback')}</p><p class="key-model">${esc(seat?.model || '')}</p><button data-action="key-stop"${stopping ? ' disabled' : ''}>stop paid use</button></div>`;
    }).join('')}</div><p class="support">stops sessions and new requests. sent turns may still bill.</p></section>`;
}

export function pinnedSessions(state) {
  return (state?.pinned_sessions || []).map(session => `<div class="paid-session"><span class="session-title">${esc(session.key_seat?.label || session.email)}</span>
    <span class="session-facts">${esc(session.tool)} / terminal ${esc(session.pid)}</span><p class="key-model">${esc(session.key_seat?.model || '')}</p>
    <button class="session-end" data-action="end-pinned-session" data-tool="${esc(session.tool)}" data-pin="${esc(session.pin)}"${session.end_requested ? ' disabled' : ''}>${session.end_requested ? 'ending…' : 'end session'}</button></div>`).join('');
}

// Key providers mirror providers.py's harness gate: chat-only catalogs are not usable seats.
// No price table lives here. Every amount comes from a bridge price envelope.
export const KEY_PROVIDERS = {
  openai: { name: "openai", harness: "codex", pricing: "https://openai.com/api/pricing/" },
  anthropic: { name: "anthropic", harness: "claude", pricing: "https://www.anthropic.com/pricing" },
  openrouter: { name: "openrouter", harness: "codex", priced: true, pricing: "https://openrouter.ai/models" },
  langdock: { name: "langdock / openai models", harness: "codex", regional: true, pricing: "https://www.langdock.com/pricing" },
  langdock_anthropic: { name: "langdock / claude models", harness: "claude", regional: true, pricing: "https://www.langdock.com/pricing" },
  deepseek: { name: "deepseek", harness: "codex", pricing: "https://api-docs.deepseek.com/quick_start/pricing" },
  xai: { name: "xai", harness: "codex", priced: true, pricing: "https://docs.x.ai/docs/models" },
  groq: { name: "groq", harness: "codex", unverified: true, pricing: "https://groq.com/pricing" },
  openai_compatible: { name: "openai-compatible", harness: "codex", unverified: true },
};

function providerName(seat) {
  return `${KEY_PROVIDERS[seat.provider]?.name || seat.provider || "key"}${seat.region ? `; ${seat.region}` : ""}`;
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
  return `$${number.toLocaleString('en-US', {useGrouping:false, minimumFractionDigits:2, maximumFractionDigits:20})}`;
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
  const unproven = (seat.responses_verified === false || seat.last_proof?.outcome === 'incompatible') && seat.harness !== 'claude';
  const validation = seat.last_validation;
  return `<article class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}"><details class="seat-disclosure"><summary class="seat-row"><span class="seat-name">${esc(seat.label)}</span><span class="seat-summary">api key<br>paid per use</span><span class="chevron" aria-hidden="true">⌄</span></summary>
    <div class="expand"><p class="key-detail">${esc(providerName(seat))}</p><p class="key-model">${esc(seat.model)}</p>
      <p class="support">paid per token. this app does not cap spend.</p>
      <button data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button>
      ${keyProofStatus(seat)}
      ${validation ? `<p class="key-check-status" role="status">${validation.operation_permitted ? 'key check passed; account access confirmed.' : 'key check was not permitted; check access with your provider.'}</p>` : ''}
      <div class="seat-detail-actions"><button data-action="key-validate" data-id="${esc(seat.id)}">check key</button><button class="logout" data-action="key-remove" data-id="${esc(seat.id)}">remove key</button></div>
      ${unproven ? `<div class="proof-gate"><input class="local-check" id="proof-${esc(seat.id)}" type="checkbox"><label class="proof-open" for="proof-${esc(seat.id)}">check endpoint…</label><div class="proof-choice"><p>this sends a real request and costs money. the price may be unknown.</p><div class="k-acts"><label class="btn" for="proof-${esc(seat.id)}">not now</label><button data-action="key-prove" data-id="${esc(seat.id)}">send request</button></div></div></div>` : ''}
    </div></details></article>`;
}

function pricingLink(provider) {
  return KEY_PROVIDERS[provider]?.pricing ? `<a class="pricing-link" href="${KEY_PROVIDERS[provider].pricing}" data-action="key-pricing" data-provider="${esc(provider)}">provider pricing</a>` : 'ask your endpoint operator for pricing';
}

export function keyConfirmations(state, answering = new Set(), now = Date.now()) {
  const requests = (state?.pending_key_switches || []).filter(r => r.status === 'pending' && Date.parse(r.expires_at) > now);
  return `<div class="key-prompts" aria-live="polite">${requests.map((r, i) => {
    const disabled = answering.has(r.id) ? ' disabled' : '';
    const input = priceRate(r.price,'input'), output = priceRate(r.price,'output');
    const unpriced = input === null && output === null;
    return `<section class="key-confirm" aria-label="paid key confirmation">
      ${i === 0 && !(state?.pinned_sessions?.length || state?.running_key_seats?.length) ? `<div class="frontispiece">${PROGRAMME_ART}</div>` : ''}
      <h1>${r.pinned ? 'pin to a<br><em>paid key?</em>' : 'keep going<br><em>on a key?</em>'}</h1>
      <p class="decision-seat">${esc(r.key_seat?.label)}</p><p class="k-model">${esc(r.key_seat?.model)}</p>
      <p class="k-fine">${r.pinned ? 'this terminal will spend money.' : 'the next turn will spend money.'} no spend cap.</p>
      <p class="key-price">${unpriced ? `prices unavailable from ${esc(providerName(r.key_seat || {}))}; check ${pricingLink(r.key_seat?.provider)}.` : `input ${esc(formatPrice(input))} / output ${esc(formatPrice(output))}<br>per million tokens. estimates, not a session cost.`}</p>
      <div class="k-acts"><button class="k-no" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${disabled}>not now</button><button class="k-go" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${disabled}>use the key</button></div>
      <p class="expiry">${answering.has(r.id) ? 'sending your answer…' : `expires in ${fmtCountdown(r.expires_at, now)}; Esc declines`}</p>
    </section>`;
  }).join('')}</div>`;
}

export function buildPaidKeyGate(state, flow) {
  const seat = state.keys?.find(key => key.id === flow.id);
  const theme = state.settings?.theme === 'dark' ? 'dark' : 'light';
  const disabled = flow.pending ? ' disabled' : '';
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="paid-key-back" aria-label="back"${disabled}>‹</button><span class="set-title">paid key use</span></header>
    <div class="set-body"><section class="set-sec"><h1 class="set-label">allow <em>paid use?</em></h1><p>open ${esc(seat?.label || flow.label)} in a new terminal?</p><p class="support">this also enables automatic fallback. real money can be spent. turning paid use off stops sessions; sent turns may still bill.</p>
    ${flow.error ? `<p class="usage-error" role="alert">${esc(flow.error)}</p>` : ''}
    <div class="k-acts gate-acts"><button data-action="paid-key-back"${disabled}>not now</button><button data-action="paid-key-enable"${disabled}>${flow.pending ? 'opening terminal…' : 'allow and open'}</button></div></section></div></div>`;
}

function isFree(model) {
  const input = priceRate(model.price,'input'), output = priceRate(model.price,'output');
  return input !== null && output !== null && Number(input) === 0 && Number(output) === 0;
}
function contextText(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '';
  return value >= 1000000 ? `${Number((value / 1000000).toFixed(2))}M` : value >= 1000 ? `${Number((value / 1000).toFixed(1))}k` : `${value}`;
}
function decimalPrice(value) {
  const text = formatPrice(value);
  const [whole, fraction = ''] = text.split('.');
  return value === null ? '<span>unavailable</span>' : `<span class="decimal"><span>${esc(whole)}</span><span>.${esc(fraction)}</span></span>`;
}

export function buildModelResults(flow) {
  const catalog = flow.catalog || {};
  const priced = catalog.sort_key === 'input_usd_per_million_tokens';
  const models = [...(catalog.models || [])].sort((a,b) => {
    if (!priced) return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    const av = priceRate(a.price,'input'), bv = priceRate(b.price,'input');
    return (av === null ? Infinity : Number(av)) - (bv === null ? Infinity : Number(bv));
  });
  const query = (flow.modelFilter || '').toLowerCase();
  const visible = models.filter(m => m.id.toLowerCase().includes(query) || (m.display_name || '').toLowerCase().includes(query));
  const paidCount = visible.filter(m => !isFree(m)).length;
  return `<p class="model-count" role="status"><span class="free-hidden">${paidCount}</span><span class="free-shown">${visible.length}</span> matching models</p>
    <div class="model-columns"><span>model / context</span>${priced ? '<span>input / output<br>USD per million tokens</span>' : ''}</div>
    <div class="model-list${priced ? '' : ' model-list--unpriced'}">${visible.map(model => `<button class="key-model-option${isFree(model) ? ' is-free' : ''}" data-action="key-model" data-model="${esc(model.id)}"><span class="model-identity"><span class="model-id">${esc(model.id)}</span>${model.context_window != null ? `<span class="model-context">${esc(contextText(model.context_window))} context</span>` : ''}</span>${priced ? `<span class="model-rates">${isFree(model) ? 'free' : decimalPrice(priceRate(model.price,'input')) + decimalPrice(priceRate(model.price,'output'))}</span>` : ''}</button>`).join('') || `<p class="empty">${query ? `no models match “${esc(flow.modelFilter)}”. change your search.` : 'no models returned. go back to check your key and endpoint.'}</p>`}
    ${visible.length && !paidCount ? '<p class="only-free free-hidden">only free models match; show free models above.</p>' : ''}</div>`;
}

export function buildModelPicker(flow) {
  const catalog = flow.catalog || {};
  const priced = catalog.sort_key === 'input_usd_per_million_tokens';
  return `<section class="model-picker"><div class="search-region"><h1 class="picker-title">find your<br><em>next model.</em></h1>
    <label class="search-label" for="key-model-filter">search by model id or name</label><input class="add-input" id="key-model-filter" type="search" autofocus autocomplete="off" spellcheck="false" placeholder="e.g. a provider or model name" value="${esc(flow.modelFilter || '')}">
    <label class="free-control"><input id="show-free-models" type="checkbox"><span class="free-hidden">free models hidden <b>show</b></span><span class="free-shown">free models shown <b>hide</b></span></label>
    <p class="catalog-note">${priced ? 'input price, low to high; unknown last.' : 'no machine-readable prices; model id order.'} ${catalog.source === 'cache' ? 'cached catalog' : 'live catalog'}; ${esc(fmtUsageAge(catalog.fetched_at))}${catalog.potentially_stale ? '; may be stale' : ''}.</p>
    ${catalog.error ? '<p class="usage-error" role="status">refresh failed; showing the last catalog. go back to reload.</p>' : ''}</div>
    <div id="key-model-results">${buildModelResults(flow)}</div>
    <footer class="model-footer">${pricingLink(flow.provider)}<p>estimates, not a spend cap. set limits with your provider.</p></footer></section>`;
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
      <span class="add-prov-tx"><span class="add-prov-name">${p.name}</span><span class="add-prov-sub">${p.harness === "claude" ? "claude code; messages" : "codex cli; responses"}${p.unverified ? "; unproven" : ""}</span></span><span class="add-chev">›</span></button>`).join("")}</div>
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
    ${keyConfirmations(state)}<div class="set-body${flow.step === 'models' ? ' picker-body' : ''}">${flow.error ? `<div class="usage-error" role="alert">${esc(flow.error)}</div>` : ""}${body}</div></div>`;
}
