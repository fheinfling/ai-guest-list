// Live glue: render state into the DOM and forward user actions to the Python bridge.
// All rendering logic lives in render.mjs (pure, unit-tested); this file is the thin wiring.
import { buildHTML, buildSettings, buildAddSeat, addUsesPaste, reduceReply } from "./render.mjs";

const root = document.getElementById("root");
const overlay = document.createElement("div");   // toast surface only (siblings of #root)
overlay.id = "overlay";
document.body.appendChild(overlay);
let state = { settings: { theme: "dark" }, tools: {} };
let lastRev = -1;   // highest state.rev applied; a lower one is a stale snapshot and is ignored

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
    // All the async-correlation logic lives in the pure reduceReply() (unit-tested); this just
    // applies its decisions to the module state + DOM. The add sub-view holds unsaved typed input,
    // so a pure state push (the 180s poll) updates `state` but the reducer returns render=false —
    // no DOM swap, no focus/caret theft.
    const out = reduceReply({ screen, add, lastRev, state }, res);
    screen = out.screen; add = out.add; lastRev = out.lastRev; state = out.state;
    if (out.render) render();
    if (out.flash) flash(out.flash);
    if (out.celebrate) celebrate();
    if (out.closeFlow) setTimeout(() => {      // auto-close this flow's "done" screen; scoped by
      if (screen === "add" && add === out.closeFlow && add.step === "done") {   // object identity so
        screen = "main"; add = null; render();  // a stale timer can't close a later add.
      }
    }, 1600);
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

// which screen occupies the popover: "main", the settings sub-view, or the add-seat sub-view
// (spec §9 — pushed sub-views on the same surface, never a modal).
let screen = "main";
let renderedScreen = null;  // what the last render() actually drew — gates scroll preservation
// transient add-a-seat flow state; non-null only while screen === "add". Held here (not in `state`,
// which the poll overwrites) so typed name/token survive a background re-render.
let add = null;

function render() {
  // A background state push (the usage poll) re-renders whatever screen is up; carry the current
  // screen's body scroll position across the innerHTML swap so a poll doesn't snap it to the top.
  // Only when the screen is unchanged — navigating must start the new screen at the top.
  const prevBody = root.querySelector(".main-body, .set-body");
  const scrollTop = screen === renderedScreen && prevBody ? prevBody.scrollTop : 0;
  root.innerHTML = screen === "settings" ? buildSettings(state)
    : screen === "add" ? buildAddSeat(state, add)
    : buildHTML(state);
  renderedScreen = screen;
  if (scrollTop) {
    const nextBody = root.querySelector(".main-body, .set-body");
    if (nextBody) nextBody.scrollTop = scrollTop;
  }
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
  const { action, tool, email, value } = el.dataset;
  switch (action) {
    case "switch": send("switch", { tool, email }); break;
    case "remove": if (confirm(`wave goodbye to ${email}?`)) send("remove", { tool, email }); break;
    // add-a-seat sub-view (spec §9). Header ＋ (no tool) → provider step; per-provider add-row and
    // the needs-login "log in" button carry a tool → deep-link straight to details, provider preset.
    case "add":
      add = { step: tool ? "details" : "provider", provider: tool || null,
              name: "", method: "browser", token: "" };
      screen = "add"; render(); break;
    case "add-provider":       // picking a provider (re)starts details; clears the name (prototype)
      add = { step: "details", provider: tool, name: "", method: "browser", token: "" };
      render(); break;
    case "add-change": add.step = "provider"; render(); break;
    case "add-method": add.method = value; render(); break;   // typed token survives via add.token
    case "add-back": addBack(); break;
    case "add-cancel": screen = "main"; add = null; render(); break;   // add=null drops any pending op
    case "add-cta": {
      const name = add.name.trim();
      // Only a codex "token" paste installs an auth.json in-app; everything else (browser sign-in,
      // and claude's setup-token) launches an official flow in Terminal and waits on the user.
      if (addUsesPaste(add)) {
        const blob = add.token.trim();
        if (!blob) break;                      // empty field → no-op, not a spinner + error toast
        add.pending = true; add.step = "connecting"; render();   // paste in flight → saving spinner
        send("paste", { tool: add.provider, blob, ...(name ? { name } : {}) });
      } else {
        // launch the login/setup-token in Terminal and wait on the USER to finish + tap "save my
        // seat" — not a saving spinner yet (that's add.pending, set on save).
        add.step = "connecting"; render();
        send("login", { tool: add.provider, method: add.method });
      }
      break;
    }
    case "add-save": {         // connecting-step CTA (browser path) → the proven snapshot handshake
      if (add.pending) break;                  // a snapshot is already in flight — don't double-send
      add.pending = true; render();            // re-render disables the button (addConnectingStep)
      const name = add.name.trim();
      send("snapshot", { tool: add.provider, ...(name ? { name } : {}) });
      break;
    }
    case "settings": screen = "settings"; render(); break;
    case "settings-back": screen = "main"; render(); break;
    case "set_theme": send("set_theme", { value }); break;
    case "set_strategy": send("set_strategy", { value }); break;
    case "quit": send("quit"); break;
  }
});

// Back navigation within the add-seat sub-view (also used by Esc).
function addBack() {
  // A dispatched save is committed (acct.add will run) and can't be un-sent — so while it's in
  // flight, back is a no-op; the reply advances to done. Backing out only makes sense before saving.
  if (add?.pending) return;
  if (add?.step === "details") add.step = "provider";
  else if (add?.step === "connecting") add.step = "details";   // abandon an un-saved login
  else { screen = "main"; add = null; }        // provider or done → leave the flow
  render();
}

// Controlled inputs: mirror the add-seat fields into `add` on each keystroke so a background poll
// re-render (which re-emits value="${...}") reproduces exactly what's typed — no lost text.
document.addEventListener("input", (e) => {
  if (!add) return;
  if (e.target.id === "add-name") add.name = e.target.value;
  else if (e.target.id === "add-token") add.token = e.target.value;
});

// toggles fire 'change' (clicking the switch graphic doesn't bubble a data-action click)
document.addEventListener("change", (e) => {
  const inp = e.target.closest('input[data-action="toggle"]');
  if (inp) send("toggle", { key: inp.dataset.key, value: inp.checked });
});

// Esc pops a sub-view (spec §9): settings → main; add → one step back (like the chevron).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (screen === "settings") { screen = "main"; render(); }
  else if (screen === "add") addBack();
});

// initial paint + ask the native side for fresh state
render();
send("ready");
