// UI tests for the pure render layer (node --test). Asserts the spec markup.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  buildHTML, dotState, dotKey, doorKey, doorMark, creditLeft, pct, fmtCountdown, needsHello,
  buildSettings, buildAddSeat, reduceReply, supervisionBanner, fmtUsageAge, fmtSessionAge, updateClockText,
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
  assert.equal(needsHello(seat({ status: "active", usage: { error: "token_expired" } })), false);
  assert.equal(needsHello(seat({ status: "active", usage: { error: "unauthorized" } })), true);
});

test("pct + creditLeft from usage5h/usageWeek", () => {
  assert.equal(pct(seat({ usage5h: 120 }), "5h"), 100);
  assert.equal(creditLeft(seat({ usage5h: 70, usageWeek: 40 })), 30);
});

test("fmtCountdown", () => {
  const now = Date.parse("2026-06-28T12:00:00Z");
  assert.equal(fmtCountdown("2026-06-28T12:12:00Z", now), "12m");
  assert.equal(fmtCountdown("2026-06-28T14:30:00Z", now), "2h 30m");
  assert.equal(fmtCountdown("2026-06-29T12:00:00Z", now), "24h");
  assert.equal(fmtCountdown("2026-07-05T11:00:00Z", now), "6d23h");
  assert.equal(fmtCountdown("2026-07-05T13:00:00Z", now), "7d1h");
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
  assert.match(mk("active", { active: true }), /pill floor floor--idle">selected/);
  assert.match(mk("ready"), /btn switch"[^>]*data-action="switch"/);
  const resting = mk("resting", { limited: true, limited_until: new Date(Date.now() + 6e6).toISOString() });
  assert.match(resting, /back in/);
  assert.match(resting, /taking a breather/);            // reassurance ONLY here
  assert.match(mk("queued", { limited: true }), /pill queued">up next/);
  assert.match(mk("needs-login"), /btn rose"[^>]*data-action="add"/);
});

test("stale usage preserves bars and labels the last successful reading", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({
      usage_stale: true,
      usage_fetched_at: "2026-06-28T12:12:00Z",
    })] },
    claude: { seats: [] },
  } }));
  assert.equal((html.match(/class="usage usage--stale"/g) || []).length, 2);
  assert.match(html, /data-usage-at="2026-06-28T12:12:00Z"/);
  assert.match(html, /usage-age--stale/);
  assert.match(html, /last known/);
  assert.match(html, /20%/);  // stale is last-known and labeled; only unknown suppresses the number
});

test("resting cards show the breather return time without duplicate refresh-status lines", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({
      status: "resting",
      limited: true,
      limited_until: "2026-09-13T18:57:00Z",
      usage_stale: true,
      usage_fetched_at: "2026-09-13T18:25:00Z",
      usage: { error: "network" },
    })] },
    claude: { seats: [] },
  } }));
  assert.match(html, /taking a breather — back/);
  assert.match(html, /class="usage usage--stale"/);  // retained data stays visibly muted
  assert.doesNotMatch(html, /last known|connection unavailable|retrying automatically/);
  assert.doesNotMatch(html, /data-usage-at=/);
});

test("old cached usage keeps last-known percentages visible but excludes credit left", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ usage_unknown: true, usage5h: 100, usageWeek: 75,
      usage_fetched_at: "2026-09-13T12:00:00Z" })] },
    claude: { seats: [] },
  } }));
  assert.equal((html.match(/class="usage usage--stale"/g) || []).length, 2);
  assert.match(html, /100%/);
  assert.match(html, /75%/);
  assert.match(html, /data-usage-at="2026-09-13T12:00:00Z"/);
  assert.match(html, /last known/);
  assert.doesNotMatch(html, /credit left/);
});

test("genuinely missing usage values still render dashes", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ usage_unknown: true, usage5h: null, usageWeek: null,
      usage: { windows: {} } })] },
    claude: { seats: [] },
  } }));
  assert.equal((html.match(/class="mono u-v">—/g) || []).length, 2);
  assert.equal((html.match(/style="width:0%"/g) || []).length, 2);
  assert.doesNotMatch(html, /credit left/);
});

test("active seats distinguish a live session from loaded-but-idle credentials", () => {
  const mk = (over) => buildHTML(state({ tools: {
    codex: { seats: [seat({ status: "active", active: true, ...over })] },
    claude: { seats: [] },
  } }));
  const live = mk({
    in_session: true,
    session_started_at: "2026-06-28T12:12:00Z",
    last_on_floor: "2026-06-28T11:45:00Z",
  });
  assert.match(live, /pill floor floor--live/);
  assert.match(live, /class="live-dot"/);
  assert.match(live, /last on the floor/);
  assert.match(live, /session started/);

  const idle = mk({ in_session: false });
  assert.match(idle, /pill floor floor--idle">selected/);
  assert.doesNotMatch(idle, /live-dot|session started/);
});

test("only the credential holder is on the floor; a parked terminal keeps its switch action", () => {
  const html = buildHTML(state({ tools: { codex: { seats: [
    seat({ email: "live@x.com", status: "active", active: true, in_session: true }),
    seat({ email: "parked@x.com", in_session: true, session_started_at: "2026-09-14T08:47:00Z" }),
  ] }, claude: { seats: [] } } }));
  assert.equal((html.match(/on the floor/g) || []).length, 1);
  const parked = html.slice(html.indexOf('class="seat seat--ready"'));
  const row = parked.slice(0, parked.indexOf('class="seat-email'));
  assert.match(row, /class="mono chip terminal-chip"[^>]*>in a terminal/);
  assert.match(row, /data-session-at="2026-09-14T08:47:00Z"/);
  assert.match(row, /data-action="switch" data-tool="codex" data-email="parked@x.com"/);
  assert.doesNotMatch(parked, /floor--live/);
});

test("parked terminals retain resting, queued, and sign-in signals beside the chip", () => {
  for (const [status, signal] of [["resting", /back in/], ["queued", /up next/],
    ["needs-login", /data-action="add"/]]) {
    const html = buildHTML(state({ tools: { codex: { seats: [seat({ status, in_session: true })] } } }));
    assert.match(html, /in a terminal/);
    assert.match(html, signal);
    assert.doesNotMatch(html, /floor--live/);
  }
});

test("terminal ages expose long-lived sessions without inventing missing start times", () => {
  const at = Date.parse("2026-09-25T08:47:00Z");
  assert.equal(fmtSessionAge("2026-09-14T08:47:00Z", at), " · 11d");
  assert.equal(fmtSessionAge("2026-09-25T06:47:00Z", at), " · 2h");
  assert.equal(fmtSessionAge("2026-09-25T08:40:00Z", at), " · 7m");
  assert.equal(fmtSessionAge("2026-09-25T08:48:00Z", at), " · 0m");
  assert.equal(fmtSessionAge(null, at), "");
  assert.equal(fmtSessionAge("invalid", at), "");
});

test("revoked entitlement has distinct sign-in copy on a non-active seat", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({
      status: "needs-login",
      active: false,
      entitlement_revoked: true,
    })] },
    claude: { seats: [] },
  } }));
  assert.match(html, /class="btn rose revoked"[^>]*>subscription ended — sign in again</);
  assert.doesNotMatch(html, />log in</);
});

test("reassurance never appears on active/ready seats", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ status: "active", active: true })] }, claude: { seats: [] } } }));
  assert.doesNotMatch(html, /taking a breather/);
});

test("both usage windows stay visible on collapsed Codex and Claude cards", () => {
  for (const tool of ["codex", "claude"]) {
  const html = buildHTML(state({ tools: {
    [tool]: { seats: [seat({ status: "active", usage5h: 0, usageWeek: 65,
      usage_fetched_at: "2026-09-13T12:00:00Z",
      usage: { windows: { weekly: { resets_at: "2026-09-14T17:00:00Z" } } },
    })] } } }));
  const collapsed = html.slice(0, html.indexOf('class="expand"'));
  assert.match(collapsed, /u-k">5h</);
  assert.match(collapsed, /u-k">7d</);
  assert.match(collapsed, /0%/);
  assert.match(collapsed, /65%/);
  assert.match(collapsed, /data-reset-at="2026-09-14T17:00:00Z"/);
  assert.match(collapsed, /data-usage-at="2026-09-13T12:00:00Z"/);
  }
});

test("Codex hides 5h only when durations confirm a weekly-only quota", () => {
  const renderSeat = (tool, usage) => buildHTML(state({ tools: {
    [tool]: { seats: [seat({ usage })] },
    [tool === "codex" ? "claude" : "codex"]: { seats: [] },
  } }));

  const weeklyOnly = renderSeat("codex", { reported_windows: ["weekly"] });
  assert.doesNotMatch(weeklyOnly, /class="mono u-k">5h</);
  assert.match(weeklyOnly, /class="mono u-k">7d</);

  const legacy = renderSeat("codex", { windows: { weekly: { used_pct: 10 } } });
  assert.match(legacy, /class="mono u-k">5h</);
  assert.match(legacy, /class="mono u-k">7d</);

  const claude = renderSeat("claude", { reported_windows: ["weekly"] });
  assert.match(claude, /class="mono u-k">5h</);
  assert.match(claude, /class="mono u-k">7d</);
});

test("usage ages handle fresh, old, missing, and future timestamps", () => {
  const at = Date.parse("2026-09-13T12:00:00Z");
  assert.equal(fmtUsageAge("2026-09-13T11:59:31Z", at), "updated 29s ago");
  assert.equal(fmtUsageAge("2026-09-13T11:58:00Z", at), "updated 2m ago");
  assert.equal(fmtUsageAge("2026-09-13T10:00:00Z", at), "updated 2h ago");
  assert.equal(fmtUsageAge("2026-09-11T12:00:00Z", at), "updated 2d ago");
  assert.equal(fmtUsageAge("2026-09-13T12:01:00Z", at), "updated 0s ago");
  assert.equal(fmtUsageAge(null, at), "waiting for first reading");
  assert.equal(fmtUsageAge("invalid", at), "waiting for first reading");
});

test("clock ticks update age and reset text without replacing the DOM", () => {
  const age = { dataset: { usageAt: "2026-09-13T11:59:30Z" } };
  const reset = { dataset: { resetAt: "2026-09-13T12:02:00Z" } };
  const rest = { dataset: { resetAt: "2026-09-13T12:03:00Z", clockPrefix: "back in" } };
  const long = { dataset: { resetAt: "2026-09-20T13:00:00Z" } };
  const terminal = { dataset: { sessionAt: "2026-09-02T12:00:00Z" } };
  const root = { querySelectorAll: (selector) => ({ "[data-usage-at]": [age],
    "[data-reset-at]": [reset, rest, long], "[data-session-at]": [terminal] })[selector],
    set innerHTML(_) { assert.fail("clock ticks must preserve existing controls and focus"); } };
  updateClockText(root, Date.parse("2026-09-13T12:00:00Z"));
  assert.equal(age.textContent, "updated 30s ago");
  assert.equal(reset.textContent, "resets in 2m");
  assert.equal(rest.textContent, "back in 3m");
  assert.equal(long.textContent, "resets in 7d1h");
  assert.equal(terminal.textContent, " · 11d");
});

test("provider throttling is visible alongside retained usage", () => {
  const html = buildHTML(state({ tools: { claude: { seats: [seat({ usage_stale: true,
    usage: { error: "rate_limited" }, usage5h: 0, usageWeek: 65 })] } } }));
  assert.match(html, /usage updates throttled · retrying automatically/);
  assert.match(html, /65%/);
  assert.match(html, /waiting for first reading/);
});

test("expired Claude token asks for an app refresh without implying logout", () => {
  const active = buildHTML(state({ tools: {
    codex: { seats: [] },
    claude: { seats: [seat({ status: "active", active: true,
      usage: { error: "token_expired" } })] },
  } }));
  assert.match(active, /usage refresh pending · open Claude to refresh/);
  assert.doesNotMatch(active, />log in<|sign in to refresh|retrying automatically/);

  const resting = buildHTML(state({ tools: {
    codex: { seats: [] },
    claude: { seats: [seat({ status: "resting", active: true, limited: true, usage5h: 100,
      limited_until: "2026-09-13T18:57:00Z", usage: { error: "token_expired" } })] },
  } }));
  assert.match(resting, /taking a breather — back/);
  assert.doesNotMatch(resting, />log in<|usage refresh pending|sign in to refresh/);
});

test("expired Codex token names Codex without implying logout", () => {
  for (const active of [false, true]) {
    const html = buildHTML(state({ tools: {
      codex: { seats: [seat({ active, status: active ? "active" : "ready",
        usage: { error: "token_expired" } })] },
      claude: { seats: [] },
    } }));
    assert.match(html, /usage refresh pending · open Codex to refresh/);
    assert.doesNotMatch(html, /open Claude|>log in<|sign in to refresh/);
  }
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

test("seat cards hide internal provider plan enums but retain known plan chips", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [
      seat({ email: "lite@x.com", name: "Lite", plan: "SELF_SERVE_BUSINESS_PROLITE" }),
      seat({ email: "team@x.com", name: "Team seat", plan: "team" }),
    ] },
    claude: { seats: [seat({ email: "max@x.com", name: "Max seat", plan: "Max" })] },
  } }));
  assert.doesNotMatch(html, /SELF_SERVE_BUSINESS_PROLITE|Self_Serve_Business_Prolite/);
  assert.match(html, /class="mono chip">Team<\/span>/);
  assert.match(html, /class="mono chip">Max<\/span>/);
});

test("buildHTML escapes user content", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ name: "<script>x" })] }, claude: { seats: [] } } }));
  assert.match(html, /&lt;script&gt;x/);
});

test("buildHTML default light theme for unknown", () => {
  assert.match(buildHTML(state({ settings: { theme: "evil" } })), /class="app theme-light"/);
});

test("inactive supervision shows a repair banner", () => {
  const st = state({
    supervision: { wrappers: true, block: false, on_path: false, active: false },
  });
  const banner = supervisionBanner(st);
  assert.match(banner, /terminal supervision is off/);
  assert.match(banner, /codex\/claude/);
  assert.match(banner, /won't auto-switch/);
  assert.match(banner, /data-action="supervision-on"/);
  assert.match(buildHTML(st), /supervision-banner--error/);
});

test("supervision read errors name the file and reason without offering installation", () => {
  const st = state({ supervision: {
    wrappers: true, block: false, active: false,
    error: "couldn't read /Users/<guest>/.zshrc: Permission denied",
  } });
  const banner = supervisionBanner(st);
  assert.match(banner, /role="alert"/);
  assert.match(banner, /couldn't read \/Users\/&lt;guest&gt;\/\.zshrc: Permission denied/);
  assert.doesNotMatch(banner, /terminal supervision is off|supervision-on|turn it on|<guest>/);
  assert.ok(buildHTML(st).includes(banner));
});

test("wired supervision outside the current PATH asks for a new terminal", () => {
  const st = state({
    supervision: { wrappers: true, block: true, on_path: false, active: true },
  });
  const banner = supervisionBanner(st);
  assert.match(banner, /open a new terminal to finish setup/);
  assert.match(banner, /supervision-banner--info/);
  assert.doesNotMatch(banner, /turn it on/);
});

test("supervision banner is absent when active or explicitly opted out", () => {
  const active = state({
    supervision: { wrappers: true, block: true, on_path: true, active: true },
  });
  assert.equal(supervisionBanner(active), "");
  assert.doesNotMatch(buildHTML(active), /supervision-banner/);

  const optedOut = state({
    settings: { theme: "light", supervise_shell: false },
    supervision: { wrappers: true, block: false, on_path: false, active: false },
  });
  assert.equal(supervisionBanner(optedOut), "");
  assert.doesNotMatch(buildHTML(optedOut), /terminal supervision is off/);
});

test("settings wires its actions", () => {
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back", notify: true } });
  assert.match(set, /data-action="set_strategy"[^>]*data-value="most_headroom"/);
  assert.match(set, /data-action="set_theme"[^>]*data-value="dark"/);
  assert.match(set, /data-key="supervise_shell"/);
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

test("add: codex 'token' method pastes an auth.json textarea in-app", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "token" }));
  assert.match(h, /id="add-token"[^>]*placeholder="[^"]*auth.json/);
  assert.ok(h.includes("save the seat →"));                    // in-app paste, not Terminal
});

test("add: codex token copy drops the unsupported 'API key' promise", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "token" }));
  assert.ok(h.includes("auth.json"));
  assert.doesNotMatch(h, /API key/i);                          // engine can't accept one → don't promise it
});

test("add: codex offers one-tap import when signed in to an unregistered account", () => {
  const st = { settings: {}, codex_live_unregistered: { email: "live@x.com" } };
  const h = buildAddSeat(st, mkAdd({ step: "details", provider: "codex" }));
  assert.match(h, /data-action="add-import"/);           // the one-tap card
  assert.ok(h.includes("use live@x.com"));                // shows the detected account
  assert.ok(h.includes("already signed in on this Mac"));
});

test("add: no import card when there's no unregistered live codex account", () => {
  assert.doesNotMatch(buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex" })),
    /data-action="add-import"/);
  // and never for claude (its live creds carry no derivable email — import is codex-only)
  assert.doesNotMatch(
    buildAddSeat({ settings: {}, codex_live_unregistered: { email: "x@x.com" } },
      mkAdd({ step: "details", provider: "claude" })),
    /data-action="add-import"/);
});

test("add: import card escapes the detected email", () => {
  const st = { settings: {}, codex_live_unregistered: { email: 'a<b>"@x.com' } };
  const h = buildAddSeat(st, mkAdd({ step: "details", provider: "codex" }));
  assert.ok(h.includes("use a&lt;b&gt;&quot;@x.com"));
  assert.doesNotMatch(h, /use a<b>/);
});

test("add: paste flow surfaces the auth.json path + a Reveal in Finder shortcut", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "token" }));
  assert.ok(h.includes("~/.codex/auth.json"));           // tells the user WHERE the file is
  assert.match(h, /data-action="add-reveal"/);           // Finder shortcut
  // the browser method shows neither the path row nor a reveal button
  const browser = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "browser" }));
  assert.doesNotMatch(browser, /data-action="add-reveal"/);
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

test("reduceReply: one-tap import success → done (importing cleared)", () => {
  const add = mkAdd({ step: "connecting", provider: "codex", method: "browser", pending: true, importing: true });
  const o = reduceReply(UI({ screen: "add", add }), { ok: true, added: "live@x.com", add_op: true });
  assert.equal(o.add.step, "done"); assert.equal(o.add.pending, false); assert.equal(o.add.importing, false);
  assert.equal(o.closeFlow, o.add);
});

test("reduceReply: import error → back to details (unlike a browser-save), importing cleared", () => {
  const add = mkAdd({ step: "connecting", provider: "codex", method: "browser", pending: true, importing: true });
  const o = reduceReply(UI({ screen: "add", add }), { ok: false, error: "already on the list", add_op: true });
  assert.equal(o.add.step, "details");            // an in-app save returns to the form, not connecting
  assert.equal(o.add.pending, false); assert.equal(o.add.importing, false);
  assert.equal(o.flash, "already on the list");
});

test("add: connecting shows a saving spinner and NO save button while importing", () => {
  const h = buildAddSeat({ settings: {} },
    mkAdd({ step: "connecting", provider: "codex", method: "browser", pending: true, importing: true }));
  assert.match(h, /class="add-spin"/);
  assert.ok(h.includes("saving your seat…"));
  assert.doesNotMatch(h, /add-save/);             // import is not the browser "save my seat" handshake
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

// Milestone 6: bridge-shaped key metadata/catalogs; all earlier tests above remain unchanged.
import { buildAddKey, buildModelPicker, keySeatCard, keyConfirmations, keyRequest, reduceKeyReply, KEY_PROVIDERS, formatPrice } from "./render.mjs";

test("Langdock routes visibly distinguish model families and harnesses, sharing one key", () => {
  const html = buildAddKey({ settings: {} }, { step: "provider" });
  const row = (id) => html.match(new RegExp(`<button[^>]*data-provider="${id}"[\\s\\S]*?</button>`))?.[0];
  assert.match(row("langdock"), /langdock · openai models/);
  assert.match(row("langdock"), /codex cli · responses/);
  assert.match(row("langdock_anthropic"), /langdock · claude models/);
  assert.match(row("langdock_anthropic"), /claude code · messages/);
  assert.match(html, /same langdock key works for both routes/);
  assert.equal(KEY_PROVIDERS.langdock.harness, "codex");
  assert.equal(KEY_PROVIDERS.langdock_anthropic.harness, "claude");
});

test("both Langdock add-key routes retain eu, us and global in details and requests", () => {
  for (const provider of ["langdock", "langdock_anthropic"]) {
    for (const region of ["eu", "us", "global"]) {
      const flow = { step: "details", provider, region, secret: " test-secret ", label: "work" };
      const html = buildAddKey({ settings: {} }, flow);
      assert.match(html, /id="key-region"/);
      assert.ok(html.includes(`<option value="${region}" selected>`));
      assert.deepEqual(keyRequest(flow), { provider, region, secret: "test-secret", allow_unverified: false });
    }
  }
});

test("Langdock Claude discovery carries its route and region through the shipped bundle", () => {
  const app = keyApp();
  app.click({ action: "key-start" });
  app.click({ action: "key-provider", provider: "langdock_anthropic" });
  app.input("key-label", "Claude work"); app.input("key-secret", "test-secret");
  app.handlers.change({ target: { id: "key-region", value: "global", closest: () => null } });
  app.click({ action: "key-discover" });
  const request = app.sent.at(-1);
  assert.equal(request.action, "models_list");
  assert.equal(request.provider, "langdock_anthropic");
  assert.equal(request.region, "global");
  app.window.AGL.result({ key_action: "models_list", key_request_id: request.key_request_id,
    ok: false, models: [], error: "no models returned; check this key and endpoint" });
  assert.match(app.root.innerHTML, /no models returned/);
  assert.match(app.root.innerHTML, /claude code sessions/);
});
import { runInNewContext } from "node:vm";

const keySeat = (over = {}) => ({ id: "key-1", label: "late-night", provider: "openrouter",
  harness: "codex", model: "vendor/model", responses_verified: true, ...over });
const keyFlow = (over = {}) => ({ step: "details", provider: "openrouter", secret: "test-secret",
  label: "late-night", region: "eu", base_url: "", allow_unverified: false, ...over });
const livePrice = (input = "1", output = "3", over = {}) => ({ source: "live", currency: "USD",
  token_unit: "per_million_tokens", estimate: true, verified_at: "2026-01-01T00:00:00Z",
  rates: { input: { status: input == null ? "unknown" : "known", value: input },
    output: { status: output == null ? "unknown" : "known", value: output } }, ...over });
const keyPrompt = (over = {}) => ({ id: "prompt-1", status: "pending", tool: "codex",
  expires_at: "2099-01-01T00:00:00Z", from_seat: { id: "work@x.com", label: "work" },
  key_seat: keySeat(), price: livePrice(), ...over });

test("key seat card sits in its harness group, with model instead of usage bars", () => {
  const h = buildHTML(state({ keys: [keySeat()] }));
  assert.match(h, /seat--key/); assert.match(h, /late-night/); assert.match(h, /vendor\/model/);
  assert.match(h, /openrouter · codex cli/);
  assert.doesNotMatch(h, /class="track"|\$0|USD 0/);
  assert.ok(h.indexOf("late-night") < h.indexOf('g-name">Claude'));
  // No running cost is shown at all: a money figure needs a price, and the providers most likely
  // to be used as key seats publish none. Supplied spend metadata must not resurrect the display.
  assert.doesNotMatch(keySeatCard(keySeat({ spend: { amount: "0.000012", currency: "USD" } })),
    /0\.000012|running cost/);
});

test("unproven responses seats say so plainly; anthropic uses messages with claude", () => {
  assert.match(keySeatCard(keySeat({ responses_verified: false })), /responses support unproven.*may not work/);
  assert.doesNotMatch(keySeatCard(keySeat()), /key-unproven/);
  const h = keySeatCard(keySeat({ provider: "anthropic", harness: "claude", responses_verified: false }));
  assert.match(h, /anthropic · claude code/); assert.doesNotMatch(h, /responses support unproven/);
});

test("endpoint proof renders four distinct outcomes and success removes the warning", () => {
  const cases = [
    ["proven", null, /proven — a Responses turn completed/],
    ["incompatible", "request_rejected", /does not support Responses; this seat will not work/],
    ["refused", "invalid_key", /refused.*authentication.*Responses support is undetermined/],
    ["inconclusive", "timeout", /inconclusive.*timed out.*Responses support is undetermined/],
  ];
  for (const [outcome, error, expected] of cases) {
    const h = keySeatCard(keySeat({ responses_verified: outcome === "proven", last_proof: {
      outcome, error, model: "checked-model", checked_at: "2026-09-26T12:00:00Z",
    } }));
    assert.match(h, expected);
    assert.match(h, /checked-model/); assert.match(h, /2026-09-26/);
    if (outcome === "proven") assert.doesNotMatch(h, /key-unproven|may not work|data-action="key-prove"/);
    else {
      assert.match(h, /data-action="key-prove"/);
      assert.match(h, /check this endpoint/);
      assert.match(h, /one real request and costs a small amount of money/);
    }
    if (outcome === "refused" || outcome === "inconclusive") assert.doesNotMatch(h, /does not support|will not work/);
  }
  // A fresh negative verdict beats an old registry-derived positive flag.
  const negative = keySeatCard(keySeat({ last_proof: { outcome: "incompatible" } }));
  assert.match(negative, /will not work/); assert.match(negative, /data-action="key-prove"/);
  const proven = keySeatCard(keySeat({ last_validation: { operation_permitted: false },
    last_proof: { outcome: "proven" } }));
  assert.doesNotMatch(proven, /unproven/);
});

test("proof rendering uses fixed refusal copy and escapes stored metadata", () => {
  for (const [error, reason] of [["insufficient_quota", "quota"], ["rate_limited", "rate limit"],
                                 ["<script>secret</script>", "access or billing"]]) {
    const h = keySeatCard(keySeat({ responses_verified: false, last_proof: {
      outcome: "refused", error, model: "<img>", checked_at: "<script>",
    } }));
    assert.ok(h.includes(`(${reason})`));
    assert.doesNotMatch(h, /<img>|<script>|secret/);
    assert.match(h, /&lt;img&gt;/);
  }
  assert.doesNotMatch(keySeatCard(keySeat({ harness: "claude", responses_verified: false })), /key-prove/);
});

test("priced picker orders by named input $/Mtok, retains both rates and cached age", () => {
  const models = [
    { id: "a-expensive", price: livePrice("9", "2") },
    { id: "b-unknown" }, { id: "z-cheap", price: livePrice("1", "8") },
  ];
  const h = buildModelPicker(keyFlow({ catalog: { sort_key: "input_usd_per_million_tokens", models,
    source: "cache", fetched_at: "2026-01-01T00:00:00Z", potentially_stale: true } }));
  assert.match(h, /sorted by input \$\/Mtok · cheap to expensive/);
  assert.ok(h.indexOf('data-model="z-cheap"') < h.indexOf('data-model="a-expensive"'));
  assert.ok(h.indexOf('data-model="a-expensive"') < h.indexOf('data-model="b-unknown"'));
  assert.match(h, /input \$1\/Mtok · output \$8\/Mtok/);
  assert.match(h, /live price estimate/); assert.match(h, /cached live catalog · updated \d+d ago · over 24h old/);
  assert.match(h, /price unavailable/);
  assert.deepEqual(models.map((m) => m.id), ["a-expensive", "b-unknown", "z-cheap"]);
});

test("unpriced picker sorts by id, has no price column, and explains missing prices", () => {
  const h = buildModelPicker(keyFlow({ provider: "openai", catalog: { sort_key: "id", models: [
    { id: "z-model" }, { id: "a-model" },
  ] } }));
  assert.ok(h.indexOf('data-model="a-model"') < h.indexOf('data-model="z-model"'));
  assert.match(h, /does not publish machine-readable prices · sorted by model id/);
  assert.match(h, /provider pricing and budget controls/);
  assert.doesNotMatch(h, /key-price|\$|Mtok|input price|output price/);
  const unavailable = buildModelPicker(keyFlow({ catalog: { sort_key: "id", models: [] } }));
  assert.match(unavailable, /prices unavailable from this catalog/);
  assert.doesNotMatch(unavailable, /does not publish/);
});

test("confirmation uses quiet hierarchy, names both seats, and preserves accessible decisions", () => {
  const h = keyConfirmations({ pending_key_switches: [keyPrompt()] });
  assert.match(h, /class="k-q">use a paid key to keep going\?/);
  assert.match(h, /class="quiet-meta">late-night · openrouter<br><span class="k-model">vendor\/model/);
  assert.match(h, /class="fare"[^>]*>\$1<span class="unit" style="font-size:13px"> \/ \$3<\/span><\/span>/);
  assert.match(h, /input \/ output per million tokens/);
  assert.match(h, /live price estimate/);
  assert.match(h, /class="k-fine">work is resting\. this app doesn't cap spend\./);
  assert.match(h, /aria-label="paid key confirmation"/); assert.match(h, /aria-live="polite"/);
  const buttons = h.match(/<button[^>]+>[^<]+<\/button>/g);
  assert.equal(buttons.length, 2);
  assert.ok(buttons.every((b) => b.includes('data-id="prompt-1"') && b.includes('data-action="key-answer"')));
  assert.match(buttons[0], /class="k-no".*>not now<\/button>/);
  assert.match(buttons[1], /class="k-go".*>use the key<\/button>/);
  assert.match(buttons[0], /data-approved="false"/); assert.match(buttons[1], /data-approved="true"/);
  assert.match(keyConfirmations({ pending_key_switches: [keyPrompt({ price: null })] }), /price unavailable/);
  assert.doesNotMatch(keyConfirmations({ pending_key_switches: [keyPrompt({ expires_at: "2000-01-01" })] }), /data-action="key-answer"/);
  assert.equal((keyConfirmations({ pending_key_switches: [keyPrompt()] }, new Set(["prompt-1"])).match(/ disabled/g) || []).length, 2);
});

test("price formatting strips zeros and keeps three significant figures for cheap models", () => {
  for (const [value, expected] of [["3.000000", "$3"], [15, "$15"], [3.25, "$3.25"],
    [3.256, "$3.26"], [1.005, "$1.01"], [0.5, "$0.5"], [0.075, "$0.075"],
    [0.0002, "$0.0002"], [0.000201, "$0.000201"], [0.000202, "$0.000202"],
    [0.07549, "$0.0755"], [0.00000001, "$0.00000001"], [0, "$0"], ["0.000", "$0"]]) {
    assert.equal(formatPrice(value), expected, String(value));
  }
  for (const value of [null, undefined, "", " ", false, NaN, Infinity, "oops", -1]) {
    assert.equal(formatPrice(value), "price unavailable");
  }
  const catalog = { sort_key: "input_usd_per_million_tokens", models: [
    { id: "cheap", price: livePrice("0.000201", "3.000000") },
    { id: "nearby", price: livePrice("0.000202", "15.000000") },
  ] };
  const picker = buildModelPicker(keyFlow({ catalog }));
  assert.match(picker, /input \$0\.000201\/Mtok · output \$3\/Mtok/);
  assert.match(picker, /input \$0\.000202\/Mtok · output \$15\/Mtok/);
  const review = buildAddKey(state(), keyFlow({ step: "review", catalog, model: "cheap" }));
  assert.match(review, /input \$0\.000201\/Mtok · output \$3\/Mtok/);
  const confirmation = keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("3.000000", "15.000000") })] });
  assert.match(confirmation, /class="fare"[^>]*>\$3<span[^>]*> \/ \$15<\/span>/);
});

test("unknown fare is explicit and both decisions remain usable", () => {
  const h = keyConfirmations({ pending_key_switches: [keyPrompt({ price: null })] });
  assert.match(h, /class="fare"[^>]*>price unavailable<\/span>/);
  assert.doesNotMatch(h, /\$\d| disabled/);
  assert.equal((h.match(/data-action="key-answer"/g) || []).length, 2);
});

test("confirmation takes its primary colour from the request harness", () => {
  for (const tool of ["codex", "claude"]) {
    // Deliberately disagree with the key fixture: the request owns this session's accent.
    const h = keyConfirmations({ pending_key_switches: [keyPrompt({ tool,
      key_seat: keySeat({ harness: tool === "claude" ? "codex" : "claude" }) })] });
    assert.ok(h.includes(`style="--accent:var(--${tool})"`));
    assert.match(h, /class="k-go".*data-approved="true"/);
  }
});

test("confirmation names the resting seat and only includes a known reset time", () => {
  for (const limited_until of ["2099-01-01T02:18:00", null, "invalid"]) {
    const h = keyConfirmations(state({ pending_key_switches: [keyPrompt({ from_seat: { id: "work@x.com", label: "Work" } })],
      tools: { codex: { seats: [seat({ limited_until })] },
        claude: { seats: [seat({ limited_until: "2099-01-01T05:00:00" })] } } }));
    if (limited_until?.startsWith("2099")) {
      assert.match(h, /class="k-fine">Work is resting until 2:18am\. this app doesn't cap spend\./);
    } else {
      assert.match(h, /class="k-fine">Work is resting\. this app doesn't cap spend\./);
      assert.doesNotMatch(h, /until/);
    }
  }
});

test("confirmation retains the stored endpoint proof", () => {
  const h = keyConfirmations({ pending_key_switches: [keyPrompt()],
    keys: [keySeat({ last_proof: { outcome: "incompatible", model: "vendor/model" } })] });
  assert.match(h, /incompatible — this endpoint does not support Responses; this seat will not work/);
});

test("unknown amounts never become fabricated or bare zero prices in any key view", () => {
  for (const value of [null, undefined, "", " ", false, NaN, Infinity, "oops", -1]) {
    const price = livePrice(null, null);
    price.rates.input = price.rates.output = { status: "known", value };
    const catalog = { sort_key: "input_usd_per_million_tokens", models: [{ id: "model", price }] };
    const views = [buildModelPicker(keyFlow({ catalog })),
      buildAddKey(state(), keyFlow({ step: "review", catalog, model: "model" })),
      keyConfirmations({ pending_key_switches: [keyPrompt({ price })] })];
    for (const h of views) {
      assert.match(h, /unavailable/);
      assert.doesNotMatch(h, /\$\d|USD \d|>0<|free/);
    }
  }
  const fabricated = livePrice("1", "3", { source: "vendored" });
  assert.match(keyConfirmations({ pending_key_switches: [keyPrompt({ price: fabricated })] }), /price unavailable/);
  // A verified zero really is zero; tiny nonzero rates must never round down to it.
  assert.match(keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("0", "0.00000001") })] }), /input \$0\/Mtok · output \$0\.00000001\/Mtok/);
});

test("add key is pushed, gates unverified providers with an unchecked explicit choice, and collects routing", () => {
  const h = buildAddKey(state(), keyFlow({ provider: "openai_compatible" }));
  assert.match(h, /app set-app add-app/); assert.doesNotMatch(h, /modal|dialog/);
  assert.match(h, /id="key-base-url"/); assert.match(h, /id="key-secret" type="password"/);
  assert.match(h, /cannot promise.*responses endpoint works/);
  assert.match(h, /id="key-ack" type="checkbox">/);
  assert.match(h, /i understand it may not work/);
  const region = buildAddKey(state(), keyFlow({ provider: "langdock", region: "us" }));
  assert.match(region, /value="us" selected/); assert.match(region, /value="global"/);
  const providers = buildAddKey(state(), keyFlow({ step: "provider" }));
  assert.doesNotMatch(providers, /data-provider="(?:together|mistral|cerebras)"/);
  assert.equal(KEY_PROVIDERS.anthropic.harness, "claude");
});

test("key requests match 5b, and replies correlate to the current request", () => {
  assert.deepEqual(keyRequest(keyFlow({ provider: "langdock", region: "us" })),
    { provider: "langdock", secret: "test-secret", region: "us", allow_unverified: false });
  assert.deepEqual(keyRequest(keyFlow({ provider: "openai_compatible", base_url: " https://local.test/v1 ", allow_unverified: true })),
    { provider: "openai_compatible", secret: "test-secret", base_url: "https://local.test/v1", allow_unverified: true });
  const flow = keyFlow({ pending: "new", step: "connecting" });
  assert.equal(reduceKeyReply(flow, { key_request_id: "old", key_action: "models_list", ok: true, models: [] }), false);
  assert.equal(flow.step, "connecting");
  assert.equal(reduceKeyReply(flow, { state: state() }), false);
  assert.equal(reduceKeyReply(flow, { key_request_id: "new", key_action: "models_list", ok: false, error: "offline" }), true);
  assert.equal(flow.step, "details"); assert.equal(flow.secret, "test-secret");
  flow.pending = "save";
  reduceKeyReply(flow, { key_request_id: "save", key_action: "key_add", ok: true, added: "key-1", seat: keySeat() });
  assert.equal(flow.step, "done"); assert.equal(flow.secret, "");
});

test("key settings default to opt-in fallback and confirmation, with clear spending consequences", () => {
  const h = buildSettings(state());
  assert.match(h, /data-key="key_fallback" >/);
  assert.match(h, /data-key="confirm_key_switch" checked/);
  assert.match(h, /real money can be spent/); assert.match(h, /without asking again/);
});

// Exercise the shipped glue with a tiny DOM boundary. No provider, native app or third-party DOM.
function keyApp() {
  const handlers = {}, sent = [];
  const root = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [], firstElementChild: null };
  const document = { getElementById: () => root, body: { appendChild() {} },
    createElement: () => ({}), addEventListener: (name, fn) => { handlers[name] = fn; },
    hidden: true, hasFocus: () => false };
  const window = { webkit: { messageHandlers: { agl: { postMessage: (msg) => sent.push(msg) } } }, addEventListener() {} };
  runInNewContext(readFileSync(new URL("./bundle.js", import.meta.url), "utf8"),
    { document, window, console, setTimeout, setInterval, clearInterval, URL });
  const click = (dataset) => handlers.click({ target: { closest: () => ({ dataset }) }, preventDefault() {} });
  const input = (id, value) => handlers.input({ target: { id, value } });
  return { window, root, sent, click, input, handlers };
}

test("endpoint proof is click-only and duplicate clicks remain blocked across polls", () => {
  const app = keyApp();
  const snapshot = { rev: 1, settings: {}, tools: {}, keys: [keySeat({ responses_verified: false })] };
  app.window.AGL.result({ state: snapshot });
  assert.equal(app.sent.filter((m) => m.action === "key_prove").length, 0);
  app.click({ action: "key-prove", id: "key-1" });
  app.click({ action: "key-prove", id: "key-1" });
  app.window.AGL.result({ state: { ...snapshot, rev: 2 } });
  app.click({ action: "key-prove", id: "key-1" });
  const calls = app.sent.filter((m) => m.action === "key_prove");
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), { action: "key_prove", id: "key-1" });
  app.window.AGL.result({ key_action: "key_prove", key_target_id: "key-1", ok: true,
    state: { ...snapshot, rev: 3, keys: [keySeat({ last_proof: { outcome: "proven", model: "model" } })] } });
  assert.doesNotMatch(app.root.innerHTML, /responses support unproven|data-action="key-prove"/);
});

test("key glue calls models_list then key_add, preserves inputs across polls and answers both ways", () => {
  const app = keyApp();
  app.click({ action: "key-start" }); app.click({ action: "key-provider", provider: "langdock" });
  app.input("key-label", "night"); app.input("key-secret", "test-secret");
  app.handlers.change({ target: { id: "key-region", value: "us", closest: () => null } });
  const typedView = app.root.innerHTML;
  app.window.AGL.result({ state: { rev: 1, settings: {}, tools: {} } });
  assert.equal(app.root.innerHTML, typedView);
  app.click({ action: "key-discover" });
  const request = app.sent.at(-1);
  assert.equal(request.action, "models_list"); assert.equal(request.region, "us"); assert.equal(request.secret, "test-secret");
  app.window.AGL.result({ key_action: "models_list", key_request_id: request.key_request_id,
    ok: true, source: "live", sort_key: "id", models: [{ id: "model" }] });
  assert.match(app.root.innerHTML, /data-model="model"/);
  app.click({ action: "key-model", model: "model" }); app.click({ action: "key-save" });
  const save = app.sent.at(-1);
  assert.equal(save.action, "key_add"); assert.equal(save.label, "night"); assert.equal(save.model, "model");
  app.window.AGL.result({ key_action: "key_add", key_request_id: save.key_request_id, ok: true, added: "key-1" });
  assert.match(app.root.innerHTML, /your key seat's saved/); assert.doesNotMatch(app.root.innerHTML, /test-secret/);
  for (const approved of ["false", "true"]) {
    app.click({ action: "key-answer", id: `request-${approved}`, approved });
    assert.equal(app.sent.at(-1).action, "answer_key_switch");
    assert.equal(app.sent.at(-1).approved, approved === "true");
  }
});

test("key glue cannot discover an unverified endpoint without an explicit acknowledgement", () => {
  const app = keyApp();
  app.click({ action: "key-start" }); app.click({ action: "key-provider", provider: "openai_compatible" });
  app.input("key-label", "night"); app.input("key-secret", "test-secret"); app.input("key-base-url", "https://local.test/v1");
  app.click({ action: "key-discover" });
  assert.equal(app.sent.at(-1).action, "ready");
  app.handlers.change({ target: { id: "key-ack", checked: true, closest: () => null } });
  app.click({ action: "key-discover" });
  assert.equal(app.sent.at(-1).action, "models_list"); assert.equal(app.sent.at(-1).allow_unverified, true);
});

test("key confirmation is pinned above the scrolling list and escapes provider-controlled strings", () => {
  const request = keyPrompt({ key_seat: keySeat({ label: '<img src=x onerror="bad">', model: '<script>bad</script>' }) });
  const h = buildHTML(state({ pending_key_switches: [request] }));
  assert.ok(h.indexOf('class="key-prompts"') < h.indexOf('class="main-body"'));
  assert.doesNotMatch(h, /<img|<script>/);
  assert.match(h, /&lt;script&gt;/);
});

test("partial prices and same-as rates preserve unknowns and cannot recurse forever", () => {
  let h = keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("2", null) })] });
  assert.match(h, /input \$2\/Mtok · output price unavailable/);
  const price = livePrice("2", null);
  price.rates.output = { status: "same_as", same_as: "input" };
  h = keyConfirmations({ pending_key_switches: [keyPrompt({ price })] });
  assert.match(h, /input \$2\/Mtok · output \$2\/Mtok/);
  price.rates.input = { status: "same_as", same_as: "output" };
  h = keyConfirmations({ pending_key_switches: [keyPrompt({ price })] });
  assert.match(h, /price unavailable/); assert.doesNotMatch(h, /\$\d/);
  h = keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("2", "4", { currency: "EUR" }) })] });
  assert.doesNotMatch(h, /\$\d/);
});

test("a running paid seat offers a pinned one-tap stop that sends the kill switch action", () => {
  const app = keyApp();
  const snapshot = state({ rev: 1, settings: { key_fallback: true },
    keys: [keySeat()], running_key_seats: ["key-1"] });
  app.window.AGL.result({ state: snapshot });
  const h = app.root.innerHTML;
  assert.match(h, /<h2 class="k-q">paid use:<\/h2>/);
  assert.match(paidStrip(h), /late-night · vendor\/model/);
  assert.match(h, /data-action="key-stop">stop paid use<\/button>/);
  assert.ok(h.indexOf('data-action="key-stop"') < h.indexOf('class="main-body"'));
  assert.match(h, /stops every session and new paid requests/);
  assert.match(h, /sent turns may still bill/);
  app.click({ action: "key-stop" });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))),
    { action: "toggle", key: "key_fallback", value: false });
  app.window.AGL.result({ state: { ...snapshot, rev: 2, settings: { key_fallback: false } } });
  assert.match(app.root.innerHTML, /paid use is stopping/);
  assert.match(app.root.innerHTML, /data-action="key-stop" disabled/);
  app.window.AGL.result({ state: { ...snapshot, rev: 2, running_key_seats: [],
    settings: { key_fallback: false } } });
  assert.doesNotMatch(app.root.innerHTML, /data-action="key-stop"/);
});

test("idle keys and subscription sessions do not offer the paid-session stop", () => {
  const h = buildHTML(state({ keys: [keySeat()], running_key_seats: [],
    tools: { codex: { seats: [seat({ status: "active", active: true, in_session: true })] } } }));
  assert.doesNotMatch(h, /data-action="key-stop"/);
});

const paidStrip = (html) => html.match(/<div class="paid-use-control"[^>]*>[\s\S]*?<\/div>/)?.[0];
const paidPin = (over = {}) => ({ pin: "first-pin", pid: 202, tool: "codex", email: "key-1",
  key_seat: keySeat(), ...over });

test("paid-use strip names and escapes the running pin's seat and model even after key removal", () => {
  const html = buildHTML(state({ keys: [], running_key_seats: ["key-1"],
    pinned_sessions: [paidPin({ key_seat: keySeat({ label: "late <shift>", model: "guest/<model>" }) })] }));
  const strip = paidStrip(html);
  assert.ok(strip);
  assert.match(strip, /late &lt;shift&gt; · guest\/&lt;model&gt; \(codex · terminal 202\)/);
  assert.doesNotMatch(strip, /<shift>|<model>/);
  assert.match(strip, /aria-label="stop paid use" role="status"/);
  assert.ok(html.indexOf(strip) < html.indexOf('data-key="auto_switch"'));
});

test("paid-use strip names both running sessions and makes its global stop explicit", () => {
  const second = keySeat({ id: "key-2", label: "writing", model: "claude-model", harness: "claude" });
  const strip = paidStrip(buildHTML(state({ running_key_seats: ["key-1", "key-2"],
    pinned_sessions: [paidPin(), paidPin({ pin: "second-pin", pid: 303, tool: "claude",
      email: "key-2", key_seat: second })] })));
  assert.match(strip, /late-night · vendor\/model \(codex · terminal 202\)/);
  assert.match(strip, /writing · claude-model \(claude · terminal 303\)/);
  assert.match(strip, /stops every session and new paid requests/);
  assert.match(strip, /sent turns may still bill/);
  assert.equal((strip.match(/data-action="key-stop"/g) || []).length, 1);
});

test("paid-use strip keeps two terminals on the same key individually identifiable", () => {
  const strip = paidStrip(buildHTML(state({ running_key_seats: ["key-1"],
    pinned_sessions: [paidPin(), paidPin({ pin: "second-pin", pid: 303 })] })));
  assert.equal((strip.match(/late-night · vendor\/model/g) || []).length, 2);
  assert.match(strip, /terminal 202/);
  assert.match(strip, /terminal 303/);
});

test("paid-use strip also names automatic fallback seats alongside pinned sessions", () => {
  const strip = paidStrip(buildHTML(state({ running_key_seats: ["key-1", "key-2"],
    keys: [keySeat({ id: "key-2", label: "fallback", model: "other/model" })],
    pinned_sessions: [paidPin()] })));
  assert.match(strip, /late-night · vendor\/model/);
  assert.match(strip, /fallback · other\/model/);
});

test("paid-use strip does not render without running sessions", () => {
  for (const snapshot of [state(), state({ keys: [keySeat()], running_key_seats: [], pinned_sessions: [] })]) {
    const html = buildHTML(snapshot);
    assert.equal(paidStrip(html), undefined);
    assert.doesNotMatch(html, /paid-use-control|data-action="key-stop"/);
  }
});

test("paid-use strip keeps session names visible while stopping and disables the action", () => {
  const strip = paidStrip(buildHTML(state({ settings: { key_fallback: false },
    running_key_seats: ["key-1"], pinned_sessions: [paidPin()] })));
  assert.match(strip, /paid use is stopping…/);
  assert.match(strip, /late-night · vendor\/model/);
  assert.match(strip, /data-action="key-stop" disabled>stop paid use/);
  assert.match(strip, /sent turns may still bill/);
});

test("paid-use strip has a heading and real button without the confirmation's card layout", () => {
  const html = buildHTML(state({ running_key_seats: ["key-1"], pinned_sessions: [paidPin()],
    pending_key_switches: [keyPrompt()] }));
  const strip = paidStrip(html);
  assert.ok(strip);
  assert.doesNotMatch(strip, /key-confirm|set-card|k-fine|k-acts|k-go|<section/);
  assert.match(strip, /<h2 class="k-q">paid use:<\/h2>/);
  assert.match(strip, /<button class="btn rose" data-action="key-stop">stop paid use<\/button>/);
  assert.doesNotMatch(strip, /class="link"/);
  assert.match(strip, /<p>late-night · vendor\/model \(codex · terminal 202\)<\/p>/);
  assert.equal((strip.match(/<div/g) || []).length, 1);
  assert.match(html, /<section class="key-confirm set-card" aria-label="paid key confirmation"/);
});

test("pinned cards retain per-session end actions alongside the global paid-use strip", () => {
  const html = buildHTML(state({ running_key_seats: ["key-1"],
    pinned_sessions: [paidPin(), paidPin({ pin: "second-pin", pid: 303, end_requested: true })] }));
  assert.doesNotMatch(paidStrip(html), /end-pinned-session/);
  const cards = [...html.matchAll(/<section class="seat seat--key pinned-session"[\s\S]*?<\/section>/g)];
  assert.equal(cards.length, 2);
  assert.match(cards[0][0], /data-action="end-pinned-session" data-tool="codex" data-pin="first-pin">end<\/button>/);
  assert.match(cards[1][0], /data-action="end-pinned-session" data-tool="codex" data-pin="second-pin" disabled>ending…<\/button>/);
});

test("paid-use settings subtitle explains prevention, session stopping, and in-flight billing", () => {
  const h = buildSettings(state());
  const row = h.match(/<label class="set-toggle-row">(?:(?!<\/label>)[\s\S])*data-key="key_fallback"(?:(?!<\/label>)[\s\S])*<\/label>/)[0];
  assert.match(row, /class="set-s">[^<]*off prevents new paid use and stops a running paid session/);
  assert.match(row, /the turn already sent may still bill/);
});

test("each key seat offers use in new terminal with its exact id", () => {
  const keys = [
    { id: 'paid-1', label: 'late shift', harness: 'codex', provider: 'openrouter', model: 'guest/model' },
    { id: 'paid-2', label: 'writing', harness: 'claude', provider: 'anthropic', model: 'claude-model' },
  ];
  const html = buildHTML(state({ keys }));
  for (const key of keys) {
    assert.ok(html.includes(`data-action="key-terminal" data-id="${key.id}">use in new terminal</button>`));
  }
  const app = readFileSync(new URL('./app.mjs', import.meta.url), 'utf8');
  assert.match(app, /send\("key_terminal", \{ id: el.dataset.id \}\)/);
});

test("pinned paid terminals have independent rows and end controls", () => {
  const key = { id: 'paid-1', label: 'late <shift>', provider: 'openrouter', model: 'guest/model' };
  const pinned_sessions = [
    { pin: 'first-pin', pid: 202, tool: 'codex', key_seat: key },
    { pin: 'second-pin', pid: 303, tool: 'codex', key_seat: key, end_requested: true },
  ];
  const html = buildHTML(state({ pinned_sessions }));
  assert.equal((html.match(/aria-label="pinned paid session"/g) || []).length, 2);
  assert.equal((html.match(/pinned · paid/g) || []).length, 2);
  assert.match(html, /late &lt;shift&gt;/);
  assert.match(html, /openrouter · codex · terminal 202/);
  assert.match(html, /guest\/model/);
  assert.match(html, /metered · paid per token/);
  assert.match(html, /data-action="end-pinned-session" data-tool="codex" data-pin="first-pin">end<\/button>/);
  assert.match(html, /data-pin="second-pin" disabled>ending…<\/button>/);
  const app = readFileSync(new URL('./app.mjs', import.meta.url), 'utf8');
  assert.match(app, /send\("end_pinned_session", \{ tool, pin: el.dataset.pin \}\)/);
});

test("pin confirmation describes one terminal without claiming subscriptions are resting", () => {
  const html = buildHTML(state({ pending_key_switches: [{
    id: 'consent-pin', pinned: true, status: 'pending', tool: 'codex',
    expires_at: '2999-01-01T00:00:00Z',
    key_seat: { id: 'key', label: 'work', provider: 'openrouter', model: 'guest/model' },
  }] }));
  assert.match(html, /pin this terminal to a paid key\?/);
  assert.match(html, /only this terminal will use the key/);
  assert.doesNotMatch(html, /the current seat is resting/);
  assert.match(html, /data-action="key-answer" data-id="consent-pin" data-approved="false"/);
  assert.match(html, /price unavailable/);
});

test("shipped pin controls send separate launch and per-terminal end actions", () => {
  const app = keyApp();
  app.click({ action: 'key-terminal', id: 'key-1' });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: 'key_terminal', id: 'key-1' });
  app.click({ action: 'end-pinned-session', tool: 'codex', pin: 'only-this-terminal' });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))),
    { action: 'end_pinned_session', tool: 'codex', pin: 'only-this-terminal' });
  assert.equal(app.sent.filter((m) => m.action === 'toggle').length, 0);
});

// The table below is duplicated verbatim in tests/test_price_format.py. Two languages format
// prices — Python for the CLI and terminal consent, JS for the popover — and they must never
// print different numbers for the same model. Changing one side fails the other.
test("prices read the same here as they do in the CLI", () => {
  const TABLE = {
    "3.000000": "3",
    "15": "15",
    "150": "150",
    "0.5": "0.5",
    "0.075": "0.075",
    "0.0002": "0.0002",
    "0.00000025": "0.00000025",
    "0": "0",
    "0.3125": "0.313",
    "1.005": "1.01",
    "0.6496": "0.65",
    "1.027": "1.03",
  };
  for (const [raw, shown] of Object.entries(TABLE)) {
    assert.equal(formatPrice(raw), `$${shown}`, `formatPrice(${raw})`);
  }
});

test("model filter matches id and display name without case sensitivity", () => {
  const catalog = { sort_key: "id", models: [
    { id: "GPT-5-mini", display_name: "Small helper" },
    { id: "vendor/large", display_name: "GPT flagship" },
    { id: "other", display_name: "Different model" },
  ] };
  for (const query of ["gpt", "GpT"]) {
    const h = buildModelPicker(keyFlow({ catalog, modelFilter: query }));
    assert.match(h, /data-model="GPT-5-mini"/);
    assert.match(h, /data-model="vendor\/large"/);
    assert.doesNotMatch(h, /data-model="other"/);
    assert.match(h, />2 of 3<\/div>/);
  }
  assert.match(buildModelPicker(keyFlow({ catalog })), />3 of 3<\/div>/);
});

test("filtering keeps the original ordering, sort header and price visibility", () => {
  const models = [
    { id: "a-match", price: livePrice("9", "2") },
    { id: "middle", price: livePrice("5", "3") },
    { id: "z-match", price: livePrice("1", "8") },
    { id: "unknown-match" },
  ];
  for (const sort_key of ["id", "input_usd_per_million_tokens"]) {
    const flow = keyFlow({ provider: "openai", catalog: { sort_key, models } });
    const original = buildModelPicker(flow);
    const filtered = buildModelPicker({ ...flow, modelFilter: "MATCH" });
    const ids = (h) => [...h.matchAll(/data-model="([^"]+)"/g)].map((m) => m[1]);
    assert.deepEqual(ids(filtered), ids(original).filter((id) => id.includes("match")));
    const header = (h) => h.match(/<div class="add-hint">([^<]*sorted by[^<]*)<\/div>/)[1];
    assert.equal(header(filtered), header(original));
    assert.match(filtered, />3 of 4<\/div>/);
    if (sort_key === "id") assert.doesNotMatch(filtered, /input \$|output \$|class="key-price/);
    else {
      assert.match(filtered, /input \$1\/Mtok · output \$8\/Mtok/);
      assert.match(filtered, /input \$9\/Mtok · output \$2\/Mtok/);
    }
  }
});

test("model filter empty state honestly names and escapes the query", () => {
  const h = buildModelPicker(keyFlow({ modelFilter: '<MiSs "me">',
    catalog: { sort_key: "id", models: [{ id: "model" }] } }));
  assert.match(h, /no models match “&lt;MiSs &quot;me&quot;&gt;”/);
  assert.match(h, />0 of 1<\/div>/);
  assert.doesNotMatch(h, /data-action="key-model"|<MiSs|no models returned/);
});

test("paid-use master switch names automatic fallback, pinned terminals and stopping", () => {
  const h = buildSettings(state());
  const row = h.match(/<label class="set-toggle-row">(?:(?!<\/label>)[\s\S])*data-key="key_fallback"(?:(?!<\/label>)[\s\S])*<\/label>/)[0];
  assert.match(row, /class="set-t">allow paid key use<\/span>/);
  assert.match(row, /on permits paid use: automatic fallback.*and pinned key terminals/);
  assert.match(row, /real money can be spent/);
  assert.match(row, /off prevents new paid use and stops a running paid session/);
  assert.doesNotMatch(row, /let a key take the floor/);
});

test("blocked named-seat launch offers informed inline enable-and-continue", () => {
  const app = keyApp();
  const snapshot = state({ rev: 1, settings: { key_fallback: false }, keys: [keySeat({ label: 'late <shift>' })] });
  app.window.AGL.result({ state: snapshot });
  app.click({ action: "key-terminal", id: "key-1" });
  app.window.AGL.result({ key_action: "key_terminal", key_target_id: "key-1", ok: false,
    code: "paid_use_disabled", error: "paid use is off", state: snapshot });
  assert.match(app.root.innerHTML, /app set-app add-app/);
  assert.match(app.root.innerHTML, /use late &lt;shift&gt; in a new terminal/);
  assert.match(app.root.innerHTML, /real money can be spent/);
  assert.match(app.root.innerHTML, /data-action="paid-key-enable">allow paid use and open terminal/);
  assert.doesNotMatch(app.root.innerHTML, /in settings|open settings|<shift>|modal/);
  const sentBeforeConsent = app.sent.length;
  app.window.AGL.result({ state: { ...snapshot, rev: 2 } });
  assert.equal(app.sent.length, sentBeforeConsent);
  app.click({ action: "paid-key-enable" });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))),
    { action: "key_terminal", id: "key-1", enable_paid: true });
  app.click({ action: "paid-key-enable" });
  assert.equal(app.sent.length, sentBeforeConsent + 1);
  app.window.AGL.result({ key_action: "key_terminal", key_target_id: "key-1", ok: true,
    state: { ...snapshot, rev: 3, settings: { key_fallback: true } } });
  assert.doesNotMatch(app.root.innerHTML, /data-action="paid-key-enable"/);
});

test("declining a blocked launch leaves paid use off and errors allow retry", () => {
  const app = keyApp();
  app.window.AGL.result({ state: state({ keys: [keySeat()] }) });
  const block = () => {
    app.click({ action: "key-terminal", id: "key-1" });
    app.window.AGL.result({ key_action: "key_terminal", key_target_id: "key-1", ok: false, code: "paid_use_disabled" });
  };
  block();
  const count = app.sent.length;
  app.click({ action: "paid-key-back" });
  assert.equal(app.sent.length, count);
  assert.doesNotMatch(app.root.innerHTML, /data-action="paid-key-enable"/);
  block();
  app.click({ action: "paid-key-enable" });
  app.window.AGL.result({ key_action: "key_terminal", key_target_id: "key-1", ok: false, error: "couldn't save that setting" });
  assert.match(app.root.innerHTML, /couldn't save that setting/);
  assert.match(app.root.innerHTML, /data-action="paid-key-enable">/);
});

test("typing filters only results, survives state pushes and resets on each picker visit", () => {
  const app = keyApp();
  app.click({ action: "key-start" }); app.click({ action: "key-provider", provider: "openai" });
  app.input("key-label", "night"); app.input("key-secret", "test-secret");
  const discover = () => {
    app.click({ action: "key-discover" });
    const request = app.sent.at(-1);
    app.window.AGL.result({ key_action: "models_list", key_request_id: request.key_request_id,
      ok: true, sort_key: "id", models: [{ id: "gpt-mini" }, { id: "other" }] });
  };
  discover();
  // The DOM boundary exposes a separate results node. Replacing root or touching the input
  // would discard the focused field/caret in the actual WebView.
  const results = { innerHTML: "" };
  app.root.querySelector = (selector) => selector === "#key-model-results" ? results : null;
  const pickerHTML = app.root.innerHTML;
  app.input("key-model-filter", "MINI");
  assert.equal(app.root.innerHTML, pickerHTML);
  assert.match(results.innerHTML, /data-model="gpt-mini"/);
  assert.doesNotMatch(results.innerHTML, /data-model="other"/);
  assert.match(results.innerHTML, />1 of 2<\/div>/);
  const filtered = results.innerHTML;
  app.window.AGL.result({ state: state({ rev: 1 }) });
  assert.equal(app.root.innerHTML, pickerHTML);
  assert.equal(results.innerHTML, filtered);
  app.click({ action: "key-model", model: "gpt-mini" });
  assert.match(app.root.innerHTML, /gpt-mini/);
  assert.doesNotMatch(app.root.innerHTML, /key-model-filter|MINI/);
  app.click({ action: "key-back" });
  assert.match(app.root.innerHTML, /data-model="other"/);
  assert.match(app.root.innerHTML, /id="key-model-filter"[^>]*value=""/);
  app.input("key-model-filter", "other");
  app.click({ action: "key-back" });
  discover();
  assert.match(app.root.innerHTML, /id="key-model-filter"[^>]*value=""/);
  app.click({ action: "key-model", model: "gpt-mini" });
  app.click({ action: "key-save" });
  assert.equal(app.sent.at(-1).model, "gpt-mini");
  assert.equal(Object.hasOwn(app.sent.at(-1), "modelFilter"), false);
});
