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
  const used = ["5h", "weekly"].map((w) => pct(seat, w)).filter((v) => v !== null);
  return used.length ? Math.round(100 - Math.max(...used)) : null;
}

export function fmtCountdown(iso, now = Date.now()) {
  if (!iso) return "";
  const ms = new Date(iso).getTime() - now;
  if (isNaN(ms) || ms <= 0) return "now";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h${mins % 60 ? ` ${mins % 60}m` : ""}`;
}

function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  let h = d.getHours(); const m = String(d.getMinutes()).padStart(2, "0");
  const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
  return `${h}:${m} ${ap}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// --- seat card --------------------------------------------------------------------------------

function statusBit(tool, seat) {
  switch (seat.status) {
    case "active": return `<span class="pill floor">on the floor</span>`;
    case "queued": return `<span class="pill queued">up next 💛</span>`;
    case "resting":
      return `<span class="mono rest-count">back in ${fmtCountdown(seat.limited_until)}</span>`;
    case "needs-login":
      return `<button class="btn rose" data-action="add" data-tool="${tool}">log in</button>`;
    default:
      return `<button class="btn switch" data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch</button>`;
  }
}

function bar(seat, win, label) {
  const v = pct(seat, win);
  const known = v !== null;
  return `<div class="usage"><span class="mono u-k">${label}</span>
    <span class="track"><span class="fill" style="width:${known ? v : 0}%"></span></span>
    <span class="mono u-v">${known ? `${Math.round(v)}%` : "—"}</span></div>`;
}

function seatCard(tool, seat) {
  const plan = seat.plan ? `<span class="mono chip">${esc(seat.plan)}</span>` : "";
  const reassure = seat.status === "resting"
    ? `<div class="reassure mono">taking a breather — back ${fmtClock(seat.limited_until)}</div>` : "";
  const credit = creditLeft(seat);
  const expanded = `<div class="expand">
    ${bar(seat, "weekly", "7d")}
    ${credit !== null ? `<div class="x-row"><span>credit left</span><span class="mono">${credit}%</span></div>` : ""}
    ${seat.last_on_floor ? `<div class="x-row"><span>last on the floor</span><span class="mono">${esc(fmtClock(seat.last_on_floor))}</span></div>` : ""}
    <button class="logout" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">log out ↗</button>
  </div>`;
  return `<div class="seat seat--${seat.status}" data-card data-tool="${tool}" data-email="${esc(seat.email)}">
    <div class="seat-row">
      <span class="dot dot--${seat.status}"></span>
      <span class="seat-name">${esc(seat.name)}</span>${plan}
      <span class="grow"></span>${statusBit(tool, seat)}
    </div>
    <div class="seat-email mono">${esc(seat.email)}</div>
    ${bar(seat, "5h", "5h")}
    ${reassure}
    ${expanded}
  </div>`;
}

function toolGroup(tool, t) {
  const meta = TOOL_META[tool];
  const seats = t?.seats || [];
  const n = seats.length;
  return `<section class="group" style="--accent:${meta.accent}">
    <div class="g-head">
      <span class="dot dot--accent"></span>
      <span class="g-name">${meta.label}</span><span class="g-count">· ${n} seat${n === 1 ? "" : "s"}</span>
      <span class="grow"></span><span class="mono g-meta">${esc(t?.plan_label || meta.plan)}</span>
    </div>
    ${seats.map((s) => seatCard(tool, s)).join("") || `<div class="empty">no seats yet</div>`}
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

// --- add-a-seat sub-view (spec §9) — a pushed screen like settings, NOT a modal ----------------
// Four steps: provider → details → connecting → done. The provider accent (teal Codex / coral
// Claude) rides a single `--accent` CSS var on the root, so step markup never branches on tool.
// `add` is the transient flow state owned by app.mjs: {step, provider, name, method, token}.

// Sign-in methods per provider:
//   codex  — browser sign-in, OR paste an auth.json blob (a textarea the engine installs directly).
//   claude — browser sign-in ONLY. A `claude setup-token` is a long-lived token for the
//            CLAUDE_CODE_OAUTH_TOKEN env var; it does NOT write the Keychain login our snapshot
//            pipeline reads, so there is no working paste/Terminal no-browser path for Claude today.
const ADD_COPY = {
  codex: {
    row: "ChatGPT sign-in · Business seat",
    chip: "Codex CLI · ChatGPT sign-in or auth.json",
    tokenHint: "paste your auth.json — handy for a headless or shared box.",
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
  </section>`;
}

function addDetailsStep(add) {
  const c = ADD_COPY[add.provider];
  const paste = addUsesPaste(add);
  const cta = paste ? "save the seat →" : "open sign-in →";
  // The method chooser shows only for a provider that has a no-browser option (codex). Claude is
  // browser-only, so it renders name + a single "open sign-in" CTA with no segmented control.
  let methodSection = "";
  if (addHasMethods(add.provider)) {
    const seg = (v, label) =>
      `<button class="sopt ${add.method === v ? "on" : ""}" data-action="add-method" data-value="${v}">${label}</button>`;
    const hint = add.method === "token" ? c.tokenHint : BROWSER_HINT;
    const tokenWrap = paste
      ? `<div class="add-tokenwrap"><textarea id="add-token" class="add-token mono"
           placeholder="${esc(c.tokenPh)}">${esc(add.token)}</textarea></div>`
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
  // A Terminal flow (browser sign-in OR claude setup-token) first WAITS for the user to finish in the
  // other window and tap "save my seat"; only then (add.pending) is a snapshot in flight. A codex
  // paste is always actively saving. Spinner + "saving…" copy show only when something is really in
  // flight — a lone spinner while we wait on the user would read as "hung".
  const terminalFlow = !addUsesPaste(add);        // a browser sign-in (codex or claude) waits on the user
  const saving = !terminalFlow || add.pending;
  const title = saving ? "saving your seat…" : "we opened your browser…";
  const sub = saving ? "tucking it away safely 💛" : "say hi over there and you're on the list 💛";
  const spin = saving ? `<div class="add-spin"></div>` : "";
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
  const body = step === "details" ? addDetailsStep(add)
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
      ${toggleRow("same_tool_only", "keep me on the same tool", "a Codex limit hops to your other Codex seat, never to Claude", s.same_tool_only)}
      ${toggleRow("notify", "tell me when it switches", "a gentle notification with who's on now", s.notify)}
      ${toggleRow("restart_app", "restart Codex after a swap", "Codex needs a fresh start · Claude picks it up live", s.restart_app)}
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
    <div class="main-body">
      ${controlBar({ icon: REFRESH, title: "auto-switch", sub: "next ready seat · soonest-reset wins",
                     key: "auto_switch", on: s.auto_switch, accentClass: "ic-auto" })}
      ${moved}
      ${toolGroup("codex", state?.tools?.codex)}
      ${toolGroup("claude", state?.tools?.claude)}
      <footer class="foot"><span>made with <span class="heart">💛</span></span>
        <button class="link" data-action="quit">quit</button></footer>
    </div>
  </div>`;
}
