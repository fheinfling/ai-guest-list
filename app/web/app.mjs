// Live glue: render state into the DOM and forward user actions to the Python bridge.
// All rendering logic lives in render.mjs (pure, unit-tested); this file is the thin wiring.
import { buildHTML, dotState } from "./render.mjs";

const root = document.getElementById("root");
let state = { settings: { theme: "dark" }, tools: {} };

// --- bridge -----------------------------------------------------------------------------------
// JS → Python via WKScriptMessageHandler named "agl". In a browser (tests/preview) we no-op.
function send(action, payload = {}) {
  const msg = { action, ...payload };
  try {
    window.webkit.messageHandlers.agl.postMessage(msg);
  } catch (_e) {
    console.log("[agl] (no bridge)", msg);
  }
}

// Python → JS: the app calls window.AGL.update(stateJson) after every engine change.
window.AGL = {
  update(next) {
    state = typeof next === "string" ? JSON.parse(next) : next;
    render();
  },
  celebrate() {
    root.firstElementChild?.classList.add("celebrate");
    setTimeout(() => root.firstElementChild?.classList.remove("celebrate"), 600);
  },
};

function render() {
  root.innerHTML = buildHTML(state);
  // report the menu-bar dot so the native side can update the status item glyph
  send("dot", dotState(state));
}

// --- event delegation -------------------------------------------------------------------------
root.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const { action, tool, email, key } = el.dataset;
  switch (action) {
    case "switch": send("switch", { tool, email }); break;
    case "remove": if (confirm(`wave goodbye to ${email}?`)) send("remove", { tool, email }); break;
    case "add": send("add", { tool }); break;
    case "headroom_install": send("headroom_install"); break;
    case "settings": send("settings"); break;
    case "quit": send("quit"); break;
    case "toggle": send("toggle", { key, value: el.checked }); break;
  }
});

// initial paint + ask the native side for fresh state
render();
send("ready");
