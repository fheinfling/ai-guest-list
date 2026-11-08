// Shipping UI contracts: roster and disclosure markup, pure helpers, and dispatcher integration.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  buildHTML, dotState, dotKey, doorKey, doorMark, creditLeft, pct, fmtCountdown, needsHello,
  signedOut, isActive, signinExpiring, usable, resetIsPast, switchNeedsConfirmation, seatIdentity, headerVerdict, seatWindows,
  buildSettings, buildAddSeat, reduceReply, supervisionBanner, engineBanner, fmtUsageAge, fmtSessionAge, updateClockText,
} from "./render.mjs";

const mkAdd = (over = {}) => ({ step: "provider", provider: null, name: "", method: "browser", token: "", ...over });

function seat(over = {}) {
  return { email: "work@x.com", name: "Work", kind: "business", plan: "Business", status: "ready",
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

test("ledger uses the app icon while the settings mark retains the live door state", () => {
  for (const door of ["open", "shut"]) {
    const html = buildHTML(state({ door }));
    assert.match(html, /<svg[^>]*class="app-icon"[^>]*role="img" aria-label="ai guest list"/);
    assert.doesNotMatch(html, /ambient-art|roster-door/);
    assert.match(doorMark({ door }), new RegExp(`door--${door}`));
  }
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
  assert.equal(fmtCountdown("2026-06-29T12:00:00Z", now), "1d");
  assert.equal(fmtCountdown("2026-07-05T11:00:00Z", now), "6d 23h");
  assert.equal(fmtCountdown("2026-07-05T13:00:00Z", now), "7d 1h");
});

test("local clickdummy type and tabular readings require no remote resources", () => {
  const html = buildHTML(state({ tools: { codex: { seats: [seat()] } } }));
  assert.match(html, /class="app-icon"[^>]*aria-label="ai guest list"/);
  assert.match(html, /<dt>email<\/dt><dd>work@x\.com/);
  assert.match(html, /class="seat-identity" title="Work · business">Work</);
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  assert.match(css, /--sans:"Hanken Grotesk",ui-rounded,/);
  assert.match(css, /font-variant-numeric:tabular-nums/);
  assert.match(css, /font-family:"Hanken Grotesk"; src:url\("fonts\/hankengrotesk.woff2"\)/);
  assert.doesNotMatch(css, /https?:/);
});

test("seat summaries retain status, countdown, switch and sign-in actions", () => {
  const mk = (status, extra) => buildHTML(state({ tools: {
    codex: { seats: [seat({ status, ...extra })] } } }));
  assert.match(mk("active"), /class="tag">on now/);
  assert.match(mk("ready"), /data-action="switch" data-tool="codex" data-email="work@x.com"/);
  assert.match(mk("resting", { limited_until: new Date(Date.now() + 6e6).toISOString() }), /until back/);
  assert.match(mk("queued"), /limited/);
  assert.match(mk("needs-login"), /data-action="add" data-tool="codex" data-email="work@x.com">sign in/);
});

for (const kind of ["seat", "roster"]) {
  test(`hostile status cannot forge markup in the ${kind} surface`, () => {
    const status = 'ready"><button data-action="remove" data-email="forged">& remove</button><i class="';
    const html = buildHTML(state({ tools: {
      codex: { seats: [seat({ status })] }, claude: { seats: [] },
    } }));
    if (kind === "seat") assert.match(html, /data-action="remove" data-tool="codex" data-email="work@x.com"/);
    else {
      const roster = buildHTML(state({ tools: { codex: { seats: [seat({ status }), seat({ email: "active@x", status: "active" })] } } }));
      assert.doesNotMatch(roster, /<button data-action="remove" data-email="forged"/);
      assert.match(roster, /80% left/);
    }
    assert.doesNotMatch(html, /<button data-action="remove" data-email="forged"/);
  });
}

test("stale usage preserves headroom and labels the last successful reading", () => {
const snapshot = state({ tools: { codex: { seats: [seat({ status: "active", usage_stale: true, usage_fetched_at: "2026-06-28T12:12:00Z" })] } } });
const main = rosterHTML(snapshot);
assert.equal((main.match(/aria-label="last known:/g) || []).length, 2);
assert.match(main, /data-usage-at="2026-06-28T12:12:00Z"/);
assert.match(main, /~80<small>%/);
assert.match(main, /week ~90%/);
assert.match(main, /last known reading/);
});

test("resting disclosures show one return countdown and retain reading provenance", () => {
  const html = buildHTML(state({ tools: { codex: { seats: [seat({ status: "resting",
    limited_until: "2026-09-13T18:57:00Z", usage_stale: true,
    usage_fetched_at: "2026-09-13T18:25:00Z", usage: { error: "network" },
  })] } } }));
  const card = detailsOf(html, "codex", "work@x.com");
  assert.equal((rosterOf(html).match(/data-clock-prefix=""/g) || []).length, 1);
  assert.match(card, /last known reading/);
  assert.doesNotMatch(html, /connection unavailable|class="issue /);
  assert.match(card, /data-usage-at="2026-09-13T18:25:00Z"/);
  assert.doesNotMatch(card, /taking a breather/);
});

test("unknown roster readings do not imply remaining credit from old cached values", () => {
const html = buildHTML(state({ tools: { codex: { seats: [seat({ status: "active", usage_unknown: true,
  usage5h: 100, usageWeek: 75, usage_fetched_at: "2026-09-13T12:00:00Z" })] } } }));
const main = rosterOf(html);
assert.doesNotMatch(main, /unknown/); // no reading is not news: no "usage unknown" anywhere
assert.match(main, /no reading yet/);
assert.doesNotMatch(main, /\d+%|class="bar"/);
assert.match(main, /data-usage-at="2026-09-13T12:00:00Z"/);
assert.match(detailsOf(html, "codex", "work@x.com"), /no reading yet/);
assert.doesNotMatch(html, /25% left/); // Unknown readings stay unknown in the inline details too.
});

test("missing usage is explicitly unknown in the roster and disclosures", () => {
const html = buildHTML(state({ tools: { codex: { seats: [seat({ status: "active", usage_unknown: true,
 usage5h: null, usageWeek: null, usage: { windows: {} } })] } } }));
assert.match(rosterOf(html), /class="fig">—</);
assert.match(rosterOf(html), /no reading yet/);
assert.doesNotMatch(rosterOf(html), /unknown/);
assert.doesNotMatch(rosterOf(html), /class="bar"|0%/);
assert.doesNotMatch(html, /style="width:0%"/);
assert.doesNotMatch(html, />0%<|>0% left</);
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
  assert.match(live, /class="tag">on now/);
  assert.match(live, /terminal attached/);
  assert.match(live, /last used/);
  assert.match(live, /data-session-at="2026-06-28T12:12:00Z"/);

  const idle = mk({ in_session: false });
  assert.match(idle, /class="tag">on now/);
  assert.doesNotMatch(idle, /terminal attached|data-session-at=/);
});

test("only the credential holder is on the floor; a parked terminal keeps its switch action", () => {
  const html = buildHTML(state({ tools: { codex: { seats: [
    seat({ email: "live@x.com", status: "active", active: true, in_session: true }),
    seat({ email: "parked@x.com", in_session: true, session_started_at: "2026-09-14T08:47:00Z" }),
  ] }, claude: { seats: [] } } }));
  assert.equal((html.match(/class="tag">on now/g) || []).length, 1);
  const parked = rowOf(html, "parked@x.com");
  const row = parked;
  const detail = detailsOf(html, "codex", "parked@x.com");
  assert.match(detail, /terminal attached/);
  assert.match(detail, /data-session-at="2026-09-14T08:47:00Z"/);
  assert.match(row, /data-action="switch" data-tool="codex" data-email="parked@x.com"/);
  assert.doesNotMatch(parked, /floor--live/);
});

test("parked terminals retain resting, queued, and sign-in signals beside the chip", () => {
  for (const [status, signal] of [["resting", /limited/], ["queued", /limited/],
    ["needs-login", /data-action="add"/]]) {
    const html = buildHTML(state({ tools: { codex: { seats: [seat({ status, in_session: true })] } } }));
    assert.match(html, /terminal attached/);
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

test("revoked entitlement asks for a subscription check on a non-active seat", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({
      status: "needs-login",
      active: false,
      entitlement_revoked: true,
    })] },
    claude: { seats: [] },
  } }));
  assert.match(html, /check your subscription to refresh usage/);
  assert.doesNotMatch(html, /data-action="add" data-tool="codex" data-email="work@x.com">sign in</);
  assert.doesNotMatch(html, />log in</);
});

test("reassurance never appears on active/ready seats", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ status: "active", active: true })] }, claude: { seats: [] } } }));
  assert.doesNotMatch(html, /taking a breather/);
});

test("both remaining windows are visible before opening either tool disclosure", () => {
for (const tool of ["codex", "claude"]) {
 const main = visibleMarkup(rosterHTML(state({ tools: { [tool]: { seats: [seat({ status: "active", usage5h: 0, usageWeek: 65 })] } } })));
 assert.match(main, /35<small>%<\/small>/);
 assert.match(main, /of week left/);
 assert.match(main, /5h 100%/);
 assert.match(main, /aria-label="35% of the week window left"/);
 assert.doesNotMatch(main, /class="track/);
}
});

test("inline details respect reported windows and retain both for legacy engines", () => {
  const renderSeat = (tool, usage) => buildHTML(state({ tools: {
    [tool]: { seats: [seat({ usage })] },
    [tool === "codex" ? "claude" : "codex"]: { seats: [] },
  } }));

  const weeklyOnly = renderSeat("codex", { reported_windows: ["weekly"] });
  assert.doesNotMatch(weeklyOnly, /<dt>5h<\/dt>/);
  assert.match(weeklyOnly, /<dt>week<\/dt>/);

  const legacy = renderSeat("codex", { windows: { weekly: { used_pct: 10 } } });
  assert.match(legacy, /<dt>5h<\/dt>/);
  assert.match(legacy, /<dt>week<\/dt>/);

  const claude = renderSeat("claude", { reported_windows: ["weekly"] });
  assert.doesNotMatch(claude, /<dt>5h<\/dt>/);
  assert.match(claude, /<dt>week<\/dt>/);
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
    "[data-reset-at]": [reset, rest, long], "[data-session-at]": [terminal], "[data-expire-at]": [] })[selector],
    set innerHTML(_) { assert.fail("clock ticks must preserve existing controls and focus"); } };
  updateClockText(root, Date.parse("2026-09-13T12:00:00Z"));
  assert.equal(age.textContent, "updated 30s ago");
  assert.equal(reset.textContent, "resets in 2m");
  assert.equal(rest.textContent, "back in 3m");
  assert.equal(long.textContent, "resets in 7d 1h");
  assert.equal(terminal.textContent, " · 11d");
});

test("provider throttling quietly retains usage", () => {
  const html = buildHTML(state({ tools: { claude: { seats: [seat({ usage_stale: true,
    usage: { error: "rate_limited" }, usage5h: 0, usageWeek: 65 })] } } }));
  assert.doesNotMatch(html, /updates throttled|class="issue /);
  assert.match(html, /35%/);
  assert.match(html, /waiting for first reading/);
});

test("expired Claude access token stays quiet without implying logout", () => {
  const active = buildHTML(state({ tools: {
    codex: { seats: [] },
    claude: { seats: [seat({ status: "active", active: true,
      usage: { error: "token_expired" } })] },
  } }));
  assert.doesNotMatch(active, /open Claude to refresh usage|class="issue /);
  assert.doesNotMatch(active, />log in<|sign in to refresh|retrying automatically/);

  const resting = buildHTML(state({ tools: {
    codex: { seats: [] },
    claude: { seats: [seat({ status: "resting", active: true, limited: true, usage5h: 100,
      limited_until: "2026-09-13T18:57:00Z", usage: { error: "token_expired" } })] },
  } }));
  assert.match(resting, /until back/);
  assert.doesNotMatch(resting, />log in<|usage refresh pending|sign in to refresh|class="issue /);
});

test("expired Codex token quietly awaits automatic recovery", () => {
  for (const active of [false, true]) {
    const html = buildHTML(state({ tools: {
      codex: { seats: [seat({ active, status: active ? "active" : "ready",
        usage: { error: "token_expired" } })] },
      claude: { seats: [] },
    } }));
    assert.doesNotMatch(html, /open Codex to refresh usage|class="issue /);
    assert.doesNotMatch(html, /open Claude|>log in<|sign in to refresh/);
  }
});

test("tool markers and written seat status do not rely on colour or emoji", () => {
const html = buildHTML(state({ tools: { codex: { seats: [seat({ status: "resting" })] }, claude: { seats: [seat()] } } }));
assert.match(html, /class="mark"/); assert.match(html, /class="mark mark--claude"/);
assert.match(html, /class="fig fig--time">—<\/span><span class="fig-l">limited<\/span>/);
assert.doesNotMatch(html, /🟢|🟡|🌸|🌿|💚/);
});

test("ledger has no headline and management lives in inline details", () => {
const html = buildHTML(state({ tools: { codex: { seats: [seat({ plan: "Business" })] } } }));
assert.doesNotMatch(html, /<h1|you can keep working|<table/);
assert.match(html, /class="tool" data-tool="codex"/);
assert.match(html, /class="seat-identity" title="Work · business">Work<\/span>/);
assert.match(detailsOf(html, "codex", "work@x.com"), /<dt>plan<\/dt><dd>Work · business/);
assert.doesNotMatch(html, /guest-drawer|seat-options/);
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
  assert.match(detailsOf(html, "codex", "team@x.com"), /Team seat · business · team/);
  assert.match(detailsOf(html, "claude", "max@x.com"), /Max seat · business · max/);
});

test("buildHTML escapes user content", () => {
  const html = buildHTML(state({ tools: {
    codex: { seats: [seat({ name: "<script>x" })] }, claude: { seats: [] } } }));
  assert.match(html, /&lt;script&gt;x/);
});

test("buildHTML default light theme for unknown", () => {
  assert.match(buildHTML(state({ settings: { theme: "evil" } })), /class="app ambient-app[^"]* theme-light/);
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
  assert.match(buildHTML(st), /issue--hard app-problem supervision-problem/);
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
  assert.match(banner, /issue--soft app-problem supervision-problem/);
  assert.doesNotMatch(banner, /turn it on/);
});

test("supervision banner is absent when active or explicitly opted out", () => {
  const active = state({
    supervision: { wrappers: true, block: true, on_path: true, active: true },
  });
  assert.equal(supervisionBanner(active), "");
  assert.doesNotMatch(buildHTML(active), /supervision-problem/);

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
  assert.match(html, /aria-label="add a subscription seat or API key"[\s\S]*data-action="add">a subscription seat/);              // header ＋ carries no tool
  assert.doesNotMatch(html, /data-action="add" data-tool="[^"]*" title="add a seat"/);
});

test("settings is a pushed sub-view, not a modal (spec §9.1)", () => {
  const set = buildSettings({ settings: { theme: "light", strategy: "soonest_back" } });
  // no dimming modal backdrop/sheet — it renders in place as the popover surface
  assert.doesNotMatch(set, /class="backdrop"/);
  assert.doesNotMatch(set, /class="sheet/);
  assert.match(set, /class="app set-app theme-light"/);
  // back chevron + done both pop to main
  assert.match(set, /data-action="settings-back"[^>]*aria-label="back"/);
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
  assert.ok(h.includes("ChatGPT subscription"));
  assert.ok(h.includes("Claude.ai subscription"));
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
  assert.match(h, /id="add-name"[^>]*placeholder="Work, Personal, Late-night"/);
  assert.doesNotMatch(h, /data-action="add-method"/);          // NO segmented control
  assert.doesNotMatch(h, /how should i sign you in/);          // NO method section
  assert.doesNotMatch(h, /id="add-token"/);                    // NO textarea
  assert.ok(h.includes("open sign-in"));                     // single browser CTA
});

test("add: codex 'token' method pastes an auth.json textarea in-app", () => {
  const h = buildAddSeat({ settings: {} }, mkAdd({ step: "details", provider: "codex", method: "token" }));
  assert.match(h, /id="add-token"[^>]*placeholder="[^"]*auth.json/);
  assert.ok(h.includes("save the seat"));                    // in-app paste, not Terminal
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
import { buildAddKey, buildModelPicker, keySeatDetails, keyConfirmations, keyRequest, reduceKeyReply, KEY_PROVIDERS, formatPrice } from "./render.mjs";

test("Langdock routes visibly distinguish model families and harnesses, sharing one key", () => {
  const html = buildAddKey({ settings: {} }, { step: "provider" });
  const row = (id) => html.match(new RegExp(`<button[^>]*data-provider="${id}"[\\s\\S]*?</button>`))?.[0];
  assert.match(row("langdock"), /langdock \(openai models\)/);
  assert.match(row("langdock"), /codex cli \(responses\)/);
  assert.match(row("langdock_anthropic"), /langdock \(claude models\)/);
  assert.match(row("langdock_anthropic"), /claude code \(messages\)/);
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
  assert.match(h, /class="seat first-of-tool key-seat"/); assert.match(h, /late-night/); assert.match(h, /vendor\/model/);
  assert.match(h, /<dt>provider<\/dt><dd>openrouter<\/dd>/);
  assert.doesNotMatch(h, /class="track"|\$0|USD 0/);
  assert.match(h, /data-action="seat-details" data-tool="key" data-email="key-1"/);
  // No running cost is shown at all: a money figure needs a price, and the providers most likely
  // to be used as key seats publish none. Supplied spend metadata must not resurrect the display.
  assert.doesNotMatch(keySeatDetails(keySeat({ spend: { amount: "0.000012", currency: "USD" } })),
    /0\.000012|running cost/);
});

test("unproven responses seats say so plainly; anthropic uses messages with claude", () => {
  assert.match(keySeatDetails(keySeat({ responses_verified: false })), /responses support unproven.*may not work/);
  assert.doesNotMatch(keySeatDetails(keySeat()), /key-unproven/);
  const h = keySeatDetails(keySeat({ provider: "anthropic", harness: "claude", responses_verified: false }));
  assert.match(h, /<dt>provider<\/dt><dd>anthropic<\/dd>/); assert.doesNotMatch(h, /responses support unproven/);
});

test("endpoint proof renders four distinct outcomes and success removes the warning", () => {
  const cases = [
    ["proven", null, /proven — a Responses turn completed/],
    ["incompatible", "request_rejected", /does not support Responses; this seat will not work/],
    ["refused", "invalid_key", /refused.*authentication.*Responses support is undetermined/],
    ["inconclusive", "timeout", /inconclusive.*timed out.*Responses support is undetermined/],
  ];
  for (const [outcome, error, expected] of cases) {
    const h = keySeatDetails(keySeat({ responses_verified: outcome === "proven", last_proof: {
      outcome, error, model: "checked-model", checked_at: "2026-09-26T12:00:00Z",
    } }));
    assert.match(h, expected);
    assert.match(h, /checked-model/); assert.match(h, /2026-09-26/);
    if (outcome === "proven") assert.doesNotMatch(h, /key-unproven|may not work|data-action="key-prove"/);
    else {
      assert.match(h, /data-action="key-prove"/);
      assert.match(h, /check this endpoint/);
      assert.match(h, /this sends a paid request\. price unavailable here/);
    }
    if (outcome === "refused" || outcome === "inconclusive") assert.doesNotMatch(h, /does not support|will not work/);
  }
  // A fresh negative verdict beats an old registry-derived positive flag.
  const negative = keySeatDetails(keySeat({ last_proof: { outcome: "incompatible" } }));
  assert.match(negative, /will not work/); assert.match(negative, /data-action="key-prove"/);
  const proven = keySeatDetails(keySeat({ last_validation: { operation_permitted: false },
    last_proof: { outcome: "proven" } }));
  assert.doesNotMatch(proven, /unproven/);
});

test("proof rendering uses fixed refusal copy and escapes stored metadata", () => {
  for (const [error, reason] of [["insufficient_quota", "quota"], ["rate_limited", "rate limit"],
                                 ["<script>secret</script>", "access or billing"]]) {
    const h = keySeatDetails(keySeat({ responses_verified: false, last_proof: {
      outcome: "refused", error, model: "<img>", checked_at: "<script>",
    } }));
    assert.ok(h.includes(`(${reason})`));
    assert.doesNotMatch(h, /<img>|<script>|secret/);
    assert.match(h, /&lt;img&gt;/);
  }
  assert.doesNotMatch(keySeatDetails(keySeat({ harness: "claude", responses_verified: false })), /key-prove/);
});

test("priced picker orders by named input $/Mtok, retains both rates and cached age", () => {
  const models = [
    { id: "a-expensive", price: livePrice("9", "2") },
    { id: "b-unknown" }, { id: "z-cheap", price: livePrice("1", "8") },
  ];
  const h = buildModelPicker(keyFlow({ catalog: { sort_key: "input_usd_per_million_tokens", models,
    source: "cache", fetched_at: "2026-01-01T00:00:00Z", potentially_stale: true } }));
  assert.match(h, /sorted by input price; unknown prices last/);
  assert.ok(h.indexOf('data-model="z-cheap"') < h.indexOf('data-model="a-expensive"'));
  assert.ok(h.indexOf('data-model="a-expensive"') < h.indexOf('data-model="b-unknown"'));
  assert.match(h, /class="model-rates"><span>\$1<\/span><span>\$8<\/span>/);
  assert.match(h, /price estimates/); assert.match(h, /cached price estimates; updated \d+d ago; over 24h old/);
  assert.match(h, /price unavailable/);
  assert.deepEqual(models.map((m) => m.id), ["a-expensive", "b-unknown", "z-cheap"]);
});

test("unpriced picker sorts by id, has no price column, and explains missing prices", () => {
  const h = buildModelPicker(keyFlow({ provider: "openai", catalog: { sort_key: "id", models: [
    { id: "z-model" }, { id: "a-model" },
  ] } }));
  assert.ok(h.indexOf('data-model="a-model"') < h.indexOf('data-model="z-model"'));
  assert.match(h, /publishes no machine-readable prices/);
  assert.match(h, /provider pricing and budget controls/);
  assert.doesNotMatch(h, /key-price|\$|Mtok|input price|output price/);
  const unavailable = buildModelPicker(keyFlow({ catalog: { sort_key: "id", models: [] } }));
  assert.match(unavailable, /publishes no machine-readable prices/);
  assert.doesNotMatch(unavailable, /model-rates/);
});

test("confirmation leads with the decision and preserves equally styled accessible answers", () => {
  const h = keyConfirmations({ pending_key_switches: [keyPrompt()] });
  assert.match(h, /<h1>use a paid key to keep going\?/);
  assert.match(h, /class="decision-seat">late-night/);
  assert.match(h, /class="k-model">vendor\/model/);
  assert.match(h, /class="key-price">input \$1 \/ output \$3<br>USD per million tokens/);
  assert.match(h, /class="k-fine">paid per token\. this app does not cap spend\./);
  assert.match(h, /aria-label="paid key confirmation"/); assert.match(h, /aria-live="polite"/);
  const buttons = h.match(/<button[^>]+>[^<]+<\/button>/g);
  assert.equal(buttons.length, 2);
  assert.ok(buttons.every((b) => b.includes('data-id="prompt-1"') && b.includes('data-action="key-answer"')));
  assert.match(buttons[0], /class="choice".*>not now<\/button>/);
  assert.match(buttons[1], /class="choice".*>use the key<\/button>/);
  assert.match(buttons[0], /data-approved="false"/); assert.match(buttons[1], /data-approved="true"/);
  assert.match(keyConfirmations({ pending_key_switches: [keyPrompt({ price: null })] }), /doesn't publish a price here/);
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
  assert.match(picker, />\$0<\/span><span>\.000201<\/span>/);
  assert.match(picker, />\$3<\/span>/);
  assert.match(picker, />\$0<\/span><span>\.000202<\/span>/);
  assert.match(picker, />\$15<\/span>/);
  const review = buildAddKey(state(), keyFlow({ step: "review", catalog, model: "cheap" }));
  assert.match(review, /input \$0\.000201\/Mtok; output \$3\/Mtok/);
  const confirmation = keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("3.000000", "15.000000") })] });
  assert.match(confirmation, /class="key-price">input \$3 \/ output \$15<br>/);
});

test("unknown fare is explicit and both decisions remain usable", () => {
  const h = keyConfirmations({ pending_key_switches: [keyPrompt({ price: null })] });
  assert.match(h, /class="key-price">[^<]*doesn't publish a price here/);
  assert.doesNotMatch(h, /\$\d| disabled/);
  assert.equal((h.match(/data-action="key-answer"/g) || []).length, 2);
});

test("consent uses the same choice styling for both harnesses", () => {
  for (const tool of ["codex", "claude"]) {
    const h = keyConfirmations({ pending_key_switches: [keyPrompt({ tool,
      key_seat: keySeat({ harness: tool === "claude" ? "codex" : "claude" }) })] });
    assert.match(h, /class="choice".*data-approved="false"/);
    assert.match(h, /class="choice".*data-approved="true"/);
    assert.doesNotMatch(h, /style="--accent/);
  }
});

test("consent shows the real request expiry without inventing subscription reset times", () => {
  for (const limited_until of ["2099-01-01T02:18:00", null, "invalid"]) {
    const h = keyConfirmations(state({ pending_key_switches: [keyPrompt()],
      tools: { codex: { seats: [seat({ limited_until })] } } }));
    assert.match(h, /late-night/);
    assert.match(h, /data-expire-at="2099-01-01T00:00:00Z"/);
    assert.doesNotMatch(h, /invalid|undefined|until|NaN/);
  }
});

test("confirmation retains the stored endpoint proof", () => {
  const h = keyConfirmations({ pending_key_switches: [keyPrompt()],
    keys: [keySeat({ last_proof: { outcome: "incompatible", model: "vendor/model" } })] });
  assert.match(h, /endpoint unproven; requests may still bill/);
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
      assert.match(h, /unavailable|doesn't publish a price here/);
      assert.doesNotMatch(h, /\$\d|USD \d|class="model-rates">free|is-free/);
    }
  }
  const fabricated = livePrice("1", "3", { source: "vendored" });
  assert.match(keyConfirmations({ pending_key_switches: [keyPrompt({ price: fabricated })] }), /doesn't publish a price here/);
  // A verified zero really is zero; tiny nonzero rates must never round down to it.
  assert.match(keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("0", "0.00000001") })] }), /input \$0 \/ output \$0\.00000001/);
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
  assert.match(h, /spend real money|real money can be spent/); assert.match(h, /without asking again/);
});

// Exercise the shipped glue with a tiny DOM boundary. No provider, native app or third-party DOM.
function keyApp(options = {}) {
  const handlers = {}, sent = [];
  const root = options.root || { innerHTML: "", querySelector: () => null, querySelectorAll: () => [], firstElementChild: null };
  const document = { getElementById: () => root, body: { appendChild() {} },
    createElement: options.createElement || (() => ({ innerHTML: "", firstElementChild: { textContent: "" }, querySelector: () => null })), addEventListener: (name, fn) => { handlers[name] = fn; },
    hidden: true, hasFocus: () => false };
  const window = { webkit: { messageHandlers: { agl: { postMessage: (msg) => sent.push(msg) } } }, addEventListener() {},
    requestAnimationFrame: options.requestAnimationFrame, matchMedia: () => options.motion || { matches: true, addEventListener() {} } };
  runInNewContext(readFileSync(new URL("./bundle.js", import.meta.url), "utf8"),
    { document, window, console, setTimeout: options.setTimeout || setTimeout,
      setInterval: options.setInterval || setInterval, clearInterval, clearTimeout, URL, confirm: options.confirm || (() => true), ResizeObserver: options.ResizeObserver });
  const click = (dataset) => handlers.click({ target: { closest: (selector) => selector === "[data-action]" ? { dataset } : null }, preventDefault() {} });
  const input = (id, value) => handlers.input({ target: { id, value } });
  return { window, root, sent, click, input, handlers, document };
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
  assert.ok(rosterOf(h).includes(keyConfirmations({ pending_key_switches: [request] })));
  assert.doesNotMatch(h, /<img|<script>/);
  assert.match(h, /&lt;script&gt;/);
});

test("partial prices and same-as rates preserve unknowns and cannot recurse forever", () => {
  let h = keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("2", null) })] });
  assert.match(h, /input \$2 \/ output price unavailable/);
  const price = livePrice("2", null);
  price.rates.output = { status: "same_as", same_as: "input" };
  h = keyConfirmations({ pending_key_switches: [keyPrompt({ price })] });
  assert.match(h, /input \$2 \/ output \$2/);
  price.rates.input = { status: "same_as", same_as: "output" };
  h = keyConfirmations({ pending_key_switches: [keyPrompt({ price })] });
  assert.match(h, /doesn't publish a price here/); assert.doesNotMatch(h, /\$\d/);
  h = keyConfirmations({ pending_key_switches: [keyPrompt({ price: livePrice("2", "4", { currency: "EUR" }) })] });
  assert.doesNotMatch(h, /\$\d/);
});

test("a running paid seat offers a pinned one-tap stop that sends the kill switch action", () => {
  const app = keyApp();
  const snapshot = state({ rev: 1, settings: { key_fallback: true },
    keys: [keySeat()], running_key_seats: ["key-1"] });
  app.window.AGL.result({ state: snapshot });
  const h = app.root.innerHTML;
  assert.match(h, /<h1 role="status">your keys are spending\.<\/h1>/);
  assert.match(paidStrip(h), /late-night[\s\S]*vendor\/model/);
  assert.match(h, /data-action="key-stop">stop all paid use<\/button>/);
  assert.ok(h.indexOf('data-action="key-stop"') < h.indexOf('class="field"'));
  assert.match(h, /stops sessions and new paid requests/);
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

const paidStrip = (html) => html.match(/<section class="paid-use-control"[^>]*>[\s\S]*?<\/section>/)?.[0];
const paidPin = (over = {}) => ({ pin: "first-pin", pid: 202, tool: "codex", email: "key-1",
  key_seat: keySeat(), ...over });

test("paid-use strip names and escapes the running pin's seat and model even after key removal", () => {
  const html = buildHTML(state({ keys: [], running_key_seats: ["key-1"],
    pinned_sessions: [paidPin({ key_seat: keySeat({ label: "late <shift>", model: "guest/<model>" }) })] }));
  const strip = paidStrip(html);
  assert.ok(strip);
  assert.match(strip, /late &lt;shift&gt;[\s\S]*codex terminal 202[\s\S]*guest\/&lt;model&gt;/);
  assert.doesNotMatch(strip, /<shift>|<model>/);
  assert.match(strip, /aria-label="paid sessions"/);
  assert.ok(rosterOf(html).includes(strip));
});

test("paid-use strip names both running sessions and makes its global stop explicit", () => {
  const second = keySeat({ id: "key-2", label: "writing", model: "claude-model", harness: "claude" });
  const strip = paidStrip(buildHTML(state({ running_key_seats: ["key-1", "key-2"],
    pinned_sessions: [paidPin(), paidPin({ pin: "second-pin", pid: 303, tool: "claude",
      email: "key-2", key_seat: second })] })));
  assert.match(strip, /late-night[\s\S]*codex terminal 202[\s\S]*vendor\/model/);
  assert.match(strip, /writing[\s\S]*claude terminal 303[\s\S]*claude-model/);
  assert.match(strip, /stops sessions and new paid requests/);
  assert.match(strip, /sent turns may still bill/);
  assert.equal((strip.match(/data-action="key-stop"/g) || []).length, 1);
});

test("paid-use strip keeps two terminals on the same key individually identifiable", () => {
  const strip = paidStrip(buildHTML(state({ running_key_seats: ["key-1"],
    pinned_sessions: [paidPin(), paidPin({ pin: "second-pin", pid: 303 })] })));
  assert.equal((strip.match(/<h2>late-night<\/h2>/g) || []).length, 2);
  assert.match(strip, /terminal 202/);
  assert.match(strip, /terminal 303/);
});

test("paid-use strip also names automatic fallback seats alongside pinned sessions", () => {
  const strip = paidStrip(buildHTML(state({ running_key_seats: ["key-1", "key-2"],
    keys: [keySeat({ id: "key-2", label: "fallback", model: "other/model" })],
    pinned_sessions: [paidPin()] })));
  assert.match(strip, /late-night[\s\S]*vendor\/model/);
  assert.match(strip, /fallback[\s\S]*other\/model/);
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
  assert.match(strip, /late-night[\s\S]*vendor\/model/);
  assert.match(strip, /data-action="key-stop" disabled>stopping paid use/);
  assert.match(strip, /sent turns may still bill/);
});

test("paid-use strip has a heading and real button without the confirmation's card layout", () => {
  const html = buildHTML(state({ running_key_seats: ["key-1"], pinned_sessions: [paidPin()],
    pending_key_switches: [keyPrompt()] }));
  const strip = paidStrip(html);
  assert.ok(strip);
  assert.doesNotMatch(strip, /key-confirm|set-card|k-fine|k-acts/);
  assert.match(strip, /<h1 role="status">your keys are spending\.<\/h1>/);
  assert.match(strip, /<button class="primary stop-all" data-action="key-stop">stop all paid use<\/button>/);
  assert.match(strip, /<h2>late-night<\/h2>/);
  assert.match(strip, /codex terminal 202/);
  assert.match(html, /<section class="key-confirm" aria-label="paid key confirmation"/);
});

test("pinned cards retain per-session end actions alongside the global paid-use strip", () => {
  const html = buildHTML(state({ running_key_seats: ["key-1"],
    pinned_sessions: [paidPin(), paidPin({ pin: "second-pin", pid: 303, end_requested: true })] }));
  assert.match(paidStrip(html), /end-pinned-session/);
  const cards = [...html.matchAll(/<article class="paid-session"[\s\S]*?<\/article>/g)];
  assert.equal(cards.length, 2);
  assert.match(cards[0][0], /data-action="end-pinned-session" data-tool="codex" data-pin="first-pin">end<\/button>/);
  assert.match(cards[1][0], /data-action="end-pinned-session" data-tool="codex" data-pin="second-pin" disabled>ending…<\/button>/);
});

test("paid-use settings subtitle explains prevention, session stopping, and in-flight billing", () => {
  const h = buildSettings(state());
  const row = h.match(/<label class="set-toggle-row">(?:(?!<\/label>)[\s\S])*data-key="key_fallback"(?:(?!<\/label>)[\s\S])*<\/label>/)[0];
  assert.match(row, /class="set-s">off stops sessions and new requests/);
  assert.match(h, /sent turns may still bill after stopping/);
});

test("each key seat offers use in new terminal with its exact id", () => {
  const keys = [
    { id: 'paid-1', label: 'late shift', harness: 'codex', provider: 'openrouter', model: 'guest/model' },
    { id: 'paid-2', label: 'writing', harness: 'claude', provider: 'anthropic', model: 'claude-model' },
  ];
  const html = buildHTML(state({ keys }));
  for (const key of keys) {
    assert.match(html, new RegExp(`data-action="key-terminal" data-id="${key.id}"[^>]*>new terminal</button>`));
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
  assert.equal((html.match(/<article class="paid-session"/g) || []).length, 2);
  assert.equal((html.match(/data-action="end-pinned-session"/g) || []).length, 2);
  assert.match(html, /late &lt;shift&gt;/);
  assert.match(html, /codex terminal 202/);
  assert.match(html, /guest\/model/);
  assert.match(html, /paid sessions/);
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
  assert.match(html, /use a paid key in this terminal\?/);
  assert.match(html, /use a paid key in this terminal/);
  assert.doesNotMatch(html, /the current seat is resting/);
  assert.match(html, /data-action="key-answer" data-id="consent-pin" data-approved="false"/);
  assert.match(html, /doesn't publish a price here/);
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
    assert.match(h, /class="without-free">2<\/span>/);
  }
  assert.match(buildModelPicker(keyFlow({ catalog })), /class="without-free">3<\/span>/);
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
    const header = (h) => h.match(/<p class="support">([^<]*(?:sorted by|machine-readable)[^<]*)<\/p>/)[1];
    assert.equal(header(filtered), header(original));
    assert.match(filtered, /class="without-free">3<\/span>/);
    if (sort_key === "id") assert.doesNotMatch(filtered, /input \$|output \$|class="key-price/);
    else {
      assert.match(filtered, /class="model-rates"><span>\$1<\/span><span>\$8<\/span>/);
      assert.match(filtered, /class="model-rates"><span>\$9<\/span><span>\$2<\/span>/);
    }
  }
});

test("model filter empty state honestly names and escapes the query", () => {
  const h = buildModelPicker(keyFlow({ modelFilter: '<MiSs "me">',
    catalog: { sort_key: "id", models: [{ id: "model" }] } }));
  assert.match(h, /no models match “&lt;MiSs &quot;me&quot;&gt;”/);
  assert.match(h, /class="without-free">0<\/span>/);
  assert.doesNotMatch(h, /data-action="key-model"|<MiSs|no models returned/);
});

test("paid-use master switch names automatic fallback, pinned terminals and stopping", () => {
  const h = buildSettings(state());
  const row = h.match(/<label class="set-toggle-row">(?:(?!<\/label>)[\s\S])*data-key="key_fallback"(?:(?!<\/label>)[\s\S])*<\/label>/)[0];
  assert.match(row, /class="set-t">allow paid use<\/span>/);
  assert.match(h, /paid use allows fallback and pinned terminals/);
  assert.match(h, /spend real money/);
  assert.match(row, /off stops sessions and new requests/);
  assert.doesNotMatch(row, /let a key take the floor/);
});

test("blocked named-seat launch offers informed inline enable-and-continue", () => {
  const app = keyApp();
  const snapshot = state({ rev: 1, settings: { key_fallback: false }, keys: [keySeat({ label: 'late <shift>' })] });
  app.window.AGL.result({ state: snapshot });
  app.click({ action: "key-terminal", id: "key-1" });
  app.window.AGL.result({ key_action: "key_terminal", key_target_id: "key-1", ok: false,
    code: "paid_use_disabled", error: "paid use is off", state: snapshot });
  assert.match(app.root.innerHTML, /app set-app/);
  assert.match(app.root.innerHTML, /allow paid use for late &lt;shift&gt;\?/);
  assert.match(app.root.innerHTML, /spend real money|real money can be spent/);
  assert.match(app.root.innerHTML, /data-action="paid-key-enable">allow and open/);
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
  assert.match(results.innerHTML, /class="without-free">1<\/span>/);
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

// Small structural reader: handles nested div/section/dl elements without a DOM dependency.
function elements(html, tag, predicate = () => true) {
  const tokens = new RegExp(`<\\/?${tag}\\b[^>]*>`, "g"), stack = [], found = [];
  for (const m of html.matchAll(tokens)) {
    if (!m[0].startsWith("</")) stack.push({ start: m.index, tag: m[0] });
    else {
      const item = stack.pop();
      if (item && predicate(item.tag)) found.push({ start: item.start, html: html.slice(item.start, m.index + m[0].length) });
    }
  }
  return found.sort((a, b) => a.start - b.start).map((x) => x.html);
}
const visibleMarkup = (html) => {
  for (const detail of elements(html, "div", (tag) => tag.includes('class="details"') && tag.includes(" hidden"))) html = html.replace(detail, "");
  return html;
};
const rosterHTML = (snapshot) => rosterOf(buildHTML(snapshot));
const toolOf = (html, tool) => elements(html, "section", (tag) => tag.includes('class="tool"') && tag.includes(`data-tool="${tool}"`))[0] || "";
const issuesOf = (html, email) => elements(html, "div", (tag) => tag.includes('class="issue ') && tag.includes(`data-email="${email}"`)).join("");
const detailsOf = (html, tool, email) => elements(html, "div", (tag) => tag.includes(`id="seat-detail-${encodeURIComponent(tool)}-${encodeURIComponent(email)}"`))[0] || "";
const answerOf = (html) => elements(html, "div", (tag) => tag.includes('class="answer"'))[0] || "";
const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("ledger fixture covers both themes, tool groups, stale usage and an absent window", () => {
const snapshot = state({ tools: {
 codex: { seats: [seat({ status: "active", usage_stale: true })] },
 claude: { seats: [seat({ name: "Studio", email: "studio@x.com", plan: "Max", status: "active", usage: { reported_windows: ["weekly"] } })] },
} });
for (const theme of ["light", "dark"]) {
 snapshot.settings.theme = theme;
 const html = buildHTML(snapshot), main = rosterOf(html);
 assert.match(html, new RegExp(`theme-${theme}`));
 assert.equal(elements(main, "section", (tag) => tag.includes('class="tool"')).length, 2);
 assert.match(toolOf(main, "codex"), /~80<small>%/);
 assert.match(toolOf(main, "claude"), /of week left/);
 assert.doesNotMatch(toolOf(main, "claude"), /of 5h left/);
 assert.match(main, /class="then"/);
}
});

for (const screen of ["spending", "nothing-ready"]) {
  test(`${screen} has a compact roster with every seat, left semantics and ticking resets`, () => {
    const now = Date.parse("2099-01-01T00:00:00Z");
    const five = "2099-01-01T02:30:00Z", week = "2099-01-04T00:00:00Z";
    const snapshot = state({ tools: {
      codex: { seats: [seat({ name: "Resting Codex", status: "resting", usage5h: 100, usageWeek: 21,
        limited: true, limited_until: five,
        usage: { windows: { "5h": { resets_at: five }, weekly: { resets_at: week } } } }),
      seat({ name: "Weekly Codex", email: "weekly@x.com", status: "queued", usageWeek: 38,
        usage: { reported_windows: ["weekly"], windows: { weekly: { resets_at: week } } } })] },
      claude: { seats: [seat({ name: "Resting Claude", email: "claude@x.com", status: "resting",
        usage5h: 38, usageWeek: 100, usage_stale: true,
        usage: { windows: { "5h": { resets_at: five }, weekly: { resets_at: week } } } })] },
    }, keys: [keySeat(), keySeat({ id: "key-2", label: "other key", harness: "claude" })],
    ...(screen === "spending" ? { running_key_seats: ["key-1"] } : {}) });
    const html = buildHTML(snapshot);
    const compact = rosterOf(html);
    assert.match(html, /aria-label="seat availability"/);
    assert.equal(elements(compact, "section", (tag) => tag.includes('class="tool"')).length, 2);
    assert.equal((compact.match(/data-action="seat-details"/g) || []).length, 5);
    for (const name of ["Resting Codex", "Weekly Codex", "Resting Claude", "late-night", "other key"]) assert.ok(compact.includes(name));
    assert.match(compact, /week 79%/);
    assert.match(compact, /62% left/);
    assert.match(compact, /5h ~62%/);
    assert.match(compact, /last known reading/);
    assert.equal((compact.match(/class="state">[^<]*paid per token/g) || []).length, 2);
    assert.match(compact, /no app spend cap/);
    const nodes = [five, week].map((resetAt) => ({ dataset: { resetAt, clockPrefix: "back in" } }));
    updateClockText({ querySelectorAll: (q) => q === "[data-reset-at]" ? nodes : [] }, now);
    assert.deepEqual(nodes.map((n) => n.textContent), ["back in 2h 30m", "back in 3d"]);
    updateClockText({ querySelectorAll: (q) => q === "[data-reset-at]" ? nodes : [] }, now + 60000);
    assert.deepEqual(nodes.map((n) => n.textContent), ["back in 2h 29m", "back in 2d 23h"]);
  });
}

for (const [screen, price] of [["asking", livePrice("2", "4")], ["asking-unpriced", null]]) {
  test(`${screen} keeps paid consent first, including during other paid use`, () => {
    for (const running_key_seats of [[], ["key-1"]]) {
      const html = buildHTML(state({ tools: { codex: { seats: [seat()] } }, keys: [keySeat()],
        pending_key_switches: [keyPrompt({ price })], running_key_seats }));
      assert.match(html, /class="key-confirm"/);
      assert.ok(html.indexOf('class="key-confirm"') < html.indexOf('class="tool"'));
      assert.match(html, /class="sheet"/);
    }
  });
}

test("running and pinned key seats say spending now while idle keys retain their terms", () => {
for (const paid of [{ running_key_seats: ["key-1"] }, { pinned_sessions: [{ key_seat: keySeat() }] }, { pinned_sessions: [{ email: "key-1" }] }]) {
 const h = rosterHTML(state({ tools: { codex: { seats: [seat()] } }, keys: [keySeat(), keySeat({ id: "idle", label: "idle key" })], ...paid }));
 const keys = elements(h, "div", (tag) => tag.includes('key-seat"'));
 assert.equal(keys.length, 2); assert.match(keys[0], /spending now/); assert.doesNotMatch(keys[1], /spending now/);
 for (const key of keys) assert.match(key, /paid per token.*no app spend cap/);
}
});

for (const tool of ["codex", "claude"]) {
  for (const status of ["resting", "queued", "ready", "active", "needs-login"]) {
    test(`${tool} ${status} has the same honest seat action in the roster and sheet`, () => {
      const email = `seat+${status}@${tool}.example`;
      const html = buildHTML(state({ tools: { [tool]: { seats: [seat({ status, email, name: "My seat" })] } } }));
      const roster = rosterOf(html);
      for (const surface of [roster]) {
        if (status === "active") {
          assert.doesNotMatch(surface, /data-action="switch"/);
        } else if (status === "needs-login") {
          assert.match(surface, new RegExp(`data-action="add" data-tool="${tool}" data-email="[^" ]+">sign in`));
          assert.doesNotMatch(surface, /data-action="switch"/);
        } else {
          assert.ok(surface.includes(`class="row-action" data-action="switch" data-tool="${tool}" data-email="${email}"`));
          assert.match(surface, /aria-label="switch to My seat/);
          if (["resting", "queued"].includes(status)) {
            assert.match(surface, /this seat's limit applies immediately/);
            assert.match(surface, />switch anyway<\/button>/);
          }
        }
      }
    });
  }
}

test("unauthorized usage takes precedence over switch in the main view", () => {
  const html = buildHTML(state({ tools: { codex: { seats: [seat({ usage: { error: "unauthorized" } })] } } }));
  assert.equal((html.match(/>sign in<\/button>/g) || []).length, 1);
  assert.doesNotMatch(html, /data-action="switch"/);
});

test("resting roster switches dispatch the emitted tool and email unchanged", () => {
  for (const tool of ["codex", "claude"]) {
    const email = "franz+codex@example.test";
    const roster = rosterHTML(state({ tools: { [tool]: { seats: [seat({ status: "resting", email })] } } }));
    const [, emittedTool, emittedEmail] = roster.match(/data-action="switch" data-tool="([^"]+)" data-email="([^"]+)"/);
    const app = keyApp();
    app.click({ action: "switch", tool: emittedTool, email: emittedEmail });
    assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "switch", tool, email });
  }
});

test("roster groups tools once in one table, active seats lead each group, and has no subscription models", () => {
const tools = {
 codex: { seats: [seat({ name: "Rest", email: "rest", status: "resting" }), seat({ name: "Active C", email: "c", status: "active", model: "invented-c" })] },
 claude: { seats: [seat({ name: "Ready", email: "ready" }), seat({ name: "Active A", email: "a", status: "active", model: "invented-a" })] },
};
const h = rosterHTML(state({ tools, keys: [keySeat({ model: "literal/model-id" })] }));
assert.equal(elements(h, "section", (tag) => tag.includes('class="tool"')).length, 2);
assert.match(toolOf(h, "codex"), /Active C.*[\s\S]*on now/);
assert.match(toolOf(h, "claude"), /Active A.*[\s\S]*on now/);
for (const tool of ["codex", "claude"]) {
  const block = toolOf(h, tool);
  assert.ok(h.indexOf('class="sheet"') > h.indexOf(block) + block.length);
}
const row = rowOf(h, "key-1");
assert.doesNotMatch(row, /literal\/model-id|\d+%/);
assert.match(row, /class="seat-identity"[^>]*>late-night<\/span>/);
assert.match(row, /data-action="seat-details" data-tool="key" data-email="key-1"[^>]*aria-expanded="false"/);
assert.match(row, /paid per token.*no app spend cap/);
assert.match(row, /data-action="key-terminal" data-id="key-1"/);
assert.match(row, /aria-label="use late-night in a new paid terminal"/);
assert.match(row, />new terminal<\/button>/);
assert.match(detailsOf(h, "key", "key-1"), /<dt>model<\/dt><dd>literal\/model-id<\/dd>/);
const open = buildHTML(state({ tools, keys: [keySeat()] }), null, new Set([JSON.stringify(["key", "key-1"])]));
assert.match(detailsOf(open, "key", "key-1"), /id="seat-detail-key-key-1">/);
assert.doesNotMatch(h, /invented-|class="track/);
assert.equal(tools.codex.seats[0].name, "Rest", "render must not reorder bridge state");
});

test("roster reports left, unknown, last known and unreported windows without inventing readings", () => {
const render = (over) => rosterHTML(state({ tools: { codex: { seats: [seat({ status: "active", ...over })] } } }));
assert.match(render({ usage5h: 38, usageWeek: 21 }), /62<small>%[\s\S]*week 79%/);
assert.match(render({ usage5h: 100, usageWeek: 0 }), /0<small>%[\s\S]*week 100%/);
for (const over of [{ usage_unknown: true }, { usage5h: null, usageWeek: null }]) {
 assert.match(render(over), /no reading yet/); assert.doesNotMatch(render(over), /unknown|\d+%|class="bar"/);
}
const stale = render({ usage_stale: true });
assert.equal((stale.match(/aria-label="last known:/g) || []).length, 2);
assert.match(stale, /last known reading/);
const weekly = render({ usage: { reported_windows: ["weekly"] } });
assert.match(weekly, /90<small>%/); assert.doesNotMatch(weekly, /80%|of 5h/);
});

test("roster escapes names and titles, key labels, literal models and unknown harness labels", () => {
  const hostile = '<img src=x onerror="bad()">&';
  const h = rosterHTML(state({ tools: { codex: { seats: [seat({ name: hostile, plan: hostile })] } },
    keys: [keySeat({ label: hostile, model: hostile, harness: hostile })] }));
  assert.ok((h.match(/&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;&amp;/g) || []).length >= 12);
  assert.doesNotMatch(h, /<img|onerror="|<script/);
  const noModel = rosterHTML(state({ tools: { codex: { seats: [seat()] } }, keys: [keySeat({ model: null })] }));
  assert.doesNotMatch(noModel, /null|undefined|unknown model/);
});

test("roster resets use the live clock hook independently for each reported window", () => {
  const now = Date.now();
  const five = new Date(now + 9000000).toISOString(), week = new Date(now + 259200000).toISOString();
  const h = rosterHTML(state({ tools: { codex: { seats: [seat({ usage: {
    windows: { "5h": { resets_at: five }, weekly: { resets_at: week } },
  } })] } } }));
  for (const reset of [five, week]) {
    assert.ok(h.includes(`data-reset-at="${reset}" data-clock-prefix="in"`));
    assert.ok(h.includes(`>in ${fmtCountdown(reset, now)}</span>`));
  }
  const nodes = [five, week].map((resetAt) => ({ dataset: { resetAt, clockPrefix: "in" } }));
  updateClockText({ querySelectorAll: (selector) => selector === "[data-reset-at]" ? nodes : [] }, now + 60000);
  assert.deepEqual(nodes.map((n) => n.textContent), ["in 2h 29m", "in 2d 23h"]);
});

test("roster omits absent or invalid reset times and resets for unreported windows", () => {
  for (const usage of [{}, { windows: { "5h": { resets_at: "invalid" } } }, {
    reported_windows: ["weekly"], windows: { "5h": { resets_at: "2099-01-01T00:00:00Z" } },
  }]) {
    const h = rosterHTML(state({ tools: { codex: { seats: [seat({ usage })] } } }));
    assert.doesNotMatch(h, /roster-reset|data-reset-at/);
  }
});

test("roster long identities retain titles, names truncate and literal models stay in details", () => {
  const name = "Wilhelmina Featherstone +codex / a realistically long name";
  const h = rosterHTML(state({ tools: { codex: { seats: [seat({ name })] } },
    keys: [keySeat({ label: name, model: "literal/long-model-id" })] }));
  assert.ok(h.includes(`title="${name} · business"`));
  assert.ok(h.includes(`title="${name}"`));
  assert.match(h, /<dt>model<\/dt><dd>literal\/long-model-id<\/dd>/);
  assert.match(css, /\.seat-identity \{[^}]*overflow:hidden; text-overflow:ellipsis; white-space:nowrap/);
});

test("roster maps known plans and hides all unrecognised labels including prototype property names", () => {
  for (const plan of ["Self_Serve_Business_Prolite", "constructor", "toString", "__proto__", null]) {
    const h = rosterHTML(state({ tools: { codex: { seats: [seat({ plan })] } } }));
    assert.doesNotMatch(h, /class="mono chip"/);
    if (plan) assert.ok(!h.includes(plan));
    assert.match(h, /80% left/);
  }
  const h = rosterHTML(state({ tools: { codex: { seats: [seat({ plan: "  TEAM  " })] } } }));
  assert.match(h, /Work · business · team/);
});

test("icon and mood themes remain available for every door, seat and approval state", () => {
  for (const theme of ["light", "dark"]) {
    for (const door of ["open", "shut", undefined]) {
      for (const status of ["active", "resting", "needs-login"]) {
        const snapshot = state({ settings: { theme }, door, tools: { codex: { seats: [seat({ status })] } } });
        const html = buildHTML(snapshot);
        assert.match(html, new RegExp(`theme-${theme} mood-${status === "active" ? "ready" : "resting"}`));
        assert.match(doorMark(snapshot), new RegExp(`door--${doorKey(snapshot)}`));
        for (const extra of [{ pending_key_switches: [keyPrompt()] }, { running_key_seats: ["key-1"] }]) {
          const other = buildHTML({ ...snapshot, ...extra });
          assert.match(other, /class="app-icon"/);
          assert.doesNotMatch(other, /ambient-art|roster-door/);
        }
      }
    }
  }
});

test("consent answers share size and weight, with no asymmetric CSS overrides", () => {
  for (const theme of ["light", "dark"]) {
    for (const price of [livePrice(), null]) {
      const h = buildHTML(state({ settings: { theme }, pending_key_switches: [keyPrompt({ price })] }));
      const buttons = [...h.matchAll(/<button([^>]*data-action="key-answer"[^>]*)>/g)].map((m) => m[1]);
      assert.equal(buttons.length, 2);
      assert.equal(buttons[0].match(/class="([^"]*)"/)[1], buttons[1].match(/class="([^"]*)"/)[1]);
      assert.ok(buttons.every((b) => !/style=|autofocus|tabindex/.test(b)));
    }
  }
  assert.match(css, /\.k-acts, \.equal-choices\s*\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)/);
  const shared = css.match(/\.choice, \.k-acts button\s*\{([^}]+)\}/)[1];
  for (const rule of ["min-height:48px", "width:100%", "padding:10px", "font-size:.95rem", "font-weight:650"]) {
    assert.ok(shared.includes(rule), rule);
  }
  // A later override targeting either answer, its position or an action would defeat the shared rule.
  const selectors = [...css.matchAll(/([^{}]+)\{[^{}]*\}/g)].map((m) => m[1])
    .filter((selector) => /k-acts|choice|key-answer|paid-key|key-prove/.test(selector)).join("\n");
  assert.doesNotMatch(selectors, /data-approved|key-answer|k-no|k-go|paid-key-enable|paid-key-back|key-prove|:(?:first|last|nth|only)-(?:child|of-type)/);
});

test("free model visibility is reversible and catalog provenance is outside result rows", () => {
  const h = buildModelPicker(keyFlow({ catalog: { sort_key: "input_usd_per_million_tokens",
    models: [{ id: "free-model", price: livePrice("0", "0") }, { id: "paid-model", price: livePrice("1", "2") }],
  } }));
  assert.match(h, /id="show-free-models" type="checkbox">/);
  assert.match(h, /class="key-model-option is-free"[^>]*data-model="free-model"/);
  assert.match(h, /class="model-rates">free</);
  assert.match(h, /free models hidden/);
  assert.match(h, /free models shown/);
  assert.equal((h.match(/price estimates/g) || []).length, 1);
  assert.match(css, /\.model-picker:not\(:has\(#show-free-models:checked\)\) \.is-free \{ display:none; \}/);
});

// Minimal scroll boxes recreated on each render, as with root.innerHTML in the app.
function scrollDOM() {
  let html = "", body;
  return {
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; body = { scrollTop: 0 }; },
    querySelector: (selector) => selector === ".body, .set-body" ? body : null,
    querySelectorAll: () => [],
    get body() { return body; },
  };
}

test("polls preserve main and settings scroll while navigation starts at the top", () => {
  const root = scrollDOM(), app = keyApp({ root });
  const snapshot = state({ rev: 1, keys: [keySeat()], tools: { codex: { seats: [seat()] } } });
  app.window.AGL.update(snapshot);
  root.body.scrollTop = 180;
  app.window.AGL.update({ ...snapshot, rev: 2 });
  assert.equal(root.body.scrollTop, 180);
  app.click({ action: "settings" });
  assert.equal(root.body.scrollTop, 0);
  root.body.scrollTop = 250;
  app.window.AGL.update({ ...snapshot, rev: 3 });
  assert.equal(root.body.scrollTop, 250);
  app.click({ action: "settings-back" });
  assert.equal(root.body.scrollTop, 0);
});

test("settings inserts consent immediately after its pinned header", () => {
  const app = keyApp();
  const insertions = [];
  app.root.querySelector = (selector) => selector === ".set-head" ? {
    insertAdjacentHTML: (position, html) => insertions.push({ position, html }),
  } : null;
  app.window.AGL.update(state({ pending_key_switches: [keyPrompt()] }));
  app.click({ action: "settings" });
  const inserted = insertions.at(-1);
  assert.equal(inserted.position, "afterend");
  assert.match(inserted.html, /data-action="key-answer"/);
  assert.match(app.root.innerHTML, /<header class="set-head">[\s\S]*<div class="set-body">/);
});

test("pending endpoint proof stays visible and disabled after a rerender", () => {
  const app = keyApp();
  const gate = { checked: false };
  const button = { dataset: { id: "key-1" }, disabled: false, textContent: "send paid check",
    closest: () => ({ querySelector: () => gate }) };
  app.root.querySelectorAll = (selector) => selector === '[data-action="key-prove"]' ? [button] : [];
  app.click({ action: "key-prove", id: "key-1" });
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "checking endpoint…");
  assert.equal(gate.checked, true);
  gate.checked = false;
  app.window.AGL.update(state({ rev: 2, keys: [keySeat({ responses_verified: false })] }));
  assert.equal(gate.checked, true);
  assert.match(app.root.innerHTML, /class="proof-gate"><input class="local-check"/);
  assert.match(app.root.innerHTML, /data-action="key-prove" data-id="key-1"/);
});

test("model filtering retains the focused input object and its caret", () => {
  const app = keyApp();
  app.click({ action: "key-start" }); app.click({ action: "key-provider", provider: "openai" });
  app.input("key-label", "night"); app.input("key-secret", "test-secret");
  app.click({ action: "key-discover" });
  app.window.AGL.result({ key_action: "models_list", key_request_id: app.sent.at(-1).key_request_id,
    ok: true, sort_key: "id", models: [{ id: "mini" }, { id: "other" }] });
  const field = { id: "key-model-filter", value: "mini", selectionStart: 2, selectionEnd: 2 };
  app.document.activeElement = field;
  const results = { innerHTML: "" };
  app.root.querySelector = (selector) => selector === "#key-model-results" ? results : null;
  let swaps = 0, html = app.root.innerHTML;
  Object.defineProperty(app.root, "innerHTML", { get: () => html, set: (value) => { swaps++; html = value; } });
  app.handlers.input({ target: field });
  app.window.AGL.update(state({ rev: 1 }));
  assert.equal(swaps, 0);
  assert.equal(app.document.activeElement, field);
  assert.equal(field.selectionStart, 2); assert.equal(field.selectionEnd, 2);
  assert.match(results.innerHTML, /data-model="mini"/);
  assert.doesNotMatch(results.innerHTML, /data-model="other"/);
});

test("clock hooks in actual seat markup update usage, resets, session age and consent expiry", () => {
  const html = buildHTML(state({ pending_key_switches: [keyPrompt()], tools: { codex: { seats: [seat({
    status: "resting", limited_until: "2099-01-01T01:00:00Z", usage_fetched_at: "2098-12-31T23:59:30Z",
    in_session: true, session_started_at: "2098-12-31T22:00:00Z",
    usage: { windows: { weekly: { resets_at: "2099-01-02T00:00:00Z" } } },
  })] } } }));
  const nodes = [...html.matchAll(/<span([^>]*data-(?:usage|reset|session|expire)-at="[^"]*"[^>]*)>/g)].map((m) => ({
    dataset: Object.fromEntries([...m[1].matchAll(/data-([\w-]+)="([^"]*)"/g)].map((a) => [a[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()), a[2]])),
  }));
  const root = { querySelectorAll: (selector) => nodes.filter((n) => selector.slice(1, -1).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) in n.dataset) };
  updateClockText(root, Date.parse("2099-01-01T00:00:00Z"));
  assert.ok(nodes.some((n) => n.textContent === "updated 30s ago"));
  assert.ok(nodes.some((n) => n.textContent === "1h"));
  assert.ok(nodes.some((n) => n.textContent === "in 1d"));
  assert.ok(nodes.some((n) => n.textContent === " · 2h"));
  assert.ok(nodes.some((n) => n.textContent === "0s"));
});

test("Escape declines live consent once without leaving the current view", () => {
  const app = keyApp();
  app.window.AGL.update(state({ pending_key_switches: [keyPrompt()] }));
  app.click({ action: "settings" });
  let prevented = 0;
  app.handlers.keydown({ key: "Escape", preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "answer_key_switch", id: "prompt-1", approved: false });
  assert.match(app.root.innerHTML, /class="set-title">settings/);
  app.handlers.keydown({ key: "Escape", preventDefault() {} });
  assert.equal(app.sent.filter((m) => m.action === "answer_key_switch").length, 1);
});

test("toast remains outside root and uses textContent and theme tokens; there is no other motion", () => {
  const callbacks = [], toast = { textContent: "" };
  const overlay = { innerHTML: "", firstElementChild: toast, querySelector: () => toast };
  const app = keyApp({ createElement: () => overlay, setTimeout: (fn, ms) => callbacks.push({ fn, ms }) });
  const classes = new Set();
  app.root.firstElementChild = { classList: { add: (s) => classes.add(s), remove: (s) => classes.delete(s) } };
  app.window.AGL.result({ ok: false, error: '<img src=x onerror="bad">' });
  assert.match(overlay.innerHTML, /class="toast" role="status"/);
  assert.doesNotMatch(overlay.innerHTML, /<img/);
  assert.equal(toast.textContent, '<img src=x onerror="bad">');
  app.window.AGL.update(state({ settings: { theme: "dark" } }));
  assert.equal(app.document.body.className, "theme-dark");
  assert.match(css, /\.toast \{[^}]*background:var\(--surface\)[^}]*color:var\(--ink\)/);
  app.window.AGL.celebrate();
  assert.equal(classes.has("celebrate"), true);
  callbacks.find((c) => c.ms === 600).fn();
  assert.equal(classes.has("celebrate"), false);
  assert.match(css, /@keyframes pulse/);
  assert.match(css, /transition:transform \.15s/);
  assert.match(css, /prefers-reduced-motion:reduce[\s\S]*animation:none !important/);
  callbacks.find((c) => c.ms === 3000).fn();
  assert.equal(overlay.innerHTML, "");
});

test("settings retains the shipping version/build footer and escapes bundle metadata", () => {
  assert.match(buildSettings(state({ app: { version: "1.2.3", build: "123" } })), /ai guest list v1\.2\.3 · build 123/);
  assert.match(buildSettings(state({ app: { version: "1.2.3", build: "dev" } })), /ai guest list v1\.2\.3<\/p>/);
  const hostile = buildSettings(state({ app: { version: '<img src="x">', build: '<script>' } }));
  assert.match(hostile, /&lt;img src=&quot;x&quot;&gt;/);
  assert.match(hostile, /&lt;script&gt;/);
  assert.doesNotMatch(hostile, /<img|<script>/);
});

test("visible clock ticks update consent over settings without replacing focused controls", () => {
  let tick;
  const app = keyApp({ setInterval: (fn) => { tick = fn; return 1; } });
  app.window.AGL.update(state({ pending_key_switches: [keyPrompt()] }));
  app.click({ action: "settings" });
  const expiry = { dataset: { expireAt: new Date(Date.now() + 60000).toISOString() }, textContent: "" };
  app.root.querySelectorAll = (selector) => selector === "[data-expire-at]" ? [expiry] : [];
  const before = app.root.innerHTML;
  app.window.AGL.setVisible(true);
  tick();
  assert.match(expiry.textContent, /^\d+s$/);
  assert.ok(parseInt(expiry.textContent, 10) <= 60);
  assert.equal(app.root.innerHTML, before);
  app.window.AGL.setVisible(false);
});

test("account-specific sign-in carries the seat to the engine; plus stays unrestricted", () => {
  for (const tool of ["codex", "claude"]) {
    const app = keyApp();
    app.click({ action: "add", tool, email: "alpha@example.test" });
    app.click({ action: "add-cta" });
    const login = app.sent.find((msg) => msg.action === "login");
    assert.equal(login.tool, tool);
    assert.equal(login.email, "alpha@example.test");
    const plus = keyApp();
    plus.click({ action: "add" });
    plus.click({ action: "add-provider", tool });
    plus.click({ action: "add-cta" });
    const plain = plus.sent.find((msg) => msg.action === "login");
    assert.equal(plain.tool, tool);
    assert.equal(plain.email, undefined);
  }
});

test("sign-in buttons carry escaped seat identity on the inline problem", () => {
  const email = 'alpha"<&@example.test';
  for (const tool of ["codex", "claude"]) {
    const html = buildHTML(state({ tools: { [tool]: { seats: [seat({ email, status: "needs-login" })] } } }));
    const target = `data-action="add" data-tool="${tool}" data-email="alpha&quot;&lt;&amp;@example.test"`;
    assert.ok(html.includes(`${target}>sign in`));
    assert.equal(html.split(`${target}>sign in`).length - 1, 1);
    assert.match(html, /class="issue issue--hard"/);
  }
});

test("engine drift warnings render as alerts, with the consented fix only for the wrapper", () => {
  const st = state({ engine_warnings: [
    { kind: "wrapper", action: "adopt_engine", text: "terminals run an older engine: <bin>/acctsw is acctsw 1.1.2" },
    { kind: "sessions", text: "2 terminal sessions run an older engine (1.1.2)" },
  ] });
  const banner = engineBanner(st);
  assert.equal((banner.match(/role="alert"/g) || []).length, 2);
  assert.match(banner, /&lt;bin&gt;\/acctsw is acctsw 1\.1\.2/);
  assert.equal((banner.match(/data-action="engine-adopt"/g) || []).length, 1);
  assert.ok(buildHTML(st).includes(banner));
  // Shown even with supervision opted out: cx/cl still run the same wrapper.
  assert.ok(buildHTML(state({ settings: { theme: "light", supervise_shell: false },
                              engine_warnings: st.engine_warnings })).includes(banner));
  assert.equal(engineBanner(state({})), "");
});

// --- signed-out, expiring sign-in and past resets ---------------------------------------------
const HOUR = 3600e3;
const inHours = (h) => new Date(Date.now() + h * HOUR).toISOString();
const win = (resets_at) => ({ used_pct: 20, resets_at });
const claudeState = (...seats) => state({ tools: { claude: { seats }, codex: { seats: [] } } });
const rosterOf = (html) => elements(html, "div", (tag) => tag.includes('class="content"'))[0] || html;
const rowOf = (html, email) => {
  const main = rosterOf(html);
  const row = elements(main, "div", (tag) => tag.includes('data-seat-email="' + email + '"'))[0];
  const answer = elements(main, "div", (tag) => tag.includes('class="answer"')).find((part) => part.includes(`data-email="${email}"`));
  return (row ? visibleMarkup(row) : answer ? visibleMarkup(answer) : "") + issuesOf(main, email);
};

test("signed-out active seat is never shown as active with numbers", () => {
  const s = seat({ status: "needs-login", active: true, signed_out: true, needs_login: true, usage: { error: "no_token", windows: { "5h": win(inHours(1)), weekly: win(inHours(30)) } } });
  const html = buildHTML(claudeState(s));
  const row = rosterOf(html);
  assert.match(row, /on now · signed out/);
  assert.doesNotMatch(row, />active</);
  assert.doesNotMatch(row, /\d+%/);
  assert.doesNotMatch(row, /data-reset-at/);
  assert.doesNotMatch(row, /class="tag">on now/);
  assert.match(row, /issue--hard/);
  assert.match(row, /data-action="add" data-tool="claude" data-email="work@x.com">sign in</);
  assert.doesNotMatch(row, /class="fig"|class="bar"/);
});

test("signed-out parked seat reads signed out with a sign in action and no numbers", () => {
  const s = seat({ status: "ready", signed_out: true, usage: { windows: { "5h": win(inHours(1)) } } });
  const row = rosterOf(buildHTML(claudeState(s, seat({ email: "b@x.com", name: "B" }))));
  const mine = rowOf(row, "work@x.com");
  assert.match(mine, /signed out/);
  assert.doesNotMatch(mine, />ready</);
  assert.doesNotMatch(mine, /\d+%/);
  assert.match(mine, />sign in<\/button>/);
  assert.doesNotMatch(mine, /data-action="switch"/);
});

test("signed-out main view hides bars and offers sign in", () => {
  const html = buildHTML(claudeState(seat({ status: "needs-login", active: true, signed_out: true, usage: { error: "no_token" } })));
  const card = rosterOf(html);
  assert.match(card, /on now · signed out/);
  assert.doesNotMatch(card, /% left|usage unknown|class="window"/);
  assert.match(card, />sign in<\/button>/);
});

test("signed-out is recognised from needs_login or status alone (old engine fields)", () => {
  assert.equal(signedOut(seat({ needs_login: true })), true);
  assert.equal(signedOut(seat({ status: "needs-login" })), true);
  assert.equal(signedOut(seat({ signed_out: true })), true);
  assert.equal(usable(seat({ signed_out: true, status: "active" })), false);
  assert.equal(usable(seat({ status: "active" })), true);
});

test("expiring sign-in shows a relative line and a sign in again action", () => {
  const s = seat({ signin_expiring: true, signin_expires_at: inHours(50) });
  const html = buildHTML(claudeState(s, seat({ email: "b@x.com", name: "B" })));
  const mine = rowOf(rosterOf(html), "work@x.com");
  assert.match(mine, /sign-in ends in 2d(?: \d+h)?/);
  assert.match(mine, />sign in again<\/button>/);
  assert.match(mine, /data-reset-at=/);
  assert.match(mine, /\d+%/, "still usable, numbers stay");
  assert.equal(signinExpiring(s), true);
  assert.doesNotMatch(rowOf(rosterOf(html), "b@x.com"), /sign-in ends/);
});

test("sign-in not flagged expiring when signin_expiring is false, even with a date", () => {
  const s = seat({ signin_expiring: false, signin_expires_at: inHours(20 * 24) });
  const html = buildHTML(claudeState(s));
  assert.doesNotMatch(html, /sign-in ends/);
  assert.doesNotMatch(html, /sign in again/);
});

test("expiring flag with a date already past does not render a countdown", () => {
  const html = buildHTML(claudeState(seat({ signin_expiring: true, signin_expires_at: inHours(-1) })));
  assert.doesNotMatch(html, /sign-in ends/);
});

test("a window reset in the past never renders 'in now'", () => {
  const s = seat({ usage: { windows: { "5h": win(inHours(-2)), weekly: win(inHours(40)) } } });
  const html = buildHTML(claudeState(s));
  assert.doesNotMatch(html, /in now|resets in now/);
  const row = rosterOf(html);
  assert.match(row, /class="details"[\s\S]*data-past-text="resetting…"[^>]*>resetting…</);
  assert.match(row, />in 1d/);
  assert.equal(resetIsPast(inHours(-1)), true);
  assert.equal(resetIsPast(inHours(1)), false);
  assert.equal(resetIsPast("garbage"), false);
  assert.match(detailsOf(html, "claude", "work@x.com"), /data-past-text="resetting…"[^>]*>resetting…</);
});

test("clock tick turns a reset that has just passed into resetting…", () => {
  const node = { dataset: { resetAt: inHours(-1), clockPrefix: "in", pastText: "resetting…" }, textContent: "" };
  const live = { dataset: { resetAt: inHours(2), clockPrefix: "in", pastText: "resetting…" }, textContent: "" };
  const root = { querySelectorAll: (q) => (q === "[data-reset-at]" ? [node, live] : []) };
  updateClockText(root);
  assert.equal(node.textContent, "resetting…");
  assert.match(live.textContent, /^in 2h/);
});

test("fact line names a signed-out seat while another seat remains usable", () => {
const html = buildHTML(claudeState(seat({ status: "ready" }), seat({ email: "b@x.com", name: "B", status: "needs-login", signed_out: true })));
const chip = factsOf(html);
assert.match(chip, /1 needs you/); assert.match(chip, /data-action="attention"/);
assert.match(issuesOf(html, "b@x.com"), /B · business[\s\S]*signed out/);
assert.doesNotMatch(html, /you can keep working|<h1/);
assert.ok(html.indexOf(chip) < html.indexOf('class="field"'));
});

test("fact line with only a signed-out active seat says no subscription is ready", () => {
const html = buildHTML(claudeState(seat({ status: "needs-login", active: true, signed_out: true, usage: { error: "no_token" } })));
assert.match(factsOf(html), /1 needs you/);
assert.match(toolOf(html, "claude"), /on now · signed out/);
assert.match(toolOf(html, "claude"), /then<\/b>[\s\S]*nothing ready/);
assert.match(html, /data-action="add" data-tool="claude" data-email="work@x.com">sign in</);
assert.doesNotMatch(factsOf(html), /all good|all limited/);
});

test("healthy ledger has no headline or attention fact line", () => {
  const html = buildHTML(claudeState(seat({ status: "active" })));
  assert.doesNotMatch(html, /<h1|class="ledger-facts"|you can keep working/);
});

test("old engine without the new fields renders as before", () => {
  const html = buildHTML(claudeState(seat({ status: "active" })));
  assert.match(html, />on now</);
  assert.match(html, /80%/);
  assert.doesNotMatch(html, /signed out|sign-in ends|resetting/);
});

test("revoked subscription is not signed out: keeps check your subscription, no sign in", () => {
  const s = seat({ status: "ready", signed_out: false, needs_login: true, usage: { error: "forbidden" } });
  assert.equal(signedOut(s), false);
  const html = buildHTML(claudeState(s));
  const card = rosterOf(html);
  assert.match(card, /check your subscription to refresh usage/);
  assert.doesNotMatch(card, />sign in<\/button>/);
  assert.doesNotMatch(card, /signed out/);
});

test("active signed-out seat is keyed on seat.active, not status", () => {
  const s = seat({ status: "needs-login", active: true, signed_out: true });
  assert.equal(isActive(s), true);
  assert.equal(isActive(seat({ status: "needs-login", active: false })), false);
  const other = seat({ email: "b@x.com", name: "B", status: "ready" });
  const html = buildHTML(claudeState(other, s));
  const table = rosterOf(html);
  assert.ok(table.indexOf("work@x.com") < table.indexOf("b@x.com") || table.indexOf("Work") < table.indexOf(">B<"), "active seat leads");
  assert.match(table, /on now · signed out/);
  assert.doesNotMatch(table, /roster-active/);
});

test("expiring seat keeps its switch action beside sign in again", () => {
const s = seat({ status: "ready", signin_expiring: true, signin_expires_at: inHours(50) });
const html = buildHTML(claudeState(s, seat({ email: "b@x.com", name: "B" })));
const mine = rowOf(html, "work@x.com");
assert.match(mine, /data-action="switch"[^>]*>switch<\/button>/);
assert.match(mine, />sign in again<\/button>/);
assert.match(issuesOf(toolOf(html, "claude"), "work@x.com"), />sign in again</);
const active = seat({ status: "active", active: true, signin_expiring: true, signin_expires_at: inHours(50) });
const a = rowOf(buildHTML(claudeState(active)), "work@x.com");
assert.doesNotMatch(a, />switch</); assert.match(a, />sign in again</);
});

test("fact line counts a signed_out seat as not ready even when its status says ready", () => {
const html = buildHTML(claudeState(seat({ status: "ready", signed_out: true, usage: { error: "no_token" } })));
assert.match(factsOf(html), /1 needs you/);
assert.match(rosterOf(html), /then<\/b>[\s\S]*nothing ready/);
assert.doesNotMatch(rosterOf(html), /data-action="switch"|all good/);
});

// --- in-place switching and business/personal crossings ---------------------------------------
import { crossingRequests } from "./render.mjs";

test("settings offers the in-place switch toggle, on unless turned off", () => {
  const on = buildSettings({ settings: { theme: "light", strategy: "soonest_back" } });
  assert.match(on, /switch seats without restarting claude/);
  assert.match(on, /data-action="toggle" data-key="in_place_switch" checked/);
  const off = buildSettings({ settings: { theme: "light", strategy: "soonest_back", in_place_switch: false } });
  assert.match(off, /data-key="in_place_switch" >/);
});

test("settings keeps parked claude seats signed in by default and dispatches its toggle", () => {
  const on = buildSettings({ settings: {} });
  assert.match(on, /keep parked claude seats signed in/);
  assert.match(on, /data-action="toggle" data-key="keep_warm" checked/);
  assert.match(buildSettings({ settings: { keep_warm: false } }), /data-key="keep_warm" >/);
  for (const checked of [false, true]) {
    const app = keyApp();
    app.handlers.change({ target: { closest: () => ({ dataset: { key: "keep_warm" }, checked }) } });
    assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))),
      { action: "toggle", key: "keep_warm", value: checked });
  }
});

const crossing = (over = {}) => ({ tool: "claude", from: "w@x", to: "p@x", at: "2026-10-31T10:00:00Z",
  from_name: "Work", to_name: "Home", from_kind: "business", to_kind: "personal", ...over });

test("a limit crossing offers switch now, not now and an automatic switching link", () => {
  const html = buildHTML(state({ pending_crossings: [crossing({ reason: "limit" })] }));
  assert.match(html, /1 needs you/);
  assert.match(html, /Work is out\. continue this session on Home \(personal seat\)\?/);
  for (const scope of ["once", "deny"]) {
    assert.match(html, new RegExp(`data-action="crossing-answer" data-scope="${scope}" data-tool="claude" data-from="w@x" data-to="p@x"`));
  }
  assert.match(html, />switch now</);
  assert.match(html, />not now</);
  assert.match(html, /data-action="crossing-auto" data-scope="once" data-tool="claude" data-from="w@x" data-to="p@x">always switch automatically<\/button>/);
  assert.doesNotMatch(html, /always allow|data-scope="always"/);
  assert.equal(crossingRequests(state({})), "");
  assert.doesNotMatch(buildHTML(state({})), /crossing-answer/);
});

test("a switch crossing asks about the other running session", () => {
  const html = crossingRequests({ pending_crossings: [crossing({ reason: "switch" })] });
  assert.match(html, /you switched claude to Home\. move the other running session too\? \(work seat → personal seat\)/);
  assert.doesNotMatch(html, /is out|continue this session/);
});

test("limit crossings omit a kind already present in the seat name", () => {
  for (const [to_name, to_kind] of [["Business", "business"], ["my personal", "personal"]]) {
    const html = crossingRequests({ pending_crossings: [crossing({ to_name, to_kind })] });
    assert.ok(html.includes(`<span class="issue-text">Work is out. continue this session on ${to_name}?</span>`));
  }
});

test("an unknown kind is said plainly and names are escaped", () => {
  const html = crossingRequests({ pending_crossings: [crossing({ to_name: "<b>x</b>", to_kind: "unknown" })] });
  assert.match(html, /continue this session on &lt;b&gt;x&lt;\/b&gt; \(plan unknown\)/);
});

test("crossing answers reach the bridge as approve_crossing / deny_crossing", () => {
  const html = crossingRequests({ pending_crossings: [crossing()] });
  const buttons = [...html.matchAll(/data-action="crossing-answer" data-scope="(\w+)" data-tool="([^"]+)" data-from="([^"]+)" data-to="([^"]+)"/g)];
  assert.equal(buttons.length, 2);
  const app = keyApp();
  for (const [, scope, tool, from, to] of buttons) app.click({ action: "crossing-answer", scope, tool, from, to });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.slice(-2))), [
    { action: "approve_crossing", tool: "claude", from: "w@x", to: "p@x", scope: "once" },
    { action: "deny_crossing", tool: "claude", from: "w@x", to: "p@x", scope: "deny" },
  ]);
});

test("the automatic switching link enables the setting then approves this crossing once", () => {
  const html = crossingRequests({ pending_crossings: [crossing()] });
  const [, action, scope, tool, from, to] = html.match(/data-action="(crossing-auto)" data-scope="(once)" data-tool="([^"]+)" data-from="([^"]+)" data-to="([^"]+)"/);
  const app = keyApp();
  app.click({ action, scope, tool, from, to });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.slice(-2))), [
    { action: "toggle", key: "crossing_auto", value: true },
    { action: "approve_crossing", tool: "claude", from: "w@x", to: "p@x", scope: "once" },
  ]);
});

test("settings offers automatic business/personal switching, off by default", () => {
  const off = buildSettings({ settings: {} });
  assert.match(off, /switch automatically between business and personal seats/);
  assert.match(off, /data-action="toggle" data-key="crossing_auto" >/);
  assert.match(buildSettings({ settings: { crossing_auto: true } }), /data-key="crossing_auto" checked/);
  assert.match(buildSettings({ settings: { crossing_auto: false } }), /data-key="crossing_auto" >/);
  const app = keyApp();
  for (const checked of [true, false]) {
    app.handlers.change({ target: { closest: () => ({ dataset: { key: "crossing_auto" }, checked }) } });
    assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "toggle", key: "crossing_auto", value: checked });
  }
});

// --- plan tiers ---------------------------------------------------------------------------------
import { tierRequests } from "./render.mjs";

test("settings offers the cross-tier toggle, off by default", () => {
  const off = buildSettings({ settings: { theme: "light", strategy: "soonest_back" } });
  assert.match(off, /switch without restarting across plan tiers/);
  assert.match(off, /data-key="in_place_cross_tier" >/);
  const on = buildSettings({ settings: { theme: "light", strategy: "soonest_back", in_place_cross_tier: true } });
  assert.match(on, /data-action="toggle" data-key="in_place_cross_tier" checked/);
});

test("a pending tier request is a needs-you item with pair, all tiers and no", () => {
  const req = { tool: "claude", a: "max", b: "team", at: "2026-10-31T10:00:00Z" };
  const html = buildHTML(state({ pending_tier_requests: [req] }));
  assert.match(html, /1 needs you/);
  assert.match(html, /models may differ/);
  assert.match(html, /until you say yes, these moves restart the session/);
  assert.match(html, />always for max↔team</);
  assert.match(html, />always for all tiers</);
  for (const scope of ["pair", "all", "deny"]) {
    assert.match(html, new RegExp(`data-action="tier-answer" data-scope="${scope}" data-tool="claude" data-a="max" data-b="team"`));
  }
  assert.equal(tierRequests(state({})), "");
  assert.doesNotMatch(buildHTML(state({})), /tier-answer/);
});

test("tier plan labels and action attributes are escaped", () => {
  const html = tierRequests({ pending_tier_requests: [{ tool: 'claude"', a: '<b>max</b>', b: 'team"' }] });
  assert.match(html, /data-tool="claude&quot;"/);
  assert.match(html, /data-a="&lt;b&gt;max&lt;\/b&gt;"/);
  assert.match(html, /data-b="team&quot;"/);
  assert.match(html, />always for &lt;b&gt;max&lt;\/b&gt;↔team&quot;</);
  assert.doesNotMatch(html, /<b>/);
});

test("tier answers reach the bridge as approve_tier / deny_tier", () => {
  const html = tierRequests({ pending_tier_requests: [{ tool: "claude", a: "max", b: "team" }] });
  const buttons = [...html.matchAll(/data-action="tier-answer" data-scope="(\w+)" data-tool="([^"]+)" data-a="([^"]+)" data-b="([^"]+)"/g)];
  assert.equal(buttons.length, 3);
  const app = keyApp();
  for (const [, scope, tool, a, b] of buttons) app.click({ action: "tier-answer", scope, tool, a, b });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.slice(-3))), [
    { action: "approve_tier", tool: "claude", a: "max", b: "team", scope: "pair" },
    { action: "approve_tier", tool: "claude", a: "max", b: "team", scope: "all" },
    { action: "deny_tier", tool: "claude", a: "max", b: "team", scope: "deny" },
  ]);
});

// Ledger contracts: the warning is readable before opening seat options, and its action stays
// with the affected seat. Names, dates and targets still come from the real snapshot fields.
const factsOf = (html) => html.match(/<(?:button|span) class="chip chip--[^>]*>[\s\S]*?<\/(?:button|span)>/)?.[0] || "";

test("header inlines app/icon.svg at 20px with an accessible name and no visible brand text", () => {
  const html = buildHTML(state());
  const header = html.match(/<header class="top">[\s\S]*?<\/header>/)[0];
  const icon = header.match(/<svg[\s\S]*?<\/svg>/)[0];
  const original = readFileSync(new URL("../icon.svg", import.meta.url), "utf8").trim();
  assert.equal(icon.replace('class="app-icon" width="20" height="20" role="img" aria-label="ai guest list" focusable="false"', 'width="1024" height="1024"'), original);
  assert.doesNotMatch(header, />ai guest list</);
  assert.match(header, /aria-label="add a subscription seat or API key"/);
  assert.match(header, /aria-label="app menu"/);
  assert.match(header, /data-action="settings"/);
});

test("fact line and inline sign-in problem escape and name the same seat", () => {
const name = 'Work <img src=x> & "personal"', email = 'seat"<&@example.test';
const html = buildHTML(claudeState(seat({ name, email, signed_out: true }), seat({ email: "ok@x", name: "Healthy" })));
assert.match(factsOf(html), /1 needs you/);
const row = rowOf(html, "seat&quot;&lt;&amp;@example.test");
assert.match(row, /Work &lt;img src=x&gt; &amp; &quot;personal&quot;/);
assert.match(row, /class="issue issue--hard"/);
assert.match(row, /signed out/);
assert.match(row, /data-action="add" data-tool="claude" data-email="seat&quot;&lt;&amp;@example.test">sign in</);
assert.doesNotMatch(row, /<img|data-action="switch"|>\d+%|data-reset-at/);
assert.doesNotMatch(rowOf(html, "ok@x"), /issue--hard|sign in/);
});

test("expiring active sign-in appears in the fact line and inline with its renewal action", () => {
const at = inHours(50);
const html = buildHTML(claudeState(seat({ active: true, status: "active", signin_expiring: true, signin_expires_at: at })));
assert.match(factsOf(html), /renew soon/);
const row = rowOf(html, "work@x.com");
assert.match(row, /class="tag">on now/); assert.match(row, /80<small>%/);
assert.match(row, /class="issue issue--soft"[\s\S]*sign-in ends in[\s\S]*>sign in again</);
assert.doesNotMatch(row, /data-action="switch"/);
assert.ok(row.includes(`data-reset-at="${at}"`));
});

test("all limited includes the selected seat, earliest return, detail actions and paid alternatives", () => {
const first = inHours(1), later = inHours(3);
const snapshot = claudeState(seat({ active: true, status: "active", limited: true, limited_until: later, usage5h: 100 }),
 seat({ email: "personal@x", name: "Personal", status: "queued", limited: true, limited_until: first, usageWeek: 100 }));
snapshot.keys = [keySeat({ label: "Emergency", harness: "claude" })];
const html = buildHTML(snapshot), main = rosterOf(html);
assert.match(factsOf(html), /all limited/); assert.match(html, /mood-resting/);
assert.match(toolOf(html, "claude"), /Personal[\s\S]*in 1h/);
assert.match(toolOf(html, "claude"), /Emergency now, paid/);
const row = rowOf(html, "personal@x");
assert.match(row, /data-clock-prefix="back in"[^>]*>back in 1h</);
assert.doesNotMatch(row, /issue--|limited;|data-action="switch"|switch anyway/);
assert.match(detailsOf(html, "claude", "personal@x"), /data-action="switch" data-tool="claude" data-email="personal@x"[^>]*>switch anyway<\/button>/);
const open = buildHTML(snapshot, null, new Set([JSON.stringify(["claude", "personal@x"])]));
assert.match(detailsOf(open, "claude", "personal@x"), /id="seat-detail-claude-personal%40x">/);
assert.doesNotMatch(rowOf(html, "work@x.com"), /data-action="switch"/);
assert.equal(usable(snapshot.tools.claude.seats[0]), false);
assert.match(main, /data-action="key-terminal" data-id="key-1"/);
assert.doesNotMatch(main, /class="issue /, "limits have one return summary, no duplicate problem line");
snapshot.keys[0].responses_verified = false;
assert.match(toolOf(buildHTML(snapshot), "claude"), /endpoint unproven/);
});

test("fact line counts live approvals and keeps crossing, tier and paid choices available", () => {
  const snapshot = claudeState(seat({ status: "active", active: true }));
  snapshot.pending_crossings = [{ tool: "claude", from: "work@x.com", to: "personal@x", from_name: "Work", to_name: "Personal", from_kind: "business", to_kind: "personal" }];
  snapshot.pending_tier_requests = [{ tool: "claude", a: "Max", b: "Pro" }];
  snapshot.pending_key_switches = [keyPrompt()];
  const html = buildHTML(snapshot);
  assert.match(factsOf(html), /3 needs you/);
  for (const action of ["crossing-answer", "crossing-auto", "tier-answer", "key-answer"]) assert.ok(html.includes(`data-action="${action}"`));
  assert.match(html, />switch now</);
  assert.match(html, />not now</);
  assert.match(html, />always switch automatically</);
  snapshot.pending_crossings = [];
  snapshot.pending_tier_requests = [];
  snapshot.pending_key_switches = [keyPrompt({ expires_at: inHours(-1) }), keyPrompt({ status: "approved" })];
  assert.match(factsOf(buildHTML(snapshot)), /all good/);
});

test("group rules use engine selection, exclude signed-out and limited seats, and name pending approval", () => {
const snapshot = claudeState(seat({ active: true, status: "active" }), seat({ email: "out@x", name: "Out", signed_out: true }),
 seat({ email: "limited@x", name: "Limited", limited: true }), seat({ email: "personal@x", name: "Personal" }), seat({ email: "client@x", name: "Client" }));
const next = () => rosterHTML(snapshot).match(/class="then">([\s\S]*?)<\/p>/)[1];
assert.match(next(), /then<\/b> Personal · business, 80% left/);
assert.doesNotMatch(next(), /Out|Limited/);
snapshot.tools.claude.selection = { available: true, email: "client@x" };
assert.match(next(), /then<\/b> Client · business, 80% left/);
snapshot.pending_crossings = [{ tool: "claude", to: "client@x" }];
assert.match(next(), /needs approval/);
});

test("stale, throttled and unknown readings stay quiet; limited resets stay in their column", () => {
  for (const over of [
    { usage_stale: true, usage_fetched_at: inHours(-1) },
    { usage_stale: true, usage: { error: "rate_limited" } },
    { usage_unknown: true },
  ]) {
    const html = buildHTML(claudeState(seat(over)));
    assert.doesNotMatch(html, /class="issue |checking usage|retrying automatically/);
    if (over.usage_stale) {
      assert.match(detailsOf(html, "claude", "work@x.com"), /last known reading/);
      assert.match(rowOf(html, "work@x.com"), /~80% left/);
    }
    assert.doesNotMatch(rowOf(html, "work@x.com"), /back in now|resets in now/);
  }
  const row = rowOf(buildHTML(claudeState(seat({ limited: true, status: "resting", limited_until: inHours(-1) }))), "work@x.com");
  assert.match(row, /data-past-text="resetting…"[^>]*>resetting…<\/span>/);
  assert.doesNotMatch(row, /class="issue |limited;|back in now|resets in now/);
});

for (const tool of ["codex", "claude"]) {
  test(`${tool} expiring seat keeps both switch and renewal in the main view`, () => {
    const html = buildHTML(state({ tools: { [tool]: { seats: [seat({ signin_expiring: true, signin_expires_at: inHours(50) })] } } }));
    const card = rosterOf(html);
    assert.match(card, /data-action="switch"[^>]*>switch<\/button>/);
    assert.match(card, /data-action="add"[^>]*>sign in again<\/button>/);
    assert.match(card, /sign-in ends in/);
  });
}

test("expiring approvals update the fact count and restore the ledger without an engine poll", () => {
  let tick;
  const app = keyApp({ setInterval: (fn) => { tick = fn; return 1; } });
  const first = keyPrompt(), second = keyPrompt({ id: "prompt-2" });
  app.window.AGL.update(state({ tools: { codex: { seats: [seat()] } }, pending_key_switches: [first, second] }));
  assert.match(factsOf(app.root.innerHTML), /2 needs you/);
  assert.doesNotMatch(app.root.innerHTML, /<table/);
  app.window.AGL.setVisible(true);
  first.expires_at = inHours(-1);
  tick();
  assert.match(factsOf(app.root.innerHTML), /1 needs you/);
  assert.doesNotMatch(app.root.innerHTML, /<table/);
  second.expires_at = inHours(-1);
  tick();
  assert.match(factsOf(app.root.innerHTML), /all good/);
  assert.match(app.root.innerHTML, /class="sheet"/);
  assert.doesNotMatch(app.root.innerHTML, /class="key-confirm"/);
  app.window.AGL.setVisible(false);
});

for (const auth of [
  { status: "ready", auth_error: "refresh_token_revoked" },
  { status: "needs-login", auth_error: "refresh_token_revoked" },
  { status: "active", active: true, usage: { error: "unauthorized" } },
]) test(`signed-in login failure offers sign in: ${auth.status}`, () => {
  const s = seat({ ...auth, signed_out: false, needs_login: true });
  const html = buildHTML(state({ tools: { codex: { seats: [s] }, claude: { seats: [] } } }));
  assert.equal(signedOut(s), false);
  assert.equal(usable(s), false);
  for (const part of [rosterOf(html)]) {
    assert.match(part, /sign-in needed/);
    assert.match(part, /data-action="add"[^>]*>sign in<\/button>/);
    assert.doesNotMatch(part, /signed out|data-action="switch"|class="roster-active"/);
  }
});

for (const revoked of [{ entitlement_revoked: true }, { usage: { error: "forbidden" } }]) {
  test(`revoked subscription has no sign-in affordance: ${JSON.stringify(revoked)}`, () => {
    const s = seat({ signed_out: false, needs_login: true, signin_expiring: true,
      signin_expires_at: inHours(1), ...revoked });
    const html = buildHTML(claudeState(s));
    for (const part of [rosterOf(html)]) {
      assert.match(part, /check your subscription to refresh usage/);
      assert.doesNotMatch(part, /sign-in needed|signed out|data-action="add"|data-action="switch"/);
    }
  });
}

// A click may ask locally, but cannot change credentials or the active marker before consent.
for (const tool of ["codex", "claude"]) {
  for (const [fromKind, toKind, crossingAuto, asks] of [
    ["business", "personal", false, true], ["personal", "business", false, true],
    ["business", "business", false, false], ["personal", "personal", false, false],
    ["business", "personal", true, false], ["unknown", "personal", false, true],
    ["business", "unknown", false, true], ["unknown", "unknown", false, true],
    [undefined, "business", false, true], ["personal", undefined, false, true],
  ]) {
    test(`${tool} crossing click ${fromKind} -> ${toKind}, automatic=${crossingAuto}`, () => {
      const app = keyApp();
      const snapshot = state({ settings: { crossing_auto: crossingAuto }, tools: { [tool]: {
        active: "current@x", seats: [
          seat({ email: "current@x", name: "Current", active: true, status: "active", kind: fromKind }),
          seat({ email: "target@x", name: "Target <&>", kind: toKind }),
        ],
      } } });
      assert.equal(switchNeedsConfirmation(snapshot, tool, "target@x"), asks);
      app.window.AGL.update(snapshot);
      const before = app.sent.length;
      const roster = rosterHTML(snapshot);
      const row = rowOf(roster, "target@x");
      assert.match(row, /class="row-action" data-action="switch"/, "the seat has a real switch button");
      assert.match(row, /<button[^>]*data-action="switch"/);
      app.click({ action: "switch", tool, email: "target@x" });
      assert.equal(snapshot.tools[tool].active, "current@x");
      if (!asks) {
        assert.doesNotMatch(app.root.innerHTML, /class="issue issue--soft switch-confirm"/);
        assert.deepEqual(JSON.parse(JSON.stringify(app.sent.slice(before))), [{ action: "switch", tool, email: "target@x" }]);
        return;
      }
      assert.equal(app.sent.length, before);
      const kind = toKind === "business" ? "work seat" : toKind === "personal" ? "personal seat" : "unknown seat kind";
      assert.ok(app.root.innerHTML.includes(`switch ${tool} to Target &lt;&amp;&gt; (${kind})? running sessions move too`));
      assert.match(toolOf(app.root.innerHTML, tool), /class="issue issue--soft switch-confirm"/);
      assert.match(rowOf(app.root.innerHTML, "current@x"), /on now/);
      app.window.AGL.update(snapshot); // a background push keeps the local question open
      assert.match(app.root.innerHTML, /data-action="switch-cancel"/);
      app.click({ action: "switch-cancel", tool, email: "target@x" });
      assert.equal(app.sent.length, before);
      assert.doesNotMatch(app.root.innerHTML, /data-action="switch-confirm"/);
      app.click({ action: "switch-confirm", tool, email: "target@x" }); // dismissed controls are inert
      assert.equal(app.sent.length, before);
      app.click({ action: "switch", tool, email: "target@x" });
      app.click({ action: "switch-confirm", tool, email: "target@x" });
      assert.deepEqual(JSON.parse(JSON.stringify(app.sent.slice(before))), [
        { action: "switch", tool, email: "target@x", confirmed_crossing: true },
      ]);
      assert.doesNotMatch(app.root.innerHTML, /data-action="switch-confirm"/);
    });
  }
}

// Compact clickdummy layout: row summary, inline decisions, footer and disclosures.
test("each seat summary has a single reset from its tighter window; details retain both", () => {
const five = inHours(2), week = inHours(48), back = inHours(5);
for (const [extra, expected, prefix] of [
 [{ usage5h: 80, usageWeek: 20 }, five, "5h resets in"], [{ usage5h: 20, usageWeek: 80 }, week, "week resets in"],
 [{ usage5h: 80, usageWeek: 80 }, five, "5h resets in"],
 [{ usage: { reported_windows: ["weekly"], windows: { "5h": win(five), weekly: win(week) } } }, week, "week resets in"],
 [{ limited: true, limited_until: back }, back, ""],
]) {
 const main = rosterHTML(claudeState(seat({ active: true, status: "active", usage: { windows: { "5h": win(five), weekly: win(week) } }, ...extra })));
 const answer = visibleMarkup(answerOf(main));
 assert.equal((answer.match(/data-reset-at=/g) || []).length, 1);
 assert.ok(answer.includes(`data-reset-at="${expected}" data-clock-prefix="${prefix}"`));
 const detail = detailsOf(main, "claude", "work@x.com");
 assert.match(detail, / hidden>/); assert.ok(detail.includes(`datetime="${week}"`));
 if (!extra.usage?.reported_windows) assert.ok(detail.includes(`datetime="${five}"`));
 const node = { dataset: { resetAt: expected, clockPrefix: prefix, pastText: "resetting…" } };
 updateClockText({ querySelectorAll: (selector) => selector === "[data-reset-at]" ? [node] : [] });
 assert.equal(node.textContent, `${prefix}${prefix ? " " : ""}${fmtCountdown(expected)}`);
}
});

test("crossing approval follows its target row once, without a separate needs-you block", () => {
const snapshot = claudeState(seat({ email: "w@x", name: "Work", status: "active", kind: "business" }),
 seat({ email: "p@x", name: "Personal", kind: "personal" }), seat({ email: "other@x", name: "Other" }));
snapshot.pending_crossings = [crossing({ to_name: "Personal" })];
const html = buildHTML(snapshot), tool = toolOf(html, "claude");
assert.match(tool, /class="issue issue--soft crossing-confirm"/);
for (const action of ["crossing-answer", "crossing-auto"]) assert.ok(tool.includes(`data-action="${action}"`));
assert.ok(tool.indexOf('class="answer"') < tool.indexOf('crossing-confirm'));
assert.equal((html.match(/class="issue issue--soft crossing-confirm"/g) || []).length, 1);
assert.doesNotMatch(toolOf(html, "codex"), /crossing-answer/);
assert.match(factsOf(html), /1 needs you/);
assert.doesNotMatch(html, /crossing-prompts|crossing-tag|orphan-issues/);
});

test("cross-tier approval follows the matching plan row with all existing choices", () => {
const snapshot = claudeState(seat({ email: "max@x", status: "active", plan: "Max" }), seat({ email: "team@x", name: "Team", plan: "Team" }));
snapshot.pending_tier_requests = [{ tool: "claude", a: "max", b: "team" }];
const html = buildHTML(snapshot), tool = toolOf(html, "claude");
for (const scope of ["pair", "all", "deny"]) assert.ok(tool.includes(`data-action="tier-answer" data-scope="${scope}"`));
assert.equal((html.match(/class="issue issue--soft tier-confirm"/g) || []).length, 1);
assert.doesNotMatch(toolOf(html, "codex"), /tier-answer/);
assert.ok(tool.indexOf('class="answer"') < tool.indexOf('tier-confirm'));
});

test("one compact fact groups sign-in problems by tool and gives an approval question", () => {
const snapshot = claudeState(seat({ name: "Personal", signed_out: true }),
 seat({ email: "b@x", name: "Business", status: "active", signin_expiring: true, signin_expires_at: inHours(48) }));
let html = buildHTML(snapshot);
assert.match(factsOf(html), /1 needs you/);
assert.match(toolOf(html, "claude"), /Personal[\s\S]*signed out/);
assert.match(toolOf(html, "claude"), /Business[\s\S]*sign-in ends in/);
assert.equal(elements(toolOf(html, "claude"), "div", (tag) => tag.includes('class="issue ')).length, 2);
snapshot.pending_crossings = [crossing({ to_name: "Personal" })];
html = buildHTML(snapshot);
assert.match(factsOf(html), /2 needs you/);
assert.doesNotMatch(factsOf(html), /signed out|ends in|<br|<h[12]/);
assert.match(toolOf(html, "claude"), /continue this session/);
assert.match(factsOf(buildHTML(claudeState(seat({ status: "active" })))), /all good/);
});

test("footer keeps only the live status line and the main view has no drawer", () => {
  const snapshot = state({ tools: { codex: { seats: [seat()] } } });
  const html = buildHTML(snapshot), footer = html.match(/<footer class="foot">[\s\S]*?<\/footer>/)[0];
  assert.match(html, /role="menuitemcheckbox" aria-checked="true" data-action="toggle" data-key="auto_switch"/);
  assert.doesNotMatch(footer, /<button|data-action/);
  assert.doesNotMatch(html, /guest-drawer|seat-options/);
  assert.doesNotMatch(css, /guest-drawer|drawer-handle|drawer-content|footer-options/);
  assert.doesNotMatch(readFileSync(new URL("./app.mjs", import.meta.url), "utf8"), /guest-drawer|seat-options|moveDrawer|drawerMotion/);
  assert.match(footer, /auto-switch on, soonest back first/);
  assert.match(buildHTML({ ...snapshot, settings: { auto_switch: true, strategy: "most_headroom" } }), /auto-switch on, most headroom first/);
  const app = keyApp();
  for (const checked of [false, true]) {
    app.handlers.change({ target: { closest: () => ({ dataset: { key: "auto_switch" }, checked }) } });
    assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "toggle", key: "auto_switch", value: checked });
  }
});

test("row detail buttons disclose locally and survive a poll without sending a seat switch", () => {
  for (const [tool, email, snapshot] of [
    ["claude", "work@x.com", claudeState(seat({ status: "active" }))],
    ["key", "key-1", state({ keys: [keySeat()] })],
  ]) {
    const app = keyApp();
    app.window.AGL.update({ ...snapshot, rev: 1 });
    const id = app.root.innerHTML.match(/data-action="seat-details"[^>]*aria-controls="([^"]+)"/)[1];
    const detail = { hidden: true }, attributes = { "aria-controls": id };
    const button = { dataset: { action: "seat-details", tool, email },
      setAttribute: (key, value) => { attributes[key] = value; }, getAttribute: (key) => attributes[key] };
    app.document.getElementById = (key) => key === id ? detail : app.root;
    const click = () => app.handlers.click({ target: { closest: (selector) => selector === "[data-action]" ? button : null } });
    click();
    assert.equal(detail.hidden, false);
    assert.equal(attributes["aria-expanded"], "true");
    app.window.AGL.update({ ...snapshot, rev: 2 });
    assert.ok(app.root.innerHTML.includes(`class="details" id="${id}">`));
    click();
    assert.equal(detail.hidden, true);
    assert.equal(attributes["aria-expanded"], "false");
    assert.equal(app.sent.filter((msg) => ["switch", "key-terminal"].includes(msg.action)).length, 0);
  }
});

test("closing the popover drops open questions, rows and menus, but hiding alone does not", () => {
  const app = keyApp();
  const snapshot = state({ tools: { claude: { active: "current@x", seats: [
    seat({ email: "current@x", name: "Current", active: true, status: "active", kind: "personal" }),
    seat({ email: "target@x", name: "Target", kind: "business" }),
  ] } } });
  const expand = () => {
    const button = { dataset: { action: "seat-details", tool: "claude", email: "target@x" },
      setAttribute() {}, getAttribute: () => "none" };
    app.handlers.click({ target: { closest: (selector) => selector === "[data-action]" ? button : null } });
  };
  const expanded = /data-email="target@x"[^>]*aria-expanded="true"/;
  app.window.AGL.update(snapshot);
  app.click({ action: "switch", tool: "claude", email: "target@x" });
  expand();
  app.window.AGL.update(snapshot); // a poll while open keeps both
  app.window.AGL.setVisible(false); // display sleep or a Space change: the popover may still be up
  app.window.AGL.update(snapshot);
  assert.match(app.root.innerHTML, /data-action="switch-confirm"/);
  assert.match(app.root.innerHTML, expanded);

  app.window.AGL.closed();
  assert.doesNotMatch(app.root.innerHTML, /data-action="switch-confirm"/);
  assert.doesNotMatch(app.root.innerHTML, /aria-expanded="true"/);
  const before = app.sent.length;
  app.click({ action: "switch-confirm", tool: "claude", email: "target@x" }); // no longer armed
  assert.equal(app.sent.length, before);

  // Closing on settings still clears the rows behind it and comes back to the main screen.
  expand();
  app.click({ action: "settings" });
  app.window.AGL.closed();
  assert.doesNotMatch(app.root.innerHTML, /aria-expanded="true"/);
  assert.match(app.root.innerHTML, /data-action="switch"/);
});

test("closing the popover drops the paid-key gate but keeps a key being typed", () => {
  const app = keyApp();
  app.window.AGL.update(state({ keys: [keySeat()] }));
  app.click({ action: "key-terminal", id: "key-1" });
  app.window.AGL.result({ ok: false, code: "paid_use_disabled", key_action: "key_terminal", key_target_id: "key-1", error: "off" });
  assert.match(app.root.innerHTML, /data-action="paid-key-enable"/);
  app.window.AGL.closed();
  assert.doesNotMatch(app.root.innerHTML, /data-action="paid-key-enable"/);

  app.click({ action: "key-terminal", id: "key-1" }); // its reply arrives only after the close
  app.window.AGL.closed();
  app.window.AGL.result({ ok: false, code: "paid_use_disabled", key_action: "key_terminal", key_target_id: "key-1", error: "off" });
  assert.doesNotMatch(app.root.innerHTML, /data-action="paid-key-enable"/);

  app.click({ action: "key-start" });
  const typing = app.root.innerHTML;
  app.window.AGL.closed();
  assert.equal(app.root.innerHTML, typing);
});

test("ledger uses clickdummy row, column, glyph, bar and control dimensions", () => {
assert.match(css, /grid-template-columns:58px 1fr; column-gap:6px; padding:9px 16px 11px/);
assert.match(css, /height:44px; padding:0 8px 0 16px/);
assert.match(css, /\.bar \{ height:6px; margin:7px 0 5px; border-radius:3px/);
assert.match(css, /\.issue \{[\s\S]*?padding:5px 5px 5px 8px; border-radius:9px/);
assert.match(css, /height:26px; padding:0 10px; border-radius:999px/);
assert.match(css, /font:700 28px\/1 var\(--figure\)/);
assert.match(css, /--field-a:#e8f8ec; --field-b:#c4ebdf/);
assert.match(css, /--field-a:#302b20; --field-b:#52412a/);
assert.match(css, /outline:2px solid var\(--focus\)/);
assert.doesNotMatch(css, /\.roster|\.ledger/);
});

test("a selected seat's single past reset summary says resetting while the other reset stays in details", () => {
const past = inHours(-1), future = inHours(48);
const html = rosterHTML(claudeState(seat({ status: "active", usage5h: 90, usageWeek: 20, usage: { windows: { "5h": win(past), weekly: win(future) } } })));
const answer = visibleMarkup(answerOf(html));
assert.match(answer, /data-past-text="resetting…"[^>]*>resetting…<\/span>/);
assert.doesNotMatch(answer, />now<|>in now<|reset-window/);
assert.equal((answer.match(/data-reset-at=/g) || []).length, 1);
assert.ok(!answer.includes(future));
assert.ok(detailsOf(html, "claude", "work@x.com").includes(future));
});

test("header chip answers needs you, all limited, renew soon and all good in priority order", () => {
  const healthy = claudeState(seat({ status: "active" }));
  assert.match(headerVerdict(healthy), /chip--calm[\s\S]*all good/);
  const expired = seat({ signin_expiring: true, signin_expires_at: inHours(48) });
  assert.match(headerVerdict(claudeState(expired)), /chip--soft[\s\S]*renew soon/);
  const resting = seat({ active: true, status: "active", limited: true, limited_until: inHours(1) });
  assert.match(headerVerdict(claudeState(resting)), /chip--soft[\s\S]*all limited/);
  assert.match(headerVerdict(claudeState({ ...resting, ...expired, limited: true })), /all limited/);
  for (const problem of [{ signed_out: true }, { signed_out: false, needs_login: true }, { entitlement_revoked: true }]) {
    const h = headerVerdict(claudeState(seat(problem), resting));
    assert.match(h, /chip--hard[\s\S]*1 needs you/);
    assert.doesNotMatch(h, /all good|all limited|renew soon/);
  }
  const all = { ...healthy, pending_crossings: [crossing()], pending_tier_requests: [{ tool: "claude", a: "max", b: "pro" }], pending_key_switches: [keyPrompt()] };
  assert.match(headerVerdict(all), /3 needs you/);
  all.pending_key_switches[0].expires_at = inHours(-1);
  assert.match(headerVerdict(all), /2 needs you/);
  assert.equal(headerVerdict(state()), "");
  for (const flag of ["usage_stale", "usage_unknown"]) assert.doesNotMatch(headerVerdict(claudeState(seat({ [flag]: true }))), /checking usage/);
  assert.doesNotMatch(buildHTML(healthy).split('<div class="body"')[0], /<h1|<h2/);
});

test("one tool block shows the tighter window, its companion, and the hooked next seat", () => {
  const snapshot = state({ tools: {
    codex: { seats: [seat({ name: "business", plan: "team", status: "active", usage5h: 85, usageWeek: 38,
      usage: { windows: { "5h": win(inHours(1.2)), weekly: win(inHours(92)) } } }),
      seat({ name: "personal", email: "personal@gmail.com", kind: "personal", plan: "plus", usage5h: 0, usageWeek: 0 })] },
    claude: { seats: [seat({ name: "business", plan: "team", status: "active" })] },
  } });
  const html = buildHTML(snapshot), tool = toolOf(html, "codex"), visible = visibleMarkup(tool);
  assert.match(visible, /business · team/);
  assert.match(visible, /15<small>%/);
  assert.match(visible, /of 5h left/);
  assert.match(visible, /style="width:15%"/);
  assert.match(visible, /5h resets in 1h 12m/);
  assert.match(visible, /week 62%/);
  assert.match(visible, /class="then"><b>then<\/b> personal · plus, full/);
  assert.match(visible, /needs approval/);
  assert.equal(elements(rosterOf(html), "section", (tag) => tag.includes('class="tool"')).length, 2);
  const sheet = elements(html, "div", (tag) => tag.includes('class="sheet"'))[0];
  assert.match(sheet, /class="seat-identity"[^>]*>personal<\/span><span class="seat-metadata"[^>]*>personal · plus<\/span>/);
  assert.doesNotMatch(sheet, /work@x.com/);
});

test("block heading keeps only the seat name and disambiguator, with kind and plan in meta", () => {
  const snapshot = claudeState(
    seat({ name: "Studio", email: "me@gmail.com", kind: "business", plan: "team", status: "active",
      usage: { windows: { "5h": win(inHours(3)), weekly: win(inHours(48)) } } }),
    seat({ name: "Studio", email: "me@icloud.com", kind: "personal", plan: "max" }));
  const tool = toolOf(buildHTML(snapshot), "claude");
  const heading = elements(tool, "div", (tag) => tag.includes('class="now"'))[0];
  const meta = elements(tool, "div", (tag) => tag.includes('class="meta"'))[0];
  assert.match(heading, /class="seat-identity"[^>]*>Studio · gmail<\/span>/);
  assert.match(heading, /class="tag">on now<\/span>.*class="fig">80<small>%<\/small>/);
  assert.doesNotMatch(heading, /class="seat-metadata"|>[^<]*(?:business|team)[^<]*</);
  assert.match(meta, /business · team · <span[^>]*>5h resets in 3h<\/span>/);
  const sheet = elements(buildHTML(snapshot), "div", (tag) => tag.includes('class="sheet"'))[0];
  assert.match(sheet, /class="seat-identity"[^>]*>Studio · icloud<\/span><span class="seat-metadata"[^>]*>personal · max<\/span>/);
  assert.match(css, /\.now-seat \{[^}]*flex-wrap:nowrap/);
  assert.match(css, /\.seat-identity \{[^}]*overflow:hidden; text-overflow:ellipsis; white-space:nowrap/);
  assert.match(css, /\.seat-metadata \{[^}]*flex:none;[^}]*font-weight:400;[^}]*white-space:nowrap/);
});

test("seat identity shows kind and plan and disambiguates duplicate names per tool", () => {
  const seats = [seat({ name: "personal", kind: "personal", plan: "max", email: "me@gmail.com" }),
    seat({ name: "personal", kind: "personal", plan: "pro", email: "me@icloud.com" }),
    seat({ name: "personal", kind: "personal", plan: "plus", email: "me+studio@gmail.com" })];
  assert.deepEqual(seats.map((s) => seatIdentity(s, seats)), ["personal · max · gmail", "personal · pro · icloud", "personal · plus · studio"]);
  const sameDomain = [seat({ email: "one@gmail.com" }), seat({ email: "two@gmail.com" })];
  assert.deepEqual(sameDomain.map((s) => seatIdentity(s, sameDomain)), ["Work · business · one", "Work · business · two"]);
  const sameLocal = [seat({ email: "me@example.com" }), seat({ email: "me@example.org" })];
  assert.deepEqual(sameLocal.map((s) => seatIdentity(s, sameLocal)), ["Work · business · me@example.com", "Work · business · me@example.org"]);
  const html = rosterHTML(claudeState(...seats));
  for (const text of ["personal · gmail", "personal · icloud", "personal · studio"]) assert.ok(html.includes(`>${text}</span>`));
  const distinctTools = rosterHTML(state({ tools: { codex: { seats: [seats[0]] }, claude: { seats: [seats[1]] } } }));
  assert.doesNotMatch(distinctTools, /· gmail|· icloud/);
  assert.equal(seatIdentity(seat({ name: "client", kind: "business", plan: "team" })), "client · business · team");
  // Unknown kind or an unrecognised plan is left out, never shown as "unknown".
  assert.equal(seatIdentity(seat({ name: "work", kind: undefined, plan: null })), "work");
  assert.equal(seatIdentity(seat({ name: "client", kind: "business", plan: "Self_Serve_Business_Prolite" })), "client · business");
  assert.match(css, /\.seat-identity \{[^}]*text-overflow:ellipsis; white-space:nowrap/);
  assert.match(css, /\.seat-metadata \{[^}]*flex:none;[^}]*font-weight:400;[^}]*white-space:nowrap/);
});

test("signed-out windows suppress cached percentages and resets, including an active flag", () => {
  const s = seat({ status: "active", signed_out: true, usage: { windows: { "5h": win(inHours(2)), weekly: win(inHours(48)) } } });
  assert.deepEqual(seatWindows(s).map((w) => [w.left, w.reset]), [[null, null], [null, null]]);
  const visible = visibleMarkup(rosterHTML(claudeState(s)));
  assert.doesNotMatch(visible, /class="tag">on now|class="fig"|class="bar"|data-reset-at|\d+%/);
  assert.match(visible, /on now · signed out/);
  assert.match(visible, />sign in</);
});

test("content height reports intrinsic size through agl and shrinks after disclosures close", () => {
  const frames = [], observed = [], observer = {};
  const sizes = { content: 240, top: 44, foot: 32, menu: 0 };
  const root = { innerHTML: "", firstElementChild: { scrollHeight: 600, getBoundingClientRect: () => ({ top: 0 }) },
    querySelector(q) {
      if (q === ".body, .set-body") return { scrollHeight: 600 }; // bounded scrollbox, not the intrinsic reading
      if (q === ".content") return { scrollHeight: sizes.content };
      if (q === ".top, .set-head") return { offsetHeight: sizes.top };
      if (q === ".foot") return { offsetHeight: sizes.foot };
      return null;
    },
    querySelectorAll(q) {
      if (q === '.header-menu .menu:not([hidden])') return sizes.menu ? [{ getBoundingClientRect: () => ({ bottom: sizes.menu }) }] : [];
      if (q === ".app, .content, .set-body, .key-prompts, .model-list") return [this.firstElementChild];
      return [];
    },
  };
  const app = keyApp({ root, requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    ResizeObserver: class { constructor(fn) { observer.change = fn; } disconnect() {} observe(node) { observed.push(node); } },
  });
  const flush = () => { while (frames.length) frames.shift()(); };
  const heights = () => app.sent.filter((m) => m.action === "resize").map((m) => m.height);
  flush(); assert.deepEqual(heights(), [316]);
  assert.ok(observed.length);
  observer.change(); observer.change(); flush(); assert.deepEqual(heights(), [316], "unchanged and duplicate resize notifications are quiet");
  sizes.content = 710; observer.change(); flush(); assert.equal(heights().at(-1), 786, "native applies the 600px clamp");
  sizes.content = 100; observer.change(); flush(); assert.equal(heights().at(-1), 176, "native applies the 220px minimum");
  sizes.menu = 285; observer.change(); flush(); assert.equal(heights().at(-1), 293);
  sizes.menu = 0; observer.change(); flush(); assert.equal(heights().at(-1), 176);
  sizes.content = 241.3; app.window.AGL.update(state()); flush(); assert.equal(heights().at(-1), 318);
  assert.match(css, /max-height:min\(600px,100vh\)/);
  assert.match(css, /\.body \{[^}]*overflow-y:auto/);
});

test("button menus retain their purpose through polls, close on Escape and navigate by keyboard", () => {
  const root = { innerHTML: "", firstElementChild: null, querySelector: () => null, querySelectorAll: () => [] };
  const app = keyApp({ root });
  const controls = ["add", "more"].map((menu) => {
    const items = [0, 1, 2].map(() => ({ focus() { app.document.activeElement = this; } }));
    const nav = { hidden: true, querySelector: () => items[0], querySelectorAll: () => items };
    const parent = { querySelector: () => nav };
    const button = { tagName: "BUTTON", dataset: { action: "menu", menu }, parentElement: parent, attributes: {},
      setAttribute(k, v) { this.attributes[k] = v; }, focus() { app.document.activeElement = this; } };
    return { nav, parent, button, items };
  });
  root.querySelectorAll = (q) => q === '[data-action="menu"]' ? controls.map((c) => c.button) : [];
  root.querySelector = (q) => q === '.header-menu .menu:not([hidden])' ? controls.find((c) => !c.nav.hidden)?.nav
    : q.startsWith('[data-action="menu"][data-menu=') ? controls.find((c) => q.includes(`"${c.button.dataset.menu}"`))?.button : null;
  const click = (control) => app.handlers.click({ target: { closest: (q) => q === "[data-action]" ? control.button : q === ".header-menu" ? control.parent : null }, preventDefault() {} });
  click(controls[0]); assert.equal(controls[0].nav.hidden, false); assert.equal(controls[0].button.attributes["aria-expanded"], "true");
  assert.equal(app.document.activeElement, controls[0].items[0]);
  app.handlers.keydown({ key: "ArrowDown", preventDefault() {} }); assert.equal(app.document.activeElement, controls[0].items[1]);
  app.handlers.keydown({ key: "End", preventDefault() {} }); assert.equal(app.document.activeElement, controls[0].items[2]);
  app.handlers.keydown({ key: "ArrowDown", preventDefault() {} }); assert.equal(app.document.activeElement, controls[0].items[0]);
  app.window.AGL.update(state()); assert.equal(controls[0].nav.hidden, false);
  click(controls[1]); assert.equal(controls[0].nav.hidden, true); assert.equal(controls[1].nav.hidden, false);
  app.handlers.keydown({ key: "Escape", preventDefault() {} });
  assert.equal(controls[1].nav.hidden, true); assert.equal(app.document.activeElement, controls[1].button);
  click(controls[1]); click(controls[1]); assert.equal(controls[1].nav.hidden, true);
  click(controls[0]); app.handlers.click({ target: { closest: () => null } }); assert.equal(controls[0].nav.hidden, true);
  app.handlers.click({ target: { closest: (q) => q === "[data-action]" ? { tagName: "BUTTON", dataset: { action: "toggle", key: "auto_switch" } } : null } });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "toggle", key: "auto_switch", value: false });
});

test("footer confirmations use text, survive a poll and restore the live status", () => {
  const status = { textContent: "" }, classes = new Set(), timers = [];
  const app = keyApp({ setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; } });
  const snapshot = state({ rev: 1 });
  app.root.querySelector = (q) => q === ".footer-status" ? status : q === ".foot" ? { classList: { add: (s) => classes.add(s) } } : null;
  app.window.AGL.result({ state: snapshot, message: '<img src=x> switched' });
  assert.equal(status.textContent, '<img src=x> switched'); assert.ok(classes.has("flash"));
  assert.doesNotMatch(app.root.innerHTML, /<img src=x>/);
  app.window.AGL.update({ ...snapshot, rev: 2 }); assert.equal(status.textContent, '<img src=x> switched');
  timers.find((t) => t.ms === 3000).fn();
  assert.match(app.root.innerHTML, /role="status" aria-live="polite">auto-switch on, soonest back first/);
  app.window.AGL.result({ ok: true, celebrate: true, state: { ...snapshot, rev: 3, moved_note: "codex is on personal now" } });
  assert.equal(status.textContent, "codex is on personal now");
});

test("remove seat lives in every subscription's inline details and asks in place", () => {
  for (const tool of ["codex", "claude"]) {
    for (const status of ["active", "ready", "resting", "queued", "needs-login"]) {
      const email = 'seat"<&@example.test', s = seat({ email, status });
      const snapshot = state({ tools: { [tool]: { seats: [s] } } });
      const html = buildHTML(snapshot), detail = detailsOf(html, tool, email);
      assert.match(detail, /^<div class="details"[^>]* hidden>/);
      assert.ok(detail.includes(`data-action="remove" data-tool="${tool}" data-email="seat&quot;&lt;&amp;@example.test">remove seat</button>`));
      assert.equal((html.match(/data-action="remove"/g) || []).length, 1);
      assert.doesNotMatch(visibleMarkup(html), /data-action="remove"/);
      const opened = buildHTML(snapshot, null, new Set([JSON.stringify([tool, email])]));
      assert.match(visibleMarkup(opened), /data-action="remove"/);
      const app = keyApp({ confirm: () => assert.fail("confirm() does nothing in the app's web view") });
      const button = { dataset: { action: "remove", tool, email }, textContent: "remove seat" };
      const press = () => app.handlers.click({ target: { closest: (selector) => selector === "[data-action]" ? button : null } });
      const sentBefore = app.sent.length;
      press();
      assert.equal(app.sent.length, sentBefore);
      assert.equal(button.textContent, `wave goodbye to ${email}?`);
      press();
      assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "remove", tool, email });
    }
  }
});

test("paid key management lives in the key row's details and preserves each action", () => {
  for (const harness of ["codex", "claude"]) {
    const key = keySeat({ harness, responses_verified: false,
      last_validation: { operation_permitted: true } });
    const snapshot = state({ keys: [key] }), html = buildHTML(snapshot);
    const detail = detailsOf(html, "key", key.id);
    assert.match(detail, /^<div class="details"[^>]* hidden>/);
    assert.match(detail, /key check passed; account access confirmed/);
    for (const action of ["key-validate", "key-remove", ...(harness === "codex" ? ["key-prove"] : [])]) {
      assert.ok(detail.includes(`data-action="${action}" data-id="key-1"`));
      assert.equal((html.match(new RegExp(`data-action="${action}"`, "g")) || []).length, 1);
      assert.ok(!visibleMarkup(html).includes(`data-action="${action}"`));
      const opened = buildHTML(snapshot, null, new Set([JSON.stringify(["key", key.id])]));
      assert.ok(visibleMarkup(opened).includes(`data-action="${action}"`));
    }
    assert.match(visibleMarkup(html), /data-action="key-terminal" data-id="key-1"/);
    if (harness === "codex") assert.match(detail, /this sends a paid request[\s\S]*not now[\s\S]*send paid check/);
    const app = keyApp();
    app.click({ action: "key-validate", id: key.id });
    assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "key_validate", id: key.id });
    const remove = { dataset: { action: "key-remove", id: key.id }, textContent: "remove key" };
    const sentBefore = app.sent.length;
    app.handlers.click({ target: { closest: (q) => q === "[data-action]" ? remove : null } });
    assert.equal(app.sent.length, sentBefore);
    assert.equal(remove.dataset.action, "key-remove-confirm");
    assert.equal(remove.textContent, "remove this key from the list?");
    app.click(remove.dataset);
    assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "key_remove", id: key.id });
  }
});

test("engine and supervision problem lines lead the main view and count in the header chip", () => {
  const warning = { text: "older terminal sessions <old>", action: "adopt_engine" };
  const supervision = { active: false, on_path: false };
  const base = state({ tools: { codex: { seats: [seat({ status: "active" })] } } });
  for (const [extra, count] of [
    [{ engine_warnings: [warning] }, 1],
    [{ supervision }, 1],
    [{ engine_warnings: [warning, { text: "2 older sessions" }], supervision }, 3],
    [{ supervision: { error: "can't read .zshrc", active: false } }, 1],
    [{ supervision: { active: true, on_path: false } }, 1],
  ]) {
    const snapshot = { ...base, ...extra, pending_key_switches: [keyPrompt()] };
    const html = buildHTML(snapshot), main = rosterOf(html);
    assert.match(headerVerdict({ ...base, ...extra }), new RegExp(`${count} needs you`));
    assert.match(headerVerdict(snapshot), new RegExp(`${count + 1} needs you`));
    assert.match(main, /^<div class="content"><div class="app-problems">/);
    const problems = elements(main, "div", (tag) => tag.includes('class="app-problems"'))[0];
    assert.equal(elements(problems, "div", (tag) => tag.includes("app-problem ")).length, count);
    assert.ok(main.indexOf(problems) < main.indexOf('class="key-prompts"'));
    assert.ok(main.indexOf(problems) < main.indexOf('class="field"'));
    assert.ok(!problems.includes("<old>"));
    assert.match(problems, /class="issue issue--(?:hard|soft) app-problem/);
  }
  const html = buildHTML({ ...base, engine_warnings: [warning], supervision });
  assert.match(rosterOf(html), /data-action="engine-adopt"/);
  assert.match(rosterOf(html), /data-action="supervision-on"/);
  assert.match(headerVerdict({ ...base, engine_warnings: [null, {}, { text: "" }] }), /all good/);
  assert.match(headerVerdict({ ...base, supervision: { active: true, on_path: true } }), /all good/);
  assert.match(headerVerdict({ ...base, settings: { supervise_shell: false }, supervision }), /all good/);
  assert.match(headerVerdict(state({ engine_warnings: [warning] })), /1 needs you/);
  assert.match(headerVerdict({ ...claudeState(seat({ signed_out: true })), engine_warnings: [warning], supervision }), /3 needs you/);
  const app = keyApp();
  app.click({ action: "engine-adopt" });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "adopt_engine" });
  app.click({ action: "supervision-on" });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "toggle", key: "supervise_shell", value: true });
  let focused = false, scrolled = false;
  app.root.querySelector = (q) => q === '.issue:not(.update-line) button, .key-confirm button'
    ? { focus() { focused = true; }, scrollIntoView() { scrolled = true; } } : null;
  app.click({ action: "attention" });
  assert.ok(focused && scrolled);
});

test("empty state guidance is in the main view, including with a pending paid approval", () => {
  for (const extra of [{}, { pending_key_switches: [keyPrompt()] }, { pending_crossings: [crossing()] }]) {
    const html = buildHTML(state(extra));
    assert.match(rosterOf(html), /use ＋ above to add a subscription or API key\./);
    assert.equal((html.match(/use ＋ above to add/g) || []).length, 1);
    assert.doesNotMatch(html, /guest-drawer|seat-options/);
  }
  assert.doesNotMatch(rosterHTML(state({ keys: [keySeat()] })), /use ＋ above to add/);
});


test("expanded details group usage, session and account with short local reset times", () => {
  const originalNow = Date.now;
  const now = new Date(2026, 10, 1, 15, 49).getTime();
  Date.now = () => now;
  try {
    const today = new Date(2026, 10, 1, 17, 10).toISOString();
    const later = new Date(2026, 10, 3, 18).toISOString();
    const fetched = new Date(now - 29000).toISOString();
    const started = new Date(now - 11 * 60000).toISOString();
    const last = new Date(2026, 10, 1, 12, 31).toISOString();
    for (const status of ["active", "ready"]) {
      const s = seat({ status, usage5h: 13, usageWeek: 50, in_session: true,
        session_started_at: started, last_on_floor: last, usage_fetched_at: fetched,
        usage: { windows: { "5h": { resets_at: today }, weekly: { resets_at: later } } } });
      const html = buildHTML(state({ tools: { claude: { seats: [s] } } }), null,
        new Set([JSON.stringify(["claude", s.email])]));
      const detail = detailsOf(html, "claude", s.email);
      const groups = elements(detail, "section", (tag) => tag.includes('class="detail-group"'));
      assert.deepEqual(groups.map((group) => group.match(/<h3>(.*?)<\/h3>/)[1]), ["usage", "session", "account"]);
      assert.match(groups[0], /87% left · resets <time[^>]*>5:10 PM<\/time> \(<span[^>]*>in 1h 21m<\/span>\)/);
      assert.match(groups[0], /<time[^>]*>Tue 3 Nov · 6:00 PM<\/time>/);
      assert.match(groups[0], /updated 29s ago/);
      assert.match(groups[0], new RegExp(`data-usage-at="${fetched}"`));
      assert.ok(groups[0].includes(`datetime="${today}"`));
      assert.ok(groups[0].includes(`data-reset-at="${today}"`));
      // Check visible text directly; full timestamps may still appear in title attributes.
      assert.doesNotMatch(detail, />[^<]*(?:11\/1\/2026|2026,|:00:00 PM|updated updated)[^<]*</);
      assert.match(groups[1], /terminal attached<span[^>]*> · 11m<\/span>/);
      assert.match(groups[1], /last used <time[^>]*>12:31 PM<\/time>/);
      assert.match(groups[1], new RegExp(`data-session-at="${started}"`));
      assert.match(groups[2], /<dt>email<\/dt><dd>work@x.com<\/dd><dt>plan<\/dt>/);
      const actions = elements(detail, "div", (tag) => tag.includes('class="detail-actions"'))[0];
      assert.match(actions, /class="row-action destructive" data-action="remove"/);
      assert.ok(detail.endsWith(`${actions}</div>`));
    }
    const idle = detailsOf(buildHTML(state({ tools: { codex: { seats: [seat()] } } })), "codex", "work@x.com");
    assert.doesNotMatch(idle, /aria-label="session"|<h3>session<\/h3>/);
    assert.match(idle, /<h3>usage<\/h3>/);
    assert.match(idle, /<h3>account<\/h3>/);
  } finally { Date.now = originalNow; }
});

test("key details share the inset panel and end with separate management actions", () => {
  const key = keySeat();
  const detail = detailsOf(buildHTML(state({ keys: [key] })), "key", key.id);
  assert.match(detail, /^<div class="details"/);
  assert.match(detail, /<h3>key details<\/h3><dl><dt>provider/);
  assert.doesNotMatch(detail, /<h3>(usage|session|account)<\/h3>/);
  const actions = elements(detail, "div", (tag) => tag.includes('class="detail-actions key-management"'))[0];
  assert.match(actions, /<h3>manage<\/h3>/);
  assert.match(actions, /data-action="key-validate"/);
  assert.match(actions, /class="row-action destructive" data-action="key-remove"/);
  assert.ok(detail.endsWith(`${actions}</div>`));
});

test("one-line seat rows reserve state and action width and truncate metadata before names", () => {
  const longName = "Franz Heinfling with a very long workspace name";
  const snapshot = claudeState(
    seat({ name: longName, status: "active", email: "active@x.com" }),
    seat({ name: longName, email: "next@x.com", plan: "team", usage5h: 16, usageWeek: 16 }));
  const html = buildHTML(snapshot);
  const row = elements(html, "div", (tag) => tag.includes('class="row"'))[0];
  assert.match(row, /class="seat-identity"[^>]*>Franz Heinfling with a very long workspace name/);
  assert.match(row, /class="seat-metadata" title="business · team">business · team<\/span>/);
  assert.match(row, /<span class="state"><span class="state--strong">next<\/span>, 84% left<\/span><button class="row-action" data-action="switch"/);
  // Check the final declarations for selectors that occur more than once in the stylesheet.
  const rule = (selector) => [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((m) => m[1].trim().split(/,\s*/).includes(selector)).map((m) => m[2]).join(" ");
  assert.match(rule(".row"), /display:grid/);
  assert.match(rule(".row"), /grid-template-columns:58px minmax\(0,1fr\) max-content max-content/);
  assert.match(rule(".row"), /column-gap:6px/);
  assert.match(rule(".row .state"), /white-space:nowrap/);
  assert.match(rule(".row-action"), /flex:none/);
  assert.doesNotMatch(rule(".row-action"), /position:\s*(?:absolute|fixed)/);
  assert.doesNotMatch(rule(".row > .row-action"), /position:\s*(?:absolute|fixed)/);
  assert.match(rule(".seat-identity"), /flex:0 1 auto/);
  for (const selector of [".seat-identity", ".seat-metadata"]) {
    for (const declaration of ["min-width:0", "overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"]) {
      assert.ok(rule(selector).includes(declaration), `${selector} needs ${declaration}`);
    }
  }
  assert.match(rule(".seat-metadata"), /flex:none/);
  assert.match(rule(".seat-metadata"), /max-width:calc\(100% - 17px\)/);
  assert.match(rule(".row .seat-identity"), /flex:none/);
  assert.match(rule(".row .seat-identity"), /max-width:calc\(100% - 17px\)/);
  assert.match(rule(".row .seat-metadata"), /flex:0 1 auto/);
  assert.match(rule(".row .seat-metadata"), /max-width:none/);
  // The heading reserves both the on-now badge and the natural-width usage reading.
  const heading = elements(toolOf(html, "claude"), "div", (tag) => tag.includes('class="now"'))[0];
  assert.match(heading, /class="seat-identity"[^>]*>Franz Heinfling/);
  assert.match(heading, /class="tag">on now<\/span>/);
  assert.match(rule(".now"), /grid-template-columns:minmax\(0,1fr\) auto auto/);
  assert.match(rule(".now-seat"), /min-width:0/);
  assert.match(rule(".now-seat"), /flex-wrap:nowrap/);
  assert.match(rule(".now-seat > .seatname"), /flex:0 1 auto/);
  assert.match(rule(".now-seat > .tag"), /flex:none/);
  assert.match(rule(".seatname .chev"), /flex:none/);
});

// --- in-app updater: a calm line in the main view, installed only on click -------------------
const upd = (over = {}) => ({ current: "1.8.0", version: "1.8.1",
  notes_url: "https://github.com/fheinfling/ai-guest-list/releases/tag/v1.8.1", phase: "available", error: null,
  checked_at: "2026-11-08T10:00:00Z", ...over });
const updateLineOf = (html) => elements(html, "div", (tag) => tag.includes("update-line"))[0] || "";

test("update line renders each phase with the right text and button state", () => {
  const base = state({ tools: { codex: { seats: [seat({ status: "active" })] } } });
  assert.equal(updateLineOf(buildHTML(base)), "");
  assert.equal(updateLineOf(buildHTML({ ...base, update: null })), "");
  assert.equal(updateLineOf(buildHTML({ ...base, update: upd({ phase: "someday" }) })), "");
  const available = updateLineOf(buildHTML({ ...base, update: upd() }));
  assert.match(available, /class="issue issue--calm update-line"/);
  assert.match(available, /<span class="issue-text">1\.8\.1 is ready<\/span>/);
  assert.match(available, /<button class="pricing-link" data-action="update-notes">what's new<\/button>/);
  assert.match(available, /<button class="row-action" data-action="update-install">update<\/button>/);
  assert.doesNotMatch(available, /disabled|aria-busy|https:/);
  for (const [phase, text] of [["downloading", "updating to 1.8.1…"], ["verifying", "checking 1.8.1…"],
    ["installing", "installing 1.8.1, the app will reopen"]]) {
    const line = updateLineOf(buildHTML({ ...base, update: upd({ phase }) }));
    assert.match(line, new RegExp(`<span class="issue-text">${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}</span>`), phase);
    assert.match(line, /<button class="row-action" data-action="update-install" disabled aria-busy="true">/, phase);
    assert.doesNotMatch(line, /update-notes/, phase);
  }
  const failed = updateLineOf(buildHTML({ ...base, update: upd({ phase: "failed", error: "signature mismatch" }) }));
  assert.match(failed, /class="issue issue--soft update-line"/);
  assert.match(failed, /couldn't update to 1\.8\.1: signature mismatch/);
  assert.match(failed, /data-action="update-notes">what's new/);
  assert.match(failed, /<button class="row-action" data-action="update-install">try again<\/button>/);
  assert.doesNotMatch(failed, /disabled/);
  assert.match(updateLineOf(buildHTML({ ...base, update: upd({ phase: "failed" }) })), /couldn't update to 1\.8\.1<\/span>/);
  // Sits in the main view after any problem lines, before the seats.
  const main = rosterOf(buildHTML({ ...base, update: upd(), supervision: { active: false } }));
  assert.ok(main.indexOf("app-problems") < main.indexOf('class="app-notes"'));
  assert.ok(main.indexOf('class="app-notes"') < main.indexOf('class="field"'));
});

test("an update never counts toward the header's needs-you chip", () => {
  const base = state({ tools: { codex: { seats: [seat({ status: "active" })] } } });
  for (const phase of ["available", "downloading", "verifying", "installing", "failed"]) {
    assert.match(factsOf(buildHTML({ ...base, update: upd({ phase, error: "x" }) })), /all good/, phase);
    const hard = { ...base, supervision: { active: false }, update: upd({ phase, error: "x" }) };
    assert.match(factsOf(buildHTML(hard)), /1 needs you/, phase);
  }
});

test("update version and error are escaped in the line and the settings footer", () => {
  const hostile = '<img src=x onerror=alert(1)>"';
  const html = buildHTML(state({ update: upd({ version: hostile, phase: "failed", error: hostile }) }));
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(updateLineOf(html), /couldn't update to &lt;img src=x onerror=alert\(1\)&gt;&quot;: &lt;img/);
  const settings = buildSettings(state({ app: { version: "1.8.0" }, update: upd({ version: hostile }) }));
  assert.doesNotMatch(settings, /<img src=x/);
  assert.match(settings, /ai guest list v1\.8\.0 · &lt;img/);
});

test("settings footer names an available update next to the running version", () => {
  assert.match(buildSettings(state({ app: { version: "1.8.0" }, update: upd() })), /<p class="set-ver">ai guest list v1\.8\.0 · 1\.8\.1 available<\/p>/);
  assert.match(buildSettings(state({ app: { version: "1.8.0" } })), /<p class="set-ver">ai guest list v1\.8\.0<\/p>/);
});

test("the more menu checks for updates with force; what's new sends no URL", () => {
  const html = buildHTML(state());
  const more = html.match(/<nav class="menu" role="menu" aria-label="more"[\s\S]*?<\/nav>/)[0];
  assert.match(more, /<button role="menuitem" data-action="update-check">check for updates<\/button>/);
  const app = keyApp();
  app.window.AGL.result({ state: { ...state({ rev: 1 }), update: upd() } });
  app.click({ action: "update-check" });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "update_check", force: true });
  app.click({ action: "update-notes" });
  assert.deepEqual(JSON.parse(JSON.stringify(app.sent.at(-1))), { action: "update_notes" });
  assert.ok(!JSON.stringify(app.sent).includes("https://"));
});

test("update sends update_install once and ignores clicks while an install is under way", () => {
  const app = keyApp();
  const installs = () => app.sent.filter((m) => m.action === "update_install");
  const push = (rev, update) => app.window.AGL.result({ state: { ...state({ rev }), update } });
  push(1, null);
  app.click({ action: "update-install" });
  assert.equal(installs().length, 0);                       // nothing known, nothing to install
  push(2, upd());
  app.click({ action: "update-install" });
  assert.deepEqual(JSON.parse(JSON.stringify(installs())), [{ action: "update_install" }]);
  assert.match(updateLineOf(app.root.innerHTML), /updating to 1\.8\.1…[\s\S]*disabled aria-busy="true"/);
  app.click({ action: "update-install" });
  push(2, upd());                                           // a same-rev poll still reading "available"
  app.click({ action: "update-install" });
  assert.equal(installs().length, 1);
  for (const phase of ["downloading", "verifying", "installing"]) {
    push(3, upd({ phase }));
    app.click({ action: "update-install" });
  }
  assert.equal(installs().length, 1);
  push(4, upd({ phase: "failed", error: "network" }));      // failure offers a retry
  assert.match(updateLineOf(app.root.innerHTML), /couldn't update to 1\.8\.1: network/);
  app.click({ action: "update-install" });
  app.click({ action: "update-install" });
  assert.equal(installs().length, 2);
});

test("reduceReply flashes each update_check outcome without a raw error", () => {
  const flashFor = (update_check, extra = {}) => reduceReply(UI(), { ok: true, update_check, ...extra }).flash;
  assert.equal(flashFor({ ok: true, up_to_date: true, version: "1.8.0", error: null }), "you're on the latest version");
  assert.equal(flashFor({ ok: true, up_to_date: false, version: "1.8.1", error: null }), "1.8.1 is available");
  assert.equal(flashFor({ ok: false, up_to_date: false, version: null, error: "timed out" }), "couldn't check for updates");
  assert.equal(flashFor({ ok: false, error: "timed out" }, { ok: false, error: "timed out" }), "couldn't check for updates");
  assert.equal(flashFor({ ok: true, up_to_date: false, version: null, error: null }), "couldn't check for updates");
  assert.equal(flashFor({ ok: true, up_to_date: true }, { background: true }), null);
});


for (const tool of ["codex", "claude"]) {
  for (const error of ["network", "rate_limited", "tls", "internal", "parse", "http_503"]) {
    test(`${tool} ${error} quietly preserves stale figures and accessible provenance`, () => {
      const html = buildHTML(state({ tools: { [tool]: { seats: [seat({ status: "active",
        active: true, usage_stale: true, usage_fetched_at: inHours(-1), usage: { error },
      })] } } }));
      assert.doesNotMatch(html, /class="issue |checking usage|retrying automatically/);
      assert.match(html, /~80<small>%/);
      assert.match(html, /week ~90%/);
      assert.match(detailsOf(html, tool, "work@x.com"), /last known reading;.*updated 1h ago/);
    });
  }
}

test("unknown figures remain dashes without an attention row", () => {
  const html = buildHTML(claudeState(seat({ status: "active", usage_unknown: true,
    usage5h: null, usageWeek: null })));
  assert.match(html, /class="fig">—</);
  assert.doesNotMatch(html, /class="issue |checking usage/);
});

test("a soft engine notice is calm and not counted; hard and severity-less warnings still need you", () => {
  const base = state({ tools: { codex: { seats: [seat({ status: "active" })] } } });
  const soft = { kind: "sessions", severity: "soft", text: "A Claude terminal on b@x.com (started 19:23 in ~/Work/now) runs engine 1.6.1." };
  const banner = engineBanner({ ...base, engine_warnings: [soft] });
  assert.match(banner, /class="issue issue--soft app-problem engine-problem" role="status"/);
  assert.match(banner, /started 19:23 in ~\/Work\/now/);
  assert.doesNotMatch(banner, /issue--hard|role="alert"/);
  assert.doesNotMatch(headerVerdict({ ...base, engine_warnings: [soft] }), /needs you/);
  for (const hard of [{ ...soft, severity: "hard" }, { text: soft.text }]) {
    assert.match(engineBanner({ ...base, engine_warnings: [hard] }), /issue--hard app-problem engine-problem" role="alert"/);
    assert.match(headerVerdict({ ...base, engine_warnings: [hard, soft] }), /1 needs you/);
  }
});

test("a seat with no reading leaves no dangling separators or unknown labels", () => {
  const html = buildHTML(state({ tools: { codex: { seats: [
    seat({ status: "active", kind: undefined, plan: "Self_Serve_Business_Prolite", usage5h: null, usageWeek: null, usage: { windows: {} } }),
    seat({ name: "next one", email: "n@x.com", kind: "business", plan: "team", usage5h: null, usageWeek: null, usage: { windows: {} } }),
    seat({ name: "third one", email: "third@x.com", kind: undefined, plan: undefined, usage5h: null, usageWeek: null, usage: { windows: {} } }),
  ] } } }));
  const visible = visibleMarkup(rosterOf(html));
  assert.doesNotMatch(visible, /unknown/);
  assert.doesNotMatch(visible, /<span> · |· <\/span>|, <\/span>|, \(needs approval\)/);
  assert.match(visible, /class="then"><b>then<\/b> next one · business · team <span class="muted">\(needs approval\)/);
  assert.match(visible, /<span class="state"><span class="state--strong">next<\/span><\/span>/);
  assert.doesNotMatch(visible, /<span(?: class="(?:seat-metadata|state)"[^>]*)?>\s*<\/span>|<div class="meta">\s*<\/div>/);
  assert.doesNotMatch(rowOf(html, "third@x.com"), /class="state"|class="seat-metadata"/);
});

test("limited seats without a usable reset show no time and keep their limited status", () => {
  for (const status of ["resting", "queued", "active"]) {
    for (const limited_until of [null, "invalid"]) {
      for (const usage_stale of [false, true]) {
        const limited = seat({ status, limited: true, limited_until, usage_stale });
        const html = buildHTML(state({ tools: { codex: { seats: [limited] } } }));
        const answer = visibleMarkup(answerOf(html));
        assert.match(answer, /class="fig fig--time">—<\/span><span class="fig-l">limited<\/span>/);
        assert.doesNotMatch(answer, /unknown|until back|data-reset-at|approximate, last known reading/);
        const roster = buildHTML(state({ tools: { codex: { seats: [
          seat({ active: true, status: "active", email: "active@x.com" }),
          { ...limited, status: status === "active" ? "resting" : status },
        ] } } }));
        assert.match(rowOf(roster, limited.email), /class="state">limited<\/span>/);
        assert.doesNotMatch(visibleMarkup(rosterOf(roster)), /unknown|back in|until back|limited,\s*</);
      }
    }
  }
});
