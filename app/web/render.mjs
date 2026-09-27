// Shipping popover and pushed views: a verdict, an open seat roster, and native disclosures.
// Pure state-to-markup rendering and bridge reducers; clock updates only change existing text.

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

// Accessible door mark for the settings legend, matching the native menu-bar states.
// Open shows the room and disco ball; shut shows a cream leaf and gold knob.
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

// Clock ticks change text only: leave focus, native disclosures, and scroll position intact.
export function updateClockText(root, now = Date.now()) {
  for (const node of root.querySelectorAll("[data-expire-at]")) {
    node.textContent = `${Math.max(0, Math.ceil((Date.parse(node.dataset.expireAt) - now) / 1000))}s`;
  }
  for (const node of root.querySelectorAll("[data-usage-at]")) {
    node.textContent = fmtUsageAge(node.dataset.usageAt, now);
  }
  for (const node of root.querySelectorAll("[data-reset-at]")) {
    node.textContent = `${node.dataset.clockPrefix || "resets in"} ${fmtCountdown(node.dataset.resetAt, now)}`;
  }
  for (const node of root.querySelectorAll("[data-session-at]")) {
    node.textContent = fmtSessionAge(node.dataset.sessionAt, now).replace(" · ", "; ");
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


function bar(seat, win, label) {
  const used = pct(seat, win);
  const reset = seat?.usage?.windows?.[win]?.resets_at;
  const other = seat?.usage?.windows?.[win === "5h" ? "weekly" : "5h"]?.resets_at;
  // The seat's return is the authoritative countdown. Window resets appear only when distinct.
  const showReset = reset && reset !== seat.limited_until && reset !== other && Number.isFinite(Date.parse(reset));
  const left = used === null ? null : Math.round(100 - used);
  return `<div class="window"><div class="usage"><span>${label} window</span><span>${left === null ? "usage unknown" : left === 0 ? "exhausted" : `${left}% left`}</span></div>
    <div class="track${left === 0 ? " exhausted" : ""}" aria-hidden="true"><span class="fill" style="width:${left ?? 0}%"></span></div>
    ${showReset ? `<span class="usage-reset" data-reset-at="${esc(reset)}" data-clock-prefix="window resets in" title="${esc(new Date(reset).toLocaleString())}">window resets in ${fmtCountdown(reset)}</span>` : ""}</div>`;
}

function seatCard(tool, seat) {
  const fetched = seat.usage_fetched_at || seat.usage?.fetched_at || "";
  const reported = seat?.usage?.reported_windows;
  const weeklyOnly = tool === "codex" && Array.isArray(reported) && reported.includes("weekly") && !reported.includes("5h");
  const stateText = seat.status === "resting"
    ? `<span data-reset-at="${esc(seat.limited_until)}" data-clock-prefix="back in" title="${esc(fmtClock(seat.limited_until))}">back in ${fmtCountdown(seat.limited_until)}</span>`
    : seat.status === "active" ? (seat.in_session ? "on the floor" : "selected")
    : seat.status === "queued" ? "up next" : needsHello(seat) ? "sign-in needed" : "ready";
  const error = seat.usage?.error;
  const issue = ({ rate_limited: "updates throttled; retrying automatically", network: "connection unavailable; retrying automatically",
    token_expired: `open ${TOOL_META[tool].label} to refresh usage`, unauthorized: "sign in to refresh usage", forbidden: "check your subscription to refresh usage", no_token: "sign in to refresh usage" })[error] || (error ? "usage update failed; retrying automatically" : "");
  const action = needsHello(seat)
    ? `<button data-action="add" data-tool="${tool}">sign in again</button>`
    : ["ready", "queued"].includes(seat.status) ? `<button data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch to this seat</button>` : "";
  return `<article class="seat seat--${esc(seat.status)}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <details class="seat-disclosure"><summary class="seat-row"><span class="seat-name">${esc(seat.name)}</span><span class="seat-state">${stateText}</span><span class="chevron" aria-hidden="true">⌄</span></summary>
    <div class="expand"><p class="seat-email">${esc(seat.email)} ${planChip(seat.plan)}</p>
    ${weeklyOnly ? "" : bar(seat, "5h", "5h")}${bar(seat, "weekly", "7d")}
    <p class="support"><span data-usage-at="${esc(fetched)}">${fmtUsageAge(fetched)}</span>${seat.usage_stale || seat.usage_unknown ? "; last known reading" : ""}</p>
    ${issue ? `<p class="usage-error" role="status">${issue}</p>` : ""}
    ${seat.in_session ? `<p class="support">terminal attached<span data-session-at="${esc(seat.session_started_at)}">${fmtSessionAge(seat.session_started_at).replace(" · ", "; ")}</span></p>` : ""}
    ${seat.last_on_floor ? `<p class="support">last on the floor ${esc(fmtClock(seat.last_on_floor))}</p>` : ""}
    <div class="seat-actions">${action}<button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out</button></div>
    </div></details></article>`;
}

function toolGroup(tool, t, keys = []) {
  const seats = t?.seats || [];
  if (!seats.length && !keys.length) return "";
  return `<section class="group" style="--accent:${TOOL_META[tool].accent}"><h2 class="g-head"><span class="tool-marker tool-marker--${tool}" aria-hidden="true"></span>${TOOL_META[tool].label}</h2>
    ${seats.map((seat) => seatCard(tool, seat)).join("")}${keys.map(keySeatCard).join("")}</section>`;
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
    row: "ChatGPT subscription",
    chip: "Codex CLI with ChatGPT sign-in or auth.json",
    tokenHint: `paste the contents of ${CODEX_AUTH_PATH} — handy for a headless or shared box.`,
    tokenPh: "paste auth.json contents",
  },
  claude: {
    row: "Claude.ai subscription",
    chip: "Claude Code with Claude.ai sign-in",
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
      ? `<div class="add-tokenwrap"><textarea id="add-token" aria-label="auth.json contents" class="add-token mono"
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
        <input id="add-name" aria-label="seat name" class="add-input" placeholder="Work, Personal, Late-night" value="${esc(add.name)}">
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

// A toggle row: title and subtitle, explicit on/off text, and a 38×24 switch.
function toggleRow(key, title, subtitle, on) {
  return `<label class="set-toggle-row"><span class="set-tx"><span class="set-t">${title}</span><span class="set-s">${subtitle}</span></span>
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}>
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
  const theme = s.theme === "dark" ? "dark" : "light";
  const strat = s.strategy === "most_headroom" ? "most_headroom" : "soonest_back";
  const app = state?.app;
  const version = app ? `v${app.version}${app.build && app.build !== "dev" ? ` · build ${app.build}` : ""}` : "";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="settings-back" aria-label="back">‹</button><span class="set-title">settings</span><button class="set-done" data-action="settings-back">done</button></header>
    <div class="set-body"><section class="set-sec"><h1 class="set-label">switching</h1>
      ${toggleRow("auto_switch", "auto-switch", "move when a seat runs out", s.auto_switch)}
      ${segBlock("choose the next seat", "", "set_strategy", strat, STRATEGY_OPTS)}
      ${toggleRow("supervise_shell", "supervise terminals", "include codex and claude commands", s.supervise_shell !== false)}
      ${toggleRow("same_tool_only", "stay on the same tool", "Codex to Codex; Claude to Claude", s.same_tool_only)}
      ${toggleRow("notify", "notify when switched", "show which seat is ready", s.notify)}
      ${toggleRow("restart_app", "restart the Codex app", "refresh its account after a switch", s.restart_app)}
      <p class="support">with supervision off, only cx/cl auto-switch. terminals switch without restarting the desktop app.</p>
    </section><section class="set-sec paid-settings"><h2 class="set-label">paid keys</h2>
      ${toggleRow("key_fallback", "allow paid use", "off stops sessions and new requests", s.key_fallback === true)}
      ${toggleRow("confirm_key_switch", "ask before using", "approve each move to a paid key", s.confirm_key_switch !== false)}
      <p class="support">paid use allows fallback and pinned terminals to spend real money. sent turns may still bill after stopping. this app does not cap spend.</p>
      <p class="support">turning “ask before using” off allows eligible keys to spend without asking again.</p>
    </section><section class="set-sec"><h2 class="set-label">appearance</h2>
      ${segBlock("theme", "", "set_theme", theme, THEME_OPTS)}
      <div class="set-legend"><p>in your menu bar</p><div>${doorMark({door:"open"})}<span>a model is free</span></div><div>${doorMark({door:"shut"})}<span>every seat is resting</span></div>
      <p>gold: just switched you</p><p>rose: sign-in needed</p></div>
    </section><p class="set-ver">ai guest list ${esc(version)}</p></div></div>`;
}


function ambientArt() { return `<svg xmlns="http://www.w3.org/2000/svg" class="ambient-art" viewBox="265 252 500 500" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="ambient-tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffe6ad"/>
      <stop offset="55%" stop-color="#f7c668"/>
      <stop offset="100%" stop-color="#e7a23f"/>
    </linearGradient>
    <radialGradient id="ambient-glow" cx="50%" cy="40%" r="62%">
      <stop offset="0%" stop-color="#fff6df" stop-opacity=".95"/>
      <stop offset="100%" stop-color="#fff6df" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="ambient-ball" cx="38%" cy="30%" r="78%">
      <stop offset="0%" stop-color="#ffffff"/>
      <stop offset="40%" stop-color="#bfe9df"/>
      <stop offset="100%" stop-color="#2f8a78"/>
    </radialGradient>
    <linearGradient id="ambient-leaf" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#f5eedd"/>
      <stop offset="100%" stop-color="#d8c8a8"/>
    </linearGradient>
    <clipPath id="ambient-ballClip"><circle cx="512" cy="470" r="172"/></clipPath>
    <clipPath id="ambient-tileClip"><rect x="92" y="92" width="840" height="840" rx="200"/></clipPath>
  </defs>

  <!-- squircle tile (macOS-style) -->
  <rect x="92" y="92" width="840" height="840" rx="200" fill="url(#ambient-tile)"/>
  <g clip-path="url(#ambient-tileClip)">
    <rect x="92" y="92" width="840" height="840" fill="url(#ambient-glow)"/>

    <!-- door swung open on the left -->
    <g transform="rotate(-7 250 520)">
      <rect x="150" y="150" width="120" height="740" rx="26" fill="url(#ambient-leaf)"/>
      <circle cx="244" cy="540" r="13" fill="#cf9b2e"/>
    </g>

    <!-- disco ball: string, glow, body, facets, highlight -->
    <line x1="512" y1="120" x2="512" y2="300" stroke="#b5905a" stroke-width="9"/>
    <circle cx="512" cy="470" r="200" fill="#fff2cf" opacity=".5"/>
    <circle cx="512" cy="470" r="172" fill="url(#ambient-ball)"/>
    <g clip-path="url(#ambient-ballClip)" stroke="#2c7e6d" stroke-width="7" opacity=".5" fill="none">
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
</svg>`; }

function subscriptionSeats(state) {
  return ["codex", "claude"].flatMap((tool) => (state?.tools?.[tool]?.seats || []).map((seat) => ({ ...seat, tool })));
}
function paidCount(state) {
  const pinned = state?.pinned_sessions || [];
  return pinned.length + (state?.running_key_seats || []).filter((id) => !pinned.some((s) => (s.key_seat?.id || s.email) === id)).length;
}
function ambientVerdict(state) {
  const seats = subscriptionSeats(state);
  const ready = seats.filter((seat) => ["ready", "active"].includes(seat.status));
  let headline = "you can keep working.";
  let action = "", detail = "";
  if (!seats.length) {
    headline = "add a seat to get started.";
    action = `<button class="primary" data-action="add">add your first seat</button>`;
  } else if (!ready.length) {
    headline = seats.every((seat) => ["resting", "queued"].includes(seat.status)) ? "every subscription is resting." : "your seats need attention.";
    const key = (state?.keys || [])[0];
    const login = seats.find(needsHello);
    const reset = seats.map((seat) => seat.limited_until).filter((iso) => iso && Number.isFinite(Date.parse(iso))).sort((a,b) => Date.parse(a)-Date.parse(b))[0];
    if (key) {
      detail = `<p class="hero-support">${esc(key.label)} ${key.responses_verified === false || key.last_proof?.outcome === "incompatible" ? "may help with paid use; endpoint unproven." : "can keep you going with paid use."}</p>`;
      action = `<button class="primary" data-action="key-terminal" data-id="${esc(key.id)}">use ${esc(key.label)} in a terminal</button>`;
    } else if (login) {
      action = `<button class="primary" data-action="add" data-tool="${esc(login.tool)}">sign in to ${esc(login.name)}</button>`;
    }
    if (reset) detail += `<p class="hero-support" title="${esc(new Date(reset).toLocaleString())}"><span data-reset-at="${esc(reset)}" data-clock-prefix="next seat back in">next seat back in ${fmtCountdown(reset)}</span></p>`;
  }
  return `<section class="ambient-verdict" aria-label="current availability"><h1 role="status">${headline}</h1>
    <div class="reading" role="status"><span class="reading-value">${ready.length}</span><span class="reading-unit">subscription ${ready.length === 1 ? "seat" : "seats"} ready</span></div>
    <div class="hero-followup">${detail}${action}</div></section>`;
}

// --- popover ----------------------------------------------------------------------------------

export function buildHTML(state) {
  const theme = state?.settings?.theme === "dark" ? "dark" : "light";
  const seats = subscriptionSeats(state);
  const paid = paidCount(state);
  const ready = seats.some((seat) => ["active", "ready"].includes(seat.status));
  const mood = paid ? "spending" : ready ? "ready" : seats.length ? "resting" : "empty";
  const moved = state?.moved_note ? `<section class="event"><h2>last switch</h2><p>${esc(state.moved_note.replaceAll(" · ", ", "))}</p></section>` : "";
  const prompts = keyConfirmations(state);
  const asking = prompts.includes('class="key-confirm"');
  // The roster is the everyday glance; consent, spending and trouble get their own verdict.
  // Multiple active tools are real; never choose one on the user's behalf.
  const glance = ready && !paid && !asking;
  const roster = glance ? `<section class="roster-glance" aria-label="current availability">
    <h1 role="status">you can keep working.</h1>
    <table class="roster" aria-label="seats and remaining headroom">
      <colgroup><col class="roster-seat-col"><col class="roster-window-col"><col class="roster-window-col"></colgroup>
      <thead><tr><th scope="col">your seats</th><th scope="col">5-hour left</th><th scope="col">weekly left</th></tr></thead>
      <tbody>${[...seats].sort((a, b) => Number(b.status === "active") - Number(a.status === "active")).map((seat) => {
        const active = seat.status === "active";
        const status = needsHello(seat) ? "sign-in needed" : active ? "active" : seat.status === "resting" ? "resting" : seat.status === "queued" ? "up next" : "ready";
        const windows = ["5h", "weekly"].map((win) => {
          const reported = seat.usage?.reported_windows;
          const used = seat.usage_unknown || (Array.isArray(reported) && !reported.includes(win)) ? null : pct(seat, win);
          const left = Number.isFinite(used) ? Math.round(100 - used) : null;
          return `<td class="roster-value${left === null ? " roster-unknown" : ""}">${left === null ? "unknown" : `${left}%`}${seat.usage_stale && left !== null ? `<span class="roster-freshness">last known</span>` : ""}</td>`;
        }).join("");
        return `<tr${active ? ' class="roster-active"' : ""}><th scope="row"><span class="roster-identity">${TOOL_META[seat.tool].label} / ${esc(seat.name)}</span><span class="roster-meta">${seat.plan ? `(${esc(seat.plan)}) ` : ""}<span class="roster-status">${status}</span></span></th>${windows}</tr>`;
      }).join("")}${(state?.keys || []).map((key) => `<tr class="roster-key"><th scope="row"><span class="roster-identity">${esc(TOOL_META[key.harness]?.label || key.harness || "API key")} / ${esc(key.label)}</span>${key.model ? `<span class="roster-meta">${esc(key.model)}</span>` : ""}</th><td class="roster-key-terms" colspan="2">paid per token<span class="roster-meta">no app spend cap</span></td></tr>`).join("")}</tbody>
    </table>
  </section>` : "";
  // app/icon.svg's tile, glow and cream leaf, closed across the room. The knob keeps its
  // original radius and colour; only the open leaf's width/position changes to close the door.
  // doorKey and doorMark remain the sole, unchanged availability/mark helpers.
  const art = !paid && !asking && doorKey(state) === "shut" ? `<svg xmlns="http://www.w3.org/2000/svg" class="ambient-art roster-door" viewBox="92 92 840 840" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id="roster-tile" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#ffe6ad"/><stop offset="55%" stop-color="#f7c668"/><stop offset="100%" stop-color="#e7a23f"/></linearGradient>
      <radialGradient id="roster-glow" cx="50%" cy="40%" r="62%"><stop offset="0%" stop-color="#fff6df" stop-opacity=".95"/><stop offset="100%" stop-color="#fff6df" stop-opacity="0"/></radialGradient>
      <linearGradient id="roster-leaf" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#f5eedd"/><stop offset="100%" stop-color="#d8c8a8"/></linearGradient>
      <clipPath id="roster-tile-clip"><rect x="92" y="92" width="840" height="840" rx="200"/></clipPath>
    </defs>
    <rect x="92" y="92" width="840" height="840" rx="200" fill="url(#roster-tile)"/>
    <g clip-path="url(#roster-tile-clip)"><rect x="92" y="92" width="840" height="840" fill="url(#roster-glow)"/>
      <rect x="248" y="150" width="528" height="740" rx="26" fill="url(#roster-leaf)"/>
      <circle cx="716" cy="540" r="13" fill="#cf9b2e"/>
    </g>
  </svg>` : ambientArt();
  return `<div class="app ambient-app${glance ? " roster-app" : ""} theme-${theme} mood-${mood}">${art}
    <header class="top"><span class="brand">ai guest list</span><div class="top-actions"><details class="header-menu"><summary class="ibtn" aria-label="add a subscription seat or API key" title="add a seat or key">＋</summary><nav class="menu-panel"><button data-action="add">a subscription seat</button><button data-action="key-start">an API key</button></nav></details>
    <details class="header-menu"><summary class="ibtn" aria-label="app menu" title="app menu">⋯</summary><nav class="menu-panel"><button data-action="settings">settings</button><button data-action="quit">quit ai guest list</button></nav></details></div></header>
    <div class="ambient-stage">${prompts}${paid ? paidUseControl(state) : `<div class="availability">${glance ? roster : ambientVerdict(state)}</div>`}</div>
    <details class="guest-drawer"><summary class="drawer-handle"><span>${glance ? "seat options" : "guest list"}</span>${glance ? "" : `<span class="drawer-count">${seats.length + (state?.keys?.length || 0)} seats</span>`}<span class="chevron" aria-hidden="true">⌃</span></summary>
      <div class="main-body">${supervisionBanner(state)}${toolGroup("codex", state?.tools?.codex, (state?.keys || []).filter((k) => k.harness === "codex"))}${toolGroup("claude", state?.tools?.claude, (state?.keys || []).filter((k) => k.harness === "claude"))}
      ${!seats.length && !state?.keys?.length ? `<p class="empty">use ＋ above to add a subscription or API key.</p>` : ""}${moved}
      </div></details></div>`;
}

function paidUseControl(state) {
  const count = paidCount(state);
  if (!count) return "";
  const pinned = state?.pinned_sessions || [];
  const stopping = state?.settings?.key_fallback === false;
  const running = (state?.running_key_seats || []).filter((id) => !pinned.some((s) => (s.key_seat?.id || s.email) === id)).map((id) => {
    const seat = state?.keys?.find((key) => key.id === id);
    return `<article class="paid-session"><div><h2>${esc(seat?.label || id)}</h2><p>${esc(seat?.harness || "")} fallback</p><p class="key-model">${esc(seat?.model || "")}</p></div><button data-action="key-stop"${stopping ? " disabled" : ""}>${stopping ? "stopping…" : "stop all"}</button></article>`;
  }).join("");
  return `<section class="paid-use-control" aria-label="paid sessions"><h1 role="status">${stopping ? "paid use is stopping…" : "your keys are spending."}</h1><div class="reading" role="status"><span class="reading-value">${count}</span><span class="reading-unit">paid ${count === 1 ? "session" : "sessions"}</span></div>
    <div class="paid-session-list">${pinnedSessions(state)}${running}</div><button class="primary stop-all" data-action="key-stop"${stopping ? " disabled" : ""}>${stopping ? "stopping paid use…" : "stop all paid use"}</button>
    <p class="support billing-note">stops sessions and new paid requests.<br>sent turns may still bill.</p></section>`;
}

export function pinnedSessions(state) {
  return (state?.pinned_sessions || []).map((session) => {
    const key = session.key_seat || {};
    return `<article class="paid-session"><div><h2>${esc(key.label || session.email)}</h2><p>${esc(session.tool)} terminal ${esc(session.pid)}</p><p class="key-model">${esc(key.model)}</p></div>
      <button class="session-end" data-action="end-pinned-session" data-tool="${esc(session.tool)}" data-pin="${esc(session.pin)}"${session.end_requested ? " disabled" : ""}>${session.end_requested ? "ending…" : "end"}</button></article>`;
  }).join("");
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
  if (amount(value) === null) return "price unavailable";
  const number = Number(value);
  return `$${number.toLocaleString("en-US", { useGrouping: false,
    ...(number >= 1 ? { maximumFractionDigits: 2 } : { maximumSignificantDigits: 3 }) })}`;
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
  const unproven = (seat.responses_verified === false || seat.last_proof?.outcome === "incompatible") && seat.harness !== "claude";
  const checked = seat.last_validation;
  return `<article class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}"><details class="seat-disclosure"><summary class="seat-row"><span class="seat-name">${esc(seat.label)}</span><span class="seat-state">paid key</span><span class="chevron" aria-hidden="true">⌄</span></summary>
    <div class="expand"><p>${esc(providerName(seat))}</p><p class="key-model">${esc(seat.model)}</p><p class="support">paid per token; this app does not cap spend.</p>
    <button data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button>${keyProofStatus(seat)}
    ${checked ? `<p class="key-check-status" role="status">${checked.operation_permitted ? "key check passed; account access confirmed" : "key check was not permitted; check access with your provider"}</p>` : ""}
    ${unproven ? `<div class="proof-gate"><input class="local-check" type="checkbox" id="proof-${esc(seat.id)}"><label class="proof-open" for="proof-${esc(seat.id)}">check this endpoint</label><div class="proof-choice"><p>this sends a paid request. price unavailable here; check provider pricing. this app does not cap spend.</p><div class="equal-choices"><label class="choice" for="proof-${esc(seat.id)}">not now</label><button class="choice" data-action="key-prove" data-id="${esc(seat.id)}">send paid check</button></div></div></div>` : ""}
    <div class="seat-actions"><button data-action="key-validate" data-id="${esc(seat.id)}">check key</button><button class="logout" data-action="key-remove" data-id="${esc(seat.id)}">remove key</button></div></div></details></article>`;
}

export function keyConfirmations(state, answering = new Set(), now = Date.now()) {
  const requests = (state?.pending_key_switches || []).filter((r) => r.status === "pending" && Date.parse(r.expires_at) > now);
  return `<div class="key-prompts" aria-live="polite">${requests.map((r) => {
    const disabled = answering.has(r.id) ? " disabled" : "";
    const provider = r.key_seat?.provider;
    const input = priceRate(r.price, "input"), output = priceRate(r.price, "output");
    const unknown = input === null && output === null;
    const seconds = Math.max(0, Math.ceil((Date.parse(r.expires_at) - now) / 1000));
    return `<section class="key-confirm" aria-label="paid key confirmation"><h1>${r.pinned ? "use a paid key in this terminal?" : "use a paid key to keep going?"}</h1>
      <div class="decision-facts"><p class="decision-seat">${esc(r.key_seat?.label)}</p><p class="k-model">${esc(r.key_seat?.model)}</p>
      ${unknown ? `<p class="key-price">${esc(KEY_PROVIDERS[provider]?.name || provider || "this provider")} doesn't publish a price here. ${KEY_PROVIDERS[provider]?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(provider)}">check pricing</button>` : "check with your endpoint operator."}</p>` : `<p class="key-price">input ${esc(formatPrice(input))} / output ${esc(formatPrice(output))}<br>USD per million tokens</p>`}
      <p class="k-fine">paid per token. this app does not cap spend.</p>
      ${(state?.keys || []).some((key) => key.id === r.key_seat?.id && key.harness !== "claude" && (key.responses_verified === false || key.last_proof?.outcome === "incompatible")) ? `<p class="key-unproven">endpoint unproven; requests may still bill.</p>` : ""}</div>
      <p class="expiry"><span data-expire-at="${esc(r.expires_at)}">${seconds}s</span> to decide; Esc declines</p>
      <div class="k-acts"><button class="choice" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${disabled}>${disabled ? "answering…" : "not now"}</button><button class="choice" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${disabled}>${disabled ? "answering…" : "use the key"}</button></div>
    </section>`;
  }).join("")}</div>`;
}

export function buildPaidKeyGate(state, flow) {
  const seat = state.keys?.find((key) => key.id === flow.id);
  const theme = state.settings?.theme === "dark" ? "dark" : "light";
  const disabled = flow.pending ? " disabled" : "";
  return `<div class="app set-app theme-${theme}"><header class="set-head"><button class="set-back" data-action="paid-key-back" aria-label="back"${disabled}>‹</button><span class="set-title">paid use is off</span></header><div class="set-body"><section class="set-sec"><h1>allow paid use for ${esc(seat?.label || flow.label)}?</h1>
    <p>this opens a paid terminal and enables automatic key fallback. real money can be spent; this app does not cap spend.</p><p class="key-price">price unavailable here. ${KEY_PROVIDERS[seat?.provider]?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(seat.provider)}">check provider pricing</button>` : "check with your endpoint operator."}</p><p>turning paid use off stops sessions and new requests. sent turns may still bill.</p>
    ${flow.error ? `<p role="alert">${esc(flow.error)}</p>` : ""}<div class="equal-choices"><button class="choice" data-action="paid-key-back"${disabled}>not now</button><button class="choice" data-action="paid-key-enable"${disabled}>${flow.pending ? "opening…" : "allow and open"}</button></div></section></div></div>`;
}

// Render only these results on input: the filter field itself keeps focus and its caret.
export function buildModelResults(flow) {
  const catalog = flow.catalog || {};
  const priced = catalog.sort_key === "input_usd_per_million_tokens";
  const query = (flow.modelFilter || "").toLowerCase();
  const isFree = (m) => ["input", "output"].every((name) => priceRate(m.price, name) !== null && Number(priceRate(m.price, name)) === 0);
  const models = [...(catalog.models || [])].sort((a,b) => {
    if (!priced) return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    const av = priceRate(a.price, "input"), bv = priceRate(b.price, "input");
    return (av === null ? Infinity : Number(av)) - (bv === null ? Infinity : Number(bv));
  });
  const visible = models.filter((model) => model.id.toLowerCase().includes(query) || (model.display_name || "").toLowerCase().includes(query));
  const context = (n) => n >= 1000000 ? `${Number((n / 1000000).toFixed(2))}M` : n >= 1000 ? `${Number((n / 1000).toFixed(1))}k` : String(n);
  const decimal = (value) => {
    const price = formatPrice(value);
    if (!price.startsWith("$") || !price.includes(".")) return `<span>${price}</span>`;
    const [whole, fraction] = price.split(".");
    return `<span class="decimal"><span>${whole}</span><span>.${fraction}</span></span>`;
  };
  return `<p class="model-count" role="status"><span class="without-free">${visible.filter((m) => !isFree(m)).length}</span><span class="with-free">${visible.length}</span> matching models</p>
    <div class="model-columns"><span>model / context</span>${priced ? `<span>input / output<br>USD per million tokens</span>` : ""}</div>
    <div class="model-list${priced ? "" : " model-list--unpriced"}">${visible.map((model) => `<button class="key-model-option${isFree(model) ? " is-free" : ""}" data-action="key-model" data-model="${esc(model.id)}"><span class="model-identity"><span class="model-id">${esc(model.id)}</span>${model.context_window != null ? `<span class="model-context">${esc(context(model.context_window))} context</span>` : ""}</span>${priced ? `<span class="model-rates">${isFree(model) ? "free" : `${decimal(priceRate(model.price, "input"))}${decimal(priceRate(model.price, "output"))}`}</span>` : ""}</button>`).join("") || `<p class="empty">${query ? `no models match “${esc(flow.modelFilter)}”.` : "no models returned; go back to check your key and endpoint."}</p>`}
    ${visible.length && visible.every(isFree) ? `<p class="only-free">only free models match; use “show” above to include them.</p>` : ""}</div>`;
}

export function buildModelPicker(flow) {
  const catalog = flow.catalog || {};
  const provider = KEY_PROVIDERS[flow.provider];
  const priced = catalog.sort_key === "input_usd_per_million_tokens";
  return `<section class="model-picker"><div class="search-region"><label class="set-label" for="key-model-filter">find your model</label><input class="add-input" id="key-model-filter" type="search" autofocus aria-label="search models" placeholder="model id or name" autocomplete="off" spellcheck="false" value="${esc(flow.modelFilter || "")}">
    <label class="free-control"><input id="show-free-models" type="checkbox"><span class="without-free">free models hidden <b>show</b></span><span class="with-free">free models shown <b>hide</b></span></label>
    <p class="support">${priced ? "sorted by input price; unknown prices last" : "this provider publishes no machine-readable prices."}</p></div><div id="key-model-results">${buildModelResults(flow)}</div>
    <footer class="model-footer"><p class="support">${catalog.source === "cache" ? "cached" : "live"} ${priced ? "price estimates" : "catalog"}; ${esc(fmtUsageAge(catalog.fetched_at))}${catalog.potentially_stale ? "; over 24h old" : ""}</p>${catalog.error ? `<p class="usage-error">refresh failed; showing the last catalog.</p>` : ""}
    ${provider?.pricing ? `<button class="pricing-link" data-action="key-pricing" data-provider="${esc(flow.provider)}">provider pricing and budget controls</button>` : `<p class="support">ask your endpoint operator about pricing.</p>`}<p class="support">this app does not cap spend.</p></footer></section>`;
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
