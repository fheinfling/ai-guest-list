// v8-instrument: a compact, fully exposed instrument panel. Fork of app/web/render.mjs.
// State/reducer helpers retain the shipping behavior; markup owns presentation only.

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

// --- seat card --------------------------------------------------------------------------------

function statusBit(tool, seat) {
  switch (seat.status) {
    case "active": return `<span class="seat-state">${seat.in_session ? "on the floor" : "selected"}</span>`;
    case "queued": return `<span class="seat-state">up next</span>`;
    case "resting": return `<span class="seat-state rest-count" data-reset-at="${esc(seat.limited_until)}" data-clock-prefix="back in" title="${esc(seat.limited_until ? new Date(seat.limited_until).toLocaleString() : "")}">back in ${fmtCountdown(seat.limited_until)}</span>`;
    case "needs-login": return `<button class="btn" data-action="add" data-tool="${tool}">${seat.entitlement_revoked ? "renew sign-in" : "log in"}</button>`;
    default: return `<button class="btn" data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch</button>`;
  }
}

function bar(seat, win, label) {
  const used = pct(seat, win);
  const left = used === null ? null : Math.round(100 - used);
  const windows = seat.usage?.windows || {};
  const reset = windows[win]?.resets_at;
  const different = windows["5h"]?.resets_at !== windows.weekly?.resets_at;
  const resetValid = reset && Number.isFinite(Date.parse(reset));
  // One return for a resting seat. Distinct window resets remain explicitly labelled.
  const showReset = resetValid && (seat.status !== "resting" || (different && reset !== seat.limited_until));
  return `<div class="window${left === 0 ? " window--empty" : ""}">
    <div class="usage"><span class="u-k">${label}</span><span class="track" aria-hidden="true"><span class="fill" style="width:${left ?? 0}%"></span></span><span class="u-v">${left === null ? "?" : `${left}%`}</span></div>
    ${showReset ? `<span class="usage-reset" title="${esc(new Date(reset).toLocaleString())}" data-reset-at="${esc(reset)}" data-clock-prefix="${label} reset">${label} reset ${fmtCountdown(reset)}</span>` : ""}
  </div>`;
}

function seatCard(tool, seat) {
  const credit = creditLeft(seat);
  const reported = seat.usage?.reported_windows;
  const weeklyOnly = tool === "codex" && Array.isArray(reported) && reported.includes("weekly") && !reported.includes("5h");
  const fetched = seat.usage_fetched_at || seat.usage?.fetched_at || "";
  const stale = seat.usage_stale || seat.usage_unknown;
  const error = ({ rate_limited: "updates throttled; retrying automatically", network: "offline; retrying automatically",
    token_expired: `open ${TOOL_META[tool].label} to refresh usage`, unauthorized: "sign in to refresh usage",
    forbidden: "check your subscription to refresh usage", no_token: "sign in to refresh usage" })[seat.usage?.error];
  return `<article class="seat seat--${esc(seat.status)}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <div class="seat-row"><strong class="seat-name">${esc(seat.name)}</strong>${statusBit(tool, seat)}</div>
    <div class="seat-email" title="${esc(seat.email)}">${esc(seat.email)}</div>
    <div class="channel"><div class="channel-number">${credit === null ? "?" : credit}<small>${credit === null ? "unknown" : "% left"}</small></div>
      <div class="channel-windows">${weeklyOnly ? "" : bar(seat, "5h", "5h")}${bar(seat, "weekly", "7d")}</div></div>
    <div class="seat-meta">${planChip(seat.plan)}<span data-usage-at="${esc(fetched)}">${fmtUsageAge(fetched)}</span>${stale ? `<span>last known</span>` : ""}</div>
    ${error ? `<p class="usage-error" role="status">${error}</p>` : ""}
    ${seat.in_session ? `<div class="session-age">terminal attached${seat.session_started_at ? `<span data-session-at="${esc(seat.session_started_at)}">${fmtSessionAge(seat.session_started_at)}</span>` : ""}</div>` : ""}
    ${seat.last_on_floor ? `<div class="seat-meta">last on floor ${esc(fmtClock(seat.last_on_floor))}</div>` : ""}
    <div class="expand"><button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out</button></div>
  </article>`;
}

function toolGroup(tool, t, keys = []) {
  const seats = [...(t?.seats || [])].sort((a, b) => Number(b.status === "active") - Number(a.status === "active"));
  if (!seats.length && !keys.length) return "";
  return `<section class="group group--${tool}" style="--accent:${TOOL_META[tool].accent}">
    <div class="g-head"><span class="tool-marker tool-marker--${tool}" aria-hidden="true"></span><h2>${TOOL_META[tool].label}</h2><span>${String(seats.length).padStart(2, "0")} seats</span><span class="g-meta">remaining quota</span></div>
    <div class="seat-grid">${seats.map((seat) => seatCard(tool, seat)).join("")}</div>${keys.map(keySeatCard).join("")}
  </section>`;
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
    row: "ChatGPT sign-in · Business seat",
    chip: "Codex CLI · ChatGPT sign-in or auth.json",
    tokenHint: `paste the contents of ${CODEX_AUTH_PATH} — handy for a headless or shared box.`,
    tokenPh: "paste auth.json contents",
  },
  claude: {
    row: "Claude.ai sign-in · Max or Pro seat",
    chip: "Claude Code · Claude.ai sign-in",
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
        <input id="add-name" class="add-input" placeholder="Work · Personal · Late-night" value="${esc(add.name)}">
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
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}><span class="toggle-state" aria-hidden="true"><span class="when-on">on</span><span class="when-off">off</span></span><span class="sw"></span></label>`;
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
  const theme = s.theme === "dark" ? "dark" : "light";
  const strategy = s.strategy === "most_headroom" ? "most_headroom" : "soonest_back";
  return `<div class="app set-app theme-${theme}">
    <header class="set-head"><button class="set-back" data-action="settings-back" aria-label="back">‹</button><span class="set-title">settings</span><button class="set-done" data-action="settings-back">done</button></header>
    ${keyConfirmations(state)}<div class="set-body settings-body">
      <section class="set-sec"><h2 class="set-label"><span>01</span> switching</h2><div class="set-card">
        ${toggleRow("auto_switch", "auto-switch", "move when your seat runs out", s.auto_switch)}
        ${segBlock("choose the next seat", "", "set_strategy", strategy, STRATEGY_OPTS)}
        ${toggleRow("supervise_shell", "supervise terminals", "auto-switch codex and claude", s.supervise_shell !== false)}
        ${toggleRow("same_tool_only", "stay on the same tool", "Codex to Codex; Claude to Claude", s.same_tool_only)}
        ${toggleRow("notify", "notify after switching", "show which seat is ready", s.notify)}
        ${toggleRow("restart_app", "restart the Codex app", "refresh its account after a swap", s.restart_app)}
      </div></section>
      <section class="set-sec paid-settings"><h2 class="set-label"><span>02</span> paid keys</h2><div class="set-card">
        ${toggleRow("key_fallback", "allow paid use", "off stops sessions and new requests", s.key_fallback === true)}
        ${toggleRow("confirm_key_switch", "ask before using a key", "approve each paid handoff", s.confirm_key_switch !== false)}
      </div><p class="add-foot">paid use permits fallback and pinned terminals. sent turns may still bill. asking off permits eligible keys to spend without another prompt. this app does not cap spend.</p></section>
      <section class="set-sec"><h2 class="set-label"><span>03</span> appearance</h2><div class="set-card">
        ${segBlock("theme", "", "set_theme", theme, THEME_OPTS)}
        <div class="set-legend"><div class="legend-art">${instrumentArt("legend", 3, 4)}<span>the ring shows the share of subscription seats ready</span></div>
          <div class="set-legend-row">${doorMark({ door: "open" })}<span>a model is free</span></div>
          <div class="set-legend-row">${doorMark({ door: "shut" })}<span>every seat is resting</span></div>
          <div class="set-legend-row"><span class="dot dot--queued"></span><span>just switched you</span></div>
          <div class="set-legend-row"><span class="dot dot--needs-login"></span><span>a seat needs sign-in</span></div>
        </div></div></section>
      <div class="set-ver">ai guest list ${state?.app?.version ? `v${esc(state.app.version)}` : ""}${state?.app?.build ? ` / ${esc(state.app.build)}` : ""}</div>
    </div></div>`;
}

// --- popover ----------------------------------------------------------------------------------

export function buildHTML(state) {
  const theme = state?.settings?.theme === "dark" ? "dark" : "light";
  const keys = state?.keys || [];
  const paid = paidUseControl(state);
  const noReady = !subscriptionSeats(state).some((seat) => ["active", "ready"].includes(seat.status));
  const keyRack = keys.length ? `<section class="key-rack"><div class="g-head"><h2>paid keys</h2><span>${String(keys.length).padStart(2, "0")}</span><span class="g-meta">${state?.settings?.key_fallback === true ? "paid use enabled" : "paid use off"}</span></div>${keys.map(keySeatCard).join("")}</section>` : "";
  return `<div class="app theme-${theme}"><header class="top"><span class="brand"><span>ai</span> guest list</span><span class="top-caption">instrument</span><span class="top-actions">
    <details class="add-menu"><summary class="ibtn" title="add a seat or key" aria-label="add a seat or key">＋</summary><div class="add-menu-panel"><button data-action="add">add a subscription seat</button><button data-action="key-start">add an API key</button></div></details><button class="ibtn" data-action="settings" title="settings" aria-label="settings">⚙</button></span></header>
    ${keyConfirmations(state)}${paid || overview(state)}
    <div class="main-body">${noReady ? keyRack : ""}${supervisionBanner(state)}
      ${toolGroup("codex", state?.tools?.codex)}${toolGroup("claude", state?.tools?.claude)}${noReady ? "" : keyRack}
      ${state?.moved_note ? `<div class="event"><span>last switch</span> ${esc(state.moved_note.replaceAll(" · ", "; "))}</div>` : ""}
      <footer class="foot"><span>quota readings / local credentials</span><button class="link" data-action="quit">quit</button></footer>
    </div></div>`;
}

function paidUseControl(state) {
  const pins = state?.pinned_sessions || [];
  const running = (state?.running_key_seats || []).filter((id) => !pins.some((s) => (s.key_seat?.id || s.email) === id));
  const count = pins.length + running.length;
  if (!count) return "";
  const stopping = state?.settings?.key_fallback === false;
  return `<section class="paid-use-control" role="status" aria-label="paid sessions">
    <div class="paid-heading">${instrumentArt("paid", count, count)}<div><h1>${stopping ? "paid use is stopping…" : "a paid session is running."}</h1><div class="paid-count">${String(count).padStart(2, "0")} <span>paid session${count === 1 ? "" : "s"}</span></div></div></div>
    <div class="paid-session-list">${pinnedSessions(state)}${running.map((id) => {
      const key = state?.keys?.find((seat) => seat.id === id);
      return `<div class="paid-session"><div><strong>${esc(key?.label || id)}</strong><span class="session-facts">${esc(key?.harness || "")} / automatic fallback</span><span class="key-model">${esc(key?.model || "")}</span></div><button class="btn session-end" data-action="key-stop"${stopping ? " disabled" : ""}>stop</button></div>`;
    }).join("")}</div>
    <button class="stop-all" data-action="key-stop"${stopping ? " disabled" : ""}>${stopping ? "stopping paid use…" : "stop all paid use"}</button><p class="add-hint">stops new requests too. sent turns may still bill.</p>
  </section>`;
}

export function pinnedSessions(state) {
  return (state?.pinned_sessions || []).map((session) => {
    const seat = session.key_seat || {};
    return `<div class="paid-session"><div><strong>${esc(seat.label || session.email)}</strong><span class="session-facts">${esc(session.tool)} / terminal ${esc(session.pid)}</span><span class="key-model">${esc(seat.model)}</span></div>
      <button class="btn session-end" data-action="end-pinned-session" data-tool="${esc(session.tool)}" data-pin="${esc(session.pin)}"${session.end_requested ? " disabled" : ""}>${session.end_requested ? "ending…" : "end"}</button></div>`;
  }).join("");
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
  return `${KEY_PROVIDERS[seat.provider]?.name || seat.provider || "key"}${seat.region ? ` / ${seat.region}` : ""}`;
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
  // Keep cents and smaller published rates; never round a small charge to zero.
  return `$${number.toLocaleString("en-US", { useGrouping: false,
    ...(number > 0 && number < 0.01 ? { maximumSignificantDigits: 3 }
      : { minimumFractionDigits: 2, maximumFractionDigits: 6 }) })}`;
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
  return `${text ? `<div class="key-proof" role="status">${text}</div><div class="add-hint">checked ${esc(proof.checked_at)} / ${esc(proof.model)}</div>` : ""}
    ${seat.responses_verified === false && proof?.outcome !== "incompatible" && proof?.outcome !== "proven" ? `<div class="key-unproven">responses support unproven — this endpoint may not work</div>` : ""}`;
}

export function keySeatCard(seat) {
  const unproven = (seat.responses_verified === false || seat.last_proof?.outcome === "incompatible") && seat.harness !== "claude";
  const validation = seat.last_validation;
  return `<article class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}">
    <div class="key-seat-head"><strong>${esc(seat.label)}</strong><span>${esc(providerName(seat))} / ${esc(seat.harness)}</span></div>
    <div class="key-model">${esc(seat.model)}</div><p class="add-hint">paid per use; no app spend limit</p>
    ${keyProofStatus(seat)}${validation ? `<p class="key-check-status" role="status">${validation.operation_permitted === false ? "key check refused; check provider access" : validation.operation_permitted === true ? "account access checked; inference may still fail" : "key check incomplete; check provider access"}</p>` : ""}
    <div class="key-actions"><button class="btn" data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button><button class="btn" data-action="key-validate" data-id="${esc(seat.id)}">check key</button><button class="logout" data-action="key-remove" data-id="${esc(seat.id)}">remove</button></div>
    ${unproven ? `<div class="proof-line"><span>endpoint check sends a paid request; cost unknown.</span><button class="btn" data-action="key-prove" data-id="${esc(seat.id)}">check endpoint</button></div>` : ""}
  </article>`;
}

export function keyConfirmations(state, answering = new Set(), now = Date.now()) {
  const requests = (state?.pending_key_switches || []).filter((r) => r.status === "pending" && Date.parse(r.expires_at) > now);
  return `<div class="key-prompts" aria-live="polite">${requests.map((r, index) => {
    const pending = answering.has(r.id);
    const disabled = pending ? " disabled" : "";
    const seat = r.key_seat || {};
    const input = priceRate(r.price, "input"), output = priceRate(r.price, "output");
    const provider = KEY_PROVIDERS[seat.provider];
    const price = input === null && output === null
      ? `<p class="price-note">${esc(provider?.name || seat.provider || "this provider")} doesn’t publish prices in this catalog.${provider?.pricing ? ` <button class="pricing-link" data-action="key-pricing" data-provider="${esc(seat.provider)}">check pricing</button>` : " check your provider’s pricing."}</p>`
      : `<div class="consent-price"><span>input ${esc(formatPrice(input))}</span><span>output ${esc(formatPrice(output))}</span><span>USD / million tokens</span></div><p class="price-note">${esc(priceAge(r.price).replaceAll(" · ", "; "))}</p>`;
    return `<section class="key-confirm" aria-label="paid key confirmation"><div class="decision-heading">${instrumentArt(`consent-${index}`, 0, 1)}<div><h1>${r.pinned ? "pin this terminal to a paid key?" : "use a paid key to keep going?"}</h1><span class="expiry" data-reset-at="${esc(r.expires_at)}" data-clock-prefix="expires in">expires in ${fmtCountdown(r.expires_at, now)}</span></div></div>
      <div class="decision-facts"><strong>${esc(seat.label)}</strong><span>${esc(providerName(seat))}</span><span class="k-model">${esc(seat.model)}</span></div>
      ${price}<p class="k-fine">${r.pinned ? "this terminal stays on the key." : "your next turn uses the key."} no app spend cap. sent turns may bill.</p>
      ${seat.responses_verified === false ? `<p class="usage-error">endpoint support unproven; this may fail.</p>` : ""}
      <div class="k-acts"><button class="k-no" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${disabled}>not now</button><button class="k-go" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${disabled}>use the key</button></div>
      <div class="decision-status" role="status">${pending ? "saving your answer…" : "Esc declines"}</div></section>`;
  }).join("")}</div>`;
}

export function buildPaidKeyGate(state, flow) {
  const seat = state.keys?.find((key) => key.id === flow.id);
  const theme = state.settings?.theme === "dark" ? "dark" : "light";
  const disabled = flow.pending ? " disabled" : "";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="paid-key-back" title="back"${disabled}>‹</button><span class="set-title">paid use is off</span></header>
    <div class="set-body"><section class="set-sec paid-gate"><div class="decision-heading">${instrumentArt("gate", 0, 1)}<h1>use ${esc(seat?.label || flow.label)} in a new terminal?</h1></div>
      <p>real money can be spent. this enables paid use for pinned terminals and automatic fallback.</p><p class="add-hint">turning paid use off stops sessions. sent turns may still bill. this app does not cap spend.</p>
      ${flow.error ? `<p class="usage-error" role="alert">${esc(flow.error)}</p>` : ""}
      <div class="k-acts"><button class="k-no" data-action="paid-key-back"${disabled}>not now</button><button class="k-go" data-action="paid-key-enable"${disabled}>allow paid use and open</button></div><p role="status">${flow.pending ? "opening your terminal…" : ""}</p>
    </section></div></div>`;
}

// Render only these results on input: the filter field itself keeps focus and its caret.
export function buildModelResults(flow) {
  const catalog = flow.catalog || {};
  const priced = catalog.sort_key === "input_usd_per_million_tokens";
  const query = (flow.modelFilter || "").toLowerCase();
  const models = [...(catalog.models || [])].sort((a, b) => {
    if (!priced) return a.id.localeCompare(b.id);
    const av = priceRate(a.price, "input"), bv = priceRate(b.price, "input");
    return (av === null ? Infinity : Number(av)) - (bv === null ? Infinity : Number(bv)) || a.id.localeCompare(b.id);
  });
  const visible = models.filter((model) => model.id.toLowerCase().includes(query) || (model.display_name || "").toLowerCase().includes(query));
  const paid = visible.filter((model) => !freeModel(model));
  return `<div class="model-count" role="status"><span class="free-hidden">${paid.length}</span><span class="free-shown">${visible.length}</span> matches / ${models.length} in catalog</div>
    <div class="model-columns"><span>model / context</span>${priced ? `<span>input</span><span>output</span>` : ""}</div>
    <div class="model-list${priced ? "" : " model-list--unpriced"}">${visible.map((model) => `<button class="key-model-option${freeModel(model) ? " is-free" : ""}" data-action="key-model" data-model="${esc(model.id)}"><span class="model-identity"><span class="model-id">${esc(model.id)}</span><span class="model-context">${contextSize(model.context_window)}</span></span>${priced ? rateCell(priceRate(model.price, "input")) + rateCell(priceRate(model.price, "output")) : ""}</button>`).join("")}
    ${!paid.length ? `<p class="empty free-hidden">${visible.length ? "matches are free previews; show free models to see them." : "no matches; try another name or provider."}</p>` : ""}${!visible.length ? `<p class="empty free-shown">no matches; try another name or provider.</p>` : ""}</div>`;
}

export function buildModelPicker(flow) {
  const catalog = flow.catalog || {};
  const provider = KEY_PROVIDERS[flow.provider];
  const priced = catalog.sort_key === "input_usd_per_million_tokens";
  return `<section class="model-picker"><div class="search-region"><label for="key-model-filter">find your model</label><input class="add-input" id="key-model-filter" type="search" autofocus aria-label="filter models" placeholder="search model id or name" autocomplete="off" spellcheck="false" value="${esc(flow.modelFilter || "")}">
    <label class="free-control"><input id="show-free-models" type="checkbox"><span class="free-hidden">free models hidden <b>show</b></span><span class="free-shown">free models shown <b>hide</b></span></label>
    <p class="catalog-note">${priced ? "USD / million tokens; lowest input first" : "no machine-readable prices; sorted by model id"}</p>
    <p class="catalog-note">${catalog.source === "cache" ? "cached catalog" : "live catalog"}; ${esc(fmtUsageAge(catalog.fetched_at))}${catalog.potentially_stale ? "; may be stale" : ""}</p>
    ${catalog.error ? `<p class="usage-error" role="status">refresh failed; showing the last reading</p>` : ""}</div>
    <div id="key-model-results">${buildModelResults(flow)}</div><footer class="model-footer">${provider?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(flow.provider)}">provider pricing and budget controls</button>` : `<p>check pricing with your endpoint operator.</p>`}<p>estimates, not a spend cap. use provider budget limits.</p></footer></section>`;
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
      <span class="add-prov-tx"><span class="add-prov-name">${p.name}</span><span class="add-prov-sub">${p.harness === "claude" ? "claude code · messages" : "codex cli · responses"}${p.unverified ? " · unproven" : ""}</span></span><span class="add-chev">›</span></button>`).join("")}</div>
      <div class="add-foot">the same langdock key works for both routes: openai models through codex, claude models through claude code.</div>
      <div class="add-foot">openrouter offers live model prices across providers. direct openai, anthropic and langdock catalogs don't publish prices.</div></section>`;
  } else if (flow.step === "details") {
    body = `<div class="add-provcard"><span class="add-prov-tx"><span class="add-provcard-t">new ${esc(provider.name)} key seat</span><span class="add-provcard-s">${provider.harness === "claude" ? "claude code" : "codex cli"} sessions</span></span><button class="add-change" data-action="key-back">change</button></div>
      ${provider.regional ? `<section class="set-sec"><label class="set-label" for="key-region">region</label><div class="set-card"><select class="add-input" id="key-region">${["eu", "us", "global"].map((r) => `<option value="${r}"${flow.region === r ? " selected" : ""}>${r}</option>`).join("")}</select></div></section>` : ""}
      ${flow.provider === "openai_compatible" ? `<section class="set-sec"><label class="set-label" for="key-base-url">your endpoint's base url</label><div class="set-card"><input class="add-input" id="key-base-url" type="url" placeholder="https://your-host/v1" value="${esc(flow.base_url)}"></div><div class="add-hint">must support responses; chat completions alone won't work.</div></section>` : ""}
      <section class="set-sec"><label class="set-label" for="key-label">name this seat</label><div class="set-card"><input class="add-input" id="key-label" placeholder="work · late-night" value="${esc(flow.label)}"></div></section>
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
    <button class="set-back" data-action="key-back" title="back"${flow.pending ? " disabled" : ""}>‹</button><span class="set-title">${flow.step === "models" ? "model catalog" : "add a key"}</span>
    ${flow.pending ? "" : `<button class="add-cancel" data-action="key-cancel">cancel</button>`}</header>
    ${keyConfirmations(state)}<div class="set-body${flow.step === "models" ? " picker-body" : ""}">${flow.error ? `<div class="usage-error" role="alert">${esc(flow.error)}</div>` : ""}${body}</div></div>`;
}


// Original app/icon.svg geometry and paint, cropped into the instrument lens.
const ICON_ART = `<defs>
    <linearGradient id="v8-icon-tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffe6ad"/>
      <stop offset="55%" stop-color="#f7c668"/>
      <stop offset="100%" stop-color="#e7a23f"/>
    </linearGradient>
    <radialGradient id="v8-icon-glow" cx="50%" cy="40%" r="62%">
      <stop offset="0%" stop-color="#fff6df" stop-opacity=".95"/>
      <stop offset="100%" stop-color="#fff6df" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="v8-icon-ball" cx="38%" cy="30%" r="78%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="40%" stop-color="#bfe9df"/>
      <stop offset="100%" stop-color="#2f8a78"/>
    </radialGradient>
    <linearGradient id="v8-icon-leaf" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#f5eedd"/>
      <stop offset="100%" stop-color="#d8c8a8"/>
    </linearGradient>
    <clipPath id="v8-icon-ballClip"><circle cx="512" cy="470" r="172"/></clipPath>
    <clipPath id="v8-icon-tileClip"><rect x="92" y="92" width="840" height="840" rx="200"/></clipPath>
  </defs>

  <!-- squircle tile (macOS-style) -->
  <rect x="92" y="92" width="840" height="840" rx="200" fill="url(#v8-icon-tile)"/>
  <g clip-path="url(#v8-icon-tileClip)">
    <rect x="92" y="92" width="840" height="840" fill="url(#v8-icon-glow)"/>

    <!-- door swung open on the left -->
    <g transform="rotate(-7 250 520)">
      <rect x="150" y="150" width="120" height="740" rx="26" fill="url(#v8-icon-leaf)"/>
      <circle cx="244" cy="540" r="13" fill="#cf9b2e"/>
    </g>

    <!-- disco ball: string, glow, body, facets, highlight -->
    <line x1="512" y1="120" x2="512" y2="300" stroke="#b5905a" stroke-width="9"/>
    <circle cx="512" cy="470" r="200" fill="#fff2cf" opacity=".5"/>
    <circle cx="512" cy="470" r="172" fill="url(#v8-icon-ball)"/>
    <g clip-path="url(#v8-icon-ballClip)" stroke="#2c7e6d" stroke-width="7" opacity=".5" fill="none">
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
  </g>`;

function instrumentArt(id, value = 0, total = 1) {
  const fraction = total > 0 ? Math.min(100, Math.max(0, value / total * 100)) : 0;
  const prefix = `v8-${id}-`;
  const artwork = ICON_ART.replaceAll("v8-icon-", prefix);
  const ticks = Array.from({ length: 40 }, (_, i) => `<path d="M72 3v${i % 5 === 0 ? 8 : 4}" transform="rotate(${i * 9} 72 72)"/>`).join("");
  return `<svg class="instrument-art" viewBox="0 0 144 144" aria-hidden="true" focusable="false">
    <defs><clipPath id="${prefix}lens"><circle cx="72" cy="72" r="48"/></clipPath></defs>
    <g class="dial-ticks">${ticks}</g>
    <circle class="dial-bed" cx="72" cy="72" r="57"/>
    <circle class="dial-value" cx="72" cy="72" r="57" pathLength="100" stroke-dasharray="${fraction} 100" transform="rotate(-90 72 72)"/>
    <g clip-path="url(#${prefix}lens)"><svg x="24" y="24" width="96" height="96" viewBox="282 240 460 460">${artwork}</svg></g>
    <circle class="dial-rim" cx="72" cy="72" r="49"/>
    <path class="dial-index" d="M68 15h8l-4 6z"/>
  </svg>`;
}

function subscriptionSeats(state) {
  return ["codex", "claude"].flatMap((tool) => (state?.tools?.[tool]?.seats || []).map((seat) => ({ ...seat, tool })));
}

function overview(state) {
  const seats = subscriptionSeats(state);
  const ready = seats.filter((seat) => ["active", "ready"].includes(seat.status));
  const resting = seats.filter((seat) => ["resting", "queued"].includes(seat.status));
  const firstBack = resting.map((seat) => seat.limited_until).filter((date) => Number.isFinite(Date.parse(date))).sort((a, b) => Date.parse(a) - Date.parse(b))[0];
  const key = state?.keys?.[0];
  const login = seats.find(needsHello);
  let title = "you can keep working.";
  let detail = "subscription seats ready";
  let action = "";
  if (!seats.length && !key) {
    title = "add your first seat.";
    detail = "your guest list starts here";
    action = `<button class="primary" data-action="add">add a seat</button>`;
  } else if (!ready.length) {
    title = seats.length && resting.length === seats.length ? "everyone’s resting." : "no subscription is ready.";
    detail = firstBack ? `<span data-reset-at="${esc(firstBack)}" data-clock-prefix="next seat in" title="${esc(new Date(firstBack).toLocaleString())}">next seat in ${fmtCountdown(firstBack)}</span>` : "your work needs a seat";
    if (key) {
      action = `<button class="primary" data-action="key-terminal" data-id="${esc(key.id)}">use ${esc(key.label)}</button><span class="overview-note">paid per use${key.responses_verified === false ? "; endpoint unproven" : ""}</span>`;
    } else if (login) {
      action = `<button class="primary" data-action="add" data-tool="${esc(login.tool)}">sign in to continue</button>`;
    } else action = `<button class="primary" data-action="add">add a seat</button>`;
  }
  return `<section class="overview${ready.length ? "" : " overview--waiting"}" aria-label="current availability" role="status">
    <h1>${title}</h1>
    <div class="instrument">${instrumentArt("availability", ready.length, seats.length)}
      <div class="readout"><div class="readout-value">${String(ready.length).padStart(2, "0")}<span>/${String(seats.length).padStart(2, "0")}</span></div>
        <span class="readout-label">seats ready</span><div class="overview-detail">${detail}</div>${action}</div>
    </div>
  </section>`;
}

function freeModel(model) {
  return priceRate(model.price, "input") !== null && priceRate(model.price, "output") !== null
    && Number(priceRate(model.price, "input")) === 0 && Number(priceRate(model.price, "output")) === 0;
}
function contextSize(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "context not published";
  const compact = value >= 1000000 ? `${Number((value / 1000000).toFixed(2))}M`
    : value >= 1000 ? `${Number((value / 1000).toFixed(1))}k` : String(value);
  return `${compact} context`;
}
function rateCell(value) {
  if (value === null) return `<span class="model-rate unknown">—</span>`;
  if (Number(value) === 0) return `<span class="model-rate">free</span>`;
  const [whole, fraction = "00"] = formatPrice(value).split(".");
  return `<span class="model-rate decimal"><span>${esc(whole)}</span><span>.${esc(fraction)}</span></span>`;
}
