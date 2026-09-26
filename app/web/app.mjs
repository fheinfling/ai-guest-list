// Live glue: render state into the DOM and forward user actions to the Python bridge.
// All rendering logic lives in render.mjs (pure, unit-tested); this file is the thin wiring.
import { buildHTML, buildSettings, buildAddSeat, addUsesPaste, reduceReply, updateClockText, buildAddKey, KEY_PROVIDERS, keyRequest, reduceKeyReply, keyConfirmations } from "./render.mjs";

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
    console.log("[agl] (no bridge)", action); // never log a pasted key or auth blob
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
    const inKeyFlow = screen === "add-key";
    if (inKeyFlow) out.screen = screen; // native settings requests must not discard a pasted key
    screen = out.screen; add = out.add; lastRev = out.lastRev; state = out.state;
    const keyChanged = reduceKeyReply(keyFlow, res);
    if (res.key_action === "answer_key_switch") answering.delete(res.key_target_id);
    if (res.key_action === "key_prove") proving.delete(res.key_target_id);
    if (keyChanged || (out.render && !inKeyFlow)) render();
    else refreshKeyPrompts(); // surface consent without replacing inputs or stealing focus
    if (out.flash && !res.key_request_id) flash(out.flash);
    if (res.key_action === "key_validate" && res.ok) flash(res.validation?.operation_permitted
      ? "key check passed — this checks account access only" : "key check wasn't permitted — check access with your provider");
    if (out.celebrate) celebrate();
    if (out.closeFlow) setTimeout(() => {      // auto-close this flow's "done" screen; scoped by
      if (screen === "add" && add === out.closeFlow && add.step === "done") {   // object identity so
        screen = "main"; add = null; render();  // a stale timer can't close a later add.
      }
    }, 1600);
  },
  // legacy single-arg state push (kept for the poll path / older callers)
  update(next) { this.result({ state: typeof next === "string" ? JSON.parse(next) : next }); },
  // Native popover lifecycle: tick cached countdowns only while this surface is actually visible.
  // This never asks the bridge for data and never touches lastRev.
  setVisible(visible) { setPopoverVisible(Boolean(visible)); },
  celebrate,
};

function celebrate() {
  root.firstElementChild?.classList.add("celebrate");
  setTimeout(() => root.firstElementChild?.classList.remove("celebrate"), 600);
}
function flash(text) {
  overlay.innerHTML = '<div class="toast"></div>';
  overlay.firstElementChild.textContent = text;
  setTimeout(() => { if (overlay.querySelector(".toast")) overlay.innerHTML = ""; }, 3000);
}

// which screen occupies the popover: "main", the settings sub-view, or the add-seat sub-view
// (spec §9 — pushed sub-views on the same surface, never a modal).
let screen = "main";
let renderedScreen = null;  // what the last render() actually drew — gates scroll preservation
// transient add-a-seat flow state; non-null only while screen === "add". Held here (not in `state`,
// which the poll overwrites) so typed name/token survive a background re-render.
let add = null;
let keyFlow = null;
let keySequence = 0;
const answering = new Set();
const proving = new Set();
let clockTimer = null;

function setPopoverVisible(visible) {
  if (visible && clockTimer === null) {
    clockTimer = setInterval(() => {
      // Tick text without rebuilding the DOM or disturbing keyboard focus.
      if (screen === "main") updateClockText(root);
      // Expired consent is never left looking actionable, even without a state revision.
      refreshKeyPrompts();
    }, 1000);
  } else if (!visible && clockTimer !== null) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

function render() {
  // A background state push (the usage poll) re-renders whatever screen is up; carry the current
  // screen's body scroll position across the innerHTML swap so a poll doesn't snap it to the top.
  // Only when the screen is unchanged — navigating must start the new screen at the top.
  const prevBody = root.querySelector(".main-body, .set-body");
  const scrollTop = screen === renderedScreen && prevBody ? prevBody.scrollTop : 0;
  // The one-second countdown paint must not collapse a seat the user is reading.
  const expandedCards = screen === renderedScreen
    ? [...root.querySelectorAll(".seat.expanded")].map((card) => [card.dataset.tool, card.dataset.email])
    : [];
  root.innerHTML = screen === "settings" ? buildSettings(state)
    : screen === "add" ? buildAddSeat(state, add)
    : screen === "add-key" ? buildAddKey(state, keyFlow)
    : buildHTML(state);
  if (!root.querySelector(".key-prompts")) {
    root.querySelector(".set-head")?.insertAdjacentHTML("afterend", keyConfirmations(state, answering));
  }
  refreshKeyPrompts();
  for (const button of root.querySelectorAll('[data-action="key-prove"]')) {
    if (proving.has(button.dataset.id)) {
      button.disabled = true; button.textContent = "checking endpoint…";
    }
  }
  renderedScreen = screen;
  if (scrollTop) {
    const nextBody = root.querySelector(".main-body, .set-body");
    if (nextBody) nextBody.scrollTop = scrollTop;
  }
  if (screen === "main" && expandedCards.length) {
    for (const card of root.querySelectorAll(".seat")) {
      if (expandedCards.some(([tool, email]) => tool === card.dataset.tool && email === card.dataset.email)) {
        card.classList.add("expanded");
      }
    }
  }
  // mirror the theme onto <body> so overlays (siblings of #root) get the same CSS vars
  const theme = (state.settings && state.settings.theme === "dark") ? "dark" : "light";
  document.body.className = "theme-" + theme;
}

function refreshKeyPrompts() {
  const prompts = root.querySelector(".key-prompts");
  if (!prompts) return;
  const pending = (state.pending_key_switches || []).filter((r) => r.status === "pending" && Date.parse(r.expires_at) > Date.now());
  const signature = JSON.stringify([pending, [...answering]]);
  // Time passing must not rebuild focused approval buttons just to update a price's age.
  if (prompts.dataset.signature !== signature) {
    const temp = document.createElement("div");
    temp.innerHTML = keyConfirmations(state, answering);
    prompts.innerHTML = temp.firstElementChild.innerHTML;
    prompts.dataset.signature = signature;
  }
}

function keyBack() {
  if (keyFlow?.pending) return;
  if (keyFlow.step === "review") keyFlow.step = "models";
  else if (keyFlow.step === "models") keyFlow.step = "details";
  else if (keyFlow.step === "details") { keyFlow.step = "provider"; keyFlow.secret = ""; }
  else { keyFlow = null; screen = "main"; }
  if (keyFlow) keyFlow.error = null;
  render();
}

function sendKeyFlow(action, extra = {}) {
  const payload = keyRequest(keyFlow);
  keyFlow.pending = `key-${++keySequence}`;
  keyFlow.operation = action; keyFlow.step = "connecting"; keyFlow.error = null;
  send(action, { ...payload, ...extra, key_request_id: keyFlow.pending });
  render();
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
  if (el.disabled) return;
  switch (action) {
    case "key-start":
      add = null;
      keyFlow = { step: "provider", label: "", secret: "", region: "eu", base_url: "", allow_unverified: false };
      screen = "add-key"; render(); break;
    case "key-provider":
      keyFlow = { step: "details", provider: el.dataset.provider, label: "", secret: "", region: "eu", base_url: "", allow_unverified: false };
      render(); break;
    case "key-back": keyBack(); break;
    case "key-cancel":
      if (!keyFlow.pending) { keyFlow = null; screen = "main"; render(); }
      break;
    case "key-discover": {
      if (!keyFlow.label.trim() || keyFlow.secret.trim().length <= 4) {
        keyFlow.error = "give this seat a name and paste your api key"; render(); break;
      }
      if (KEY_PROVIDERS[keyFlow.provider].unverified && !keyFlow.allow_unverified) {
        keyFlow.error = "please choose whether you're happy to try an endpoint we cannot promise works"; render(); break;
      }
      if (keyFlow.provider === "openai_compatible") {
        try {
          const url = new URL(keyFlow.base_url.trim());
          if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) throw Error();
        } catch {
          keyFlow.error = "enter an http or https base url without credentials, a query or a fragment"; render(); break;
        }
      }
      sendKeyFlow("models_list"); break;
    }
    case "key-model": keyFlow.model = el.dataset.model; keyFlow.step = "review"; render(); break;
    case "key-save":
      if (!keyFlow.pending) sendKeyFlow("key_add", { label: keyFlow.label.trim(), model: keyFlow.model });
      break;
    case "key-answer":
      if (answering.has(el.dataset.id)) break;
      answering.add(el.dataset.id); refreshKeyPrompts();
      send("answer_key_switch", { id: el.dataset.id, approved: el.dataset.approved === "true" }); break;
    case "key-validate": send("key_validate", { id: el.dataset.id }); break;
    case "key-prove":
      if (!proving.has(el.dataset.id)) {
        proving.add(el.dataset.id); render();
        send("key_prove", { id: el.dataset.id });
      }
      break;
    case "key-remove":
      el.textContent = "remove this key from the list?"; el.dataset.action = "key-remove-confirm"; break;
    case "key-remove-confirm": send("key_remove", { id: el.dataset.id }); break;
    case "key-pricing":
      e.preventDefault(); send("key_pricing", { provider: el.dataset.provider }); break;
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
      // Only a codex "token" paste installs an auth.json in-app; browser sign-in (both providers)
      // launches the official flow in Terminal and waits on the user.
      if (addUsesPaste(add)) {
        const blob = add.token.trim();
        if (!blob) break;                      // empty field → no-op, not a spinner + error toast
        add.pending = true; add.step = "connecting"; render();   // paste in flight → saving spinner
        send("paste", { tool: add.provider, blob, ...(name ? { name } : {}) });
      } else {
        // launch the browser sign-in and wait on the USER to finish + tap "save my seat" — not a
        // saving spinner yet (that's add.pending, set on save).
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
    case "add-import": {       // one-tap: register the codex account already signed in on this Mac
      if (add.pending) break;
      const name = add.name.trim();
      // in-app save (like paste): pending + importing → saving spinner, no "save my seat" handshake.
      add.pending = true; add.importing = true; add.step = "connecting"; render();
      send("import_current", { tool: add.provider, ...(name ? { name } : {}) });
      break;
    }
    case "add-reveal": send("reveal"); break;   // native: reveal ~/.codex/auth.json in Finder
    case "settings": screen = "settings"; render(); break;
    case "settings-back": screen = "main"; render(); break;
    case "supervision-on": send("toggle", { key: "supervise_shell", value: true }); break;
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
  const fields = { "key-label": "label", "key-secret": "secret", "key-base-url": "base_url" };
  if (keyFlow && fields[e.target.id]) keyFlow[fields[e.target.id]] = e.target.value;
  if (!add) return;
  if (e.target.id === "add-name") add.name = e.target.value;
  else if (e.target.id === "add-token") add.token = e.target.value;
});

// toggles fire 'change' (clicking the switch graphic doesn't bubble a data-action click)
document.addEventListener("change", (e) => {
  if (keyFlow && e.target.id === "key-ack") keyFlow.allow_unverified = e.target.checked;
  if (keyFlow && e.target.id === "key-region") keyFlow.region = e.target.value;
  const inp = e.target.closest('input[data-action="toggle"]');
  if (inp) send("toggle", { key: inp.dataset.key, value: inp.checked });
});

// Esc pops a sub-view (spec §9): settings → main; add → one step back (like the chevron).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (screen === "settings") { screen = "main"; render(); }
  else if (screen === "add") addBack();
  else if (screen === "add-key") keyBack();
});

// WKWebView normally reflects popover visibility here; the native shell also calls setVisible()
// explicitly so transient closes always clear the interval even on macOS versions that do not.
document.addEventListener("visibilitychange", () => setPopoverVisible(!document.hidden));
window.addEventListener("pagehide", () => setPopoverVisible(false));

// initial paint + ask the native side for fresh state
render();
send("ready");
if (!document.hidden && document.hasFocus()) setPopoverVisible(true);
