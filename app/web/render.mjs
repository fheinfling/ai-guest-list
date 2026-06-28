// Pure render logic for the "ai guest list" popover — no DOM/bridge side effects, so it can be
// unit-tested under node. app.mjs wires these outputs into the live WKWebView DOM + bridge.

export const TOOL_META = {
  codex: { label: "Codex", sub: "chatgpt business", accent: "var(--codex)" },
  claude: { label: "Claude", sub: "claude code", accent: "var(--claude)" },
};

// The menu-bar dot — copy straight from the design.
export function dotState(state) {
  const tools = state?.tools || {};
  const seats = [...(tools.codex?.seats || []), ...(tools.claude?.seats || [])];
  if (state?.recently_switched) return { key: "switched", emoji: "🔵", label: "just switched you" };
  if (seats.some(needsHello)) return { key: "hello", emoji: "🌸", label: "needs a hello" };
  if (seats.some((s) => s.active && s.limited)) return { key: "resting", emoji: "🟡", label: "a seat's resting" };
  return { key: "fresh", emoji: "🟢", label: "everyone's fresh" };
}

// A seat "needs a hello" when its creds can't authenticate (usage came back unauthorized).
export function needsHello(seat) {
  return !!(seat.usage && seat.usage.error === "unauthorized");
}

export function pct(seat, win) {
  const w = seat?.usage?.windows?.[win];
  const v = w?.used_pct;
  return typeof v === "number" ? Math.max(0, Math.min(100, v)) : null;
}

export function creditLeft(seat) {
  // "credit left" = 100 - max(used across windows); null if unknown.
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

function bar(label, value) {
  const known = value !== null;
  const w = known ? value : 0;
  return `<div class="bar"><span class="bar-label">${label}</span>
    <span class="track"><span class="fill" style="width:${w}%"></span></span>
    <span class="bar-val mono">${known ? `${Math.round(value)}%` : "—"}</span></div>`;
}

function seatLine(tool, seat) {
  const cls = seat.active ? "on-floor" : seat.limited ? "resting" : "ready";
  const tag = seat.active ? "on the floor" : seat.limited ? `💤 ${fmtCountdown(seat.limited_until)}` : "ready";
  const action = seat.active ? "" : `data-action="switch" data-tool="${tool}" data-email="${esc(seat.email)}"`;
  return `<div class="seat ${cls}">
    <button class="seat-main" ${action} ${seat.active ? "disabled" : ""}>
      <span class="seat-name">${esc(seat.name)}</span>
      <span class="seat-tag">${tag}</span>
    </button>
    <button class="seat-x" title="wave goodbye" data-action="remove" data-tool="${tool}" data-email="${esc(seat.email)}">×</button>
  </div>`;
}

function toolCard(tool, t) {
  const meta = TOOL_META[tool];
  const seats = t?.seats || [];
  const active = seats.find((s) => s.active);
  const credit = active ? creditLeft(active) : null;
  const usage = active
    ? `<div class="bars">${bar("5h", pct(active, "5h"))}${bar("weekly", pct(active, "weekly"))}</div>`
    : `<div class="empty">no one on the floor — <button class="link" data-action="add" data-tool="${tool}">add a seat</button></div>`;
  const creditLine = credit !== null
    ? `<span class="credit mono">${credit}% credit left</span>` : "";
  return `<section class="card" data-tool="${tool}" style="--tool:${meta.accent}">
    <header class="card-head">
      <div><h2>${meta.label}</h2><p class="sub">${meta.sub}</p></div>
      ${creditLine}
    </header>
    ${usage}
    <div class="seats">${seats.map((s) => seatLine(tool, s)).join("")}</div>
    <button class="add-row" data-action="add" data-tool="${tool}">＋ add a seat</button>
  </section>`;
}

function toggle(id, label, on, hint = "") {
  return `<label class="toggle"><input type="checkbox" data-action="toggle" data-key="${id}" ${on ? "checked" : ""}>
    <span class="toggle-ui"></span><span class="toggle-label">${label}${hint ? `<small>${hint}</small>` : ""}</span></label>`;
}

// Full popover body for a given state.
export function buildHTML(state) {
  const s = state?.settings || {};
  const dot = dotState(state);
  const hr = state?.headroom_available;
  return `<div class="app theme-${s.theme || "dark"}">
    <header class="top">
      <span class="brand">ai guest list <span class="dot ${dot.key}">${dot.emoji}</span></span>
      <span class="dot-copy">${dot.label}</span>
    </header>
    <div class="toggles">
      ${toggle("auto_switch", "auto-switch", s.auto_switch, "switch by hand, or let auto do it for you")}
      ${toggle("headroom", "slow sips — make the credit last", s.headroom && hr, hr ? "compresses context to save tokens" : "install headroom to enable")}
      ${hr ? "" : `<button class="link hr-install" data-action="headroom_install">install headroom →</button>`}
    </div>
    ${toolCard("codex", state?.tools?.codex)}
    ${toolCard("claude", state?.tools?.claude)}
    <footer class="foot">
      <span>made with <span class="heart">♥</span></span>
      <span class="foot-actions">
        <button class="link" data-action="settings">settings</button> ·
        <button class="link" data-action="quit">quit</button>
      </span>
    </footer>
  </div>`;
}
