// v4-booth-console: a render-only fork. The dispatcher and bridge remain unchanged.
// Hanken throughout; capacity strips show headroom, never consumption.

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

function seatStatus(seat) {
  if (needsHello(seat)) return "needs sign-in";
  if (seat.status === "resting") return "resting";
  if (seat.status === "queued") return "up next";
  if (seat.in_session) return "on the floor";
  return seat.status === "active" || seat.active ? "selected" : "ready";
}

function returnTime(seat) {
  if (seat.status !== "resting" || !seat.limited_until) return "";
  const absolute = Number.isFinite(Date.parse(seat.limited_until))
    ? new Date(seat.limited_until).toLocaleString() : "return time unavailable";
  return `<span class="return-time" tabindex="0" title="${esc(absolute)}"><span data-reset-at="${esc(seat.limited_until)}" data-clock-prefix="back in">back in ${fmtCountdown(seat.limited_until)}</span><span class="time-tooltip">${esc(absolute)}</span></span>`;
}

function capacity(seat, win, label) {
  const used = pct(seat, win);
  if (used === null || !Number.isFinite(used)) return `<span class="capacity capacity--unknown"><span class="sr-only">${label}: </span>usage unavailable</span>`;
  const left = Math.round(100 - used), filled = Math.ceil(left / 20);
  const stale = seat.usage_stale || seat.usage_unknown;
  return `<span class="capacity${left === 0 ? " capacity--empty" : ""}" data-window-label="${label}" aria-label="${label}: ${stale ? "last known, " : ""}${left}% left${left === 0 ? ", resting" : ""}">
    <span class="segments" aria-hidden="true">${Array.from({length: 5}, (_, i) => `<i class="segment${i < filled ? " full" : ""}"></i>`).join("")}</span>
    <span class="capacity-number">${left}%<span class="sr-only"> left${left === 0 ? ", resting" : ""}</span></span></span>`;
}

function seatCard(tool, seat) {
  const reported = seat.usage?.reported_windows;
  const weeklyOnly = tool === "codex" && Array.isArray(reported) && reported.includes("weekly") && !reported.includes("5h");
  const fetched = seat.usage_fetched_at || seat.usage?.fetched_at || "";
  const stale = seat.usage_stale || seat.usage_unknown;
  const windows = seat.usage?.windows || {};
  const resetsDiffer = windows["5h"]?.resets_at !== windows.weekly?.resets_at;
  const windowDetail = (win, label) => {
    const reset = windows[win]?.resets_at;
    // Seat return appears once. Distinct window resets are labelled and live in details.
    const timer = resetsDiffer && reset && reset !== seat.limited_until && Number.isFinite(Date.parse(reset))
      ? `<span data-reset-at="${esc(reset)}" data-clock-prefix="resets in">resets in ${fmtCountdown(reset)}</span>` : "";
    return `<div class="x-row"><span>${label}</span>${timer || `<span>${reset === seat.limited_until ? "seat return above" : "reset shared with other window"}</span>`}</div>`;
  };
  const issue = ({ rate_limited: "usage updates are throttled; checking again automatically", network: "connection unavailable; checking again automatically", token_expired: `open ${TOOL_META[tool].label} to refresh usage`, unauthorized: "sign in to refresh usage", forbidden: "check your subscription to refresh usage", no_token: "sign in to refresh usage" })[seat.usage?.error];
  const action = needsHello(seat)
    ? `<button data-action="add" data-tool="${tool}">${seat.entitlement_revoked ? "subscription ended; sign in again" : "sign in"}</button>`
    : !["active", "queued", "resting"].includes(seat.status)
      ? `<button data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch</button>` : "";
  return `<article class="seat seat--${esc(seat.status || "ready")}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <div class="seat-grid"><div class="seat-identity"><span class="seat-name">${esc(seat.name)}</span><span class="seat-state">${seatStatus(seat)}</span>${returnTime(seat)}${stale ? `<span class="last-known">last known</span>` : ""}</div>
    ${weeklyOnly ? `<span class="capacity"><span class="sr-only">5h window: </span>not reported</span>` : capacity(seat, "5h", "5h window")}${capacity(seat, "weekly", "weekly window")}
    <button class="seat-disclose" aria-label="show or hide details for ${esc(seat.name)}">⌄</button></div>
    <div class="expand"><div class="seat-email">${esc(seat.email)}</div>${planChip(seat.plan)}
      ${weeklyOnly ? "" : windowDetail("5h", "5h window")}${windowDetail("weekly", "weekly window")}
      <p class="support" data-usage-at="${esc(fetched)}">${fmtUsageAge(fetched)}</p>${issue ? `<p class="usage-error" role="status">${issue}</p>` : ""}
      ${seat.session_started_at ? `<p class="support">terminal attached<span data-session-at="${esc(seat.session_started_at)}">${fmtSessionAge(seat.session_started_at)}</span></p>` : ""}
      <div class="entry-actions">${action}<button data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out</button></div>
    </div></article>`;
}

function toolGroup(tool, t, keys = []) {
  const meta = TOOL_META[tool], seats = t?.seats || [];
  return `<section class="group group--${tool}" aria-label="${meta.label} seats"><div class="matrix-heading seat-grid"><h2><span class="tool-marker ${tool}" aria-hidden="true"></span>${meta.label}</h2><span>5h left</span><span>week left</span><span class="sr-only">details</span></div>
    ${seats.map(seat => seatCard(tool, seat)).join("")}${keys.map(keySeatCard).join("")}${!seats.length && !keys.length ? `<p class="empty">no seats yet</p>` : ""}</section>`;
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
      <span>terminal supervision is off; <span class="mono">codex/claude</span> won't auto-switch</span>
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
    tokenHint: `paste the contents of ${CODEX_AUTH_PATH} from the account you want to add.`,
    tokenPh: "paste auth.json contents",
  },
  claude: {
    row: "Claude.ai sign-in; Max or Pro seat",
    chip: "Claude Code; Claude.ai sign-in",
  },
};
const BROWSER_HINT = "open the official sign-in to save this seat on your Mac.";

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
  return `<section class="set-sec"><h2 class="set-label">a subscription seat</h2><div class="set-card">${["codex", "claude"].map(tool => `<button class="add-prov" data-action="add-provider" data-tool="${tool}"><span class="tool-marker ${tool}" aria-hidden="true"></span><span class="add-prov-tx"><span class="add-prov-name">${TOOL_META[tool].label}</span><span class="add-prov-sub">${tool === "codex" ? "ChatGPT sign-in or auth.json" : "Claude.ai sign-in"}</span></span></button>`).join("")}</div><p class="add-foot">your credentials stay on your Mac.</p></section>`;
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
              <span class="add-provcard-s">use the login on this Mac</span></span>
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
      ? `<div class="add-tokenwrap"><textarea aria-label="auth.json contents" id="add-token" class="add-token mono"
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
        <input aria-label="seat name" id="add-name" class="add-input" placeholder="Work, Personal, Late-night" value="${esc(add.name)}">
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
    <div class="add-sub">your seat is saved</div></div>`;
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
      <button class="set-back" data-action="add-back" aria-label="back">‹</button>
      <span class="set-title">add a seat</span>
      ${cancel}
    </header>
    ${auxiliaryUrgency(state)}<div class="set-body" tabindex="0" aria-label="add a seat">${body}</div>
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
  return strat === "most_headroom" ? "choose the seat with room left" : "choose the seat that returns first";
}
function toggleRow(key, title, subtitle, on) {
  return `<label class="set-toggle-row"><span class="set-tx"><span class="set-t">${title}</span><span class="set-s">${subtitle}</span></span><input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}><span class="sw" aria-hidden="true"><span class="sw-on">on</span><span class="sw-off">off</span></span></label>`;
}
function segBlock(label, hint, action, current, options) {
  return `<div class="set-seg-row"><span class="set-t">${label}</span>${hint ? `<span class="set-s">${hint}</span>` : ""}<div class="set-seg" role="group" aria-label="${label}">${options.map(o => `<button class="sopt${current === o.v ? " on" : ""}" aria-pressed="${current === o.v}" data-action="${action}" data-value="${o.v}">${o.label}</button>`).join("")}</div></div>`;
}
export function buildSettings(state) {
  const s = state?.settings || {}, theme = s.theme === "dark" ? "dark" : "light";
  const strat = s.strategy === "most_headroom" ? "most_headroom" : "soonest_back";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="settings-back" aria-label="back">‹</button><h1 class="set-title">settings</h1><button data-action="settings-back">done</button></header>
    ${auxiliaryUrgency(state)}<div class="set-body" tabindex="0" aria-label="settings"><section class="set-sec"><h2 class="set-label">switching</h2>
      ${toggleRow("auto_switch", "auto-switch", "switch when a seat rests", s.auto_switch)}
      ${segBlock("when a seat runs out", strategyHint(strat), "set_strategy", strat, STRATEGY_OPTS)}
      ${toggleRow("supervise_shell", "supervise terminal commands", "use codex or claude directly", s.supervise_shell !== false)}
      ${toggleRow("same_tool_only", "keep the same tool", "stay with the current tool", s.same_tool_only)}
      ${toggleRow("notify", "notify when switching", "show who takes over", s.notify)}
      ${toggleRow("restart_app", "restart the Codex app", "use the new desktop login", s.restart_app)}
    </section><section class="set-sec paid-settings"><h2 class="set-label">paid keys</h2>
      ${toggleRow("key_fallback", "allow paid key use", "permits billed requests", s.key_fallback === true)}
      ${toggleRow("confirm_key_switch", "ask before using", "approve each paid switch", s.confirm_key_switch !== false)}
      <p class="set-s">real money. paid use starts off. enabling it permits fallback and pinned terminals. turning it off stops paid sessions and new requests. sent turns may still bill.</p><p class="set-s">turning confirmation off lets eligible keys spend without asking.</p>
    </section><section class="set-sec"><h2 class="set-label">appearance</h2>${segBlock("theme", "", "set_theme", theme, THEME_OPTS)}
      <div class="set-legend"><h3>what the icon shows</h3><div>${doorMark({door:"open"})}<span>a seat is ready</span></div><div>${doorMark({door:"shut"})}<span>every seat is resting</span></div><p>selected credentials can be ready without a live terminal.</p></div>
    </section><p class="set-ver">ai guest list${state?.app?.version ? ` v${esc(state.app.version)}` : ""}</p></div></div>`;
}

// --- popover ----------------------------------------------------------------------------------

function addMenu(empty) {
  return `<details class="header-menu add-menu"><summary class="ibtn" aria-label="add a seat or an API key">${empty ? "add a seat" : "＋"}</summary><div class="menu-panel"><button data-action="add">a subscription seat</button><button data-action="key-start">an API key</button></div></details>`;
}
function verdict(state) {
  const requests = (state?.pending_key_switches || []).filter(r => r.status === "pending" && Date.parse(r.expires_at) > Date.now());
  if (requests.length) return `<section class="verdict verdict--asking" tabindex="0"><h1 role="status">${esc(requests[0].from_seat?.label || "your seat")} is resting; a paid key needs your answer.</h1></section>`;
  const all = ["codex", "claude"].flatMap(tool => (state?.tools?.[tool]?.seats || []).map(seat => ({seat, tool})));
  if (!all.length && !(state?.keys || []).length) return `<section class="verdict" tabindex="0"><h1 role="status">add a seat so you can keep working.</h1></section>`;
  const ready = all.find(({seat}) => seat.status === "active") || all.find(({seat}) => seat.status === "ready");
  if (ready) return `<section class="verdict" tabindex="0"><h1 role="status">${esc(ready.seat.name)} is ${ready.seat.in_session ? "on the floor with" : "ready for"} ${TOOL_META[ready.tool].label}.</h1></section>`;
  const key = (state?.keys || [])[0];
  if (key) {
    const tool = TOOL_META[key.harness]?.label || key.harness;
    const unproven = key.responses_verified === false && key.harness !== "claude";
    return `<section class="verdict verdict--blocked" tabindex="0"><h1 role="status">your ${esc(tool)} seats are resting; ${esc(key.label)} is ${unproven ? "unproven" : "a paid option"}</h1><p>paid per use. price unknown. no spend cap.</p><button class="primary" data-action="key-terminal" data-id="${esc(key.id)}">use in new terminal</button></section>`;
  }
  const repair = all.find(({seat}) => needsHello(seat));
  if (repair) return `<section class="verdict" tabindex="0"><h1 role="status">${esc(repair.seat.name)} needs sign-in.</h1><button class="primary" data-action="add" data-tool="${repair.tool}">sign in again</button></section>`;
  return `<section class="verdict" tabindex="0"><h1 role="status">your seats are resting; wait for a seat to return.</h1></section>`;
}
export function buildHTML(state) {
  const s = state?.settings || {}, theme = s.theme === "dark" ? "dark" : "light";
  const empty = !["codex", "claude"].some(t => state?.tools?.[t]?.seats?.length) && !state?.keys?.length;
  const paid = paidUseControl(state);
  const history = state?.moved_note ? `<section class="event"><h2>last switch</h2><p>${esc(state.moved_note.replace(/ · /g, "; ").replace(/ → /g, " to ").replace(/ — /g, "; ").replace(/^auto-moved /, ""))}</p></section>` : "";
  return `<div class="app theme-${theme}"><header class="top">${doorMark(state)}<span class="brand">ai guest list</span><div class="top-actions">${addMenu(empty)}<details class="header-menu"><summary class="ibtn" aria-label="settings and switching">⋯</summary><div class="menu-panel"><button data-action="settings">settings</button>${toggleRow("auto_switch", "auto-switch", "switch when a seat rests", s.auto_switch)}</div></details></div></header>
    <div class="urgency${paid ? " urgency--paid" : ""}">${paid || verdict(state)}${keyConfirmations(state)}</div>
    <div class="main-body" tabindex="0" aria-label="seat details"><div class="console">
      ${toolGroup("codex", state?.tools?.codex, (state?.keys || []).filter(k => k.harness === "codex"))}${toolGroup("claude", state?.tools?.claude, (state?.keys || []).filter(k => k.harness === "claude"))}
    </div>${supervisionBanner(state)}${history}<footer class="foot"><span>ai guest list</span><button data-action="quit">quit</button></footer></div></div>`;
}
function paidSessions(state) {
  const pins = state?.pinned_sessions || [];
  const entries = pins.map(s => ({...s, key_seat: s.key_seat || state?.keys?.find(k => `key:${k.id}` === s.email) || {label:s.email}}));
  for (const id of state?.running_key_seats || []) {
    if (pins.some(s => s.key_seat?.id === id || s.email === id || s.email === `key:${id}`)) continue;
    const key = state?.keys?.find(k => k.id === id) || {label:id};
    entries.push({key_seat:key, tool:key.harness});
  }
  return entries;
}
function sessionRow(s, single = false) {
  const seat = s.key_seat || {};
  return `<section class="paid-session">${single ? "" : `<h3>${esc(seat.label || s.email)} is spending${s.pid != null ? ` in terminal ${esc(s.pid)}` : ""}.</h3>`}<div class="session-facts"><span>${esc(providerName(seat))}</span><span>${esc(TOOL_META[s.tool]?.label || s.tool)}</span><span>${esc(seat.model)}</span></div>
    ${s.pin ? `<button class="stop" data-action="end-pinned-session" data-tool="${esc(s.tool)}" data-pin="${esc(s.pin)}"${s.end_requested ? " disabled" : ""}>${s.end_requested ? "ending…" : "end session"}</button>` : `<span class="support">use stop paid use to end this fallback</span>`}</section>`;
}
function paidUseControl(state) {
  const sessions = paidSessions(state);
  if (!sessions.length) return "";
  const stopping = state?.settings?.key_fallback === false;
  const first = sessions[0];
  return `<section class="paid-use-control" aria-label="paid sessions"><h1 role="status" tabindex="0">${stopping ? "paid use is stopping…" : sessions.length === 1 ? `${esc(first.key_seat.label || first.email)} is spending${first.pid != null ? ` in terminal ${esc(first.pid)}` : ""}.` : `${sessions.length} paid sessions are spending.`}</h1><div class="paid-session-list" tabindex="0" aria-label="running paid sessions">${sessions.map(s => sessionRow(s, sessions.length === 1 && !stopping)).join("")}</div><div class="paid-stop"><button class="stop" data-action="key-stop"${stopping ? " disabled" : ""}>stop paid use</button><p tabindex="0">stops sessions and new requests. sent turns still bill.</p></div></section>`;
}
function auxiliaryUrgency(state) {
  const paid = paidUseControl(state);
  return `<div class="urgency${paid ? " urgency--paid" : ""}">${paid}${keyConfirmations(state)}</div>`;
}
// Kept as an export for callers; buildHTML has exactly one owner for session facts.
export function pinnedSessions(state) {
  return (state?.pinned_sessions || []).map(s => sessionRow(s)).join("");
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
  return `${KEY_PROVIDERS[seat.provider]?.name || seat.provider || "key"}${seat.region ? `, ${seat.region}` : ""}`;
}

// Reject missing/blank/boolean values before numeric coercion: Number(null) is NOT a free model.
// Preserve decimal strings until formatting, including tiny nonzero rates.
function amount(value) {
  if (!["string", "number"].includes(typeof value) || String(value).trim() === "") return null;
  return Number.isFinite(Number(value)) && Number(value) >= 0 ? String(value) : null;
}
export function formatPrice(value) {
  const raw = amount(value);
  if (raw === null) return "price unavailable";
  // Expand exponent notation without rounding supplied decimal precision.
  const [base, exponent = "0"] = raw.toLowerCase().split("e");
  const [whole, fraction = ""] = base.replace(/^\+/, "").split(".");
  const digits = whole + fraction, point = whole.length + Number(exponent);
  let decimal = point <= 0 ? `0.${"0".repeat(-point)}${digits}` : point >= digits.length ? digits + "0".repeat(point - digits.length) : `${digits.slice(0, point)}.${digits.slice(point)}`;
  let [integer, decimals = ""] = decimal.split(".");
  return `$${integer.replace(/^0+(?=\d)/, "")}.${decimals.padEnd(2, "0")}`;
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
  return `${price?.potentially_stale ? "last known price estimate" : price?.source === "cache" ? "cached price estimate" : "live price estimate"}; ${age}${price?.potentially_stale ? "; over 24h old" : ""}`;
}
function priceText(price) {
  const input = priceRate(price, "input"), output = priceRate(price, "output");
  if (input === null && output === null) return "price unknown";
  return `input ${input === null ? "unknown" : formatPrice(input)} / output ${output === null ? "unknown" : formatPrice(output)} per million tokens`;
}
function priceHTML(price) {
  return `<p class="key-price">${esc(priceText(price))}</p>${priceText(price) === "price unknown" ? "" : `<p class="add-hint">${esc(priceAge(price))}</p>`}`;
}

export function keyProofStatus(seat) {
  const proof = seat.last_proof;
  const copy = {
    proven: "inference proven; a Responses turn completed",
    incompatible: "this endpoint does not support Responses; use another key",
    refused: "the provider rejected the request; check your access and billing",
    inconclusive: "no completed turn; inference access is still unproven",
  }[proof?.outcome];
  const unproven = seat.harness !== "claude" && seat.responses_verified === false && proof?.outcome !== "proven";
  return `${copy ? `<p class="key-proof" role="status">${copy}</p>` : ""}${unproven && !copy ? `<p class="key-unproven">inference access is unproven; this endpoint may not work.</p>` : ""}`;
}
export function keySeatCard(seat) {
  const unproven = (seat.responses_verified === false || seat.last_proof?.outcome === "incompatible") && seat.harness !== "claude";
  const validation = seat.last_validation;
  return `<article class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}"><div class="key-seat-row"><div class="seat-identity"><span class="seat-name">${esc(seat.label)}</span><span class="seat-state">api key; ${esc(providerName(seat))}</span></div><button class="seat-disclose" aria-label="show or hide details for ${esc(seat.label)}">⌄</button></div>
    ${keyProofStatus(seat)}${validation ? `<p class="key-validation" role="status">${validation.operation_permitted === false ? "account check was not permitted; check access with your provider." : "account access checked."}${seat.responses_verified ? "" : " inference access is still unproven."}</p>` : ""}
    <div class="expand"><p class="key-model">${esc(seat.model)}</p><p class="add-hint">paid per use. price unknown. no spend cap.</p><button class="primary" data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button>
    ${unproven ? `<p class="add-hint">this check sends a billed request. no spend cap. sent turns still bill.</p><details class="proof-choice"><summary><span class="proof-closed">review endpoint check</span><span class="proof-open">not now</span></summary><div class="proof-content"><button data-action="key-prove" data-id="${esc(seat.id)}">send billed check</button></div></details>` : ""}
    <div class="entry-actions"><button data-action="key-validate" data-id="${esc(seat.id)}">check key</button><button data-action="key-remove" data-id="${esc(seat.id)}">remove key</button></div></div></article>`;
}

function unpricedReason(provider) {
  return ["openai", "anthropic", "langdock", "langdock_anthropic"].includes(provider)
    ? `${KEY_PROVIDERS[provider].name} doesn't publish machine-readable prices`
    : "price unknown";
}
export function keyConfirmations(state, answering = new Set(), now = Date.now()) {
  const requests = (state?.pending_key_switches || []).filter(r => r.status === "pending");
  // Keep an expired pending request visible instead of silently erasing the decision.
  return `<div class="key-prompts">${requests.map(r => {
    const expired = !Number.isFinite(Date.parse(r.expires_at)) || Date.parse(r.expires_at) <= now;
    const disabled = expired || answering.has(r.id) ? " disabled" : "";
    const key = r.key_seat || {}, input = priceRate(r.price, "input"), output = priceRate(r.price, "output");
    const secs = Math.max(0, Math.ceil((Date.parse(r.expires_at) - now) / 1000));
    const deadline = Number.isFinite(secs) ? `${Math.floor(secs / 60)}m ${secs % 60}s` : "unknown";
    return `<section class="key-confirm" aria-label="paid key confirmation"><div class="decision-copy" tabindex="0"><h2 class="k-q" tabindex="-1" autofocus>${r.pinned ? `pin this terminal to ${esc(key.label)}'s paid key?` : `use ${esc(key.label)}'s paid key to continue?`}</h2><p class="quiet-meta">${esc(providerName(key))}</p><p class="k-model">${esc(key.model)}</p>
      ${input === null && output === null ? `<p class="key-price">${esc(unpricedReason(key.provider))}; <button class="inline-button" data-action="key-pricing" data-provider="${esc(key.provider)}">check its pricing page</button>.</p>` : priceHTML(r.price)}
      ${(state?.keys || []).find(k => k.id === key.id)?.responses_verified === false ? `<p class="key-unproven">inference access is unproven.</p>` : ""}
      <p class="k-fine">no spend cap. sent turns still bill.</p><p class="expiry">${expired ? "request expired; no paid switch approved" : `<span data-reset-at="${esc(r.expires_at)}" data-clock-prefix="expires in">expires in ${deadline}</span>`}</p>${answering.has(r.id) ? `<p role="status">sending your answer…</p>` : ""}</div>
      <div class="k-acts"><button class="k-no" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${disabled}>not now</button><button class="k-go" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${disabled}>use the key</button></div></section>`;
  }).join("")}</div>`;
}
export function buildPaidKeyGate(state, flow) {
  const seat = state.keys?.find(key => key.id === flow.id), theme = state.settings?.theme === "dark" ? "dark" : "light";
  const disabled = flow.pending ? " disabled" : "";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button data-action="paid-key-back" aria-label="back"${disabled}>‹</button><h1 class="set-title">allow paid key use</h1></header>${auxiliaryUrgency(state)}<div class="set-body" tabindex="0"><section class="set-sec"><h2 class="k-q" tabindex="-1" autofocus>use ${esc(seat?.label || flow.label)} in a new terminal?</h2><p>real money can be spent. this enables paid keys for pinned terminals and automatic fallback.</p><p>no spend cap. turning paid use off stops sessions and new requests. sent turns still bill.</p>${seat ? keyProofStatus(seat) : ""}${flow.error ? `<p class="usage-error" role="alert">${esc(flow.error)}</p>` : ""}<div class="k-acts gate-acts"><button class="k-no" data-action="paid-key-back"${disabled}>not now</button><button class="k-go" data-action="paid-key-enable"${disabled}>${flow.pending ? "opening terminal…" : "allow paid use and open terminal"}</button></div></section></div></div>`;
}
function isFree(model) {
  const input = priceRate(model.price, "input"), output = priceRate(model.price, "output");
  return /:free$/i.test(model.id) || (input !== null && output !== null && Number(input) === 0 && Number(output) === 0);
}
function pricedCatalog(catalog) {
  return (catalog.models || []).some(model => priceRate(model.price, "input") !== null || priceRate(model.price, "output") !== null);
}
function compactContext(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "";
  return n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);
}
function modelID(id) {
  return esc(id).replace(/([/_.:-])/g, "$1<wbr>");
}
function priceCell(value) {
  if (value === null) return `<span class="price-unknown">unknown</span>`;
  const [integer, fraction] = formatPrice(value).split(".");
  return `<span class="decimal"><span>${integer}</span><span>.${fraction}</span></span>`;
}
export function buildModelResults(flow) {
  const catalog = flow.catalog || {}, priced = pricedCatalog(catalog);
  const models = [...(catalog.models || [])].sort((a,b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const query = (flow.modelFilter || "").toLowerCase();
  const matches = models.filter(m => m.id.toLowerCase().includes(query) || (m.display_name || "").toLowerCase().includes(query));
  const paid = matches.filter(m => !isFree(m)), free = matches.length - paid.length;
  const fractions = matches.flatMap(m => ["input", "output"].map(k => priceRate(m.price, k))).filter(v => v !== null).map(v => formatPrice(v).split(".")[1].length);
  const width = Math.max(2, ...fractions);
  return `<div class="results-meta" role="status" aria-live="polite"><span class="free-hidden">${paid.length} of ${models.length} models; model id order</span><span class="free-shown">${matches.length} of ${models.length} models; model id order</span></div><div class="model-columns${priced ? "" : " unpriced"}"><span>model / context</span>${priced ? `<span>input / output<br>$ per 1M tokens</span>` : ""}</div>
    <div class="model-list${priced ? "" : " unpriced"}" tabindex="0" aria-label="model results" style="--price-fractions:${width + 0.5}ch">${matches.map(model => `<button class="key-model-option${isFree(model) ? " free-model" : ""}" data-action="key-model" data-model="${esc(model.id)}"><span class="model-identity"><span class="model-id">${modelID(model.id)}</span>${compactContext(model.context_window) ? `<span class="model-context" title="${esc(Number(model.context_window).toLocaleString("en-US"))} tokens" aria-label="${esc(model.context_window)} tokens"><span class="context-compact">${compactContext(model.context_window)} context</span><span class="context-exact">${esc(Number(model.context_window).toLocaleString("en-US"))} tokens</span></span>` : ""}</span>${priced ? `<span class="model-prices">${isFree(model) ? "free" : `<span aria-label="input price">${priceCell(priceRate(model.price, "input"))}</span><span aria-label="output price">${priceCell(priceRate(model.price, "output"))}</span>`}</span>` : ""}</button>`).join("")}
    ${!paid.length ? `<p class="empty${free ? " free-hidden" : ""}">no ${query ? "paid models match your search" : "paid models returned"}.${free ? ` ${free} hidden free match${free === 1 ? "" : "es"}; use show above.` : " go back to check the key and endpoint."}</p>` : ""}</div>`;
}
export function buildModelPicker(flow) {
  const catalog = flow.catalog || {}, provider = KEY_PROVIDERS[flow.provider], priced = pricedCatalog(catalog);
  const price = (catalog.models || []).find(m => priceRate(m.price, "input") !== null || priceRate(m.price, "output") !== null)?.price;
  const provenance = !priced ? (provider?.priced ? "catalog prices unavailable. check provider pricing." : `${provider?.name || flow.provider} doesn't publish machine-readable prices. check its pricing page.`) : catalog.error ? "catalog refresh failed; showing last known prices." : catalog.source === "cache" ? `cached price estimate; ${fmtUsageAge(catalog.fetched_at)}${catalog.potentially_stale ? "; over 24h old" : ""}` : priceAge({...price, potentially_stale: catalog.potentially_stale || price?.potentially_stale});
  return `<section class="model-picker"><div class="picker-controls"><label for="key-model-filter">search models</label><input class="add-input" id="key-model-filter" type="search" autofocus placeholder="try claude, gpt or a model id" autocomplete="off" spellcheck="false" value="${esc(flow.modelFilter || "")}"><label class="free-toggle"><input type="checkbox" id="show-free-models"${flow.showFree ? " checked" : ""}><span class="free-hidden">free models hidden <b>show</b></span><span class="free-shown">free models shown <b>hide</b></span></label></div>
    <div id="key-model-results">${buildModelResults(flow)}</div><footer class="picker-footer"><p>${esc(provenance)}</p><p class="free-shown">free OpenRouter models are rate-limited previews.</p><div>${provider?.pricing ? `<button class="pricing-button" data-action="key-pricing" data-provider="${esc(flow.provider)}">check provider pricing</button>` : `<span>ask your endpoint operator for pricing.</span>`}<span>no spend cap.</span></div></footer></section>`;
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

function keyFlowError(flow) {
  if (flow.error === "couldn't load models — try again") return "models did not load; check your key and endpoint, then load models again.";
  if (flow.error === "couldn't save this key — try again") return "the key was not saved; check its details before saving again.";
  return flow.error;
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
      <div class="add-foot">openrouter offers live model prices across providers. direct openai, anthropic and langdock catalogs don't publish machine-readable prices.</div></section>`;
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
      <div class="add-foot">use your provider's budget controls; this app does not cap spend.</div></section><button class="add-cta" data-action="key-save">save the key seat</button>`;
  } else {
    body = `<div class="add-center add-center--done"><div class="add-heart">💛</div><div class="add-welcome">welcome, ${esc(flow.label)}</div><div class="add-sub">your key seat's saved</div>
      ${flow.savedSeat?.last_validation?.operation_permitted === false ? `<div class="usage-error">the key check wasn't permitted. check access with your provider before using it.</div>` : ""}
      <button class="add-cta" data-action="key-cancel">back to the guest list</button></div>`;
  }
  return `<div class="app set-app add-app theme-${theme}" style="--accent:${accent}"><header class="set-head">
    <button class="set-back" data-action="key-back" aria-label="back"${flow.pending ? " disabled" : ""}>‹</button><span class="set-title">add a key</span>
    ${flow.pending ? "" : `<button class="add-cancel" data-action="key-cancel">cancel</button>`}</header>
    ${auxiliaryUrgency(state)}<div class="set-body${flow.step === "models" ? " model-body" : ""}" tabindex="0" aria-label="add a key">${flow.error ? `<div class="usage-error" role="alert">${esc(keyFlowError(flow))}</div>` : ""}${body}</div></div>`;
}
