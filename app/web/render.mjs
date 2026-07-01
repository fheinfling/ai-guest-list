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
    ? `<div class="reassure mono">taking a breather — back ${fmtClock(seat.limited_until)} 💛</div>` : "";
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

// funnel icon (three stacked bars narrowing) for Headroom — inline SVG, currentColor.
const FUNNEL = `<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><g fill="currentColor">
  <rect x="2" y="3" width="12" height="2" rx="1"/><rect x="4" y="7" width="8" height="2" rx="1"/>
  <rect x="6" y="11" width="4" height="2" rx="1"/></g></svg>`;
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

// --- overlays (add-a-seat, settings) ----------------------------------------------------------

export function buildPicker(plan) {
  const methods = (plan?.methods || []).map((m) =>
    m.command
      ? `<button class="pk-m" data-action="login" data-tool="${esc(plan.tool)}" data-command="${esc(m.command)}">${esc(m.label)}</button>`
      : `<button class="pk-m" data-action="paste-open" data-tool="${esc(plan.tool)}">${esc(m.label)}</button>`
  ).join("");
  return `<div class="backdrop" data-action="picker-close"><div class="sheet">
    <h3>${esc(plan?.title || "who's joining the list?")}</h3><p class="sub">how should i sign you in?</p>
    ${methods}<button class="link" data-action="picker-close">cancel</button></div></div>`;
}

export function buildSaveSeat(tool) {
  return `<div class="backdrop"><div class="sheet"><h3>signed in?</h3>
    <p class="sub">i'll keep your ${esc(tool)} seat warm</p>
    <button class="pk-m" data-action="snapshot" data-tool="${esc(tool)}">save my seat 💛</button>
    <button class="link" data-action="picker-close">not yet</button></div></div>`;
}

export function buildPaste(tool) {
  const hint = tool === "claude" ? "paste a setup-token (sk-ant-oat…)" : "paste auth.json";
  return `<div class="backdrop"><div class="sheet"><h3>${hint}</h3><p class="sub">no browser dance</p>
    <textarea id="paste-blob" class="paste mono" placeholder="${tool === "claude" ? "sk-ant-oat…" : "{ ... }"}"></textarea>
    <button class="pk-m" data-action="paste-save" data-tool="${esc(tool)}">save my seat 💛</button>
    <button class="link" data-action="picker-close">cancel</button></div></div>`;
}

// Settings sub-view building blocks (spec §9.1): grouped iOS-style cards, every row a subtitle,
// segmented controls full-width on their own line. Friendly labels are display-only — data-value
// carries the real persisted value the bridge validates.
const STRATEGY_OPTS = [
  { v: "most_headroom", label: "most headroom" },
  { v: "soonest_back", label: "soonest back" },
];
const SAVINGS_OPTS = [
  { v: "conservative", label: "easy" },
  { v: "moderate", label: "balanced" },
  { v: "aggressive", label: "max" },
];
const THEME_OPTS = [{ v: "light", label: "light" }, { v: "dark", label: "dark" }];

function strategyHint(strat) {
  return strat === "most_headroom"
    ? "i jump to whoever's got the most room left to breathe"
    : "if everyone's capped, i hold the seat that wakes up first — shortest wait wins";
}
function savingsHint(level) {
  if (level === "conservative") return "trims the obvious. safest, smallest savings";
  if (level === "aggressive") return "squeezes hardest. most tokens saved 💛";
  return "balanced — strong savings, full fidelity";
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

function fmtTokens(n) {
  // Threshold at 999_500, not 1e6: Math.round(999_600 / 1e3) is 1000, which would render "1000k"
  // instead of rolling over to "1.0M". Anything that rounds to >= 1000k belongs in the M bucket.
  return n >= 999500 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`;
}

// Lifetime totals from the proxy /stats endpoint, e.g. " · 12.7M tokens · $63 saved".
function hrLifetime(stats) {
  if (!stats) return "";
  const bits = [];
  // Number.isFinite (not truthy): a legitimately-zero total is still real info and must render, but
  // anything non-numeric is rejected — /stats is an untrusted loopback response and these values go
  // into innerHTML, so a stray string must never reach fmtTokens' `${n}` fallback.
  if (Number.isFinite(stats.tokens_saved)) bits.push(`${fmtTokens(stats.tokens_saved)} tokens`);
  if (Number.isFinite(stats.usd_saved)) bits.push(`$${Math.round(stats.usd_saved)} saved`);
  return bits.length ? ` · ${bits.join(" · ")}` : "";
}

// Settings sub-view (spec §9.1) — a full-panel pushed screen, NOT a modal. Renders into #root in
// place of the popover; back chevron / done / Esc pop back to main. Every change persists live.
export function buildSettings(state) {
  const s = state?.settings || {};
  const theme = s.theme === "dark" ? "dark" : "light";
  const strat = s.strategy === "most_headroom" ? "most_headroom" : "soonest_back";
  const level = SAVINGS_OPTS.some((o) => o.v === s.savings_level) ? s.savings_level : "conservative";
  const app = state?.app;
  const ver = app ? `v${app.version}${app.build && app.build !== "dev" ? ` · build ${app.build}` : ""}` : "";

  const autoSwitch = `<section class="set-sec"><span class="set-label">auto-switch</span>
    <div class="set-card">
      ${segBlock("when a seat runs out", strategyHint(strat), "set_strategy", strat, STRATEGY_OPTS)}
      ${toggleRow("same_tool_only", "keep me on the same tool", "a Codex limit hops to your other Codex seat, never to Claude", s.same_tool_only)}
      ${toggleRow("notify", "tell me when it switches", "a gentle notification with who's on now", s.notify)}
      ${toggleRow("restart_app", "restart Codex after a swap", "Codex needs a fresh start · Claude picks it up live", s.restart_app)}
    </div></section>`;

  const headroom = `<section class="set-sec"><span class="set-label">headroom</span>
    <div class="set-card">
      ${toggleRow("headroom", "wrap new sessions", "compress context so every limit stretches further", s.headroom)}
      ${s.headroom ? segBlock("savings level", savingsHint(level), "set_savings_level", level, SAVINGS_OPTS) : ""}
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
      ${headroom}
      ${appearance}
      <div class="set-ver">ai guest list ${ver}</div>
    </div>
  </div>`;
}

// --- popover ----------------------------------------------------------------------------------

export function buildHTML(state) {
  const s = state?.settings || {};
  const theme = s.theme === "dark" ? "dark" : "light";
  const hr = state?.headroom_available;
  const c = state?.counts || { resting: 0, ready: 0 };
  const moved = state?.moved_note ? `<div class="event mono">↪ ${esc(state.moved_note)}</div>` : "";
  const hrSub = !hr
    ? "install Headroom to enable"
    : state?.headroom_proxy_down
      // toggled on but the proxy isn't actually running (e.g. a restart failed) — say so rather than
      // claim it's wrapping; recovery restarts it, so this clears itself.
      ? "save-credit paused — reconnecting… 💛"
      : (state?.headroom_savings != null
          ? `wrapping Codex &amp; Claude · ~${state.headroom_savings}% fewer tokens (${state?.headroom_savings_measured ? "measured" : "est."})${hrLifetime(state?.headroom_stats)} 💛`
          : "wrapping Codex &amp; Claude 💛");
  return `<div class="app theme-${theme}">
    <header class="top">
      ${doorMark(state)}
      <span class="brand-tx"><span class="brand"><span class="ai">ai</span> guest list</span>
        <span class="substatus">${c.resting} resting · ${c.ready} ready 💛</span></span>
      <span class="top-actions">
        <button class="ibtn" data-action="settings" title="settings">⋯</button>
        <button class="ibtn" data-action="add" data-tool="codex" title="add a seat">＋</button>
      </span>
    </header>
    ${controlBar({ icon: REFRESH, title: "auto-switch", sub: "next ready seat · soonest-reset wins",
                   key: "auto_switch", on: s.auto_switch, accentClass: "ic-auto" })}
    ${controlBar({ icon: FUNNEL, title: "Headroom", chip: "COMPRESSES CONTEXT", sub: hrSub,
                   key: "headroom", on: s.headroom && hr, accentClass: "ic-hr" })}
    ${hr ? "" : `<button class="hr-install" data-action="headroom_install">install Headroom →</button>`}
    ${moved}
    ${toolGroup("codex", state?.tools?.codex)}
    ${toolGroup("claude", state?.tools?.claude)}
    <footer class="foot"><span>made with <span class="heart">💛</span></span>
      <button class="link" data-action="quit">quit</button></footer>
  </div>`;
}
