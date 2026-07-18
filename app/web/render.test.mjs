// UI tests for the pure render layer (node --test). Asserts the spec markup.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  buildHTML, dotState, dotKey, doorKey, doorMark, creditLeft, pct, fmtCountdown, needsHello,
  buildSettings, buildAddSeat, reduceReply,
} from "./render.mjs";

const mkAdd = (over = {}) => ({ step: "provider", provider: null, name: "", method: "browser", token: "", ...over });

function seat(over = {}) {
  return { email: "work@x.com", name: "Work", plan: "Business", status: "ready",
           active: false, limited: false, limited_until: null, usage5h: 20, usageWeek: 10, ...over };
}
function state(over = {}) {
  return {
    settings: { theme: "light", auto_switch: true, strategy: "soonest_back" },
    counts: { resting: 1, ready: 2 },
    tools: {
      codex: { active: null, plan_label: "CHATGPT BUSINESS", seats: [] },
      claude: { active: null, plan_label: "CLAUDE CODE", seats: [] },
    },
    ...over,
  };
}

test("dotKey golden parity with python fixture", () => {
  const path = fileURLToPath(new URL("../../tests/fixtures/dot_cases.json", import.meta.url));
  for (const c of JSON.parse(readFileSync(path, "utf8"))) assert.equal(dotKey(c.state), c.expected, c.name);
});

test("dotState reads bridge-provided state.dot", () => {
  assert.equal(dotState({ dot: "amber" }).key, "amber");
  assert.equal(dotState({ dot: "hello" }).label, "needs a hello");
});

test("doorKey golden parity with python fixture", () => {
  const path = fileURLToPath(new URL("../../tests/fixtures/door_cases.json", import.meta.url));
  for (const c of JSON.parse(readFileSync(path, "utf8"))) assert.equal(doorKey(c.state), c.expected, c.name);
});

test("doorKey prefers bridge-provided state.door, falls back to seats", () => {
  assert.equal(doorKey({ door: "shut" }), "shut");
  assert.equal(doorKey(state({ tools: { codex: { seats: [{ status: "active" }] }, claude: { seats: [] } } })), "open");
});

test("header renders the live door mark, not the old gradient avatar", () => {
  const open = buildHTML(state({ door: "open" }));
  assert.match(open, /class="avatar door door--open"/);
  assert.match(open, /door-ball/);
  const shut = buildHTML(state({ door: "shut" }));
  assert.match(shut, /class="avatar door door--shut"/);
  assert.doesNotMatch(doorMark({ door: "shut" }), /linear-gradient\(135deg/);
});

test("needsHello detects needs-login status", () => {
  assert.equal(needsHello(seat({ status: "needs-login" })), true);
  assert.equal(needsHello(seat({ status: "ready" })), false);
});

test("pct + creditLeft from usage5h/usageWeek", () => {
  assert.equal(pct(seat({ usage5h: 120 }), "5h"), 100);
  assert.equal(creditLeft(seat({ usage5h: 70, usageWeek: 40 })), 30);
});

test("fmtCountdown", () => {
  const now = Date.parse("2026-06-28T12:00:00Z");
  assert.equal(fmtCountdown("2026-06-28T12:12:00Z", now), "12m");
  assert.equal(fmtCountdown("2026-06-28T14:30:00Z", now), "2h 30m");
});

test("type discipline: Outfit wordmark w/ gold 'ai', email mono, seat name NOT mono", () => {
  const s = state({ tools: { codex: { plan_label: "CHATGPT BUSINESS",
    seats: [seat({ status: "ready" })] }, claude: { seats: [] } } });
  const html = buildHTML(s);
  assert.match(html, /class="brand"><span class="ai">ai<\/span> guest list/);  // Outfit wordmark, accented "ai"
  assert.doesNotMatch(html, /class="brand mono"/);          // wordmark is no longer mono
  assert.match(html, /class="seat-email mono"[^>]*>work@x\.com/);  // email mono
  assert.match(html, /class="seat-name">Work</);            // name is sans (no mono class)
});

test("status: active=pill, ready=switch btn, resting=countdown+reassurance, needs-login=log in", () => {
  const mk = (st, extra) => buildHTML(state({ tools: {
    codex: { plan_label: "CHATGPT BUSINESS", seats: [seat({ status: st, ...extra })] }, claude: { seats: [] } } }));
  assert.match(mk("active", { active: true }), /pill floor">on the floor/);
  assert.match(mk("ready"), /btn switch"[^>]*data-action="switch"/);
  const resting = mk("resting", { limited: true, limited_until: new Date(Date.now() + 6e6).toISOString() });
  assert.match(resting, /back in/);
  assert.match(resting, /taking a breather/);            // reassurance ONLY here
  assert.match(mk("queued", { limited: true }), /pill queued">up next/);
  assert.match(mk("needs-login"), /btn rose"[^>]*data-action="add"/);
});

test("reassurance never appears on active/ready seats", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ status: "active", active: true })] }, claude: { seats: [] } } }));
  assert.doesNotMatch(html, /taking a breather/);
});

test("single 5h bar in collapsed card; 7d lives in expand", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ status: "ready", usage5h: 25, usageWeek: 60 })] }, claude: { seats: [] } } }));
  assert.match(html, /u-k">5h</);
  assert.match(html, /u-k">7d</);            // present but inside .expand (hidden until tapped)
  assert.match(html, /class="expand"/);
  assert.match(html, /25%/);
});

test("flat status dots, not emoji", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ status: "resting", limited: true })] }, claude: { seats: [] } } }));
  assert.match(html, /class="dot dot--resting"/);
  assert.doesNotMatch(html, /🟢|🟡|🌸|🌿|💚/);  // no status emoji, no green heart
});

test("header substatus + plan chip + section meta", () => {
  const html = buildHTML(state({ counts: { resting: 1, ready: 3 }, tools: {
    codex: { plan_label: "CHATGPT BUSINESS", seats: [seat({ plan: "Business" })] }, claude: { seats: [] } } }));
  assert.match(html, /1 resting · 3 ready/);
  assert.match(html, /class="mono chip">BUSINESS|class="mono chip">Business/);
  assert.match(html, /class="mono g-meta">CHATGPT BUSINESS/);
  assert.match(html, /made with <span class="heart">💛/);
});

test("buildHTML escapes user content", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ name: "<script>x" })] }, claude: { seats: [] } } }));
  assert.match(html, /&lt;script&gt;x/);
});

test("buildHTML default light theme for unknown", () => {
  assert.match(buildHTML(state({ settings: { theme: "evil" } })), /class="app theme-light"/);
});

test("settings wires its actions", () => {
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back", notify: true } });
  assert.match(set, /data-action="set_strategy"[^>]*data-value="most_headroom"/);
  assert.match(set, /data-action="set_theme"[^>]*data-value="dark"/);
});

test("header ＋ opens the provider step (no hardcoded tool)", () => {
  const html = buildHTML(state({}));
  assert.match(html, /data-action="add" title="add a seat"/);              // header ＋ carries no tool
  assert.doesNotMatch(html, /data-action="add" data-tool="[^"]*" title="add a seat"/);
});

test("settings is a pushed sub-view, not a modal (spec §9.1)", () => {
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back" } });
  // no dimming modal backdrop/sheet — it renders in place as the popover surface
  assert.doesNotMatch(set, /class="backdrop"/);
  assert.doesNotMatch(set, /class="sheet/);
  assert.match(set, /class="app set-app theme-light"/);
  // back chevron + done both pop to main
  assert.match(set, /data-action="settings-back"[^>]*title="back"/);
  assert.match(set, /data-action="settings-back"[^>]*>done</);
  // grouped section labels
  for (const label of ["auto-switch", "appearance"]) assert.ok(set.includes(`>${label}<`));
  // every control row carries a one-line subtitle
  assert.match(set, /class="set-s"/);
  // quiet version footer, and the prototype-only demo group is dropped
  assert.match(set, /class="set-ver"/);
  assert.doesNotMatch(set, /cap both Codex seats|try the demo/i);
});

test("add-a-seat is a pushed sub-view, not a modal (spec §9)", () => {
  const st = { settings: { theme: "light" } };
  for (const step of ["provider", "details", "connecting", "done"]) {
    const h = buildAddSeat(st, mkAdd({ step, provider: "codex" }));
    assert.doesNotMatch(h, /class="backdrop"/, step);
    assert.doesNotMatch(h, /class="sheet/, step);
    assert.doesNotMatch(h, /pk-m/, step);
    assert.match(h, /class="app set-app add-app theme-light"/, step);
    assert.match(h, /data-action="add-back"[^>]*title="back"/, step);
  }
});

test("add: provider step is one grouped card with both providers", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "provider" }));
  assert.ok(h.includes("who's joining the list?"));
  assert.match(h, /data-action="add-provider" data-tool="codex"/);
  assert.match(h, /data-action="add-provider" data-tool="claude"/);
  assert.ok(h.includes("ChatGPT sign-in · Business seat"));
  assert.ok(h.includes("Claude.ai sign-in · Max or Pro seat"));
  assert.ok(h.includes("nothing leaves your Mac"));
  assert.match(h, /data-action="add-cancel"/);                 // cancel shows on provider
});

test("add: cancel only on provider|details, never connecting|done", () => {
  for (const step of ["provider", "details"])
    assert.match(buildAddSeat({ settings: {} }, mkAdd({ step, provider: "codex" })), /add-cancel/, step);
  for (const step of ["connecting", "done"])
    assert.doesNotMatch(buildAddSeat({ settings: {} }, mkAdd({ step, provider: "codex" })), /add-cancel/, step);
});

test("add: claude is browser-only — no method chooser, no token surface", () => {
  // `claude setup-token` produces an env-var token, not the Keychain login this app snapshots, so
  // there is no working no-browser path for Claude — the details step is name + a single sign-in CTA.
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "claude" }));
  assert.match(h, /--accent:var\(--claude\)/);
  assert.ok(h.includes("new Claude seat") && h.includes("Claude.ai sign-in"));
  assert.match(h, /data-action="add-change"/);
  assert.match(h, /id="add-name"[^>]*placeholder="Work · Personal · Late-night"/);
  assert.doesNotMatch(h, /data-action="add-method"/);          // NO segmented control
  assert.doesNotMatch(h, /how should i sign you in/);          // NO method section
  assert.doesNotMatch(h, /id="add-token"/);                    // NO textarea
  assert.ok(h.includes("open sign-in →"));                     // single browser CTA
});

test("add: codex setup-token method DOES paste an auth.json textarea", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "token" }));
  assert.match(h, /id="add-token"[^>]*placeholder="[^"]*auth.json/);
  assert.ok(h.includes("save the seat →"));                    // in-app paste, not Terminal
});

test("add: codex token copy drops the unsupported 'API key' promise", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "token" }));
  assert.ok(h.includes("auth.json"));
  assert.doesNotMatch(h, /API key/i);                          // engine can't accept one → don't promise it
});

test("add: typed name + token survive a re-render (escaped, controlled)", () => {
  const h = buildAddSeat({ settings: {} },
    mkAdd({ step: "details", provider: "codex", method: "token", name: 'Wo"rk', token: "sk-x<y" }));
  assert.match(h, /value="Wo&quot;rk"/);                       // name reproduced, escaped
  assert.ok(h.includes("sk-x&lt;y"));                          // token reproduced, escaped
});

test("add: connecting — browser waits for the user, no premature spinner", () => {
  // before "save my seat": waiting on the user, save button live, NO spinner (would read as hung)
  const wait = buildAddSeat({ settings: {} }, mkAdd({ step: "connecting", provider: "codex", method: "browser" }));
  assert.doesNotMatch(wait, /class="add-spin"/);
  assert.ok(wait.includes("we opened your browser…"));
  assert.match(wait, /data-action="add-save"[^>]*>save my seat 💛</);
  assert.doesNotMatch(wait, /add-save"[^>]*disabled/);
  // after clicking save (pending): spinner on, button disabled, copy switches to "saving…"
  const saving = buildAddSeat({ settings: {} }, mkAdd({ step: "connecting", provider: "codex", method: "browser", pending: true }));
  assert.match(saving, /class="add-spin"/);
  assert.ok(saving.includes("saving your seat…"));
  assert.match(saving, /data-action="add-save"[^>]*disabled/);
  // a codex paste is always actively saving (spinner on, resolves via the bridge — no save button)
  const paste = buildAddSeat({ settings: {} }, mkAdd({ step: "connecting", provider: "codex", method: "token", pending: true }));
  assert.match(paste, /class="add-spin"/);
  assert.ok(paste.includes("saving your seat…"));
  assert.doesNotMatch(paste, /add-save/);
  // claude is browser-only: same waiting connecting step with a save button, no premature spinner
  const claude = buildAddSeat({ settings: {} }, mkAdd({ step: "connecting", provider: "claude", method: "browser" }));
  assert.doesNotMatch(claude, /class="add-spin"/);
  assert.ok(claude.includes("we opened your browser…"));
  assert.match(claude, /data-action="add-save"[^>]*>save my seat 💛</);
});

test("add: done greets the seat, escapes, falls back to 'new seat'", () => {
  assert.match(buildAddSeat({ settings: {} }, mkAdd({ step: "done", provider: "codex", name: "Work" })),
    /class="add-welcome">welcome, Work</);
  assert.match(buildAddSeat({ settings: {} }, mkAdd({ step: "done", provider: "codex", name: "  " })),
    /welcome, new seat</);
  assert.ok(buildAddSeat({ settings: {} }, mkAdd({ step: "done", provider: "codex", name: "<b>" }))
    .includes("welcome, &lt;b&gt;"));
});

test("buildHTML has no retired Headroom surface", () => {
  const html = buildHTML(state({}));
  assert.doesNotMatch(html, /COMPRESSES CONTEXT|save-credit|headroom_install|fewer tokens/i);
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back" } });
  assert.doesNotMatch(set, /set_savings_level|>headroom</);
});

// --- reduceReply: the async add-flow state machine (pure; these are the cases that kept regressing)
const UI = (over = {}) => ({ screen: "main", add: null, lastRev: -1, state: {}, ...over });
const stateRev = (rev) => ({ rev, settings: {}, tools: {}, counts: {} });

test("reduceReply: a pure usage poll on main renders, no add involvement", () => {
  const o = reduceReply(UI(), { ok: true, state: stateRev(1) });
  assert.equal(o.screen, "main"); assert.equal(o.render, true); assert.equal(o.lastRev, 1);
});

test("reduceReply: a poll while on the add screen does NOT render (keeps typed input)", () => {
  const add = mkAdd({ step: "details", provider: "codex", name: "Wo" });
  const o = reduceReply(UI({ screen: "add", add }), { ok: true, state: stateRev(2) });
  assert.equal(o.render, false);           // swallow — no DOM swap
  assert.equal(o.state.rev, 2);            // but state IS updated silently
  assert.equal(o.add.name, "Wo");          // typed input untouched
});

test("reduceReply: stale snapshot (lower rev) is ignored", () => {
  const o = reduceReply(UI({ lastRev: 5, state: stateRev(5) }), { ok: true, state: stateRev(4) });
  assert.equal(o.lastRev, 5); assert.equal(o.state.rev, 5);   // kept the newer state
});

test("reduceReply: our paste/snapshot success → done + schedules close", () => {
  const add = mkAdd({ step: "connecting", provider: "codex", method: "token", pending: true });
  const o = reduceReply(UI({ screen: "add", add }), { ok: true, added: "x@x.com", add_op: true });
  assert.equal(o.add.step, "done"); assert.equal(o.add.pending, false);
  assert.equal(o.render, true); assert.equal(o.closeFlow, o.add);   // caller auto-closes this flow
});

test("reduceReply: a stale success for a flow we already left is ignored (no pending)", () => {
  const add = mkAdd({ step: "details", provider: "claude" });   // fresh flow, not pending
  const o = reduceReply(UI({ screen: "add", add }), { ok: true, added: "stale@x.com", add_op: true });
  assert.equal(o.add.step, "details"); assert.equal(o.render, false); assert.equal(o.closeFlow, null);
});

test("reduceReply: codex-paste error → back to details, toasts", () => {
  const add = mkAdd({ step: "connecting", provider: "codex", method: "token", pending: true });
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "bad auth.json", add_op: true });
  assert.equal(o.add.step, "details"); assert.equal(o.add.pending, false);
  assert.equal(o.render, true); assert.equal(o.flash, "bad auth.json");
});

test("reduceReply: browser-save error stays on connecting to retry", () => {
  const add = mkAdd({ step: "connecting", provider: "codex", method: "browser", pending: true });
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "no creds yet", add_op: true });
  assert.equal(o.add.step, "connecting"); assert.equal(o.add.pending, false);   // save button re-enables
});

test("reduceReply: login-LAUNCH failure (not pending) → back to details", () => {
  const add = mkAdd({ step: "connecting", provider: "claude", method: "browser" });   // awaiting user, no save
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "couldn't open", add_op: true });
  assert.equal(o.add.step, "details"); assert.equal(o.render, true); assert.equal(o.flash, "couldn't open");
});

test("reduceReply: an add-op error after the user left does NOT toast over main", () => {
  const o = reduceReply(UI({ screen: "main", add: null }), { ok: false, error: "late fail", add_op: true });
  assert.equal(o.flash, null);             // suppressed — user isn't in the add flow anymore
});

test("reduceReply: a background poll error never toasts", () => {
  const o = reduceReply(UI(), { ok: false, error: "usage blip", background: true, state: stateRev(3) });
  assert.equal(o.flash, null);
});

test("reduceReply: a normal user-action error DOES toast", () => {
  const o = reduceReply(UI(), { ok: false, error: "switch failed" });
  assert.equal(o.flash, "switch failed");
});

test("reduceReply: settings_panel opens settings, but not while a save is in flight", () => {
  assert.equal(reduceReply(UI(), { settings_panel: true }).screen, "settings");
  const add = mkAdd({ step: "connecting", provider: "codex", method: "token", pending: true });
  assert.equal(reduceReply(UI({ screen: "add", add }), { settings_panel: true }).screen, "add");  // guarded
});

test("reduceReply: celebrate flag is passed through", () => {
  assert.equal(reduceReply(UI(), { ok: true, celebrate: true, state: stateRev(1) }).celebrate, true);
});

test("reduceReply: an add-op error for ANOTHER tool does not steer the current flow", () => {
  // user abandoned a codex login, is now on a claude connecting step; the stale codex failure lands
  const add = mkAdd({ step: "connecting", provider: "claude", method: "browser" });
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "codex fail", add_op: true, tool: "codex" });
  assert.equal(o.add.step, "connecting");   // NOT sent back to details
  assert.equal(o.render, false);
  assert.equal(o.flash, null);              // and not toasted over the claude flow
});

test("reduceReply: an add-op reply for the SAME tool still applies", () => {
  const add = mkAdd({ step: "connecting", provider: "claude", method: "browser" });
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "claude fail", add_op: true, tool: "claude" });
  assert.equal(o.add.step, "details"); assert.equal(o.flash, "claude fail");
});

test("reduceReply: a tool-less add-op error falls back to the current flow", () => {
  const add = mkAdd({ step: "connecting", provider: "codex", method: "token", pending: true });
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "generic", add_op: true });
  assert.equal(o.add.step, "details"); assert.equal(o.flash, "generic");   // no tool → still ours
});
