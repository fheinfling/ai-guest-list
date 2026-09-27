// v3-doorway. Fork of app/web/render.mjs; pure render functions, unchanged bridge contracts.
// One doorway contains the answer. Hanken Grotesk carries every UI and data label.

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
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
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

// --- seat card --------------------------------------------------------------------------------

function statusBit(tool, seat) {
  if (seat.status === "resting" || seat.status === "queued") {
    const at = seat.limited_until;
    const valid = at && Number.isFinite(Date.parse(at));
    return valid ? `<span class="rest-count" tabindex="0" title="${esc(new Date(at).toLocaleString())}" aria-label="resting; back ${esc(new Date(at).toLocaleString())}"><span data-reset-at="${esc(at)}" data-clock-prefix="back in">back in ${fmtCountdown(at)}</span></span>` : '<span>resting</span>';
  }
  if (needsHello(seat)) return '<span>needs sign-in</span>';
  if (seat.in_session) return '<span>on the floor</span>';
  return `<span>${seat.active || seat.status === "active" ? "selected" : "ready"}</span>`;
}

function bar(seat, win, label) {
  const used = pct(seat, win);
  const left = used === null || seat.usage_unknown ? null : Math.round(100 - used);
  const windows = seat.usage?.windows || {};
  const reset = windows[win]?.resets_at;
  const other = windows[win === "5h" ? "weekly" : "5h"]?.resets_at;
  const distinct = reset && other && reset !== other && reset !== seat.limited_until;
  return `<div class="window"><div class="usage"><span>${label} window</span><span>${left === null ? "usage unavailable" : `${seat.usage_stale ? "last known " : ""}${left}% left${left === 0 ? " / resting" : ""}`}</span></div>
    <div class="track${left === 0 ? " exhausted" : ""}" aria-hidden="true"><span class="fill" style="width:${left ?? 0}%"></span></div>
    ${distinct ? `<span class="usage-reset" tabindex="0" title="${esc(new Date(reset).toLocaleString())}" aria-label="${label} window resets ${esc(new Date(reset).toLocaleString())}"><span data-reset-at="${esc(reset)}" data-clock-prefix="window resets in">window resets in ${fmtCountdown(reset)}</span></span>` : ""}</div>`;
}

function seatCard(tool, seat) {
  const credit = seat.usage_unknown ? null : creditLeft(seat);
  const reported = seat.usage?.reported_windows;
  const weeklyOnly = tool === "codex" && Array.isArray(reported) && reported.includes("weekly") && !reported.includes("5h");
  const fetchedAt = seat.usage_fetched_at || seat.usage?.fetched_at || "";
  const issue = ({rate_limited:"usage updates throttled; retrying automatically", network:"connection unavailable; retrying automatically", token_expired:`open ${TOOL_META[tool].label} to refresh usage`, unauthorized:"sign in to refresh usage", forbidden:"check your subscription to refresh usage", no_token:"sign in to refresh usage"})[seat.usage?.error];
  const action = needsHello(seat)
    ? `<button class="btn" data-action="add" data-tool="${tool}">${seat.entitlement_revoked ? "subscription ended; sign in again" : "sign in again"}</button>`
    : !seat.active && !["active", "resting", "queued"].includes(seat.status)
      ? `<button class="btn" data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch to this seat</button>` : "";
  return `<div class="seat seat--${esc(seat.status || "ready")}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <details class="seat-disclosure"><summary class="seat-row"><span class="seat-name">${esc(seat.name || seat.email)}</span>
      <span class="seat-summary"><span>${statusBit(tool, seat)}</span><span class="headroom">${credit === null ? "usage unavailable" : `${seat.usage_stale ? "last known " : ""}${credit}% left`}</span></span><span class="chevron" aria-hidden="true">⌄</span></summary>
      <div class="expand"><div class="seat-email">${esc(seat.email)} ${planChip(seat.plan)}</div>
      ${weeklyOnly ? "" : bar(seat, "5h", "5h")}${bar(seat, "weekly", "weekly")}
      <div class="usage-age" data-usage-at="${esc(fetchedAt)}">${fmtUsageAge(fetchedAt)}</div>
      ${seat.session_started_at ? `<div class="session-age">terminal attached <span data-session-at="${esc(seat.session_started_at)}">${fmtSessionAge(seat.session_started_at)}</span></div>` : ""}
      ${issue ? `<p class="usage-error" role="status">${issue}</p>` : ""}
      <div class="seat-detail-actions">${action}<button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out</button></div></div>
    </details></div>`;
}

function toolGroup(tool, t, keys = []) {
  const seats = [...(t?.seats || [])].sort((a,b) => Number(Boolean(b.active)) - Number(Boolean(a.active)));
  if (!seats.length && !keys.length) return "";
  return `<section class="group" style="--accent:${TOOL_META[tool].accent}"><h2 class="g-head"><span class="tool-marker tool-marker--${tool}" aria-hidden="true"></span>${TOOL_META[tool].label}</h2>
    ${seats.map(seat => seatCard(tool, seat)).join("")}${keys.map(keySeatCard).join("")}</section>`;
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
      <span>terminal supervision is off; codex and claude won't auto-switch</span>
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
    tokenHint: `paste the contents of ${CODEX_AUTH_PATH} from your other account.`,
    tokenPh: "paste auth.json contents",
  },
  claude: {
    row: "Claude.ai sign-in; Max or Pro seat",
    chip: "Claude Code; Claude.ai sign-in",
  },
};
const BROWSER_HINT = "open the official sign-in, then save the credentials on this Mac.";

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
  return `<section class="set-sec"><h2 class="set-label">a subscription seat</h2><div class="set-card">${["codex","claude"].map(tool => `<button class="add-prov" data-action="add-provider" data-tool="${tool}"><span class="tool-marker tool-marker--${tool}" aria-hidden="true"></span><span class="add-prov-tx"><span class="add-prov-name">${TOOL_META[tool].label}</span><span class="add-prov-sub">${tool === "codex" ? "ChatGPT sign-in" : "Claude.ai sign-in"}</span></span><span aria-hidden="true">›</span></button>`).join("")}</div><p class="add-foot">save your credentials on this Mac to switch between seats.</p><button class="add-row" data-action="key-start">add an API key instead</button></section>`;
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
              <span class="add-provcard-s">save the current login</span></span>
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
        <input id="add-name" class="add-input" placeholder="Work, Personal or Late-night" value="${esc(add.name)}">
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
    <div class="add-sub">your seat's saved</div></div>`;
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
    <div class="set-body" tabindex="0">${body}</div>
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
  return strat === "most_headroom" ? "choose the seat with most room" : "choose the seat that returns first";
}
function toggleRow(key, title, subtitle, on) {
  return `<label class="set-toggle-row"><span class="set-tx"><span class="set-t">${title}</span><span class="set-s">${subtitle}</span></span><input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}><span class="toggle-state" aria-hidden="true"><span class="when-on">on</span><span class="when-off">off</span></span><span class="sw" aria-hidden="true"></span></label>`;
}
function segBlock(label, hint, action, current, options) {
  return `<div class="set-seg-row"><span class="set-t">${label}</span>${hint ? `<span class="set-s">${hint}</span>` : ""}<div class="set-seg">${options.map(o => `<button class="sopt ${current === o.v ? "on" : ""}" data-action="${action}" data-value="${o.v}" aria-pressed="${current === o.v}">${o.label}</button>`).join("")}</div></div>`;
}
export function buildSettings(state) {
  const s = state?.settings || {};
  const theme = s.theme === "dark" ? "dark" : "light";
  const strat = s.strategy === "most_headroom" ? "most_headroom" : "soonest_back";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="settings-back" aria-label="back to guest list">‹</button><span class="set-title">settings</span><button class="set-done" data-action="settings-back">done</button></header><div class="set-body" tabindex="0" aria-label="settings">
    <section class="set-sec switching-settings"><h2 class="set-label">switching</h2>
      ${toggleRow("auto_switch", "auto-switch", "switch when a seat rests", s.auto_switch)}
      ${segBlock("when a seat runs out", strategyHint(strat), "set_strategy", strat, STRATEGY_OPTS)}
      ${toggleRow("supervise_shell", "supervise terminal commands", "use codex or claude directly", s.supervise_shell !== false)}
      ${toggleRow("same_tool_only", "stay with the same tool", "keep the current tool", s.same_tool_only)}
      ${toggleRow("notify", "tell me when it switches", "show who takes over", s.notify)}
      ${toggleRow("restart_app", "restart the Codex app", "use the new desktop login", s.restart_app)}
    </section>
    <section class="set-sec paid-settings"><h2 class="set-label">paid keys</h2>
      ${toggleRow("key_fallback", "allow paid key use", "permits billed requests", s.key_fallback === true)}
      ${toggleRow("confirm_key_switch", "ask before using a key", "approve each paid switch", s.confirm_key_switch !== false)}
      <p class="add-foot">paid use starts off. enabling it permits fallback and pinned terminals. turning it off stops paid sessions and new requests. sent turns may still bill.</p>
      <p class="add-foot">turning confirmation off lets eligible keys spend without asking. this app has no spend cap.</p>
    </section>
    <section class="set-sec"><h2 class="set-label">appearance</h2>${segBlock("theme", "", "set_theme", theme, THEME_OPTS)}
      <div class="set-legend"><span class="set-t">what the icon shows</span><div class="set-legend-row">${doorMark({door:"open"})}<span>a seat is ready</span></div><div class="set-legend-row">${doorMark({door:"shut"})}<span>every seat is resting</span></div><p>the menu bar also signals a recent switch or a seat needing sign-in.</p></div></section>
    <p class="add-foot">supervision off: use cx or cl to auto-switch. restarting updates the desktop login; terminals switch on their own.</p><div class="set-ver">ai guest list${state?.app?.version ? ` v${esc(state.app.version)}` : ""}</div></div></div>`;
}

// --- popover ----------------------------------------------------------------------------------

function currentRequests(state) {
  return (state?.pending_key_switches || []).filter(r => r.status === "pending");
}
function paidSessions(state) {
  const pins = state?.pinned_sessions || [];
  const sessions = pins.map(pin => ({...pin, seat:pin.key_seat || {}, pinned:true}));
  for (const id of state?.running_key_seats || []) {
    if (pins.some(pin => [pin.key_seat?.id, pin.email, String(pin.email || "").replace(/^key:/, "")].includes(id))) continue;
    const seat = state?.keys?.find(key => key.id === id) || {label:id};
    sessions.push({seat, tool:seat.harness, pinned:false});
  }
  return sessions;
}
function verdict(state) {
  const requests = currentRequests(state);
  if (requests.length) {
    const first = requests[0];
    return `<div class="plain-verdict" role="status">${first.pinned ? "a paid terminal needs your answer." : `${esc(first.from_seat?.label || "your seat")} is resting; a paid key needs your answer.`}</div>`;
  }
  const tools = ["codex","claude"];
  const seats = tools.flatMap(tool => (state?.tools?.[tool]?.seats || []).map(seat => ({tool, seat})));
  if (!seats.length && !state?.keys?.length) return '<section class="verdict aperture" role="status"><h1>add a seat so you can keep working.</h1></section>';
  const active = seats.find(({seat}) => seat.in_session && ["active","ready"].includes(seat.status)) || seats.find(({seat}) => seat.active && ["active","ready"].includes(seat.status)) || seats.find(({seat}) => ["active","ready"].includes(seat.status));
  if (active) return `<section class="verdict aperture" role="status"><h1>${esc(active.seat.name || active.seat.email)} is ${active.seat.in_session ? "on the floor with" : "ready for"} ${TOOL_META[active.tool].label}.</h1></section>`;
  const key = state?.keys?.[0];
  if (key) {
    const unproven = key.responses_verified === false || key.last_proof?.outcome === "incompatible";
    return `<section class="verdict verdict--blocked aperture"><h1 role="status">your ${esc(TOOL_META[key.harness]?.label || "subscription")} seats are resting; ${esc(key.label)} ${unproven ? "is unproven" : "can keep you going"}</h1><p class="support">paid per use. price unknown. no spend cap.</p><button class="primary" data-action="key-terminal" data-id="${esc(key.id)}">use in new terminal</button></section>`;
  }
  const repair = seats.find(({seat}) => needsHello(seat));
  return `<section class="verdict aperture"><h1 role="status">${repair ? "sign in to keep working." : "your seats are resting."}</h1>${repair ? `<button class="primary" data-action="add" data-tool="${repair.tool}">sign in again</button>` : `<p class="support">the guest list shows when each seat returns.</p>`}</section>`;
}
export function buildHTML(state) {
  const s = state?.settings || {};
  const theme = s.theme === "dark" ? "dark" : "light";
  const money = paidSessions(state).length > 0;
  const asking = currentRequests(state).length > 0;
  const empty = !["codex","claude"].some(tool => state?.tools?.[tool]?.seats?.length) && !state?.keys?.length;
  const moved = state?.moved_note ? `<section class="event"><h2>last switch</h2><p>${esc(state.moved_note.replace(/ · /g, "; ").replace(/ → /g, " to ").replace(/ — /g, "; "))}</p></section>` : "";
  return `<div class="app theme-${theme}"><header class="top"><span class="brand">ai guest list</span><nav class="top-actions" aria-label="guest list controls">
    <details class="header-menu"><summary class="ibtn${empty ? " add-empty" : ""}" aria-label="add a seat or API key">${empty ? "add a seat" : "＋"}</summary><div class="menu-panel"><button data-action="add">a subscription seat</button><button data-action="key-start">an API key</button></div></details>
    <details class="header-menu"><summary class="ibtn" aria-label="settings and switching">⋯</summary><div class="menu-panel"><button data-action="settings">open settings</button>${toggleRow("auto_switch", "auto-switch", "switch when a seat rests", s.auto_switch)}</div></details></nav></header>
    <div class="urgency${money ? " urgency--spending aperture" : ""}${asking ? " urgency--asking" : ""}${money && asking ? " urgency--combined" : ""}">
      ${money ? paidUseControl(state) : verdict(state)}${keyConfirmations(state)}</div>
    <div class="main-body" tabindex="0" aria-label="guest list details">
      ${toolGroup("codex", state?.tools?.codex, (state?.keys || []).filter(key => key.harness === "codex"))}
      ${toolGroup("claude", state?.tools?.claude, (state?.keys || []).filter(key => key.harness === "claude"))}
      ${supervisionBanner(state)}${moved}<footer class="foot"><button class="link" data-action="quit">quit</button></footer></div></div>`;
}
function paidUseControl(state) {
  const sessions = paidSessions(state);
  if (!sessions.length) return "";
  const first = sessions[0];
  const stopping = state.settings?.key_fallback === false;
  const title = stopping ? "paid use is stopping…" : sessions.length === 1
    ? `${first.seat.label || "your key"} is spending${first.pid ? ` in terminal ${first.pid}` : ""}.`
    : `${sessions.length} paid sessions are spending.`;
  return `<section class="paid-use-control"><h1 role="status">${esc(title)}</h1><button class="primary" data-action="key-stop"${stopping ? " disabled" : ""}>${stopping ? "stopping paid use…" : "stop paid use"}</button>
    <div class="paid-session-list" tabindex="0" aria-label="paid sessions">${sessions.map(session => sessionRow(session, sessions.length === 1)).join("")}</div><p class="support">stops sessions and new requests. sent turns still bill.</p></section>`;
}
function sessionRow(session, single = false) {
  const seat = session.seat || {};
  return `<div class="paid-session">${single ? "" : `<p class="session-title">${esc(seat.label || "your key")}${session.pid ? ` in terminal ${esc(session.pid)}` : ""}</p>`}<div class="session-facts"><span>${esc(providerName(seat))}</span><span>${esc(TOOL_META[session.tool]?.label || session.tool || "")}</span></div><p class="key-model">${esc(seat.model)}</p>
    ${session.pinned ? `<button class="btn session-end" data-action="end-pinned-session" data-tool="${esc(session.tool)}" data-pin="${esc(session.pin)}"${session.end_requested ? " disabled" : ""}>${session.end_requested ? "ending session…" : "end session"}</button>` : '<p class="support">fallback session; use stop paid use to end it.</p>'}</div>`;
}
export function pinnedSessions(state) {
  return paidSessions(state).filter(session => session.pinned).map(session => sessionRow(session)).join("");
}

// Key providers mirror providers.py's harness gate: chat-only catalogs are not usable seats.
// No price table lives here. Every amount comes from a bridge price envelope.
export const KEY_PROVIDERS = {
  openai: { name: "openai", harness: "codex", pricing: "https://openai.com/api/pricing/" },
  anthropic: { name: "anthropic", harness: "claude", pricing: "https://www.anthropic.com/pricing" },
  openrouter: { name: "openrouter", harness: "codex", priced: true, pricing: "https://openrouter.ai/models" },
  langdock: { name: "langdock (openai models)", harness: "codex", regional: true, pricing: "https://www.langdock.com/pricing" },
  langdock_anthropic: { name: "langdock (claude models)", harness: "claude", regional: true, pricing: "https://www.langdock.com/pricing" },
  deepseek: { name: "deepseek", harness: "codex", pricing: "https://api-docs.deepseek.com/quick_start/pricing" },
  xai: { name: "xai", harness: "codex", priced: true, pricing: "https://docs.x.ai/docs/models" },
  groq: { name: "groq", harness: "codex", unverified: true, pricing: "https://groq.com/pricing" },
  openai_compatible: { name: "openai-compatible", harness: "codex", unverified: true },
};

function providerName(seat) {
  return `${KEY_PROVIDERS[seat.provider]?.name || seat.provider || "key"}${seat.region ? ` (${seat.region})` : ""}`;
}

// Reject missing/blank/boolean values before numeric coercion: Number(null) is NOT a free model.
// Preserve decimal strings until formatting, including tiny nonzero rates.
function amount(value) {
  if (!["string", "number"].includes(typeof value) || String(value).trim() === "") return null;
  return Number.isFinite(Number(value)) && Number(value) >= 0 ? String(value) : null;
}
export function formatPrice(value) {
  const raw = amount(value)?.trim() ?? null;
  if (raw === null) return "unknown";
  // Expand scientific notation without quantizing provider precision.
  const [coefficient, exponentText] = raw.toLowerCase().split("e");
  let decimal = coefficient;
  if (exponentText !== undefined) {
    const [whole, frac = ""] = coefficient.split(".");
    const digits = whole + frac;
    const point = whole.length + Number(exponentText);
    decimal = point <= 0 ? `0.${"0".repeat(-point)}${digits}` : point >= digits.length ? digits + "0".repeat(point - digits.length) : `${digits.slice(0,point)}.${digits.slice(point)}`;
  }
  const [whole, fraction = ""] = decimal.split(".");
  return `$${whole || "0"}.${fraction.padEnd(2,"0")}`;
}

function priceRate(price, name, seen = []) {
  if (price?.source !== "live" || price.currency !== "USD" || price.token_unit !== "per_million_tokens") return null;
  const rate = price.rates?.[name];
  if (seen.includes(name)) return null;
  if (rate?.status === "same_as") return priceRate(price, rate.same_as, [...seen, name]);
  return rate?.status === "known" ? amount(rate.value) : null;
}
function priceAge(price, now = Date.now()) {
  const age = price?.verified_at && Number.isFinite(Date.parse(price.verified_at)) ? fmtUsageAge(price.verified_at, now)
    : amount(price?.age_seconds) !== null ? `fetched ${Math.floor(Number(price.age_seconds) / 60)}m ago` : "fetch time unavailable";
  return `${price?.potentially_stale ? "stale price estimate" : "live price estimate"}; ${age}`;
}
function priceText(price) {
  const input = priceRate(price,"input"), output = priceRate(price,"output");
  if (input === null && output === null) return "price unknown";
  return `input ${formatPrice(input)} / output ${formatPrice(output)} per million tokens`;
}
function priceHTML(price) {
  const text = priceText(price);
  return `<p class="key-price">${esc(text)}</p>${text === "price unknown" ? "" : `<p class="support">${esc(priceAge(price))}</p>`}`;
}
function pricingAction(provider, label = "check its pricing page") {
  return KEY_PROVIDERS[provider]?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(provider)}">${label}</button>` : "check pricing with your endpoint operator";
}
function priceExplanation(price, provider) {
  if (priceRate(price,"input") !== null || priceRate(price,"output") !== null) return priceHTML(price);
  const knownUnpriced = ["openai","anthropic","langdock","langdock_anthropic"].includes(provider);
  return `<p class="support">${esc(KEY_PROVIDERS[provider]?.name || provider || "this provider")} ${knownUnpriced ? "doesn't publish machine-readable prices" : "prices are unavailable"}; ${pricingAction(provider)}.</p>`;
}

export function keyProofStatus(seat) {
  if (seat.harness === "claude") return "";
  const proof = seat.last_proof;
  let text = "";
  if (proof?.outcome === "proven") text = "endpoint checked; a Responses turn completed.";
  else if (proof?.outcome === "incompatible") text = "this endpoint cannot run Responses; choose a different endpoint.";
  else if (proof?.outcome === "refused") text = "the provider rejected the check; review access and billing with your provider. inference is unproven.";
  else if (proof?.outcome === "inconclusive") text = "the check did not complete; check your connection and endpoint before trying again. inference is unproven.";
  else if (seat.responses_verified === false) text = "inference access is unproven; this endpoint may not work.";
  return text ? `<p class="key-proof" role="status">${text}</p>` : "";
}
export function keySeatCard(seat) {
  const unproven = (seat.responses_verified === false || seat.last_proof?.outcome === "incompatible") && seat.harness !== "claude";
  const checked = seat.last_validation;
  const proofId = `proof-${encodeURIComponent(seat.id || "key")}`;
  return `<div class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}"><details class="seat-disclosure"><summary class="seat-row"><span class="seat-name">${esc(seat.label)}</span><span class="seat-summary">API key</span><span class="chevron" aria-hidden="true">⌄</span></summary>
    <div class="expand"><p class="key-detail">${esc(providerName(seat))}</p><p class="key-model">${esc(seat.model)}</p><p class="support">paid per use. price unknown. no spend cap.</p>
    <button class="primary" data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button>
    ${unproven ? `<div class="proof-gate"><input class="local-check" type="checkbox" id="${proofId}" aria-label="review or decline the billed endpoint check"><label class="btn proof-open" for="${proofId}">check this endpoint</label><div class="proof-choice"><p class="support">this check sends a billed request. price unknown. sent turns still bill.</p><div class="k-acts"><label class="btn" for="${proofId}">not now</label><button class="btn" data-action="key-prove" data-id="${esc(seat.id)}">check endpoint</button></div></div></div>` : ""}
    <div class="seat-detail-actions"><button class="btn" data-action="key-validate" data-id="${esc(seat.id)}">check key</button><button class="logout" data-action="key-remove" data-id="${esc(seat.id)}">remove key</button></div></div></details>
    ${keyProofStatus(seat)}${checked ? `<p class="key-check-status" role="status">${checked.operation_permitted === false ? "the provider rejected the key check; check access with your provider or add a replacement with +." : "account access checked."}${seat.responses_verified ? "" : " inference access is still unproven."}</p>` : ""}</div>`;
}

export function keyConfirmations(state, answering = new Set(), now = Date.now()) {
  const requests = currentRequests(state);
  return `<div class="key-prompts">${requests.map((r,index) => {
    const expired = !Number.isFinite(Date.parse(r.expires_at)) || Date.parse(r.expires_at) <= now;
    const pending = answering.has(r.id);
    const seat = r.key_seat || {};
    const heading = r.pinned ? `pin this terminal to ${seat.label || "this key"}'s paid key?` : `use ${seat.label || "this key"}'s paid key to continue?`;
    return `<section class="key-confirm" aria-label="paid key confirmation"><div class="decision-frame aperture" tabindex="0" aria-label="paid key decision"><h1 tabindex="-1"${index === 0 ? " autofocus" : ""}>${esc(heading)}</h1><p class="support">${esc(providerName(seat))}</p><p class="k-model">${esc(seat.model)}</p>${priceExplanation(r.price,seat.provider)}
      ${r.proof || r.operation === "key_prove" ? '<p class="support">this check sends a billed request.</p>' : ""}
      <p class="support">no spend cap. sent turns still bill.</p></div>
      <p class="expiry"${expired || pending ? ' role="status"' : ""}>${expired ? "request expired; no paid switch approved" : pending ? "sending your answer…" : `<span data-reset-at="${esc(r.expires_at)}" data-clock-prefix="expires in">expires in ${fmtCountdown(r.expires_at,now)}</span>`}</p>
      <div class="k-acts"><button class="k-no" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${pending || expired ? " disabled" : ""}>not now</button><button class="k-go" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${pending || expired ? " disabled" : ""}>use the key</button></div></section>`;
  }).join("")}</div>`;
}
export function buildPaidKeyGate(state, flow) {
  const seat = state.keys?.find(key => key.id === flow.id);
  const theme = state.settings?.theme === "dark" ? "dark" : "light";
  const disabled = flow.pending ? " disabled" : "";
  return `<div class="app set-app add-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="paid-key-back" aria-label="back"${disabled}>‹</button><span class="set-title">allow paid key use</span></header><div class="set-body" tabindex="0"><section class="set-sec paid-settings"><h1 tabindex="-1" autofocus>use ${esc(seat?.label || flow.label)} in a new terminal?</h1><p>paid use is off. enabling it permits paid terminals and automatic fallback.</p><p class="support">real money can be spent. price unknown. no spend cap. turning paid use off stops sessions and new requests. sent turns still bill.</p>${seat ? keyProofStatus(seat) : ""}${flow.error ? `<p role="alert">${esc(flow.error)}</p>` : ""}${flow.pending ? '<p role="status">opening your terminal…</p>' : ""}<div class="k-acts"><button class="k-no" data-action="paid-key-back"${disabled}>not now</button><button class="k-go" data-action="paid-key-enable"${disabled}>allow paid use and open terminal</button></div></section></div></div>`;
}


// Render only these results on input: the filter field itself keeps focus and its caret.
function freeModel(model) {
  const input = priceRate(model.price,"input"), output = priceRate(model.price,"output");
  return /:free$/i.test(model.id) || (input !== null && output !== null && Number(input) === 0 && Number(output) === 0);
}
function contextText(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "";
  return n >= 1000000 ? `${Number((n / 1000000).toFixed(2))}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);
}
function decimalPrice(value) {
  if (value === null) return '<span class="rate-unknown">unknown</span>';
  const [whole, fraction] = formatPrice(value).split(".");
  return `<span class="decimal"><span>${esc(whole)}</span><span>.${esc(fraction)}</span></span>`;
}
export function buildModelResults(flow) {
  const models = [...(flow.catalog?.models || [])].sort((a,b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const query = (flow.modelFilter || "").toLowerCase();
  const matches = models.filter(model => model.id.toLowerCase().includes(query) || (model.display_name || "").toLowerCase().includes(query));
  const paid = matches.filter(model => !freeModel(model));
  const priced = models.some(model => priceRate(model.price,"input") !== null || priceRate(model.price,"output") !== null);
  return `<div class="model-count"><span class="free-hidden">${paid.length}</span><span class="free-shown">${matches.length}</span> of ${models.length} models; model id order</div>
    <div class="model-columns${priced ? "" : " model-columns--unpriced"}"><span>model / context</span>${priced ? '<span>input / output<br>$ per 1M tokens</span>' : ""}</div>
    <div class="model-list${priced ? "" : " model-list--unpriced"}" tabindex="0" aria-label="model results">${matches.map(model => `<button class="key-model-option${freeModel(model) ? " is-free" : ""}" data-action="key-model" data-model="${esc(model.id)}"><span class="model-identity"><span class="model-id">${esc(model.id)}</span>${contextText(model.context_window) ? `<span class="model-context" title="${esc(Number(model.context_window).toLocaleString("en-US"))} tokens" aria-label="${esc(model.context_window)} token context">${contextText(model.context_window)} context</span>` : ""}</span>${priced ? `<span class="model-rates">${freeModel(model) ? '<span>free</span>' : `<span aria-label="input ${esc(formatPrice(priceRate(model.price,"input")))} dollars per million tokens">${decimalPrice(priceRate(model.price,"input"))}</span><span aria-label="output ${esc(formatPrice(priceRate(model.price,"output")))} dollars per million tokens">${decimalPrice(priceRate(model.price,"output"))}</span>`}</span>` : ""}</button>`).join("")}
    ${!paid.length ? `<p class="empty free-hidden">${matches.length ? `${matches.length} free matches are hidden. use show above.` : query ? `no models match “${esc(flow.modelFilter)}”. change your search.` : "no models returned; go back and check this endpoint."}</p>` : ""}${!matches.length ? '<p class="empty free-shown">no matching models; change your search.</p>' : ""}</div>`;
}
export function buildModelPicker(flow) {
  const catalog = flow.catalog || {};
  const priced = (catalog.models || []).some(model => priceRate(model.price,"input") !== null || priceRate(model.price,"output") !== null);
  const price = catalog.models?.find(model => priceRate(model.price,"input") !== null || priceRate(model.price,"output") !== null)?.price;
  let source = priced ? priceAge(price) : `${KEY_PROVIDERS[flow.provider]?.name || "this provider"} ${KEY_PROVIDERS[flow.provider]?.priced ? "prices are unavailable" : "doesn't publish machine-readable prices"}.`;
  if (catalog.error) source = "catalog refresh failed; showing the last reading.";
  else if (catalog.source === "cache") source = `cached catalog${catalog.potentially_stale ? "; over 24h old" : ""}; ${catalog.fetched_at ? fmtUsageAge(catalog.fetched_at) : "fetch time unavailable"}`;
  return `<section class="model-picker"><div class="search-region"><label for="key-model-filter">search models</label><input class="add-input" id="key-model-filter" type="search" aria-label="search models" placeholder="try claude, gpt or a model id" autocomplete="off" spellcheck="false" autofocus value="${esc(flow.modelFilter || "")}"><label class="free-control"><input id="show-free-models" type="checkbox"><span class="free-hidden">free models hidden <b>show</b></span><span class="free-shown">free models shown <b>hide</b></span></label></div><div id="key-model-results">${buildModelResults(flow)}</div>
    <footer class="model-footer"><p class="support">${esc(source)}</p>${pricingAction(flow.provider,"check provider pricing")}<p class="support">this app doesn't cap spend.</p><p class="support free-shown">free OpenRouter models are rate-limited previews.</p></footer></section>`;
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
      <span class="add-prov-tx"><span class="add-prov-name">${p.name}</span><span class="add-prov-sub">${p.harness === "claude" ? "claude code (messages)" : "codex cli (responses)"}${p.unverified ? "; unproven" : ""}</span></span><span class="add-chev">›</span></button>`).join("")}</div>
      <div class="add-foot">the same langdock key works for both routes: openai models through codex, claude models through claude code.</div>
      <div class="add-foot">openrouter offers live model prices across providers. direct openai, anthropic and langdock catalogs don't publish machine-readable prices.</div></section>`;
  } else if (flow.step === "details") {
    body = `<div class="add-provcard"><span class="add-prov-tx"><span class="add-provcard-t">new ${esc(provider.name)} key seat</span><span class="add-provcard-s">${provider.harness === "claude" ? "claude code" : "codex cli"} sessions</span></span><button class="add-change" data-action="key-back">change</button></div>
      ${provider.regional ? `<section class="set-sec"><label class="set-label" for="key-region">region</label><div class="set-card"><select class="add-input" id="key-region">${["eu", "us", "global"].map((r) => `<option value="${r}"${flow.region === r ? " selected" : ""}>${r}</option>`).join("")}</select></div></section>` : ""}
      ${flow.provider === "openai_compatible" ? `<section class="set-sec"><label class="set-label" for="key-base-url">your endpoint's base url</label><div class="set-card"><input class="add-input" id="key-base-url" type="url" placeholder="https://your-host/v1" value="${esc(flow.base_url)}"></div><div class="add-hint">must support responses; chat completions alone won't work.</div></section>` : ""}
      <section class="set-sec"><label class="set-label" for="key-label">name this seat</label><div class="set-card"><input class="add-input" id="key-label" placeholder="work or late-night" value="${esc(flow.label)}"></div></section>
      <section class="set-sec"><label class="set-label" for="key-secret">paste your api key</label><div class="set-card"><input class="add-input mono" id="key-secret" type="password" autocomplete="off" spellcheck="false" value="${esc(flow.secret)}"></div><div class="add-hint">sent only to the provider you chose; saved in your Mac's keychain.</div></section>
      ${provider.unverified ? `<section class="set-sec"><span class="set-t">this endpoint is unproven</span><div class="add-hint">we cannot promise its responses endpoint works with codex, even if it lists models. requests may still cost money.</div><label class="key-ack"><input id="key-ack" type="checkbox"${flow.allow_unverified ? " checked" : ""}> i understand it may not work, and want to try this endpoint</label></section>` : ""}
      <button class="add-cta" data-action="key-discover">pick a model</button>`;
  } else if (flow.step === "connecting") {
    body = `<div class="add-center"><div class="add-spin"></div><div class="add-h">${flow.operation === "key_add" ? "saving your key seat…" : "looking up models…"}</div><div class="add-sub">${flow.operation === "key_add" ? "saving in your Mac’s keychain" : "asking only the provider you chose"}</div></div>`;
  } else if (flow.step === "models") {
    body = buildModelPicker(flow);
  } else if (flow.step === "review") {
    const model = flow.catalog?.models?.find((m) => m.id === flow.model);
    body = `<section class="set-sec"><span class="set-label">a seat for ${esc(flow.label)}</span><div class="set-card add-method"><span class="set-t">${esc(providerName(flow))}</span><div class="key-model mono">${esc(flow.model)}</div>${priceHTML(model?.price)}</div>
      <div class="add-foot">saving a key does not start a paid session. choose “use in new terminal” when you're ready.</div>
      <div class="add-foot">use your provider's budget controls for limits. this app does not cap spend.</div></section><button class="add-cta" data-action="key-save">save the key seat</button>`;
  } else {
    body = `<div class="add-center add-center--done"><div class="add-heart">💛</div><div class="add-welcome">welcome, ${esc(flow.label)}</div><div class="add-sub">your key seat's saved</div>
      ${flow.savedSeat?.last_validation?.operation_permitted === false ? `<div class="usage-error">the key check wasn't permitted. check access with your provider before using it.</div>` : ""}
      <button class="add-cta" data-action="key-cancel">back to the guest list</button></div>`;
  }
  return `<div class="app set-app add-app theme-${theme}" style="--accent:${accent}"><header class="set-head">
    <button class="set-back" data-action="key-back" title="back"${flow.pending ? " disabled" : ""}>‹</button><span class="set-title">add a key</span>
    ${flow.pending ? "" : `<button class="add-cancel" data-action="key-cancel">cancel</button>`}</header>
    ${keyConfirmations(state)}<div class="set-body${flow.step === "models" ? " picker-body" : ""}" tabindex="0">${flow.error ? `<div class="usage-error" role="alert">${esc(flow.error)}</div>` : ""}${body}</div></div>`;
}
