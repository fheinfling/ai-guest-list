// Pure render logic for the "ai guest list" popover (spec-driven). No DOM/bridge side effects →
// unit-testable under node. Type rule (spec §0/§3): humanist SANS for UI; MONO only for the
// wordmark, emails, %, countdowns, plan codes and section meta. Status = flat colored dots, never
// emoji; the only emoji is 💛.

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
    case "active":
      return seat.in_session
        ? `<span class="pill floor floor--live"><span class="live-dot" aria-hidden="true"></span>on the floor</span>`
        : `<span class="pill floor floor--idle">selected</span>`;
    case "queued": return `<span class="pill queued">up next 💛</span>`;
    case "resting":
      return `<span class="mono rest-count" data-reset-at="${esc(seat.limited_until)}" data-clock-prefix="back in">back in ${fmtCountdown(seat.limited_until)}</span>`;
    case "needs-login":
      return seat.entitlement_revoked
        ? `<button class="btn rose revoked" data-action="add" data-tool="${tool}">subscription ended — sign in again</button>`
        : `<button class="btn rose" data-action="add" data-tool="${tool}">log in</button>`;
    default:
      return `<button class="btn switch" data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch</button>`;
  }
}

function bar(seat, win, label) {
  const v = pct(seat, win);
  const known = v !== null;
  const lastKnown = seat?.usage_stale || seat?.usage_unknown;
  const reset = seat?.usage?.windows?.[win]?.resets_at;
  const resetTime = reset ? new Date(reset).getTime() : NaN;
  const timer = Number.isFinite(resetTime)
    ? `<div class="usage-reset mono" data-reset-at="${esc(reset)}" title="${esc(new Date(reset).toLocaleString())}">resets in ${fmtCountdown(reset)}</div>`
    : "";
  return `<div class="usage${lastKnown ? " usage--stale" : ""}"><span class="mono u-k">${label}</span>
    <span class="track"><span class="fill" style="width:${known ? v : 0}%"></span></span>
    <span class="mono u-v">${known ? `${Math.round(v)}%` : "—"}</span></div>${timer}`;
}

function seatCard(tool, seat) {
  const plan = planChip(seat.plan);
  const parkedSession = !seat.active && seat.in_session;
  const started = seat.session_started_at || "";
  const sessionDate = started && Number.isFinite(new Date(started).getTime())
    ? new Date(started).toLocaleString() : "";
  // A long-lived terminal is useful context, not a second claim to the live credentials.
  // Keep the action alongside it; the pair can wrap below the name in the narrow popover.
  const action = parkedSession
    ? `<span class="seat-actions"><span class="mono chip terminal-chip" title="${esc(sessionDate ? `session started ${sessionDate}` : "supervised terminal attached")}">in a terminal<span data-session-at="${esc(started)}">${fmtSessionAge(started)}</span></span>${statusBit(tool, seat)}</span>`
    : statusBit(tool, seat);
  const reported = seat?.usage?.reported_windows;
  const weeklyOnly = tool === "codex" && Array.isArray(reported) &&
    reported.includes("weekly") && !reported.includes("5h");
  const fetchedAt = seat.usage_fetched_at || seat.usage?.fetched_at || "";
  const error = seat.usage?.error;
  const lastKnown = seat.usage_stale || seat.usage_unknown;
  const issue = ({ rate_limited: "usage updates throttled · retrying automatically",
    network: "connection unavailable · retrying automatically",
    token_expired: `usage refresh pending · open ${tool === "codex" ? "Codex" : "Claude"} to refresh`,
    unauthorized: "usage unavailable · sign in to refresh",
    forbidden: "usage unavailable · check your subscription",
    no_token: "usage unavailable · sign in to refresh",
  })[error] || (error ? "usage update failed · retrying automatically" : "");
  const freshness = seat.status === "resting" ? "" :
    `<div class="usage-age mono${lastKnown ? " usage-age--stale" : ""}"><span data-usage-at="${esc(fetchedAt)}">${fmtUsageAge(fetchedAt)}</span>${lastKnown ? " · last known" : ""}</div>
    ${issue ? `<div class="usage-error">${issue}</div>` : ""}`;
  const reassure = seat.status === "resting"
    ? `<div class="reassure mono">taking a breather — back ${fmtClock(seat.limited_until)}</div>` : "";
  const credit = creditLeft(seat);
  const sessionStarted = sessionDate;
  const expanded = `<div class="expand">
    ${credit !== null ? `<div class="x-row"><span>credit left</span><span class="mono">${credit}%</span></div>` : ""}
    ${seat.last_on_floor ? `<div class="x-row"><span>last on the floor</span><span class="mono">${esc(fmtClock(seat.last_on_floor))}</span></div>` : ""}
    ${sessionStarted ? `<div class="x-row"><span>session started</span><span class="mono">${esc(sessionStarted)}</span></div>` : ""}
    <button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out ↗</button>
  </div>`;
  return `<div class="seat seat--${seat.status}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <div class="seat-row${parkedSession ? " seat-row--session" : ""}">
      <span class="dot dot--${seat.status}"></span>
      <span class="seat-name">${esc(seat.name)}</span>${plan}
      <span class="grow"></span>${action}
    </div>
    <div class="seat-email mono">${esc(seat.email)}</div>
    ${weeklyOnly ? "" : bar(seat, "5h", "5h")}
    ${bar(seat, "weekly", "7d")}
    ${freshness}
    ${reassure}
    ${expanded}
  </div>`;
}

function toolGroup(tool, t, keys = []) {
  const meta = TOOL_META[tool];
  const seats = t?.seats || [];
  const n = seats.length + keys.length;
  return `<section class="group" style="--accent:${meta.accent}">
    <div class="g-head">
      <span class="dot dot--accent"></span>
      <span class="g-name">${meta.label}</span><span class="g-count">· ${n} seat${n === 1 ? "" : "s"}</span>
      <span class="grow"></span><span class="mono g-meta">${esc(t?.plan_label || meta.plan)}</span>
    </div>
    ${seats.map((s) => seatCard(tool, s)).join("") + keys.map(keySeatCard).join("") || `<div class="empty">no seats yet</div>`}
    <button class="add-row" data-action="add" data-tool="${tool}">＋ add a seat</button>
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
  const cta = paste ? "save the seat →" : "open sign-in →";
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
  return `<label class="set-toggle-row">
    <span class="set-tx"><span class="set-t">${title}</span><span class="set-s">${subtitle}</span></span>
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}><span class="sw"></span></label>`;
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
  const ver = app ? `v${app.version}${app.build && app.build !== "dev" ? ` · build ${app.build}` : ""}` : "";

  const autoSwitch = `<section class="set-sec"><span class="set-label">auto-switch</span>
    <div class="set-card">
      ${segBlock("when a seat runs out", strategyHint(strat), "set_strategy", strat, STRATEGY_OPTS)}
      ${toggleRow("supervise_shell", "supervise terminal commands", "codex/claude auto-switch seats · off: only cx/cl do", s.supervise_shell !== false)}
      ${toggleRow("same_tool_only", "keep me on the same tool", "a Codex limit hops to your other Codex seat, never to Claude", s.same_tool_only)}
      ${toggleRow("key_fallback", "let a key take the floor", "off by default. on allows pinned key terminals and keys when subscription seats rest — real money can be spent. off prevents new paid use and stops a running paid session; the turn already sent may still bill", s.key_fallback === true)}
      ${toggleRow("confirm_key_switch", "ask before using a paid key", "ask me to approve each hop onto a key. turning this off lets eligible keys spend money without asking again", s.confirm_key_switch !== false)}
      ${toggleRow("notify", "tell me when it switches", "a gentle notification with who's on now", s.notify)}
      ${toggleRow("restart_app", "restart the Codex app after a swap", "the desktop app keeps the old account until it relaunches · terminals switch on their own", s.restart_app)}
    </div></section>`;

  const appearance = `<section class="set-sec"><span class="set-label">appearance</span>
    <div class="set-card">
      ${segBlock("theme", "", "set_theme", theme, THEME_OPTS)}
      <div class="set-legend">
        <span class="set-t">what the icon shows</span>
        <div class="set-legend-row">${doorMark({ door: "open" })}<span class="set-s">a model's free — come on in</span></div>
        <div class="set-legend-row">${doorMark({ door: "shut" })}<span class="set-s">every seat's resting</span></div>
        <div class="set-legend-row"><span class="dot dot--queued"></span><span class="set-s">just switched you</span></div>
        <div class="set-legend-row"><span class="dot dot--needs-login"></span><span class="set-s">a seat needs a hello</span></div>
      </div>
    </div></section>`;

  return `<div class="app set-app theme-${theme}">
    <header class="set-head">
      <button class="set-back" data-action="settings-back" title="back">‹</button>
      <span class="set-title">settings</span>
      <button class="set-done" data-action="settings-back">done</button>
    </header>
    <div class="set-body">
      ${autoSwitch}
      ${appearance}
      <div class="set-ver">ai guest list ${ver}</div>
    </div>
  </div>`;
}

// --- popover ----------------------------------------------------------------------------------

export function buildHTML(state) {
  const s = state?.settings || {};
  const theme = s.theme === "dark" ? "dark" : "light";
  const c = state?.counts || { resting: 0, ready: 0 };
  const moved = state?.moved_note ? `<div class="event mono">↪ ${esc(state.moved_note)}</div>` : "";
  return `<div class="app theme-${theme}">
    <header class="top">
      ${doorMark(state)}
      <span class="brand-tx"><span class="brand"><span class="ai">ai</span> guest list</span>
        <span class="substatus">${c.resting} resting · ${c.ready} ready</span></span>
      <span class="top-actions">
        <button class="ibtn" data-action="settings" title="settings">⋯</button>
        <button class="ibtn" data-action="add" title="add a seat">＋</button>
      </span>
    </header>
    ${keyConfirmations(state)}
    ${paidUseControl(state)}
    <div class="main-body">
      ${supervisionBanner(state)}
      ${controlBar({ icon: REFRESH, title: "auto-switch", sub: "next ready seat · soonest-reset wins",
                     key: "auto_switch", on: s.auto_switch, accentClass: "ic-auto" })}
      ${moved}
      ${pinnedSessions(state)}
      ${toolGroup("codex", state?.tools?.codex, (state?.keys || []).filter((k) => k.harness === "codex"))}
      ${toolGroup("claude", state?.tools?.claude, (state?.keys || []).filter((k) => k.harness === "claude"))}
      <button class="add-row" data-action="key-start">＋ add a key</button>
      <footer class="foot"><span>made with <span class="heart">💛</span></span>
        <button class="link" data-action="quit">quit</button></footer>
    </div>
  </div>`;
}

function paidUseControl(state) {
  if (!state?.running_key_seats?.length) return "";
  const stopping = state.settings?.key_fallback === false;
  return `<section class="key-confirm set-card paid-use-control" aria-label="stop paid use">
    <div class="k-q">${stopping ? "paid use is stopping…" : "a paid key is on the floor"}</div>
    <div class="k-fine">stop new paid requests and the running session. the turn already sent may still bill.</div>
    <div class="k-acts"><button class="k-go" data-action="key-stop"${stopping ? " disabled" : ""}>stop paid use</button></div>
  </section>`;
}

export function pinnedSessions(state) {
  return (state?.pinned_sessions || []).map((s) => {
    const seat = s.key_seat || {};
    return `<section class="seat seat--key pinned-session" aria-label="pinned paid session">
      <div class="seat-row"><span class="seat-name">${esc(seat.label)}</span><span class="mono chip">pinned · paid</span></div>
      <div class="key-detail">${esc(providerName(seat))} · ${esc(s.tool)} · terminal ${esc(s.pid)}</div>
      <div class="key-model mono">${esc(seat.model)}</div>
      <div class="add-hint">metered · paid per token. end here, then resume on a subscription seat.</div>
      <button class="btn switch" data-action="end-pinned-session" data-tool="${esc(s.tool)}" data-pin="${esc(s.pin)}"${s.end_requested ? " disabled" : ""}>${s.end_requested ? "ending…" : "end"}</button>
    </section>`;
  }).join("");
}

// Key providers mirror providers.py's harness gate: chat-only catalogs are not usable seats.
// No price table lives here. Every amount comes from a bridge price envelope.
export const KEY_PROVIDERS = {
  openai: { name: "openai", harness: "codex", pricing: "https://openai.com/api/pricing/" },
  anthropic: { name: "anthropic", harness: "claude", pricing: "https://www.anthropic.com/pricing" },
  openrouter: { name: "openrouter", harness: "codex", priced: true, pricing: "https://openrouter.ai/models" },
  langdock: { name: "langdock", harness: "codex", pricing: "https://www.langdock.com/pricing" },
  deepseek: { name: "deepseek", harness: "codex", pricing: "https://api-docs.deepseek.com/quick_start/pricing" },
  xai: { name: "xai", harness: "codex", priced: true, pricing: "https://docs.x.ai/docs/models" },
  groq: { name: "groq", harness: "codex", unverified: true, pricing: "https://groq.com/pricing" },
  openai_compatible: { name: "openai-compatible", harness: "codex", unverified: true },
};

function providerName(seat) {
  return `${KEY_PROVIDERS[seat.provider]?.name || seat.provider || "key"}${seat.region ? ` · ${seat.region}` : ""}`;
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
  return `live price estimate · ${age}${price?.potentially_stale ? " · over 24h old" : ""}`;
}
function priceText(price) {
  const input = priceRate(price, "input"), output = priceRate(price, "output");
  if (input === null && output === null) return "price unavailable";
  return `input ${formatPrice(input)}${input === null ? "" : "/Mtok"} · output ${formatPrice(output)}${output === null ? "" : "/Mtok"}`;
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
  return `${text ? `<div class="key-proof" role="status">${text}</div><div class="add-hint">checked ${esc(proof.checked_at)} · ${esc(proof.model)}</div>` : ""}
    ${seat.responses_verified === false && proof?.outcome !== "incompatible" && proof?.outcome !== "proven" ? `<div class="key-unproven">responses support unproven — this endpoint may not work</div>` : ""}`;
}

export function keySeatCard(seat) {
  // No running cost is shown. A money figure needs a price, and only OpenRouter and xAI publish
  // one for a provider that can actually be a key seat — so the card would read "unavailable" for
  // OpenAI, Anthropic and Langdock, which is worse than not offering the number at all.
  const unproven = (seat.responses_verified === false || seat.last_proof?.outcome === "incompatible") && seat.harness !== "claude";
  const validation = seat.last_validation;
  return `<div class="seat seat--key" data-card data-tool="${esc(seat.harness)}" data-email="key:${esc(seat.id)}">
    <div class="seat-row"><span class="dot dot--accent"></span><span class="seat-name">${esc(seat.label)}</span><span class="mono chip">api key</span></div>
    <div class="key-detail">${esc(providerName(seat))} · ${seat.harness === "claude" ? "claude code" : "codex cli"}</div>
    <div class="key-model mono">${esc(seat.model)}</div>
    <button class="btn switch" data-action="key-terminal" data-id="${esc(seat.id)}">use in new terminal</button>
    ${keyProofStatus(seat)}
    ${validation?.operation_permitted === false ? `<div class="usage-error">key check wasn't permitted${seat.responses_verified ? "" : " — inference access is still unproven"}</div>` : ""}
    <div class="expand"><div class="add-hint">paid per use · this app does not limit spend</div>
      ${unproven ? `<div class="add-hint">checking this endpoint sends one real request and costs a small amount of money.</div>
      <button class="btn switch" data-action="key-prove" data-id="${esc(seat.id)}">check this endpoint</button>` : ""}
      <button class="btn switch" data-action="key-validate" data-id="${esc(seat.id)}">check key</button>
      <button class="logout" data-action="key-remove" data-id="${esc(seat.id)}">remove key ↗</button>
    </div></div>`;
}

export function keyConfirmations(state, answering = new Set(), now = Date.now()) {
  const requests = (state?.pending_key_switches || []).filter((r) => r.status === "pending" && Date.parse(r.expires_at) > now);
  return `<div class="key-prompts" aria-live="polite">${requests.map((r) => {
    const disabled = answering.has(r.id) ? " disabled" : "";
    const seat = state?.keys?.find((k) => k.id === r.key_seat?.id);
    const previous = state?.tools?.[r.tool]?.seats?.find((s) => s.email === r.from_seat?.id);
    const reset = fmtClock(previous?.limited_until).replace(" ", "").toLowerCase();
    const from = r.from_seat?.label || r.from_seat?.id || "the current seat";
    const input = priceRate(r.price, "input"), output = priceRate(r.price, "output");
    const fare = input === null && output === null ? "price unavailable"
      : `${esc(formatPrice(input))}<span class="unit" style="font-size:13px"> / ${esc(formatPrice(output))}</span>`;
    const priceHint = priceText(r.price) + (input === null && output === null ? "" : ` · ${priceAge(r.price)}`);
    const accent = TOOL_META[r.tool]?.accent || TOOL_META.codex.accent;
    return `<section class="key-confirm set-card" aria-label="paid key confirmation" style="--accent:${accent}">
      <div class="k-q">${r.pinned ? "pin this terminal to a paid key?" : "use a paid key to keep going?"}</div>
      <span class="fare" title="${esc(priceHint)}">${fare}</span>
      <div class="unit">input / output per million tokens</div>
      <div class="quiet-meta">${esc(r.key_seat?.label)} · ${esc(providerName(r.key_seat || {}))}<br><span class="k-model">${esc(r.key_seat?.model)}</span></div>
      ${seat ? keyProofStatus(seat) : ""}
      <div class="k-fine">${r.pinned ? "only this terminal will use the key" : `${esc(from)} is resting${reset ? ` until ${esc(reset)}` : ""}`}. this app doesn't cap spend.</div>
      <div class="k-acts">
        <button class="k-no" data-action="key-answer" data-id="${esc(r.id)}" data-approved="false"${disabled}>not now</button>
        <button class="k-go" data-action="key-answer" data-id="${esc(r.id)}" data-approved="true"${disabled}>use the key</button>
      </div></section>`;
  }).join("")}</div>`;
}

export function buildModelPicker(flow) {
  const catalog = flow.catalog || {};
  const provider = KEY_PROVIDERS[flow.provider];
  const priced = catalog.sort_key === "input_usd_per_million_tokens";
  const models = [...(catalog.models || [])].sort((a, b) => {
    if (!priced) return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    const av = priceRate(a.price, "input"), bv = priceRate(b.price, "input");
    return (av === null ? Infinity : Number(av)) - (bv === null ? Infinity : Number(bv));
  });
  const message = priced ? "sorted by input $/Mtok · cheap to expensive · unknown input prices last"
    : provider?.priced ? "prices unavailable from this catalog · sorted by model id"
    : "this provider does not publish machine-readable prices · sorted by model id";
  return `<section class="set-sec"><span class="set-label">pick a model</span>
    <div class="add-hint">${message}</div>
    ${catalog.source === "cache" ? `<div class="add-hint">cached live catalog · ${esc(fmtUsageAge(catalog.fetched_at))}${catalog.potentially_stale ? " · over 24h old" : ""}</div>` : ""}
    ${catalog.error ? `<div class="usage-error">couldn't refresh the catalog — showing the last reading</div>` : ""}
    <div class="set-card">${models.map((model) => `<button class="add-prov key-model-option" data-action="key-model" data-model="${esc(model.id)}">
      <span class="add-prov-tx"><span class="key-model mono">${esc(model.id)}</span>
      ${model.display_name && model.display_name !== model.id ? `<span class="add-prov-sub">${esc(model.display_name)}</span>` : ""}
      ${priced ? priceHTML(model.price) : ""}
      ${model.context_window != null ? `<span class="add-hint">${esc(model.context_window)} token context</span>` : ""}</span><span class="add-chev">›</span>
    </button>`).join("") || `<div class="add-method add-hint">no models returned. go back to check this key and endpoint.</div>`}</div>
    ${provider?.pricing ? `<a class="add-hint" href="${provider.pricing}" data-action="key-pricing" data-provider="${flow.provider}">provider pricing and budget controls ↗</a>` : `<div class="add-hint">check your endpoint's pricing and budget controls with its operator.</div>`}
    <div class="add-foot">prices are estimates. use your provider's budget controls for limits — this app does not limit spend.</div>
  </section>`;
}

export function keyRequest(flow) {
  return { provider: flow.provider, secret: flow.secret.trim(), allow_unverified: flow.allow_unverified === true,
    ...(flow.provider === "langdock" ? { region: flow.region } : {}),
    ...(flow.provider === "openai_compatible" ? { base_url: flow.base_url.trim() } : {}) };
}

// Native echoes only our request id/action, never the secret. Polls and abandoned-flow replies
// cannot advance a later flow, even when both use the same provider.
export function reduceKeyReply(flow, res) {
  if (!flow?.pending || flow.pending !== res.key_request_id) return false;
  flow.pending = null;
  if (res.key_action === "models_list") {
    if (res.ok && Array.isArray(res.models)) { flow.catalog = res; flow.step = "models"; }
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
      <div class="add-foot">openrouter offers live model prices across providers. direct openai, anthropic and langdock catalogs don't publish prices.</div></section>`;
  } else if (flow.step === "details") {
    body = `<div class="add-provcard"><span class="add-prov-tx"><span class="add-provcard-t">new ${esc(provider.name)} key seat</span><span class="add-provcard-s">${provider.harness === "claude" ? "claude code" : "codex cli"} sessions</span></span><button class="add-change" data-action="key-back">change</button></div>
      ${flow.provider === "langdock" ? `<section class="set-sec"><label class="set-label" for="key-region">region</label><div class="set-card"><select class="add-input" id="key-region">${["eu", "us", "global"].map((r) => `<option value="${r}"${flow.region === r ? " selected" : ""}>${r}</option>`).join("")}</select></div></section>` : ""}
      ${flow.provider === "openai_compatible" ? `<section class="set-sec"><label class="set-label" for="key-base-url">your endpoint's base url</label><div class="set-card"><input class="add-input" id="key-base-url" type="url" placeholder="https://your-host/v1" value="${esc(flow.base_url)}"></div><div class="add-hint">must support responses; chat completions alone won't work.</div></section>` : ""}
      <section class="set-sec"><label class="set-label" for="key-label">name this seat</label><div class="set-card"><input class="add-input" id="key-label" placeholder="work · late-night" value="${esc(flow.label)}"></div></section>
      <section class="set-sec"><label class="set-label" for="key-secret">paste your api key</label><div class="set-card"><input class="add-input mono" id="key-secret" type="password" autocomplete="off" spellcheck="false" value="${esc(flow.secret)}"></div><div class="add-hint">sent only to the provider you chose; saved in your Mac's keychain.</div></section>
      ${provider.unverified ? `<section class="set-sec"><span class="set-t">this endpoint is unproven</span><div class="add-hint">we cannot promise its responses endpoint works with codex, even if it lists models. requests may still cost money.</div><label class="key-ack"><input id="key-ack" type="checkbox"${flow.allow_unverified ? " checked" : ""}> i understand it may not work, and want to try this endpoint</label></section>` : ""}
      <button class="add-cta" data-action="key-discover">pick a model →</button>`;
  } else if (flow.step === "connecting") {
    body = `<div class="add-center"><div class="add-spin"></div><div class="add-h">${flow.operation === "key_add" ? "saving your key seat…" : "looking up models…"}</div><div class="add-sub">${flow.operation === "key_add" ? "tucking the key away safely 💛" : "asking only the provider you chose"}</div></div>`;
  } else if (flow.step === "models") {
    body = buildModelPicker(flow);
  } else if (flow.step === "review") {
    const model = flow.catalog?.models?.find((m) => m.id === flow.model);
    body = `<section class="set-sec"><span class="set-label">a seat for ${esc(flow.label)}</span><div class="set-card add-method"><span class="set-t">${esc(providerName(flow))}</span><div class="key-model mono">${esc(flow.model)}</div>${priceHTML(model?.price)}</div>
      <div class="add-foot">saving a key does not start a paid session. allow keys to take the floor in settings when you're ready.</div>
      <div class="add-foot">use your provider's budget controls for limits — this app does not cap spend.</div></section><button class="add-cta" data-action="key-save">save the key seat →</button>`;
  } else {
    body = `<div class="add-center add-center--done"><div class="add-heart">💛</div><div class="add-welcome">welcome, ${esc(flow.label)}</div><div class="add-sub">your key seat's saved</div>
      ${flow.savedSeat?.last_validation?.operation_permitted === false ? `<div class="usage-error">the key check wasn't permitted. check access with your provider before using it.</div>` : ""}
      <button class="add-cta" data-action="key-cancel">back to the guest list</button></div>`;
  }
  return `<div class="app set-app add-app theme-${theme}" style="--accent:${accent}"><header class="set-head">
    <button class="set-back" data-action="key-back" title="back"${flow.pending ? " disabled" : ""}>‹</button><span class="set-title">add a key</span>
    ${flow.pending ? "" : `<button class="add-cancel" data-action="key-cancel">cancel</button>`}</header>
    ${keyConfirmations(state)}<div class="set-body">${flow.error ? `<div class="usage-error" role="alert">${esc(flow.error)}</div>` : ""}${body}</div></div>`;
}
