// Pure render logic for the "ai guest list" popover — no DOM/bridge side effects, so it can be
// unit-tested under node. app.mjs wires these outputs into the live WKWebView DOM + bridge.

export const TOOL_META = {
  codex: { label: "Codex", sub: "chatgpt business", plan: "CHATGPT BUSINESS", accent: "var(--codex)" },
  claude: { label: "Claude", sub: "claude code", plan: "CLAUDE CODE", accent: "var(--claude)" },
};

const DOT_COPY = {
  switched: { emoji: "🔵", label: "just switched you" },
  hello: { emoji: "🌸", label: "needs a hello" },
  resting: { emoji: "🟡", label: "a seat's resting" },
  fresh: { emoji: "🟢", label: "everyone's fresh" },
};
const THEMES = new Set(["dark", "light"]);

export function dotState(state) {
  const key = state?.dot || dotKey(state);
  return { key, ...DOT_COPY[key] };
}

export function dotKey(state) {
  const tools = state?.tools || {};
  const seats = [...(tools.codex?.seats || []), ...(tools.claude?.seats || [])];
  if (state?.recently_switched) return "switched";
  if (seats.some(needsHello)) return "hello";
  if (seats.some((s) => s.active && s.limited)) return "resting";
  return "fresh";
}

export function needsHello(seat) {
  return (seat.usage || {}).error === "unauthorized";
}

export function pct(seat, win) {
  const v = seat?.usage?.windows?.[win]?.used_pct;
  return typeof v === "number" ? Math.max(0, Math.min(100, v)) : null;
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
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  return `in ${hrs}h${mins % 60 ? ` ${mins % 60}m` : ""}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// --- pieces -----------------------------------------------------------------------------------

function counts(state) {
  const seats = ["codex", "claude"].flatMap((t) => state?.tools?.[t]?.seats || []);
  const resting = seats.filter((s) => s.limited).length;
  const ready = seats.filter((s) => !s.limited).length;
  return { resting, ready };
}

function miniBar(label, value) {
  const known = value !== null;
  return `<div class="usage"><span class="usage-k">${label}</span>
    <span class="track"><span class="fill" style="width:${known ? value : 0}%"></span></span>
    <span class="usage-v">${known ? `${Math.round(value)}%` : "—"}</span></div>`;
}

function seatCard(tool, seat) {
  const state = seat.active ? "on-floor" : seat.limited ? "resting" : "ready";
  const plan = seat.plan ? `<span class="seat-plan">${esc(seat.plan)}</span>` : "";
  let status;
  if (seat.active) status = `<span class="status floor">on the floor</span>`;
  else if (seat.limited) status = `<span class="status rest">back ${fmtCountdown(seat.limited_until)}</span>`;
  else status = `<button class="status switch" data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}">switch</button>`;

  const note = seat.limited
    ? `<span class="seat-note rest">taking a breather 💛</span>`
    : seat.active ? `<span class="seat-note floor">keeping it warm 💚</span>` : "";

  return `<div class="seat-card ${state}">
    <div class="seat-row">
      <span class="seat-name">${esc(seat.name || seat.email)}</span>${plan}
      <span class="seat-spacer"></span>${status}
      <button class="seat-x" title="wave goodbye" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">×</button>
    </div>
    <div class="usages">${miniBar("5h", pct(seat, "5h"))}${miniBar("7d", pct(seat, "weekly"))}</div>
    <div class="seat-row bottom"><span class="seat-email">${esc(seat.email)}</span>${note}</div>
  </div>`;
}

function toolSection(tool, t) {
  const meta = TOOL_META[tool];
  const seats = t?.seats || [];
  const n = seats.length;
  return `<section class="tool" data-tool="${tool}" style="--tool:${meta.accent}">
    <div class="sec-head">
      <span class="sec-dot"></span><span class="sec-name">${meta.label}</span>
      <span class="sec-count">· ${n} seat${n === 1 ? "" : "s"}</span>
      <span class="sec-spacer"></span><span class="sec-plan">${esc(t?.plan_label || meta.plan)}</span>
    </div>
    ${seats.map((s) => seatCard(tool, s)).join("") || `<div class="empty">no seats yet</div>`}
    <button class="add-row" data-action="add" data-tool="${tool}">＋ add a seat</button>
  </section>`;
}

function pill(key, icon, title, sub, on, extra = "") {
  return `<label class="pill"><span class="pill-ic">${icon}</span>
    <span class="pill-tx"><span class="pill-t">${title}</span><span class="pill-s">${sub}</span></span>
    <input type="checkbox" data-action="toggle" data-key="${key}" ${on ? "checked" : ""}>
    <span class="sw"></span>${extra}</label>`;
}

// --- overlays (add-a-seat) --------------------------------------------------------------------

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
    <button class="pk-m" data-action="snapshot" data-tool="${esc(tool)}">save my seat 🎟️</button>
    <button class="link" data-action="picker-close">not yet</button></div></div>`;
}

export function buildPaste(tool) {
  return `<div class="backdrop"><div class="sheet"><h3>paste auth.json</h3><p class="sub">no browser dance</p>
    <textarea id="paste-blob" class="paste" placeholder="{ ... }"></textarea>
    <button class="pk-m" data-action="paste-save" data-tool="${esc(tool)}">save my seat 🎟️</button>
    <button class="link" data-action="picker-close">cancel</button></div></div>`;
}

// --- popover ----------------------------------------------------------------------------------

export function buildHTML(state) {
  const s = state?.settings || {};
  const dot = dotState(state);
  const hr = state?.headroom_available;
  const theme = THEMES.has(s.theme) ? s.theme : "light";
  const c = counts(state);
  const moved = state?.moved_note
    ? `<div class="moved">↪ ${esc(state.moved_note)}</div>` : "";
  return `<div class="app theme-${theme}">
    <header class="top">
      <span class="avatar"></span>
      <span class="brand-tx"><span class="brand">ai guest list</span>
        <span class="substatus">${c.resting} resting · ${c.ready} ready ${dot.emoji}</span></span>
      <span class="top-actions">
        <button class="ibtn" data-action="settings" title="settings">⋯</button>
        <button class="ibtn" data-action="add" data-tool="codex" title="add a seat">＋</button>
      </span>
    </header>
    ${pill("auto_switch", "↻", "auto-switch", "next ready seat · soonest-reset wins", s.auto_switch)}
    ${pill("headroom", "🍃", "slow sips", hr ? "compress context · save credit" : "install to enable",
           s.headroom && hr,
           hr ? "" : `<button class="hr-install" data-action="headroom_install" title="install headroom">get</button>`)}
    ${moved}
    ${toolSection("codex", state?.tools?.codex)}
    ${toolSection("claude", state?.tools?.claude)}
    <footer class="foot"><span>made with <span class="heart">💚</span> keeps your seat warm</span>
      <button class="link" data-action="quit">quit</button></footer>
  </div>`;
}
