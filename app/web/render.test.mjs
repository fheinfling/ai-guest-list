// UI tests for the pure render layer (node --test). Asserts the spec markup.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  buildHTML, dotState, dotKey, doorKey, doorMark, creditLeft, pct, fmtCountdown, needsHello,
  buildPicker, buildSaveSeat, buildPaste, buildSettings,
} from "./render.mjs";

function seat(over = {}) {
  return { email: "work@x.com", name: "Work", plan: "Business", status: "ready",
           active: false, limited: false, limited_until: null, usage5h: 20, usageWeek: 10, ...over };
}
function state(over = {}) {
  return {
    settings: { theme: "light", auto_switch: true, headroom: true, strategy: "soonest_back" },
    headroom_available: true, counts: { resting: 1, ready: 2 },
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
  assert.match(html, /1 resting · 3 ready 💛/);
  assert.match(html, /class="mono chip">BUSINESS|class="mono chip">Business/);
  assert.match(html, /class="mono g-meta">CHATGPT BUSINESS/);
  assert.match(html, /made with <span class="heart">💛/);
});

test("Headroom bar shows chip + savings, install link when unavailable", () => {
  assert.match(buildHTML(state({ headroom_available: true, headroom_savings: 68 })), /COMPRESSES CONTEXT/);
  assert.match(buildHTML(state({ headroom_available: true, headroom_savings: 68 })), /~68% fewer tokens/);
  assert.match(buildHTML(state({ headroom_available: false })), /data-action="headroom_install"/);
});

test("buildHTML escapes user content", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ name: "<script>x" })] }, claude: { seats: [] } } }));
  assert.match(html, /&lt;script&gt;x/);
});

test("buildHTML default light theme for unknown", () => {
  assert.match(buildHTML(state({ settings: { theme: "evil" } })), /class="app theme-light"/);
});

test("overlays wire their actions", () => {
  assert.match(buildPicker({ tool: "codex", methods: [{ label: "ChatGPT sign-in", command: "codex login" }] }),
    /data-action="login"[^>]*data-command="codex login"/);
  assert.match(buildSaveSeat("claude"), /data-action="snapshot"[^>]*data-tool="claude"/);
  assert.match(buildPaste("claude"), /sk-ant-oat/);
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back", notify: true, headroom: true } });
  assert.match(set, /data-action="set_strategy"[^>]*data-value="most_headroom"/);
  assert.match(set, /data-action="set_theme"[^>]*data-value="dark"/);
});

test("settings is a pushed sub-view, not a modal (spec §9.1)", () => {
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back", headroom: false } });
  // no dimming modal backdrop/sheet — it renders in place as the popover surface
  assert.doesNotMatch(set, /class="backdrop"/);
  assert.doesNotMatch(set, /class="sheet/);
  assert.match(set, /class="app set-app theme-light"/);
  // back chevron + done both pop to main
  assert.match(set, /data-action="settings-back"[^>]*title="back"/);
  assert.match(set, /data-action="settings-back"[^>]*>done</);
  // grouped section labels
  for (const label of ["auto-switch", "headroom", "appearance"]) assert.ok(set.includes(`>${label}<`));
  // every control row carries a one-line subtitle
  assert.match(set, /class="set-s"/);
  // quiet version footer, and the prototype-only demo group is dropped
  assert.match(set, /class="set-ver"/);
  assert.doesNotMatch(set, /cap both Codex seats|try the demo/i);
});

test("settings renders the savings-level selector only when headroom is on", () => {
  const on = buildSettings({ settings: { theme: "light", strategy: "soonest_back", headroom: true, savings_level: "moderate" } });
  // friendly labels are display-only; data-value carries the real persisted level
  assert.match(on, /data-action="set_savings_level"[^>]*data-value="conservative"[^>]*>easy</);
  assert.match(on, /data-action="set_savings_level"[^>]*data-value="aggressive"[^>]*>max</);
  // the current level is marked selected
  assert.match(on, /class="sopt on"[^>]*data-value="moderate"/);
  // hidden when Headroom is off (nothing to configure)
  const off = buildSettings({ settings: { theme: "light", strategy: "soonest_back", headroom: false } });
  assert.doesNotMatch(off, /set_savings_level/);
});

test("buildHTML labels measured vs estimated savings and shows lifetime totals", () => {
  const meas = buildHTML(state({ headroom_available: true, headroom_savings: 28,
    headroom_savings_measured: true, headroom_stats: { tokens_saved: 12748404, usd_saved: 63.74 } }));
  assert.match(meas, /28% fewer tokens \(measured\)/);
  assert.match(meas, /12\.7M tokens · \$64 saved/);  // lifetime totals surfaced (63.74 → $64)
  const est = buildHTML(state({ headroom_available: true, headroom_savings: 35,
    headroom_savings_measured: false }));
  assert.match(est, /35% fewer tokens \(est\.\)/);
  // a legitimately-zero total still renders (must not be dropped as falsy)
  const zero = buildHTML(state({ headroom_available: true, headroom_savings: 12,
    headroom_savings_measured: true, headroom_stats: { tokens_saved: 4200, usd_saved: 0 } }));
  assert.match(zero, /4k tokens · \$0 saved/);
  // non-numeric stats (untrusted /stats response) must not reach innerHTML
  const evil = buildHTML(state({ headroom_available: true, headroom_savings: 12,
    headroom_savings_measured: true, headroom_stats: { tokens_saved: "<img src=x onerror=alert(1)>", usd_saved: "x" } }));
  assert.doesNotMatch(evil, /<img src=x/);
  // just-under-1M rolls over to "1.0M", not "1000k" (Math.round(999600/1e3) === 1000)
  const rollover = buildHTML(state({ headroom_available: true, headroom_savings: 12,
    headroom_savings_measured: true, headroom_stats: { tokens_saved: 999600, usd_saved: 5 } }));
  assert.match(rollover, /1\.0M tokens/);
  assert.doesNotMatch(rollover, /1000k/);
});

test("buildHTML shows a paused substatus when headroom is on but the proxy is down", () => {
  const down = buildHTML(state({ headroom_available: true, headroom_proxy_down: true,
    headroom_savings: 40, headroom_savings_measured: true }));
  assert.match(down, /save-credit paused/);
  assert.doesNotMatch(down, /fewer tokens/);  // don't claim it's wrapping while the proxy is dead
});
