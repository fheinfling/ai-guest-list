// UI tests for the pure render layer (node --test). No jsdom needed: buildHTML returns a string,
// so we assert structure/state-reflection directly. dotState/creditLeft/fmtCountdown are pure.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  buildHTML, dotState, dotKey, creditLeft, pct, fmtCountdown, needsHello,
  buildPicker, buildSaveSeat, buildPaste, buildSettings,
} from "./render.mjs";

function seat(over = {}) {
  return { email: "a@x.com", name: "work", active: false, limited: false,
           limited_until: null, usage: null, ...over };
}
function state(over = {}) {
  return {
    settings: { theme: "dark", auto_switch: true, headroom: false },
    headroom_available: false,
    tools: {
      codex: { active: null, seats: [], selection: {} },
      claude: { active: null, seats: [], selection: {} },
    },
    ...over,
  };
}

test("dotState: fresh when all seats have credit", () => {
  const s = state({ tools: { codex: { seats: [seat({ active: true })] }, claude: { seats: [] } } });
  assert.equal(dotState(s).key, "fresh");
});

test("dotState: resting when active seat is limited", () => {
  const s = state({ tools: { codex: { seats: [seat({ active: true, limited: true })] }, claude: { seats: [] } } });
  assert.equal(dotState(s).key, "resting");
});

test("dotState: needs a hello on unauthorized usage", () => {
  const s = state({ tools: { codex: { seats: [seat({ usage: { error: "unauthorized" } })] }, claude: { seats: [] } } });
  assert.equal(dotState(s).key, "hello");
});

test("dotState: switched takes precedence", () => {
  const s = state({ recently_switched: true,
    tools: { codex: { seats: [seat({ active: true, limited: true })] }, claude: { seats: [] } } });
  assert.equal(dotState(s).key, "switched");
});

test("needsHello detects unauthorized", () => {
  assert.equal(needsHello(seat({ usage: { error: "unauthorized" } })), true);
  assert.equal(needsHello(seat({ usage: { error: "rate_limited" } })), false);
});

test("pct clamps and reads window", () => {
  const s = seat({ usage: { windows: { "5h": { used_pct: 120 }, weekly: { used_pct: 30 } } } });
  assert.equal(pct(s, "5h"), 100);
  assert.equal(pct(s, "weekly"), 30);
  assert.equal(pct(seat(), "5h"), null);
});

test("creditLeft = 100 - max(used)", () => {
  const s = seat({ usage: { windows: { "5h": { used_pct: 70 }, weekly: { used_pct: 40 } } } });
  assert.equal(creditLeft(s), 30);
  assert.equal(creditLeft(seat()), null);
});

test("fmtCountdown formats minutes and hours", () => {
  const now = Date.parse("2026-06-28T12:00:00Z");
  assert.equal(fmtCountdown("2026-06-28T12:12:00Z", now), "in 12m");
  assert.equal(fmtCountdown("2026-06-28T14:30:00Z", now), "in 2h 30m");
  assert.equal(fmtCountdown("2026-06-28T11:00:00Z", now), "now");
  assert.equal(fmtCountdown(null), "");
});

test("buildHTML reflects seats, active marker and credit", () => {
  const s = state({ tools: {
    codex: { active: "a@x.com", seats: [
      seat({ active: true, usage: { windows: { "5h": { used_pct: 25 }, weekly: { used_pct: 10 } } } }),
      seat({ email: "b@x.com", name: "spare" }),
    ] },
    claude: { active: null, seats: [] },
  } });
  const html = buildHTML(s);
  assert.match(html, /on the floor/);
  assert.match(html, /CHATGPT BUSINESS/);          // section plan label
  assert.match(html, /25%/);                        // 5h usage shown
  assert.match(html, /data-action="switch"[^>]*data-email="b@x.com"/);
  assert.match(html, /add a seat/);
  assert.match(html, /made with/);
});

test("buildHTML headroom hint + install button when unavailable", () => {
  const html = buildHTML(state({ headroom_available: false }));
  assert.match(html, /install to enable/);
  assert.match(html, /data-action="headroom_install"/);
});

test("buildHTML escapes seat names", () => {
  const s = state({ tools: { codex: { active: null, seats: [seat({ name: "<script>x" })] }, claude: { seats: [] } } });
  assert.match(buildHTML(s), /&lt;script&gt;x/);
});

test("buildHTML uses a safe theme class for unknown themes (defaults to light)", () => {
  const html = buildHTML(state({ settings: { theme: "evil\" onload=x" } }));
  assert.match(html, /class="app theme-light"/);  // unknown theme → light default, no injection
});

test("dotState reads the bridge-provided state.dot (source of truth)", () => {
  assert.equal(dotState({ dot: "resting" }).key, "resting");
  assert.equal(dotState({ dot: "hello" }).emoji, "🌸");
});

test("dotKey golden parity with python fixture", () => {
  const path = fileURLToPath(new URL("../../tests/fixtures/dot_cases.json", import.meta.url));
  const cases = JSON.parse(readFileSync(path, "utf8"));
  for (const c of cases) assert.equal(dotKey(c.state), c.expected, c.name);
});

test("buildPicker renders command + paste methods with actions", () => {
  const html = buildPicker({ tool: "codex", title: "who's joining the list?", methods: [
    { id: "browser", label: "ChatGPT sign-in", command: "codex login" },
    { id: "paste", label: "paste auth.json", command: null },
  ]});
  assert.match(html, /data-action="login"[^>]*data-command="codex login"/);
  assert.match(html, /data-action="paste-open"[^>]*data-tool="codex"/);
});

test("buildSaveSeat + buildPaste wire snapshot/paste-save", () => {
  assert.match(buildSaveSeat("claude"), /data-action="snapshot"[^>]*data-tool="claude"/);
  assert.match(buildPaste("codex"), /data-action="paste-save"[^>]*data-tool="codex"/);
});

test("buildSettings renders toggles, theme segments and dot legend", () => {
  const html = buildSettings({ settings: { theme: "light", notify: true, celebrations: false } });
  assert.match(html, /data-action="toggle"[^>]*data-key="same_tool_only"/);
  assert.match(html, /data-action="toggle"[^>]*data-key="notify"[^>]*checked/);
  assert.match(html, /data-action="set_theme"[^>]*data-value="dark"/);
  assert.match(html, /seg on[^>]*data-value="light"|data-value="light"[^>]*>/);  // light selected
  assert.match(html, /what the dot means/);
});
