// Live glue: render state into the DOM and forward user actions to the Python bridge.
// All rendering logic lives in render.mjs (pure, unit-tested); this file is the thin wiring.
import { buildHTML, buildPicker, buildSaveSeat, buildPaste, buildSettings } from "./render.mjs";

const root = document.getElementById("root");
const overlay = document.createElement("div");
overlay.id = "overlay";
document.body.appendChild(overlay);
let state = { settings: { theme: "dark" }, tools: {} };

// --- bridge -----------------------------------------------------------------------------------
function send(action, payload = {}) {
  const msg = { action, ...payload };
  try {
    window.webkit.messageHandlers.agl.postMessage(msg);
  } catch (_e) {
    console.log("[agl] (no bridge)", msg);
  }
}

// Python → JS: the shell calls window.AGL.result(result) after every action.
window.AGL = {
  result(res) {
    res = typeof res === "string" ? JSON.parse(res) : res;
    if (res.settings_panel) screen = "settings"; // native entrypoint into the settings sub-view
    if (res.state) state = res.state;
    if (res.state || res.settings_panel) render(); // re-renders current screen (settings live-updates)
    if (res.error) flash(res.error);
    if (res.login) { overlay.innerHTML = buildPicker(res.login); }
    if (res.await_snapshot) { overlay.innerHTML = buildSaveSeat(res.tool); }
    if (res.celebrate) celebrate();
  },
  // legacy single-arg state push (kept for the poll path / older callers)
  update(next) { this.result({ state: typeof next === "string" ? JSON.parse(next) : next }); },
  celebrate,
};

function celebrate() {
  root.firstElementChild?.classList.add("celebrate");
  setTimeout(() => root.firstElementChild?.classList.remove("celebrate"), 600);
}
function flash(text) {
  overlay.innerHTML = `<div class="toast">${text}</div>`;
  setTimeout(() => { if (overlay.querySelector(".toast")) overlay.innerHTML = ""; }, 3000);
}
function closeOverlay() { overlay.innerHTML = ""; }

// which screen occupies the popover: "main" or the settings sub-view (spec §9.1 — a pushed
// sub-view on the same surface, never a modal).
let screen = "main";

function render() {
  root.innerHTML = screen === "settings" ? buildSettings(state) : buildHTML(state);
  // mirror the theme onto <body> so overlays (siblings of #root) get the same CSS vars
  const theme = (state.settings && state.settings.theme === "dark") ? "dark" : "light";
  document.body.className = "theme-" + theme;
}

// --- event delegation (whole document, so overlay buttons work too) ---------------------------
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) {
    // tapping a seat card body (not an action) expands/collapses it (spec §6)
    const card = e.target.closest("[data-card]");
    if (card) card.classList.toggle("expanded");
    return;
  }
  const { action, tool, email, key, command, value } = el.dataset;
  switch (action) {
    case "switch": send("switch", { tool, email }); break;
    case "remove": if (confirm(`wave goodbye to ${email}?`)) send("remove", { tool, email }); break;
    case "add": send("add", { tool }); break;
    case "login": closeOverlay(); send("login", { tool, command }); break;
    case "paste-open": overlay.innerHTML = buildPaste(tool); break;
    case "paste-save": {
      const blob = document.getElementById("paste-blob")?.value?.trim();
      if (blob) { closeOverlay(); send("paste", { tool, blob }); }
      break;
    }
    case "snapshot": closeOverlay(); send("snapshot", { tool }); break;
    case "headroom_install": send("headroom_install"); break;
    case "picker-close":
      // close on backdrop click or an explicit cancel/done button; ignore clicks inside the sheet
      if (el.classList.contains("backdrop") && e.target !== el) break;
      closeOverlay();
      break;
    case "settings": screen = "settings"; render(); break;
    case "settings-back": screen = "main"; render(); break;
    case "set_theme": send("set_theme", { value }); break;
    case "set_strategy": send("set_strategy", { value }); break;
    case "set_savings_level": send("set_savings_level", { value }); break;
    case "quit": send("quit"); break;
  }
});

// toggles fire 'change' (clicking the switch graphic doesn't bubble a data-action click)
document.addEventListener("change", (e) => {
  const inp = e.target.closest('input[data-action="toggle"]');
  if (inp) send("toggle", { key: inp.dataset.key, value: inp.checked });
});

// Esc pops the settings sub-view back to main (spec §9.1)
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && screen === "settings") { screen = "main"; render(); }
});

// initial paint + ask the native side for fresh state
render();
send("ready");
